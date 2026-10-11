"""Regressions for source-preserving ordinary planning corrections."""
import copy
import json

import pytest

from feeder_agents.adaptive_planning import AdaptiveProposal, _compile, adaptive_plan
from feeder_agents.requirement_contract import audit_delivery, audit_ledger, compare
from feeder_agents.requirements import RequirementLedger, check_ledger_plan
from feeder_agents.transmission import TransmissionSpec


@pytest.mark.parametrize(
    "actual, expected, operator, satisfied",
    [
        ([], [], "eq", True),
        ([], ["replace_conductor"], "eq", False),
        (["replace_conductor"], [], "eq", False),
        ([], [], "contains", True),
        ([], ["replace_conductor"], "contains", False),
        (["replace_conductor"], [], "contains", True),
        ([], [], "excludes", True),
        ([], ["replace_conductor"], "excludes", True),
        (["replace_conductor"], [], "excludes", True),
        (["replace_conductor"], ["replace_conductor"], "excludes", False),
        (None, [], "eq", False),
        (None, [], "excludes", False),
        ([], None, "excludes", False),
        ([], [], "ge", False),
    ],
)
def test_empty_repair_sets_obey_equality_and_membership(actual, expected, operator, satisfied):
    assert compare(actual, expected, operator) is satisfied


def _requirement(identifier, quote, field, value, segment_ids=None):
    return dict(id=identifier, segment_ids=segment_ids or [0], evidence=quote,
                meaning=quote, priority="hard", disposition="supported",
                target_field=field, expected_value=value)


def test_explicit_no_transmission_repairs_passes_plan_and_observation_audit():
    ledger = RequirementLedger(
        network_kind="transmission", model_family="transmission", summary="No automatic repairs",
        requirements=[_requirement("repairs", "Disable automatic repairs", "transmission.allowed_repairs", [])],
    )
    brief = dict(status="ready", plan_type="transmission",
                 plan=dict(spec=TransmissionSpec(allowed_repairs=[]).model_dump()))
    check_ledger_plan(ledger, brief)
    assert brief["requirement_acceptance"]["all_satisfied"]
    assert audit_ledger(ledger, {"allowed_repairs": []})["all_satisfied"]
    assert not audit_ledger(ledger, {})["all_satisfied"]
    assert not audit_ledger(ledger, {"allowed_repairs": ["replace_conductor"]})["all_satisfied"]


class OfflinePlanner:
    """Replay only structured model responses; keep all controller checks real."""
    model_name = "offline-m98-contract"

    def __init__(self, proposals):
        self.proposals = iter(proposals)

    def with_structured_output(self, schema, **kwargs):
        parent = self

        class Bound:
            def invoke(self, messages):
                if schema.__name__ == "FailureDiagnosis":
                    context = json.loads(messages[-1][1])
                    return dict(action_id=context["allowed_actions"][0]["id"],
                                evidence_ids=[fact["id"] for fact in context["facts"]],
                                reason="Correct the recorded source omission within its offered scope.",
                                expected_change="Preserve existing constraints and add the missing exact quote.")
                if schema.__name__ == "SemanticReview":
                    return dict(approved=True, issues=[])
                return copy.deepcopy(next(parent.proposals))

        return Bound()


def _missing_clause_proposals():
    request = "Generate exactly 23 total buses including the source; Total bus count must be 23"
    first = dict(status="ready", family="single_voltage", single_voltage_spec=dict(n_buses=23),
                 ledger=dict(network_kind="distribution", model_family="single_voltage", summary=request,
                             requirements=[_requirement("count", "exactly 23 total buses including the source",
                                                        "n_buses", 23)]))
    corrected = copy.deepcopy(first)
    corrected["ledger"]["requirements"][0]["segment_ids"] = [0, 1]
    corrected["ledger"]["requirements"].append(
        _requirement("repeated_count", "Total bus count must be 23", "n_buses", 23, [1]))
    return request, first, corrected


def test_complete_ledger_derives_segment_ids_before_comparing_scope(tmp_path):
    request, first, corrected = _missing_clause_proposals()
    result = adaptive_plan(request, tmp_path, OfflinePlanner([first, corrected]))
    assert result["status"] == "ready"
    assert result["plan"]["spec"]["n_buses"] == 23
    entries = result["requirement_ledger"]["requirements"]
    assert [(entry["id"], entry["segment_ids"], entry["expected_value"]) for entry in entries] == [
        ("count", [0], 23), ("repeated_count", [1], 23),
    ]
    analysis = json.loads((tmp_path / "failure_analysis" / "attempt_01.json").read_text())
    assert analysis["decision"]["action_id"] == "complete_ledger"
    assert analysis["verification"]["status"] == "passed"


@pytest.mark.parametrize("mutation", ["value", "priority", "evidence", "inferred_permission"])
def test_ledger_completion_still_rejects_changed_source_contract(tmp_path, mutation):
    request, first, corrected = _missing_clause_proposals()
    entry = corrected["ledger"]["requirements"][0]
    if mutation == "value":
        entry["expected_value"] = 24
        corrected["single_voltage_spec"]["n_buses"] = 24
    elif mutation == "priority":
        entry["priority"] = "preference"
    elif mutation == "evidence":
        entry["evidence"] = "Total bus count must be 23"
    else:
        corrected["ledger"]["requirements"].append(dict(
            id="defaults", evidence="The system may choose unspecified equipment", meaning="Default selection",
            priority="permission", disposition="supported"))
    result = adaptive_plan(request, tmp_path, OfflinePlanner([first, corrected, corrected]))
    assert result["status"] == "planning_failed"


def test_inferred_default_permission_is_not_user_evidence():
    request, proposal, _ = _missing_clause_proposals()
    proposal["ledger"]["requirements"].append(dict(
        id="defaults", evidence="The system may choose unspecified equipment", meaning="Default selection",
        priority="permission", disposition="supported"))
    with pytest.raises(ValueError, match="not an exact request quote"):
        _compile(AdaptiveProposal.model_validate(proposal), request)


@pytest.mark.parametrize("family", ["feeder", "hierarchical", "transmission"])
def test_existing_visualization_export_is_a_planned_deliverable(family):
    from feeder_agents.hierarchy import HierarchicalSpec
    from feeder_agents.schemas import ExperimentSpec

    spec = {"feeder": ExperimentSpec, "hierarchical": HierarchicalSpec,
            "transmission": TransmissionSpec}[family]()
    ledger = RequirementLedger(
        network_kind="transmission" if family == "transmission" else "distribution",
        summary="Export a visualization", requirements=[
            dict(_requirement("view", "Export a visualization", "deliverables", "visualization"),
                 operator="contains")],
    )
    brief = dict(status="ready", plan_type=family, plan=dict(spec=spec.model_dump()))
    check_ledger_plan(ledger, brief)
    assert brief["requirement_acceptance"]["all_satisfied"]


@pytest.mark.parametrize("family", ["feeder", "hierarchical", "transmission"])
def test_visualization_audit_requires_current_renderer_output(tmp_path, family):
    from pathlib import Path
    from feeder_agents.hierarchy import HierarchicalSpec
    from feeder_agents.hierarchy_workflow import run_hierarchy
    from feeder_agents.schemas import ExperimentSpec
    from feeder_agents.transmission import run_transmission
    from feeder_agents.workflow import run_experiment

    if family == "transmission":
        spec = TransmissionSpec(n_buses=9, n_generators=3, total_mw=180, seed=41)
        output = run_transmission(spec, tmp_path, "visualization-audit")
    elif family == "hierarchical":
        spec = HierarchicalSpec(users=12, total_kw=48, seed=41)
        output = run_hierarchy(spec, tmp_path, "visualization-audit")
    else:
        spec = ExperimentSpec(n_buses=9, total_kw_min=48, total_kw_max=48, seed=41)
        output = run_experiment(spec, tmp_path, "visualization-audit")
    assert output["accepted"] == 1
    brief = dict(status="ready", plan_type=family, plan=dict(spec=spec.model_dump()))
    ledger = RequirementLedger(
        network_kind="transmission" if family == "transmission" else "distribution",
        model_family="single_voltage" if family == "feeder" else family, summary="Export visualization",
        requirements=[dict(_requirement("view", "Export visualization", "deliverables", "visualization"),
                           operator="contains")],
    )
    report = audit_delivery(ledger, brief, output)
    assert report["all_satisfied"]
    folder = Path(output["directory"]) / "sample_00000"
    artifact = folder / "visualization.html"
    original = artifact.read_text()
    artifact.write_text("<html><body>Unrelated network visualization</body></html>")
    report = audit_delivery(ledger, brief, output)
    assert report["samples"][0]["checks"][0]["status"] == "failed"
    artifact.unlink()
    report = audit_delivery(ledger, brief, output)
    assert report["samples"][0]["checks"][0]["status"] == "failed"
    artifact.write_text(original)
    assert audit_delivery(ledger, brief, output)["all_satisfied"]


def test_no_feeder_repairs_requires_bound_policy_and_unchanged_execution(tmp_path):
    from pathlib import Path
    from feeder_agents.artifacts import file_digest
    from feeder_agents.schemas import ExperimentSpec
    from feeder_agents.workflow import run_experiment

    spec = ExperimentSpec(n_buses=9, total_kw_min=48, total_kw_max=48,
                          repair_policy=dict(strategy="none"), seed=41)
    output = run_experiment(spec, tmp_path, "no-repairs")
    brief = dict(status="ready", plan_type="feeder", plan=dict(spec=spec.model_dump()))
    ledger = RequirementLedger(network_kind="distribution", model_family="single_voltage",
        summary="Disable automatic repairs", requirements=[_requirement(
            "repairs", "Disable automatic repairs", "repair_policy.strategy", "none")])
    assert audit_delivery(ledger, brief, output)["all_satisfied"]
    folder = Path(output["directory"]) / "sample_00000"
    history_path = folder / "history.json"
    history = json.loads(history_path.read_text())
    history.append(dict(action="rollback", attempt=1))
    history_path.write_text(json.dumps(history))
    # Update the normal integrity markers to isolate semantic history checking.
    # A cryptographically consistent record of an attempted repair is still not
    # evidence that repairs were disabled, even if the final model is unchanged.
    outcome_path = folder / "outcome.json"
    outcome = json.loads(outcome_path.read_text())
    outcome["artifacts"]["history.json"] = file_digest(history_path)
    outcome_path.write_text(json.dumps(outcome))
    (folder / "completion.json").write_text(json.dumps(dict(outcome_sha256=file_digest(outcome_path))))
    report = audit_delivery(ledger, brief, output)
    assert not report["all_satisfied"]
    assert report["samples"][0]["checks"][0]["status"] == "unverified"


def test_zero_repairs_does_not_invent_a_disabled_repair_policy(tmp_path):
    from feeder_agents.schemas import ExperimentSpec
    from feeder_agents.workflow import run_experiment

    spec = ExperimentSpec(n_buses=9, total_kw_min=48, total_kw_max=48,
                          repair_policy=dict(strategy="fixed"), seed=41)
    output = run_experiment(spec, tmp_path, "unused-repairs")
    assert output["samples"][0]["repairs"] == 0
    brief = dict(status="ready", plan_type="feeder", plan=dict(spec=spec.model_dump()))
    ledger = RequirementLedger(network_kind="distribution", model_family="single_voltage",
        summary="Disable automatic repairs", requirements=[_requirement(
            "repairs", "Disable automatic repairs", "repair_policy.strategy", "none")])
    assert not audit_delivery(ledger, brief, output)["all_satisfied"]


@pytest.mark.parametrize("field,quote,family,format_name", [
    ("distribution.deliverables", '\u4ea4\u4ed8MATPOWER\u6587\u4ef6', "transmission", "matpower"),
    ("semantic_capabilities.transmission.deliverables", '\u8f93\u51faMATPOWER\u6a21\u578b', "transmission", "matpower"),
    ("deliverables", '\u8f93\u51faOpenDSS\u5373\u53ef', "hierarchical", "opendss"),
])
def test_source_format_request_accepts_its_companion_visualization(field, quote, family, format_name):
    ledger = RequirementLedger(
        network_kind="transmission" if family == "transmission" else "distribution",
        model_family=family, summary=quote,
        requirements=[_requirement("format", quote, field, [format_name])],
    )
    proposal = AdaptiveProposal(status="ready", family=family, ledger=ledger,
        **({"transmission_spec": dict(n_buses=9)} if family == "transmission"
           else {"distribution_spec": dict(users=12)}))
    brief = _compile(proposal, quote)
    assert brief["requirement_acceptance"]["all_satisfied"]
    assert not audit_ledger(proposal.ledger, {"deliverables": ["visualization"]})["all_satisfied"]


@pytest.mark.parametrize("quote", ['\u53ea\u8f93\u51faOpenDSS', '\u4ec5\u8f93\u51faOpenDSS', "Export only OpenDSS"])
def test_exclusive_source_exports_are_not_relaxed_to_inclusion(quote):
    ledger = RequirementLedger(network_kind="distribution", model_family="hierarchical", summary=quote,
        requirements=[_requirement("format", quote, "deliverables", ["opendss"])])
    proposal = AdaptiveProposal(status="ready", family="hierarchical", ledger=ledger,
                                distribution_spec=dict(users=12))
    with pytest.raises(ValueError, match="Hard requirement changed"):
        _compile(proposal, quote)
    assert proposal.ledger.requirements[0].operator == "eq"
