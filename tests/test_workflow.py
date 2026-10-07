import json

import pytest

from feeder_agents.schemas import ExperimentSpec
from feeder_agents.workflow import run_experiment
from feeder_agents.memory import MemoryStore


def test_batch_produces_models_and_resume_rejects_changed_spec(tmp_path):
    spec = ExperimentSpec(count=2, n_loads_min=5, n_loads_max=5,
                          total_kw_min=300, total_kw_max=300)
    result = run_experiment(spec, tmp_path, 'run1', workers=1)
    assert result['attempted'] == 2
    assert result['accepted'] == 2
    master = tmp_path / 'experiments/run1/sample_00000/opendss/Master.dss'
    assert master.exists()
    assert (master.parent.parent / 'visualization.html').exists()
    before = master.stat().st_mtime_ns
    second = run_experiment(spec, tmp_path, 'run1', workers=1)
    assert second['accepted'] == 2
    assert master.stat().st_mtime_ns == before
    with pytest.raises(ValueError, match='configuration'):
        run_experiment(spec.model_copy(update={'seed': 999}), tmp_path, 'run1')


def test_process_workers_have_independent_models(tmp_path):
    spec = ExperimentSpec(count=3, n_loads_min=4, n_loads_max=8)
    result = run_experiment(spec, tmp_path, 'parallel', workers=2)
    assert result['attempted'] == 3
    assert len({s['model_hash'] for s in result['samples']}) == 3
    assert all(s['solver_converged'] for s in result['samples'])


def test_stress_and_unrepairable_results_are_not_falsely_certified(tmp_path):
    common = dict(n_loads_min=4, n_loads_max=4, total_kw_min=8000,
                  total_kw_max=8000, segment_km_min=2, segment_km_max=2,
                  max_repairs=0)
    normal = run_experiment(ExperimentSpec(**common), tmp_path, 'normal')
    assert normal['accepted'] == 0
    stress = run_experiment(ExperimentSpec(**common, mode='stress'), tmp_path, 'stress')
    for sample in stress['samples']:
        assert sample['operational_pass'] is False
        report = json.loads((tmp_path / sample['relative_path'] / 'validation.json').read_text())
        assert any(c['status'] == 'fail' for c in report['checks'])


def test_memory_persists_and_isolates_projects(tmp_path):
    db = tmp_path / 'memory.sqlite'
    MemoryStore(db).put('project_a', 'preferred_profile', {'voltage': 10})
    assert MemoryStore(db).get('project_a', 'preferred_profile') == {'voltage': 10}
    assert MemoryStore(db).get('project_b', 'preferred_profile') is None


@pytest.mark.parametrize('run_id', ['../escape', '/tmp/run', 'x/y', ''])
def test_experiment_ids_cannot_escape_workspace(tmp_path, run_id):
    with pytest.raises(ValueError):
        run_experiment(ExperimentSpec(), tmp_path, run_id)


@pytest.mark.parametrize('corruption', ['malformed', 'false_verdict', 'wrong_seed'])
def test_resume_rebuilds_untrustworthy_completion_record(tmp_path, corruption):
    spec = ExperimentSpec(n_loads_min=3, n_loads_max=3,
                          total_kw_min=100, total_kw_max=100)
    first = run_experiment(spec, tmp_path, 'resume')
    record = tmp_path / 'experiments/resume/sample_00000/outcome.json'
    value = json.loads(record.read_text())
    if corruption == 'malformed':
        record.write_text('{')
    else:
        value.update({'accepted': False, 'valid': False} if corruption == 'false_verdict' else {'seed': -1})
        record.write_text(json.dumps(value))
    resumed = run_experiment(spec, tmp_path, 'resume')
    assert resumed['accepted'] == first['accepted'] == 1
    assert resumed['samples'][0]['seed'] == first['samples'][0]['seed']
    assert resumed['samples'][0]['valid'] is True
