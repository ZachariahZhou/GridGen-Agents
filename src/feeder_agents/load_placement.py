"""Finite-reference conditional load placement; no fitted population claims."""
from collections import Counter, defaultdict
from copy import deepcopy
import math
import random

import networkx as nx

from .artifacts import digest
from .references import extract_joint_features, get_reference
from .schemas import Load


PRIOR_STRENGTH = 2.0


def reference_occupancy(case_id):
    """Validate a packaged reference and expose its non-source occupancy."""
    entry = get_reference(case_id)
    case = entry['case']
    for table in ('bus', 'gen', 'branch'):
        if any(not isinstance(value, (int, float)) or not math.isfinite(value)
               for row in case[table] for value in row):
            raise ValueError('Reference contains nonfinite electrical data')
    if not math.isfinite(case['base_mva']) or case['base_mva'] <= 0:
        raise ValueError('Reference base MVA must be finite and positive')
    if any(row[2] < 0 or row[3] < 0 for row in case['bus']):
        raise ValueError('Reference requires nonnegative source and non-source loads')
    features = extract_joint_features(case)
    if features['status'] != 'extracted_not_fitted':
        raise ValueError('Conditional load placement requires a single-source radial reference')
    nodes = features['nodes']
    candidates = [row for row in nodes if row['role'] != 'source']
    positive_count = sum(row['p_kw'] > 0 for row in candidates)
    if not candidates or not positive_count:
        raise ValueError('Reference has no positive non-source loads')
    return dict(case_id=case_id, fraction=positive_count / len(candidates),
                reference_hash=digest(case), provenance=deepcopy(entry['provenance']),
                positive_count=positive_count, non_source_count=len(candidates),
                nodes=deepcopy(nodes))


def _bin(children, depth, max_depth):
    child_class = str(children) if children < 2 else '2+'
    quartile = min(3, int(4 * depth / max_depth))
    return child_class, quartile


def _label(key):
    return f'child{key[0]}_depth{key[1]}'


def sample_reference_loads(feeder, spec, target_load_count, seed):
    """Replace only loads using conditional occupancy and empirical magnitudes.

    Occupancy probabilities are weights for a fixed-size sample, not independent
    Bernoulli probabilities or the marginal inclusion probabilities after fixing
    the count. Smoothing and quartile boundaries are declared research choices.
    """
    bus_ids = [bus.id for bus in feeder.buses]
    non_source_count = len(bus_ids) - 1
    if (isinstance(target_load_count, bool) or not isinstance(target_load_count, int)
            or not 1 <= target_load_count <= non_source_count):
        raise ValueError('target load count must be an integer from 1 to the non-source bus count')
    if not math.isfinite(feeder.total_kw) or feeder.total_kw <= 0:
        raise ValueError('Feeder total load must be finite and positive')
    if (not math.isfinite(spec.power_factor) or not 0 < spec.power_factor <= 1
            or not math.isfinite(spec.pv_ratio) or spec.pv_ratio < 0):
        raise ValueError('Invalid power factor or PV ratio')
    graph = nx.Graph()
    graph.add_nodes_from(bus_ids)
    graph.add_edges_from((line.bus1, line.bus2) for line in feeder.lines)
    if (len(set(bus_ids)) != len(bus_ids) or set(graph) != set(bus_ids)
            or feeder.source_bus not in graph or len(feeder.lines) != len(bus_ids) - 1
            or not nx.is_tree(graph)):
        raise ValueError('Target feeder must be a single-source radial tree with unique buses')
    tree = nx.bfs_tree(graph, feeder.source_bus)
    depths = nx.single_source_shortest_path_length(tree, feeder.source_bus)
    max_depth = max(depths.values())
    occupancy = reference_occupancy(spec.scenario.reference_case_id)
    reference_nodes = occupancy['nodes']
    reference_max_depth = max(row['depth_edges'] for row in reference_nodes)
    bins, positive_bins, positive_classes = defaultdict(list), defaultdict(list), defaultdict(list)
    positives = []
    for row in reference_nodes:
        if row['role'] == 'source':
            continue
        key = _bin(row['children'], row['depth_edges'], reference_max_depth)
        bins[key].append(row)
        if row['p_kw'] > 0:
            positives.append(row)
            positive_bins[key].append(row)
            positive_classes[key[0]].append(row)
    # A separate stream leaves topology/geometry and the parent generator RNG intact.
    rng = random.Random(f'load-placement-v1:{seed}:{occupancy["reference_hash"]}')
    targets = []
    for bus_id in sorted(bus_ids):
        if bus_id == feeder.source_bus:
            continue
        key = _bin(tree.out_degree(bus_id), depths[bus_id], max_depth)
        probability = (len(positive_bins[key]) + PRIOR_STRENGTH * occupancy['fraction']) / (len(bins[key]) + PRIOR_STRENGTH)
        targets.append(dict(bus_id=bus_id, children=tree.out_degree(bus_id),
                            depth_edges=depths[bus_id], normalized_depth=depths[bus_id]/max_depth,
                            bin=_label(key), occupancy_probability=probability,
                            selection_key=-math.log(1-rng.random())/probability,
                            selected=False, reference_bus_id=None, raw_weight_kw=None,
                            fallback=None, _key=key))
    selected = sorted(targets, key=lambda row: (row['selection_key'], row['bus_id']))[:target_load_count]
    fallbacks = Counter()
    for row in selected:
        key = row['_key']
        if positive_bins[key]:
            pool, fallback = positive_bins[key], 'same_bin'
        elif positive_classes[key[0]]:
            pool, fallback = positive_classes[key[0]], 'same_child_class'
        else:
            pool, fallback = positives, 'global_positive'
        chosen = rng.choice(pool)
        row.update(selected=True, reference_bus_id=chosen['bus_id'],
                   raw_weight_kw=chosen['p_kw'], fallback=fallback)
        fallbacks[fallback] += 1
    weight_sum = math.fsum(row['raw_weight_kw'] for row in selected)
    tangent = math.tan(math.acos(spec.power_factor))
    loads = []
    for i, row in enumerate(sorted(selected, key=lambda row: row['bus_id']), 1):
        kw = feeder.total_kw * row['raw_weight_kw'] / weight_sum
        row['normalized_kw'] = kw
        loads.append(Load(id=f'load{i}', bus=row['bus_id'], kw=kw, kvar=kw*tangent,
                          pv_kw=kw*spec.pv_ratio, contract_kva=kw/spec.power_factor))
    all_keys = sorted(set(bins) | {row['_key'] for row in targets})
    bin_statistics = { _label(key): dict(reference_count=len(bins[key]),
        reference_positive_count=len(positive_bins[key]),
        occupancy_probability=(len(positive_bins[key])+PRIOR_STRENGTH*occupancy['fraction'])/(len(bins[key])+PRIOR_STRENGTH))
        for key in all_keys }
    for row in targets:
        del row['_key']
    evidence = dict(method='reference_conditioned_empirical', case_id=occupancy['case_id'],
        reference_hash=occupancy['reference_hash'], provenance=occupancy['provenance'],
        reference_positive_count=occupancy['positive_count'],
        reference_non_source_count=occupancy['non_source_count'],
        reference_occupancy_fraction=occupancy['fraction'], reference_nodes=reference_nodes,
        target_positive_count=target_load_count, target_non_source_count=non_source_count,
        target_zero_load_junction_count=non_source_count-target_load_count,
        prior_strength=PRIOR_STRENGTH, depth_bins='min(3, floor(4 * depth_edges / max_depth_edges))',
        bin_statistics=bin_statistics, fallback_counts=dict(fallbacks), target_nodes=targets,
        seed_stream=f'load-placement-v1:{seed}',
        boundary='Finite-reference empirical conditioning; not a learned population distribution or confidence interval. '
                 'Occupancy probabilities are selection weights, not fixed-count marginal inclusion probabilities. '
                 'Prior strength 2 and depth quartiles are research choices. Reference Q and PV are not transferred; '
                 'explicit target power factor and PV ratio are preserved.')
    feeder.loads = loads
    return evidence
