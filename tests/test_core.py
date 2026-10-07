import math

import networkx as nx
import pytest
from pydantic import ValidationError

from feeder_agents.schemas import ExperimentSpec
from feeder_agents.generation import generate_feeder, upgrade_conductors
from feeder_agents.rules import load_rules, search_sources


@pytest.mark.parametrize('kwargs', [
    {'n_loads_min': 10, 'n_loads_max': 2}, {'count': 0},
    {'total_kw_min': float('nan')}, {'voltage_kv': 110},
    {'power_factor': 0}, {'topology': 'meshed'}, {'max_repairs': -1},
])
def test_invalid_or_unsupported_request_is_rejected(kwargs):
    with pytest.raises(ValidationError):
        ExperimentSpec(**kwargs)


def test_generator_is_connected_reproducible_and_conserves_demand():
    spec = ExperimentSpec(n_loads_min=20, n_loads_max=20,
                          total_kw_min=1000, total_kw_max=1000, pv_ratio=0.4)
    a = generate_feeder(spec, 41)
    b = generate_feeder(spec, 41)
    assert a == b
    assert a != generate_feeder(spec, 42)
    graph = nx.Graph([(line.bus1, line.bus2) for line in a.lines])
    assert nx.is_tree(graph)
    assert len(a.loads) == 20
    assert len(a.buses) == 21
    assert sum(x.kw for x in a.loads) == pytest.approx(1000)
    assert sum(x.pv_kw for x in a.loads) == pytest.approx(400)
    for line in a.lines:
        assert spec.segment_km_min <= line.length_km <= spec.segment_km_max
    assert math.isfinite(sum(x.kvar for x in a.loads))


def test_conductor_repair_cannot_change_experimental_conditions():
    feeder = generate_feeder(ExperimentSpec(), 5)
    repaired = upgrade_conductors(feeder)
    assert repaired.loads == feeder.loads
    assert repaired.buses == feeder.buses
    assert [(x.bus1, x.bus2, x.length_km) for x in repaired.lines] == [
        (x.bus1, x.bus2, x.length_km) for x in feeder.lines]
    assert any(a.conductor != b.conductor for a, b in zip(feeder.lines, repaired.lines))
    assert feeder == generate_feeder(ExperimentSpec(), 5)


def test_rule_provenance_and_metadata_only_documents():
    rules = load_rules()
    assert rules['cn.frequency']['parameters']['hz'] == 50
    assert rules['cn.voltage']['parameters'] == {'min_pu': 0.93, 'max_pu': 1.07}
    assert all(r['source_id'] != 'dlt5729-2023' for r in rules.values())
    sources = search_sources('5729')
    assert sources[0]['access'] == 'metadata_only'
    assert search_sources('电压')[0]['id'] == 'ndrc-2024'
