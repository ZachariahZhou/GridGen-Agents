"""Spatially constrained, replayable MV edits for explicit research targets."""
import math

import networkx as nx

from .structure_targets import mv_graph,structural_constraints,structure_measurements


def structural_permission(spec,request=''):
    from .hierarchy_topology import topology_permission
    return spec.structure_targets is not None and topology_permission(request,True)


def _checked_change(feeder,component,parent,spec):
    if spec.structure_targets is None or spec.mv_topology_policy!='branched_v4':
        raise ValueError('MV structural actions require explicit v4 structural targets')
    graph=mv_graph(feeder);buses={b.id:b for b in feeder.buses}
    line=next((e for e in feeder.lines if e.id==component),None)
    if line is None or line.bus1 not in graph or line.bus2 not in graph or parent not in graph:
        raise ValueError('MV action must reference an existing MV line and bus')
    a,b=line.bus1,line.bus2
    if a==feeder.source_bus or parent in (a,b,feeder.source_bus) or graph.has_edge(parent,b):
        raise ValueError('MV action cannot change source exit or duplicate a connection')
    if graph.degree(a)<3 or not 2<=graph.degree(parent)<4:
        raise ValueError('MV action must preserve terminal set and degree<=4')
    paths=nx.single_source_shortest_path(graph,feeder.source_bus)
    if b in paths[parent]:raise ValueError('MV action would create a cycle in the detached subtree')
    if not set(line.phases)<=set(buses[parent].phases):raise ValueError('MV action violates phase continuity')
    length=math.dist((buses[parent].x_km,buses[parent].y_km),(buses[b].x_km,buses[b].y_km))
    if not spec.mv_segment_km_min-1e-10<=length<=spec.mv_segment_km_max+1e-10:
        raise ValueError('MV reconnection violates actual spatial length bounds')
    proposal=graph.copy();proposal.remove_edge(a,b);proposal.add_edge(parent,b)
    if not nx.is_tree(proposal) or proposal.degree(feeder.source_bus)!=1:raise ValueError('Invalid MV operating tree')
    newpaths=nx.single_source_shortest_path(proposal,feeder.source_bus)
    for tie in feeder.tie_lines:
        if tie.bus1 in newpaths[tie.bus2] or tie.bus2 in newpaths[tie.bus1] or nx.shortest_path_length(proposal,tie.bus1,tie.bus2)<3:
            raise ValueError('MV edit invalidates a normally-open branch-to-branch tie')
    leaves={v for v,d in graph.degree if v!=feeder.source_bus and d==1}
    if len(leaves)>=4 and len(graph)>=17:
        trunk=set(max(newpaths.values(),key=len))
        if not any(d>=3 and v not in trunk for v,d in proposal.degree):raise ValueError('MV edit removes nested branching')
    return line,length


def apply_structural_action(feeder,action,spec,authorized=False):
    if not authorized:raise ValueError('MV structural adjustment is not authorized')
    if action.get('kind')!='reattach_mv_branch':raise ValueError('Unknown MV structural action')
    line,length=_checked_change(feeder,action.get('component'),action.get('new_parent'),spec)
    changed=feeder.model_copy(deep=True);target=next(e for e in changed.lines if e.id==line.id)
    target.bus1=action['new_parent'];target.length_km=length
    record=dict(component=line.id,old_parent=line.bus1,branch_root=line.bus2,new_parent=target.bus1,
        old_length_km=line.length_km,new_length_km=length)
    changed.design_evidence.setdefault('mv_structure_actions',[]).append(record)
    return changed


def lineage_matches(feeder,spec,canonical_edges):
    """Rebuild canonical MV lines and replay every recorded endpoint change."""
    records=feeder.design_evidence.get('mv_structure_actions',[])
    if not isinstance(records,list) or not records or len(records)>8 or spec.structure_targets is None:return False
    if any(not isinstance(r,dict) for r in records):return False
    try:
        name=lambda i:feeder.source_bus if i==0 else f'b{i}'
        buses={b.id:b for b in feeder.buses};current=feeder.model_copy(deep=True)
        ids={b for b in mv_graph(feeder)}
        mv_lines={e.id:e for e in current.lines if e.bus1 in ids and e.bus2 in ids}
        if set(mv_lines)!={f'l{i}' for i in range(1,len(canonical_edges)+1)}:return False
        for i,(a,b) in enumerate(canonical_edges,1):
            line=mv_lines[f'l{i}'];line.bus1=name(a);line.bus2=name(b)
            line.length_km=math.dist((buses[line.bus1].x_km,buses[line.bus1].y_km),(buses[line.bus2].x_km,buses[line.bus2].y_km))
        for record in records:
            line,length=_checked_change(current,record['component'],record['new_parent'],spec)
            if line.bus1!=record['old_parent'] or line.bus2!=record['branch_root']:return False
            if not math.isclose(line.length_km,record['old_length_km'],abs_tol=1e-10) or not math.isclose(length,record['new_length_km'],abs_tol=1e-10):return False
            line.bus1=record['new_parent'];line.length_km=length
        actual={e.id:e for e in feeder.lines if e.id in mv_lines}
        return all((actual[k].bus1,actual[k].bus2)==(e.bus1,e.bus2) and math.isclose(actual[k].length_km,e.length_km,abs_tol=1e-10) for k,e in mv_lines.items())
    except (KeyError,TypeError,ValueError,nx.NetworkXException):return False


def structural_contract_preserved(base,trial,spec,local_topology=False):
    from .hierarchy_topology_design import topology_contract_matches
    from .hierarchy_actions import contract_preserved
    from .hierarchy_topology import topology_contract_preserved
    if not topology_contract_matches(trial,spec):return False
    normalized=trial.model_copy(deep=True);original={e.id:e for e in base.lines};ids=set(mv_graph(base))
    for e in normalized.lines:
        old=original.get(e.id)
        if old is not None and old.bus1 in ids and old.bus2 in ids:
            e.bus1=old.bus1;e.length_km=old.length_km
    return topology_contract_preserved(base,normalized,spec) if local_topology else contract_preserved(base,normalized)


def propose_structural_actions(feeder,spec,limit=4,*,eligible=None):
    issues=structural_constraints(feeder,spec)
    if not issues or not any(r['deficit']>0 for r in issues):return []
    graph=mv_graph(feeder);buses={b.id:b for b in feeder.buses};candidates=[];checked=0
    before={r['key']:r['deficit'] for r in issues};before_values=structure_measurements(feeder)
    for line in feeder.lines:
        if line.bus1 not in graph or line.bus1==feeder.source_bus or graph.degree(line.bus1)<3:continue
        for parent in sorted(graph):
            if graph.degree(parent) not in (2,3) or parent in (line.bus1,line.bus2,feeder.source_bus):continue
            distance=math.dist((buses[parent].x_km,buses[parent].y_km),(buses[line.bus2].x_km,buses[line.bus2].y_km))
            if not spec.mv_segment_km_min-1e-10<=distance<=spec.mv_segment_km_max+1e-10:continue
            action=dict(kind='reattach_mv_branch',component=line.id,new_parent=parent)
            try:trial=apply_structural_action(feeder,action,spec,authorized=True)
            except ValueError:continue
            checked+=1;after=structural_constraints(trial,spec)
            if all(r['deficit']<=before[r['key']]+1e-10 for r in after) and sum(r['deficit'] for r in after)<sum(before.values())-1e-9:
                action['evidence']=dict(before=before_values,predicted_structure=structure_measurements(trial),
                    unmet_targets=[r for r in issues if r['deficit']>0][:12],
                    before_deficit=sum(before.values()),predicted_deficit=sum(r['deficit'] for r in after),electrical_status='not_solved; full power flow required')
                candidates.append((sum(r['deficit'] for r in after),distance,line.id,parent,action))
            if checked>=256:break
        if checked>=256:break
    offered=[]
    for row in sorted(candidates,key=lambda r:r[:4]):
        if eligible is None or eligible(row[-1]):offered.append(row[-1])
        if len(offered)>=limit:break
    return offered
