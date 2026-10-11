import pytest

from feeder_agents.hierarchy import HierarchicalSpec, generate_hierarchy
from feeder_agents.requirement_contract import audit_ledger, model_observations
from feeder_agents.requirements import RequirementLedger


def phase_audit(feeder, weights=(.5, .3, .2)):
    ledger = RequirementLedger(network_kind='distribution', summary='Phase allocation', requirements=[dict(
        id='phases', segment_ids=[0], evidence='ABC customer allocation 50/30/20', meaning='Customer phase allocation',
        priority='hard', disposition='supported', target_field='hierarchy.phase_weights', expected_value=list(weights))])
    return audit_ledger(ledger, model_observations('distribution', feeder))


def test_observes_integer_phase_allocation_without_design_provenance():
    feeder = generate_hierarchy(HierarchicalSpec(users=12, phase_weights=[.5, .3, .2]), 41)
    feeder.design_evidence = {}
    observed = model_observations('distribution', feeder)
    assert observed['phase_weights'] == [6 / 12, 4 / 12, 2 / 12]
    assert phase_audit(feeder)['all_satisfied']
    assert not phase_audit(feeder, (.3, .5, .2))['all_satisfied']


@pytest.mark.parametrize('corruption', ['allocation', 'phase_power_label', 'phase_power_value'])
def test_rejects_changed_allocation_or_inconsistent_phase_power(corruption):
    feeder = generate_hierarchy(HierarchicalSpec(users=12, phase_weights=[.5, .3, .2]), 41)
    load = next(load for load in feeder.loads if load.phases == [1])
    if corruption == 'allocation':
        load.phases = [2]
        load.phase_powers[0].phase = 2
    elif corruption == 'phase_power_label':
        load.phase_powers[0].phase = 2
    else:
        load.phase_powers[0].kw += 1
    assert not phase_audit(feeder)['all_satisfied']
