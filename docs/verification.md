# M102 release verification

Local publication checks completed on **2026-10-12**, Linux / Python 3.12.7.

| Check | Result |
|---|---|
| Complete curated regression suite | **847 passed, 3 skipped**, 245.33 seconds |
| Fresh packaged generation examples | **7/7** accepted |
| Shipped callable models, independently reloaded and solved | **5/5** accepted |
| Compact final evidence | 30 delivery grades, 118 response tasks, 225 target components, 48 independent ratios, 144 static electrical runs verified |
| Offline wheel build and isolated installation | Passed |
| Installed solver/export checks outside checkout | **4/4** passed: balanced distribution, unbalanced distribution, hierarchy, transmission |
| Wheel/source correspondence | All **160** runtime Python/catalog entries byte-identical |
| Minimum-version syntax | Runtime parses with Python 3.11 grammar |
| External LLM calls during these packaging checks | **0** |

Three skipped tests optionally replay exact historical M97 raw artifacts. Those large original directories are not distributed. Active runtime, native-worker, authorization, planning, response, revision and export tests ran. Test fixtures needed by the selected regressions are included locally; they do not depend on the parent research workspace.

The installation check reused existing dependency packages while installing the newly built wheel into an isolated target. It is not a clean-machine network installation test. The tested direct dependencies are listed in [requirements-tested.txt](../requirements-tested.txt).

The gallery was regenerated using the published runtime. Its [manifest](assets/gallery_manifest.json) records source/model/figure hashes. Model reload checks execute copies in temporary directories and compare MATPOWER matrices with saved JSON before electrical evaluation.

The [machine-readable verification summary](verification-summary.json) records the wheel checksum and solver checks. [Research results](results/README.md) are independent recorded numerical evidence, not a new live-provider experiment performed during publication preparation.

## Reproduce

```bash
python -m pip install -e '.[dev,transmission,plots]'
python -m pytest -q
python scripts/release_smoke.py --transmission
python scripts/check_examples.py
python scripts/verify_release_results.py --solvers
python scripts/build_gallery.py
```

The public source and documentation use English. Chinese recognition patterns and original request fixtures use Unicode escapes without discarding bilingual input support. Numerical acceptance conditions and user-permission checks remain active.

No remote CI outcome is claimed before publication. After pushing, inspect [GitHub Actions](https://github.com/ZachariahZhou/GridGen-Agents/actions/workflows/package-check.yml) for the actual Python 3.11/3.12 package and solver jobs.
