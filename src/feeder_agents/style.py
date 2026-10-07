"""Auditable research style controls and observed morphology, not planning certification."""
from collections import Counter
import math
from itertools import combinations
import networkx as nx


def style_contract(spec, feeder):
    graph=nx.Graph()
    graph.add_nodes_from(b.id for b in feeder.buses)
    graph.add_edges_from((e.bus1,e.bus2) for e in feeder.lines)
    depth=nx.single_source_shortest_path_length(graph,feeder.source_bus)
    loads={l.bus for l in feeder.loads}
    xs=[b.x_km for b in feeder.buses];ys=[b.y_km for b in feeder.buses]
    trunk=feeder.design_evidence.get('trunk_buses',[])
    trunk_edges={frozenset((a,b)) for a,b in zip(trunk,trunk[1:])}
    total_length=sum(l.length_km for l in feeder.lines)
    by_id={b.id:b for b in feeder.buses}
    anchors=[by_id[v['anchor']] for v in feeder.design_evidence.get('villages',[])]
    distances=[math.hypot(a.x_km-b.x_km,a.y_km-b.y_km) for a,b in combinations(anchors,2)]
    return {
        'version':'research_style_v1','purpose':'synthetic_research_case',
        'controls':{'scenario':spec.scenario.model_dump(mode='json'),'phase_design':spec.phase_design.model_dump(mode='json'),'equipment_design':spec.equipment_design.model_dump(mode='json'),'voltage_kv':spec.voltage_kv,
                    'mode':spec.mode,'power_factor':spec.power_factor,'pv_ratio':spec.pv_ratio,
                    'segment_km':[spec.segment_km_min,spec.segment_km_max]},
        'observed':{'source_branches':graph.degree(feeder.source_bus),
            'max_children':max((graph.degree(n) if n==feeder.source_bus else graph.degree(n)-1) for n in graph),
            'mean_depth':sum(depth.values())/max(1,len(depth)-1),
            'max_degree':max(dict(graph.degree()).values()),
            'tie_count':len(feeder.tie_lines),
            'physical_cycle_rank':len(feeder.lines)+len(feeder.tie_lines)-len(feeder.buses)+1,
            'load_weighted_depth':sum(l.kw*depth[l.bus] for l in feeder.loads)/sum(l.kw for l in feeder.loads),
            'trunk_length_share':sum(l.length_km for l in feeder.lines if frozenset((l.bus1,l.bus2)) in trunk_edges)/total_length if total_length else 0,
            'village_count':len(anchors),
            'mean_village_anchor_distance_km':sum(distances)/len(distances) if distances else 0,
            'buses':len(feeder.buses),'load_sites':len(loads),
            'junctions':len(set(graph)-loads-{feeder.source_bus}),
            'max_depth':max(depth.values()),'leaf_fraction':sum(graph.degree(n)==1 for n in graph if n!=feeder.source_bus)/max(1,len(graph)-1),
            'branch_nodes':sum(graph.degree(n)>=3 for n in graph),
            'extent_x_km':max(xs)-min(xs),'extent_y_km':max(ys)-min(ys),
            'total_line_km':sum(l.length_km for l in feeder.lines),
            'total_kw':sum(l.kw for l in feeder.loads),'pv_kw':sum(l.pv_kw for l in feeder.loads),
            'phase_mode':feeder.phase_mode,'phase_counts':dict(Counter(str(len(l.phases)) for l in feeder.lines)),
            'construction':dict(Counter(l.construction for l in feeder.lines)),
            'conductors':dict(Counter(l.conductor for l in feeder.lines))},
        'evidence':{'reference_case_id':spec.scenario.reference_case_id,
                    'calibration_profile':spec.scenario.calibration_profile,
                    'document_rule_ids':[r.rule_id for r in spec.document_rules]},
        'scope':'Supported controls and measured model morphology; not a learned style classifier or full engineering compliance.',
        'origins':'User/inferred/default provenance, when available, remains in the design brief and experiment plan.'}
