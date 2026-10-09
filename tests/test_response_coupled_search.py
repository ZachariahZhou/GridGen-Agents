"""Coupled targets require selective proposals within the original preview budget."""
import pytest

from feeder_agents.response_design import run_response_design


@pytest.mark.parametrize('seed,ports', [(1207, ('b4', 'b9')), (1217, ('b10', 'b20')), (1223, ('b24', 'b3'))])
def test_opposed_ports_fit_original_bounded_search(seed, ports, tmp_path):
    plan = dict(
        research_question='Increase one port response while decreasing the other.',
        base_spec=dict(
            network_kind='distribution', n_buses=25, voltage_kv=10,
            total_kw_min=360, total_kw_max=360, seed=seed,
            scenario=dict(kind='urban', engineering_profile='urban', layout='spatial_mst'),
            phase_design=dict(mode='unbalanced', single_phase_laterals=0, two_phase_laterals=0),
            repair_policy=dict(strategy='none')),
        probes=[dict(id='a', kind='voltage_p', step_mw=.01, injection_buses=[ports[0]]),
                dict(id='b', kind='voltage_p', step_mw=.01, injection_buses=[ports[1]])],
        targets=[dict(probe_id='a', reference='baseline_ratio', lower=1.15, upper=1.35),
                 dict(probe_id='b', reference='baseline_ratio', lower=.65, upper=.85)],
        search=dict(allowed_actions=['replace_conductor', 'scale_layout', 'scale_subtree'],
                    max_layout_scale_change=.75, max_rounds=4, candidates_per_round=16,
                    max_branch_length_change=.75, max_node_displacement_km=.75,
                    max_joint_actions=2))
    result = run_response_design(plan, tmp_path, f'opposed_{seed}')
    assert result['target_met'], [row['observed'] for row in result['selected']['targets']]
    assert result['verification_passed']
    assert result['protected_contract_preserved']
    assert result['evaluated_candidates'] <= 65
    assert result['selected']['targets'][0]['target']['lower'] == 1.15
    assert result['selected']['targets'][1]['target']['upper'] == .85


def radial_proxy_case(kind='voltage_p', injection='b', monitor='b'):
    from types import SimpleNamespace
    model = dict(kind='distribution', feeder=SimpleNamespace(
        source_bus='source', lines=[
            SimpleNamespace(id='trunk', bus1='source', bus2='junction', length_km=1., conductor='research_small'),
            SimpleNamespace(id='left', bus1='junction', bus2='a', length_km=1., conductor='research_small'),
            SimpleNamespace(id='right', bus1='junction', bus2='b', length_km=1., conductor='research_small')]))
    probe = dict(id='p', kind=kind, injection_buses=[injection], monitor_buses=[monitor], aggregation='mean_abs')
    row = dict(target=dict(probe_id='p', lower=.4, upper=.8), observed=1., deficit=.25, passed=False)
    return model, [probe], dict(targets=[row])


def test_path_ranking_protects_the_other_target_without_mutating_assessment():
    import copy
    from feeder_agents.response_distribution import rank_distribution_actions
    model, probes, assessment = radial_proxy_case()
    probes[0]['id'] = 'b'
    probes.append(dict(probes[0], id='a', injection_buses=['a'], monitor_buses=['a']))
    assessment['targets'] = [
        dict(target=dict(probe_id='a', lower=1.15, upper=1.35), observed=1.2, deficit=0., passed=True),
        dict(target=dict(probe_id='b', lower=.65, upper=.85), observed=.9, deficit=.05/.85, passed=False)]
    shared = dict(kind='replace_conductor', line_id='trunk', conductor='research_medium')
    selective = dict(kind='replace_conductor', line_id='right', conductor='research_medium')
    original = copy.deepcopy(assessment)
    ranked = rank_distribution_actions(model, probes, assessment, [shared, selective])
    assert ranked[0]['line_id'] == 'right'
    assert ranked[0]['proxy_response_estimates'] == pytest.approx(dict(a=1.2, b=.675))
    assert ranked[0]['proxy_predicted_deficits'] == [0., 0.]
    assert assessment == original
    assert selective == dict(kind='replace_conductor', line_id='right', conductor='research_medium')


def test_transfer_voltage_proxy_uses_only_the_shared_source_path():
    from feeder_agents.response_distribution import rank_distribution_actions
    model, probes, assessment = radial_proxy_case(injection='a', monitor='b')
    private = dict(kind='scale_subtree', child_bus='a', factor=.5)
    shared = dict(kind='replace_conductor', line_id='trunk', conductor='research_medium')
    ranked = rank_distribution_actions(model, probes, assessment, [private, shared])
    assert ranked[0]['line_id'] == 'trunk'
    assert ranked[0]['proxy_response_estimates']['p'] == pytest.approx(.5)
    assert ranked[1]['proxy_response_estimates']['p'] == pytest.approx(1.)


def test_reactive_voltage_proxy_uses_reactance_and_full_compound_scaling():
    from feeder_agents.response_distribution import rank_distribution_actions
    model, probes, assessment = radial_proxy_case(kind='voltage_q', injection='a', monitor='b')
    action = dict(kind='compound', actions=[
        dict(kind='replace_conductor', line_id='trunk', conductor='research_medium'),
        dict(kind='scale_layout', factor=.5, original_scale=.5)])
    ranked = rank_distribution_actions(model, probes, assessment, [action])
    assert ranked[0]['proxy_response_estimates']['p'] == pytest.approx(.4571428571428572)


def test_unmodelled_action_family_keeps_a_preview_slot():
    from feeder_agents.response_distribution import rank_distribution_actions
    model, probes, assessment = radial_proxy_case()
    scalable = [dict(kind='scale_layout', factor=.8 + index*.005, original_scale=.8 + index*.005)
                for index in range(20)]
    transfer = dict(kind='redistribute_load', donor='load_a', receiver='load_b', kw=1.)
    ranked = rank_distribution_actions(model, probes, assessment, scalable + [transfer])
    assert any(action['kind'] == 'redistribute_load' for action in ranked[:3])
    assert len(ranked) == 21
