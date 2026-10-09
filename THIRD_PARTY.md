# Third-party sources and distribution terms

Original GridGen-Agents code uses the root [Apache License 2.0](LICENSE), retained from the initial GitHub commit. That license does not relicense third-party code, reference cases or derived equipment data. Public availability does not establish unrestricted redistribution rights.

## Packaged data

| Files | Source and purpose | Retained evidence |
|---|---|---|
| `matpower_references.json`, `taxonomy_references.json` | MATPOWER reference cases and statistics | Case provenance, commits and source text; [MATPOWER license](third_party/MATPOWER_LICENSE.txt) |
| `calibration_epri_dpv_j1_k1.json`, `equipment_epri.json`, `equipment_joint.json` | EPRI DPV feeder statistics and equipment observations from GRIDAPPSD/Powergrid-Models | Provenance, source and permission fields, hashes and extraction evidence |
| `equipment_epri_large.json` | EPRI test-feeder equipment records from tshort/OpenDSS | Repository, commit, source digest and source text |
| `lv_conductors.json`, `lv_underground.json` and related catalogs | Public equipment documents and project mappings | Per-record references and units |
| `sources.json`, `rules.json`, `design_requirements.json` and related catalogs | Clause summaries, source metadata and research constraints | Distinction between source clauses, metadata and modeling assumptions |

**MATPOWER explicitly distinguishes its software license from case-data licenses.** Including its BSD notice does not establish that every reference case has BSD permission. The existing permission field in `equipment_epri.json` describes research/study conditions and is not a blanket open-source license for the entire derived catalog. Applicable distribution conditions must be checked against the specific materials.

Only runtime catalogs are included. Raw downloads, complete reports and extraction intermediates are excluded. A record's `source_path` identifies provenance; it is not a dependency on the original research machine.

Public explanatory text is presented in English. Unicode escapes in retained source identifiers or evidence preserve the original decoded values; they do not alter attribution or third-party distribution terms.

## Python dependencies

LangChain, LangGraph, OpenDSSDirect.py, NetworkX, Pydantic, Streamlit, PYPOWER, SciPy and Matplotlib are installed through the package manager. Their code and virtual environments are not vendored here. Dependency ranges are listed in `pyproject.toml`; each project retains its own terms.

## Figures

Figures in `docs/assets/` are rendered from newly generated cases produced by packaged specifications. The gallery manifest records source, model, validation and image hashes. These are synthetic model diagrams, not third-party maps or new natural-language success-rate measurements.
