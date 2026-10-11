"""Independent oracle and same-tool comparator regressions; no live model calls."""
import copy
import json
from types import SimpleNamespace

import pytest


def adapter():
    from feeder_agents.benchmark import m101_language_evidence
    return m101_language_evidence


@pytest.mark.parametrize('arm', ['one_proposal', 'full_outer'])
def test_recorded_worker_keeps_original_request_protection_in_both_arms(tmp_path, monkeypatch, arm):
    from feeder_agents import agent as production
    from feeder_agents.benchmark.m97_language_runtime import worker
    from feeder_agents.agent_boundary import OriginalRequestMiddleware

    class OfflineModel:
        model_name = 'offline-test'
        def model_copy(self, **kwargs):
            return self

    captured = []
    def create(**kwargs):
        captured.extend(kwargs['middleware'])
        return SimpleNamespace(invoke=lambda *args, **kw: {'messages': []})

    monkeypatch.setattr(production, 'configured_model', lambda **kwargs: OfflineModel())
    monkeypatch.setattr(production, 'create_agent', create)
    (tmp_path / 'public_cases.json').write_text(json.dumps([{'id': 'case', 'messages': ['Inspect capabilities.']}]))
    result = worker(tmp_path, 'case', arm, 0)
    assert result['status'] == 'completed'
    assert sum(isinstance(item, OriginalRequestMiddleware) for item in captured) == 1
    assert [type(item).__name__ for item in captured].count('ModelCallLimitMiddleware') == 1
    assert [type(item).__name__ for item in captured].count('ToolCallLimitMiddleware') == 1


def sample_revision_case():
    return dict(id='pv_feedback_alpha', category='revision', expected_outcome='generation',
        messages=['Generate a feeder.', 'Set PV to 30%.', 'Set PV to 45%.'],
        witness_spec={'network_kind': 'distribution', 'seed': 101001},
        requirements={'n_buses': 7, 'pv_ratio': .45},
        turn_requirements=[{'n_buses': 7, 'pv_ratio': .15},
                           {'n_buses': 7, 'pv_ratio': .30}, {'n_buses': 7, 'pv_ratio': .45}],
        revision_actions=[[{'kind': 'pv_ratio', 'value': .30}], [{'kind': 'pv_ratio', 'value': .45}]],
        preserve_groups=['topology', 'geometry', 'equipment', 'loads', 'phase_connectivity', 'phase_loads', 'voltage'])


def test_revision_oracle_requires_every_turn_without_case_suffix_conventions():
    module = adapter()
    case = sample_revision_case()
    assert module.validate_cases([case])[0]['id'] == 'pv_feedback_alpha'
    del case['turn_requirements'][1]
    with pytest.raises(ValueError, match='turn_requirements'):
        module.validate_cases([case])


def test_hierarchy_facts_come_from_actual_model_and_wrong_counts_fail():
    from feeder_agents.hierarchy import HierarchicalSpec, generate_hierarchy
    module = adapter()
    spec = HierarchicalSpec(mv_buses=7, transformer_count=3, lv_branches=2,
        users=18, total_kw=36, pv_ratio=.25, seed=101002)
    model = generate_hierarchy(spec, spec.seed)
    actual = module.observations(model, spec.model_dump(), 'hierarchy')
    expected = {'n_buses': 34, 'users': 18, 'transformer_count': 3,
                'voltage_kv': 10, 'lv_voltage_kv': .4, 'total_kw': 36, 'pv_ratio': .25,
                'phase_mode': 'unbalanced', 'phase_customer_counts': [9, 5, 4], 'lv_branches': 2}
    assert all(module.check_requirements(expected, actual).values())
    assert not module.check_requirements({'n_buses': 35}, actual)['n_buses']
    damaged = model.model_copy(deep=True)
    damaged.transformers.pop()
    facts = module.observations(damaged, spec.model_dump(), 'hierarchy')
    assert not module.check_requirements(expected, facts)['transformer_count']


def test_actual_transmission_radius_is_an_upper_bound():
    import numpy as np
    module = adapter()
    case = {'bus': np.zeros((2, 13)), 'gen': np.zeros((1, 21))}
    case['bus'][:, 0] = [1, 2]
    case['bus'][:, 9] = 220
    facts = module.observations(case, {'seed': 1}, 'transmission',
        {'positions_km': {'1': [0, 0], '2': [12, 16]}})
    assert facts['radius_km'] == 20
    assert module.check_requirements({'radius_km': 21}, facts)['radius_km']
    assert not module.check_requirements({'radius_km': 19}, facts)['radius_km']


def test_protected_revision_checks_actual_fields_and_explicit_actions():
    from feeder_agents.schemas import ExperimentSpec
    from feeder_agents.generation import generate_feeder
    from feeder_agents.revisions import apply_revision, RevisionPlan
    module = adapter()
    spec = ExperimentSpec(n_buses=7, total_kw_min=12, total_kw_max=12, pv_ratio=.15,
        phase_design={'mode': 'unbalanced'}, repair_policy={'strategy': 'none'})
    original = generate_feeder(spec, 101003)
    case = sample_revision_case()
    case['witness_spec'] = spec.model_dump()
    plan = RevisionPlan(feedback=case['messages'][1], actions=[{
        'kind': 'pv_ratio', 'value': .30, 'evidence': case['messages'][1]}])
    revised, target, _ = apply_revision(original, spec, plan)
    checks = module.revision_checks(case, original, revised, spec.model_dump(), 1)
    assert all(checks.values())
    damaged = revised.model_copy(deep=True)
    damaged.buses[1].x_km += .01
    assert not module.revision_checks(case, original, damaged, spec.model_dump(), 1)['preserved_geometry']
    damaged = revised.model_copy(deep=True)
    damaged.loads[0].phase_powers[0].kw += .01
    assert not module.revision_checks(case, original, damaged, spec.model_dump(), 1)['preserved_phase_loads']


def test_requested_phase_scaling_is_checked_independently_of_revision_implementation(monkeypatch):
    from feeder_agents.schemas import ExperimentSpec
    from feeder_agents.generation import generate_feeder
    from feeder_agents import revisions
    module = adapter()
    spec = ExperimentSpec(n_buses=7, total_kw_min=12, total_kw_max=12, pv_ratio=.15,
        phase_design={'mode': 'unbalanced'}, repair_policy={'strategy': 'none'})
    original = generate_feeder(spec, 101004)
    after = original.model_copy(deep=True)
    for load in after.loads:
        for field in ('kw', 'kvar', 'pv_kw'):
            setattr(load, field, getattr(load, field) * 1.1)
        for phase in load.phase_powers:
            for field in ('kw', 'kvar', 'pv_kw'):
                setattr(phase, field, getattr(phase, field) * 1.1)
    after.total_kw *= 1.1
    case = sample_revision_case()
    case['revision_actions'][0] = [{'kind': 'load_scale', 'value': 1.1}, {'kind': 'pv_ratio', 'value': .15}]
    case['preserve_groups'] = ['topology', 'equipment', 'geometry', 'phase_connectivity', 'voltage']
    monkeypatch.setattr(revisions, 'apply_revision', lambda *a, **k: pytest.fail('Evaluator reused implementation under test'))
    assert all(module.revision_checks(case, original, after, spec.model_dump(), 1).values())
    after.loads[0].phase_powers[0].pv_kw += .01
    after.loads[0].phase_powers[1].pv_kw -= .01
    assert not module.revision_checks(case, original, after, spec.model_dump(), 1)['per_phase_pv_scale']


def test_final_model_cannot_replace_missing_or_wrong_intermediate_revision():
    module = adapter()
    case = sample_revision_case()
    artifacts = [dict(turn=i, accepted=True, usable=True, checks={'physical': True},
        facts={'n_buses': 7, 'pv_ratio': pv}, path=f'/models/{i}')
        for i, pv in enumerate([.15, .30, .45])]
    revisions = [dict(turn=i, checks={'parent_verified': True, 'consecutive_parent': True}, passed=True) for i in (1, 2)]
    assert module.grade_turns(case, artifacts, revisions)['artifact_fulfillment']
    assert not module.grade_turns(case, [artifacts[0], artifacts[2]], revisions)['artifact_fulfillment']
    artifacts[1]['facts']['pv_ratio'] = .20
    assert not module.grade_turns(case, artifacts, revisions)['artifact_fulfillment']


def test_selection_pairs_both_arms_and_validates_requested_repetitions():
    module = adapter()
    suite = {'cases': [{'id': 'a', 'expected_outcome': 'generation'},
                       {'id': 'b', 'expected_outcome': 'unsupported'}], 'repetitions': [0, 1]}
    jobs = module.select_jobs(suite)
    assert len(jobs) == 8
    assert set(arm for _, arm, _ in jobs) == {'one_proposal', 'full_outer'}
    with pytest.raises(ValueError, match='repetition'):
        module.select_jobs(suite, repetitions=[2])


def test_same_source_pair_summary_retains_missing_arm_and_failed_certificates():
    module = adapter()
    rows = [dict(case_id='a', arm='one_proposal', repetition=0, expected_outcome='generation',
                 artifact_fulfillment=False, fulfilled_and_completed=False, known_feasible=True),
            dict(case_id='a', arm='full_outer', repetition=0, expected_outcome='generation',
                 artifact_fulfillment=True, fulfilled_and_completed=True, known_feasible=True),
            dict(case_id='b', arm='full_outer', repetition=0, expected_outcome='generation',
                 artifact_fulfillment=False, fulfilled_and_completed=False, known_feasible=False)]
    summary = module.summarize(rows)
    assert summary['arms']['full_outer']['positive_n'] == 2
    assert summary['arms']['full_outer']['certified_positive_n'] == 1
    assert summary['paired']['complete_pairs'] == 1
    assert summary['paired']['missing_arm_pairs'] == 1
    assert summary['paired']['full_only_success'] == 1


@pytest.mark.parametrize('domain', ['distribution', 'hierarchy'])
def test_saved_outputs_are_reloaded_and_independently_graded(tmp_path, domain):
    from feeder_agents.artifacts import atomic_json
    module = adapter()
    project = tmp_path / 'project'
    if domain == 'distribution':
        from feeder_agents.schemas import ExperimentSpec
        from feeder_agents.workflow import run_experiment
        spec = ExperimentSpec(n_buses=7, total_kw_min=12, total_kw_max=12, pv_ratio=.25,
            phase_design={'mode': 'unbalanced'}, seed=101111, repair_policy={'strategy': 'none'})
        result = run_experiment(spec, project, 'saved_model')
        expected = {'n_buses': 7, 'total_kw': 12, 'pv_ratio': .25, 'phase_mode': 'unbalanced'}
    else:
        from feeder_agents.hierarchy import HierarchicalSpec
        from feeder_agents.hierarchy_workflow import run_hierarchy
        spec = HierarchicalSpec(mv_buses=7, transformer_count=3, lv_branches=2,
            users=18, total_kw=36, pv_ratio=.25, seed=101112)
        result = run_hierarchy(spec, project, 'saved_model')
        expected = {'n_buses': 34, 'users': 18, 'total_kw': 36, 'pv_ratio': .25,
                    'transformer_count': 3, 'lv_branches': 2, 'phase_customer_counts': [9, 5, 4]}
    assert result['accepted'] == 1
    case = dict(id='physical', domain=domain, requirements=expected, messages=['Generate the authored model.'])
    events = [dict(kind='tool_start', run_id='call', turn=0, name='design_from_language', inputs={'design_id': 'saved_model'}),
              dict(kind='tool_end', run_id='call', turn=0, output={'outcome': {'directory': result['directory']}})]
    records = module.collect_artifacts(project, case, events, tmp_path / 'independent')
    assert len(records) == 1
    assert records[0]['accepted'], records
    assert records[0]['usable']
    assert module.grade_turns(case, records, [])['artifact_fulfillment']
    case['requirements']['n_buses'] += 1
    assert not module.grade_turns(case, records, [])['artifact_fulfillment']


def test_freeze_isolates_public_inputs_and_rejects_oracle_mutation(tmp_path, monkeypatch):
    module = adapter()
    monkeypatch.setattr(module, 'configured_model', lambda: SimpleNamespace(model_name='offline', openai_api_base='offline'))
    cases = [{'id': 'conflict', 'category': 'control', 'messages': ['Require both 31 and 37 buses.'],
              'expected_outcome': 'clarification', 'requirements': {}, 'witness_spec': None}]
    path = tmp_path / 'authored.json'
    path.write_text(json.dumps(cases))
    root = tmp_path / 'frozen'
    result = module.freeze(root, path, source_root=__import__('pathlib').Path(__file__).resolve().parents[1])
    assert result['jobs'] == 4
    assert json.loads((root / 'public_cases.json').read_text()) == [{'id': 'conflict', 'messages': cases[0]['messages']}]
    assert not list((root / 'source').rglob('.env'))
    assert module.launch(root, dry_run=True)['live_api_calls'] == 0
    oracles = json.loads((root / 'oracles.json').read_text())
    oracles[0]['requirements'] = {'n_buses': 31}
    (root / 'oracles.json').write_text(json.dumps(oracles))
    with pytest.raises(ValueError, match='oracles'):
        module.verify(root)


def test_transmission_certificate_preserves_numeric_arrays_and_no_edit_search(tmp_path):
    from feeder_agents.transmission import TransmissionSpec
    module = adapter()
    spec = TransmissionSpec(n_buses=57, n_generators=6, total_mw=80, seed=101200,
                            allowed_repairs=[], radius_km=10)
    case = dict(id='transmission_numeric', category='transmission', domain='transmission',
        messages=['Generate the authored transmission case.'], expected_outcome='generation',
        witness_spec=spec.model_dump(), requirements={'n_buses': 57, 'n_generators': 6, 'total_mw': 80, 'radius_km': 10})
    certificate = module.certify(case, tmp_path)
    assert 'error' not in certificate, certificate
    assert certificate['steps'][0]['checks']['export_matches_json']
    assert isinstance(json.loads((tmp_path / case['id'] / 'turn_00/model.json').read_text())['bus'], list)
    case.update(id='transmission_no_edit', task='transmission_transfer', response_interval=[.98, 1.02],
                max_layout_scale_change=0, forbidden_actions=['scale_layout'])
    certificate = module.certify(case, tmp_path)
    assert 'error' not in certificate, certificate
    assert certificate['compiled_plan']['response_plan']['search']['allowed_actions'] == []


def test_launcher_keeps_native_grading_on_main_thread(tmp_path, monkeypatch):
    import threading
    from feeder_agents.artifacts import atomic_json
    module = adapter()
    monkeypatch.setattr(module, 'configured_model', lambda: SimpleNamespace(model_name='offline', openai_api_base='offline'))
    cases = [{'id': 'conflict', 'category': 'control', 'messages': ['Require both 31 and 37 buses.'],
              'expected_outcome': 'clarification', 'requirements': {}, 'witness_spec': None}]
    path = tmp_path / 'authored.json'
    path.write_text(json.dumps(cases))
    root = tmp_path / 'frozen'
    module.freeze(root, path, source_root=__import__('pathlib').Path(__file__).resolve().parents[1])
    suite = module.verify(root)
    for case_id, arm, repetition in module.select_jobs(suite):
        atomic_json(root / 'jobs' / f'{case_id}__{arm}__r{repetition}' / 'worker_result.json',
                    {'status': 'completed', 'turns': []})
    original = module.collect_job
    observed = []
    def checked_collect(*args, **kwargs):
        observed.append(threading.current_thread() is threading.main_thread())
        return original(*args, **kwargs)
    monkeypatch.setattr(module, 'collect_job', checked_collect)
    result = module.launch(root, workers=2)
    assert observed == [True] * 4
    assert sum(counts['evaluation_errors'] for counts in result['arms'].values()) == 0


@pytest.mark.parametrize('filename,check', [('Loads.dss', 'export_load_locations_phases'),
                                          ('Generation.dss', 'export_pv_locations_phases')])
def test_export_identity_rejects_moved_load_or_pv_with_unchanged_power(tmp_path, filename, check):
    from feeder_agents.schemas import ExperimentSpec
    from feeder_agents.generation import generate_feeder
    from feeder_agents.simulation import export_dss
    module = adapter()
    spec = ExperimentSpec(n_buses=7, total_kw_min=12, total_kw_max=12, pv_ratio=.25,
                          repair_policy={'strategy': 'none'})
    model = generate_feeder(spec, 101333)
    master = export_dss(model, tmp_path / 'opendss')
    assert all(module.export_identity_checks(master, model).values())
    path = master.parent / filename
    text = path.read_text()
    damaged = text.replace(f'bus1={model.loads[0].bus}.', f'bus1={model.loads[1].bus}.', 1)
    assert damaged != text
    path.write_text(damaged)
    assert not module.export_identity_checks(master, model).get(check, True)


@pytest.fixture
def research_artifact(tmp_path):
    from feeder_agents.schemas import ExperimentSpec
    from feeder_agents.research_tasks import run_research_task
    spec = ExperimentSpec(n_buses=7, total_kw_min=12, total_kw_max=12, seed=101444,
                          repair_policy={'strategy': 'none'})
    project = tmp_path / 'project'
    result = run_research_task(dict(task='voltage_control', research_question='Finite Q test',
        base_spec=spec.model_dump(), response_reference='baseline_ratio', response_lower=.98,
        response_upper=1.02, support_mvar=.01, search={'allowed_actions': []}), project, 'research')
    case = dict(id='research', category='research', cluster_id='research', domain='distribution',
        messages=['Measure the unchanged feeder with a 10 kvar intervention.'], expected_outcome='generation',
        task='voltage_control', response_interval=[.98, 1.02], support_mvar=.01,
        requirements={'n_buses': 7, 'task': 'voltage_control', 'response_lower': .98, 'response_upper': 1.02})
    events = [dict(kind='tool_start', run_id='call', turn=0, name='design_from_language', inputs={'design_id': 'research'}),
              dict(kind='tool_end', run_id='call', turn=0, output=result)]
    return project, case, events, result


def test_research_collection_audits_nested_response_and_actual_contract(research_artifact, tmp_path):
    project, case, events, result = research_artifact
    records = adapter().collect_artifacts(project, case, events, tmp_path / 'audit')
    assert len(records) == 1
    record = records[0]
    assert 'error' not in record, record
    assert record['research_evidence']['passed'], record['research_evidence']
    assert record['accepted']
    assert record['facts']['response_reference'] == 'baseline_ratio'
    assert record['facts']['response_lower'] == .98
    assert adapter().grade_turns(case, records, [])['artifact_fulfillment']
    case['response_interval'] = [2, 2.1]
    records = adapter().collect_artifacts(project, case, events, tmp_path / 'wrong_contract')
    assert not records[0]['accepted']
    assert records[0]['facts']['response_lower'] == .98


def test_research_evaluator_exception_fails_closed(research_artifact, tmp_path, monkeypatch):
    from feeder_agents.benchmark import m97_language_semantic
    project, case, events, result = research_artifact
    def broken(*args, **kwargs):
        raise KeyError('intentional_auditor_failure')
    monkeypatch.setattr(m97_language_semantic, 'audit_artifact', broken)
    record = adapter().collect_artifacts(project, case, events, tmp_path / 'audit')[0]
    assert not record['accepted']
    assert record['physical_accepted']
    assert 'intentional_auditor_failure' in record['evaluation_error']


def test_ordinary_physical_artifact_cannot_satisfy_research_without_evidence(tmp_path):
    from feeder_agents.schemas import ExperimentSpec
    from feeder_agents.workflow import run_experiment
    project = tmp_path / 'project'
    result = run_experiment(ExperimentSpec(n_buses=7, total_kw_min=12, total_kw_max=12,
        seed=101445, repair_policy={'strategy': 'none'}), project, 'ordinary')
    case = dict(id='research', domain='distribution', messages=['Run the finite Q research task'],
        requirements={}, task='voltage_control', response_interval=[1.1, 1.3])
    records = adapter().collect_artifacts(project, case, [], tmp_path / 'audit')
    assert len(records) == 1
    assert not records[0]['accepted']
    assert records[0]['research_evidence']['checks']['saved_response_evidence'] is False
    assert 'evaluation_error' not in records[0]


def test_truthful_unmet_uses_task_verdict_and_rejects_audit_exceptions():
    module = adapter()
    case = dict(expected_outcome='truthful_unmet', requirements={}, task='voltage_control')
    events = [dict(kind='tool_end', output={'status': 'task_not_satisfied'})]
    raw = {'turns': []}
    artifact = dict(accepted=False, physical_accepted=True, usable=True, facts={},
                    task_status='task_not_satisfied')
    assert module.negative_grade(case, events, raw, [artifact])['correct_negative_proxy']
    artifact['evaluation_error'] = 'incomplete audit'
    assert not module.negative_grade(case, events, raw, [artifact])['correct_negative_proxy']


def test_research_declared_permission_scope_cannot_widen_even_without_applied_edits():
    module = adapter()
    case = {'max_layout_scale_change': .3, 'forbidden_actions': ['replace_conductor']}
    plan = {'search': {'allowed_actions': ['scale_layout'], 'max_layout_scale_change': .75}}
    assert not all(module.declared_research_scope_checks(case, plan).values())
    plan['search']['max_layout_scale_change'] = .3
    assert all(module.declared_research_scope_checks(case, plan).values())
    plan['search']['allowed_actions'].append('replace_conductor')
    assert not module.declared_research_scope_checks(case, plan)['forbidden_actions_excluded']


def test_research_absolute_target_cannot_be_graded_as_baseline_ratio(research_artifact, tmp_path):
    from feeder_agents.research_tasks import run_research_task
    project, case, events, result = research_artifact
    payload = dict(result['compiled_plan']['task_plan'], response_reference='absolute')
    result = run_research_task(payload, project, 'absolute')
    records = adapter().collect_artifacts(project, case, [], tmp_path / 'audit')
    actual = next(record for record in records if record['run_id'] == 'absolute')
    assert 'evaluation_error' not in actual
    assert actual['facts']['response_reference'] == 'absolute'
    assert not actual['accepted']
    assert not any(probe['checks']['original_target_bounds'] for probe in actual['research_evidence']['probes'])
