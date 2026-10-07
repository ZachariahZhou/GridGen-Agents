# Snapshot scope

The repository is a curated release, not a copy of the full research workspace. M87 identifies the algorithm milestone; `0.1.0` is the package version. The English presentation update does not introduce a new generation algorithm.

## Included

- All 100 runtime Python modules and 18 required JSON catalogs.
- The Streamlit entry point, installation metadata, blank-key environment template and tested direct dependencies.
- Seven executable specifications, 19 selected test files and one shared fixture module.
- Solver/example checks and a gallery regeneration script.
- English documentation, freshly generated PNG/PDF figures, compact provenance/metrics and retained upstream notices.

## Excluded

- Original Git history, `.env`, virtual environments, caches and editor settings.
- Historical experiment batches, model outputs, raw API responses, private memory, databases and checkpoints.
- Raw reference downloads and complete extraction intermediates.
- Historical milestone scripts, most historical tests, paper drafts and progress reports.

## Provenance and changes

The initial release copied the M87 runtime byte-for-byte. This update translates the public interface and presentation strings, updates matching label assertions and regenerates figures from current examples. Original-language source evidence and bilingual input fixtures remain available internally. File-level provenance, differences and hashes are recorded in [SNAPSHOT.json](../SNAPSHOT.json).

Historical tests `test_m69_distribution_matpower.py` and `test_m87_transmission_equipment.py` were renamed to functional names. Shared planning helpers were extracted into `tests/planning_fixtures.py`; numerical assertions were retained.

The figure generator executes all seven specifications in a fresh workspace. Six preselected cases appear in the overview; the second figure shows the urban MV/LV case at three display scopes. All seven cases, including the rural MV/LV example, appear in the metric table. Fixed example results are not estimates of general natural-language success.

## Repository history

The target is [ZachariahZhou/GridGen-Agents](https://github.com/ZachariahZhou/GridGen-Agents), branch `main`. The initial commit `c91c8c200f877b9abbce9998189f4807ef1051bf` and its Apache-2.0 license are preserved. The first curated code release is `47acf3ae39fb7c323db7ab1fac07886f4a17fa0f`.

Third-party terms remain separate, as described in [THIRD_PARTY.md](../THIRD_PARTY.md). Current publication status is recorded by Git history.
