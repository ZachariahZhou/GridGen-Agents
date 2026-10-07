"""Measured structural diagnostics; deliberately not a realism certification."""
from collections import Counter
import hashlib
import statistics

import networkx as nx


def graph_structure_report(model):
    """Inspect a complete HierarchicalFeeder, never an incomplete reference adapter.

    Reference records need a separate adapter with explicit unknown values and
    component-level accounting. Missing devices must not become measured zeros.
    """
    value=model.model_dump() if hasattr(model,'model_dump') else model
    required={'buses','lines','transformers','tie_lines','source_bus','voltage_kv'}
    if not required<=value.keys() or any('role' not in b for b in value.get('buses',[])):
        raise ValueError('Graph diagnostics require a complete hierarchical feeder; use a reference-specific audit for incomplete metadata')
    ids={b['id'] for b in value['buses'] if b.get('voltage_kv',value['voltage_kv'])>1}
    lines=[e for e in value['lines'] if e['bus1'] in ids and e['bus2'] in ids]
    graph=nx.Graph();graph.add_nodes_from(sorted(ids))
    graph.add_edges_from((e['bus1'],e['bus2']) for e in lines)
    degrees=Counter(dict(graph.degree()).values());source=value['source_bus']
    terminals={b for b,d in graph.degree() if d==1 and b!=source}
    transformers=value.get('transformers',[]);taps={t['bus1'] for t in transformers}
    tree=nx.is_tree(graph) if graph else False
    paths=nx.single_source_shortest_path(graph,source) if source in graph else {}
    longest=max(paths.values(),key=len,default=[])
    physical=graph.copy();cycles=[];cycle_paths=[]
    for e in value.get('tie_lines',[]):
        a,b=e['bus1'],e['bus2']
        if a not in graph or b not in graph:continue
        physical.add_edge(a,b)
        if tree and nx.has_path(graph,a,b):
            path=nx.shortest_path(graph,a,b)
            cycles.append(len(path));cycle_paths.append({frozenset((u,v)) for u,v in zip(path,path[1:])})
    lengths=[e['length_km'] for e in lines if e['length_km']>0]
    cv=statistics.pstdev(lengths)/statistics.mean(lengths) if lengths else None
    motif_counts=Counter();junction_counts=[]
    full=nx.Graph();full.add_edges_from((e['bus1'],e['bus2']) for e in value['lines'])
    buses={b['id']:b for b in value['buses']}
    for t in transformers:
        nodes={b['id'] for b in value['buses'] if b.get('transformer_id')==t['id']}
        lv=full.subgraph(nodes)
        junction_counts.append(sum(buses[b]['role']=='lv_branch' for b in nodes))
        if not lv or not nx.is_tree(lv) or t['bus2'] not in lv:continue
        rooted=nx.bfs_tree(lv,t['bus2']);signatures={}
        for b in reversed(list(rooted)):
            text=buses[b]['role']+'('+','.join(sorted(signatures[c] for c in rooted.successors(b)))+')'
            signatures[b]=hashlib.sha256(text.encode()).hexdigest()
        motif_counts[signatures[t['bus2']]]+=1
    fraction=len(taps&terminals)/len(taps) if taps else None
    notices=[]
    def notice(code,evidence,interpretation):
        notices.append(dict(code=code,evidence=evidence,interpretation=interpretation,kind='structural_review',changes_electrical_acceptance=False))
    if len(graph)>=10 and max(degrees,default=0)<=3:
        notice('degree_tail_limited',dict(max_degree=max(degrees)),
            'Low-degree operation can be valid. This graph alone does not cover reference feeders with occasional higher-degree junctions.')
    if len(taps)>=5 and fraction>.8:
        notice('terminal_tap_concentration',dict(fraction=fraction,terminal_count=len(terminals),transformers=len(taps)),
            'Most transformer taps are terminal. With every MV terminal requiring a transformer, this fraction has a budget-imposed lower bound; do not add transformers without changing the request.')
    if len(lengths)>=10 and cv is not None and cv<.08:
        notice('uniform_segments',dict(cv=cv,review_threshold=.08),
            'Near-uniform MV lengths warrant review of synthetic corridor spacing; the threshold is a diagnostic heuristic, not an engineering limit.')
    if len(cycles)>1 and len(set(cycles))==1:
        notice('repeated_cycle_size',dict(cycle_sizes=cycles),
            'All local tie fundamental cycles have the same size. This may reflect a selection prior, not the reference population.')
    if len(junction_counts)>1 and len(set(junction_counts))==1:
        notice('uniform_lv_branch_budget',dict(junctions_per_transformer=junction_counts[0]),
            'The common LV junction count is an explicit spec contract. Different user counts do not remove this shared structural motif.')
    recorded_joint=value.get('design_evidence',{}).get('mv_initial_design',{}).get('topology_design',{}).get('construction',{}).get('joint_design')
    joint={}
    if recorded_joint is not None:
        joint=dict(recorded_joint)
        joint.update(inline_transformers=len(taps-terminals),degree4_achieved=sum(d==4 for _,d in graph.degree))
        joint['degree4_target_met']=joint['degree4_achieved']>=joint['degree4_target']
    goals=value.get('design_evidence',{}).get('spec',{}).get('structure_targets')
    explicit={}
    if goals:
        from .hierarchy import HierarchicalFeeder,historical_hierarchy_spec
        from .structure_targets import structural_constraints
        instance=model if isinstance(model,HierarchicalFeeder) else HierarchicalFeeder.model_validate(value)
        explicit=dict(targets=goals,measurements=structural_constraints(instance,historical_hierarchy_spec(value['design_evidence']['spec'])),
            accepted_action_count=len(value.get('design_evidence',{}).get('mv_structure_actions',[])))
    return dict(schema_version=1,realism_status='not_established',joint_design=joint,
        **({'explicit_structure_targets':explicit} if explicit else {}),
        scope='Energized simple MV graph; physical open ties and LV motifs measured separately. No calibrated universal realism threshold.',
        mv=dict(nodes=len(graph),edges=graph.number_of_edges(),radial_connected=tree,
            degree_counts=dict(sorted(degrees.items())),max_degree=max(degrees,default=0),
            source_degree=graph.degree(source) if source in graph else None,
            terminal_count=len(terminals),max_root_hops=max(len(longest)-1,0),
            off_trunk_branch_points=sum(d>=3 and b not in longest for b,d in graph.degree())),
        roles=dict(transformers=len(taps),terminal_taps=len(taps&terminals),terminal_tap_fraction=fraction,
            all_mv_terminals_have_transformers=terminals<=taps),
        physical=dict(tie_count=len(value.get('tie_lines',[])),fundamental_cycle_sizes=cycles,
            odd_fundamental_cycles=sum(size%2 for size in cycles),
            cycle_rank=physical.number_of_edges()-len(physical)+nx.number_connected_components(physical),
            bridge_count=len(list(nx.bridges(physical))),
            shared_cycle_path_edges=[len(a&b) for i,a in enumerate(cycle_paths) for b in cycle_paths[i+1:]],
            equivalent_source_count=sum(b.get('role')=='source' for b in value['buses']),backup_supply_verified=False,
            fundamental_cycles_valid=tree),
        geometry=dict(line_length_samples=len(lengths),line_length_cv=cv,
            min_line_km=min(lengths,default=None),max_line_km=max(lengths,default=None)),
        lv=dict(junction_counts=junction_counts,distinct_rooted_shapes=len(motif_counts),
            largest_shape_count=max(motif_counts.values(),default=0)),notices=notices)
