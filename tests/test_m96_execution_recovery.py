"""Infrastructure recovery must replay affected paired blocks, not successes only."""
import importlib.util
from pathlib import Path
import pytest


def module():
    path=Path(__file__).parents[1]/'scripts/resume_m96_response_mechanisms.py'
    spec=importlib.util.spec_from_file_location('m96_recovery',path)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value


def jobs():
    return [dict(job_id=f'{n}_{b}_{a}',public_task=dict(network_id=n),budget=b,arm=a)
        for n in ('n1','n2') for b in (160,320) for a in ('full','fixed','random')]


def test_recovery_replays_all_arms_in_affected_network_budget():
    data=jobs();complete=[dict(job_id=j['job_id'],success=False) for j in data if j['budget']==160]
    complete.append(dict(job_id='n1_320_full',success=True))
    started={r['job_id'] for r in complete}|{'n1_320_fixed'}
    plan=module().recovery_selection(data,complete,started)
    assert plan['interrupted_ids']==['n1_320_fixed']
    assert plan['replaced_completed_ids']==['n1_320_full']
    assert len(plan['retained_ids'])==6 and len(plan['replay_ids'])==6
    complete[-1]['success']=False
    assert module().recovery_selection(data,complete,started)==plan


def test_recovery_rejects_untracked_or_duplicated_records():
    helper=module().recovery_selection
    with pytest.raises(ValueError):helper(jobs(),[dict(job_id='foreign')],{'foreign'})
    with pytest.raises(ValueError):helper(jobs(),[dict(job_id='n1_160_full')]*2,{'n1_160_full'})
    with pytest.raises(ValueError):helper(jobs(),[dict(job_id='n1_160_full')],set())
