"""Static, provenance-preserving summaries of representative GridLAB-D feeders.

This reader never executes GLM directives and is not an electrical converter.
Equipment definitions retain raw properties; missing ratings stay missing.
"""
from collections import Counter
from bisect import bisect_right
import math
import re

import networkx as nx

from .rules import data


def clean_glm(text):
    pattern = r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//[^\n]*'
    return re.sub(pattern, lambda m: re.sub(r'[^\n]', ' ', m[0])
                  if m[0].startswith(('//', '/*')) else m[0], text, flags=re.S)


def parse_objects(text):
    """Read literal properties, including colon IDs and nested object scopes."""
    text = clean_glm(text)
    # Mask quoted content for brace matching without changing character offsets.
    masked = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
                    lambda m: ' ' * len(m[0]), text)
    rows = []
    newlines = [i for i,c in enumerate(text) if c == '\n']
    for match in re.finditer(r'\bobject\s+([\w]+)(?::([\w]+))?\s*\{', masked):
        start = match.end()
        depth = 1
        direct = []
        for i in range(start, len(text)):
            c = masked[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    break
            direct.append(text[i] if depth == 1 and c not in '{}' else ' ')
        else:
            raise ValueError('Unclosed GLM object')
        body = re.sub(r'\bobject\s+\w+(?::\w+)?\s+(?=\s*;|$)', '', ''.join(direct))
        properties = {}
        for prop in re.finditer(r'(?:^|;)\s*([\w.]+)\s+([^;]+)(?=;|$)', body):
            properties[prop[1]] = prop[2].strip().strip('"\'')
        rows.append(dict(object_type=match[1], object_id=match[2], properties=properties,
                         source_line=bisect_right(newlines, match.start()) + 1))
    return rows


def quantity(raw, default_unit, units):
    if raw is None:
        return None
    match = re.fullmatch(r'\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([^\s]*)\s*', raw)
    if not match:
        return None
    value = float(match[1])
    unit = match[2] or default_unit
    if not math.isfinite(value) or value <= 0 or unit not in units:
        return None
    return value * units[unit]


def length_km(raw):
    # GridLAB-D powerflow line.length is published in feet.
    return quantity(raw, 'ft', {'ft': .0003048, 'm': .001, 'km': 1., 'mile': 1.609344, 'mi': 1.609344})


def distribution(values):
    values = sorted(values)
    if not values:
        return dict(count=0, min=None, p10=None, median=None, p90=None, max=None)
    def q(p):
        pos = (len(values)-1)*p
        a = int(pos)
        return values[a] + (values[min(a+1, len(values)-1)]-values[a])*(pos-a)
    return dict(count=len(values), min=values[0], p10=q(.1), median=q(.5), p90=q(.9), max=values[-1])


EQUIPMENT_TYPES = {'overhead_line_conductor', 'underground_line_conductor',
                   'line_configuration', 'line_spacing', 'transformer_configuration',
                   'regulator_configuration', 'triplex_line_conductor', 'triplex_line_configuration'}
LINK_TYPES = {'overhead_line', 'underground_line', 'triplex_line', 'transformer',
              'regulator', 'switch', 'fuse', 'recloser', 'sectionalizer'}


def analyze_model(objects):
    graph = nx.MultiGraph()
    phases = Counter()
    lengths = {'overhead_line': [], 'underground_line': []}
    mv_lengths = {'overhead_line': [], 'underground_line': []}
    voltage_classes = Counter()
    joint_lines = []
    open_count = 0
    partial_count = 0
    equipment = []
    roots = []
    voltages = []
    names = {o['properties'].get('name'): o for o in objects if o['properties'].get('name')}
    aliases = nx.utils.UnionFind()
    node_types = {'node', 'meter', 'load', 'triplex_node', 'triplex_meter', 'triplex_load'}
    attachment_count = 0
    for name, o in names.items():
        parent = o['properties'].get('parent')
        if o['object_type'] in node_types and parent in names and names[parent]['object_type'] in node_types:
            aliases.union(name, parent)
            attachment_count += 1
    missing_endpoints = set()
    for o in objects:
        p = o['properties']
        kind = o['object_type']
        if p.get('bustype') in {'SWING', 'SWING_PQ'}:
            roots.append(p.get('name'))
            v = quantity(p.get('nominal_voltage'), 'V', {'V': 1., 'kV': 1000.})
            if v:
                voltages.append(v * math.sqrt(3) / 1000)
        if kind in EQUIPMENT_TYPES:
            equipment.append(o)
        if kind not in LINK_TYPES or not p.get('from') or not p.get('to'):
            continue
        phase = ''.join(c for c in 'ABC' if c in p.get('phases', ''))
        phase_states = [p.get(f'phase_{c}_state', 'CLOSED') for c in phase]
        opened = p.get('status') == 'OPEN' or bool(phase_states) and all(s in {'OPEN', 'BLOWN'} for s in phase_states)
        if opened:
            open_count += 1
            continue
        partial_count += int(any(s in {'OPEN', 'BLOWN'} for s in phase_states))
        missing_endpoints.update(n for n in (p['from'],p['to']) if n not in names)
        graph.add_edge(aliases[p['from']], aliases[p['to']])
        if kind in lengths:
            phases[phase or 'unknown'] += 1
            length = length_km(p.get('length'))
            endpoint_v = [quantity(names.get(n, {}).get('properties', {}).get('nominal_voltage'),
                                   'V', {'V': 1., 'kV': 1000.}) for n in (p['from'], p['to'])]
            category = 'unknown'
            if all(v is not None for v in endpoint_v):
                lo,hi = sorted(v*math.sqrt(3)/1000 for v in endpoint_v)
                category = 'lv' if hi <= 1 else 'mv' if lo > 1 and hi <= 35 else 'other_or_mixed'
            voltage_classes[category] += 1
            if length is not None:
                lengths[kind].append(length)
                if category == 'mv':
                    mv_lengths[kind].append(length)
            joint_lines.append(dict(name=p.get('name'), construction=kind, phases=phase,
                                    length_km=length, voltage_class=category,
                                    endpoint_nominal_ln_v=endpoint_v, configuration=p.get('configuration'),
                                    source_path=o.get('source_path'), source_line=o['source_line']))
    components = nx.number_connected_components(graph) if graph else 0
    cycle_rank = graph.number_of_edges() - graph.number_of_nodes() + components
    depths = []
    if len(roots) == 1 and aliases[roots[0]] in graph:
        depths = list(nx.single_source_shortest_path_length(graph, aliases[roots[0]]).values())
    conductors = [o for o in equipment if o['object_type'].endswith('line_conductor')]
    ratings = [quantity(o['properties'].get('rating.summer.continuous'), 'A', {'A': 1.}) for o in conductors]
    transformers = [o for o in equipment if o['object_type'] == 'transformer_configuration']
    kva = [quantity(o['properties'].get('power_rating'), 'kVA', {'kVA': 1., 'VA': .001, 'MVA': 1000.}) for o in transformers]
    n_lines = sum(phases.values())
    return dict(object_counts=dict(sorted(Counter(o['object_type'] for o in objects).items())),
                source_voltage_ll_kv=sorted(set(round(v, 4) for v in voltages)),
                source_voltage_basis='sqrt(3) times explicit SWING nominal_voltage; no filename inference',
                line_phase_counts=dict(phases), line_count=n_lines,
                line_length_km={k: distribution(v) for k, v in lengths.items()},
                mv_line_length_km={k: distribution(v) for k, v in mv_lengths.items()},
                line_voltage_counts=dict(voltage_classes),
                overhead_fraction_by_count=sum(r['construction']=='overhead_line' for r in joint_lines)/n_lines if n_lines else None,
                topology=dict(scope='link-endpoint multigraph with node parent aliases contracted; not phase connectivity or electrical verification',
                              contracted_parent_attachments=attachment_count,
                              nodes=graph.number_of_nodes(), edges=graph.number_of_edges(), components=components,
                              cycle_rank=cycle_rank, radial=bool(graph) and components==1 and cycle_rank==0,
                              open_link_count=open_count, partially_open_link_count=partial_count,
                              missing_endpoint_definitions=sorted(missing_endpoints),
                              degree_counts=dict(sorted(Counter(d for _,d in graph.degree()).items())),
                              source_count=len(roots), reachable_from_source=len(depths), depth=distribution(depths)),
                equipment=dict(definition_count=len(equipment), conductor_count=len(conductors),
                               explicit_summer_rating_count=sum(v is not None for v in ratings),
                               summer_rating_a=distribution([v for v in ratings if v is not None]),
                               transformer_configuration_count=len(transformers),
                               transformer_config_rating_kva=distribution([v for v in kva if v is not None]),
                               selectable_for_generation=False,
                               boundary='Raw definitions only; no verified GLM-to-DSS matrices or ampacity fallback'),
                joint_lines=joint_lines, equipment_definitions=equipment)


def describe_taxonomy_references(family='all', construction='all', limit=36):
    """Agent-visible per-feeder priors, never pooled as actual utility samples."""
    if family not in {'all', 'pnnl_taxonomy', 'pge_prototypes'}:
        raise ValueError('Unknown taxonomy family')
    if construction not in {'all', 'overhead', 'underground'} or not 1 <= limit <= 36:
        raise ValueError('Invalid construction or limit')
    library = data('taxonomy_references.json')
    cases = []
    for case in library['cases']:
        if family != 'all' and case['family'] != family:
            continue
        stats = case['statistics']
        fraction = stats['overhead_fraction_by_count']
        if construction == 'overhead' and (fraction is None or fraction < .5):
            continue
        if construction == 'underground' and (fraction is None or fraction > .5):
            continue
        cases.append({k: case[k] for k in ('case_id', 'family', 'evidence_class', 'direct_opendss_export')} | {
            'source_url': case['source_url'],
            'source_voltage_ll_kv': stats['source_voltage_ll_kv'],
            'line_count': stats['line_count'], 'line_phase_counts': stats['line_phase_counts'],
            'overhead_fraction_by_count': fraction, 'line_length_km': stats['line_length_km'],
            'mv_line_length_km': stats['mv_line_length_km'], 'line_voltage_counts': stats['line_voltage_counts'],
            'topology': stats['topology'], 'equipment': stats['equipment']})
    return dict(model_count=len(library['cases']), matched_count=len(cases),
                family_counts=dict(Counter(c['family'] for c in library['cases'])),
                boundary=library['boundary'], cases=cases[:limit])


def taxonomy_design_context():
    """Small fixed-size context; detailed provenance remains available via tool."""
    result = describe_taxonomy_references()
    return {k: result[k] for k in ('model_count', 'family_counts', 'boundary')} | {
        'cases': [dict(case_id=c['case_id'], family=c['family'],
                       source_voltage_ll_kv=c['source_voltage_ll_kv'],
                       overhead_fraction=c['overhead_fraction_by_count'],
                       line_phase_counts=c['line_phase_counts'],
                       line_voltage_counts=c['line_voltage_counts'],
                       mv_length_km={k: {q: v[q] for q in ('count', 'p10', 'median', 'p90')}
                                  for k,v in c['mv_line_length_km'].items()}) for c in result['cases']]}
