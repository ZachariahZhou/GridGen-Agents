"""Persisted diagnostic experience must remain scoped, verified and advisory."""
import copy
import json
from pathlib import Path

import pytest

from feeder_agents.adaptive_planning import adaptive_plan
from feeder_agents.artifacts import file_digest
from planning_fixtures import DiagnosticModel as Model, hierarchy_proposal as proposal


def make_run(root, memory, users=12, failed=False):
    wrong=proposal(users+3);fixed=proposal(users)
    for value in (wrong,fixed):
        item=value['ledger']['requirements'][0]
        item.update(evidence=f'{users}\u4e2a\u7528\u6237',expected_value=users)
        value['distribution_spec']['total_kw']=24
    if failed:fixed['distribution_spec']['total_kw']=48
    result=adaptive_plan(f'{users}\u4e2a\u7528\u6237',root,Model([wrong,fixed]),memory=memory)
    return result,json.loads((root/'failure_analysis/attempt_01.json').read_text())


def store(path,mode='learn'):
    from feeder_agents.planning_memory import PlanningMemory
    return PlanningMemory(path,mode=mode)


def test_success_persists_and_is_retrieved_for_different_count(tmp_path):
    memory=store(tmp_path/'memory.sqlite')
    first,bundle=make_run(tmp_path/'first',memory)
    assert first['status']=='ready'
    assert memory.stats()['planning_verified']==1
    memory=store(tmp_path/'memory.sqlite')  # new instance, persistent across requests
    _,second=make_run(tmp_path/'second',memory,users=18)
    hints=second['experience_memory']
    assert hints and hints[0]['action_id']=='align_spec'
    assert hints[0]['planning_successes']==1
    assert hints[0]['examples'][0]['verification_level']=='planning_verified'
    assert first['planning_memory']['recorded_episodes']==1


def test_failed_adjustment_is_counterexample_not_success(tmp_path):
    memory=store(tmp_path/'memory.sqlite')
    result,bundle=make_run(tmp_path/'bad',memory,failed=True)
    assert result['status']=='planning_failed'
    hints=memory.retrieve(bundle)
    assert hints[0]['planning_successes']==0
    assert hints[0]['failed_adjustments']==1
    assert 'unrelated' in hints[0]['examples'][0]['verification_error']


def test_idempotence_invalidation_and_source_tampering(tmp_path):
    memory=store(tmp_path/'memory.sqlite')
    _,bundle=make_run(tmp_path/'run',memory)
    memory.ingest_run(tmp_path/'run')
    hints=memory.retrieve(bundle)
    assert hints[0]['observations']==1
    key=hints[0]['examples'][0]['episode_id']
    memory.invalidate(key,'\u4eba\u5de5\u590d\u6838\u53d1\u73b0\u4e0d\u9002\u7528')
    assert not memory.retrieve(bundle)
    other=store(tmp_path/'other.sqlite')
    other.ingest_run(tmp_path/'run')
    (tmp_path/'run/failure_analysis/attempt_01.json').write_text('{}')
    assert not other.retrieve(bundle)


def test_family_scene_voltage_version_and_allowed_action_filter(tmp_path,monkeypatch):
    memory=store(tmp_path/'memory.sqlite')
    _,bundle=make_run(tmp_path/'run',memory)
    for field,value in [('scene','rural'),('voltage_kv',20)]:
        query=copy.deepcopy(bundle);query['candidate']['distribution_spec'][field]=value
        assert not memory.retrieve(query)
    query=copy.deepcopy(bundle);query['candidate']['family']='transmission'
    assert not memory.retrieve(query)
    query=copy.deepcopy(bundle);query['allowed_actions']=[dict(id='stop')]
    assert not memory.retrieve(query)
    monkeypatch.setattr('feeder_agents.planning_memory.compatibility_signature',lambda:'changed-runtime')
    assert not memory.retrieve(bundle)


def test_read_only_snapshot_does_not_learn_from_evaluation(tmp_path):
    memory=store(tmp_path/'memory.sqlite')
    _,bundle=make_run(tmp_path/'train',memory)
    snapshot=tmp_path/'snapshot.sqlite'
    memory.snapshot(snapshot)
    before=file_digest(snapshot)
    frozen=store(snapshot,mode='read_only')
    result,_=make_run(tmp_path/'evaluation',frozen,users=18)
    assert result['status']=='ready' and file_digest(snapshot)==before
    assert frozen.stats()['episodes']==1
    assert result['planning_memory']['mode']=='read_only'
    with pytest.raises(ValueError):frozen.invalidate('x','no writes')


def test_snapshot_keeps_its_verified_evidence_after_source_cleanup(tmp_path):
    memory=store(tmp_path/'memory.sqlite')
    _,bundle=make_run(tmp_path/'train',memory)
    snapshot=tmp_path/'snapshot.sqlite';memory.snapshot(snapshot)
    (tmp_path/'train/failure_analysis/attempt_01.json').unlink()
    frozen=store(snapshot,mode='read_only')
    assert frozen.retrieve(bundle)[0]['planning_successes']==1


def test_identical_repair_renews_stale_proof_without_double_counting(tmp_path):
    memory=store(tmp_path/'memory.sqlite')
    make_run(tmp_path/'run',memory)
    _,bundle=make_run(tmp_path/'run',memory)
    assert memory.retrieve(bundle)[0]['observations']==1
    assert memory.stats()['episodes']==1


def test_identical_repair_new_directory_can_gain_delivery_evidence(tmp_path):
    from feeder_agents.delivery import execute_delivery
    memory=store(tmp_path/'memory.sqlite')
    make_run(tmp_path/'first',memory)
    brief,bundle=make_run(tmp_path/'second',memory)
    result=execute_delivery(brief,tmp_path/'delivery','model','12\u4e2a\u7528\u6237',None,feedback=False,recovery=False,memory=False)
    assert memory.verify_delivery(tmp_path/'second',brief,result['output'])==1
    hints=memory.retrieve(bundle)
    assert hints[0]['delivery_successes']==hints[0]['observations']==1


def test_validator_code_changes_invalidate_memory_signature(monkeypatch):
    import feeder_agents.planning_memory as module
    before=module.compatibility_signature()
    real=module.file_digest
    monkeypatch.setattr(module,'file_digest',lambda path:'changed-validator' if path.name=='hierarchy.py' else real(path))
    assert module.compatibility_signature()!=before


def test_other_generation_manifest_cannot_promote_planning_experience(tmp_path):
    from feeder_agents.delivery import execute_delivery
    memory=store(tmp_path/'memory.sqlite')
    wrong=proposal(15);fixed=proposal()
    for p in (wrong,fixed):p['distribution_spec'].update(count=2,total_kw=24)
    brief=adaptive_plan('12\u4e2a\u7528\u6237',tmp_path/'planner',Model([wrong,fixed]),memory=memory)
    other=copy.deepcopy(brief)
    other['plan']['spec'].update(count=1,scene='rural',seed=43)
    result=execute_delivery(other,tmp_path/'delivery','different','12\u4e2a\u7528\u6237',None,feedback=False,recovery=False,memory=False)
    with pytest.raises(ValueError,match='manifest'):
        memory.verify_delivery(tmp_path/'planner',brief,result['output'])


def test_memory_is_advisory_and_reaches_diagnostic_model(tmp_path):
    memory=store(tmp_path/'memory.sqlite')
    _,bundle=make_run(tmp_path/'train',memory)
    class ObservingModel(Model):
        def with_structured_output(self,schema,**kwargs):
            bound=super().with_structured_output(schema,**kwargs)
            class Bound:
                def invoke(self,messages):
                    if schema.__name__=='FailureDiagnosis':
                        context=json.loads(messages[-1][1])
                        assert context['experience_memory'][0]['action_id']=='align_spec'
                        assert context['facts'][0]['expected']==12
                    return bound.invoke(messages)
            return Bound()
    # Retrieved advice cannot legalize a corrupt correction.
    wrong=proposal(15);wrong['distribution_spec']['total_kw']=24
    changed=proposal();changed['distribution_spec']['total_kw']=99
    result=adaptive_plan('12\u4e2a\u7528\u6237',tmp_path/'query',ObservingModel([wrong,changed]),memory=memory)
    assert result['status']=='planning_failed'


def test_memory_errors_do_not_break_planning(tmp_path):
    class Broken:
        mode='learn'
        path=tmp_path/'broken.sqlite'
        def retrieve(self,*args):raise OSError('memory unavailable')
        def ingest_run(self,*args):raise OSError('memory unavailable')
    result,_=make_run(tmp_path/'run',Broken())
    assert result['status']=='ready'
    assert result['planning_memory']['errors']


def test_no_memory_by_default_in_low_level_planner(tmp_path):
    assert adaptive_plan('12\u4e2a\u7528\u6237',tmp_path,Model([proposal()]))['status']=='ready'
    assert not list(tmp_path.rglob('*.sqlite'))


def test_delivery_promotes_only_matching_verified_model(tmp_path):
    from feeder_agents.delivery import execute_delivery
    memory=store(tmp_path/'memory.sqlite')
    brief,bundle=make_run(tmp_path/'planner',memory)
    result=execute_delivery(brief,tmp_path/'delivery','model','12\u4e2a\u7528\u6237',None,
                            feedback=False,recovery=False,memory=False)
    assert not memory.retrieve(bundle)[0]['delivery_successes']
    promoted=memory.verify_delivery(tmp_path/'planner',brief,result['output'])
    assert promoted==1
    assert memory.retrieve(bundle)[0]['delivery_successes']==1
    assert memory.stats()['delivery_verified']==1
    changed=copy.deepcopy(brief);changed['plan']['spec']['total_kw']=25
    with pytest.raises(ValueError):memory.verify_delivery(tmp_path/'planner',changed,result['output'])


def test_schema_invalid_partial_ledger_cannot_train_downgraded_requirement(tmp_path):
    memory=store(tmp_path/'memory.sqlite')
    _,bundle=make_run(tmp_path/'run',memory)
    # An imported record cannot just assert that a bad adjustment passed.
    path=tmp_path/'run/failure_analysis/attempt_01.json'
    payload=json.loads(path.read_text())
    payload['adjustment']['candidate']['distribution_spec']['total_kw']=99
    path.write_text(json.dumps(payload))
    other=store(tmp_path/'other.sqlite')
    report=other.ingest_run(tmp_path/'run')
    assert report['rejected'] and other.stats()['planning_verified']==0


def test_public_designs_share_project_memory_and_can_disable_it(tmp_path):
    from feeder_agents.design import design_from_request
    first=design_from_request('12\u4e2a\u7528\u6237',tmp_path,'first',execute=False,
        model=Model([proposal(15),proposal()]),planning_mode='adaptive')
    assert first['planning_memory']['recorded_episodes']==1
    second=design_from_request('12\u4e2a\u7528\u6237',tmp_path,'second',execute=False,
        model=Model([proposal(15),proposal()]),planning_mode='adaptive')
    episode=json.loads((tmp_path/'designs/second/failure_analysis/attempt_01.json').read_text())
    assert episode['experience_memory'][0]['observations']==1
    disabled=tmp_path/'disabled'
    design_from_request('12\u4e2a\u7528\u6237',disabled,'off',execute=False,model=Model([proposal()]),
                        planning_mode='adaptive',planning_memory_mode='off')
    assert not (disabled/'planning_experiences.sqlite').exists()


def test_delivery_entrypoint_promotes_its_planning_memory(tmp_path):
    from feeder_agents.delivery import execute_delivery
    memory=store(tmp_path/'memory.sqlite')
    brief,bundle=make_run(tmp_path/'planner',memory)
    result=execute_delivery(brief,tmp_path/'delivery','model','12\u4e2a\u7528\u6237',None,feedback=False,recovery=False)
    assert result['planning_memory']['promoted_episodes']==1
    assert memory.retrieve(bundle)[0]['delivery_successes']==1


def test_diagnostic_cannot_cite_a_nonretrieved_memory(tmp_path):
    from feeder_agents.planning_diagnostics import diagnose_failure
    memory=store(tmp_path/'memory.sqlite')
    _,bundle=make_run(tmp_path/'train',memory)
    bundle['experience_memory']=memory.retrieve(bundle)
    key=bundle['experience_memory'][0]['examples'][0]['episode_id']
    supported=Model([],diagnosis=dict(action_id='align_spec',evidence_ids=['F1'],reason='\u4f9d\u636e\u5f53\u524d\u4e8b\u5b9e',
                                     expected_change='\u6539\u7528\u6237\u6570',memory_episode_ids=[key]))
    assert diagnose_failure(bundle,supported,tmp_path/'valid_diagnosis')['memory_episode_ids']==[key]
    model=Model([],diagnosis=dict(action_id='align_spec',evidence_ids=['F1'],reason='\u4f9d\u636e\u5f53\u524d\u4e8b\u5b9e',
                                 expected_change='\u6539\u7528\u6237\u6570',memory_episode_ids=['invented-episode']))
    with pytest.raises(ValueError,match='memory'):diagnose_failure(bundle,model,tmp_path/'diagnosis')


def test_second_diagnosis_receives_short_term_failed_adjustment(tmp_path):
    # Two distinct deterministic errors: first repair representation, then count.
    wrong=proposal(15);wrong['distribution_spec']['deliverables']=['opendss']
    result=adaptive_plan('12\u4e2a\u7528\u6237',tmp_path,Model([wrong,proposal(15),proposal()]))
    assert result['status']=='ready'
    second=json.loads((tmp_path/'failure_analysis/attempt_02.json').read_text())
    trajectory=second['trajectory_memory']
    assert len(trajectory)==1
    assert trajectory[0]['action_id']=='repair_representation'
    assert trajectory[0]['verification']['status']=='failed'
    assert 'distribution_spec.deliverables' in [c['path'] for c in trajectory[0]['changes']]
