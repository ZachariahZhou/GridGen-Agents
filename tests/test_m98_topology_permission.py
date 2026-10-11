"""Topology amendments preserve affirmative numeric limits in English."""
import pytest

SOURCE = ('I explicitly authorize equipment replacement of at most two conductors, '
    'at most one branch reconnection, new/old rewire length ratio at most 1.2, '
    'maximum node degree four. Geometry scaling remains forbidden. '
    'Preserve original targets, baseline, demand, PV and voltage.')


def plan():
    return dict(research_question='Permission source check', base_spec=dict(network_kind='distribution',
        n_buses=13, total_kw_min=120, total_kw_max=120, repair_policy={'strategy': 'none'}),
        probes=[dict(id='q', kind='voltage_q')], targets=[dict(probe_id='q', reference='baseline_ratio', lower=1.1, upper=1.3)],
        search=dict(allowed_actions=[]))


def grant():
    return dict(tier='equipment_topology', max_changed_conductors=2, max_rewired_lines=1,
        max_rewire_length_ratio=1.2, max_node_degree=4)


def test_exact_recorded_topology_amendment_is_accepted():
    from feeder_agents.response_permissions import permission_source_issues
    assert permission_source_issues(SOURCE, plan(), grant()) == []


@pytest.mark.parametrize('field,value', [('max_changed_conductors', 3), ('max_rewired_lines', 2),
    ('max_rewire_length_ratio', 1.21), ('max_node_degree', 5)])
def test_each_topology_amendment_limit_remains_enforced(field, value):
    from feeder_agents.response_permissions import permission_source_issues
    assert permission_source_issues(SOURCE, plan(), grant() | {field: value})


@pytest.mark.parametrize('source', [SOURCE.replace('I explicitly authorize', 'I do not authorize'),
    SOURCE + ' No branch reconnection is allowed.', SOURCE.replace('I explicitly authorize', 'A report mentions')])
def test_denied_or_descriptive_topology_numbers_do_not_authorize(source):
    from feeder_agents.response_permissions import permission_source_issues
    assert permission_source_issues(source, plan(), grant())
