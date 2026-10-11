"""Failure-sensitive checks of the small M101 saved-artifact evaluator."""
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


def evaluator():
    path = Path(__file__).parents[1] / "scripts/recheck_m101_transmission.py"
    assert path.exists(), "M101 saved-artifact evaluator is missing"
    spec = importlib.util.spec_from_file_location("m101_transmission_recheck", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tiny_solution():
    bus = np.zeros((2, 13))
    bus[:, 0] = [1, 2]
    bus[:, 1] = [3, 1]
    bus[:, 7] = [1.02, 1.01]
    bus[:, 9] = 110
    bus[:, 11:13] = [1.05, .95]
    bus[1, 2:6] = [20, 5, 1, -2]
    branch = np.zeros((1, 17))
    branch[0, :2] = [1, 2]
    branch[0, 5] = 100
    branch[0, 10] = 1
    branch[0, 15:17] = [-21.0201, -7.0402]
    branch[0, 13:15] = [21.1, 7.2]
    gen = np.zeros((1, 21))
    gen[0, :10] = [1, 21.1, 7.2, 10, -10, 1.02, 100, 1, 100, 0]
    return dict(version="2", baseMVA=100., bus=bus, gen=gen, branch=branch)


def test_balance_includes_voltage_dependent_shunts_and_detects_q_violation():
    module = evaluator()
    solved = tiny_solution()
    spec = SimpleNamespace(voltage_min_pu=.95, voltage_max_pu=1.05)
    result = module.physical_metrics(solved, spec)
    assert result["max_bus_p_residual_mw"] < 1e-12
    assert result["max_bus_q_residual_mvar"] < 1e-12
    assert result["q_violation_count"] == 0
    assert result["independently_within_limits"]
    solved["gen"][0, 3] = 7
    result = module.physical_metrics(solved, spec)
    assert result["max_q_violation_mvar"] == pytest.approx(.2)
    assert result["q_violation_count"] == 1
    assert result["q_witnesses"][0]["bus"] == 1
    assert not result["independently_within_limits"]


def test_protected_diff_rejects_relocation_capability_and_branch_count_changes():
    module = evaluator()
    baseline = tiny_solution()
    meta = dict(positions_km={"1": [0, 0], "2": [1, 0]}, generator_types=["thermal"])
    spec = SimpleNamespace(allowed_repairs=["shunt_step", "voltage_setpoint"])
    trial = copy.deepcopy(baseline)
    trial["bus"][0, 2] += 1
    trial["bus"][1, 2] -= 1
    trial["gen"][0, 3] += 10
    trial["branch"] = np.concatenate([trial["branch"], trial["branch"]])
    checked = module.protected_checks(baseline, meta, trial, meta, spec)
    assert not checked["bus_protected_columns"]
    assert not checked["generator_protected_columns"]
    assert not checked["branch_count"]
    assert not all(checked.values())


def test_export_fidelity_rejects_single_injection_change(tmp_path):
    module = evaluator()
    from feeder_agents.transmission import export_case
    case = tiny_solution()
    case["branch"] = case["branch"][:, :13]
    path = tmp_path / "case.m"
    export_case(case, path)
    assert module.export_matches(path, case)
    changed = copy.deepcopy(case)
    changed["bus"][1, 2] += 1
    assert not module.export_matches(path, changed)
