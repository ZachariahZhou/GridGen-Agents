"""Permission revisions preserve the task and bound cumulative equipment edits."""
import copy

import pytest


def record_work(search=40,total=52):
    from feeder_agents.solver_accounting import record_ac_solve,solver_phase
    with solver_phase('search'):
        for _ in range(search):record_ac_solve('test')
    with solver_phase('verification'):
        for _ in range(total-search):record_ac_solve('test')


def plan_payload():
    return dict(research_question='Selective response under bounded design freedoms.',
        base_spec=dict(network_kind='distribution',n_buses=13,seed=42,
            scenario=dict(kind='urban',engineering_profile='urban',layout='spatial_mst'),
            phase_design=dict(mode='unbalanced',single_phase_laterals=0,two_phase_laterals=0)),
        probes=[dict(id='a',kind='voltage_q')],targets=[dict(probe_id='a',reference='baseline_ratio',lower=1.1,upper=1.25)],
        search=dict(allowed_actions=['scale_layout','scale_subtree'],max_layout_scale_change=.2,
            max_branch_length_change=.2,max_node_displacement_km=.2,max_search_ac_calls=160))


def test_revision_adds_only_the_explicit_permission_fields():
    from feeder_agents.response_permissions import prepare_permission_revision
    from feeder_agents.response_schema import ResponseDesignPlan
    original=ResponseDesignPlan.model_validate(plan_payload()).model_dump(mode='json')
    before=copy.deepcopy(original)
    revision=prepare_permission_revision(original,dict(tier='equipment_topology',max_changed_conductors=4,
        max_rewired_lines=2,max_rewire_length_ratio=2,max_node_degree=4))
    assert original==before and revision['status']=='draft'
    assert revision['added_actions']==['replace_conductor','rewire_branch']
    revised=copy.deepcopy(revision['plan'])
    for key in ('allowed_actions','max_changed_conductors','max_rewired_lines','max_rewire_length_ratio','max_node_degree'):
        revised['search'][key]=original['search'][key]
    assert revised==original


@pytest.mark.parametrize('grant',[
    dict(tier='equipment'),dict(tier='equipment',max_changed_conductors=0),
    dict(tier='equipment_topology',max_changed_conductors=4),
    dict(tier='equipment',max_changed_conductors=4,max_rewired_lines=2),
    dict(tier='all',max_changed_conductors=4),
])
def test_incomplete_or_unrecognized_grants_are_rejected(grant):
    from feeder_agents.response_permissions import prepare_permission_revision
    with pytest.raises(ValueError):prepare_permission_revision(plan_payload(),grant)


def test_conductor_cap_requires_conductor_permission():
    from feeder_agents.response_schema import ResponseDesignPlan
    p=plan_payload();p['search']['max_changed_conductors']=2
    with pytest.raises(ValueError,match='replace_conductor'):
        ResponseDesignPlan.model_validate(p)


def test_equipment_cap_is_cumulative_and_restoration_releases_capacity():
    from feeder_agents.response_schema import ResponseDesignPlan
    from feeder_agents.response_measurement import create_model
    from feeder_agents.response_design import apply_response_action,research_contract_preserved
    from feeder_agents.response_distribution import compatible_conductors
    from feeder_agents.rules import data
    p=plan_payload();p['search'].update(allowed_actions=['replace_conductor'],max_changed_conductors=1)
    for name in ('max_layout_scale_change','max_branch_length_change','max_node_displacement_km'):p['search'].pop(name)
    plan=ResponseDesignPlan.model_validate(p);original=create_model(plan.base_spec)
    moves=[]
    for line in original['feeder'].lines[:2]:
        name=next(k for k in compatible_conductors(line,plan.base_spec,data('conductors.json')) if k!=line.conductor)
        moves.append(dict(kind='replace_conductor',line_id=line.id,conductor=name))
    first=apply_response_action(original,plan.base_spec,moves[0])
    assert research_contract_preserved(original,first,plan)
    second=apply_response_action(first,plan.base_spec,moves[1])
    assert not research_contract_preserved(original,second,plan)
    compound=apply_response_action(original,plan.base_spec,dict(kind='compound',actions=moves))
    assert not research_contract_preserved(original,compound,plan)
    restored=apply_response_action(second,plan.base_spec,dict(moves[0],conductor=original['feeder'].lines[0].conductor))
    assert research_contract_preserved(original,restored,plan)


def test_agent_permission_tool_drafts_without_ac_calls(tmp_path):
    from feeder_agents.agent import make_tools
    from feeder_agents.solver_accounting import SolverMeter
    tools={tool.name:tool for tool in make_tools(tmp_path,'test')}
    assert 'revise_electrical_response_permissions' in tools
    with SolverMeter() as meter:
        output=tools['revise_electrical_response_permissions'].invoke(dict(plan=plan_payload(),
            permissions=dict(tier='equipment',max_changed_conductors=4),design_id='expanded_draft'))
    assert output['status']=='draft'
    assert output['plan']['search']['max_changed_conductors']==4
    assert meter.snapshot()['total_ac_calls']==0


@pytest.mark.parametrize('cap,issue',[(None,True),(5,True),(4,False),(2,False)])
@pytest.mark.parametrize('request_text',[
    'Allow replacing up to 4 conductors.', 'Replace no more than 4 conductors.',
    'Allow at most 4 conductor replacements.', '\u6700\u591a\u66f4\u63624\u6839\u5bfc\u7ebf\u3002',
])
def test_language_equipment_limit_cannot_be_omitted_or_exceeded(cap,issue,request_text):
    from feeder_agents.response_language import _local_permission_issues
    from feeder_agents.response_schema import ResponseSearch
    search=ResponseSearch(allowed_actions=['replace_conductor'],max_changed_conductors=cap)
    assert bool(_local_permission_issues(request_text,search)) is issue


def test_explicit_geometry_revision_preserves_targets_and_solver_budget():
    from feeder_agents.response_permissions import prepare_permission_revision
    p=plan_payload()
    result=prepare_permission_revision(p,dict(tier='equipment_geometry',max_changed_conductors=4,
        max_layout_scale_change=.5,max_branch_length_change=.5,max_node_displacement_km=.2))
    assert result['plan']['search']['max_branch_length_change']==.5
    assert result['plan']['targets']==p['targets']
    assert result['plan']['search']['max_search_ac_calls']==160


def test_unrelated_topology_limit_is_not_a_conductor_cap():
    from feeder_agents.response_language import _local_permission_issues
    from feeder_agents.response_schema import ResponseSearch
    assert not _local_permission_issues('Choose compatible conductors. Maximum degree is 4.',
        ResponseSearch(allowed_actions=['replace_conductor']))


def test_permission_ladder_stops_at_first_success_and_records_total_work(tmp_path,monkeypatch):
    from feeder_agents.response_permission_workflow import run_permission_ladder
    import feeder_agents.response_permission_workflow as workflow
    monkeypatch.setattr(workflow,"_baseline_identity",lambda result:result["baseline"])
    import feeder_agents.response_design as design
    calls=[]
    def run(plan,workspace,design_id):
        record_work()
        calls.append(plan);success=len(calls)==2
        return dict(status='completed' if success else 'target_not_met',target_met=success,
            protected_contract_preserved=True,selected=dict(base_accepted=True,valid=True,deficits=[0 if success else .1]),
            baseline=dict(base_accepted=True,valid=True,probes={'a':{'value':.2,'probe':{'id':'a'}}}),
            result_hash=str(len(calls)),directory=str(tmp_path/design_id),
            solver_accounting=dict(total_ac_calls=52,by_phase={'search':40}))
    monkeypatch.setattr(design,'run_response_design',run)
    grants=[dict(tier='equipment',max_changed_conductors=4),
        dict(tier='equipment_topology',max_changed_conductors=4,max_rewired_lines=2,max_rewire_length_ratio=2,max_node_degree=4),
        dict(tier='equipment_geometry',max_changed_conductors=4,max_layout_scale_change=.5,max_branch_length_change=.5,max_node_displacement_km=.2)]
    output=run_permission_ladder(plan_payload(),grants,tmp_path,'ladder')
    assert len(calls)==2
    assert output['target_met'] and output['selected_stage']==2
    assert output['solver_accounting']['search_ac_calls']==80
    assert output['solver_accounting']['total_ac_calls']==104
    assert all(c['base_spec']==calls[0]['base_spec'] and c['targets']==calls[0]['targets'] for c in calls)


def test_permission_ladder_retains_best_accepted_physical_model_on_failure(tmp_path,monkeypatch):
    from feeder_agents.response_permission_workflow import run_permission_ladder
    import feeder_agents.response_permission_workflow as workflow
    monkeypatch.setattr(workflow,"_baseline_identity",lambda result:result["baseline"])
    import feeder_agents.response_design as design
    def run(plan,workspace,design_id):
        record_work()
        first=plan['search']['max_changed_conductors']==1
        return dict(status='target_not_met',target_met=False,protected_contract_preserved=True,
            selected=dict(base_accepted=True,valid=True,deficits=[.1 if first else .3]),
            baseline=dict(base_accepted=True,valid=True,probes={'a':{'value':.2,'probe':{'id':'a'}}}),
            result_hash='first' if first else 'second',directory=str(tmp_path/design_id),
            solver_accounting=dict(total_ac_calls=52,by_phase={'search':40}))
    monkeypatch.setattr(design,'run_response_design',run)
    output=run_permission_ladder(plan_payload(),[dict(tier='equipment',max_changed_conductors=n) for n in (1,2)],tmp_path,'unmet')
    assert not output['target_met'] and output['selected_stage']==1


@pytest.mark.parametrize('failure',['baseline_mismatch','partial_solve'])
def test_ladder_counts_failed_stage_work_and_preserves_attempt_identity(tmp_path,monkeypatch,failure):
    import json
    import feeder_agents.response_design as design
    import feeder_agents.response_permission_workflow as workflow
    monkeypatch.setattr(workflow,'_baseline_identity',lambda result:result['baseline'])
    calls=[]
    def run(plan,workspace,design_id):
        calls.append(design_id)
        if len(calls)==2 and failure=='partial_solve':
            record_work(search=3,total=5)
            raise RuntimeError('Interrupted stage')
        record_work()
        return dict(target_met=False,protected_contract_preserved=True,
            selected=dict(base_accepted=True,valid=True,deficits=[.1]),
            baseline=len(calls),result_hash=str(len(calls)),directory=str(tmp_path/design_id),
            solver_accounting=dict(total_ac_calls=52,by_phase={'search':40}))
    monkeypatch.setattr(design,'run_response_design',run)
    with pytest.raises((ValueError,RuntimeError)):
        workflow.run_permission_ladder(plan_payload(),[dict(tier='equipment',max_changed_conductors=n) for n in (1,2)],tmp_path,'failure')
    error=json.loads((tmp_path/'permission_ladders/failure/error.json').read_text())
    expected=(80,104) if failure=='baseline_mismatch' else (43,57)
    assert error['solver_accounting']['search_ac_calls']==expected[0]
    assert error['solver_accounting']['total_ac_calls']==expected[1]
    assert error['failed_stage']['design_id']==calls[-1]
    assert error['failed_stage']['directory']
    assert error['failed_stage']['stage']==2


def test_cached_stage_accounting_is_historical_not_new_work(tmp_path,monkeypatch):
    import feeder_agents.response_design as design
    import feeder_agents.response_permission_workflow as workflow
    monkeypatch.setattr(workflow,'_baseline_identity',lambda result:0)
    monkeypatch.setattr(design,'run_response_design',lambda *args:dict(target_met=True,
        protected_contract_preserved=True,selected=dict(base_accepted=True,valid=True,deficits=[0]),
        result_hash='cached',directory=str(tmp_path/'cached'),
        solver_accounting=dict(total_ac_calls=52,by_phase={'search':40})))
    result=workflow.run_permission_ladder(plan_payload(),[dict(tier='equipment',max_changed_conductors=4)],tmp_path,'cached')
    assert result['solver_accounting']['total_ac_calls']==0
    assert result['stages'][0]['historical_solver_accounting']['total_ac_calls']==52
