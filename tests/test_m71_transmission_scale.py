"""Regression coverage for the bounded M71 transmission scale extension."""
import networkx as nx
import numpy as np
import pytest

from feeder_agents.transmission import (
    TransmissionSpec,
    VoltageLayer,
    generate_case,
    parameter_checks,
    transmission_capabilities,
    validate_case,
)


def t237_spec():
    return TransmissionSpec(
        n_buses=237,
        n_generators=36,
        total_mw=6500,
        voltage_kv=500,
        mesh_style="mixed",
        radius_km=85,
        seed=701,
        voltage_layers=[{"kv": 500, "buses": 97}, {"kv": 220, "buses": 140}],
    )


def test_t237_generation_keeps_layers_demands_and_real_ac_solution():
    spec = t237_spec()
    case, metadata = generate_case(spec, 701)
    assert case["bus"].shape == (237, 13)
    assert case["gen"].shape == (36, 21)
    assert np.count_nonzero(case["bus"][:, 9] == 500) == 97
    assert np.count_nonzero(case["bus"][:, 9] == 220) == 140
    assert case["bus"][:, 2].sum() == pytest.approx(6500)
    assert sum(e["kind"] == "transformer" for e in metadata["branch_evidence"]) == 2
    checked, solved = validate_case(case, spec, metadata)
    assert checked["accepted"], checked
    assert checked["converged"], checked
    assert checked["checks"]["finite"]
    assert checked["checks"]["demand"]
    assert checked["checks"]["topology"]
    assert solved["branch"].shape[1] >= 17
    assert all(parameter_checks(case, spec, metadata).values())


def test_299_bus_layer_generates_connected_mesh_with_exact_requested_size():
    spec = TransmissionSpec(
        n_buses=299, n_generators=40, total_mw=3500,
        voltage_layers=[{"kv": 330, "buses": 299}],
    )
    case, metadata = generate_case(spec, 701)
    graph = nx.Graph()
    graph.add_edges_from((int(row[0]), int(row[1])) for row in case["branch"])
    assert len(case["bus"]) == len(graph) == 299
    assert len(case["branch"]) == 398
    assert nx.is_connected(graph)
    assert case["bus"][:, 2].sum() == pytest.approx(3500)
    assert all(parameter_checks(case, spec, metadata).values())


def test_300_bus_boundary_is_advertised_and_validated():
    advertised = transmission_capabilities()["schema"]
    assert advertised["properties"]["n_buses"]["maximum"] == 300
    assert advertised["$defs"]["VoltageLayer"]["properties"]["buses"]["maximum"] == 300
    spec = TransmissionSpec(n_buses=300, voltage_layers=[{"kv": 220, "buses": 300}])
    assert spec.n_buses == 300


@pytest.mark.parametrize("count", [301, 1000])
def test_above_300_buses_is_rejected_for_total_and_layer(count):
    with pytest.raises(ValueError):
        TransmissionSpec(n_buses=count)
    with pytest.raises(ValueError):
        VoltageLayer(kv=220, buses=count)
