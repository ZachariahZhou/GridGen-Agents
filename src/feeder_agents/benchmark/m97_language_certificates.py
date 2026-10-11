"""Uniform independent audit of source requirements and witness permissions.

Never imported by the agent. Original certificates remain immutable. One known
certificate-adapter omission is corrected with a separately timestamped witness.
"""
import datetime
import hashlib
import json
import math
from pathlib import Path

from feeder_agents.artifacts import atomic_json,digest
from feeder_agents.schemas import ExperimentSpec
from feeder_agents.transmission import TransmissionSpec
from feeder_agents.response_measurement import create_model
from feeder_agents.response_design import apply_response_action
from feeder_agents.revisions import apply_revision,RevisionPlan,_groups
from .m97_language import required_checks


def audit_certificates(root):
    root=Path(root);suite=json.loads((root/'suite.json').read_text());rows=[]
    for case in suite['cases']:
        if case['expected_outcome']!='generation':continue
        spec=case['witness_spec'];research=bool(case.get('task'));typed=(TransmissionSpec if spec['network_kind']=='transmission' else ExperimentSpec).model_validate(spec)
        if not research:typed=typed.model_copy(update={'seed':int.from_bytes(hashlib.sha256(f'{typed.seed}:0'.encode()).digest()[:4],'big')})
        original=create_model(typed);witness=original
        certificate=json.loads((root/'private_witnesses'/case['id']/'certificate.json').read_text())
        checks={};factor=1.;revision_steps=[];addendum=None
        if research:
            action=certificate.get('witness_action')
            if action:
                factor=action['factor']
                addendum_path=root/'analysis/certificate_addendum'/case['id']/'certificate.json'
                if addendum_path.exists():
                    addendum=json.loads(addendum_path.read_text());factor=addendum['factor'];action=dict(action,factor=factor,original_scale=factor)
                witness=apply_response_action(original,typed,action)
            compiled=certificate['compiled_plan'];plan=compiled['task_plan']
            proofs=addendum or certificate
            checks['full_step_targets']=bool(proofs.get('full_targets',proofs.get('targets'))) and all(t['passed'] for t in proofs.get('full_targets',proofs.get('targets',[])))
            checks['half_step_targets']=bool(proofs.get('half_targets')) and all(t['passed'] for t in proofs.get('half_targets',[]))
            checks['finite_task']=bool(proofs.get('finite_task_accepted',proofs.get('finite_task')))
            checks['compiled_task_family']=plan['task']==case['task']
            checks['source_response_interval']=[plan.get('response_lower'),plan.get('response_upper')]==case['response_interval']
            checks['finite_support_mvar']=math.isclose(plan['support_mvar'],case.get('support_mvar',.01))
            checks['finite_transfer_mw']=math.isclose(plan['transfer_mw'],1.)
        if case.get('revision'):
            suffix=int(case['id'].rsplit('_',1)[1])
            for turn,text in enumerate(case['messages'][1:],1):
                actions=([dict(kind='pv_ratio',value=.4 if turn==1 else .6,evidence=text)] if suffix<=3 else
                         [dict(kind='load_scale',value=1.1,evidence=text),dict(kind='pv_ratio',value=.2,evidence=text)] if suffix<=5 else
                         [dict(kind='geometry_scale',value=1.05,evidence=text)] if turn==1 else [dict(kind='pv_ratio',value=.3,evidence=text)])
                feeder,typed,diff=apply_revision(witness['feeder'],typed,RevisionPlan(feedback=text,actions=actions,preserve=['topology','voltage']))
                witness=dict(kind='distribution',feeder=feeder)
                preserved=['topology','equipment','voltage']+(['geometry','loads'] if suffix<=3 else ['geometry'] if suffix<=5 else ['loads']+(['geometry'] if turn==2 else []))
                revision_steps.append(dict(turn=turn,passed=all(diff[g]['preserved'] for g in preserved),preserved_groups=preserved))
            checks['all_untouched_revision_groups']=all(step['passed'] for step in revision_steps)
        actual={};changes={}
        if witness['kind']=='distribution':
            f=witness['feeder'];a=original['feeder']
            actual=dict(n_buses=len(f.buses),voltage_kv=f.voltage_kv,total_kw=sum(l.kw for l in f.loads),pv_ratio=sum(l.pv_kw for l in f.loads)/sum(l.kw for l in f.loads),
                        scene=spec['scenario']['kind'],phase_mode=f.phase_mode,seed=spec['seed'],count=spec['count'],repair_strategy=spec['repair_policy']['strategy'])
            old_lines={e.id:e for e in a.lines};new_lines={e.id:e for e in f.lines};old_buses={b.id:b for b in a.buses};new_buses={b.id:b for b in f.buses}
            changes=dict(changed_conductors=sum(old_lines[k].conductor!=new_lines[k].conductor for k in old_lines),
                         rewired_lines=sum({old_lines[k].bus1,old_lines[k].bus2}!={new_lines[k].bus1,new_lines[k].bus2} for k in old_lines),
                         max_branch_length_change=max(abs(new_lines[k].length_km/old_lines[k].length_km-1) for k in old_lines),
                         max_node_displacement_km=max(math.dist((old_buses[k].x_km,old_buses[k].y_km),(new_buses[k].x_km,new_buses[k].y_km)) for k in old_buses))
            bounds=case.get('numeric_limits',{})
            for field,limit in bounds.items():
                metric={'max_changed_conductors':'changed_conductors','max_rewired_lines':'rewired_lines'}.get(field,field)
                checks['numeric_permission_'+field]=metric in changes and changes[metric]<=limit+1e-9
            if case.get('max_layout_scale_change') is not None:checks['layout_scale_permission']=abs(factor-1)<=case['max_layout_scale_change']+1e-9
            forbidden=case.get('forbidden_actions',[])
            for action,metric in [('replace_conductor','changed_conductors'),('rewire_branch','rewired_lines'),('scale_layout','max_branch_length_change'),('scale_subtree','max_node_displacement_km')]:
                if action in forbidden:checks['forbidden_'+action]=changes[metric]<1e-9
            if 'redistribute_load' in forbidden:checks['forbidden_redistribute_load']=_groups(a)['loads']==_groups(f)['loads']
        else:
            c=witness['case'];actual=dict(n_buses=len(c['bus']),n_generators=len(c['gen']),voltage_kv=float(c['bus'][0,9]),total_mw=float(c['bus'][:,2].sum()))
            checks['transmission_radius']=spec['radius_km']==20
            checks['transmission_repairs_disabled']=spec['allowed_repairs']==[]
        if research:actual.update(task=plan['task'],response_lower=plan['response_lower'],response_upper=plan['response_upper'],support_mvar=plan['support_mvar'])
        checks.update({'source_'+k:v for k,v in required_checks(case['requirements'],actual).items()})
        checks['original_electrical_certificate']=bool(certificate['certified'])
        checks['amended_certificate']=bool(addendum['certified']) if addendum else True
        row=dict(case_id=case['id'],original_claim_certified=certificate['certified'],original_witness_valid=addendum.get('frozen_witness_valid',True) if addendum else True,
                 amended_witness_valid=all(checks.values()),checks=checks,actual=actual,actual_changes=changes,revision_steps=revision_steps,
                 replacement_certificate=str(root/'analysis/certificate_addendum'/case['id']/'certificate.json') if addendum else None)
        rows.append(row)
    report=dict(timestamp_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),frozen_suite_hash=digest(suite),positive_cases=len(rows),
                original_claimed_certified=sum(r['original_claim_certified'] for r in rows),original_witness_valid=sum(r['original_witness_valid'] for r in rows),
                amended_certified=sum(r['amended_witness_valid'] for r in rows),cases=rows,
                scope='Uniform independent source-requirement and numerical-permission audit of every positive witness, with unchanged prompts, objectives and agent methods. All original certificates retained.')
    atomic_json(root/'analysis/certificate_audit.json',report)
    print(json.dumps({k:v for k,v in report.items() if k!='cases'},indent=2))
    for row in rows:
        if not row['amended_witness_valid']:print(row['case_id'],[k for k,v in row['checks'].items() if not v])
    return report

if __name__=='__main__':
    import sys
    audit_certificates(Path(sys.argv[1]))
