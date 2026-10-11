"""Replay preserved formal failures without external model calls."""
import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from feeder_agents.adaptive_planning import AdaptiveProposal, _compile, adaptive_plan
from feeder_agents.planning_diagnostics import failure_bundle, normalize_candidate
from feeder_agents.planning_patches import apply_scoped_patch
from feeder_agents.structured_planning import StructuredPlanningError
from feeder_agents.artifacts import digest
from feeder_agents.requirements import RequirementLedger
from test_m98_ordinary_planning_contract import OfflinePlanner


CASES = json.loads((Path(__file__).parent / "fixtures/m98_formal_planning_candidates.json").read_text())
SCHEMA_CASES = [name for name, case in CASES.items() if "corrected_candidate" not in case]
COMPLETION_CASES = [name for name, case in CASES.items() if "corrected_candidate" in case]


@pytest.mark.parametrize("name", SCHEMA_CASES)
def test_recorded_missing_topology_has_a_bounded_default_repair(name):
    case = CASES[name]
    raw = copy.deepcopy(case["candidate"])
    with pytest.raises(ValidationError) as caught:
        AdaptiveProposal.model_validate(raw)
    bundle = failure_bundle(StructuredPlanningError(str(caught.value), caught.value),
                            "schema", case["request"], raw)
    action = next(a for a in bundle["allowed_actions"] if a["id"] == "repair_representation")
    repair = next(r for r in action["validated_repairs"] if r["id"] == "default_unbound_layout")
    assert repair["edits"] == [dict(path="single_voltage_spec.scenario.layout", operation="set", value="spatial_mst")]
    fixed = apply_scoped_patch(raw, repair["edits"], action, digest(raw))
    assert fixed["ledger"] == normalize_candidate(raw)["ledger"]
    assert fixed["single_voltage_spec"]["n_buses"] == raw["single_voltage_spec"]["n_buses"]
    assert AdaptiveProposal.model_validate(fixed).single_voltage_spec.scenario.topology is None


@pytest.mark.parametrize("name", COMPLETION_CASES)
def test_recorded_id_only_rename_does_not_block_source_completion(tmp_path, name):
    case = CASES[name]
    result = adaptive_plan(case["request"], tmp_path,
                           OfflinePlanner([case["candidate"], case["corrected_candidate"]]))
    assert result["status"] == "ready"
    original = {r["id"]: r for r in case["candidate"]["ledger"]["requirements"]}
    final = {r["id"]: r for r in result["requirement_ledger"]["requirements"]}
    for key, item in original.items():
        assert final[key] == item
    assert len(final) == len(original) + 1


@pytest.mark.parametrize("name", [name for name in SCHEMA_CASES if name.startswith("units_counts")])
def test_recorded_english_closed_bus_interval_remains_the_user_contract(name):
    case = CASES[name]
    raw = copy.deepcopy(case["candidate"])
    raw["single_voltage_spec"]["scenario"]["layout"] = "spatial_mst"
    brief = _compile(AdaptiveProposal.model_validate(raw), case["request"])
    bounds = [(r["operator"], r["expected_value"]) for r in brief["requirement_ledger"]["requirements"]
              if r["target_field"] == "n_buses" and r["priority"] == "hard"]
    assert set(bounds) == {("ge", 31), ("le", 39)}
    assert brief["plan"]["spec"]["n_buses"] == 35


@pytest.mark.parametrize("field,value", [("scenario.layout", "structured_radial"),
                                         ("scenario.topology.family", "comb"),
                                         ("single_voltage_spec.scenario.layout", "structured_radial"),
                                         ("single_voltage_spec.scenario.topology.family", "comb")])
def test_missing_topology_repair_cannot_replace_a_bound_layout(field, value):
    from feeder_agents.planning_representation import representation_repairs

    raw = copy.deepcopy(CASES[SCHEMA_CASES[0]]["candidate"])
    raw["ledger"]["requirements"].append(dict(id="topology", evidence="Use structured radial topology",
        meaning="User topology requirement", priority="hard", disposition="supported",
        target_field=field, expected_value=value))
    assert not any(r["id"] == "default_unbound_layout" for r in representation_repairs(raw))


@pytest.mark.parametrize("name", SCHEMA_CASES)
def test_recorded_schema_repair_replays_through_all_planning_checks(tmp_path, name):
    case = CASES[name]
    fixed = copy.deepcopy(case["candidate"])
    fixed["single_voltage_spec"]["scenario"]["layout"] = "spatial_mst"
    complete = copy.deepcopy(fixed)
    if "\n" in case["request"]:
        quote = case["request"].split("\n")[-1]
        complete["ledger"]["requirements"].append(dict(id="supplemental_count", evidence=quote,
            meaning="Preserve the repeated total bus count", priority="hard", disposition="supported",
            target_field="n_buses", expected_value=29, operator="eq"))
    result = adaptive_plan(case["request"], tmp_path, OfflinePlanner([case["candidate"], fixed, complete]))
    assert result["status"] == "ready"
    assert result["plan"]["spec"]["repair_policy"]["strategy"] == "none"
    assert result["plan"]["spec"]["seed"] == 971031


@pytest.mark.parametrize("mutation", ["value", "quote", "priority", "meaning"])
def test_id_restoration_cannot_hide_requirement_changes(tmp_path, mutation):
    case = copy.deepcopy(CASES[COMPLETION_CASES[0]])
    # The fixed structured count clause is now bound without a second proposal.
    # Keep a genuinely omitted compound clause so the unsafe correction is
    # actually consumed and the original scope invariant remains exercised.
    old_quote = '\u7ed3\u6784\u5316\u8865\u5145\u7ea6\u675f\uff1a\u603b\u6bcd\u7ebf\u8282\u70b9\u6570\uff08\u5305\u542b1\u4e2a\u7535\u6e90\u8282\u70b9\uff09\u4e3a29'
    new_quote = '\u603b\u8282\u70b9\u6570\u518d\u6b21\u660e\u786e\u4e3a29\uff0c\u5305\u542b\u7535\u6e90\u8282\u70b9'
    case['request'] = case['request'].replace(old_quote, new_quote)
    for item in case['corrected_candidate']['ledger']['requirements']:
        if item['evidence'] == old_quote:
            item['evidence'] = new_quote
    entry = case["corrected_candidate"]["ledger"]["requirements"][0]
    if mutation == "value":entry["expected_value"] = 30
    elif mutation == "quote":entry["evidence"] = "10 kV"
    elif mutation == "priority":entry["priority"] = "preference"
    else:entry["meaning"] = "The original count is optional"
    result = adaptive_plan(case["request"], tmp_path,
                           OfflinePlanner([case["candidate"], case["corrected_candidate"]]))
    assert result["status"] == "planning_failed"
    assert result["adaptive_trace"]["stop_reason"] == "adjustment_scope_violation"


@pytest.mark.parametrize("count,passes", [(31, True), (39, True), (30, False), (40, False)])
def test_english_interval_accepts_endpoints_and_rejects_outside(count, passes):
    case = CASES[SCHEMA_CASES[0]]
    raw = copy.deepcopy(case["candidate"])
    raw["single_voltage_spec"]["scenario"]["layout"] = "spatial_mst"
    raw["single_voltage_spec"]["n_buses"] = count
    if passes:
        assert _compile(AdaptiveProposal.model_validate(raw), case["request"])["status"] == "ready"
    else:
        with pytest.raises(ValueError, match="Hard requirement changed"):
            _compile(AdaptiveProposal.model_validate(raw), case["request"])


def test_english_interval_preserves_an_independent_exact_count():
    case = CASES[SCHEMA_CASES[0]]
    raw = copy.deepcopy(case["candidate"])
    raw["single_voltage_spec"]["scenario"]["layout"] = "spatial_mst"
    # Literal quotes remain valid when the request is extended; source refs do not.
    from feeder_agents.source_bindings import bind_payload_sources
    raw = bind_payload_sources(raw, case["request"], strict=True)[0]
    quote = "Use exactly 33 buses"
    raw["ledger"]["requirements"].append(dict(id="exact", evidence=quote, meaning=quote,
        priority="hard", disposition="supported", target_field="n_buses", expected_value=33))
    with pytest.raises(ValueError, match="expected=33"):
        _compile(AdaptiveProposal.model_validate(raw), case["request"] + "; " + quote)


@pytest.mark.parametrize("allow_rewire", [False, True])
def test_no_reconnection_delivery_needs_bound_permission_and_unchanged_baseline(tmp_path, allow_rewire):
    from feeder_agents.requirement_contract import audit_delivery
    from feeder_agents.schemas import ExperimentSpec
    from feeder_agents.workflow import run_experiment

    spec = ExperimentSpec(n_buses=9, total_kw_min=48, total_kw_max=48,
                          repair_policy=dict(strategy="none", allow_rewire=allow_rewire))
    output = run_experiment(spec, tmp_path, "no-reconnect")
    brief = dict(status="ready", plan_type="feeder", plan=dict(spec=spec.model_dump()))
    ledger = RequirementLedger(network_kind="distribution", model_family="single_voltage", summary="No reconnection",
        requirements=[dict(id="no_reconnect", evidence="Do not reconnect branches", meaning="No reconnection",
                          priority="hard", disposition="supported", target_field="repair_policy.allow_rewire", expected_value=False)])
    report = audit_delivery(ledger, brief, output)
    check = report["samples"][0]["checks"][0]
    assert check["observed"] is allow_rewire
    assert report["all_satisfied"] is (not allow_rewire)
    assert check["evidence_kind"] == "replayed_execution_policy"
    folder = Path(output["directory"]) / "sample_00000"
    history = json.loads((folder / "history.json").read_text())
    history[0]["action"] = "reconnect_branch"
    (folder / "history.json").write_text(json.dumps(history))
    report = audit_delivery(ledger, brief, output)
    assert report["samples"][0]["checks"][0]["status"] == "unverified"
