"""Outcome-independent suite rules and hidden constructive witnesses."""
import copy
from ..response_schema import ResponseDesignPlan

ATTEMPTS=((.65,.55),(.85,.65),(.45,.35),(.75,.25))


def public_task(network_id,task_id,layer,space,plan):
    return dict(network_id=network_id,task_id=task_id,layer=layer,space=space,
        plan=ResponseDesignPlan.model_validate(plan).model_dump(mode='json'))


def witness_plan(plan,ratios):
    result=copy.deepcopy(plan)
    nontrivial=False
    for target in result['targets']:
        center=ratios[target['probe_id']]
        radius=max(.0005,.15*abs(center-1))
        target.update(lower=max(0.,center-radius),upper=center+radius)
        nontrivial=nontrivial or not target['lower']<=1<=target['upper']
    if not nontrivial:raise ValueError('Constructed witness task is trivial at baseline')
    return result


def construction_status(baseline_valid,contract_valid,response_valid,consistent):
    if not baseline_valid:return 'invalid_baseline'
    if not contract_valid:return 'witness_contract_failed'
    if not response_valid:return 'witness_response_failed'
    if not consistent:return 'step_consistency_failed'
    return 'certified'


def check_unique_networks(records):
    if len({r['physical_hash'] for r in records})!=len(records):
        raise ValueError('Independent networks have duplicate physical fingerprints')


def select_ports(model,shared):
    import networkx as nx
    feeder=model['feeder'];graph=nx.Graph()
    graph.add_weighted_edges_from((e.bus1,e.bus2,e.length_km) for e in feeder.lines)
    distance,paths=nx.single_source_dijkstra(graph,feeder.source_bus)
    leaves=sorted(b for b in graph if b!=feeder.source_bus and graph.degree(b)==1)
    if len(leaves)<2:leaves=sorted(b for b in graph if b!=feeder.source_bus)
    a=max(leaves,key=lambda b:(distance[b],b))
    def overlap(b):
        first={frozenset(e) for e in zip(paths[a],paths[a][1:])}
        second={frozenset(e) for e in zip(paths[b],paths[b][1:])}
        length=sum(graph[u][v]['weight'] for u,v in (tuple(e) for e in first&second))
        return length/min(distance[a],distance[b])
    candidates=[b for b in leaves if b!=a]
    b=sorted(candidates,key=lambda b:((-overlap(b) if shared else overlap(b)),-distance[b],b))[0]
    children=[paths[a][max(1,len(paths[a])//2)],paths[b][max(1,len(paths[b])//2)]]
    if children[0]==children[1]:children=[a,b]
    return dict(a=a,b=b,children=children,shared_path_fraction=overlap(b),
        selection='Farthest leaf A; B chosen by topology-only shared-path fraction, then distance. No response search inspected.')


def witness_actions(model,plan,ports,family,attempt):
    from ..response_design import apply_response_action
    from ..response_geometry import compensation_scale_interval
    from ..response_distribution import compatible_conductors
    from ..rules import data
    import networkx as nx
    spec=plan.base_spec;search=plan.search
    first,second=ATTEMPTS[attempt];state=model;moves=[]
    if family in ('equipment','mixed'):
        feeder=model['feeder'];catalogue=data('conductors.json')
        graph=nx.Graph((e.bus1,e.bus2) for e in feeder.lines)
        path=nx.shortest_path(graph,feeder.source_bus,ports['a'])
        edges=[next(e for e in feeder.lines if {e.bus1,e.bus2}=={u,v}) for u,v in zip(path,path[1:])]
        line=edges[len(edges)//2]
        names=[n for n in compatible_conductors(line,spec,catalogue) if n!=line.conductor]
        if not names:raise ValueError('No compatible alternative conductor on frozen witness path')
        names.sort(key=lambda n:(catalogue[n]['x1'],catalogue[n]['r1'],n))
        quantiles=(.75,.25,.95,.05)
        name=names[round(quantiles[attempt]*(len(names)-1))]
        move=dict(kind='replace_conductor',line_id=line.id,conductor=name)
        moves.append(move);state=apply_response_action(state,spec,move)
    if family in ('geometry','mixed'):
        pairs=[(ports['children'][0],first,True),(ports['children'][1],second,False)] if family=='geometry' else [(ports['children'][1],second,False)]
        for child,fraction,increase in pairs:
            move=dict(kind='scale_subtree',child_bus=child)
            interval=compensation_scale_interval(state,model,spec,search,move)
            if interval['empty']:raise ValueError('Frozen witness direction has empty geometric interval')
            move['factor']=1+fraction*((interval['upper'] if increase else interval['lower'])-1)
            moves.append(move);state=apply_response_action(state,spec,move)
    return state,moves
