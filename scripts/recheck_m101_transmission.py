"""Bounded recheck of the two historical M97 transmission Q failures.

No live model calls, reseeding, relaxed capabilities or altered requirements.
The supplemental search starts from each historical delivered final artifact;
its result is kept separate from current-code reconstruction of the task.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
from pathlib import Path
import shutil

import numpy as np

from feeder_agents.artifacts import atomic_json, digest, file_digest
from feeder_agents.matpower import parse_matpower
from feeder_agents.solver_accounting import SolverMeter, solver_phase
from feeder_agents.transmission import TransmissionSpec, case_payload, export_case, validate_case


CASE_IDS = ("t04_r0", "t05_r2")
POLICY = dict(max_rounds=4, max_candidate_evaluations=24,
              local_voltage_steps_pu=[.00005, .0001, .0005, .001],
              shunt_steps=[.05, .025], max_witness_generators=2,
              selection="First fully accepted eligible candidate; otherwise minimum deficit among no-new-violation candidates in the round.",
              starting_arm="Historical saved final case, without regeneration.",
              changes="Originally permitted local voltage_setpoint and shunt_step only.")


def read(path):
    return json.loads(Path(path).read_text())


def arrays(payload):
    return {k: np.asarray(v, float) if k in ("bus", "gen", "branch") else v
            for k, v in payload.items()}


def parsed_case(path):
    parsed = parse_matpower(Path(path))
    return dict(version="2", baseMVA=parsed["base_mva"],
                **{k: np.asarray(parsed[k], float) for k in ("bus", "gen", "branch")})


def export_matches(path, case):
    parsed = parsed_case(path)
    return bool(math.isclose(parsed["baseMVA"], case["baseMVA"], rel_tol=0, abs_tol=1e-12)
                and all(parsed[k].shape == np.asarray(case[k]).shape
                        and np.allclose(parsed[k], case[k], rtol=1e-12, atol=1e-12)
                        for k in ("bus", "gen", "branch")))


def protected_checks(base, original, case, meta, spec):
    """Direct array comparisons, independent of production preserved()."""
    bus_cols = [0, 1, 2, 3, 4, 6, 9, 10, 11, 12]
    gen_cols = [0, 3, 4, 6, 7, 8, 9]
    if "redispatch" not in spec.allowed_repairs:
        gen_cols.append(1)
    return dict(
        base_mva=base["baseMVA"] == case["baseMVA"],
        bus_count=len(base["bus"]) == len(case["bus"]),
        generator_count=len(base["gen"]) == len(case["gen"]),
        branch_count=len(base["branch"]) == len(case["branch"]),
        bus_protected_columns=np.array_equal(base["bus"][:, bus_cols], case["bus"][:, bus_cols]),
        generator_protected_columns=np.array_equal(base["gen"][:, gen_cols], case["gen"][:, gen_cols]),
        branch_endpoints_and_status=np.array_equal(base["branch"][:, [0, 1, 10]], case["branch"][:, [0, 1, 10]]),
        positions=original["positions_km"] == meta["positions_km"],
        generator_types=original["generator_types"] == meta["generator_types"],
    )


def physical_metrics(solved, spec):
    """Numerical margins and per-bus P/Q residuals from solved matrices."""
    bus, gen, branch = (solved[k] for k in ("bus", "gen", "branch"))
    finite = all(np.isfinite(v).all() for v in (bus, gen, branch))
    if not finite or branch.shape[1] < 17:
        return dict(finite=False, independently_within_limits=False)
    index = {int(r[0]): i for i, r in enumerate(bus)}
    residual = -(bus[:, 2] + 1j * bus[:, 3]) - (bus[:, 4] - 1j * bus[:, 5]) * bus[:, 7] ** 2
    for row in gen:
        if row[7] > 0:
            residual[index[int(row[0])]] += row[1] + 1j * row[2]
    for row in branch:
        if row[10] > 0:
            residual[index[int(row[0])]] -= row[13] + 1j * row[14]
            residual[index[int(row[1])]] -= row[15] + 1j * row[16]
    active = gen[:, 7] > 0
    qmargin = np.minimum(gen[:, 2] - gen[:, 4], gen[:, 3] - gen[:, 2])
    pmargin = np.minimum(gen[:, 1] - gen[:, 9], gen[:, 8] - gen[:, 1])
    loading = np.maximum(np.hypot(branch[:, 13], branch[:, 14]),
                         np.hypot(branch[:, 15], branch[:, 16])) / np.maximum(branch[:, 5], 1e-12)
    qbad = np.flatnonzero(active & (qmargin < -1e-5))
    q_witnesses = [dict(gen_index=int(i), bus=int(gen[i, 0]), q_mvar=float(gen[i, 2]),
                        qmin_mvar=float(gen[i, 4]), qmax_mvar=float(gen[i, 3]),
                        excess_mvar=float(-qmargin[i])) for i in qbad]
    p_residual = float(np.abs(residual.real).max())
    q_residual = float(np.abs(residual.imag).max())
    return dict(
        finite=True, min_voltage_pu=float(bus[:, 7].min()), max_voltage_pu=float(bus[:, 7].max()),
        voltage_lower_margin_pu=float(bus[:, 7].min() - spec.voltage_min_pu),
        voltage_upper_margin_pu=float(spec.voltage_max_pu - bus[:, 7].max()),
        minimum_p_margin_mw=float(pmargin[active].min()), minimum_q_margin_mvar=float(qmargin[active].min()),
        max_p_violation_mw=float(np.maximum(-pmargin[active], 0).max()),
        max_q_violation_mvar=float(np.maximum(-qmargin[active], 0).max()),
        q_violation_count=len(q_witnesses), q_witnesses=q_witnesses,
        max_branch_loading=float(loading.max()), thermal_margin_ratio=float(1 - loading.max()),
        max_bus_p_residual_mw=p_residual, max_bus_q_residual_mvar=q_residual,
        independently_within_limits=bool(
            np.all(qmargin[active] >= -1e-5) and np.all(pmargin[active] >= -1e-5)
            and np.all(bus[:, 7] >= spec.voltage_min_pu - 1e-5)
            and np.all(bus[:, 7] <= spec.voltage_max_pu + 1e-5)
            and np.all(loading <= 1 + 1e-5) and np.all(branch[:, 5] > 0)
            and p_residual <= 1e-5 and q_residual <= 1e-5),
    )


def candidate_actions(case, meta, spec, assessment):
    seen = set()
    actions = []
    for witness in assessment["witnesses"]:
        key = witness["key"]
        if not key.startswith("gen") or not key.endswith((":qmin", ":qmax")):
            continue
        index = int(key.split(":")[0][3:])
        if index in seen:
            continue
        seen.add(index)
        direction = 1 if key.endswith(":qmin") else -1
        for step in POLICY["local_voltage_steps_pu"]:
            actions.append(dict(kind="voltage_setpoint", index=index,
                                value=round(float(case["gen"][index, 5]) + direction * step, 7)))
        if len(seen) >= POLICY["max_witness_generators"]:
            break
    absorption = sum(w["deficit"] for w in assessment["witnesses"] if w["key"].endswith(":qmin"))
    supply = sum(w["deficit"] for w in assessment["witnesses"] if w["key"].endswith(":qmax"))
    direction = 1 if absorption >= supply else -1
    for step in POLICY["shunt_steps"]:
        value = round(float(meta["shunt_fraction"]) + direction * step, 7)
        if 0 <= value <= 1:
            actions.append(dict(kind="shunt_step", value=value))
    return [a for a in actions if a["kind"] in spec.allowed_repairs]


def save_candidate(path, case, meta, checked, solved):
    atomic_json(path / "case.json", case_payload(case))
    atomic_json(path / "metadata.json", meta)
    atomic_json(path / "assessment.json", checked)
    atomic_json(path / "solution.json", case_payload(solved))


def repair_one(task, historical, destination):
    from feeder_agents.transmission_feedback import assessment, acceptable, apply
    cid = task["id"]
    out = destination / cid
    if (out / "result.json").exists():
        return read(out / "result.json")
    old = historical / "cases" / cid / "transmission_experiments/construction/sample_00000"
    base = arrays(read(old / "case.json")); original = read(old / "metadata.json")
    initial = arrays(read(old / "feedback/candidate_0000/case.json"))
    initial_meta = read(old / "feedback/candidate_0000/metadata.json")
    case, meta = copy.deepcopy(base), copy.deepcopy(original)
    spec = TransmissionSpec(**task["spec"])
    trace = []; evaluations = 0; selected = "baseline"; seen = set()
    with SolverMeter() as meter:
        with solver_phase("saved_final_baseline"):
            before, solved = assessment(case, meta, spec)
        current = before
        save_candidate(out / selected, case, meta, current, solved)
        before_metrics = physical_metrics(solved, spec)
        for round_number in range(1, POLICY["max_rounds"] + 1):
            if current["checked"]["accepted"] or evaluations >= POLICY["max_candidate_evaluations"]:
                break
            best = None
            for action in candidate_actions(case, meta, spec, current):
                if evaluations >= POLICY["max_candidate_evaluations"]:
                    break
                parent = selected
                entry = dict(round=round_number, parent=parent, action=action, selected=False)
                try:
                    trial, trial_meta = apply(case, meta, spec, action)
                    fingerprint = digest(dict(case=case_payload(trial), metadata=trial_meta))
                    if fingerprint in seen:
                        entry["rejection"] = "already_evaluated"; trace.append(entry); continue
                    seen.add(fingerprint)
                    protected = protected_checks(base, original, trial, trial_meta, spec)
                    if not all(protected.values()):
                        raise ValueError("Protected fields changed")
                    evaluations += 1
                    candidate = f"candidate_{evaluations:04d}"
                    with solver_phase("bounded_candidate"):
                        after, trial_solved = assessment(trial, trial_meta, spec)
                    save_candidate(out / candidate, trial, trial_meta, after, trial_solved)
                    entry.update(candidate=candidate, eligible=acceptable(current, after),
                                 before=current["score"], after=after["score"],
                                 accepted=after["checked"]["accepted"], protected_checks=protected,
                                 independent_metrics=physical_metrics(trial_solved, spec))
                    trace.append(entry)
                    atomic_json(out / "trace.json", trace)
                    if entry["eligible"] and (best is None or after["score"] < best[0]):
                        best = (after["score"], trial, trial_meta, after, trial_solved, candidate, entry)
                    if entry["eligible"] and entry["accepted"]:
                        break
                except Exception as exc:
                    entry["error"] = f"{type(exc).__name__}: {exc}"
                    trace.append(entry); atomic_json(out / "trace.json", trace)
            if best is None:
                break
            _, case, meta, current, solved, selected, entry = best
            entry["selected"] = True
            atomic_json(out / "trace.json", trace)
        final = out / "delivered"
        atomic_json(final / "case.json", case_payload(case)); atomic_json(final / "metadata.json", meta)
        export_case(case, final / "case_generated.m")
        with solver_phase("fresh_export_audit"):
            checked, fresh = validate_case(parsed_case(final / "case_generated.m"), spec, meta)
        atomic_json(final / "validation.json", checked)
        atomic_json(final / "solution.json", case_payload(fresh))
        direct = physical_metrics(fresh, spec)
        protected = protected_checks(base, original, case, meta, spec)
        historical_protected = protected_checks(initial, initial_meta, case, meta, spec)
        result = dict(id=cid, spec=task["spec"], historical_input=str(old),
                      historical_input_hashes={name: file_digest(old / name) for name in ("case.json", "metadata.json", "case_generated.m")},
                      baseline_accepted=before["checked"]["accepted"], before_metrics=before_metrics,
                      selected_candidate=selected, candidate_evaluations=evaluations,
                      accepted=checked["accepted"], failed_rules=[k for k, v in checked["checks"].items() if not v],
                      export_matches_json=export_matches(final / "case_generated.m", case),
                      independent_metrics=direct, protected_from_saved_final=protected,
                      protected_from_original_initial=historical_protected,
                      trace=trace, committed_actions=[r["action"] for r in trace if r["selected"]],
                      stop_reason="accepted" if checked["accepted"] else "bounded_search_exhausted")
    result["solver_meter"] = dict(meter.snapshot(), events=meter.events)
    result["verified_delivery"] = bool(result["accepted"] and result["export_matches_json"]
                                       and direct["independently_within_limits"]
                                       and all(protected.values()) and all(historical_protected.values()))
    atomic_json(out / "result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="experiments/m101_transmission_recheck")
    parser.add_argument("--historical", default="experiments/m97_construction_v1")
    parser.add_argument("--phase", choices=("all", "rerun", "repair"), default="all")
    args = parser.parse_args()
    root, historical = Path(args.root), Path(args.historical)
    protocol = read(historical / "protocol.json")
    tasks = [t for t in protocol["tasks"] if t["id"] in CASE_IDS]
    if args.phase in ("all", "rerun"):
        from feeder_agents.benchmark.m97_construction import run_task
        for task in tasks:
            row = run_task(task, root / "current_construction")
            print(json.dumps({k: row.get(k) for k in ("id", "final_accepted", "actual_ac_calls")}), flush=True)
    if args.phase in ("all", "repair"):
        frozen = root / "repair_protocol.json"
        if not frozen.exists():
            package = Path(__file__).resolve().parents[1] / "src/feeder_agents"
            shutil.copytree(package, root / "source/feeder_agents", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            shutil.copy2(__file__, root / "source/recheck_m101_transmission.py")
            inventory = {str(p.relative_to(root / "source")): file_digest(p) for p in (root / "source").rglob("*") if p.is_file()}
            atomic_json(frozen, dict(policy=POLICY, tasks=tasks, historical_protocol_hash=protocol["protocol_hash"],
                                     source_inventory=inventory, source_hash=digest(inventory),
                                     independence="Direct export-matrix comparison, protected-column comparison and numerical margins/residuals; shared PYPOWER solver and catalogue checks."))
        elif read(frozen)["policy"] != POLICY:
            raise ValueError("Frozen search policy changed; use a new root")
        rows = [repair_one(t, historical, root / "supplemental_repair") for t in tasks]
        summary = dict(cases=len(rows), supplemental_verified_deliveries=sum(r["verified_delivery"] for r in rows),
                       supplemental_ac_calls=sum(r["solver_meter"]["total_ac_calls"] for r in rows),
                       current_construction=[read(root / "current_construction/cases" / t["id"] / "record.json")
                                             for t in tasks if (root / "current_construction/cases" / t["id"] / "record.json").exists()],
                       results=rows, interpretation="Targeted bounded continuation of two known failures, separate from the historical 70/72 cohort and current reconstruction.")
        atomic_json(root / "summary.json", summary)
        print(json.dumps({k: summary[k] for k in ("cases", "supplemental_verified_deliveries", "supplemental_ac_calls")}), flush=True)


if __name__ == "__main__":
    main()
