"""Scientific contracts for response measurement and controlled generation."""
import copy
import json
from pathlib import Path

import numpy as np
import pytest


def plan_payload(**updates):
    payload = dict(research_question='Terminal voltage response',
        base_spec=dict(network_kind='distribution', n_buses=13,
            total_kw_min=180, total_kw_max=180, seed=41,
            repair_policy=dict(strategy='none')),
        probes=[dict(id='terminal', kind='voltage_p', step_mw=.01)],
        targets=[dict(probe_id='terminal', reference='baseline_ratio', lower=1.05, upper=1.5)],
        search=dict(max_rounds=3, candidates_per_round=6, allowed_actions=['replace_conductor']))
    payload.update(updates)
    return payload


def test_response_plan_rejects_conflicts_and_unsupported_domains():
    from feeder_agents.response_schema import ResponseDesignPlan
    for patch in [dict(targets=[dict(probe_id='terminal',lower=2,upper=1)]),
                  dict(targets=[dict(probe_id='unknown',lower=1)]),
                  dict(probes=[dict(id='terminal',kind='voltage_p',step_mw=0)]),
                  dict(probes=[dict(id='terminal',kind='transfer_p',injection_buses=['1'],withdrawal_buses=['1'])]),
                  dict(base_spec=dict(network_kind='hierarchical'))]:
        with pytest.raises(ValueError): ResponseDesignPlan.model_validate(plan_payload(**patch))
    p=plan_payload();p['targets']=[dict(probe_id='terminal',lower=2),dict(probe_id='terminal',upper=1)]
    with pytest.raises(ValueError,match='Conflicting'): ResponseDesignPlan.model_validate(p)


@pytest.mark.parametrize('kind,unit',[('voltage_p','pu/MW'),('voltage_q','pu/MVAr')])
def test_dss_response_has_correct_sign_units_and_independent_step(tmp_path,kind,unit):
    from feeder_agents.response_schema import ResponseDesignPlan
    from feeder_agents.response_measurement import create_model,resolve_probes,measure
    p=ResponseDesignPlan.model_validate(plan_payload(probes=[dict(id='terminal',kind=kind,step_mw=.01)]))
    model=create_model(p.base_spec);before=model['feeder'].model_dump()
    probes=resolve_probes(model,p.probes)
    a=measure(model,p.base_spec,probes,tmp_path/'a')
    b=measure(model,p.base_spec,probes,tmp_path/'b',step_factor=.5)
    assert a['base_accepted'] and a['probes']['terminal']['valid']
    assert a['probes']['terminal']['value']>0
    assert a['probes']['terminal']['unit']==unit
    assert all(v>0 for v in a['probes']['terminal']['signed_response'].values())
    assert a['probes']['terminal']['value']==pytest.approx(b['probes']['terminal']['value'],rel=.01)
    assert model['feeder'].model_dump()==before
    assert (tmp_path/'a/probes/terminal/plus/simulation.json').exists()


def test_missing_phase_is_rejected_instead_of_silently_averaged():
    from feeder_agents.response_schema import ResponseDesignPlan
    from feeder_agents.response_measurement import create_model,resolve_probes
    p=ResponseDesignPlan.model_validate(plan_payload())
    m=create_model(p.base_spec);bus=m['feeder'].loads[-1].bus
    next(b for b in m['feeder'].buses if b.id==bus).phases=[1]
    probe=p.probes[0].model_copy(update=dict(injection_buses=[bus],monitor_buses=[bus],phases=[2]))
    with pytest.raises(ValueError,match='phase'): resolve_probes(m,[probe])


def test_repair_gate_preserves_each_research_target():
    from feeder_agents.response_design import accept_candidate
    before=dict(base_accepted=True,valid=True,deficits=[0,.2])
    assert not accept_candidate(before,dict(base_accepted=True,valid=True,deficits=[.01,0]),True)
    assert not accept_candidate(before,dict(base_accepted=True,valid=True,deficits=[0,.1]),False)
    assert accept_candidate(before,dict(base_accepted=True,valid=True,deficits=[0,.1]),True)


def test_transmission_transfer_uses_balanced_mw_and_preserves_case(tmp_path):
    from feeder_agents.response_schema import ResponseDesignPlan
    from feeder_agents.response_measurement import create_model,resolve_probes,measure
    p=ResponseDesignPlan.model_validate(plan_payload(
        base_spec=dict(network_kind='transmission',n_buses=9,n_generators=3,total_mw=180,seed=41),
        probes=[dict(id='transfer',kind='transfer_p',injection_buses=['4'],withdrawal_buses=['8'],step_mw=.1)],
        targets=[dict(probe_id='transfer',lower=0,upper=1)],
        search=dict(allowed_actions=['parallel_line'],max_rounds=1)))
    m=create_model(p.base_spec);before=copy.deepcopy(m['case'])
    a=measure(m,p.base_spec,resolve_probes(m,p.probes),tmp_path)
    assert a['base_accepted'] and a['probes']['transfer']['valid']
    assert 0<a['probes']['transfer']['value']<1.1
    assert a['probes']['transfer']['unit']=='MW/MW'
    assert np.array_equal(m['case']['bus'],before['bus'])


def test_response_run_exports_verified_model_and_rejects_changed_run(tmp_path):
    from feeder_agents.response_design import run_response_design
    p=plan_payload(targets=[dict(probe_id='terminal',reference='baseline_ratio',lower=.99,upper=1.01)])
    r=run_response_design(p,tmp_path,'response')
    assert r['target_met'] and r['verification_passed'] and r['protected_contract_preserved']
    assert (Path(r['directory'])/'selected/opendss/Master.dss').exists()
    assert (Path(r['directory'])/'candidates.csv').exists()
    assert run_response_design(p,tmp_path,'response')['result_hash']==r['result_hash']
    p['targets'][0]['lower']=.95
    with pytest.raises(ValueError,match='changed'):run_response_design(p,tmp_path,'response')


def test_exhausted_search_does_not_claim_infeasibility(tmp_path):
    from feeder_agents.response_design import run_response_design
    p=plan_payload(targets=[dict(probe_id='terminal',lower=100)],search=dict(max_rounds=0))
    r=run_response_design(p,tmp_path,'unmet')
    assert not r['target_met'] and r['status']=='target_not_met'
    assert r['stop_reason']=='search_exhausted'


def test_language_entry_preserves_exact_node_count_and_surfaces_ambiguity(tmp_path):
    from feeder_agents.design import design_from_request

    class Model:
        model_name='offline_contract_test'
        def with_structured_output(self,*args,**kwargs):return self
        def invoke(self,messages):
            return dict(status='ready',plan=plan_payload(),issues=[])

    r=design_from_request('\u751f\u621013\u4e2a\u8282\u70b9\u7684\u9988\u7ebf\uff0c\u5c06\u672b\u7aef\u7535\u538b\u5bf9\u6709\u529f\u6ce8\u5165\u7684\u7075\u654f\u5ea6\u63d0\u9ad85%\u523050%\u3002',
                          tmp_path,'language',execute=False,model=Model())
    assert r['status']=='draft' and r['plan_type']=='electrical_response'
    assert r['plan']['base_spec']['n_buses']==13
    r=design_from_request('\u751f\u621037\u4e2a\u8282\u70b9\u7684\u9988\u7ebf\uff0c\u5c06\u672b\u7aef\u7535\u538b\u5bf9\u6709\u529f\u6ce8\u5165\u7684\u7075\u654f\u5ea6\u63d0\u9ad85%\u523050%\u3002',
                          tmp_path,'mismatch',execute=False,model=Model())
    assert r['status']=='needs_clarification'


def test_response_tools_are_available_to_langchain(tmp_path):
    from feeder_agents.agent import make_tools
    names={tool.name for tool in make_tools(tmp_path,'response')}
    assert {'design_electrical_response','describe_electrical_response_capabilities'}<=names


def test_corrupted_response_artifact_is_not_reused(tmp_path):
    from feeder_agents.response_design import run_response_design
    p=plan_payload(targets=[dict(probe_id='terminal',reference='baseline_ratio',lower=.99,upper=1.01)])
    r=run_response_design(p,tmp_path,'integrity')
    (Path(r['directory'])/'selected/opendss/Lines.dss').write_text('corrupted')
    with pytest.raises(ValueError,match='artifacts changed'):run_response_design(p,tmp_path,'integrity')


def test_catalogue_search_changes_response_without_changing_research_case(tmp_path):
    from feeder_agents.response_design import run_response_design,protected_fingerprint
    from feeder_agents.schemas import Feeder
    base=dict(network_kind='distribution',n_buses=37,total_kw_min=480,total_kw_max=480,seed=41,
              scenario=dict(kind='rural',engineering_profile='rural',layout='rural_villages',village_count=3),
              phase_design=dict(mode='unbalanced'))
    r=run_response_design(plan_payload(base_spec=base),tmp_path,'controlled')
    assert r['target_met'] and r['accepted_steps']>0
    assert 1.05<=r['selected']['probes']['terminal']['value']/r['baseline']['probes']['terminal']['value']<=1.5
    root=Path(r['directory'])
    a=Feeder.model_validate(json.loads((root/'baseline/model/feeder.json').read_text()))
    b=Feeder.model_validate(json.loads((root/'selected/feeder.json').read_text()))
    assert protected_fingerprint(dict(kind='distribution',feeder=a))==protected_fingerprint(dict(kind='distribution',feeder=b))


def test_response_design_is_readable_in_existing_web_history(tmp_path):
    from feeder_agents.response_language import design_response_from_request
    from feeder_agents.ui_saved_records import load_verified_design_record
    class Model:
        model_name='offline_contract_test'
        def with_structured_output(self,*args,**kwargs):return self
        def invoke(self,messages):return dict(status='ready',plan=plan_payload(
            targets=[dict(probe_id='terminal',reference='baseline_ratio',lower=.99,upper=1.01)]),issues=[])
    project=tmp_path/'projects/default'
    design_response_from_request('\u751f\u621013\u4e2a\u8282\u70b9\u7684\u9988\u7ebf\uff0c\u672b\u7aef\u7075\u654f\u5ea6\u4fdd\u6301\u5728\u57fa\u51c6\u76840.99\u81f31.01\u500d\u3002',project,'web',model=Model())
    displayed,warning=load_verified_design_record(project/'designs/web/result.json',tmp_path)
    assert warning is None and displayed['outcome']['target_met']
    assert displayed['verified_family']=='electrical_response'
    assert 'dataset.zip' in displayed['outcome']['artifacts']


def test_failed_language_compilation_returns_auditable_failure_not_user_conflict(tmp_path):
    from feeder_agents.response_language import design_response_from_request
    class InvalidModel:
        def with_structured_output(self,*args,**kwargs):return self
        def invoke(self,messages):
            p=plan_payload();p['base_spec']['phase_design']=dict(mode='unbalanced',load_phase_weights=[1,1,1])
            return dict(status='ready',plan=p,issues=[])
    r=design_response_from_request('\u751f\u621013\u8282\u70b9\u7684\u7535\u538b\u7075\u654f\u5ea6\u7814\u7a76\u9988\u7ebf\u3002',tmp_path,'bad_compile',model=InvalidModel())
    assert r['status']=='planning_failed' and 'outcome' not in r
    assert (tmp_path/'designs/bad_compile/result.json').exists()


def test_transfer_candidates_rank_predicted_effect_not_existing_line_loading(tmp_path):
    from feeder_agents.response_transmission import rank_parallel_actions
    from feeder_agents.response_schema import ResponseDesignPlan
    from feeder_agents.response_measurement import create_model,resolve_probes,measure
    from feeder_agents.response_design import score_response
    p=ResponseDesignPlan.model_validate(plan_payload(
        base_spec=dict(network_kind='transmission',n_buses=9,n_generators=3,total_mw=180,seed=41),
        probes=[dict(id='transfer',kind='transfer_p',injection_buses=['4'],withdrawal_buses=['8'],aggregation='max_abs')],
        targets=[dict(probe_id='transfer',reference='baseline_ratio',lower=.5,upper=.98)],
        search=dict(allowed_actions=['parallel_line'])))
    m=create_model(p.base_spec);probes=resolve_probes(m,p.probes)
    measured=measure(m,p.base_spec,probes,tmp_path)
    scored=score_response(measured,p.targets,measured)
    actions=[dict(kind='parallel_line',index=i,value=e['circuits']+1,proxy_effect=0)
             for i,e in enumerate(m['metadata']['branch_evidence']) if e['kind']=='line' and e['circuits']<4]
    ranked=rank_parallel_actions(m,probes,scored,actions)
    assert {a['index'] for a in ranked}=={a['index'] for a in actions}
    assert [a['dc_predicted_deficit'] for a in ranked]==sorted(a['dc_predicted_deficit'] for a in ranked)
    assert ranked[0]['dc_predicted_deficit']<sum(scored['deficits'])


def urban_response_plan():
    return plan_payload(base_spec=dict(network_kind='distribution',n_buses=25,
        total_kw_min=360,total_kw_max=360,seed=42,
        scenario=dict(kind='urban',engineering_profile='urban',layout='spatial_mst'),
        phase_design=dict(mode='unbalanced'),repair_policy=dict(strategy='none')),
        targets=[dict(probe_id='terminal',reference='baseline_ratio',lower=1.05,upper=1.25)],
        search=dict(allowed_actions=['replace_conductor'],max_rounds=4,candidates_per_round=8))


def test_catalogue_exhaustion_is_diagnosed_without_changing_permissions(tmp_path):
    from feeder_agents.response_design import run_response_design
    r=run_response_design(urban_response_plan(),tmp_path,'catalogue_limit')
    assert not r['target_met'] and r['protected_contract_preserved']
    assert r['diagnosis']['code']=='catalogue_direction_exhausted'
    assert r['diagnosis']['infeasibility_proven'] is False
    assert r['diagnosis']['requires_explicit_permission']
    assert r['accepted_steps']<4
    assert 'diagnosis.json' in r['artifacts']


def test_layout_response_action_requires_explicit_bounded_permission():
    from feeder_agents.response_schema import ResponseDesignPlan
    p=urban_response_plan();p['search']['allowed_actions'].append('scale_layout')
    with pytest.raises(ValueError,match='max_layout_scale_change'):ResponseDesignPlan.model_validate(p)
    p['search']['max_layout_scale_change']=.15
    assert ResponseDesignPlan.model_validate(p).search.max_layout_scale_change==.15
    p['base_spec']['scenario']['positions_km']=[[i*.05,0] for i in range(25)]
    with pytest.raises(ValueError,match='coordinates'):ResponseDesignPlan.model_validate(p)


def test_layout_response_design_meets_target_with_original_baseline_and_geometry(tmp_path):
    from feeder_agents.response_design import run_response_design,research_contract_preserved
    from feeder_agents.response_schema import ResponseDesignPlan
    from feeder_agents.schemas import Feeder
    p=urban_response_plan();p['search'].update(allowed_actions=['replace_conductor','scale_layout'],max_layout_scale_change=.15)
    r=run_response_design(p,tmp_path,'bounded_layout')
    assert r['target_met'] and r['verification_passed'] and r['protected_contract_preserved']
    assert 1.05<=r['selected']['targets'][0]['observed']<=1.25
    root=Path(r['directory']);read=lambda path:dict(kind='distribution',feeder=Feeder.model_validate(json.loads(path.read_text())))
    baseline=read(root/'baseline/model/feeder.json');selected=read(root/'selected/feeder.json')
    plan=ResponseDesignPlan.model_validate(p)
    assert research_contract_preserved(baseline,selected,plan)
    assert any(e['action']['kind']=='scale_layout' for e in json.loads((root/'history.json').read_text()) if e.get('committed'))
    assert not research_contract_preserved(baseline,selected,ResponseDesignPlan.model_validate(urban_response_plan()))
    selected['feeder'].buses[-1].x_km+=.001
    assert not research_contract_preserved(baseline,selected,plan)


def test_language_does_not_grant_layout_changes_without_user_permission(tmp_path):
    from feeder_agents.response_language import design_response_from_request
    class Model:
        def with_structured_output(self,*args,**kwargs):return self
        def invoke(self,messages):
            p=urban_response_plan();p['search'].update(allowed_actions=['replace_conductor','scale_layout'],max_layout_scale_change=.15)
            return dict(status='ready',plan=p,issues=[])
    r=design_response_from_request('\u751f\u621025\u8282\u70b9\u57ce\u5e02\u9988\u7ebf\uff0c\u672b\u7aef\u7075\u654f\u5ea6\u63d0\u9ad85%\u81f325%\uff0c\u53ea\u5141\u8bb8\u6362\u5bfc\u7ebf\u3002',tmp_path,'forbidden',execute=False,model=Model())
    assert r['status']=='needs_clarification' and not r['plan']
    r=design_response_from_request('\u751f\u621025\u8282\u70b9\u57ce\u5e02\u9988\u7ebf\uff0c\u672b\u7aef\u7075\u654f\u5ea6\u63d0\u9ad85%\u81f325%\uff0c\u5141\u8bb8\u6574\u4f53\u7a7a\u95f4\u7f29\u653e\u4e0d\u8d85\u8fc715%\u3002',tmp_path,'permitted',execute=False,model=Model())
    assert r['status']=='draft'


def test_layout_bounds_are_measured_against_original_not_previous_candidate(tmp_path):
    from feeder_agents.response_design import apply_response_action,research_contract_preserved
    from feeder_agents.response_measurement import create_model
    from feeder_agents.response_schema import ResponseDesignPlan
    p=urban_response_plan();p['search'].update(allowed_actions=['scale_layout'],max_layout_scale_change=.15)
    plan=ResponseDesignPlan.model_validate(p);original=create_model(plan.base_spec)
    action=dict(kind='scale_layout',factor=1.1,original_scale=1.1)
    first=apply_response_action(original,plan.base_spec,action)
    assert research_contract_preserved(original,first,plan)
    second=apply_response_action(first,plan.base_spec,dict(action,original_scale=1.21))
    assert not research_contract_preserved(original,second,plan)
    first['feeder'].loads[0].kw+=1
    assert not research_contract_preserved(original,first,plan)
