"""The requested radius is a measured coordinate envelope, not a scale label."""
import copy
import math

import numpy as np
import pytest

from feeder_agents.requirement_contract import model_observations
from feeder_agents.transmission import TransmissionSpec, generate_case, validate_case
from feeder_agents.transmission_topology import build_topology


@pytest.mark.parametrize("family", ["ring_chords", "regional", "corridor"])
@pytest.mark.parametrize("layers", [None, [dict(kv=220, buses=6), dict(kv=110, buses=6)]])
def test_requested_radius_bounds_every_layer_and_is_attained(family, layers):
    spec = TransmissionSpec(n_buses=12, n_generators=3, total_mw=180, radius_km=20,
                            mesh_family=family, voltage_layers=layers)
    positions, *_ = build_topology(spec, np.random.default_rng(41), 41)
    distances = [math.hypot(*xy) for xy in positions.values()]
    assert max(distances) == pytest.approx(20, abs=1e-10)
    assert all(distance <= 20 + 1e-10 for distance in distances)


def test_radius_scaling_preserves_seeded_shape_topology_and_loads():
    small = TransmissionSpec(n_buses=12, n_generators=3, total_mw=180, radius_km=10, allowed_repairs=[])
    large = small.model_copy(update=dict(radius_km=20))
    case_a, metadata_a = generate_case(small, 41)
    case_b, metadata_b = generate_case(large, 41)
    assert np.array_equal(case_a["branch"][:, :2], case_b["branch"][:, :2])
    assert np.array_equal(case_a["bus"][:, 2:4], case_b["bus"][:, 2:4])
    for bus, xy in metadata_a["positions_km"].items():
        assert np.allclose(np.asarray(xy) * 2, metadata_b["positions_km"][bus])
    assert max(math.hypot(*xy) for xy in metadata_b["positions_km"].values()) == pytest.approx(20)
    assert validate_case(case_b, large, metadata_b)[0]["accepted"]


def test_radius_observation_uses_complete_actual_positions():
    spec = TransmissionSpec(n_buses=9, n_generators=3, total_mw=180, radius_km=20)
    case, metadata = generate_case(spec, 41)
    observed = model_observations("transmission", case, metadata)
    assert observed["radius_km"] == pytest.approx(20)
    moved = copy.deepcopy(metadata)
    first = next(iter(moved["positions_km"]))
    moved["positions_km"][first] = [30, 40]
    assert model_observations("transmission", case, moved)["radius_km"] == pytest.approx(50)
    del moved["positions_km"][first]
    assert model_observations("transmission", case, moved).get("radius_km") is None
    assert model_observations("transmission", case, {}).get("radius_km") is None
