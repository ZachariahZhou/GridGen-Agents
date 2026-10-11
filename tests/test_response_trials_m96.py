"""Actual-work accounting, explicit outcomes and immutable trial inputs."""
import copy
import pytest
from test_response_permissions_m95 import plan_payload,record_work


def task():
    return dict(network_id='development_13',task_id='dev_request',layer='research',space='G',plan=plan_payload())


def fake_result(success=True):
    return dict(target_met=success,selected=dict(target_met=True,base_accepted=True,valid=True,
        deficits=[0.],targets=[dict(observed=1.2)]),protected_contract_preserved=True,
        stop_reason='targets_met' if success else 'independent_verification_failed',result_hash='fake',
        solver_accounting=dict(total_ac_calls=52,by_phase={'search':40}),
        diagnosis=dict(design_space=dict(actual_changes={'changed_conductors':0})))


@pytest.mark.parametrize('outcome',['partial','verification_failed','cached'])
def test_trial_counts_actual_work_and_distinguishes_final_failure(tmp_path,monkeypatch,outcome):
    import feeder_agents.response_design as engine
    import feeder_agents.benchmark.response_trials as trials
    def run(*args,**kwargs):
        if outcome=='partial':
            record_work(3,5);raise RuntimeError('failed after actual attempts')
        if outcome!='cached':record_work()
        return fake_result(outcome=='cached')
    monkeypatch.setattr(engine,'run_response_design',run)
    monkeypatch.setattr(trials,'extract_evidence',lambda result,folder:dict(baseline_hash='grid',probes_hash='ports'))
    row=trials.run_response_trial(task(),'full',None,80,tmp_path)
    assert row['search_ac_calls']==(3 if outcome=='partial' else 0 if outcome=='cached' else 40)
    assert row['total_ac_calls']==(5 if outcome=='partial' else 0 if outcome=='cached' else 52)
    assert row['success'] is (outcome=='cached')
    if outcome=='partial':assert row['outcome']=='execution_error' and row['error']
    if outcome=='verification_failed':assert row['outcome']=='independent_verification_failed'
    if outcome=='cached':assert row['historical_solver_accounting']['total_ac_calls']==52


def test_trial_identity_reuse_is_rejected(tmp_path,monkeypatch):
    import feeder_agents.response_design as engine
    import feeder_agents.benchmark.response_trials as trials
    monkeypatch.setattr(engine,'run_response_design',lambda *args,**kw:fake_result())
    monkeypatch.setattr(trials,'extract_evidence',lambda *args:{})
    trials.run_response_trial(task(),'full',None,80,tmp_path)
    with pytest.raises(FileExistsError):trials.run_response_trial(task(),'full',None,80,tmp_path)


def test_common_plan_only_changes_declared_ablation_fields():
    from feeder_agents.benchmark.response_trials import trial_plan
    public=task();old=copy.deepcopy(public)
    full=trial_plan(public,'full',160)
    fixed=trial_plan(public,'fixed',160)
    single=trial_plan(public,'single',160)
    assert fixed['search']['compensation_mode']=='fixed'
    fixed['search']['compensation_mode']=full['search']['compensation_mode']
    assert fixed==full and public==old
    assert single['search']['max_joint_actions']==1
    single['search']['max_joint_actions']=2
    assert single==full
    for budget in (80,320):
        variant=trial_plan(public,'full',budget)
        assert variant['search']['max_rounds']==32
        variant['search']['max_search_ac_calls']=160
        assert variant==full


def test_suite_rejects_private_inputs_and_freezes_complete_jobs(tmp_path):
    from feeder_agents.benchmark.response_trials import freeze_response_suite
    bad=dict(task(),witness_actions=[{'secret':True}])
    with pytest.raises(ValueError):freeze_response_suite([bad],['full'],[160],[1],tmp_path/'bad')
    manifest=freeze_response_suite([task()],['full','fixed','single','random'],[80,160],[1,2,3,4,5],tmp_path/'good')
    assert len(manifest['jobs'])==16
    assert len({j['job_id'] for j in manifest['jobs']})==16
    assert all(j['public_task']['plan']['base_spec']['seed']==42 for j in manifest['jobs'])
    with pytest.raises(FileExistsError):freeze_response_suite([task()],['full'],[80],[1],tmp_path/'good')


def test_frozen_manifest_detects_mutation_and_runtime_drift(tmp_path,monkeypatch):
    import json
    import feeder_agents.benchmark.response_trials as trials
    from feeder_agents.artifacts import atomic_json
    root=tmp_path/'frozen'
    trials.freeze_response_suite([task()],['full'],[80],[1],root)
    trials.verify_frozen_suite(root)
    monkeypatch.setattr(trials,'runtime_versions',lambda:{'changed':'runtime'})
    with pytest.raises(ValueError,match='runtime'):trials.verify_frozen_suite(root)
    monkeypatch.undo()
    value=json.loads((root/'suite.json').read_text());value['jobs'][0]['budget']=160
    atomic_json(root/'suite.json',value)
    with pytest.raises(ValueError,match='checksum'):trials.verify_frozen_suite(root)


def test_reference_binding_rejects_changed_constructed_baseline(tmp_path,monkeypatch):
    import feeder_agents.response_design as engine
    import feeder_agents.benchmark.response_trials as trials
    monkeypatch.setattr(engine,'run_response_design',lambda *args,**kw:fake_result())
    monkeypatch.setattr(trials,'extract_evidence',lambda *args:dict(baseline_hash='different',baseline_probes_hash='same'))
    row=trials.run_response_trial(task(),'full',None,80,tmp_path,
        reference=dict(baseline_hash='certified',baseline_probes_hash='same'))
    assert not row['success'] and row['outcome']=='execution_error'
    assert row['reference_matches'] is False
