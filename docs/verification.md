# Release verification

Latest local check: **M89-task-v1, 238/238 tests passed on 2026-10-09**, rerun after the English publication update in 104.87 seconds. The rebuilt wheel passed all four installed solver checks. The preceding publication check executed seven base generation examples and validated eight additional response/task specifications. Earlier dated sections retain their original scope.

Date: 2026-10-07. Platform: Linux / Python 3.12.7. This record covers release packaging and the English presentation update. No external LLM was called, and the full paper study was not repeated.

## Initial English presentation update (M87)

| Check | Result |
|---|---|
| Full curated regression suite | **159/159 passed**, 104.24 seconds |
| Fresh packaged examples | **7/7 accepted** by their implemented checks |
| Distribution cases | 25/37 buses, using OpenDSS |
| Urban and rural MV/LV cases | 40 buses each, using OpenDSS |
| Transmission cases | 37/139/237 buses, using PYPOWER AC validation |
| Current wheel build | Passed; 100 runtime Python modules and 18 JSON catalogs |
| Wheel runtime contents | Byte-identical to the updated release source |
| Fresh HTML views and generated reports | Seven of each; English presentation text |
| Gallery provenance | Source, specification, model, evidence and figure hashes recorded and checked |

The overview uses six cases selected before generation. The rural MV/LV case is also executed and included in the metric table. The hierarchy detail reuses the newly generated urban model. No historical network or replacement seed is substituted. PNG and vector PDF outputs share the same source cases.

The default UI controls, report templates, figure labels and public Markdown documentation use English. In the current publication tree, the five research reports are also translated into English, while original-language documents remain in the parent research workspace. Bilingual interpretation patterns and fixtures use Unicode escapes to preserve their decoded text. User-provided input retains its supplied language at runtime. Test changes align widget and diagram-label assertions with the English interface; numerical acceptance assertions remain in place.

The current gallery is a deterministic example demonstration. It is not a new measurement of natural-language end-to-end success or real-grid population fidelity. Results, hashes and reproduction instructions are in [the gallery manifest](assets/gallery_manifest.json), [metric table](assets/gallery_metrics.csv) and [usage guide](usage.md).

## Initial release checks

The initial curated release was copied outside the research workspace, built offline with the setuptools PEP 517 backend and installed using `pip --no-deps --no-index --target`. CLI and solver checks ran outside the source directory, with the import location verified. Installed dependencies were reused; this was not a clean-machine network-installation test.

Four solver smoke cases passed: balanced distribution (9 buses), unbalanced distribution (9 buses), MV/LV distribution (23 buses), and transmission (9 buses, including MATPOWER reload). All seven example specifications also passed.

During initial test extraction, missing historical helper imports caused three failures. Helpers were isolated in `tests/planning_fixtures.py`; 48 affected tests passed on targeted rerun. The complete curated suite subsequently passed 159/159 in 112.04 seconds before the first code commit. The English update was independently rerun in full, as recorded above.

## Packaging and evidence

Wheel builds use the project's virtual-environment interpreter and installed setuptools backend. CI explicitly installs the build frontend. Tested direct dependencies are listed in [requirements-tested.txt](../requirements-tested.txt).

The original M87 generation algorithms and numerical acceptance rules are retained. Presentation modules now differ from the initial byte-for-byte snapshot; [SNAPSHOT.json](../SNAPSHOT.json) records these differences rather than claiming the complete runtime is unchanged.

Generated cases, memory, raw solver logs and private configuration stay outside tracked release files. The gallery script saves its local cases under the ignored `workspace/gallery/` directory. Shareable summaries are in [verification-summary.json](verification-summary.json). The Apache-2.0 license remains unchanged; third-party terms are documented separately in [THIRD_PARTY.md](../THIRD_PARTY.md).

GitHub CI status is reported by [Actions](https://github.com/ZachariahZhou/GridGen-Agents/actions/workflows/package-check.yml), separately from these local checks.


## M88 response extension (2026-10-08)

The complete curated release suite passed **173 tests** in 96.46 seconds. Fourteen added tests exercise real solver measurements, target preservation, exact counts, language compilation failures, UI history, integrity checks, and DC action ranking. The response pilot contains 18 initial tasks and six separately recorded failed-case retests; see [the experiment record](response-experiments.md). A live call using the actual `.env` model, `qwen3.7-plus`, compiled one 37-bus task and selected two verified edits. This single example is not a natural-language success-rate estimate.

## M88 response v2 (2026-10-09)

The complete release suite passed **179 tests** in 102.93 seconds, including 20 response tests. New checks cover reactive-power voltage differences, catalogue exhaustion, bounded spatial permissions, fixed-coordinate rejection, original-baseline scale limits, forbidden load/geometry changes and unauthorized LLM permissions. Only the remaining three urban experiments were replayed. Catalogue-only coordinated changes remained below the requested response interval; a separately recorded experiment explicitly permitting 15% spatial scaling passed all three targets and half-step checks. Original baseline files are byte-identical across these comparisons. The live `.env` model `qwen3.7-plus` also completed one urban request and selected one verified spatial action. See [failure analysis](response-failure-analysis.md); this is not a same-contract 18/18 success claim.

## M88 response v3 (2026-10-09)

The final curated suite passed **201 tests in 101.04 seconds**, including 42 response tests. New regressions cover original-reference geometry and load budgets, physical reconnection and redistribution, joint moves, compensating proposals, tight displacement limits, normally-open tie geometry and denied language permissions. All seven added final regression cases failed before the corresponding fixes and passed afterward.

Eighteen paired response tasks used three fixed seeds, three target patterns and restricted/expanded permissions, with equal round and candidate-preview limits. Expanded permissions passed 9/9 complete response tasks; restricted permissions passed 0/9. All 18 selected base models passed electrical and protected-contract checks, and all 18 passed half-step numerical consistency. The restricted group's final verification flag remains false because target attainment is also required. Nine paired baseline hashes match. A separate live `qwen3.7-plus` compilation and selection passed selective A/B control in one accepted step. This is a design-space experiment plus one language integration check, not a general success-rate estimate.

All saved results were read through artifact-integrity verification. Batch source hashes remain as recorded: the final tie-direction and English-denial fixes, plus a compiler-version label, followed the batch and live run and do not affect those tie-free affirmative-input cases. The full suite above tests the final source. No historical batches or wheel-installation checks were repeated. [Complete record](response-design-space.md).

## M89 research tasks (2026-10-09)

The curated release suite passed **237 tests in 102.33 seconds**, including **36 task tests**. A subsequent prompt-only field-placement example was checked by all 36 task tests (4.78 seconds) and a successful real Qwen call. Added checks cover domain compatibility, real-PV port selection, fixed interventions, downstream failure gates, exact counts, negations, source units, one-sided response bounds, audited correction calls, CLI exports, corrupted UI history and immutable run identities. Six newly added checks failed before the final source-bound/correction fixes and passed afterward.

Nine structured cases (three tasks × seeds 41–43) passed target attainment, protected contracts, electrical base checks, finite task experiments and half-step verification. All original and selected metrics, source/model/result hashes and accepted actions are retained in the [task data](assets/research_tasks_results.json) and [provenance](assets/research_tasks_provenance.json). Independent readers verified all nine artifact sets and the three completed language results.

Three distinct live requests used the actual `.env` model `qwen3.7-plus`. PV and transmission completed on their initial workflow runs; transmission used one internal schema correction. The voltage request first produced an unsourced explicit default interval, then a separately retained retest failed on misplaced schema fields. Source-feedback correction and explicit field-path instructions were added; the final voltage retest completed in one model call. Both failed records remain in the [live ledger](assets/research_tasks_live.json). These results span targeted fixes and are not a same-version first-attempt 100% claim. Candidate selection was deterministic; the LLM compiled the research requests. No historical batches were rerun. [Complete method and experiment report](research-task-workflows.md).

### Publication preparation

The publication check reproduced a CI example-registry failure: the legacy script rejected the new `research_*.yaml` family. The checker now executes its seven original generation examples and separately validates all five response plans and three research-task plans against their strict schemas. Its output explicitly marks these eight protocol specifications as not physically executed by this checker; the task experiments above remain their separate numerical evidence. The failing invocation and corrected invocation were both observed locally. The GitHub workflow file was unchanged.

The final full regression run passed 237 tests in 103.94 seconds. An offline wheel build succeeded; the wheel contains byte-identical runtime modules and catalogs. Installing it into an isolated target and running outside the checkout passed balanced distribution, unbalanced distribution, MV/LV hierarchy and transmission solver/export checks. The seven base examples all passed. No new external LLM calls were made during publication preparation. `.env`, model workspaces and raw API logs are excluded from the release manifest.

### English publication update

The final English tree passed **238 tests in 104.87 seconds**, including a new check rejecting literal CJK text in public source, tests, documentation and configuration. Five research reports, LLM instructions, diagnostics, comments and catalog descriptions use English. Chinese input-recognition patterns and fixtures retain their decoded values through Unicode escapes; tokenizer-based conversion verified exact Python AST equivalence before and after escaping. Two old error-message assertions were updated to the translated English messages after the first language-check run; numerical acceptance assertions were retained.

The offline wheel was rebuilt and installed outside the checkout. All 132 runtime Python/catalog entries match the final release source, and four real solver checks passed: balanced distribution, unbalanced distribution, MV/LV hierarchy and transmission, including MATPOWER reload. The wheel SHA-256 is `4d19b842a98ca9979831e584f07db14ab902ad535de257f6da05a2eed405b6dc`.

Original-language reference metadata is retained in escaped provenance fields. The two live experiment ledgers changed only their Unicode serialization: decoded requests, outputs and outcomes remain identical. Numerical rules and experiment results were not changed. Historical source hashes describe their recorded experiments; the current publication file hashes are in [SNAPSHOT.json](../SNAPSHOT.json). This update made no external LLM calls and did not rerun the historical batches, so it does not establish a new success rate for the translated prompts.
