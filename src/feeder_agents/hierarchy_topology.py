"""Bounded LV service reconnection within one transformer supply area."""
import math
import re
import networkx as nx
from .hierarchy_actions import design_contract,physical_graph


def topology_permission(request,requested=False):
    # Conservative opt-in. A stated freeze always overrides a UI/CLI opt-in.
    frozen=(r'(?:保持|保留|固定)[^，,。；;\n]{0,24}(?:拓扑|连接|接线|结构)[^，,。；;\n]{0,12}(?:不变|原状)?',
            r'(?:不允许|禁止|不得|不要|不能|不需|不改变|不调整|不修改)[^，,。；;\n]{0,16}(?:拓扑|结构|重接|连接|接线)',
            r'(?:keep|preserve|fix|freeze)[^.;\n]{0,24}(?:topology|connections|network structure)',
            r'(?:no|do not|must not)[^.;\n]{0,20}(?:rewir|reconnect|topology|network structure)',
            r'(?:拓扑|网络结构|连接|接线)[^，,。；;\n]{0,12}(?:不可|不得|不能|不许|禁止)[^，,。；;\n]{0,8}(?:改|调|重接)',
            r'(?:topology|network structure|connections)[^.;\n]{0,24}(?:remain unchanged|stay unchanged|must not change|cannot be changed)')
    if any(re.search(pattern,request,re.I) for pattern in frozen):return False
    explicit=re.search(r'(?:允许|可以)[^，,。；;\n]{0,16}(?:局部拓扑调整|调整拓扑|支路重接|用户重接)|allow[^.;\n]{0,20}(?:rewir|reconnect)',request,re.I)
    return bool(requested or explicit)


def _check_reconnection(feeder,line,parent,spec,kind='reconnect_service'):
    buses={b.id:b for b in feeder.buses};old=buses[line.bus1];child=buses[line.bus2]
    public_mode=spec.customer_connection in ('distributed_taps','mixed_taps')
    subtree=kind=='reconnect_subtree'
    if kind not in ('reconnect_service','reconnect_subtree'):raise ValueError('Unsupported topology action')
    if child.role!='customer' or old.role not in (('lv_branch','customer') if public_mode else ('lv_branch',)):
        raise ValueError('Reconnection must keep the customer attachment representation')
    if subtree:
        if not public_mode or child.phases!=[1,2,3] or parent.role not in ('customer','lv_branch') or parent.phases!=[1,2,3]:
            raise ValueError('A customer subtree must attach to a public three-phase point')
    else:
        allowed=('customer','lv_branch') if spec.customer_connection=='mixed_taps' else (('customer',) if public_mode else ('lv_branch',))
        if parent.role not in allowed:raise ValueError('Invalid terminal service parent')
        if spec.customer_connection=='distributed_taps' and any(e.bus1==parent.id for e in feeder.lines):
            raise ValueError('Distributed service reconnection requires a terminal receiving point')
    if not child.transformer_id or not (old.transformer_id==parent.transformer_id==child.transformer_id):
        raise ValueError('Reconnection cannot cross a transformer supply area')
    if not (old.region_id==parent.region_id==child.region_id):raise ValueError('Reconnection cannot cross installation regions')
    if not (old.voltage_kv==parent.voltage_kv==child.voltage_kv==spec.lv_voltage_kv):raise ValueError('Reconnection cannot change voltage level')
    if not set(line.phases)<=set(parent.phases) or line.phases!=child.phases:raise ValueError('Reconnection must preserve phase continuity')
    graph=physical_graph(feeder)
    if not nx.is_tree(graph):raise ValueError('Baseline must be a connected radial network')
    cut=graph.copy();cut.remove_edge(old.id,child.id)
    moved=nx.node_connected_component(cut,child.id)
    if parent.id in moved:raise ValueError('Reconnection would form a cycle')
    if subtree:
        if any(buses[n].role!='customer' or buses[n].transformer_id!=child.transformer_id or buses[n].region_id!=child.region_id or buses[n].voltage_kv!=child.voltage_kv for n in moved):
            raise ValueError('Only a same-area customer subtree may be moved')
        if any(e.bus1==parent.id and e.id!=line.id and buses[e.bus2].phases==[1,2,3] for e in feeder.lines):
            raise ValueError('Receiving public point already has a public continuation')
    elif len(moved)!=1:raise ValueError('Only terminal customer services may be moved')
    length=math.hypot(parent.x_km-child.x_km,parent.y_km-child.y_km)
    if not spec.service_km_min-1e-12<=length<=spec.service_km_max+1e-12:raise ValueError('Reconnected service outside specified length bounds')
    if not subtree and length>line.length_km+1e-10:raise ValueError('Reconnected terminal service must not be longer')
    return length


def topology_contract_preserved(base,trial,spec):
    a=design_contract(base);b=design_contract(trial)
    a.pop('lines');b.pop('lines')
    if a!=b:return False
    if len(base.lines)!=len(trial.lines) or {l.id for l in base.lines}!={l.id for l in trial.lines}:return False
    buses={x.id:x for x in base.buses}
    try:
        previous=base.design_evidence.get('local_topology_actions',[])
        recorded=trial.design_evidence.get('local_topology_actions',[])
        if recorded[:len(previous)]!=previous:return False
        replay=base.model_copy(deep=True);lines={e.id:e for e in replay.lines}
        for action in recorded[len(previous):]:
            if action['kind'] not in ('reconnect_service','reconnect_subtree'):return False
            line=lines[action['component']];parent=buses[action['new_parent']]
            if action.get('old_parent')!=line.bus1 or not math.isclose(action['old_length_km'],line.length_km,abs_tol=1e-12):return False
            length=_check_reconnection(replay,line,parent,spec,action['kind'])
            if not math.isclose(action['new_length_km'],length,rel_tol=1e-9,abs_tol=1e-12):return False
            line.bus1=parent.id;line.length_km=length
            from .customer_connections import connection_matches
            if not connection_matches(replay,spec.customer_connection):return False
        for line in trial.lines:
            if line.model_dump(exclude={'conductor'})!=lines[line.id].model_dump(exclude={'conductor'}):return False
        from .customer_connections import connection_matches
        return connection_matches(trial,spec.customer_connection)
    except (ValueError,KeyError,TypeError,nx.NetworkXError):return False


def apply_local_reconnection(feeder,action,spec,authorized=False):
    if not authorized:raise ValueError('Local topology modification is not authorized')
    if action.get('kind') not in ('reconnect_service','reconnect_subtree'):raise ValueError('Unsupported topology action')
    line=next((l for l in feeder.lines if l.id==action.get('component')),None)
    parent=next((b for b in feeder.buses if b.id==action.get('new_parent')),None)
    if line is None or parent is None or parent.id==line.bus1:raise ValueError('Invalid or unchanged reconnection endpoint')
    length=_check_reconnection(feeder,line,parent,spec,action['kind'])
    changed=feeder.model_copy(deep=True);target=next(l for l in changed.lines if l.id==line.id)
    target.bus1=parent.id;target.length_km=length
    changed.design_evidence.setdefault('local_topology_actions',[]).append({**action,'old_parent':line.bus1,'old_length_km':line.length_km,'new_length_km':length})
    row=changed.design_evidence.get('lv_equipment',{}).get('lines',{}).get(line.id)
    if row:
        row['baseline_length_km']=row.get('baseline_length_km',line.length_km)
        row['topology_notice']='Service endpoint changed; original sizing estimate is historical; acceptance uses new power flow.'
    if not topology_contract_preserved(feeder,changed,spec):raise ValueError('Reconnection changed fixed fields or radiality')
    return changed


def propose_reconnections(feeder,spec,assessment,limit=3,*,protect_customer_allocation=None,eligible=None):
    from .customer_connections import connection_matches,allocation_protected,allocation_preserved
    protect=allocation_protected(spec,protect_customer_allocation)
    buses={b.id:b for b in feeder.buses};lines={l.id:l for l in feeder.lines}
    paths=nx.single_source_shortest_path(physical_graph(feeder),feeder.source_bus)
    candidates=[];seen=set()
    for issue in sorted(assessment['constraints'],key=lambda c:-c['deficit']):
        if issue['deficit']<=0 or issue['metric'] not in ('lv_min_voltage_pu','lv_max_voltage_pu','max_line_loading_ratio'):continue
        witness=issue['witness'];affected=lines[witness].bus2 if witness in lines else witness
        if affected not in paths:continue
        for line in feeder.lines:
            if buses[line.bus2].role!='customer' or (affected not in paths[line.bus2] and line.bus2 not in paths[affected]):continue
            kinds=['reconnect_service']
            if spec.customer_connection in ('distributed_taps','mixed_taps') and buses[line.bus2].phases==[1,2,3]:kinds.append('reconnect_subtree')
            for parent in feeder.buses:
                if parent.id==line.bus1 or parent.role not in ('customer','lv_branch'):continue
                for kind in kinds:
                    key=(kind,line.id,parent.id)
                    if key in seen:continue
                    seen.add(key)
                    try:length=_check_reconnection(feeder,line,parent,spec,kind)
                    except ValueError:continue
                    updated=line.model_copy(update={'bus1':parent.id,'length_km':length})
                    preview=feeder.model_copy(update={'lines':[updated if e.id==line.id else e for e in feeder.lines]})
                    if not connection_matches(preview,spec.customer_connection):continue
                    if protect and not allocation_preserved(feeder,preview):continue
                    action=dict(kind=kind,component=line.id,new_parent=parent.id,evidence={
                        'witness':witness,'metric':issue['metric'],'actual':issue['actual'],'threshold':issue['threshold'],
                        'old_parent':line.bus1,'old_length_km':line.length_km,'new_length_km':length,
                        'interpretation':'Same-area attachment hypothesis; a longer permitted link may relieve upstream flow. Full electrical validation and non-regression required.'})
                    candidates.append((length-line.length_km,line.id,parent.id,action))
    ordered=sorted(candidates,key=lambda r:r[:3]);out=[]
    groups=[[r[-1] for r in ordered if r[-1]['kind']==kind] for kind in ('reconnect_service','reconnect_subtree')]
    while any(groups) and len(out)<limit:
        for group in groups:
            if group and len(out)<limit:
                action=group.pop(0)
                if eligible is None or eligible(action):out.append(action)
    return out
