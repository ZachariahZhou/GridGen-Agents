"""Task suitability must be measured on the delivered grid, not inferred from prose."""
import copy
import json

import pytest


def task_payload(task='voltage_control', **changes):
    base=dict(network_kind='distribution',n_buses=13,voltage_kv=10,total_kw_min=180,total_kw_max=180,
        pv_ratio=.6 if task=='static_pv_impact' else 0,seed=42,
        scenario=dict(kind='rural',engineering_profile='rural',layout='spatial_mst'),
        phase_design=dict(mode='unbalanced',single_phase_laterals=0,two_phase_laterals=0),
        repair_policy=dict(strategy='none'))
    if task=='transmission_transfer':
        base=dict(network_kind='transmission',n_buses=17,n_generators=3,total_mw=120,voltage_kv=110,seed=42)
    p=dict(task=task,research_question='Generate a reusable grid for this research task',base_spec=base)
    p.update(changes);return p


def test_task_contract_rejects_wrong_domain_no_pv_and_conflicting_permissions():
    from feeder_agents.research_schema import ResearchTaskPlan
    p=task_payload('static_pv_impact');p['base_spec']['pv_ratio']=0
    with pytest.raises(ValueError,match='PV'):ResearchTaskPlan.model_validate(p)
    p=task_payload();p['task']='transmission_transfer'
    with pytest.raises(ValueError,match='transmission'):ResearchTaskPlan.model_validate(p)
    with pytest.raises(ValueError,match='geometry'):
        ResearchTaskPlan.model_validate(task_payload(preserve_geometry=True,search=dict(allowed_actions=['scale_layout'],max_layout_scale_change=.3)))
    with pytest.raises(ValueError,match='interval'):
        ResearchTaskPlan.model_validate(task_payload(response_lower=2,response_upper=1))


@pytest.mark.parametrize('task',['voltage_control','static_pv_impact','transmission_transfer'])
def test_compiled_roles_are_real_frozen_ports_with_declared_defaults(task):
    from feeder_agents.research_tasks import compile_research_task
    from feeder_agents.response_schema import ResponseDesignPlan
    from feeder_agents.response_measurement import create_model
    result=compile_research_task(task_payload(task));plan=ResponseDesignPlan.model_validate(result['response_plan'])
    model=create_model(plan.base_spec);probe=plan.probes[0]
    assert probe.injection_buses
    assert result['context']['probe']['injection_buses']==probe.injection_buses
    assert result['origins']['response_interval']=='research_protocol_default'
    if task=='transmission_transfer':
        assert not set(probe.injection_buses)&set(probe.withdrawal_buses)
        assert set(probe.injection_buses+probe.withdrawal_buses)<=set(str(int(v)) for v in model['case']['bus'][:,0])
    else:
        assert len(model['feeder'].buses)==13
        assert set(probe.injection_buses)<={l.bus for l in model['feeder'].loads if task!='static_pv_impact' or l.pv_kw>0}


def test_fixed_geometry_removes_default_scaling_and_explicit_bounds_survive():
    from feeder_agents.research_tasks import compile_research_task
    result=compile_research_task(task_payload(preserve_geometry=True,response_lower=.7,response_upper=.9,injection_buses=['b1']))
    assert result['response_plan']['search']['allowed_actions']==['replace_conductor']
    assert result['response_plan']['targets'][0]['lower']==.7
    assert result['response_plan']['probes'][0]['injection_buses']==['b1']
    assert result['origins']['response_interval']=='user'


@pytest.mark.parametrize('task',['voltage_control','static_pv_impact','transmission_transfer'])
def test_finite_experiments_use_actual_solver_and_leave_base_model_unchanged(tmp_path,task):
    from feeder_agents.research_tasks import compile_research_task
    from feeder_agents.research_measurement import evaluate_research_task
    from feeder_agents.response_schema import ResponseDesignPlan
    from feeder_agents.response_measurement import create_model
    from feeder_agents.response_design import protected_fingerprint
    compiled=compile_research_task(task_payload(task));spec=ResponseDesignPlan.model_validate(compiled['response_plan']).base_spec
    model=create_model(spec);before=protected_fingerprint(model)
    out=evaluate_research_task(model,spec,compiled['context'],tmp_path)
    assert out['accepted'],out
    assert out['metrics']['max_voltage_change_pu']>0
    assert protected_fingerprint(model)==before
    assert (tmp_path/'task_validation.json').is_file()
    if task=='static_pv_impact':
        assert out['metrics']['pv_on_kw']==pytest.approx(108)
        assert out['metrics']['pv_off_kw']==0
        assert out['metrics']['load_kw_difference']==pytest.approx(0,abs=1e-4)
    if task=='transmission_transfer':
        assert out['metrics']['net_transfer_injection_mw']==pytest.approx(0)
        assert out['metrics']['max_branch_flow_change_mw']>0


def test_failed_downstream_experiment_prevents_task_success(tmp_path):
    from feeder_agents.research_tasks import run_research_task
    p=task_payload(support_mvar=100,response_lower=.5,response_upper=2,search=dict(max_rounds=0))
    result=run_research_task(p,tmp_path,'impossible_support')
    assert not result['target_met']
    assert not result['task_validation']['accepted']
    assert result['electrical_base_accepted']
    assert not result['diagnosis']['infeasibility_proven']


@pytest.mark.parametrize('task',['voltage_control','static_pv_impact','transmission_transfer'])
def test_complete_task_exports_and_reader_detects_corruption(tmp_path,task):
    from feeder_agents.research_tasks import run_research_task,read_research_task_result
    p=task_payload(task,response_lower=.9,response_upper=1.1,search=dict(max_rounds=0))
    result=run_research_task(p,tmp_path,'complete')
    assert result['target_met'] and result['verification_passed']
    root=tmp_path/'research_tasks/complete'
    assert read_research_task_result(root)['target_met']
    assert (root/'dataset.zip').is_file() and (root/'selected/visualization.html').is_file()
    (root/'suitability.json').write_text('{}')
    with pytest.raises(ValueError,match='changed'):read_research_task_result(root)


def test_research_language_route_preserves_exact_counts_and_supports_draft(tmp_path):
    from feeder_agents.design import design_from_request
    class Model:
        def with_structured_output(self,*args,**kwargs):return self
        def invoke(self,messages):return dict(status='ready',plan=task_payload(),issues=[])
    good=design_from_request('\u751f\u621013\u4e2a\u8282\u70b9\u7684\u519c\u6751\u9988\u7ebf\uff0c\u7528\u4e8e\u7814\u7a76\u7535\u538b\u63a7\u5236\u3002',tmp_path,'draft',execute=False,model=Model())
    assert good['status']=='draft' and good['plan_type']=='research_task'
    bad=design_from_request('\u751f\u621037\u4e2a\u8282\u70b9\u7684\u519c\u6751\u9988\u7ebf\uff0c\u7528\u4e8e\u7814\u7a76\u7535\u538b\u63a7\u5236\u3002',tmp_path,'bad',execute=False,model=Model())
    assert bad['status']=='needs_clarification' and not (tmp_path/'research_tasks/bad').exists()


def test_denied_geometry_cannot_be_granted_by_task_language_model(tmp_path):
    from feeder_agents.research_language import design_research_from_request
    class Model:
        def with_structured_output(self,*args,**kwargs):return self
        def invoke(self,messages):return dict(status='ready',plan=task_payload(),issues=[])
    result=design_research_from_request('\u751f\u621013\u8282\u70b9\u9988\u7ebf\u7528\u4e8e\u7814\u7a76\u7535\u538b\u63a7\u5236\u3002\u56fa\u5b9a\u5750\u6807\uff0c\u4e0d\u5141\u8bb8\u7a7a\u95f4\u7f29\u653e\u3002',tmp_path,'fixed',execute=False,model=Model())
    assert result['status']=='needs_clarification'


def test_negated_research_task_is_not_routed_as_a_positive_task():
    from feeder_agents.research_language import is_research_request
    assert not is_research_request('Generate a feeder; do not study voltage control.')
    assert not is_research_request('\u751f\u6210\u519c\u6751\u9988\u7ebf\uff0c\u4e0d\u7814\u7a76\u5149\u4f0f\u63a5\u5165\u5f71\u54cd\u3002')
    assert is_research_request('Generate a grid to study static PV integration impacts.')


def test_research_ui_reconstructs_acceptance_from_hashed_artifacts(tmp_path):
    from feeder_agents.research_language import design_research_from_request
    from feeder_agents.ui_saved_records import load_verified_design_record
    class Model:
        def with_structured_output(self,*args,**kwargs):return self
        def invoke(self,messages):return dict(status='ready',plan=task_payload(response_lower=.9,response_upper=1.1,search=dict(max_rounds=0)),issues=[])
    project=tmp_path/'projects/default'
    result=design_research_from_request('\u751f\u621013\u8282\u70b9\u9988\u7ebf\u7528\u4e8e\u7535\u538b\u63a7\u5236\u7814\u7a76\uff0c\u54cd\u5e94\u500d\u73870.9\u52301.1\uff0c\u4ec5\u6d4b\u91cf\u3002',project,'ui',model=Model())
    path=project/'designs/ui/result.json'
    display,error=load_verified_design_record(path,tmp_path)
    assert error is None and display['verified_family']=='research_task'
    assert display['outcome']['target_met']==result['outcome']['target_met']
    (project/'research_tasks/ui/suitability.json').write_text('{}')
    display,error=load_verified_design_record(path,tmp_path)
    assert display is None and error


def test_research_cli_executes_structured_task_without_llm(tmp_path,monkeypatch,capsys):
    from feeder_agents.cli import main
    plan=tmp_path/'task.json';plan.write_text(json.dumps(task_payload(response_lower=.9,response_upper=1.1,search=dict(max_rounds=0))))
    monkeypatch.setattr('sys.argv',['feeder-agents','--workspace',str(tmp_path),'research-design','--plan',str(plan),'--id','cli'])
    main();result=json.loads(capsys.readouterr().out)
    assert result['target_met'] and result['task']=='voltage_control'


def test_task_rejects_unsupported_timeseries_before_model_call(tmp_path):
    from feeder_agents.research_language import design_research_from_request
    result=design_research_from_request('\u7814\u7a76\u5149\u4f0f\u63a5\u5165\u5f71\u54cd\uff0c\u8981\u6c42\u751f\u62108760\u5c0f\u65f6\u65f6\u5e8f\u6570\u636e\u3002',tmp_path,'unsupported')
    assert result['status']=='unsupported' and result['interpreter_model'] is None


def test_research_cache_can_resume_identical_task(tmp_path):
    from feeder_agents.research_tasks import run_research_task
    p=task_payload(response_lower=.9,response_upper=1.1,search=dict(max_rounds=0))
    a=run_research_task(p,tmp_path,'resume');b=run_research_task(p,tmp_path,'resume')
    assert a['result_hash']==b['result_hash']
    with pytest.raises(ValueError,match='changed'):
        run_research_task(dict(p,support_mvar=.02),tmp_path,'resume')


@pytest.mark.parametrize('text,patch',[
    ('\u751f\u621013\u8282\u70b9\u9988\u7ebf\u7528\u4e8e\u7535\u538b\u63a7\u5236\uff0c\u5141\u8bb8\u6574\u4f53\u7a7a\u95f4\u7f29\u653e\u6700\u591a10%\u3002',{}),
    ('\u751f\u621013\u8282\u70b9\u9988\u7ebf\u7528\u4e8e\u7535\u538b\u63a7\u5236\uff0c\u53ea\u5141\u8bb8\u66f4\u6362\u5bfc\u7ebf\u3002',{}),
    ('\u751f\u621013\u8282\u70b9\u9988\u7ebf\u7528\u4e8e\u7535\u538b\u63a7\u5236\uff0c\u603b\u8d1f\u8377\u5fc5\u987b\u4e3a300 kW\u3002',{}),
    ('\u751f\u621013\u8282\u70b9\u9988\u7ebf\u7528\u4e8e\u7535\u538b\u63a7\u5236\uff0c\u65e0\u529f\u652f\u6491\u9884\u7b97\u5fc5\u987b\u662f100 kvar\u3002',{}),
    ('\u751f\u621013\u8282\u70b9\u9988\u7ebf\u7528\u4e8e\u7535\u538b\u63a7\u5236\uff0c\u8981\u6c4220kV\u3002',{}),
    ('\u751f\u621013\u8282\u70b9\u9988\u7ebf\u7528\u4e8e\u7535\u538b\u63a7\u5236\uff0c\u8981\u6c42\u9ad8\u7075\u654f\u5ea6\u3002',dict(response_lower=1.1,response_upper=1.3)),
    ('\u7814\u7a76\u5149\u4f0f\u63a5\u5165\u5f71\u54cd\uff0c\u4f46\u4e0d\u5f97\u63a5\u5165\u4efb\u4f55\u5149\u4f0f\u3002',dict(task='static_pv_impact')),
    ('Study transmission transfer, with a transfer budget of 20 MW.',dict(task='transmission_transfer')),
])
def test_explicit_source_requirements_cannot_be_replaced_by_task_defaults(text,patch):
    from feeder_agents.research_language import _source_issues
    from feeder_agents.research_schema import ResearchTaskPlan
    task=patch.get('task','voltage_control');p=task_payload(task);p.update(patch)
    assert _source_issues(text,ResearchTaskPlan.model_validate(p),None)


def test_source_unit_conversion_accepts_matching_mvar_and_demand():
    from feeder_agents.research_language import _source_issues
    from feeder_agents.research_schema import ResearchTaskPlan
    p=task_payload(support_mvar=.1,preserve_geometry=True,response_lower=.9,response_upper=1.1)
    assert not _source_issues('\u751f\u621013\u8282\u70b910kV\u9988\u7ebf\u7528\u4e8e\u7535\u538b\u63a7\u5236\uff0c\u603b\u8d1f\u8377\u5fc5\u987b\u4e3a180 kW\uff0c\u65e0\u529f\u652f\u6491\u9884\u7b97\u5fc5\u987b\u662f100 kvar\uff0c\u54cd\u5e94\u500d\u73870.9\u52301.1\uff0c\u56fa\u5b9a\u5750\u6807\u3002',ResearchTaskPlan.model_validate(p),None)


def test_task_local_negation_does_not_shadow_the_affirmative_task():
    from feeder_agents.research_language import research_task_names
    assert research_task_names('Generate a feeder for voltage control, not static PV integration impact.')=={'voltage_control'}
    assert not research_task_names('Generate a feeder without voltage control.')


@pytest.mark.parametrize('clause,reference,lower,upper',[
    ('\u54cd\u5e94\u500d\u7387\u81f3\u5c111.1','baseline_ratio',1.1,None),
    ('response ratio at most 1.2','baseline_ratio',None,1.2),
    ('\u54cd\u5e94\u500d\u7387\u81f3\u5c111.1\uff0c\u54cd\u5e94\u500d\u7387\u81f3\u591a1.3','baseline_ratio',1.1,1.3),
    ('\u7075\u654f\u5ea6\u81f3\u5c110.01 pu/MVAr','absolute',.01,None),
    ('sensitivity at most 0.02 pu/MVAr','absolute',None,.02),
])
def test_response_source_preserves_one_sided_and_combined_bounds(clause,reference,lower,upper):
    from feeder_agents.research_language import _source_issues
    from feeder_agents.research_schema import ResearchTaskPlan
    request='\u751f\u621013\u8282\u70b9\u9988\u7ebf\u7528\u4e8e\u7535\u538b\u63a7\u5236\uff0c'+clause+'。'
    plan=ResearchTaskPlan.model_validate(task_payload(response_reference=reference,response_lower=lower,response_upper=upper))
    assert not _source_issues(request,plan,None)
    equality=plan.model_copy(update=dict(response_lower=lower or upper,response_upper=lower or upper))
    assert _source_issues(request,equality,None)


def test_contradictory_response_source_cannot_be_silently_reconciled():
    from feeder_agents.research_language import _source_issues
    from feeder_agents.research_schema import ResearchTaskPlan
    plan=ResearchTaskPlan.model_validate(task_payload(response_lower=1.2,response_upper=1.3))
    assert _source_issues('\u7535\u538b\u63a7\u5236\uff0c\u54cd\u5e94\u500d\u7387\u81f3\u5c111.2\uff0c\u54cd\u5e94\u500d\u7387\u81f3\u591a1.1\u3002',plan,None)


def test_task_source_error_gets_one_audited_llm_correction(tmp_path):
    from feeder_agents.research_language import design_research_from_request
    class Model:
        calls=0
        def with_structured_output(self,*args,**kwargs):return self
        def invoke(self,messages):
            self.calls+=1
            p=task_payload(response_lower=1.1,response_upper=1.3) if self.calls==1 else task_payload()
            return dict(status='ready',plan=p,issues=[])
    model=Model()
    result=design_research_from_request('\u751f\u621013\u8282\u70b9\u9988\u7ebf\u7528\u4e8e\u7535\u538b\u63a7\u5236\uff0c\u54cd\u5e94\u533a\u95f4\u4f7f\u7528\u9ed8\u8ba4\u534f\u8bae\u3002',tmp_path,'corrected',execute=False,model=model)
    assert result['status']=='draft' and model.calls==2
    records=sorted((tmp_path/'designs/corrected/model_calls').glob('*/attempt_*.json'))
    assert [json.loads(p.read_text())['status'] for p in records]==['invalid','valid']
    assert 'numerical source' in json.loads(records[0].read_text())['validation_error']
