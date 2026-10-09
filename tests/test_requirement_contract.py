import pytest
from feeder_agents.requirements import RequirementLedger,check_ledger_plan
from feeder_agents.hierarchy import HierarchicalSpec,generate_hierarchy


def ledger(field,value,**kw):
    return RequirementLedger(network_kind='distribution',summary='\u7814\u7a76\u9988\u7ebf',requirements=[dict(id='r',segment_ids=[0],evidence='\u4e09\u76f8\u4e0d\u5e73\u8861',meaning='\u6a21\u578b\u8981\u6c42',priority='hard',disposition='supported',target_field=field,expected_value=value,**kw)])


def brief(**values):
    return dict(status='ready',plan_type='hierarchical',plan={'spec':HierarchicalSpec(**values).model_dump()})


def test_hierarchy_builtin_phase_capability_is_not_a_missing_field():
    check_ledger_plan(ledger('model_family','hierarchical'),brief())
    check_ledger_plan(ledger('phase_design.mode','unbalanced'),brief())
    with pytest.raises(ValueError):check_ledger_plan(ledger('phase_design.mode','balanced'),brief())


def test_installation_requirement_compares_effective_choice():
    check_ledger_plan(ledger('hierarchy.lv_installation','aerial_bundle'),brief(lv_installation='auto',lv_equipment_profile='nexans_abc'))
    with pytest.raises(ValueError):check_ledger_plan(ledger('hierarchy.lv_installation','buried_direct'),brief(lv_installation='aerial_bundle'))


def test_final_model_cannot_pass_using_original_spec_after_load_changed():
    from feeder_agents.requirement_contract import model_observations,audit_ledger
    spec=HierarchicalSpec(users=12,total_kw=48,pv_ratio=0)
    feeder=generate_hierarchy(spec,41)
    r=ledger('hierarchy.total_kw',48)
    assert audit_ledger(r,model_observations('distribution',feeder))['all_satisfied']
    feeder.loads[0].kw+=10
    assert not audit_ledger(r,model_observations('distribution',feeder))['all_satisfied']


def test_final_phase_observation_and_missing_evidence_fail_closed():
    from feeder_agents.requirement_contract import model_observations,audit_ledger
    feeder=generate_hierarchy(HierarchicalSpec(users=12),41)
    assert audit_ledger(ledger('phase_design.mode','unbalanced'),model_observations('distribution',feeder))['all_satisfied']
    assert not audit_ledger(ledger('phase_design.mode','unbalanced'),{})['all_satisfied']


def test_paper_and_live_comparators_handle_numeric_roundoff_identically():
    from feeder_agents.requirement_contract import compare
    assert compare(47.9999999999,48)
    assert not compare(47,48)
    assert not compare(None,48)
    assert compare(['buried_direct'],'buried_direct')
    assert not compare(['buried_direct','aerial_bundle'],'buried_direct')


def test_capability_mapping_does_not_erase_real_unsupported_requirement():
    from feeder_agents.requirements import validate_ledger
    r=ledger('phase_design.mode','unbalanced');r.model_family='hierarchical'
    r.requirements[0].disposition='unsupported';r.requirements[0].reason='\u6ca1\u6709phase_design\u5b57\u6bb5'
    with pytest.raises(ValueError,match='built-in'):validate_ledger('\u4e09\u76f8\u4e0d\u5e73\u8861',r)
    r.requirements[0].target_field='neutral_displacement';r.requirements[0].reason='\u4e0d\u652f\u6301\u4e2d\u6027\u7ebf\u4f4d\u79fb'
    validate_ledger('\u4e09\u76f8\u4e0d\u5e73\u8861',r)


def test_legacy_phase_assignment_compiles_to_builtin_capability(tmp_path):
    from feeder_agents.schemas import DesignIntent
    from feeder_agents.design import compile_intent
    intent=DesignIntent(summary='\u5c42\u7ea7\u9988\u7ebf',task='hierarchical',assignments=[dict(field='phase_design.mode',value='unbalanced',origin='user',evidence='\u4e09\u76f8\u4e0d\u5e73\u8861',reason='\u7528\u6237\u8981\u6c42')])
    result=compile_intent('\u4e09\u76f8\u4e0d\u5e73\u8861',intent,tmp_path)
    check_ledger_plan(ledger('phase_design.mode','unbalanced'),result)
    intent.assignments[0].value='balanced'
    with pytest.raises(ValueError):compile_intent('\u4e09\u76f8\u4e0d\u5e73\u8861',intent,tmp_path)



def _saved_actual_hierarchy(root,spec):
    import hashlib,json
    from feeder_agents.hierarchy import export_hierarchy,evaluate_hierarchy
    from feeder_agents.simulation import simulate
    root.mkdir(parents=True,exist_ok=True)
    seed=int.from_bytes(hashlib.sha256(f'{spec.seed}:0'.encode()).digest()[:4],'big')
    f=generate_hierarchy(spec,seed)
    folder=root/'sample_00000'
    measured=simulate(export_hierarchy(f,folder/'opendss'))
    checked=evaluate_hierarchy(f,spec,measured)
    assert checked['accepted']
    (folder/'feeder.json').write_text(f.model_dump_json())
    (folder/'simulation.json').write_text(json.dumps(measured))
    (folder/'validation.json').write_text(json.dumps(checked))
    (root/'manifest.json').write_text(json.dumps(dict(spec=spec.model_dump())))
    return f,dict(directory=str(root),attempted=1,accepted=1,failed_or_unaccepted=0,
        samples=[dict(sample_id=folder.name,seed=seed,accepted=True)],verified_report='\u7535\u6c14\u901a\u8fc7')


def test_delivery_audit_reads_final_artifact_not_planner_assertion(tmp_path):
    import json
    from feeder_agents.requirement_contract import audit_delivery
    f,out=_saved_actual_hierarchy(tmp_path,HierarchicalSpec(users=12,total_kw=48))
    folder=tmp_path/'sample_00000'
    plan=brief(users=12,total_kw=48)
    assert audit_delivery(ledger('hierarchy.total_kw',48),plan,out)['jointly_accepted']==1
    f.loads[0].kw+=10;(folder/'feeder.json').write_text(f.model_dump_json())
    assert audit_delivery(ledger('hierarchy.total_kw',48),plan,out)['jointly_accepted']==0


def test_natural_language_delivery_exposes_requirement_failure(tmp_path,monkeypatch):
    import json
    from feeder_agents.design import design_from_request
    f,output=_saved_actual_hierarchy(tmp_path/'generated',HierarchicalSpec(users=12,total_kw=48))
    plan=brief(users=12,total_kw=48)
    plan.update(intent={'summary':'\u7814\u7a76\u9988\u7ebf','assumptions':[]},parameter_evidence=[],interpreter_model='test',requirement_ledger=ledger('hierarchy.total_kw',60).model_dump())
    monkeypatch.setattr('feeder_agents.design.interpret_request',lambda *a,**kw:plan)
    monkeypatch.setattr('feeder_agents.hierarchy_workflow.run_hierarchy',lambda *a,**kw:dict(experiment_id='test',**output))
    result=design_from_request('\u79d1\u7814\u9988\u7ebf',tmp_path,'delivery')
    assert result['outcome']['accepted']==1
    assert result['outcome']['requirement_accepted']==0
    assert result['requirement_acceptance']['samples'][0]['failed']==1
    assert (tmp_path/'designs/delivery/requirement_acceptance.json').exists()


def test_voltage_bound_is_checked_against_solved_voltage_not_config():
    from feeder_agents.requirement_contract import audit_ledger
    r=ledger('transmission.voltage_min_pu',.95)
    assert audit_ledger(r,{'min_voltage_pu':.98})['all_satisfied']
    assert not audit_ledger(r,{'min_voltage_pu':.94,'voltage_min_pu':.95})['all_satisfied']
    r.requirements[0].target_field='transmission.voltage_max_pu';r.requirements[0].expected_value=1.05
    assert audit_ledger(r,{'max_voltage_pu':1.01})['all_satisfied']
    assert not audit_ledger(r,{'max_voltage_pu':1.06})['all_satisfied']
