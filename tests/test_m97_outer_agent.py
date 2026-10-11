import copy

from feeder_agents.benchmark.m97_language import (
    build_cases, public_case, grade_evidence, required_checks, unauthorized_actions,
)


def test_public_messages_never_receive_answer_keys_or_witnesses():
    cases = build_cases()
    assert len(cases) >= 42
    assert len({c['category'] for c in cases}) == 7
    for case in cases:
        public = public_case(case)
        assert set(public) == {'id', 'messages'}
        assert 'expected' not in public and 'witness' not in public
        assert public['messages'] == case['messages']


def test_narrative_success_does_not_replace_artifact_acceptance():
    case = {'expected_outcome': 'generation', 'requirements': {'n_buses': 23}}
    result = grade_evidence(case, [], [], 'Successfully generated 23 buses.', [])
    assert not result['generation_success']
    assert not result['appropriate_outcome']


def test_contradiction_decision_is_not_generation_success():
    case = {'expected_outcome': 'clarification', 'requirements': {}}
    result = grade_evidence(case, [], [{'status': 'needs_clarification'}], '', [])
    assert result['appropriate_outcome']
    assert not result['generation_success']


def test_revision_requires_parent_identity_and_untouched_fields():
    case = {'expected_outcome': 'generation', 'requirements': {'n_buses': 23, 'total_kw': 120},
            'revision': True, 'required_revisions': 2, 'preserve_groups': ['topology', 'equipment']}
    artifact = {'facts': {'n_buses': 23, 'total_kw': 120}, 'accepted': True, 'usable': True}
    bad = grade_evidence(case, [artifact], [], '', [])
    assert not bad['generation_success']
    revisions = [{'parent_verified': True, 'preserved': {'topology': True, 'equipment': True}},
                 {'parent_verified': True, 'preserved': {'topology': True, 'equipment': False}}]
    bad = grade_evidence(case, [artifact], [], '', revisions)
    assert not bad['revision_preserved']
    revisions[-1]['preserved']['equipment'] = True
    good = grade_evidence(case, [artifact], [], '', revisions)
    assert good['generation_success']


def test_required_counts_units_and_prohibitions_are_independent():
    checks = required_checks({'n_buses': [31, 39], 'total_kw': 240, 'pv_ratio': .6},
                             {'n_buses': 35, 'total_kw': 240, 'pv_ratio': .6})
    assert all(checks.values())
    assert not required_checks({'total_kw': 240}, {'total_kw': .24})['total_kw']
    case = {'forbidden_actions': ['replace_conductor']}
    assert unauthorized_actions(case, [{'allowed_actions': ['scale_layout']}]) == []
    assert unauthorized_actions(case, [{'allowed_actions': ['replace_conductor']}])


def test_numeric_permission_does_not_authorize_equipment():
    case = {'forbidden_actions': ['replace_conductor'], 'max_layout_scale_change': .3}
    assert unauthorized_actions(case, [{'max_layout_scale_change': .5}])
    assert unauthorized_actions(case, [{'tier': 'equipment_geometry', 'max_changed_conductors': 2}])


def test_one_proposal_allows_prerequisites_and_executes_one_design():
    from feeder_agents.benchmark.m97_language_gate import ProposalGate
    gate=ProposalGate()
    for name in ['read_project_memory','describe_equipment_profiles','lookup_design_rules']:
        assert gate.start(name,{})
        gate.finish(name,{}, {})
        assert not gate.done
    assert gate.start('design_from_language',{'request':'Original user request','design_id':'one'})
    gate.finish('design_from_language',{}, {'status':'completed'})
    assert gate.done
    assert not gate.start('design_from_language',{'request':'Try again','design_id':'two'})


def test_one_proposal_allows_only_immutable_plan_execution():
    from feeder_agents.benchmark.m97_language_gate import ProposalGate
    gate=ProposalGate();assert gate.start('plan_experiment',{'parameters':{'n_buses':23}})
    gate.finish('plan_experiment',{}, {'plan_id':'original'})
    assert gate.start('describe_equipment_profiles',{})
    assert gate.start('execute_plan',{'plan_id':'original','experiment_id':'first'})
    gate.finish('execute_plan',{}, {})
    assert gate.done
    gate.reset();assert gate.start('plan_experiment',{})
    gate.finish('plan_experiment',{}, {'plan_id':'original'})
    assert not gate.start('execute_plan',{'plan_id':'changed','experiment_id':'second'})


def test_one_proposal_stops_after_failure_and_resets_per_user_revision():
    from feeder_agents.benchmark.m97_language_gate import ProposalGate
    gate=ProposalGate();assert gate.start('generate_cases',{})
    gate.finish('generate_cases',{}, {'status':'invalid_design'})
    assert gate.done
    gate.reset();assert gate.start('revise_existing_case',{'parent_id':'original','revision_id':'new'})


def test_provider_usage_is_counted_once_without_usage_metadata():
    from feeder_agents.benchmark.m97_language_analysis import usage_from_events
    usage={'prompt_tokens':100,'completion_tokens':20,'total_tokens':120}
    events=[dict(kind='model_end',response={'llm_output':{'token_usage':usage},'generations':[[{'message':{'response_metadata':{'token_usage':usage}}}]]})]
    result=usage_from_events(events)
    assert result['total_tokens']==120
    assert result['usage_calls_observed']==1


def test_first_turn_permissions_cannot_borrow_later_authorization():
    from feeder_agents.benchmark.m97_language_analysis import policy_for_turn
    case={'category':'permissions','forbidden_actions':[],'max_layout_scale_change':.3}
    assert unauthorized_actions(policy_for_turn(case,0),{'allowed_actions':['replace_conductor']})
    assert not unauthorized_actions(policy_for_turn(case,1),{'allowed_actions':['replace_conductor']})


def test_postrun_revision_audit_checks_intermediate_requirement():
    from feeder_agents.benchmark.m97_language_analysis import revision_audit
    groups={k:['same'] for k in ('topology','equipment','voltage','geometry','loads')}
    artifacts=[]
    for name,pv in [('base',.2),('r1',.3),('r2',.6)]:
        artifacts.append(dict(path=f'/workspace/experiments/{name}/sample_00000/feeder.json',accepted=True,
                              facts={'total_kw':120,'pv_ratio':pv},groups=copy.deepcopy(groups)))
    grade=dict(artifacts=artifacts,revisions=[dict(revision_id='r1',parent_id='base',parent_verified=True),
                                            dict(revision_id='r2',parent_id='r1',parent_verified=True)])
    events=[dict(kind='tool_end',turn=t,output={'directory':f'/workspace/revisions/r{t}'}) for t in (1,2)]
    case=dict(id='revisions_01',revision=True,witness_spec={'total_kw_min':120})
    audit=revision_audit(case,grade,events)
    assert not audit['passed']
    assert audit['steps'][1]['passed']
    assert not audit['steps'][0]['checks']['pv_ratio']
    grade['artifacts'][1]['facts']['pv_ratio']=.4
    assert revision_audit(case,grade,events)['passed']


def test_chain_audit_separates_routing_execution_acceptance_and_completion():
    from feeder_agents.benchmark.m97_language_analysis import chain_audit
    case={'expected_outcome':'generation','task':'voltage_control'}
    row={'interpretation_passed':12,'interpretation_total':12,'generation_success':False,'status':'turn_error'}
    grade={'artifacts':[{'accepted':True,'usable':True,'facts':{}}]}
    events=[{'kind':'tool_start','name':'design_electrical_response','turn':0}]
    audit=chain_audit(case,row,grade,events,{'applicable':False,'passed':True,'steps':[]})
    assert audit['structured_requirements_complete']
    assert not audit['compatible_entrypoint_invoked']
    assert audit['saved_artifact'] and audit['accepted_runnable_artifact']
    assert not audit['request_fulfilled'] and not audit['conversation_completed']
    events[0]['name']='design_research_grid'
    assert chain_audit(case,row,grade,events,{'applicable':False,'passed':True,'steps':[]})['compatible_entrypoint_invoked']


def test_unique_request_clustering_ignores_ids_and_normalizes_whitespace():
    from feeder_agents.benchmark.m97_language_analysis import request_key
    assert request_key({'id':'a','messages':['Make  23 buses.','Set PV to 40%.']}) == request_key({'id':'b','messages':['Make 23 buses.','Set PV to 40%.']})
    assert request_key({'messages':['Make 23 buses.','Set PV to 40%.']}) != request_key({'messages':['Make 23 buses.','Set PV to 60%.']})


def test_negative_evidence_audit_normalizes_markdown_without_changing_frozen_grade():
    from feeder_agents.benchmark.m97_language_analysis import negative_reason_audit
    case={'id':'unsupported_01','expected_outcome':'unsupported','messages':['Certify N-1.']}
    raw={'turns':[{'messages':[{'type':'human','content':'Certify N-1.'},{'type':'ai','content':'N-1 certification is not a supported capability. I cannot deliver it.'}]}]}
    result=negative_reason_audit(case,[],raw)
    assert result['expected_decision_and_reason']
    raw['turns'][0]['messages'][-1]['content']='N-1 certification is available; I completed it.'
    assert not negative_reason_audit(case,[],raw)['expected_decision_and_reason']


def test_authorization_audit_checks_first_turn_committed_geometry(tmp_path):
    import json
    from feeder_agents.benchmark.m97_language_analysis import authorization_audit
    folder=tmp_path/'response_designs'/'first'
    (folder/'baseline/model').mkdir(parents=True);(folder/'selected').mkdir()
    before={'buses':[{'id':'s','x_km':0,'y_km':0},{'id':'b','x_km':1,'y_km':0}],
            'lines':[{'id':'l','bus1':'s','bus2':'b','length_km':1,'conductor':'same'}]}
    after=copy.deepcopy(before);after['buses'][1]['x_km']=1.2;after['lines'][0]['length_km']=1.2
    (folder/'baseline/model/feeder.json').write_text(json.dumps(before));(folder/'selected/feeder.json').write_text(json.dumps(after))
    case={'category':'permissions','forbidden_actions':[],'max_layout_scale_change':.3}
    events=[{'kind':'tool_start','name':'design_electrical_response','turn':0,'inputs':{'design_id':'first'}}]
    audit=authorization_audit(case,events,tmp_path)
    assert audit['physical_selected_unauthorized_any'] and audit['committed_any']
    assert not audit['final_narrative_delivered_unauthorized_any']
    raw={'turns':[{'messages':[{'type':'ai','content':f'Saved result: {folder}'}]}]}
    assert authorization_audit(case,events,tmp_path,raw)['final_narrative_delivered_unauthorized_any']
    events[0]['turn']=1
    assert not authorization_audit(case,events,tmp_path)['physical_selected_unauthorized_any']


def test_postrun_authorization_ignores_null_optional_schema_bound():
    from feeder_agents.benchmark.m97_language_analysis import audit_unauthorized_actions
    assert audit_unauthorized_actions({'max_layout_scale_change':0},{'max_layout_scale_change':None}) == []
    assert audit_unauthorized_actions({'max_layout_scale_change':0},{'max_layout_scale_change':.3})


def test_finite_q_audit_requires_actual_solver_intervention_not_metadata():
    from feeder_agents.benchmark.m97_language_semantic import finite_q_checks
    base={'converged':True,'load_kw':120.,'generation_kw':0.,'load_kvar':40.,'bus_phase_voltage_pu':{'b':{'1':.99}}}
    plus=dict(base,load_kvar=30.,bus_phase_voltage_pu={'b':{'1':.991}})
    assert all(finite_q_checks(base,plus,.01).values())
    assert not all(finite_q_checks(base,plus,.02).values())
    assert not all(finite_q_checks(base,{},.01).values())
    assert not all(finite_q_checks(base,base,.01).values())


def test_finite_pv_audit_does_not_relabel_a_p_derivative_as_pv_off():
    from feeder_agents.benchmark.m97_language_semantic import finite_pv_checks
    on={'converged':True,'generation_kw':72.,'load_kw':120.,'bus_phase_voltage_pu':{'b':{'1':1.01}}}
    off=dict(on,generation_kw=0.,bus_phase_voltage_pu={'b':{'1':.99}})
    assert all(finite_pv_checks(on,off,72).values())
    assert not all(finite_pv_checks(on,on,72).values())
    assert not all(finite_pv_checks(on,{},72).values())


def test_finite_transfer_audit_requires_exact_recorded_one_mw_state():
    import numpy as np
    from feeder_agents.benchmark.m97_language_semantic import finite_transfer_checks
    bus=np.zeros((2,13));bus[:,2]=50;bus[:,3]=30;bus[:,7]=1
    gen=np.zeros((1,21));gen[0,1]=100;gen[0,3]=100;gen[0,4]=-100;gen[0,8]=200
    branch=np.zeros((1,17));branch[0,5]=100
    base={'bus':bus.tolist(),'gen':gen.tolist(),'branch':branch.tolist()}
    after_bus=bus.copy();after_bus[:,2]=[49,51];after_branch=branch.copy();after_branch[0,13]=1;after_branch[0,15]=-1
    altered={'converged':True,'solution':{'bus':after_bus.tolist(),'gen':gen.tolist(),'branch':after_branch.tolist()}}
    assert all(finite_transfer_checks(base,altered,1.,{}).values())
    assert not all(finite_transfer_checks(base,altered,.5,{}).values())
    assert not all(finite_transfer_checks(base,{},1.,{}).values())

    altered['converged']=False
    assert not all(finite_transfer_checks(base,altered,1.,{}).values())
