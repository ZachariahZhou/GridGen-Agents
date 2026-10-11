"""Regression for coupled response sizing under an unchanged AC budget."""
import copy

import pytest


@pytest.mark.parametrize('aggregation', ['mean_abs', 'max_abs'])
def test_conditional_compensation_fits_only_affine_aggregation_without_ac_calls(aggregation):
    from types import SimpleNamespace
    from feeder_agents.response_compensation import conditional_compensation_actions
    from feeder_agents.response_distribution import rank_distribution_actions
    from feeder_agents.solver_accounting import SolverMeter
    model = dict(kind='distribution', feeder=SimpleNamespace(source_bus='s', lines=[
        SimpleNamespace(id=name, bus1=u, bus2=v, length_km=1., conductor='research_small')
        for name, u, v in [('trunk', 's', 'j'), ('left', 'j', 'a'), ('right', 'j', 'b')]]))
    probes = [dict(id=p, kind='voltage_q', injection_buses=[p], monitor_buses=[p], aggregation=aggregation)
              for p in ('a', 'b')]
    assessment = dict(proxy_gains=dict(a=.4, b=.8), targets=[
        dict(target=dict(probe_id='a', lower=1.15, upper=1.35), observed=1., deficit=.15/1.35, passed=False),
        dict(target=dict(probe_id='b', lower=.99, upper=1.01), observed=1., deficit=0., passed=True)])
    first = dict(kind='scale_layout', factor=1.5, original_scale=1.5, probe_id='a')
    counter = dict(kind='scale_subtree', child_bus='b', factor=.9, probe_id='b')
    before = copy.deepcopy((assessment, first, counter))
    with SolverMeter() as meter:
        actions = conditional_compensation_actions(model, probes, assessment, [first], [counter])
        ranked = rank_distribution_actions(model, probes, assessment, actions)
    assert meter.snapshot()['total_ac_calls'] == 0
    assert (assessment, first, counter) == before
    if aggregation == 'max_abs':
        assert actions == []  # A maximum can switch its active monitored port.
        return
    assert ranked[0]['proxy_predicted_deficits'] == pytest.approx([0., 0.])
    assert ranked[0]['proxy_response_estimates'] == pytest.approx(dict(a=1.2, b=1.))
    assert ranked[0]['actions'][1]['factor'] == pytest.approx(1/3)


def selective_plan(seed=1311):
    return dict(
        research_question='Raise one Q response while preserving another response.',
        base_spec=dict(network_kind='distribution', n_buses=25, voltage_kv=10,
            total_kw_min=360, total_kw_max=360, seed=seed,
            scenario=dict(kind='urban', engineering_profile='urban', layout='spatial_mst'),
            phase_design=dict(mode='unbalanced', single_phase_laterals=0, two_phase_laterals=0),
            repair_policy=dict(strategy='none')),
        probes=[dict(id=port, kind='voltage_q', step_mw=.01, injection_buses=[bus])
                for port, bus in [('a', 'b1'), ('b', 'b9')]],
        targets=[dict(probe_id='a', reference='baseline_ratio', lower=1.15, upper=1.35),
                 dict(probe_id='b', reference='baseline_ratio', lower=.99, upper=1.01)],
        search=dict(allowed_actions=['replace_conductor', 'scale_layout', 'scale_subtree'],
            max_layout_scale_change=.75, max_branch_length_change=.75,
            max_node_displacement_km=.75, max_joint_actions=2,
            max_rounds=6, candidates_per_round=8, strategy='archive',
            max_search_ac_calls=160, archive_width=4, exploration_deficit_allowance=.15))


@pytest.mark.parametrize('compensation_mode',['conditional','bounded'])
def test_selective_q_repair_fits_original_budget(tmp_path,compensation_mode):
    from feeder_agents.response_design import run_response_design
    plan = selective_plan()
    plan['search']['compensation_mode']=compensation_mode
    original = copy.deepcopy(plan)
    result = run_response_design(plan, tmp_path, 'selective_q_1311')
    assert plan == original
    assert result['target_met'], [r['observed'] for r in result['selected']['targets']]
    assert result['verification_passed']
    assert result['protected_contract_preserved']
    assert result['solver_accounting']['by_phase']['search'] <= 160
    for row in result['verification']['targets']:
        assert row['target']['lower'] <= row['observed'] <= row['target']['upper']
