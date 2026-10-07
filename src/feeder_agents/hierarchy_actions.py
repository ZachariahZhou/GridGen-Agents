"""Local sourced edits; no load, phase, topology or geometry mutation."""
import math
import networkx as nx

from .artifacts import digest
from .rules import data


def physical_graph(feeder):
    graph=nx.Graph()
    graph.add_weighted_edges_from((e.bus1,e.bus2,e.length_km) for e in feeder.lines)
    graph.add_weighted_edges_from((t.bus1,t.bus2,0.) for t in feeder.transformers)
    return graph


def pv_weights(feeder):
    return feeder.design_evidence.get('hierarchy_inverse_pv_weights') or {l.id:l.kw/feeder.total_kw for l in feeder.loads}


def design_contract(feeder):
    """Physical fields no authorized action may change, plus phase PV totals."""
    weights=pv_weights(feeder)
    return {'buses':[b.model_dump() for b in feeder.buses],
        'lines':[e.model_dump(exclude={'conductor'}) for e in feeder.lines],
        'ties':[e.model_dump() for e in feeder.tie_lines],
        'transformer_connections':[(t.id,t.bus1,t.bus2,t.primary_kv,t.secondary_kv,t.connection) for t in feeder.transformers],
        'loads':[l.model_dump() for l in feeder.loads],'total_kw':feeder.total_kw,
        'voltage_kv':feeder.voltage_kv,'frequency_hz':feeder.frequency_hz,'source_bus':feeder.source_bus,
        'phase_pv_totals':[sum(weights[l.id] for l in feeder.loads if l.phases==[phase]) for phase in (1,2,3)]}


def contract_preserved(base,trial):
    a=design_contract(base);b=design_contract(trial)
    wa=a.pop('phase_pv_totals');wb=b.pop('phase_pv_totals')
    return a==b and all(math.isclose(x,y,abs_tol=1e-12,rel_tol=0) for x,y in zip(wa,wb))


def transformer_upgrade(feeder,transformer_id,evidence):
    old=next(t for t in feeder.transformers if t.id==transformer_id)
    candidates=[c for c in data('hierarchy_equipment.json')['transformers'] if c['kva']>old.kva
        and c['xhl_percent']*feeder.frequency_hz/c['source_frequency_hz']/c['kva']<=old.xhl_percent/old.kva
        and c['loadloss_percent']/c['kva']<=old.loadloss_percent/old.kva]
    if not candidates:return None
    chosen=min(candidates,key=lambda c:(c['kva'],-c['observations'],c['id']))
    return dict(kind='upgrade_transformer',component=old.id,replacement=chosen['id'],evidence=evidence)


def apply_action(feeder,action,allowed):
    kind=action['kind']
    if kind=='joint_local':
        steps=action['actions']
        if not 2<=len(steps)<=3 or any(s['kind'] not in ('upgrade_transformer','upgrade_mv_line','upgrade_lv_line','relocate_pv') for s in steps):
            raise ValueError('Joint local edits require two or three authorized equipment/PV actions')
        if len({(s['kind'],s.get('component'),s.get('donor'),s.get('recipient')) for s in steps})!=len(steps):raise ValueError('Duplicate component in joint edit')
        changed=feeder
        for step in steps:changed=apply_action(changed,step,allowed)
        return changed
    if kind not in allowed:raise ValueError('Local action is not authorized')
    changed=feeder.model_copy(deep=True)
    if kind=='upgrade_transformer':
        old=next(t for t in feeder.transformers if t.id==action['component'])
        target=next(t for t in changed.transformers if t.id==old.id)
        source=data('hierarchy_equipment.json')
        c=next(c for c in source['transformers'] if c['id']==action['replacement'])
        if (c['kva']<=old.kva or c['xhl_percent']*feeder.frequency_hz/c['source_frequency_hz']/c['kva']>old.xhl_percent/old.kva
                or c['loadloss_percent']/c['kva']>old.loadloss_percent/old.kva):
            raise ValueError('Transformer upgrade must increase rating and not increase equivalent R/X')
        target.kva=c['kva'];target.xhl_percent=c['xhl_percent']*feeder.frequency_hz/c['source_frequency_hz']
        for field in ('loadloss_percent','noloadloss_percent','imag_percent'):setattr(target,field,c[field])
        target.source={**c,'transfer':source['transfer'],'inverse_parent_equipment':old.source['id']}
    elif kind=='upgrade_mv_line':
        from .equipment import next_upgrade
        line=next(e for e in changed.lines if e.id==action['component'])
        buses={b.id:b for b in feeder.buses}
        if buses[line.bus1].voltage_kv<=1:raise ValueError('Use upgrade_lv_line for an LV product')
        replacement=next_upgrade(line,['J1','K1','Ckt5','Ckt24'])
        if replacement is None or replacement!=action['replacement']:raise ValueError('Not a compatible sourced MV upgrade')
        line.conductor=replacement;changed.equipment_catalog[replacement]=data('conductors.json')[replacement]
    elif kind=='upgrade_lv_line':
        from .lv_equipment import next_lv_upgrade, catalogue, model_code, code_id, resistance
        line=next(e for e in changed.lines if e.id==action['component'])
        buses={b.id:b for b in feeder.buses}
        if buses[line.bus1].voltage_kv>=1:raise ValueError('LV action requires an LV line')
        replacement=next_lv_upgrade(feeder,line)
        if replacement is None or replacement!=action['replacement']:raise ValueError('Not a compatible sourced LV upgrade')
        old=feeder.equipment_catalog[line.conductor]
        product=next(p for p in catalogue().values() if code_id(p,line.phases,old.get('installation'))==replacement)
        changed.equipment_catalog[replacement]=model_code(product,line.phases,old['ampacity_derating'],old.get('installation'))
        line.conductor=replacement
        row=changed.design_evidence.get('lv_equipment',{}).get('lines',{}).get(line.id)
        if row:
            row['baseline_product']=row.get('baseline_product',row['product'])
            row['product']=product['id']
            row['estimated_drop_pu']*=math.hypot(resistance(product),product['x1_50hz_ohm_km'])/math.hypot(resistance(old),old['x1_50hz_ohm_km'])
            row['selection']='Local sourced upgrade; baseline demand estimate retained; cross-condition simulations govern acceptance'
    elif kind=='relocate_pv':
        loads={l.id:l for l in feeder.loads};a=loads[action['donor']];b=loads[action['recipient']]
        fraction=action['fraction']
        if a.id==b.id or a.phases!=b.phases or not math.isfinite(fraction) or not 0<fraction<=.5:
            raise ValueError('PV transfer requires different customers on the same phase and fraction <=0.5')
        weights=dict(pv_weights(feeder));amount=weights[a.id]*fraction
        weights[a.id]-=amount;weights[b.id]+=amount
        changed.design_evidence['hierarchy_inverse_pv_weights']=weights
    else:raise ValueError('Unknown local action')
    changed.design_evidence.setdefault('hierarchy_inverse_actions',[]).append(action)
    if not contract_preserved(feeder,changed):raise ValueError('Local action changed a protected design field')
    return changed


def propose_actions(feeder,assessment,search,*,eligible=None):
    """Measurement-localized hypotheses; simulation, not this heuristic, decides."""
    from .equipment import next_upgrade
    buses={b.id:b for b in feeder.buses};lines={e.id:e for e in feeder.lines}
    txs={t.id:t for t in feeder.transformers};graph=physical_graph(feeder)
    distances=nx.single_source_dijkstra_path_length(graph,feeder.source_bus)
    paths=nx.single_source_shortest_path(graph,feeder.source_bus)
    proposals=[];seen=set()
    def add(action):
        if not action or action['kind'] not in search.allowed_actions:return
        key=digest({k:v for k,v in action.items() if k!='evidence'})
        if key not in seen:seen.add(key);proposals.append(action)
    def line_action(edge,evidence):
        if buses[edge.bus1].voltage_kv>1:
            replacement=next_upgrade(edge,['J1','K1','Ckt5','Ckt24']);kind='upgrade_mv_line'
        else:
            from .lv_equipment import next_lv_upgrade
            replacement=next_lv_upgrade(feeder,edge);kind='upgrade_lv_line'
        if replacement:add(dict(kind=kind,component=edge.id,replacement=replacement,evidence=evidence))
    violations=sorted((c for c in assessment['constraints'] if c['deficit']>0),key=lambda c:-c['deficit'])
    for issue in violations:
        witness=issue['witness'];evidence={k:issue[k] for k in ('condition','metric','operator','threshold','actual','witness','phase')}
        evidence['interpretation']='Location-based modification hypothesis; attribution is not causal proof. Every condition must be simulated.'
        path=[]
        if witness in txs:add(transformer_upgrade(feeder,witness,evidence))
        if witness in lines:
            edge=lines[witness]
            line_action(edge,evidence)
            path=paths[edge.bus2]
        elif witness in buses:path=paths[witness]
        # Voltage problems can originate on any upstream element; propose
        # separate local alternatives rather than upgrading the whole feeder.
        if path:
            for tx in feeder.transformers:
                if tx.bus2 in path:add(transformer_upgrade(feeder,tx.id,evidence))
            for edge in feeder.lines:
                if edge.bus1 in path and edge.bus2 in path:line_action(edge,evidence)
        if 'voltage' in issue['metric'] and witness in buses:
            local=[l for l in feeder.loads if witness in paths[l.bus] and (issue['phase'] is None or l.phases==[issue['phase']])]
            if not local:continue
            affected=max(local,key=lambda l:distances[l.bus])
            others=[l for l in feeder.loads if l.id!=affected.id and l.phases==affected.phases]
            if not others:continue
            near=min(others,key=lambda l:distances[l.bus])
            # To lower excessive voltage, move PV away from the affected user;
            # to lift deficient voltage, move PV toward it.
            donor,recipient=(affected,near) if issue['operator']=='le' else (near,affected)
            if pv_weights(feeder)[donor.id]>0:
                add(dict(kind='relocate_pv',donor=donor.id,recipient=recipient.id,
                         fraction=search.pv_transfer_fraction,evidence=evidence))
    equipment=[a for a in proposals if a['kind'] in ('upgrade_transformer','upgrade_mv_line','upgrade_lv_line')]
    def joint(actions):
        return dict(kind='joint_local',actions=actions,evidence={
            'interpretation':'Localized actions evaluated as one candidate across every search condition. Intermediate changes are never accepted; every constituent must be authorized.'})
    limit=search.candidates_per_round
    if search.proposal_policy=='violation_order':
        selected=[a for a in proposals if eligible is None or eligible(a)][:limit]
        if limit>=3 and len(equipment)>=2:
            combined=joint(equipment[:2])
            if eligible is None or eligible(combined):selected=selected[:limit-1]+[combined]
        return selected
    # Preserve a PV hypothesis when equipment paths would otherwise fill the budget.
    # Round-robin condition/action buckets retain severity order within each bucket.
    from collections import defaultdict
    buckets=defaultdict(list)
    for a in proposals:buckets[(a['evidence']['condition'],a['kind'])].append(a)
    ordered=[]
    while any(buckets.values()):
        for items in buckets.values():
            if items:ordered.append(items.pop(0))
    pv=next((a for a in ordered if a['kind']=='relocate_pv'),None)
    joint_actions=[]
    if pv and equipment and limit>=3:joint_actions.append(joint([equipment[0],pv]))
    if len(equipment)>=2 and limit>=3 and (not joint_actions or limit>=5):joint_actions.append(joint(equipment[:2]))
    # Three simultaneously constrained transformers can be weakly coupled through
    # the source voltage. Offer the already-supported three-edit transaction;
    # do not relax per-constraint acceptance to admit a regressing partial edit.
    coupled=[a for a in equipment if a['kind']=='upgrade_transformer'
             and a['evidence']['metric']=='max_transformer_loading_ratio']
    if limit>=5 and len(coupled)>=3:
        by_condition={}
        for action in coupled:by_condition.setdefault(action['evidence']['condition'],[]).append(action)
        group=next((items[:3] for items in by_condition.values() if len(items)>=3),None)
        if group:joint_actions.append(joint(group))
    # Build combinations before excluding singles: a rejected constituent may
    # still improve when applied jointly. Exclusions do not consume the budget.
    ordered=[a for a in ordered if eligible is None or eligible(a)]
    joint_actions=[a for a in joint_actions if eligible is None or eligible(a)]
    if pv not in ordered:pv=None
    slots=limit-len(joint_actions)
    selected=ordered[:slots]
    if pv and slots>=2 and pv not in selected:selected[-1]=pv
    return selected+joint_actions
