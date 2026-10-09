"""A locally best corridor can obstruct a short, feasible transfer design."""
import json
from pathlib import Path

import numpy as np
import pytest


def test_two_step_proxy_accounts_for_the_competing_path_and_remaining_horizon():
    from feeder_agents.response_transmission import rank_parallel_actions

    # Three equal-reactance branches: 2/3 of a 2->3 transfer takes the direct
    # line. Doubling each of the other two branches reduces this to 1/2.
    bus = np.zeros((3, 13)); bus[:, 0] = [1, 2, 3]; bus[:, 1] = [3, 1, 1]
    branch = np.zeros((3, 13)); branch[:, :2] = [[1, 2], [1, 3], [2, 3]]
    branch[:, 3] = 1.; branch[:, 10] = 1
    model = dict(case=dict(bus=bus, branch=branch), metadata=dict(
        branch_evidence=[dict(circuits=1) for _ in range(3)]))
    probes = [dict(id='t', injection_buses=['2'], withdrawal_buses=['3'],
                   monitor_branches=[0, 1, 2], aggregation='max_abs')]
    assessment = dict(targets=[dict(target=dict(probe_id='t', lower=.4, upper=.8),
                                    observed=1., deficit=.25)])
    actions = [dict(kind='parallel_line', index=i, value=2, proxy_effect=0) for i in range(3)]
    one = rank_parallel_actions(model, probes, assessment, actions, lookahead=1)
    two = rank_parallel_actions(model, probes, assessment, actions, lookahead=2)
    assert all('dc_lookahead_action' not in action for action in one)
    first = next(action for action in two if action['index'] == 0)
    assert first['dc_response_ratios']['t'] == pytest.approx(.9)
    assert first['dc_predicted_deficit'] == pytest.approx(.125)
    assert first['dc_lookahead_deficit'] == 0
    assert first['dc_lookahead_action'] == dict(kind='parallel_line', index=1, value=2)


def test_transfer_lookahead_escapes_greedy_corridor_trap(tmp_path):
    from feeder_agents.research_tasks import run_research_task

    result = run_research_task(dict(
        task='transmission_transfer', research_question='Reduce peak AC transfer response',
        base_spec=dict(network_kind='transmission', n_buses=139, n_generators=12,
                       voltage_kv=220, total_mw=1200, seed=1105),
        transfer_mw=10, response_lower=.5, response_upper=.98,
        search=dict(allowed_actions=['parallel_line'], max_rounds=4,
                    candidates_per_round=12, max_joint_actions=1)), tmp_path, 'lookahead')
    assert result['target_met'] and result['verification_passed']
    assert result['task_validation']['accepted'] and result['electrical_base_accepted']
    assert result['protected_contract_preserved']
    response = json.loads((Path(result['directory'])/'response_designs/response/result.json').read_text())
    assert .5 <= response['selected']['targets'][0]['observed'] <= .98
    assert response['evaluated_candidates'] <= 49
    trace = json.loads((Path(response['directory'])/'history.json').read_text())
    assert all(row['action']['kind'] == 'parallel_line' for row in trace if row.get('committed'))
    selected = json.loads((Path(response['directory'])/'selected/metadata.json').read_text())
    assert all(e['circuits'] <= 4 for e in selected['branch_evidence'] if e['kind'] == 'line')
