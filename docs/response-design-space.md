# M88-response-v3: Expanded design freedoms and multiport electrical-response control

Date: 2026-10-09. This report records the implementation, experimental protocol, complete results and reproduction commands. The package version remains 0.1.0; the local feature milestone is M88-response-v3. No GitHub commit or push was made during this milestone's experiment work.

## 1. What changed

The earlier limitation involved the available response-changing design variables as well as the number of search rounds. Conductor replacements within one catalog can quickly reach the limits of available equipment. Uniform scaling tends to change sensitivity at several buses together, making objectives such as "increase A while preserving B" or "increase A while decreasing B" difficult. Additional rounds do not automatically create these missing design freedoms.

This extension separates **conditions that the research request requires the model to preserve** from **design variables the user permits it to change**. Exact bus counts, total load, PV injections, phases and voltage levels remain fixed. Explicit permissions can allow changes to equipment, line geometry, local connections or load distribution. Response targets, probe ports and the initial model's reference values remain fixed throughout.

Conductor choices still use complete catalog parameters. Geometry changes update both coordinates and actual line lengths, and every committed candidate must pass complete electrical checks. Changing a plotting layout alone does not alter electrical response.

## 2. Implemented actions and numerical bounds

All new actions are disabled by default. A structured plan must supply both the action name and its numerical bounds. The natural-language entry point also checks that permissions and values are grounded in the request and returns a clarification state when they are unclear.

| Action | Design effect | Bounds and applicability |
|---|---|---|
| `replace_conductor` | Change complete equipment parameter combinations along a path | Compatible phase count, construction type and source scope; single-segment and coordinated replacements; no arbitrary independent R/X changes |
| `scale_layout` | Change the network's overall electrical scale | The software limit on proportional change relative to the original geometry increases from 25% to 90%; original segment-length ranges still apply; source position fixed; generated distribution coordinates only, excluding explicit coordinates |
| `scale_subtree` | Selectively change a downstream region | Both a line-length change limit, at most 90%, and a displacement limit, at most 10 km, are required; currently `spatial_mst` only; scaling about the parent bus preserves supply connectivity and energized-branch directions |
| `rewire_branch` | Change local supply paths and shared impedance | Limits on changed branches, at most 20; new/original length ratio, at most 3; and bus degree, at most 8; currently `spatial_mst` without normally-open ties; coordinates fixed, with radial connectivity and phase continuity preserved |
| `redistribute_load` | Change local operating points and voltage response | Network-wide moved-load fraction, at most 50%, and per-load relative change, at most 90%, must be specified; only heterogeneous `spatial_mst` without prescribed special load shapes; preserves total P/Q, PV at each bus, each load's power factor and its phase-allocation proportions |
| `parallel_line` | Change transmission branch transfer responses | Retains the existing complete-circuit physical model and maximum of four circuits; this milestone adds no new transmission parameter-action types |

These maxima are **software configuration limits, not engineering standards or amounts every case can actually use**. Segment-length ranges, displacement, phases, equipment ratings, voltages and other checks can further restrict feasible changes. The examples permit 75% spatial change rather than using the 90% maximum by default.

When both global and local scaling are enabled, the final length-change envelope for lines that have not been reconnected uses the larger of the two permissions relative to original lengths. The allowances cannot be multiplied to accumulate additional change. Bus displacement and original segment-length ranges also apply. The two actions share a final geometry budget rather than independent cumulative budgets. Endpoints, actual lengths and budgets of normally-open ties remain checked, but their original direction is not enforced: a tie across subtrees may rotate during legal scaling. Reconnected lines use their own new/original length-ratio budget.

Moved load is measured by the final model's distance from the original model, rather than the sum of transfers across rounds:

\[
\eta_P=\frac{\sum_i |P_i-P_i^0|}{2\sum_i P_i^0}.
\]

Moving the same load repeatedly between buses therefore does not expand the permitted final load-distribution range. Each load also remains within its change limit relative to its own original value. If loads have different power factors, total Q conservation must pass as well as total P conservation.

Search can be configured up to 32 rounds and 64 AC-prevalidated candidates per round; defaults remain four rounds and eight candidates. Joint previews can combine at most two basic action types per candidate. A coordinated replacement across multiple conductor segments counts as one basic action.

## 3. How the feedback loop uses these freedoms

1. **Parse and confirm the executable design space.** Identify measurements, target intervals, protected conditions and permitted actions; check exact bus counts and conflicts between permissions and targets.
2. **Generate the original model and freeze the reference.** Fix injection/observation ports and original-response denominators. Later reconnection does not automatically move probe buses.
3. **Propose local candidates in the direction of the mismatch.** Path R/X values guide proposals and ranking only. Original lengths, displacement limits and connectivity constraints determine bounded steps. For tight displacement limits, the allowed step interval is computed before proposing actions.
4. **Propose joint and compensating candidates.** Two changes are applied together to a copy before a full solve. Satisfied ports can generate compensating actions, but only jointly with a primary action for another port; they cannot independently invalidate a satisfied target.
5. **Perform numerical acceptance before selection.** Check original requirements, electrical results and each target deficit. No individual deficit may increase, and total deficit must strictly improve. The LLM selects only among verified actions; deterministic selection can execute the same numerical loop.
6. **Commit or stop, then verify.** After target attainment, independent h/2 measurements verify the result without participating in selection. Unsuccessful runs retain their models, diagnoses, candidates and rejections. Bounded-search failure is not an infeasibility proof.

The new `proposals/round_XX.json` records prescreened actions and rejection reasons. `history.json` records AC candidates, selections and commits. `diagnosis.json` reports permissions and actual changes. All are included in the model package and integrity checks.

Joint search currently combines at most two basic actions and does not explore arbitrary sequences that temporarily worsen the objective. Regression tests verify compensating proposals. The successful cases below do not all depend on that mechanism, so their results cannot independently establish each component's contribution.

## 4. Paired experimental design

Question: **For identical networks, targets and prevalidation budgets, can an expanded design space achieve electrical responses that the restricted configuration did not reach?**

- Fixed urban `spatial_mst` cases: 25 buses, 10 kV and 360 kW, with unbalanced three-phase loads. All buses have three phases; single-phase and two-phase branch counts are zero. PV is zero in this batch.
- Seeds are 41, 42 and 43. Failed samples are retained, and seeds are not replaced based on results.
- Total active-power perturbation h=0.01 MW is divided equally among three-phase ports. The observation is the mean absolute sensitivity of the port's own three-phase voltages. Final verification uses h/2 and a 3% relative response-consistency tolerance.
- Single-port tasks select the farthest load bus. Two-port tasks select A/B by separation of their source paths before power-flow measurement; the rule and frozen ports are archived. The batch therefore supports evidence for relatively separated paths, not arbitrary strongly coupled ports.
- Both configurations use at most four rounds and 16 AC-prevalidated candidates per round, with deterministic selection to isolate the effect of action space.

| Task | Response targets relative to original reference | Restricted configuration | Expanded configuration |
|---|---|---|---|
| Larger response change | A: 1.30–1.50 | Catalog replacement + uniform ±15% scaling | Catalog replacement + uniform ±75% scaling |
| Spatial selectivity | A: 1.15–1.35; B: 0.99–1.01 | Same restricted permissions, single-action candidates | Also permit local subtree ±75% scaling, displacement ≤0.75 km and two-action joint candidates |
| Opposite-direction control | A: 1.15–1.35; B: 0.65–0.85 | Same restricted permissions, single-action candidates | Same expanded permissions as spatial selectivity |

There are 3 tasks × 3 seeds × 2 configurations = 18 design runs. All nine pairs of original `feeder.json` files pass byte-level hash equality checks. Targets and perturbation protocols are identical across groups, but **permitted modification scopes differ**. This experiment evaluates design-space effects; it does not compare algorithms under identical permissions or test LLM superiority over non-LLM selection.

## 5. Complete results

| Task | Seed | Restricted A | Restricted B | Expanded A | Expanded B | Expanded accepted steps |
|---|---:|---:|---:|---:|---:|---:|
| Larger change | 41 | 1.201476 | — | 1.329374 | — | 1 |
| Larger change | 42 | 1.183962 | — | 1.335051 | — | 1 |
| Larger change | 43 | 1.186264 | — | 1.334575 | — | 1 |
| Selectivity | 41 | 1.051821 | 1.000001 | 1.189326 | 1.000000 | 1 |
| Selectivity | 42 | 1.073705 | 1.000000 | 1.193375 | 1.000000 | 1 |
| Selectivity | 43 | 1.045911 | 1.009515 | 1.207459 | 0.999998 | 1 |
| Opposite directions | 41 | 1.051822 | 0.792635 | 1.172125 | 0.679116 | 2 |
| Opposite directions | 42 | 1.073705 | 0.789240 | 1.182203 | 0.832581 | 2 |
| Opposite directions | 43 | 1.000000 | 0.881013 | 1.181635 | 0.786219 | 2 |

**The expanded configuration passed all targets, reference electrical checks, requirement-preservation checks and final half-step verification in 9/9 cases. The restricted configuration met all targets in 0/9 cases.** Across both groups, 18/18 passed reference electrical and protected-requirement checks, and 18/18 passed half-step numerical consistency. The restricted group's `verification_passed=false` reflects the additional requirement for target attainment; it does not mean nine power-flow failures or inconsistent numerical derivatives.

Larger-change tasks used 34% uniform enlargement, below their 75% allowance. The responses come from AC solutions and are not exact multiples of the geometric scale. Selectivity tasks used local subtree enlargement: A increased by approximately 18.9%–20.7%, while B's absolute percentage change was no more than 0.00022%. Opposite-direction tasks used joint actions followed by further adjustments: A increased by approximately 17.2%–18.2%, while B decreased by approximately 16.7%–32.1%.

The restricted group performed 224 candidate previews and the expanded group 182, excluding original references. Search stops when targets are met or no feasible improvement remains; it need not exhaust the budget. Reduced overall runtime is not a central conclusion of this experiment.

[Per-case CSV](assets/response_flexibility_results.csv) · [Complete result summaries](assets/response_flexibility_results.json) · [Frozen input plans](assets/response_flexibility_suite.json) · [Vector PDF](assets/response_flexibility.pdf)

![Paired electrical response control](assets/response_flexibility.png)

## 6. Live end-to-end LLM check

The actual `.env` model, **qwen3.7-plus**, received an urban feeder request for 25 buses, 10 kV and 360 kW: A=b1 must reach 1.15–1.35 times its original response, while B=b9 must remain within 0.99–1.01. Only catalog replacement and bounded subtree scaling were permitted; global scaling was prohibited. The request specified four rounds, 16 candidates, two-action combinations and LLM selection.

The model compiled the plan correctly and made one candidate selection. The selected joint action replaced the conductor on l2 and enlarged the b1 subtree by 24.7%, with a maximum bus displacement of approximately 0.03879 km. The result was A=**1.285286** and B=**1.000000332**. Total load remained 360 kW; response targets, power flow, protected conditions and final verification all passed.

This single record verifies integration of live natural-language compilation, permission checks, LLM candidate selection and the numerical feedback loop. It does not estimate accuracy on arbitrary natural-language requests. The request, plan, selection rationale and result summary are in the [LLM check record](assets/response_flexibility_live.json); complete raw calls remain in the local directory `workspace/response_language_v3_check`.

## 7. Regression checks, reproduction and version evidence

The final curated suite passed **201 tests in 101.04 seconds**, including 42 response tests. New checks cover budgets relative to the original model, total P/Q and individual PV preservation, phase protection, radiality and actual power flow after reconnection, legal geometry changes of normally-open ties, small steps under tight displacement limits, joint compensation and denied permissions in natural language. Identified problems were first reproduced with failing tests and then fixed.

Review addressed four problem classes:

1. Candidate steps were too large under tight displacement limits. Scaling intervals are now computed from displacement-ball constraints.
2. Satisfied ports generated no compensating candidates. Reverse compensation is now permitted only within joint previews.
3. Length/displacement limits alone did not exclude arbitrary coordinate distortion. Directions of energized, unreconnected branches are now checked; an erroneous application of this constraint to normally-open ties was corrected.
4. Positive-authorization substring matching misread denials in English and Chinese. Denial patterns now include the Chinese equivalent of "not permitted," represented in public source as `\u4e0d\u5141\u8bb8`, and "Do not allow" and "Don't/Don’t allow." Language checks use conservative rules; unfamiliar wording may require clarification. They are not a general semantic proof procedure.

All 18 paired experiments record source hash `4434d8436b17b36fcd7de250489460c6c9ad6553467062583987fe7253961b6b`. After the batch and live LLM check, final changes added the normally-open-tie direction exception, recognition of contracted English denials and a compiler-version label. They do not affect this batch's tie-free tasks with affirmative permissions. Original runtime hashes are retained rather than replaced with the final source hash. The full 201-test suite ran against the final source. Artifact integrity was reread and verified for all 18 results and the LLM result.

Run from the curated release directory:

```bash
# Deterministic, executable selective-control example; no API key required.
feeder-agents response-design --plan examples/response_selective_25.yaml --id selective_demo

# Example of separately bounded geometry, reconnection and load permissions.
feeder-agents response-design --plan examples/response_flexible_25.yaml --id flexible_demo

# Replay the paired protocol into a NEW directory, preserving prior evidence.
python scripts/run_response_flexibility.py --output workspace/response_flexibility_replay
python scripts/plot_response_flexibility.py \
  --input workspace/response_flexibility_replay/results.json --output workspace/response_flexibility_replay/figures
```

Both new examples were executed through the CLI on the final source and passed complete result-hash verification. Both produced A=1.193375 and B=0.999999756. The example granting all permissions still selected local geometry; it does not establish additional benefit from branch reconnection or load redistribution for this task. [Example check records](assets/response_flexibility_examples.json) are separate from the 18 paired experiments.

Historical prototypes and intermediate results remain in their respective directories. This report's batch data come only from `workspace/response_flexibility_final_20261009`. Models and complete solver records remain in the local ignored workspace. The curated Git directory contains scripts, examples, tests and compact evidence. Each task's `selected/opendss/Master.dss` is directly usable.

## 8. Supported research claims

The method can be stated as: **synthesize research grid models with specified local electrical responses through physical feedback within explicit design freedoms.** This extension demonstrates larger response changes, spatial selectivity and opposite-direction multiport control in addition to generation of convergent models.

The models can support comparisons of voltage-control/reactive-support algorithms across networks with different sensitivities, or paired cases with different static PV-injection impacts. Transmission continues to support branch-response control for specified power transfers. These applications share d|V|/dP, d|V|/dQ and dP_branch/dP_transfer measurements; multiple downstream optimization algorithms have not been implemented.

The results do not establish that all targets are realizable, arbitrary response matrices can be matched, equipment distributions are more realistic than a baseline, or multi-voltage hierarchy responses can be controlled. Load redistribution and branch reconnection have functional and physical-acceptance evidence, but have not received the batch target-attainment comparisons performed for spatial changes. Three urban seeds cannot substitute for rural, large-scale or strongly coupled-port studies. The original 18-task historical experiment retains its reporting scope; this new 9/9 result does not overwrite historical failures.
