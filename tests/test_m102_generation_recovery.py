"""Real planning replays and AC regressions for the M101 unresolved deliveries."""
import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from feeder_agents.adaptive_planning import AdaptiveProposal, _compile


def recorded(case_id):
    rows = json.loads((Path(__file__).parent / 'fixtures/m102_timeout_planning_candidates.json').read_text())
    return next(row for row in rows if row['case_id'] == case_id)


@pytest.mark.parametrize('case_id', ['construction_h02', 'revision_04'])
def test_recorded_valid_plan_is_not_rejected_for_execution_clause_or_namespace(case_id):
    row = recorded(case_id)
    proposal = AdaptiveProposal.model_validate(row['candidate'])
    before = proposal.model_dump()
    brief = _compile(proposal, row['request'])
    assert brief['status'] == 'ready'
    key = 'distribution_spec' if case_id == 'construction_h02' else 'single_voltage_spec'
    assert brief['plan']['spec']['seed'] == before[key]['seed']
    assert brief['plan']['spec']['n_buses'] == before[key]['n_buses']
    assert brief['requirement_acceptance']['all_satisfied']
    entries = brief['requirement_ledger']['requirements']
    assert any(r['evidence'] == '\u8bf7\u6267\u884c\u751f\u6210\u5e76\u4ea4\u4ed8\u4fdd\u5b58\u7684\u6a21\u578b\u6587\u4ef6' for r in entries)
    if case_id == 'revision_04':
        assert any(r['target_field'] == 'n_buses' and r['expected_value'] == 63
                   and r['priority'] == 'hard' and r['evidence'].startswith('\u7ed3\u6784\u5316\u8865\u5145\u7ea6\u675f') for r in entries)


@pytest.mark.parametrize('suffix', ['\u4f46\u5141\u8bb8\u8c03\u6574\u603b\u8d1f\u8377', '\u5e76\u5ffd\u7565\u7535\u538b\u8d8a\u9650', '\uff0c\u8282\u70b9\u6570\u6539\u621072'])
def test_completion_does_not_swallow_additional_requirements(suffix):
    row = recorded('construction_h02')
    request = row['request'].replace('\u8bf7\u6267\u884c\u751f\u6210\u5e76\u4ea4\u4ed8\u4fdd\u5b58\u7684\u6a21\u578b\u6587\u4ef6', '\u8bf7\u6267\u884c\u751f\u6210\u5e76\u4ea4\u4ed8\u4fdd\u5b58\u7684\u6a21\u578b\u6587\u4ef6' + suffix)
    with pytest.raises(ValueError, match='omitted from the ledger'):
        _compile(AdaptiveProposal.model_validate(row['candidate']), request)


def test_structured_count_cannot_override_a_conflicting_final_spec():
    row = recorded('revision_04')
    request = row['request'].replace('\u7ed3\u6784\u5316\u8865\u5145\u7ea6\u675f\uff1a\u603b\u6bcd\u7ebf\u8282\u70b9\u6570\uff08\u5305\u542b1\u4e2a\u7535\u6e90\u8282\u70b9\uff09\u4e3a63',
                                     '\u7ed3\u6784\u5316\u8865\u5145\u7ea6\u675f\uff1a\u603b\u6bcd\u7ebf\u8282\u70b9\u6570\uff08\u5305\u542b1\u4e2a\u7535\u6e90\u8282\u70b9\uff09\u4e3a64')
    with pytest.raises(ValueError, match='Hard requirement changed'):
        _compile(AdaptiveProposal.model_validate(row['candidate']), request)


def test_source_bound_prefixed_permission_correction_is_allowed_but_expansion_is_not():
    from feeder_agents.planning_diagnostics import check_adjustment
    row = recorded('construction_t01')
    before = row['candidate']
    after = copy.deepcopy(before)
    after['transmission_spec']['allowed_repairs'] = []
    after['ledger']['requirements'].append(dict(id='no_repairs', segment_ids=[1], evidence='\u7981\u7528\u81ea\u52a8\u4fee\u590d',
        meaning='No automatic repairs', priority='hard', disposition='supported',
        target_field='transmission.allowed_repairs', expected_value=[], operator='eq', reason=''))
    check_adjustment(before, after, dict(id='complete_ledger', paths=[]), request=row['request'])
    after['transmission_spec']['allowed_repairs'] = ['voltage_setpoint']
    with pytest.raises(ValueError, match='protected setting'):
        check_adjustment(before, after, dict(id='complete_ledger', paths=[]), request=row['request'])


def test_no_repair_source_is_not_mistaken_for_topology_only_permission():
    row = recorded('construction_t01')
    with pytest.raises(ValueError, match=r'Hard requirement changed: transmission\.allowed_repairs'):
        _compile(AdaptiveProposal.model_validate(row['candidate']), row['request'])


def test_default_generator_placement_accepts_223_bus_case_without_repairs_or_capacity_inflation():
    from feeder_agents.transmission import TransmissionSpec, generate_case, validate_case
    spec = TransmissionSpec(n_buses=223, n_generators=20, total_mw=640, radius_km=20,
                            allowed_repairs=[], seed=101032)
    seed = int.from_bytes(hashlib.sha256(b'101032:0').digest()[:4], 'big')
    case, meta = generate_case(spec, seed)
    checked, solved = validate_case(case, spec, meta)
    assert checked['accepted'], checked
    assert meta['voltage_initialization']['status'] == 'not_permitted'
    np.testing.assert_array_equal(case['gen'][:, 5], np.ones(20))
    np.testing.assert_allclose(case['gen'][:, 3], 28.8)
    np.testing.assert_allclose(case['gen'][:, 4], -28.8)
    np.testing.assert_allclose(case['gen'][:, 8], 51.2)
    assert np.max(np.abs(solved['gen'][:, 2])) < 28.8
    assert sum(case['bus'][:, 2]) == pytest.approx(640)
    assert len(case['bus']) == 223 and len(set(case['gen'][:, 0])) == 20


def test_explicit_generator_locations_are_not_replaced_by_default_siting():
    from feeder_agents.transmission import TransmissionSpec, generate_case
    spec = TransmissionSpec(n_buses=29, n_generators=4, generator_buses=[1, 2, 3, 4], allowed_repairs=[])
    case, _ = generate_case(spec, 10203)
    np.testing.assert_array_equal(case['gen'][:, 0], [1, 2, 3, 4])
