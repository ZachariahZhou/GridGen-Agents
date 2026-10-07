"""Explicit research hypotheses for a trunk with local settlement branches."""
import math
import random

import networkx as nx

from .rules import data
from .schemas import Bus, Line


def design_villages(feeder, spec):
    rng = random.Random(f'villages:{feeder.seed}')
    n = len(feeder.loads)
    groups = spec.scenario.village_count or max(1, min(6, n // 4))
    trunk_count = max(groups, min(n-groups, math.ceil(n*spec.scenario.trunk_fraction)))
    low, high = spec.segment_km_min, spec.segment_km_max
    trunk_step = low+(high-low)*rng.uniform(.75, .95)
    local_step = low+(high-low)*rng.uniform(.15, .3)
    width = max(1, min(3, math.floor(.8*trunk_step/local_step)+1))
    buses = [Bus(id='source', x_km=0, y_km=0)]
    edges = []
    for i in range(1, trunk_count+1):
        buses.append(Bus(id=f'b{i}', x_km=i*trunk_step, y_km=0))
        edges.append((i-1, i))
    # Each village has its own trunk anchor. Local nearest-edge construction
    # cannot cross settlement boundaries or shortcut the main trunk.
    villages = []
    remaining = n-trunk_count
    for j in range(groups):
        size = remaining//groups + (j < remaining % groups)
        anchor = (j+1)*trunk_count//groups
        members = []
        side = 1 if j % 2 == 0 else -1
        for k in range(size):
            index = len(buses)
            buses.append(Bus(id=f'b{index}',
                x_km=buses[anchor].x_km+(k % width)*local_step,
                y_km=side*(1+k//width)*local_step))
            members.append(index)
        candidates = nx.Graph()
        indices = [anchor]+members
        candidates.add_nodes_from(indices)
        for offset, a in enumerate(indices):
            for b in indices[offset+1:]:
                distance = math.hypot(buses[a].x_km-buses[b].x_km, buses[a].y_km-buses[b].y_km)
                if low-1e-10 <= distance <= high+1e-10:
                    candidates.add_edge(a, b, weight=distance)
        if not nx.is_connected(candidates):
            raise ValueError('Village distance graph disconnected; bounds were not relaxed')
        tree = nx.minimum_spanning_tree(candidates)
        edges.extend(nx.bfs_edges(tree, anchor))
        villages.append({'id':f'village_{j+1}', 'anchor':buses[anchor].id,
                         'buses':[buses[i].id for i in members]})
    feeder.buses = buses
    feeder.lines = [Line(id=f'l{k}', bus1=buses[a].id, bus2=buses[b].id,
        length_km=math.hypot(buses[a].x_km-buses[b].x_km, buses[a].y_km-buses[b].y_km),
        conductor='research_small') for k,(a,b) in enumerate(edges,1)]
    # Preserve total demand and within-group random heterogeneity; concentrate
    # 80% of load in villages. Every non-source bus remains a load point.
    trunk_loads = feeder.loads[:trunk_count]
    village_loads = feeder.loads[trunk_count:]
    for loads, share in ((trunk_loads,.2), (village_loads,.8)):
        factor = feeder.total_kw*share/sum(load.kw for load in loads)
        for load in loads:
            load.kw *= factor
            load.kvar *= factor
            load.pv_kw *= factor
            load.contract_kva *= factor
    feeder.design_evidence = {
        'method':'rural_villages_v1', 'status':'uncalibrated_research_hypothesis',
        'trunk_buses':[b.id for b in buses[:trunk_count+1]], 'villages':villages,
        'village_load_share':.8, 'trunk_step_km':trunk_step, 'local_step_km':local_step,
        'initial_conductor_sizing':size_conductors(feeder),
    }
    feeder.assumptions += [
        f'Rural hierarchy: {groups} synthetic settlements attached to one trunk; local distance-bounded MST inside each settlement.',
        'Trunk carries 20% of load; settlement buses carry 80%. All non-source buses are aggregate load points, not additional junctions.',
        f'Cluster count, requested {spec.scenario.trunk_fraction:g} trunk-node fraction (clipped for village membership) and 20/80 load split are explicit research hypotheses, not design-standard clauses.',
        'Initial conductor selection uses downstream aggregate demand at nominal voltage and 80% catalogue ampacity; this margin is a research assumption.',
        'No real roads, terrain, LV transformers, statistical calibration or guaranteed voltage feasibility; OpenDSS remains authoritative.',
    ]
    return feeder


def size_conductors(feeder):
    """Size from gross-load and full-PV endpoint currents, excluding losses."""
    graph = nx.DiGraph((line.bus1,line.bus2) for line in feeder.lines)
    totals = {bus.id:[0.,0.,0.] for bus in feeder.buses}
    for load in feeder.loads:
        values = totals[load.bus]
        for i,value in enumerate((load.kw,load.kvar,load.pv_kw)):
            values[i] += value
    for bus in reversed(list(nx.topological_sort(graph))):
        for parent in graph.predecessors(bus):
            totals[parent] = [a+b for a,b in zip(totals[parent],totals[bus])]
    catalogue = data('conductors.json')
    grades = sorted(('research_small','research_medium','research_large'), key=lambda key:catalogue[key]['normamps'])
    records = []
    for line in feeder.lines:
        p,q,pv = totals[line.bus2]
        amps = max(math.hypot(p,q),math.hypot(p-pv,q))/(math.sqrt(3)*feeder.voltage_kv)
        eligible = [grade for grade in grades if .8*catalogue[grade]['normamps'] >= amps]
        line.conductor = eligible[0] if eligible else grades[-1]
        records.append({'line_id':line.id,'downstream_kw':p,'downstream_kvar':q,'downstream_pv_kw':pv,
            'estimated_current_a':amps,'initial_conductor':line.conductor,
            'planning_loading_limit':.8,'catalogue_margin_satisfied':bool(eligible)})
    return records


def hierarchy_matches(feeder, spec):
    """Check the declared hierarchy against actual edges and load allocation."""
    evidence = feeder.design_evidence
    trunk = evidence.get('trunk_buses', [])
    villages = evidence.get('villages', [])
    n = len(feeder.loads)
    expected = spec.scenario.village_count or max(1, min(6,n//4))
    trunk_count = max(expected,min(n-expected,math.ceil(n*spec.scenario.trunk_fraction)))
    if trunk != ['source']+[f'b{i}' for i in range(1,trunk_count+1)] or len(villages) != expected:
        return False
    graph = nx.Graph((line.bus1,line.bus2) for line in feeder.lines)
    if not nx.is_tree(graph):
        return False
    allowed = {frozenset((a,b)) for a,b in zip(trunk,trunk[1:])}
    seen = set(trunk)
    village_buses = set()
    for village in villages:
        anchor, members = village.get('anchor'), village.get('buses', [])
        if anchor not in trunk[1:] or not members or len(set(members))!=len(members) or seen.intersection(members):
            return False
        local = graph.subgraph([anchor]+members)
        if len(local)!=len(members)+1 or not nx.is_tree(local):
            return False
        allowed.update(frozenset(edge) for edge in local.edges)
        seen.update(members)
        village_buses.update(members)
    if seen!={bus.id for bus in feeder.buses} or allowed!={frozenset(edge) for edge in graph.edges}:
        return False
    village_kw = sum(load.kw for load in feeder.loads if load.bus in village_buses)
    return math.isclose(village_kw,.8*sum(load.kw for load in feeder.loads),rel_tol=1e-9,abs_tol=1e-7)
