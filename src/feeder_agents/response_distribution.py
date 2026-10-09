"""Catalogue path moves and explicitly permitted, bounded spatial scaling."""
import math
from itertools import zip_longest

import networkx as nx

from .rules import data


def compatible_conductors(line, spec, catalogue):
    current = catalogue[line.conductor]
    return {name: c for name, c in catalogue.items() if c.get('selectable', True) and (
        (c.get('source_feeder') in spec.equipment_design.reference_feeders
         and c.get('construction') == line.construction and c.get('nphases') == len(line.phases))
        if 'source_feeder' in current else 'source_feeder' not in c)}


def probe_lines(feeder, probe):
    graph = nx.Graph()
    graph.add_edges_from((e.bus1, e.bus2, dict(id=e.id)) for e in feeder.lines)
    paths = set()
    for bus in probe['injection_buses'] + probe['monitor_buses']:
        nodes = nx.shortest_path(graph, feeder.source_bus, bus)
        paths.update(graph[a][b]['id'] for a, b in zip(nodes, nodes[1:]))
    return [e for e in feeder.lines if e.id in paths]


def conductor_options(model, spec, probes, assessment):
    catalogue = data('conductors.json'); suggestions = []
    by_id = {p['id']: p for p in probes}
    for row in assessment['targets']:
        if row['passed']: continue
        p = by_id[row['target']['probe_id']]
        component = 'r1' if p['kind'] == 'voltage_p' else 'x1'
        increase = row['observed'] is not None and row['observed'] < (row['target']['lower'] or 0)
        extreme_moves = []
        for line in probe_lines(model['feeder'], p):
            current = catalogue[line.conductor]; compatible = []
            for name, c in compatible_conductors(line, spec, catalogue).items():
                if name == line.conductor: continue
                ratio = c[component]/current[component]
                if (increase and ratio <= 1) or (not increase and ratio >= 1): continue
                effect = line.length_km*abs(c[component]-current[component])
                compatible.append((abs(math.log(ratio/(1.4 if increase else 1/1.4))), name, ratio, effect))
            for _, name, ratio, effect in sorted(compatible)[:3]:
                suggestions.append(dict(kind='replace_conductor', line_id=line.id, conductor=name,
                    probe_id=p['id'],proxy_effect=effect, diagnosis=f'{p["id"]}: change {component} on the fixed path; catalogue ratio {ratio:.4g}; AC verification required'))
            if compatible:
                _, name, _, effect = max(compatible, key=lambda x: (x[3], x[1]))
                extreme_moves.append(dict(line_id=line.id, conductor=name, proxy_effect=effect))
        # Coordinated path moves avoid spending a full round on each small
        # catalogue increment. Whole tuples only; preview and gates are unchanged.
        extreme_moves.sort(key=lambda a: (-a['proxy_effect'], a['line_id']))
        for size in sorted({2, 4, len(extreme_moves)}):
            if not 2 <= size <= len(extreme_moves): continue
            moves = extreme_moves[:size]
            suggestions.append(dict(kind='replace_conductor',probe_id=p['id'],
                replacements=[{k: v for k, v in a.items() if k != 'proxy_effect'} for a in moves],
                proxy_effect=sum(a['proxy_effect'] for a in moves),
                diagnosis=f'{p["id"]}: coordinate {size} compatible whole-tuple replacements on the fixed path; {component} is a ranking proxy, not an AC bound'))
    return sorted(suggestions, key=lambda a: -a['proxy_effect'])


def layout_options(model, original, spec, assessment, search):
    if 'scale_layout' not in search.allowed_actions: return []
    feeder = model['feeder']; reference = original['feeder']
    scale = feeder.lines[0].length_km/reference.lines[0].length_km
    bound = search.max_layout_scale_change
    lo = max((1-bound)/scale, max(spec.segment_km_min/e.length_km for e in feeder.lines))
    hi = min((1+bound)/scale, min(spec.segment_km_max/e.length_km for e in feeder.lines))
    actions = []
    for row in assessment['targets']:
        value = row['observed']
        if row['passed'] or value is None or value <= 1e-12: continue
        lower, upper = row['target']['lower'], row['target']['upper']
        increase = lower is not None and value < lower
        width = upper-lower if lower is not None and upper is not None else .1*value
        desired = ((lower+.2*width) if increase else (upper-.2*width))/value
        for weight in (1., .75, 1.25):
            factor = min(hi, max(lo, 1+(desired-1)*weight))
            if lo > hi or (increase and factor <= 1+1e-9) or (not increase and factor >= 1-1e-9): continue
            actions.append(dict(kind='scale_layout', factor=factor, original_scale=scale*factor,probe_id=row['target']['probe_id'],
                proxy_effect=0., diagnosis='Explicitly permitted uniform scaling of generated coordinates and all line lengths; source anchored; original scale bound and segment limits enforced; AC verification required'))
    return actions


def catalogue_diagnosis(model, spec, probes, assessment):
    """Evidence about the enumerated path moves, never a physical upper bound."""
    catalogue = data('conductors.json'); evidence = []
    by_id = {p['id']: p for p in probes}
    for row in assessment['targets']:
        if row['passed']: continue
        probe = by_id[row['target']['probe_id']]
        component = 'r1' if probe['kind'] == 'voltage_p' else 'x1'
        current = smallest = largest = 0.; types = set(); count = 0
        for line in probe_lines(model['feeder'], probe):
            compatible = compatible_conductors(line, spec, catalogue)
            types.update(compatible); count += 1
            current += line.length_km*catalogue[line.conductor][component]
            smallest += line.length_km*min(c[component] for c in compatible.values())
            largest += line.length_km*max(c[component] for c in compatible.values())
        evidence.append(dict(probe_id=probe['id'], observed=row['observed'], requested=row['target'],
            path_lines=count, compatible_equipment_types=len(types), ranking_component=component,
            path_component_ohm=current, catalogue_path_component_range_ohm=[smallest, largest],
            scope='Path-impedance proxy range; not an AC response envelope or infeasibility proof'))
    return evidence


def rank_distribution_actions(model, probes, assessment, actions, *, preserve_diversity=False):
    """Rank previews by a calibrated radial-path proxy, never by an AC bound.

    Voltage response at a monitored port depends on the source path shared
    with each injection port. Estimate the change to that shared impedance
    under the complete proposal, then score every original target together.
    Exact circuit solves and the original-reference contract remain mandatory.
    Before joint construction, preserve every family and target port so the
    easier goal cannot fill the entire pool of elementary moves to be paired.
    """
    if not actions or model['kind'] != 'distribution':
        return actions
    feeder = model['feeder']
    catalogue = data('conductors.json')
    initial = {line.id: (line.bus1, line.bus2, line.length_km, line.conductor)
               for line in feeder.lines}
    graph = nx.Graph((line.bus1, line.bus2) for line in feeder.lines)
    tree = nx.bfs_tree(graph, feeder.source_bus)
    subtree_lines = {}
    for child in tree:
        descendants = nx.descendants(tree, child) | {child}
        subtree_lines[child] = {line.id for line in feeder.lines
                                if line.bus1 in descendants or line.bus2 in descendants}

    def path_components(lines):
        paths_graph = nx.Graph()
        paths_graph.add_edges_from((a, b, dict(line_id=key))
                                  for key, (a, b, _, _) in lines.items())
        paths = nx.single_source_shortest_path(paths_graph, feeder.source_bus)
        edge_paths = {bus: {paths_graph[a][b]['line_id'] for a, b in zip(path, path[1:])}
                      for bus, path in paths.items()}
        values = {}
        for probe in probes:
            component = 'r1' if probe['kind'] == 'voltage_p' else 'x1'
            injection_ports = probe.get('injection_ports') or [(bus, 1) for bus in probe['injection_buses']]
            monitor_ports = probe.get('monitor_ports') or [(bus, 1) for bus in probe['monitor_buses']]
            monitored = []
            for bus, phase in monitor_ports:
                shared = sum(sum(lines[key][2] * catalogue[lines[key][3]][component]
                                 for key in edge_paths[bus] & edge_paths[injection])
                             for injection, injection_phase in injection_ports if injection_phase == phase)
                monitored.append(shared / len(injection_ports))
            values[probe['id']] = (max(monitored) if probe.get('aggregation') == 'max_abs'
                                   else sum(monitored) / len(monitored))
        return values

    reference = path_components(initial)
    ranked = []
    fallback = {}
    for ordinal, action in enumerate(actions):
        lines = dict(initial)
        supported = True
        for move in action.get('actions', [action]):
            kind = move['kind']
            if kind == 'replace_conductor':
                for replacement in move.get('replacements') or [move]:
                    key = replacement['line_id']
                    a, b, length, _ = lines[key]
                    lines[key] = (a, b, length, replacement['conductor'])
            elif kind in {'scale_layout', 'scale_subtree'}:
                changed = lines if kind == 'scale_layout' else subtree_lines[move['child_bus']]
                for key in changed:
                    a, b, length, conductor = lines[key]
                    lines[key] = (a, b, length * move['factor'], conductor)
            else:
                # Reconnection and load redistribution change the operating
                # point beyond this fixed-topology proxy; retain fallback order.
                supported = False
                break
        if not supported:
            fallback.setdefault(kind, []).append(action)
            continue
        predicted = path_components(lines)
        deficits = []
        responses = {}
        worsening = 0.
        centering = 0.
        for row in assessment['targets']:
            target = row['target']; key = target['probe_id']; observed = row['observed']
            if observed is None or reference[key] <= 1e-12:
                estimate = observed
            else:
                estimate = observed * predicted[key] / reference[key]
            responses[key] = estimate
            lower, upper = target['lower'], target['upper']
            scale = max(lower or 0, upper or 0, 1e-9)
            deficit = (max((lower or 0) - estimate,
                           estimate - (upper if upper is not None else math.inf), 0.) / scale
                       if estimate is not None else 1e12)
            deficits.append(deficit)
            worsening += max(0., deficit - row['deficit'] - 1e-9)
            if estimate is not None and lower is not None and upper is not None:
                centering += abs(estimate - (lower + upper) / 2) / scale
        proposal = dict(action, proxy_predicted_deficits=deficits,
                        proxy_response_estimates=responses)
        ranked.append(((worsening, sum(deficits), centering,
                        len(action.get('actions', [action])), ordinal), proposal))
    ordered = [action for _, action in sorted(ranked, key=lambda item: item[0])]
    if preserve_diversity:
        families = {}
        for action in ordered:
            families.setdefault((action['kind'], action.get('probe_id')), []).append(action)
        ordered = [action for group in zip_longest(*families.values())
                   for action in group if action is not None]
    # A proxy that cannot estimate an authorized family must not starve that
    # family of AC previews. Keep its original order and interleave families.
    return [action for group in zip_longest(ordered, *fallback.values())
            for action in group if action is not None]
