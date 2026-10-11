"""Public/private separation and honest feasibility construction denominators."""
import pytest
from test_response_permissions_m95 import plan_payload


def test_public_task_cannot_carry_witness_evidence():
    from feeder_agents.benchmark.response_suite import public_task
    p=public_task('n','t','witness','G',plan_payload())
    assert set(p)=={'network_id','task_id','layer','space','plan'}
    assert all('witness' not in k for k in p)


def test_trivial_witness_intervals_are_rejected():
    from feeder_agents.benchmark.response_suite import witness_plan
    with pytest.raises(ValueError,match='trivial'):
        witness_plan(plan_payload(),{'a':1.0})
    result=witness_plan(plan_payload(),{'a':1.15})
    assert result['targets'][0]['lower']>1
    assert result['targets'][0]['lower']<1.15<result['targets'][0]['upper']


def test_invalid_baseline_keeps_research_request_but_not_false_witness():
    from feeder_agents.benchmark.response_suite import construction_status
    assert construction_status(False,False,False,False)=='invalid_baseline'
    assert construction_status(True,False,True,True)=='witness_contract_failed'
    assert construction_status(True,True,False,True)=='witness_response_failed'
    assert construction_status(True,True,True,False)=='step_consistency_failed'
    assert construction_status(True,True,True,True)=='certified'


def test_duplicate_network_fingerprints_are_rejected():
    from feeder_agents.benchmark.response_suite import check_unique_networks
    with pytest.raises(ValueError,match='physical'):
        check_unique_networks([{'network_id':'a','physical_hash':'same'},{'network_id':'b','physical_hash':'same'}])
    check_unique_networks([{'network_id':'a','physical_hash':'first'},{'network_id':'b','physical_hash':'second'}])


def test_witness_attempts_are_finite_and_family_specific():
    from feeder_agents.benchmark.response_suite import ATTEMPTS
    assert len(ATTEMPTS)==4
    assert all(0<a<1 and 0<b<1 for a,b in ATTEMPTS)
