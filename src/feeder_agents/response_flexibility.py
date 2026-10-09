"""Local, task-directed freedoms. Proxies propose; AC verification decides."""
import copy
import math

import networkx as nx

from .response_distribution import probe_lines
from .rules import data


def rooted(feeder):
    graph=nx.Graph()
    graph.add_nodes_from(b.id for b in feeder.buses)
    graph.add_edges_from((e.bus1,e.bus2,dict(line=e)) for e in feeder.lines)
    if not nx.is_tree(graph):raise ValueError('Local response moves require a connected tree')
    return graph,nx.bfs_tree(graph,feeder.source_bus)


def distribution_actions(model,original,spec,probes,assessment,search):
    feeder=model['feeder'];graph,tree=rooted(feeder);catalogue=data('conductors.json')
    positions={b.id:(b.x_km,b.y_km) for b in feeder.buses};busmap={b.id:b for b in feeder.buses}
    reference={e.id:e for e in original['feeder'].lines};by_probe={p['id']:p for p in probes}
    actions=[]
    for row in assessment['targets']:
        if row['passed'] or row['observed'] is None:continue
        probe=by_probe[row['target']['probe_id']];increase=row['observed']<(row['target']['lower'] or 0)
        component='r1' if probe['kind']=='voltage_p' else 'x1'
        relevant=probe_lines(feeder,probe);ids={e.id for e in relevant}
        total=sum(e.length_km*catalogue[e.conductor][component] for e in relevant)
        lo,hi=row['target']['lower'],row['target']['upper']
        width=hi-lo if lo is not None and hi is not None else .1*max(row['observed'],1e-9)
        desired=((lo+.2*width) if increase else (hi-.2*width))/max(row['observed'],1e-9)
        for parent,child in tree.edges:
            edge=graph[parent][child]['line']
            if edge.id not in ids:continue
            subtree=nx.descendants(tree,child)|{child}
            if 'scale_subtree' in search.allowed_actions:
                changed=[e for e in feeder.lines if e.bus1 in subtree or e.bus2 in subtree]
                influence=sum(e.length_km*catalogue[e.conductor][component] for e in changed if e.id in ids)/max(total,1e-12)
                # Prefer a local branch that excludes other protected ports.
                interference=sum(bool(set(by_probe[r['target']['probe_id']]['monitor_buses'])&subtree)
                    for r in assessment['targets'] if r is not row)
                bound=max(search.max_branch_length_change,search.max_layout_scale_change or 0)
                lower=max(max((1-bound)*reference[e.id].length_km/e.length_km,spec.segment_km_min/e.length_km) for e in changed)
                upper=min(min((1+bound)*reference[e.id].length_km/e.length_km,spec.segment_km_max/e.length_km) for e in changed)
                originals={b.id:(b.x_km,b.y_km) for b in original['feeder'].buses}
                for node in subtree:
                    v=[positions[node][i]-positions[parent][i] for i in (0,1)]
                    d=[positions[node][i]-originals[node][i] for i in (0,1)]
                    vv=sum(x*x for x in v)
                    if vv<=1e-20:continue
                    dv=sum(x*y for x,y in zip(d,v))
                    disc=dv*dv-vv*(sum(x*x for x in d)-search.max_node_displacement_km**2)
                    if disc<0:lower,upper=1.,0.;break
                    radius=math.sqrt(max(0.,disc))
                    lower=max(lower,1+(-dv-radius)/vv);upper=min(upper,1+(-dv+radius)/vv)
                for weight in (1.,.65,1.3):
                    factor=min(upper,max(lower,1+(desired-1)*weight/max(influence,1e-9)))
                    if lower>upper or (increase and factor<=1+1e-8) or (not increase and factor>=1-1e-8):continue
                    actions.append(dict(kind='scale_subtree',child_bus=child,factor=factor,probe_id=probe['id'],
                        proxy_effect=influence*abs(factor-1)/(1+10*interference),
                        diagnosis='Scale the selected downstream subtree about its parent, preserving connectivity; original geometry budgets and all AC targets must pass'))
            if 'rewire_branch' in search.allowed_actions:
                distances=nx.single_source_dijkstra_path_length(graph,feeder.source_bus,
                    weight=lambda a,b,d:d['line'].length_km*catalogue[d['line'].conductor][component])
                for destination in sorted(set(graph)-subtree-{parent}):
                    if graph.degree(destination)>=search.max_node_degree or len(busmap[destination].phases)!=3:continue
                    length=math.dist(positions[destination],positions[child]);ratio=length/reference[edge.id].length_km
                    if not spec.segment_km_min<=length<=spec.segment_km_max:continue
                    if not 1/search.max_rewire_length_ratio<=ratio<=search.max_rewire_length_ratio:continue
                    delta=distances[destination]+length*catalogue[edge.conductor][component]-distances[child]
                    if (increase and delta<=1e-10) or (not increase and delta>=-1e-10):continue
                    actions.append(dict(kind='rewire_branch',line_id=edge.id,child_bus=child,parent_bus=destination,
                        probe_id=probe['id'],proxy_effect=abs(delta),
                        diagnosis='Cross-cut reconnect changes source-path impedance without adding buses or cycles; phase, degree, distance and AC gates remain mandatory'))
        if 'redistribute_load' in search.allowed_actions:
            local=[l for l in feeder.loads if l.bus in probe['injection_buses']]
            others=sorted([l for l in feeder.loads if l not in local],key=lambda l:-l.kw)[:4]
            for load in local:
                for other in others:
                    donor,receiver=(other,load) if increase else (load,other)
                    amount=min(search.max_load_redistribution_fraction*original['feeder'].total_kw,
                        .8*search.max_load_bus_change*min(donor.kw,receiver.kw))
                    actions.append(dict(kind='redistribute_load',donor=donor.id,receiver=receiver.id,kw=amount,
                        probe_id=probe['id'],proxy_effect=amount,
                        diagnosis='Change the static demand allocation, preserving total P/Q, each local power factor, phase fractions and each PV injection; AC response change must be measured'))
    return sorted(actions,key=lambda a:-a['proxy_effect'])


def apply_distribution_action(model,spec,action):
    trial=copy.deepcopy(model);feeder=trial['feeder'];kind=action['kind']
    if kind in {'scale_subtree','rewire_branch'}:
        graph,tree=rooted(feeder);child=action['child_bus']
        if child==feeder.source_bus or child not in tree:raise ValueError('Invalid subtree root')
        parent=next(tree.predecessors(child));subtree=nx.descendants(tree,child)|{child}
        if kind=='scale_subtree':
            factor=action['factor']
            if not math.isfinite(factor) or factor<=0:raise ValueError('Invalid subtree scale')
            anchor=next(b for b in feeder.buses if b.id==parent)
            for bus in feeder.buses:
                if bus.id in subtree:
                    bus.x_km=anchor.x_km+(bus.x_km-anchor.x_km)*factor
                    bus.y_km=anchor.y_km+(bus.y_km-anchor.y_km)*factor
        else:
            destination=action['parent_bus'];line=graph[parent][child]['line']
            if line.id!=action['line_id'] or destination not in set(graph)-subtree-{parent}:raise ValueError('Reconnect must cross the subtree cut')
            line.bus1,line.bus2=destination,child
        positions={b.id:(b.x_km,b.y_km) for b in feeder.buses}
        for line in feeder.lines+feeder.tie_lines:line.length_km=math.dist(positions[line.bus1],positions[line.bus2])
    elif kind=='redistribute_load':
        from .phases import scale_phase_load
        if action['donor']==action['receiver'] or not math.isfinite(action['kw']) or action['kw']<=0:raise ValueError('Invalid load transfer')
        loads={l.id:l for l in feeder.loads}
        for name,sign in ((action['donor'],-1),(action['receiver'],1)):
            load=loads[name];factor=(load.kw+sign*action['kw'])/load.kw
            scale_phase_load(load,factor,1.)
    else:raise ValueError('Unknown distribution response action')
    feeder.design_evidence.setdefault('response_local_changes',[]).append(action)
    return trial
