"""Complete denominators and paired network-level aggregation."""
import pytest


def fixture_rows():
    return [dict(job_id=f'{network}_{arm}_{seed}',network_id=network,task_id=network+'_research_GE',
        layer='research',space='GE',arm=arm,random_seed=seed,budget=160,
        success=(network=='n1' and (arm=='full' or seed==1)),search_ac_calls=160,total_ac_calls=172,
        deficit=0. if network=='n1' else .1,outcome='accepted' if network=='n1' else 'search_exhausted')
        for network in ('n1','n2') for arm,seeds in [('full',[None]),('random',[1,2])] for seed in seeds]


def test_missing_trial_and_unknown_work_are_not_silently_dropped():
    from feeder_agents.benchmark.response_summary import validate_rows
    rows=fixture_rows();expected=[dict(job_id=r['job_id']) for r in rows]
    with pytest.raises(ValueError,match='missing'):validate_rows(expected,rows[:-1])
    broken=[dict(r) for r in rows];broken[0]['total_ac_calls']=None
    with pytest.raises(ValueError,match='accounting'):validate_rows(expected,broken)
    validate_rows(expected,rows)


def test_random_seeds_are_averaged_not_free_best_of_five():
    from feeder_agents.benchmark.response_summary import network_scores
    scores=network_scores(fixture_rows())
    group=next(r for r in scores if r['network_id']=='n1' and r['arm']=='random')
    assert group['success_rate']==.5
    assert group['trials']==2 and group['search_ac_calls_sum']==320


def test_repeated_variants_do_not_increase_independent_network_count():
    from feeder_agents.benchmark.response_summary import paired_effect
    rows=fixture_rows()
    for arm in ('full','random'):
        for seed in ([None] if arm=='full' else [1,2]):
            rows.append(dict(job_id=f'n1_extra_{arm}_{seed}',network_id='n1',task_id='n1_extra',layer='research',space='GE',
                arm=arm,random_seed=seed,budget=160,success=False,search_ac_calls=160,total_ac_calls=172,deficit=.1))
    result=paired_effect(rows,'full','random',space='GE',layer='research',budget=160)
    assert result['networks']==2
    assert result['mean_success_difference']==.125
    assert result['bootstrap_networks']==2


def test_auditable_errors_remain_in_success_denominator_with_unknown_deficit():
    from feeder_agents.benchmark.response_summary import network_scores,group_summary
    rows=fixture_rows();rows[0].update(success=False,outcome='execution_error',error='solver error')
    rows[0].pop('deficit')
    validate=[dict(job_id=r['job_id']) for r in rows]
    from feeder_agents.benchmark.response_summary import validate_rows
    validate_rows(validate,rows)
    scores=network_scores(rows)
    assert next(r for r in scores if r['network_id']=='n1' and r['arm']=='full')['success_rate']==0
    values=group_summary(rows,space='GE',layer='research',budget=160,arm='full')
    assert values['trials']==2 and values['accepted_trials']==0
    assert values['deficit_observations']==1 and values['deficit_unavailable']==1
    assert values['mean_deficit_observed']==.1


def test_error_audit_accepts_metered_failure_but_rejects_modified_record(tmp_path,monkeypatch):
    import importlib.util
    from pathlib import Path
    from feeder_agents.artifacts import atomic_json
    import feeder_agents.response_design as engine
    from feeder_agents.benchmark.response_trials import run_response_trial,source_inventory
    from feeder_agents.artifacts import digest
    from test_response_trials_m96 import task
    from test_response_permissions_m95 import record_work
    spec=importlib.util.spec_from_file_location('m96_summary',Path(__file__).parents[1]/'scripts/summarize_m96_response_mechanisms.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    def fail(*args,**kw):
        record_work(3,5);raise RuntimeError('controlled metered failure')
    monkeypatch.setattr(engine,'run_response_design',fail)
    row=run_response_trial(task(),'full',None,80,tmp_path)
    job=dict(job_id=row['job_id'],public_task=task(),arm='full',random_seed=None,budget=80)
    _,files=source_inventory()
    audit=module.audit_record(row,job,digest(files))
    assert all(v for k,v in audit.items() if k!='job_id')
    row['success']=True
    atomic_json(Path(row['directory']).parents[1]/'row.json',row)
    audit=module.audit_record(row,job,digest(files))
    assert not audit['row_checksum'] and not audit['error_is_failure']
