import math
import random

from .rules import data
from .schemas import Bus, ExperimentSpec, Feeder, Line, Load


def _legacy_feeder(spec: ExperimentSpec, seed: int) -> Feeder:
    rng = random.Random(seed)
    n = rng.randint(spec.n_loads_min, spec.n_loads_max)
    total = rng.uniform(spec.total_kw_min, spec.total_kw_max)
    weights = [rng.lognormvariate(0, 0.5) for _ in range(n)]
    scale = total / sum(weights)
    buses = [Bus(id='source', x_km=0, y_km=0)]
    lines, loads = [], []
    q_over_p = math.tan(math.acos(spec.power_factor))
    for i in range(1, n + 1):
        # Bounded-depth branching tree; local coordinates are synthetic, not GIS.
        parent_index = rng.randrange(max(1, (i + 2) // 2))
        parent = buses[parent_index]
        length = rng.uniform(spec.segment_km_min, spec.segment_km_max)
        angle = rng.uniform(-math.pi, math.pi)
        bus = Bus(id=f'b{i}', x_km=parent.x_km + length * math.cos(angle),
                  y_km=parent.y_km + length * math.sin(angle))
        buses.append(bus)
        lines.append(Line(id=f'l{i}', bus1=parent.id, bus2=bus.id,
                          length_km=length, conductor='research_small'))
        kw = weights[i-1] * scale
        loads.append(Load(id=f'load{i}', bus=bus.id, kw=kw, kvar=kw*q_over_p,
                          pv_kw=kw*spec.pv_ratio, contract_kva=kw/spec.power_factor))
    return Feeder(seed=seed, voltage_kv=spec.voltage_kv,
                  frequency_hz=spec.frequency_hz, total_kw=total,
                  buses=buses, lines=lines, loads=loads, assumptions=[
                      'Balanced three-phase single-voltage aggregate / receiving-point model; no explicit transformers or neutral.',
                      'Synthetic coordinates in km, not GIS; line crossings are not junctions.',
                      'Illustrative sequence impedances in ohm/km, zero shunt capacitance.',
                      'Finite source equivalent: 1000 MVA three-phase and 500 MVA one-phase short circuit.',
                      'Contract kVA approximated by coincident load kVA; not real customer contract data.',
                      'PV is fixed unity-power-factor equivalent generation; no inverter controls or time series.',
                      'No full population calibration or complete standards certification.'
                  ])


def upgrade_conductors(feeder: Feeder) -> Feeder:
    upgraded = feeder.model_copy(deep=True)
    from .equipment import next_upgrade
    for line in upgraded.lines:
        replacement=next_upgrade(line)
        if replacement:line.conductor=replacement
    return upgraded


def _generate_feeder(spec: ExperimentSpec, seed: int) -> Feeder:
    if spec.scenario.layout == 'legacy_random':
        return _legacy_feeder(spec, seed)
    import networkx as nx
    from collections import defaultdict

    # Separate spatial randomness from demand randomness so PV/load changes do
    # not change geometry when the seed and point-count conditions stay fixed.
    feeder = _legacy_feeder(spec, seed)
    if spec.scenario.layout == 'structured_radial':
        from .topologies import design_topology
        return design_topology(feeder,spec)
    if spec.scenario.layout == 'empirical_tree':
        from .calibrated import design_empirical
        return design_empirical(feeder,spec)
    if spec.scenario.layout == 'rural_villages':
        from .settlements import design_villages
        return design_villages(feeder, spec)
    rng = random.Random(f'spatial:{seed}')
    count = len(feeder.buses)
    points = spec.scenario.positions_km
    if points is None:
        aspect = spec.scenario.aspect_ratio or (1.0 if spec.scenario.kind == 'urban' else 6.0)
        rows = max(1, round(math.sqrt(count / aspect)))
        spacing = (spec.segment_km_min + spec.segment_km_max) / 2
        jitter = min((spec.segment_km_max-spec.segment_km_min)/8, spacing*.1)
        points = [(i//rows*spacing + (rng.uniform(-jitter, jitter) if i else 0),
                   i%rows*spacing + (rng.uniform(-jitter, jitter) if i else 0))
                  for i in range(count)]
    buses = [Bus(id=b.id, x_km=p[0], y_km=p[1]) for b, p in zip(feeder.buses, points)]
    graph = nx.Graph()
    graph.add_nodes_from(range(count))
    radius = spec.segment_km_max + 1e-10
    buckets = defaultdict(list)
    for i, a in enumerate(buses):
        cell = (math.floor(a.x_km/radius), math.floor(a.y_km/radius))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in buckets[(cell[0]+dx, cell[1]+dy)]:
                    b = buses[j]
                    distance = math.hypot(a.x_km-b.x_km, a.y_km-b.y_km)
                    if distance > 0 and spec.segment_km_min-1e-10 <= distance <= radius:
                        graph.add_edge(j, i, weight=distance)
        buckets[cell].append(i)
    if not nx.is_connected(graph):
        raise ValueError(f'Spatial candidate graph disconnected ({nx.number_connected_components(graph)} components); distance bounds were not relaxed')
    tree = nx.minimum_spanning_tree(graph)
    feeder.buses = buses
    feeder.lines = [Line(id=f'l{k}', bus1=buses[a].id, bus2=buses[b].id,
        length_km=tree[a][b]['weight'], conductor='research_small')
        for k, (a, b) in enumerate(nx.bfs_edges(tree, 0), 1)]
    feeder.assumptions += [
        f'Spatial profile: {spec.scenario.kind}; local Cartesian km; distance-bounded minimum spanning tree.',
        'Explicit coordinates.' if spec.scenario.positions_km else
        'Synthetic compact/elongated jittered settlement lattice; not calibrated roads or real GIS.',
        'MST minimizes candidate-edge length only, not electrical cost or full engineering design.']
    return feeder


def spatial_metrics(feeder: Feeder) -> dict:
    import networkx as nx
    graph = nx.Graph()
    graph.add_weighted_edges_from((e.bus1, e.bus2, e.length_km) for e in feeder.lines)
    return {'max_source_path_km': max(nx.single_source_dijkstra_path_length(graph, feeder.source_bus).values()),
            'max_degree': max(dict(graph.degree()).values()),
            'max_depth': max(nx.single_source_shortest_path_length(graph, feeder.source_bus).values()),
            'total_line_km': sum(e.length_km for e in feeder.lines)}


def generate_feeder(spec: ExperimentSpec, seed: int) -> Feeder:
    from .rule_system import describe_rule_system
    if spec.scenario.load_placement == 'reference_conditioned':
        from .load_placement import reference_occupancy, sample_reference_loads
        from .settlements import size_conductors
        count_rng = random.Random(f'load-count:{seed}')
        load_count = count_rng.randint(spec.n_loads_min, spec.n_loads_max)
        fraction = reference_occupancy(spec.scenario.reference_case_id)['fraction']
        bus_count = spec.n_buses or math.ceil(load_count/fraction-1e-12)+1
        payload = spec.model_dump()
        payload.update(n_buses=None, n_loads_min=bus_count-1, n_loads_max=bus_count-1)
        payload['scenario'].update(load_placement='all_nodes', reference_case_id=None)
        geometry_spec = ExperimentSpec.model_validate(payload)
        feeder = _generate_feeder(geometry_spec, seed)
        evidence = sample_reference_loads(feeder, spec, load_count, seed)
        feeder.design_evidence.update(method=f'reference_conditioned_{spec.scenario.layout}_v1', load_placement=evidence,
            initial_conductor_sizing=size_conductors(feeder))
        feeder.assumptions += [
            'Reference-conditioned load occupancy and positive weights by child-count/depth bins; finite-count conditioning and smoothing are research assumptions.',
            'Reference voltage/region transferred to requested MV voltage; geometry, conductor parameters and source remain synthetic priors.']
    else:
        feeder = _generate_feeder(spec, seed)
    from .load_shapes import apply_load_shape
    apply_load_shape(feeder,spec)
    if spec.scenario.load_shape!='heterogeneous':
        from .settlements import size_conductors
        feeder.design_evidence['initial_conductor_sizing']=size_conductors(feeder)
    if spec.scenario.tie_count:
        from .ties import attach_ties
        feeder=attach_ties(feeder,spec)
    from .scene_profiles import apply_scene_profile
    from .phases import assign_phases,size_phase_conductors
    feeder.assumptions=[a for a in feeder.assumptions if not a.startswith('Balanced three-phase') and not a.startswith('Illustrative sequence impedances')]
    feeder=apply_scene_profile(feeder,spec)
    assign_phases(feeder,spec)
    if feeder.phase_mode=='unbalanced':
        feeder.design_evidence['initial_conductor_sizing']=size_phase_conductors(feeder)
    from .equipment import assign_equipment
    assign_equipment(feeder,spec)
    from .style import style_contract
    feeder.design_evidence['final_equipment_assignments']=[{'line_id':line.id,'equipment_id':line.conductor} for line in feeder.lines+feeder.tie_lines]
    feeder.design_evidence['style_contract'] = style_contract(spec, feeder)
    feeder.design_evidence['load_semantics'] = spec.load_semantics
    feeder.design_evidence['rule_system'] = describe_rule_system(spec)
    feeder.assumptions.append(f'Selected voltage rule profile: {spec.voltage_kv:g} kV; single-voltage {feeder.phase_mode} equivalent, no transformers/explicit neutral/protection.')
    return feeder
