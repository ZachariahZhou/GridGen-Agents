"""Verify numerical edit budgets and immutable fields against the first model."""
import copy
import math

import networkx as nx

from .artifacts import digest


def change_summary(original,trial):
    if original['kind']!='distribution':
        return dict(changed_circuits=sum(a['circuits']!=b['circuits'] for a,b in
            zip(original['metadata']['branch_evidence'],trial['metadata']['branch_evidence']) if a['kind']=='line'))
    a,b=original['feeder'],trial['feeder']
    ratios=[n.length_km/o.length_km for o,n in zip(a.lines,b.lines)]
    return dict(line_length_ratio_min=min(ratios),line_length_ratio_max=max(ratios),
        maximum_node_displacement_km=max(math.hypot(n.x_km-o.x_km,n.y_km-o.y_km) for o,n in zip(a.buses,b.buses)),
        rewired_lines=sum({o.bus1,o.bus2}!={n.bus1,n.bus2} for o,n in zip(a.lines,b.lines)),
        changed_conductors=sum(o.conductor!=n.conductor for o,n in zip(a.lines,b.lines)),
        redistributed_load_fraction=sum(abs(n.kw-o.kw) for o,n in zip(a.loads,b.loads))/(2*a.total_kw),
        maximum_bus_load_change=max(abs(n.kw/o.kw-1) for o,n in zip(a.loads,b.loads)),
        total_kw_before=sum(l.kw for l in a.loads),total_kw_after=sum(l.kw for l in b.loads),
        total_pv_kw_before=sum(l.pv_kw for l in a.loads),total_pv_kw_after=sum(l.pv_kw for l in b.loads))


def distribution_contract(original,trial,plan):
    a,b=original['feeder'],trial['feeder'];search=plan.search;spec=plan.base_spec
    allowed=set(search.allowed_actions);normalized=copy.deepcopy(b)
    close=lambda x,y:math.isclose(x,y,rel_tol=1e-8,abs_tol=1e-9)
    for name in ('buses','lines','tie_lines','loads'):
        if [v.id for v in getattr(a,name)]!=[v.id for v in getattr(b,name)]:return False
    source=next(v for v in a.buses if v.id==a.source_bus)
    actual_source=next(v for v in b.buses if v.id==a.source_bus)
    if not (close(source.x_km,actual_source.x_km) and close(source.y_km,actual_source.y_km)):return False
    old_positions={v.id:(v.x_km,v.y_km) for v in a.buses};positions={v.id:(v.x_km,v.y_km) for v in b.buses}
    geometry=bool(allowed&{'scale_layout','scale_subtree'})
    scale=1.
    if geometry:
        anchor=old_positions[a.source_bus]
        if 'scale_subtree' not in allowed:
            farthest=max(old_positions,key=lambda key:math.dist(anchor,old_positions[key]))
            scale=math.dist(anchor,positions[farthest])/math.dist(anchor,old_positions[farthest])
            if abs(scale-1)>search.max_layout_scale_change+1e-9:return False
        for old,new,restore in zip(a.buses,b.buses,normalized.buses):
            if 'scale_subtree' in allowed:
                if math.dist(old_positions[old.id],positions[new.id])>search.max_node_displacement_km+1e-9:return False
            elif any(not close(v,c+scale*(o-c)) for v,o,c in zip(positions[new.id],old_positions[old.id],anchor)):return False
            restore.x_km,restore.y_km=old.x_km,old.y_km
    elif old_positions!=positions:return False
    changed=0
    energized_ids={line.id for line in a.lines}
    for old,new,restore in zip(a.lines+a.tie_lines,b.lines+b.tie_lines,normalized.lines+normalized.tie_lines):
        if new.bus1 not in positions or new.bus2 not in positions:return False
        if not close(new.length_km,math.dist(positions[new.bus1],positions[new.bus2])):return False
        if new in b.lines and not spec.segment_km_min-1e-9<=new.length_km<=spec.segment_km_max+1e-9:return False
        reconnected={old.bus1,old.bus2}!={new.bus1,new.bus2}
        if reconnected:
            if 'rewire_branch' not in allowed or new in b.tie_lines:return False
            changed+=1
            if not 1/search.max_rewire_length_ratio-1e-9<=new.length_km/old.length_km<=search.max_rewire_length_ratio+1e-9:return False
            restore.bus1,restore.bus2=old.bus1,old.bus2
        elif geometry:
            if 'scale_subtree' in allowed:
                bound=max(search.max_branch_length_change,search.max_layout_scale_change or 0)
                if abs(new.length_km/old.length_km-1)>bound+1e-9:return False
                factor=new.length_km/old.length_km
                if old.id in energized_ids and any(not close(positions[new.bus2][i]-positions[new.bus1][i],
                    factor*(old_positions[old.bus2][i]-old_positions[old.bus1][i])) for i in (0,1)):return False
            elif not close(new.length_km,old.length_km*scale):return False
        elif not close(new.length_km,old.length_km):return False
        if geometry or reconnected:restore.length_km=old.length_km
        if 'replace_conductor' in allowed:restore.conductor=old.conductor
    if 'rewire_branch' in allowed:
        graph=nx.Graph((e.bus1,e.bus2) for e in b.lines)
        if changed>search.max_rewired_lines or len(graph)!=len(b.buses) or not nx.is_tree(graph):return False
        if max(dict(graph.degree).values())>search.max_node_degree:return False
    if 'redistribute_load' in allowed:
        moved=0.
        for old,new,restore in zip(a.loads,b.loads,normalized.loads):
            factor=new.kw/old.kw
            if not math.isfinite(factor) or factor<=0 or abs(factor-1)>search.max_load_bus_change+1e-9:return False
            moved+=abs(new.kw-old.kw)
            for field in ('kw','kvar','contract_kva'):
                if not close(getattr(new,field),getattr(old,field)*factor):return False
                setattr(restore,field,getattr(old,field))
            if [p.phase for p in old.phase_powers]!=[p.phase for p in new.phase_powers]:return False
            for p,q,r in zip(old.phase_powers,new.phase_powers,restore.phase_powers):
                for field in ('kw','kvar'):
                    if not close(getattr(q,field),getattr(p,field)*factor):return False
                    setattr(r,field,getattr(p,field))
        if moved/2>a.total_kw*search.max_load_redistribution_fraction+1e-8:return False
        if any(not close(sum(getattr(l,k) for l in a.loads),sum(getattr(l,k) for l in b.loads)) for k in ('kw','kvar')):return False
    from .phases import phase_contract_matches
    from .equipment import equipment_contract_matches
    if not phase_contract_matches(b,spec) or not equipment_contract_matches(b,spec):return False
    def fields(feeder):
        value=feeder.model_dump();value.pop('design_evidence');value.pop('assumptions');return value
    return digest(fields(a))==digest(fields(normalized))
