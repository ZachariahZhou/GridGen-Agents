"""M81: real transmission validation binds every requested hard constraint."""

import copy

import numpy as np
import pytest

from feeder_agents.schemas import DocumentRule
from feeder_agents.transmission import (
    TransmissionSpec,
    condition_case,
    generate_case,
    read_transmission_result,
    run_transmission,
    validate_case,
)


def document_rule(threshold):
    return DocumentRule(
        rule_id='doc.' + '1' * 20,
        document_id='2' * 64,
        source_title='Research line-length constraint',
        chunk_index=0,
        voltage_levels_kv=[220],
        metric='max_line_km',
        operator='le',
        threshold=threshold,
        unit='km',
        quote='Each line must remain within the stated length bound.',
        locator='section 1',
    )


def test_duplicate_document_ids_are_rejected_at_submission():
    with pytest.raises(ValueError, match='Duplicate document rule IDs'):
        TransmissionSpec(document_rules=[document_rule(.01), document_rule(1000)])


def test_document_aggregation_cannot_overwrite_failure_after_unvalidated_copy():
    spec = TransmissionSpec(n_buses=9, total_mw=180)
    case, metadata = generate_case(spec, 41)
    # model_copy does not validate updates: aggregation must still retain failure.
    changed = spec.model_copy(update={'document_rules': [document_rule(.01), document_rule(1000)]})
    checked, _ = validate_case(case, changed, metadata)
    assert [row['passed'] for row in checked['document_rules']] == [False, True]
    assert not checked['checks']['rule:doc.' + '1' * 20]
    assert not checked['accepted']


@pytest.mark.parametrize('overrides,condition', [
    ({'total_mw': 20000}, {'name': 'peak', 'load_scale': 3}),
    ({}, {'name': 'voltage', 'targets': [
        {'metric': 'min_voltage_pu', 'operator': 'ge', 'threshold': 1.2}]}),
    ({}, {'name': 'capacity', 'targets': [
        {'metric': 'max_branch_loading', 'operator': 'ge', 'threshold': 1.1}]}),
    ({}, {'name': 'conflict', 'targets': [
        {'metric': 'losses_mw', 'operator': 'ge', 'threshold': 2},
        {'metric': 'losses_mw', 'operator': 'le', 'threshold': 1}]}),
])
def test_composed_condition_is_validated_at_submission(overrides, condition):
    with pytest.raises(ValueError, match='validation condition'):
        TransmissionSpec(**overrides, validation_conditions=[condition])


def test_condition_at_construction_cap_keeps_original_equipment_and_exact_scale():
    spec = TransmissionSpec(total_mw=25000, validation_conditions=[{'name': 'peak', 'load_scale': 2}])
    case, metadata = generate_case(spec, 41)
    original = copy.deepcopy(case)
    trial, derived = condition_case(case, spec, spec.validation_conditions[0])
    assert derived.total_mw == 50000
    assert derived.validation_conditions == []
    np.testing.assert_array_equal(trial['bus'][:, 2:4], original['bus'][:, 2:4] * 2)
    np.testing.assert_array_equal(trial['branch'], original['branch'])
    np.testing.assert_array_equal(trial['gen'][:, 3:], original['gen'][:, 3:])
    np.testing.assert_array_equal(case['bus'], original['bus'])


@pytest.mark.parametrize('corruption', ['zero_q', 'reverse_q', 'redistribute_q', 'zero_p_nonzero_q'])
def test_actual_reactive_demand_must_match_requested_pf_per_bus(corruption):
    spec = TransmissionSpec(n_buses=9, total_mw=180)
    case, metadata = generate_case(spec, 41)
    if corruption == 'zero_q':
        case['bus'][:, 3] = 0
    elif corruption == 'reverse_q':
        case['bus'][:, 3] *= -1
    elif corruption == 'redistribute_q':
        case['bus'][0, 3] += 1
        case['bus'][1, 3] -= 1
    else:
        case['bus'][1, 2] += case['bus'][0, 2]
        case['bus'][0, 2] = 0
    checked, _ = validate_case(case, spec, metadata)
    assert checked['converged']
    assert not checked['checks']['demand']
    assert not checked['accepted']


def test_reactive_mismatch_is_structurally_invalid_for_feedback():
    from feeder_agents.transmission_feedback import assessment
    spec = TransmissionSpec(n_buses=9, total_mw=180)
    case, metadata = generate_case(spec, 41)
    case['bus'][:, 3] = 0
    checked, _ = assessment(case, metadata, spec)
    assert not checked['valid']


@pytest.mark.parametrize('power_factor', [.85, .97, 1.])
def test_real_ac_validates_unchanged_pf_in_base_and_scaled_conditions(power_factor):
    spec = TransmissionSpec(n_buses=9, total_mw=180, power_factor=power_factor,
                            validation_conditions=[{'name': 'light', 'load_scale': .8}])
    case, metadata = generate_case(spec, 41)
    checked, _ = validate_case(case, spec, metadata)
    assert checked['accepted'], checked
    assert checked['checks']['demand']
    assert checked['conditions'][0]['checks']['demand']
    assert checked['conditions'][0]['metrics']['demand_mw'] == pytest.approx(144)


def test_pf_check_uses_signed_pd_and_handles_zero_demand_without_division():
    spec = TransmissionSpec(n_buses=9, total_mw=180, power_factor=.97)
    case, metadata = generate_case(spec, 41)
    # The PF equality must preserve signs. This does not add a new generation mode.
    demand = case['bus'][0, 2]
    reactive = case['bus'][0, 3]
    case['bus'][0, 2:4] = [-demand, -reactive]
    case['bus'][1, 2:4] += [2 * demand, 2 * reactive]
    case['bus'][3, 2:4] += case['bus'][2, 2:4]
    case['bus'][2, 2:4] = 0
    checked, _ = validate_case(case, spec, metadata)
    assert checked['converged']
    assert checked['checks']['demand']
    case['bus'][0, 3] *= -1
    changed, _ = validate_case(case, spec, metadata)
    assert not changed['checks']['demand']


def test_valid_conditions_survive_workflow_export_and_verified_reload(tmp_path):
    spec = TransmissionSpec(n_buses=9, total_mw=180,
                            validation_conditions=[{'name': 'light', 'load_scale': .8}])
    result = run_transmission(spec, tmp_path, 'm81_valid_conditions', use_memory=False)
    assert result['accepted'] == 1
    assert result['samples'][0]['checks']['demand']
    assert result['samples'][0]['conditions'][0]['checks']['demand']
    assert read_transmission_result(result['directory']) == result
