"""Post-run task equivalence from existing solver states, without new solver/API calls.

Tool names, task labels and storage family are not acceptance requirements.
Frozen grades remain unchanged; every research-positive execution is audited.
"""
from __future__ import annotations
import json
import math
from pathlib import Path
import numpy as np
from feeder_agents.artifacts import atomic_json,file_digest
from .m97_language import required_checks


def finite_q_checks(base,altered,support_mvar):
    checks={'recorded_states':bool(base and altered),'converged':bool(base.get('converged') and altered.get('converged'))}
    for name in ('load_kw','generation_kw'):
        checks[name+'_preserved']=all(isinstance(record.get(name),(int,float)) for record in (base,altered)) and math.isclose(base[name],altered[name],rel_tol=1e-5,abs_tol=.01)
    checks['exact_q_support']=all(isinstance(record.get('load_kvar'),(int,float)) for record in (base,altered)) and math.isclose(base['load_kvar']-altered['load_kvar'],1000*support_mvar,rel_tol=1e-5,abs_tol=.01)
    before=base.get('bus_phase_voltage_pu',{});after=altered.get('bus_phase_voltage_pu',{})
    changes=[abs(after[bus][phase]-voltage) for bus,phases in before.items() for phase,voltage in phases.items() if bus in after and phase in after[bus]]
    checks['nonzero_voltage_effect']=bool(changes) and max(changes)>1e-10
    return checks


def finite_transfer_checks(base,altered,amount,spec):
    if not base or not altered:return {'recorded_transfer_state':False}
    before=np.asarray(base['bus']);solution=altered.get('solution',altered);bus=np.asarray(solution['bus']);gen=np.asarray(solution['gen']);branch=np.asarray(solution['branch'])
    delta=before[:,2]-bus[:,2];loading=np.maximum(np.hypot(branch[:,13],branch[:,14]),np.hypot(branch[:,15],branch[:,16]))/branch[:,5]
    losses=float(np.sum(branch[:,13]+branch[:,15]));tol=1e-5
    return dict(recorded_transfer_state=True,converged=bool(altered.get('converged')),exact_transfer_mw=abs(float(delta[delta>0].sum())-amount)<tol and abs(float(-delta[delta<0].sum())-amount)<tol,
        fixed_q=bool(np.allclose(bus[:,3],before[:,3],rtol=0,atol=1e-9)),finite=bool(all(np.isfinite(value).all() for value in (bus,gen,branch))),
        voltage=bool(np.all(bus[:,7]>=spec.get('voltage_min_pu',.9)-tol) and np.all(bus[:,7]<=spec.get('voltage_max_pu',1.1)+tol)),
        branch_ratings=bool(np.all(branch[:,5]>0) and np.all(loading<=1+tol)),generator_p_limits=bool(np.all(gen[:,1]>=gen[:,9]-tol) and np.all(gen[:,1]<=gen[:,8]+tol)),
        generator_q_limits=bool(np.all(gen[:,2]>=gen[:,4]-tol) and np.all(gen[:,2]<=gen[:,3]+tol)),
        power_balance=abs(gen[:,1].sum()-bus[:,2].sum()-losses)<tol and losses>=-tol,
        nonzero_branch_effect=bool(np.max(abs(branch[:,13]-np.asarray(base['branch'])[:,13]))>1e-10))


def finite_pv_checks(base,off,installed):
    if not base or not off:return {'recorded_full_pv_off_on_experiment':False}
    changes=[abs(off['bus_phase_voltage_pu'][bus][phase]-voltage) for bus,phases in base.get('bus_phase_voltage_pu',{}).items() for phase,voltage in phases.items()]
    return dict(pv_on_generation=math.isclose(base.get('generation_kw',-1),installed,abs_tol=.01),full_pv_off=abs(off.get('generation_kw',1))<.01,
        fixed_load=math.isclose(base['load_kw'],off['load_kw'],rel_tol=1e-5,abs_tol=.01),pv_present=installed>0,
        converged=bool(base.get('converged') and off.get('converged')),nonzero_voltage_effect=bool(changes) and max(changes)>1e-10)


def read(path):return json.loads(Path(path).read_text())


def audit_artifact(case,folder,authorization):
    from feeder_agents.response_design import read_response_result
    folder=Path(folder);manifest=read(folder/'manifest.json');plan=manifest['plan'];spec=plan['base_spec']
    result=read_response_result(folder)  # Validates saved hashes; never runs a solver.
    selected=read(folder/'selected/response.json');reference=read(folder/'baseline/response.json');half=read(folder/'verification/response.json')
    winner=result['winner'];measurement=folder/'baseline' if winner=='baseline' else folder/'trials'/winner
    task=case['task'];kind={'voltage_control':'voltage_q','static_pv_impact':'voltage_p','transmission_transfer':'transfer_p'}[task]
    expected=case['requirements'];checks={};details={};model_path=folder/'selected'/('case.json' if spec['network_kind']=='transmission' else 'feeder.json');model=read(model_path)
    if spec['network_kind']=='distribution':
        facts=dict(n_buses=len(model['buses']),voltage_kv=model['voltage_kv'],total_kw=sum(load['kw'] for load in model['loads']),
            pv_ratio=sum(load['pv_kw'] for load in model['loads'])/sum(load['kw'] for load in model['loads']),scene=spec['scenario']['kind'],phase_mode=model['phase_mode'],seed=spec.get('seed'),count=spec.get('count'))
        runnable=bool(list((folder/'selected/opendss').glob('*'))) and (folder/'selected/visualization.html').exists()
    else:
        facts=dict(n_buses=len(model['bus']),voltage_kv=model['bus'][0][9],total_mw=sum(bus[2] for bus in model['bus']),n_generators=len(model['gen']))
        runnable=(folder/'selected/case_generated.m').exists() and (folder/'selected/visualization.html').exists()
    checks.update({'source_'+key:value for key,value in required_checks({k:v for k,v in expected.items() if k not in {'task','response_lower','response_upper','support_mvar','repair_strategy'}},facts).items()})
    checks.update(runnable=runnable,base_accepted=bool(selected['base_accepted']))
    physical=[record for record in authorization.get('selected_physical_audit',[]) if Path(record['directory']).resolve()==folder.resolve()]
    checks['authorized_selected_changes']=not any(record['violations'] for record in physical)
    if spec['network_kind']=='distribution':checks['selected_permission_evidence_present']=bool(physical)
    else:
        checks['transmission_original_model_preserved']=read(folder/'baseline/model/case.json')==model
    if case.get('permission_amendment'):
        original_hash=file_digest(folder/'baseline/model'/model_path.name)
        first_turn_hashes={record['original_model_sha256'] for record in authorization.get('selected_physical_audit',[]) if record['turn']==0}
        checks['original_reference_preserved']=original_hash in first_turn_hashes
    details['declared_repair_strategy']=spec.get('repair_policy',{}).get('strategy')
    details['declared_repair_strategy_matches']=spec.get('repair_policy',{}).get('strategy')==expected.get('repair_strategy') if 'repair_strategy' in expected else None
    # run_response_design constructs directly and applies only recorded response actions.
    # repair_policy is inert metadata in that path; no automatic repair loop is called.
    details['automatic_repair_scope']='Frozen run_response_design directly calls create_model and recorded response actions; repair_policy does not trigger automatic repair.'
    probes=[]
    for probe_id,probe_result in selected['probes'].items():
        probe=probe_result['probe'];target=next((target for target in plan['targets'] if target['probe_id']==probe_id),{})
        pchecks=dict(task_response_kind=probe['kind']==kind,
            original_target_bounds=target.get('reference')=='baseline_ratio' and target.get('lower')==case['response_interval'][0] and target.get('upper')==case['response_interval'][1],
            full_and_half_valid=bool(probe_result.get('valid') and half['probes'].get(probe_id,{}).get('valid')))
        if task=='transmission_transfer':pchecks['peak_branch_aggregation']=probe.get('aggregation')=='max_abs'
        ratio=None;half_ratio=None
        try:
            denominator=reference['probes'][probe_id]['value'];ratio=probe_result['value']/denominator;half_ratio=half['probes'][probe_id]['value']/denominator
            pchecks['actual_full_half_targets']=all(case['response_interval'][0]-1e-9<=v<=case['response_interval'][1]+1e-9 for v in (ratio,half_ratio))
            pchecks['actual_half_step']=math.isclose(half['probes'][probe_id]['step'],probe_result['step']*.5,rel_tol=1e-9)
            pchecks['full_half_consistency']=abs(probe_result['value']-half['probes'][probe_id]['value'])/max(abs(probe_result['value']),1e-12)<=.03
        except (KeyError,TypeError,ZeroDivisionError):pchecks['actual_full_half_targets']=False
        finite={};evidence=[]
        if task=='voltage_control':
            amount=case.get('support_mvar',.01);base=read(measurement/'base/simulation.json')
            plus=measurement/'probes'/probe_id/'plus/simulation.json'
            explicit=measurement/'task/q_support/simulation.json'
            state=explicit if explicit.exists() else plus if math.isclose(probe_result['step'],amount,rel_tol=1e-9) and plus.exists() else None
            altered=read(state) if state else {};finite=finite_q_checks(base,altered,amount)
            if state:
                from feeder_agents.research_measurement import _distribution_checks
                from feeder_agents.schemas import Feeder,ExperimentSpec
                operational,_=_distribution_checks(Feeder.model_validate(model),ExperimentSpec.model_validate(spec),altered)
                finite.update(operational);evidence=[str(measurement/'base/simulation.json'),str(state)]
        elif task=='static_pv_impact':
            off_path=measurement/'task/pv_off/simulation.json';base=read(measurement/'base/simulation.json')
            if off_path.exists():
                off=read(off_path);installed=sum(load['pv_kw'] for load in model['loads'])
                finite=finite_pv_checks(base,off,installed)
                from feeder_agents.research_measurement import _distribution_checks
                from feeder_agents.schemas import Feeder,ExperimentSpec
                operational,_=_distribution_checks(Feeder.model_validate(model),ExperimentSpec.model_validate(spec),off);finite.update(operational)
                evidence=[str(measurement/'base/simulation.json'),str(off_path)]
            else:finite={'recorded_full_pv_off_on_experiment':False}
        else:
            base_path=measurement/'task/reference/simulation.json';transfer=measurement/'task/transfer/simulation.json'
            if transfer.exists():base=read(base_path);base=base.get('solution',base);altered=read(transfer)
            else:
                base_path=measurement/'base/simulation.json';base=read(base_path);transfer=measurement/'probes'/probe_id/'plus/simulation.json'
                altered=read(transfer) if transfer.exists() and math.isclose(probe_result['step'],1.,rel_tol=1e-9) else {}
            finite=finite_transfer_checks(base,altered,1.,spec);evidence=[str(base_path),str(transfer)] if altered else []
        pchecks['finite_requested_experiment']=bool(finite) and all(finite.values())
        probes.append(dict(probe_id=probe_id,checks=pchecks,finite_checks=finite,solver_evidence=evidence,full_ratio=ratio,half_ratio=half_ratio,passed=all(pchecks.values())))
    checks['equivalent_response_and_finite_task']=any(probe['passed'] for probe in probes)
    return dict(directory=str(folder),selected_model_sha256=file_digest(model_path),checks=checks,probes=probes,details=details,
        response_finite_evidence=checks['equivalent_response_and_finite_task'],passed=all(checks.values()))


def audit(root):
    root=Path(root);suite=read(root/'suite.json');rows=[]
    for case in suite['cases']:
        if case.get('task') is None or case['expected_outcome']!='generation':continue
        for arm in suite['arms']:
            for repetition in suite['repetitions']:
                job=root/'jobs'/f"{case['id']}__{arm}__r{repetition}";grade=read(job/'independent_grade.json')['row'];authorization=read(root/'analysis/authorization'/job.name/'audit.json')['authorization']
                records=[]
                for response in sorted(job.glob('workspace/projects/conversation/**/selected/response.json'),key=lambda path:path.stat().st_mtime_ns):
                    folder=response.parent.parent
                    if not (folder/'manifest.json').exists() or 'plan' not in read(folder/'manifest.json'):continue
                    try:records.append(audit_artifact(case,folder,authorization))
                    except Exception as exc:records.append(dict(directory=str(folder),passed=False,error=f'{type(exc).__name__}: {exc}'))
                row=dict(case_id=case['id'],arm=arm,repetition=repetition,task=case['task'],status=grade['status'],
                    frozen_generation_success=grade['generation_success'],semantic_artifact_fulfillment=bool(records and records[-1]['passed']),any_semantic_artifact=any(record['passed'] for record in records),
                    response_finite_evidence=any(record.get('response_finite_evidence',False) for record in records),artifacts=records)
                row['semantic_fulfilled_and_completed']=row['semantic_artifact_fulfillment'] and row['status']=='completed';rows.append(row)
    report=dict(scope='Independent post-run semantic-equivalence audit of every research-positive execution. Uses only immutable saved artifacts and solver states, with no new simulation or API calls. Tool names, task-label metadata and storage families are not requirements. Frozen grades remain unchanged.',
        policy='Accept recorded +Q at the exact requested support as a finite experiment if physical checks pass; require real PV off/on states or the exact 1 MW transfer. Inactive repair_policy metadata does not prove automatic repair. Original target bounds, all actual model requirements, runnable exports and committed permission limits remain mandatory.',
        rows=rows,groups={arm:dict(n=sum(row['arm']==arm for row in rows),semantic_artifact_fulfillment=sum(row['arm']==arm and row['semantic_artifact_fulfillment'] for row in rows),semantic_fulfilled_and_completed=sum(row['arm']==arm and row['semantic_fulfilled_and_completed'] for row in rows)) for arm in suite['arms']},source_hash=file_digest(Path(__file__)))
    atomic_json(root/'analysis/semantic_equivalence.json',report)
    print(json.dumps(report['groups'],indent=2))
    return report

if __name__=='__main__':
    import sys
    audit(Path(sys.argv[1]))
