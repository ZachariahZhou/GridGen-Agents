# Release verification

Date: 2026-10-07. Platform: Linux / Python 3.12.7. This record covers release packaging and the English presentation update. No external LLM was called, and the full paper study was not repeated.

## Current presentation update

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

The default UI controls, report templates, figure labels and public Markdown documentation use English. Original source evidence, user-provided text and bilingual interpretation fixtures retain their original language. Test changes align widget and diagram-label assertions with the English interface; numerical acceptance assertions remain in place.

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
