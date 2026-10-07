"""Public-input adapter to the production delivery engine; no answer-key access."""
import copy
import json
from pathlib import Path
import numpy as np
from .adapters import plan,FixedRepair
from .evaluate import observations
from ..artifacts import atomic_json,digest
from ..delivery import execute_delivery
from ..hierarchy import HierarchicalSpec,HierarchicalFeeder,evaluate_hierarchy
from ..transmission import TransmissionSpec,validate_case


def execute_request(public,method,root,seed,rounds,recovery_rounds,model,proposal_override=None):
    if set(public)!={'request','domain'}:raise ValueError('Only public request and domain may enter delivery')
    root=Path(root)
    proposal=copy.deepcopy(proposal_override) if proposal_override is not None else plan(method,public,root/'planner',model)
    atomic_json(root/'proposal.json',proposal)
    if proposal['status']!='ready':return dict(status=proposal['status'],issues=proposal.get('issues',[]),generated=False)
    family='hierarchical' if public['domain']=='distribution' else 'transmission'
    cls=HierarchicalSpec if family=='hierarchical' else TransmissionSpec
    spec=cls.model_validate(proposal['spec'])
    if spec.count!=1:raise ValueError('End-to-end studies require exactly one sample per trial')
    # Common physical seed and benchmark envelope are evaluator-controlled for
    # every arm. Retain stricter requested bounds; private requirements stay out.
    values=spec.model_dump();values['seed']=seed
    bands={'mv_voltage_min_pu':.93,'mv_voltage_max_pu':1.07,'lv_voltage_min_pu':.9,'lv_voltage_max_pu':1.1} if family=='hierarchical' else {'voltage_min_pu':.95,'voltage_max_pu':1.05}
    for key,value in bands.items():values[key]=(max if '_min_' in key else min)(values[key],value)
    if family=='hierarchical':values['max_vuf_percent']=min(values['max_vuf_percent'],2.)
    spec=cls.model_validate(values)
    if method in ('adaptive','agent'):
        path=root/'planner/designs/plan/brief.json'
        if not path.exists():raise ValueError('Agent execution requires its saved source ledger, not a bare overridden spec')
        brief=json.loads(path.read_text());brief.pop('brief_hash',None)
        if brief['plan_type']!=family:raise ValueError('Planner changed model family')
    else:
        brief=dict(status='ready',plan_type=family,intent=dict(summary=public['request'],assumptions=[]),parameter_evidence=[])
    brief['plan']=dict(spec=spec.model_dump())
    atomic_json(root/'execution_brief.json',brief)
    repair_model=FixedRepair() if method=='fixed_repair' else model
    delivered=execute_delivery(brief,root/'delivery','case',public['request'],repair_model,
        feedback=method!='one_shot',recovery=method=='adaptive',rounds=rounds,recovery_rounds=recovery_rounds,memory=False)
    output=delivered['output'];final_brief=delivered['brief']
    spec=cls.model_validate(final_brief['plan']['spec'])
    atomic_json(root/'delivery_result.json',delivered)
    if output.get('samples') and all(s.get('error') for s in output['samples']):
        return dict(status='generation_failed',generated=False,electrical_valid=False,export_reload=False,
            product_accepted=False,delivery_directory=output['directory'],
            issues=[s['error'] for s in output['samples']],
            construction_failures=[s.get('construction_failure') for s in output['samples']],
            recovery_stop=(delivered.get('trace') or {}).get('stop_reason'),execution='end_to_end',memory_eligible=False)
    product_accepted=output.get('accepted',0)==1
    if final_brief.get('requirement_ledger'):
        from ..requirements import RequirementLedger
        from ..requirement_contract import audit_delivery
        audit=audit_delivery(RequirementLedger.model_validate(final_brief['requirement_ledger']),final_brief,output)
        product_accepted=audit['all_satisfied']
        atomic_json(root/'delivered_requirement_audit.json',audit)
    folder=Path(output['directory'])/'sample_00000'
    if not folder.resolve().is_relative_to(root.resolve()):raise ValueError('Delivery escaped trial directory')
    if family=='hierarchical':
        from ..simulation import simulate
        network=HierarchicalFeeder.model_validate_json((folder/'feeder.json').read_text())
        sim=simulate(folder/'opendss/Master.dss')
        validation=evaluate_hierarchy(network,spec,sim)
        obs=observations(public['domain'],network,validation=validation)
        reloaded=bool(sim.get('converged'))
        atomic_json(root/'reloaded_simulation.json',sim)
    else:
        from ..matpower import parse_matpower
        parsed=parse_matpower(folder/'case_generated.m')
        network=dict(version='2',baseMVA=parsed['base_mva'],**{k:np.asarray(parsed[k],dtype=float) for k in ('bus','gen','branch')})
        meta=json.loads((folder/'metadata.json').read_text())
        validation,_=validate_case(network,spec,meta)
        obs=observations(public['domain'],network,meta,validation)
        reloaded=bool(validation['converged'])
    atomic_json(root/'independent_validation.json',validation)
    trace=delivered['trace'] or {}
    return dict(status='generated' if product_accepted else 'completed_unaccepted',generated=True,
        observed=obs,electrical_valid=bool(validation['accepted']),export_reload=reloaded,
        product_accepted=product_accepted,delivery_directory=output['directory'],
        recovery_steps=sum(r.get('selected',False) for r in trace.get('rounds',[])),
        recovery_stop=trace.get('stop_reason'),execution='end_to_end',memory_eligible=False,
        issues=[] if product_accepted else ['Production delivery did not pass its complete acceptance checks'])


def audit_pairs(root,rows):
    """Verify shared baseline planning and every available pre-repair model."""
    root=Path(root)
    by_key={(r['task_id'],r['seed'],r['method']):r for r in rows}
    pairs=[]
    for (task,seed,method),one in by_key.items():
        if method!='one_shot' or (task,seed,'fixed_repair') not in by_key:continue
        fixed=by_key[task,seed,'fixed_repair']
        base=root/'trials'/task/'language'
        paths=[base/m/str(seed)/'proposal.json' for m in ('one_shot','fixed_repair')]
        same=(json.loads(paths[0].read_text())==json.loads(paths[1].read_text()) if paths[0].exists()
              else one['status']==fixed['status']=='error' and one['issues']==fixed['issues'])
        initial_equal=None
        if one.get('generated') and fixed.get('generated'):
            name='feeder.json' if one['domain']=='distribution' else 'case.json'
            a=Path(one['delivery_directory'])/'sample_00000'
            b=Path(fixed['delivery_directory'])/'sample_00000/feedback/candidate_0000'
            initial_equal=digest(json.loads((a/name).read_text()))==digest(json.loads((b/name).read_text()))
            if one['domain']=='transmission':
                initial_equal=initial_equal and json.loads((a/'metadata.json').read_text())==json.loads((b/'metadata.json').read_text())
        pairs.append(dict(task=task,seed=seed,planning_equal=same,initial_equal=initial_equal))
    return dict(passed=all(p['planning_equal'] and p['initial_equal'] is not False for p in pairs),
                groups=len(pairs),physical_pairs=sum(p['initial_equal'] is not None for p in pairs),pairs=pairs)
