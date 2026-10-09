"""Reactive dispatch regressions use fresh AC solves and unchanged nameplates."""
import copy

import numpy as np
import pytest

from feeder_agents.transmission import TransmissionSpec, generate_case, validate_case


@pytest.mark.parametrize('size,seed', [(139, 1106), (139, 1107), (139, 1301), (237, 1302)])
def test_local_voltage_initialization_recovers_q_limits_without_added_capacity(size, seed):
    spec = TransmissionSpec(n_buses=size, n_generators=12, total_mw=1200, seed=seed)
    case, metadata = generate_case(spec, seed)
    checked, solved = validate_case(case, spec, metadata)
    assert checked['accepted'], checked
    # The original flat-voltage dispatch must still reproduce the actual bug.
    nominal = copy.deepcopy(case)
    nominal['gen'][:, 5] = 1.
    before, _ = validate_case(nominal, spec, metadata)
    assert not before['checks']['generator_q_limits']
    np.testing.assert_array_equal(case['gen'][:, 3], np.full(12, 90.))
    np.testing.assert_array_equal(case['gen'][:, 4], np.full(12, -90.))
    np.testing.assert_array_equal(case['gen'][:, 8], np.full(12, 160.))
    assert np.max(np.abs(case['gen'][:, 5]-1.)) <= .01
    assert np.max(np.abs(solved['gen'][:, 2])) < 90.
    evidence = metadata['voltage_initialization']
    assert evidence['status'] == 'repaired'
    assert evidence['evaluations'] <= evidence['max_evaluations']
    assert evidence['initial_checks']['generator_q_limits'] is False
    assert evidence['final_checks']['generator_q_limits'] is True


def test_voltage_initialization_honors_repair_permissions():
    spec = TransmissionSpec(n_buses=139, n_generators=12, total_mw=1200,
                            seed=1106, allowed_repairs=['parallel_line'])
    case, metadata = generate_case(spec, spec.seed)
    np.testing.assert_array_equal(case['gen'][:, 5], np.ones(12))
    checked, _ = validate_case(case, spec, metadata)
    assert not checked['checks']['generator_q_limits']
    assert metadata['voltage_initialization']['status'] == 'not_permitted'


def test_initialized_model_is_stable_before_compiler_reference_is_frozen(tmp_path):
    from feeder_agents.research_tasks import compile_research_task, _model_hash
    from feeder_agents.research_measurement import evaluate_research_task
    from feeder_agents.response_measurement import create_model

    payload = dict(task='transmission_transfer', research_question='Balanced transfer',
        base_spec=dict(network_kind='transmission', n_buses=139, n_generators=12,
                       total_mw=1200., seed=1106), transfer_mw=10.)
    compiled = compile_research_task(payload)
    spec = TransmissionSpec(**compiled['response_plan']['base_spec'])
    model = create_model(spec)
    assert _model_hash(model) == compiled['context']['original_model_hash']
    assert _model_hash(create_model(spec)) == _model_hash(model)
    checked, _ = validate_case(model['case'], spec, model['metadata'])
    assert checked['accepted']
    finite = evaluate_research_task(model, spec, compiled['context'], tmp_path)
    assert finite['accepted'], finite
