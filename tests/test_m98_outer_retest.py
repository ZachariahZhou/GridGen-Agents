import json
from pathlib import Path

import pytest


def adapter():
    from feeder_agents.benchmark import m98_outer_retest
    return m98_outer_retest


def test_selection_preserves_public_messages_and_pair_keys():
    suite = {'cases': [
        {'id': 'p', 'messages': ['Exact original.'], 'expected_outcome': 'generation'},
        {'id': 'n', 'messages': ['Negative.'], 'expected_outcome': 'unsupported'},
    ], 'repetitions': [0, 1, 2]}
    selected = adapter().select_jobs(suite, scope='positives', case_ids=['p'], repetitions=[2])
    assert selected == [('p', 'full_outer', 2)]
    assert suite['cases'][0]['messages'] == ['Exact original.']
    with pytest.raises(ValueError, match='Unknown case'):
        adapter().select_jobs(suite, case_ids=['missing'])
    with pytest.raises(ValueError, match='repetition'):
        adapter().select_jobs(suite, repetitions=[3])


def test_timeout_retains_partial_artifact_and_raw_events(tmp_path):
    module = adapter()
    job = tmp_path / 'job'
    job.mkdir()
    (job / 'events.jsonl').write_text('{"kind":"model_start"}\n')
    (job / 'selected.json').write_text('{"saved":true}')
    result = module.record_worker_exit(job, returncode=-9, elapsed=180.1)
    assert result['status'] == 'timeout'
    assert json.loads((job / 'selected.json').read_text())['saved']
    assert (job / 'events.jsonl').read_text() == '{"kind":"model_start"}\n'
    assert module.record_worker_exit(job, returncode=0, elapsed=1) == result


def test_public_validation_rejects_answer_key_injection():
    cases = [{'id': 'p', 'messages': ['Original.'], 'expected_outcome': 'generation'}]
    module = adapter()
    module.validate_public(cases, [{'id': 'p', 'messages': ['Original.']}])
    with pytest.raises(ValueError, match='public'):
        module.validate_public(cases, [{'id': 'p', 'messages': ['Original.'], 'witness': {}}])
    with pytest.raises(ValueError, match='public'):
        module.validate_public(cases, [{'id': 'p', 'messages': ['Changed.']}])


def test_paired_summary_keeps_grading_errors_in_denominator():
    rows = [
        {'case_id': 'p', 'arm': 'full_outer', 'repetition': 0, 'primary_positive': True,
         'status': 'completed', 'semantic_artifact_fulfillment': True,
         'semantic_fulfilled_and_completed': True},
        {'case_id': 'q', 'arm': 'full_outer', 'repetition': 0, 'primary_positive': True,
         'status': 'worker_crash', 'evaluation_error': 'saved corrupt artifact',
         'semantic_artifact_fulfillment': None, 'semantic_fulfilled_and_completed': False},
    ]
    prior = [{**r, 'semantic_artifact_fulfillment': False,
              'semantic_fulfilled_and_completed': False} for r in rows]
    result = adapter().paired_summary(rows, prior)
    assert result['positive_n'] == 2
    assert result['evaluation_errors'] == 1
    assert result['artifact_fulfilled'] == 1
    assert result['matched_positive_n'] == 2
    assert result['artifact_improvements'] == 1
    assert result['artifact_unknown_pairs'] == 1


def test_trace_causes_separate_provider_and_conversation_timeouts():
    row = {'status': 'timeout', 'diagnosis_flags': [], 'semantic_artifact_fulfillment': True}
    events = [{'kind': 'model_start', 'run_id': 'x', 'tags': ['outer_model'], 'seconds': 160}]
    causes = adapter().trace_causes(row, events, {'turns': []}, {}, {})
    assert 'conversation_timeout' in causes
    assert 'pending_outer_model_at_timeout' in causes
    assert 'provider_timeout' not in causes
    events.append({'kind': 'model_error', 'run_id': 'x', 'error_type': 'OpenAITimeoutError', 'error': 'Request timed out.'})
    assert 'provider_timeout' in adapter().trace_causes(row, events, {'turns': []}, {}, {})


def test_freeze_preserves_bytes_and_detects_source_tampering(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from feeder_agents.artifacts import digest
    module = adapter()
    paired = tmp_path / 'prior'
    paired.mkdir()
    (paired / 'analysis').mkdir()
    suite = {'cases': [{'id': 'p', 'messages': ['Same prompt.'], 'expected_outcome': 'generation'}],
             'repetitions': [0, 1, 2], 'model_settings': module.SETTINGS,
             'conversation_timeout_seconds': 180, 'workers': 8, 'model': 'test-model',
             'endpoint_hash': digest('https://provider.example/v1')}
    (paired / 'suite.json').write_text(json.dumps(suite))
    (paired / 'suite_checksum.json').write_text(json.dumps({'sha256': digest(suite)}))
    (paired / 'public_cases.json').write_text('[ {"id": "p", "messages": ["Same prompt."]} ]\n')
    for filename in ['metrics.csv', 'semantic_equivalence.json', 'certificate_audit.json']:
        (paired / 'analysis' / filename).write_text('{}')
    source = tmp_path / 'checkout'
    package = source / 'src/feeder_agents'
    package.mkdir(parents=True)
    (package / 'agent.py').write_text('# source under test\n')
    (package / '.env').write_text('DO_NOT_COPY=secret')
    for filename in module.EVALUATORS:
        path = package / filename
        path.parent.mkdir(exist_ok=True)
        path.write_text('# evaluator\n')
    monkeypatch.setattr(module, 'configured_model', lambda: SimpleNamespace(
        model_name='test-model', openai_api_base='https://provider.example/v1'))
    destination = tmp_path / 'new'
    module.freeze(destination, paired, source)
    assert (destination / 'public_cases.json').read_bytes() == (paired / 'public_cases.json').read_bytes()
    assert not list(destination.rglob('.env'))
    assert not (destination / 'private_witnesses').exists()
    module.verify(destination)
    (destination / 'source/feeder_agents/agent.py').write_text('# changed\n')
    with pytest.raises(ValueError, match='Frozen source changed'):
        module.verify(destination)
    with pytest.raises(ValueError, match='new folder'):
        module.freeze(paired, paired, source)


def test_launcher_continues_after_each_evaluation_error(tmp_path, monkeypatch):
    from feeder_agents.benchmark import m97_language_runtime as runtime
    module = adapter()
    cases = [{'id': name, 'messages': ['Original.'], 'category': 'ordinary',
              'expected_outcome': 'generation', 'witness': {'certified': True}}
             for name in ['a', 'b']]
    suite = {'cases': cases, 'workers': 8, 'repetitions': [0], 'paired_root': str(tmp_path / 'prior'),
             'evaluator_hashes': {}}
    (tmp_path / 'suite.json').write_text(json.dumps(suite))
    (tmp_path / 'analysis').mkdir()
    (tmp_path / 'prior/analysis').mkdir(parents=True)
    (tmp_path / 'prior/analysis/metrics.csv').write_text('case_id,arm,repetition\n')
    for case in cases:
        folder = tmp_path / 'jobs' / f"{case['id']}__full_outer__r0"
        folder.mkdir(parents=True)
        (folder / 'worker_result.json').write_text('{"status":"completed"}')
        (folder / 'events.jsonl').write_text('{"kind":"model_start"}\n')
    monkeypatch.setattr(module, 'verify', lambda *args, **kwargs: suite)
    def invalid_grade(*args):
        raise ValueError('malformed saved artifact')
    monkeypatch.setattr(runtime, 'collect_job', invalid_grade)
    summary = module.launch(tmp_path, workers=2)
    assert summary['observed'] == 2
    assert summary['positive_n'] == 2
    assert summary['evaluation_errors'] == 2
    assert summary['model_calls'] == 2
    assert len(list((tmp_path / 'jobs').glob('*/job_outcome.json'))) == 2


def test_semantic_adapter_reproduces_all_original_research_grades(tmp_path):
    original = Path(__file__).resolve().parents[1] / 'experiments/m97_outer_agent_v2'
    if not (original / 'analysis/semantic_equivalence.json').exists():
        pytest.skip('Immutable local M97 evidence is not installed')
    (tmp_path / 'jobs').symlink_to(original / 'jobs', target_is_directory=True)
    (tmp_path / 'analysis').mkdir()
    (tmp_path / 'analysis/authorization').symlink_to(original / 'analysis/authorization', target_is_directory=True)
    suite = json.loads((original / 'suite.json').read_text())
    expected = json.loads((original / 'analysis/semantic_equivalence.json').read_text())['rows']
    actual = adapter().semantic_rows(tmp_path, suite)
    def decisions(rows):
        return {adapter().key(row): (row['semantic_artifact_fulfillment'], row['semantic_fulfilled_and_completed']) for row in rows}
    assert len(actual) == len(expected) == 96
    assert decisions(actual) == decisions(expected)


def test_report_keeps_valid_semantic_result_when_other_job_has_corrupt_grade(tmp_path, monkeypatch):
    original = Path(__file__).resolve().parents[1] / 'experiments/m97_outer_agent_v2'
    if not (original / 'analysis/semantic_equivalence.json').exists():
        pytest.skip('Immutable local M97 evidence is not installed')
    module = adapter()
    suite = json.loads((original / 'suite.json').read_text())
    suite.update(paired_root=str(original), evaluator_hashes={})
    (tmp_path / 'suite.json').write_text(json.dumps(suite))
    (tmp_path / 'analysis').mkdir()
    (tmp_path / 'jobs').mkdir()
    good = 'units_counts_04__full_outer__r0'
    (tmp_path / 'jobs' / good).symlink_to(original / 'jobs' / good, target_is_directory=True)
    bad = tmp_path / 'jobs/ordinary_04__full_outer__r0'
    bad.mkdir()
    (bad / 'independent_grade.json').write_text('{invalid grade')
    (bad / 'worker_result.json').write_text('{"status":"worker_crash"}')
    (bad / 'job_outcome.json').write_text(json.dumps(dict(case_id='ordinary_04', arm='full_outer', repetition=0, status='worker_crash')))
    monkeypatch.setattr(module, 'verify', lambda *args, **kwargs: suite)
    summary = module.report(tmp_path)
    assert summary['observed'] == summary['positive_n'] == 2
    assert summary['artifact_fulfilled'] == 1
    assert summary['evaluation_errors'] == 1


def test_uniform_report_reproduces_all_288_original_decisions(tmp_path, monkeypatch):
    original = Path(__file__).resolve().parents[1] / 'experiments/m97_outer_agent_v2'
    if not (original / 'analysis/metrics.csv').exists():
        pytest.skip('Immutable local M97 evidence is not installed')
    module = adapter()
    suite = json.loads((original / 'suite.json').read_text())
    suite.update(paired_root=str(original), evaluator_hashes={})
    (tmp_path / 'jobs').symlink_to(original / 'jobs', target_is_directory=True)
    (tmp_path / 'analysis').mkdir()
    monkeypatch.setattr(module, 'verify', lambda *args, **kwargs: suite)
    summary = module.report(tmp_path)
    actual = {module.key(row): row for row in module.csv_rows(tmp_path / 'analysis/metrics.csv')}
    original_rows = module.csv_rows(original / 'analysis/metrics.csv')
    assert summary['observed'] == len(actual) == len(original_rows) == 288
    assert summary['evaluation_errors'] == 0
    for previous in original_rows:
        current = actual[module.key(previous)]
        for field in ['semantic_artifact_fulfillment', 'semantic_fulfilled_and_completed',
                      'authorization_committed_any', 'postrun_negative_decision_reason_proxy']:
            assert current[field] == previous[field], (module.key(previous), field)
