"""Explicit synthetic load-shape hypotheses over an existing feeder topology."""
from collections import defaultdict
import math

import networkx as nx


def _depths(feeder):
    graph = nx.Graph()
    graph.add_nodes_from(bus.id for bus in feeder.buses)
    graph.add_edges_from((line.bus1, line.bus2) for line in feeder.lines)
    if feeder.source_bus not in graph or not nx.is_tree(graph):
        raise ValueError('Load shapes require a connected radial feeder')
    depths = nx.single_source_shortest_path_length(graph, feeder.source_bus)
    maximum = max(depths.values(), default=0)
    if not maximum or any(load.bus not in depths or load.bus == feeder.source_bus
                          or load.kw <= 0 for load in feeder.loads):
        raise ValueError('Load shapes require positive non-source load sites')
    return depths, maximum


def apply_load_shape(feeder, spec):
    """Reweight positive sites, retaining occupancy and each electrical ratio.

    Directional weights multiply the incoming heterogeneous allocation by
    exp(+/- concentration * graph_depth / max_graph_depth). Geometry and line
    lengths do not define depth. The default leaves incoming loads untouched.
    """
    shape = spec.scenario.load_shape
    concentration = spec.scenario.load_concentration
    if shape not in {'heterogeneous', 'uniform', 'downstream_heavy', 'upstream_heavy'}:
        raise ValueError('Unknown load shape')
    if not math.isfinite(concentration) or not 0 <= concentration <= 4:
        raise ValueError('Load concentration must lie between 0 and 4')
    depths, maximum = _depths(feeder)
    original = [load.kw for load in feeder.loads]
    total = math.fsum(original)
    if not total or not math.isfinite(feeder.total_kw) or feeder.total_kw <= 0:
        raise ValueError('Load shapes require a finite positive total load')
    site_kw = defaultdict(float)
    for load in feeder.loads:
        site_kw[load.bus] += load.kw
    weights = []
    for load in feeder.loads:
        depth = depths[load.bus]/maximum
        if shape == 'uniform':
            weights.append(load.kw/site_kw[load.bus])
        elif shape == 'downstream_heavy':
            weights.append(load.kw*math.exp(concentration*depth))
        elif shape == 'upstream_heavy':
            weights.append(load.kw*math.exp(-concentration*depth))
        else:
            weights.append(load.kw)
    if shape != 'heterogeneous':
        weight_total = math.fsum(weights)
        updated = [feeder.total_kw*weight/weight_total for weight in weights]
        # Put the rounding residual on the largest load to protect positivity.
        largest = max(range(len(updated)), key=updated.__getitem__)
        updated[largest] += feeder.total_kw-math.fsum(updated)
        for load, previous, kw in zip(feeder.loads, original, updated):
            scale = kw/previous
            load.kw = kw
            load.kvar *= scale
            load.pv_kw *= scale
            load.contract_kva *= scale
    final_total = math.fsum(load.kw for load in feeder.loads)
    rows = [dict(load_id=load.id, bus=load.bus, depth_edges=depths[load.bus],
                 normalized_depth=depths[load.bus]/maximum,
                 baseline_share=previous/total, share=load.kw/final_total)
            for load, previous in zip(feeder.loads, original)]
    evidence = dict(method='synthetic_graph_depth_reweighting_v1', shape=shape,
        concentration=concentration, max_depth_edges=maximum,
        positive_site_count=len(site_kw), loads=rows,
        baseline_weighted_normalized_depth=math.fsum(row['baseline_share']*row['normalized_depth'] for row in rows),
        weighted_normalized_depth=math.fsum(row['share']*row['normalized_depth'] for row in rows),
        depth_definition='Number of graph edges from the source, divided by maximum feeder depth.',
        hypothesis={'heterogeneous': 'Retain the existing heterogeneous allocation.',
                    'uniform': 'Equal active power at each existing positive load site.',
                    'downstream_heavy': 'Multiply existing weights by exp(concentration * normalized_depth).',
                    'upstream_heavy': 'Multiply existing weights by exp(-concentration * normalized_depth).'}[shape],
        boundary='Explicit synthetic hypothesis, not an empirical population distribution. '
                 'Directional concentration is relative to the same incoming allocation; individual loads need not rank by depth.')
    feeder.design_evidence['load_shape'] = evidence
    return evidence


def load_shape_matches(feeder, spec):
    """Check equality or measured directional depth association, not load ranks."""
    shape = spec.scenario.load_shape
    if shape == 'heterogeneous':
        return True
    if not feeder.loads:
        return False
    site_kw = defaultdict(float)
    for load in feeder.loads:
        site_kw[load.bus] += load.kw
    if shape == 'uniform':
        average = math.fsum(site_kw.values())/len(site_kw)
        return all(math.isclose(kw, average, rel_tol=1e-9, abs_tol=1e-9)
                   for kw in site_kw.values())
    evidence = feeder.design_evidence.get('load_shape', {})
    if (evidence.get('shape') != shape
            or evidence.get('concentration') != spec.scenario.load_concentration):
        return False
    try:
        depths, maximum = _depths(feeder)
        baseline = {row['load_id']: row for row in evidence['loads']}
        if set(baseline) != {load.id for load in feeder.loads}:
            return False
        if any(baseline[load.id]['bus'] != load.bus for load in feeder.loads):
            return False
        before = math.fsum(baseline[load.id]['baseline_share']*depths[load.bus]/maximum
                           for load in feeder.loads)
        total = math.fsum(load.kw for load in feeder.loads)
        after = math.fsum(load.kw/total*depths[load.bus]/maximum for load in feeder.loads)
    except (ValueError, KeyError, TypeError, ZeroDivisionError):
        return False
    if spec.scenario.load_concentration == 0:
        return math.isclose(after, before, rel_tol=1e-9, abs_tol=1e-9)
    if shape == 'downstream_heavy':
        return after >= before-1e-9
    if shape == 'upstream_heavy':
        return after <= before+1e-9
    return False
