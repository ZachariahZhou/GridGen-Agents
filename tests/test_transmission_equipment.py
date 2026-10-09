"""Behavioral checks for joint, voltage-conditioned transmission equipment."""
import copy

import numpy as np
import pytest

from feeder_agents.transmission import TransmissionSpec, generate_case, parameter_checks
from feeder_agents.transmission_model import refresh


@pytest.mark.parametrize('kv', [110, 220, 330, 500, 750])
def test_same_voltage_network_can_use_distinct_joint_line_types(kv):
    spec = TransmissionSpec(n_buses=37, n_generators=6, voltage_kv=kv, total_mw=300)
    case, meta = generate_case(spec, 42)
    ratios = case['branch'][:, 3] / case['branch'][:, 2]
    assert np.std(ratios) / np.mean(ratios) > .01
    assert all(parameter_checks(case, spec, meta).values())


def test_equipment_draw_is_repeatable_without_changing_demands_or_topology():
    spec = TransmissionSpec(n_buses=37, total_mw=300)
    first, metadata = generate_case(spec, 42)
    second, repeated = generate_case(spec, 42)
    for key in ('bus', 'gen', 'branch'):
        np.testing.assert_array_equal(first[key], second[key])
    assert metadata == repeated


def test_current_screen_selects_a_feasible_joint_type_or_declares_capacity_limit():
    from feeder_agents.transmission_equipment import select_line_profile
    # At 220 kV / PF=.97, 1200 MW exceeds four 0.96 kA circuits with
    # 20% headroom, but fits four 1.15 kA circuits. Fields stay coupled.
    for seed in range(5):
        profile, evidence = select_line_profile(220, 50, 1200, .97, np.random.default_rng(seed))
        assert profile['max_i_ka'] == 1.15
        assert (profile['r_ohm_km'],profile['x_ohm_km'],profile['c_nf_km']) == (.042,.275,11.7)
        assert evidence['within_four_circuit_screen']
    _, evidence = select_line_profile(220, 50, 5000, .97, np.random.default_rng(1))
    assert not evidence['within_four_circuit_screen']


@pytest.mark.parametrize('corruption', ['resistance', 'profile_id', 'missing_catalogue_hash'])
def test_recomputed_matrix_cannot_hide_invalid_equipment(corruption):
    spec = TransmissionSpec(n_buses=37, total_mw=300)
    case, metadata = generate_case(spec, 42)
    if corruption == 'resistance':
        metadata['branch_evidence'][0]['r_ohm_km'] *= 1.2
    elif corruption == 'profile_id':
        metadata['branch_evidence'][0]['profile_id'] = 'not_a_catalogue_type'
    else:
        metadata.pop('line_family_catalogue_hash', None)
    refresh(case, metadata)
    assert not all(parameter_checks(case, spec, metadata).values())


def test_legacy_homogeneous_model_remains_valid():
    from feeder_agents.rules import data
    from feeder_agents.transmission_model import line_record
    from feeder_agents.artifacts import digest
    spec = TransmissionSpec(n_buses=9, total_mw=180)
    case, metadata = generate_case(spec, 41)
    old = copy.deepcopy(metadata)
    for key in ('line_parameter_policy', 'line_family_catalogue_hash'):
        old.pop(key, None)
    old['catalogue_hash'] = digest(data('transmission_lines.json'))
    old['branch_evidence'] = [line_record(e['from_bus'], e['to_bus'], old['positions_km'],
                                         e['voltage_kv'], e['circuits'])
                              for e in old['branch_evidence']]
    refresh(case, old)
    assert all(parameter_checks(case, spec, old).values())


def test_parallel_repair_preserves_type_and_physical_scaling():
    from feeder_agents.transmission_feedback import apply
    spec = TransmissionSpec(n_buses=9, total_mw=180)
    case, metadata = generate_case(spec, 41)
    index = next(i for i, e in enumerate(metadata['branch_evidence']) if e['circuits'] == 1)
    revised, evidence = apply(case, metadata, spec, {'kind': 'parallel_line', 'index': index, 'value': 2})
    np.testing.assert_allclose(revised['branch'][index, 2:4], case['branch'][index, 2:4] / 2)
    np.testing.assert_allclose(revised['branch'][index, 4:8], case['branch'][index, 4:8] * 2)
    assert evidence['branch_evidence'][index]['profile_id'] == metadata['branch_evidence'][index]['profile_id']
    assert all(parameter_checks(revised, spec, evidence).values())


def test_local_voltage_action_changes_only_the_identified_generator():
    from feeder_agents.transmission_feedback import apply
    spec = TransmissionSpec(n_buses=9, total_mw=180)
    case, metadata = generate_case(spec, 41)
    revised, _ = apply(case, metadata, spec,
                       {'kind': 'voltage_setpoint', 'index': 1, 'value': .9995})
    np.testing.assert_array_equal(revised['gen'][[0, 2], :], case['gen'][[0, 2], :])
    assert revised['gen'][1, 5] == .9995
    with pytest.raises(ValueError):
        apply(case, metadata, spec, {'kind': 'voltage_setpoint', 'index': -1, 'value': .9995})
    with pytest.raises(ValueError, match='not authorized'):
        apply(case, metadata, spec.model_copy(update={'allowed_repairs': []}),
              {'kind': 'voltage_setpoint', 'index': 1, 'value': .9995})


def test_local_q_violation_has_a_fine_repair_that_passes_fresh_ac(tmp_path):
    from feeder_agents.transmission_feedback import run_feedback, preserved
    from feeder_agents.transmission import validate_case
    spec = TransmissionSpec(n_buses=237, n_generators=36, total_mw=6500,
        voltage_layers=[{'kv': 500, 'buses': 97}, {'kv': 220, 'buses': 140}], radial_bus_count=15)
    case, meta = generate_case(spec, 43)
    # Recreate the nominal operating point to exercise the feedback repair;
    # generation now initializes permitted local voltage controls itself.
    case['gen'][:, 5] = 1.
    before, _ = validate_case(case, spec, meta)
    assert before['converged'] and not before['checks']['generator_q_limits']

    class SelectLocalVoltage:
        # Tests execution/replay with an explicit choice; this is not an LLM experiment.
        def with_structured_output(self, *args, **kwargs):
            return self

        def invoke(self, messages):
            import json
            offered = json.loads(messages[-1][1])['options']
            key = next(k for k, v in offered.items() if v['kind']=='voltage_setpoint'
                       and v.get('index')==28 and v['value']==.9995)
            return dict(decision='repair', action_id=key,
                        diagnosis='Local generator Q upper limit violation',
                        reason='Small local voltage reduction with fresh AC verification')

    result = run_feedback(case, meta, spec, tmp_path, model=SelectLocalVoltage(), max_rounds=1)
    assert result['assessment']['checked']['accepted']
    assert result['report']['accepted_steps'] == 1
    assert preserved(case, meta, result['case'], result['metadata'], spec)


def test_local_voltage_candidates_advance_after_prior_generators_are_exhausted():
    from feeder_agents.artifacts import digest
    from feeder_agents.transmission_feedback import assessment, propose
    spec = TransmissionSpec(n_buses=12,n_generators=4,total_mw=180,
                            allowed_repairs=['voltage_setpoint'])
    case, meta = generate_case(spec,42)
    # A deliberately narrow but ordered Q range creates actual local violations.
    case['gen'][:,3] = case['gen'][:,4] + .1
    checked, solved = assessment(case,meta,spec)
    first = propose(case,meta,spec,checked,solved)
    used_indices = {a['index'] for a in first if 'index' in a}
    assert len(used_indices)==2
    tried = {digest(a) for a in first}
    later = propose(case,meta,spec,checked,solved,eligible=lambda a:digest(a) not in tried)
    assert any(a.get('index') not in used_indices for a in later if 'index' in a)
