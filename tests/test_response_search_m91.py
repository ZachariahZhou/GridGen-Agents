"""Bounded exploration must preserve physics and report real solver work."""
import copy

import pytest


def test_nested_meters_count_attempts_once_and_restore_phase():
    from feeder_agents.solver_accounting import SolverMeter, record_ac_solve, solver_phase
    with SolverMeter() as outer:
        with solver_phase('compile'):
            record_ac_solve('pypower')
        with SolverMeter() as inner:
            with solver_phase('search'):
                record_ac_solve('opendss')
        with solver_phase('verification'):
            record_ac_solve('pypower')
    assert outer.snapshot()['total_ac_calls'] == 3
    assert inner.snapshot()['total_ac_calls'] == 1
    assert outer.snapshot()['by_phase'] == {'compile': 1, 'search': 1, 'verification': 1}
    assert outer.snapshot()['by_backend'] == {'pypower': 2, 'opendss': 1}


def test_search_limit_counts_failed_attempts_but_reserves_verification():
    from feeder_agents.solver_accounting import (
        ACSearchBudgetExceeded, SolverMeter, record_ac_solve, solver_phase)
    with SolverMeter(max_search_ac_calls=1) as meter:
        with solver_phase('search'):
            with pytest.raises(ValueError, match='solver failed'):
                record_ac_solve('opendss')
                raise ValueError('solver failed')
            with pytest.raises(ACSearchBudgetExceeded):
                record_ac_solve('opendss')
        with solver_phase('verification'):
            record_ac_solve('opendss')
    assert meter.snapshot()['total_ac_calls'] == 2
    assert meter.snapshot()['by_phase'] == {'search': 1, 'verification': 1}


def assessment(deficits, feasible=True):
    return dict(deficits=deficits, base_accepted=feasible, valid=True,
                target_met=feasible and not any(deficits))


def test_archive_allows_bounded_response_tradeoff_without_relaxing_physics():
    from feeder_agents.response_design import accept_candidate
    from feeder_agents.response_search import exploration_eligible
    before = assessment([0., .2])
    intermediate = assessment([.1, .15])
    assert not accept_candidate(before, intermediate, True)
    assert exploration_eligible(intermediate, before, preserved=True, allowance=.1)
    assert not exploration_eligible(intermediate, before, preserved=False, allowance=.1)
    assert not exploration_eligible(assessment([0., 0.], False), before,
                                    preserved=True, allowance=.1)
    assert not exploration_eligible(assessment([.3, .1]), before,
                                    preserved=True, allowance=.1)


def test_archive_retains_complementary_ports_and_does_not_mutate_records():
    from feeder_agents.response_search import retain_archive
    records = [dict(candidate='a', assessment=assessment([0., .3])),
               dict(candidate='b', assessment=assessment([.3, 0.])),
               dict(candidate='c', assessment=assessment([.1, .1])),
               dict(candidate='d', assessment=assessment([.2, .2]))]
    before = copy.deepcopy(records)
    kept = retain_archive(records, 3)
    assert {s['candidate'] for s in kept} == {'a', 'b', 'c'}
    assert records == before


def test_physical_identity_ignores_explanations_but_tracks_parameters():
    from feeder_agents.response_search import physical_fingerprint
    from feeder_agents.response_measurement import create_model
    from feeder_agents.schemas import ExperimentSpec
    original = create_model(ExperimentSpec(n_buses=9, seed=19))
    annotated = copy.deepcopy(original)
    annotated['feeder'].design_evidence['trace'] = ['first', 'second']
    assert physical_fingerprint(annotated) == physical_fingerprint(original)
    annotated['feeder'].lines[0].length_km *= 1.1
    assert physical_fingerprint(annotated) != physical_fingerprint(original)


def test_real_distribution_budget_keeps_partial_candidate_out_of_archive(tmp_path):
    from feeder_agents.response_design import run_response_design, read_response_result
    from feeder_agents.solver_accounting import SolverMeter
    plan = dict(research_question='Count actual AC calls and reject incomplete measurements.',
        base_spec=dict(network_kind='distribution', n_buses=9, seed=19),
        probes=[dict(id='q', kind='voltage_q')],
        targets=[dict(probe_id='q', reference='baseline_ratio', lower=1.15, upper=1.35)],
        search=dict(allowed_actions=['scale_layout'], max_layout_scale_change=.5,
                    strategy='archive', max_search_ac_calls=1, max_rounds=3))
    result = run_response_design(plan, tmp_path, 'budget')
    assert not result['target_met']
    assert result['winner'] == 'baseline'
    assert result['stop_reason'] == 'search_ac_budget_exhausted'
    calls = result['solver_accounting']
    assert calls['by_phase']['search'] == 1
    assert calls['by_phase']['baseline'] == 3
    assert calls['by_phase']['verification'] == 3
    assert calls['by_phase']['export'] == 2
    assert calls['total_ac_calls'] == 9
    assert result['accepted_steps'] == 0
    with SolverMeter() as meter:
        saved = read_response_result(result['directory'])
    assert saved['result_hash'] == result['result_hash']
    assert meter.snapshot()['total_ac_calls'] == 0


def test_transmission_solver_boundary_is_metered():
    from feeder_agents.solver_accounting import SolverMeter, solver_phase
    from feeder_agents.transmission import dependencies
    runpf, options, case9 = dependencies()
    with SolverMeter() as meter, solver_phase('verification'):
        _, converged = runpf(case9(), options(VERBOSE=0, OUT_ALL=0))
    assert converged
    assert meter.snapshot()['by_backend'] == {'pypower': 1}


@pytest.mark.parametrize('strategy,expected', [('monotone', False), ('archive', True)])
def test_runner_can_cross_a_tradeoff_without_returning_a_worse_incumbent(
        strategy, expected, tmp_path, monkeypatch):
    """A controlled response landscape tests loop decisions, not AC accuracy."""
    import feeder_agents.response_design as runner
    from feeder_agents.solver_accounting import record_ac_solve
    from feeder_agents.response_measurement import create_model
    from feeder_agents.schemas import ExperimentSpec
    spec = ExperimentSpec(n_buses=9, seed=19)
    model = create_model(spec)
    line_id = model['feeder'].lines[0].id
    original_length = model['feeder'].lines[0].length_km

    def proposals(current, *args, **kwargs):
        scale = current['feeder'].lines[0].length_km/original_length
        final = scale > 1.05
        return [dict(kind='scale_layout', action_id='second' if final else 'first',
                     factor=1.2/scale if final else 1.1, original_scale=1.2 if final else 1.1)]

    def measure(current, spec, probes, folder, step_factor=1):
        from feeder_agents.artifacts import atomic_json
        record_ac_solve('controlled_test_landscape')
        scale = current['feeder'].lines[0].length_km/original_length
        values = (1., 1.) if scale < 1.05 else ((1.1, 1.15) if scale < 1.15 else (1., 1.3))
        out = dict(base_accepted=True, valid=True, base_validation={'accepted': True},
            probes={key: dict(value=value, valid=True, signed_response={key: value})
                    for key, value in zip(('a', 'b'), values)})
        atomic_json(folder/'response.json', out)
        return out

    monkeypatch.setattr(runner, 'propose_response_actions', proposals)
    monkeypatch.setattr(runner, 'measure', measure)
    # Keep the real constructor, action application, contract check and export.
    # Provide its normal validation fields; export's separate AC solve is real.
    original_export = runner._export
    def export(candidate, spec, folder, assessment, accepted=None):
        data = copy.deepcopy(assessment)
        data['base_validation'].update(checks=[], failures=[], accepted=True)
        return original_export(candidate, spec, folder, data, accepted)
    monkeypatch.setattr(runner, '_export', export)
    plan = dict(research_question='Loop mechanism: compensate a temporary response tradeoff.',
        base_spec=spec.model_dump(), probes=[dict(id=p, kind='voltage_q') for p in ('a', 'b')],
        targets=[dict(probe_id='a', lower=.99, upper=1.01), dict(probe_id='b', lower=1.25, upper=1.35)],
        search=dict(allowed_actions=['scale_layout'], max_layout_scale_change=.5,
                    strategy=strategy, max_rounds=2, exploration_deficit_allowance=.15))
    result = runner.run_response_design(plan, tmp_path, strategy)
    assert result['target_met'] is expected
    assert result['protected_contract_preserved']
    assert result['selected']['deficits'][0] == 0
    assert result['accepted_steps'] == (2 if expected else 0)


def test_auto_strategy_and_solver_budget_are_schema_checked():
    from feeder_agents.response_schema import ResponseSearch
    assert ResponseSearch().strategy == 'auto'
    for kwargs in ({'max_search_ac_calls': 0}, {'archive_width': 0},
                   {'exploration_deficit_allowance': -1}):
        with pytest.raises(ValueError):
            ResponseSearch(**kwargs)


def test_monotone_search_ignores_solver_scale_noise_but_not_actual_tradeoffs():
    from feeder_agents.response_design import accept_candidate
    before = assessment([.111111111, .176470588])
    # Observed on independent rural P-response ports: the first deficit moves
    # by 2e-7 while the second target becomes satisfied. This is not evidence
    # of a meaningful coupled tradeoff.
    assert accept_candidate(before, assessment([.111111311, 0.]), True)
    assert not accept_candidate(before, assessment([.111211111, 0.]), True)
    assert not accept_candidate(before, assessment([.111111311, 0.], False), True)


def test_measured_action_gain_corrects_fixed_source_proxy_without_changing_targets():
    from feeder_agents.response_search import learn_proxy_gains
    before = dict(targets=[dict(target={'probe_id': 'a'}, observed=1.)])
    after = dict(targets=[dict(target={'probe_id': 'a'}, observed=1.04)])
    action = dict(proxy_raw_response_estimates={'a': 1.2})
    assert learn_proxy_gains(before, after, action) == pytest.approx({'a': .2})
    assert 'proxy_gains' not in before
    assert learn_proxy_gains(before, after, dict(proxy_raw_response_estimates={'a': 1.+1e-12})) == {}


def test_calibrated_layout_proposal_targets_response_change_not_total_response():
    from types import SimpleNamespace
    from feeder_agents.response_distribution import layout_options
    line = SimpleNamespace(length_km=1.)
    model = {'feeder': SimpleNamespace(lines=[line])}
    spec = SimpleNamespace(segment_km_min=.1, segment_km_max=3.)
    search = SimpleNamespace(allowed_actions=['scale_layout'], max_layout_scale_change=.9)
    measured = dict(proxy_gains={'a': .2}, targets=[dict(passed=False, observed=1.,
        target=dict(probe_id='a', lower=1.1, upper=1.2))])
    options = layout_options(model, model, spec, measured, search)
    assert options[0]['factor'] == pytest.approx(1.6)


def test_cached_research_workflow_does_not_recompile_or_solve(tmp_path):
    from feeder_agents.research_tasks import run_research_task
    from feeder_agents.solver_accounting import SolverMeter
    plan = dict(task='transmission_transfer', research_question='Cache the verified task.',
        base_spec=dict(network_kind='transmission', n_buses=17, n_generators=3,
                       voltage_kv=110, total_mw=120, seed=42),
        response_lower=.9, response_upper=1.1, search=dict(max_rounds=0))
    first = run_research_task(plan, tmp_path, 'cached')
    assert first['solver_accounting']['by_phase']['compile'] >= 1
    assert first['solver_accounting']['total_ac_calls'] > first['response_solver_accounting']['total_ac_calls']
    with SolverMeter() as meter:
        cached = run_research_task(plan, tmp_path, 'cached')
    assert cached['result_hash'] == first['result_hash']
    assert meter.snapshot()['total_ac_calls'] == 0
    with SolverMeter() as meter, pytest.raises(ValueError, match='changed'):
        run_research_task(dict(plan, transfer_mw=2.), tmp_path, 'cached')
    assert meter.snapshot()['total_ac_calls'] == 0


def test_commuting_geometry_actions_share_physical_identity():
    from feeder_agents.response_search import physical_fingerprint
    from feeder_agents.response_measurement import create_model
    from feeder_agents.response_design import apply_response_action
    from feeder_agents.schemas import ExperimentSpec
    spec = ExperimentSpec(n_buses=9, seed=19)
    original = create_model(spec)
    def scaled(factors):
        model, product = original, 1.
        for factor in factors:
            product *= factor
            model = apply_response_action(model, spec, dict(kind='scale_layout',
                factor=factor, original_scale=product))
        return model
    assert physical_fingerprint(scaled([1.1, 1.2])) == physical_fingerprint(scaled([1.2, 1.1]))


def test_language_research_cache_has_no_extra_ac_compilation(tmp_path):
    from feeder_agents.research_language import design_research_from_request
    from feeder_agents.solver_accounting import SolverMeter
    plan = dict(task='transmission_transfer', research_question='Measure transfer response.',
        base_spec=dict(network_kind='transmission', n_buses=17, n_generators=3,
                       voltage_kv=110, total_mw=120, seed=42),
        response_lower=.9, response_upper=1.1, search=dict(max_rounds=0))
    class Interpreter:
        def with_structured_output(self, *args, **kwargs): return self
        def invoke(self, messages): return dict(status='ready', plan=plan, issues=[])
    request='Generate a 17-bus transmission grid for power transfer research, response ratio 0.9 to 1.1, measurement only.'
    with SolverMeter() as fresh:
        first=design_research_from_request(request,tmp_path,'language_cache',model=Interpreter())
    assert 'outcome' in first, first
    import json
    stored=json.loads((tmp_path/'research_tasks/language_cache/result.json').read_text())
    assert fresh.snapshot()['total_ac_calls'] == stored['solver_accounting']['total_ac_calls']
    with SolverMeter() as cached:
        second=design_research_from_request(request,tmp_path,'language_cache',model=Interpreter())
    assert cached.snapshot()['total_ac_calls'] == 0
    assert second['outcome'] == first['outcome']
