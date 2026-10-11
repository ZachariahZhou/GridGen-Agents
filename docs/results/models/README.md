# Callable example models

These compact artifacts provide a direct entry into OpenDSS or MATPOWER without an API key. OpenDSS entry points use relative file references. The JSON model/specification and electrical validation are supplied alongside each export.

| Directory | Case | Entry point |
|---|---|---|
| `rural_37` | Three-village, three-phase distribution feeder | `opendss/Master.dss` |
| `urban_hierarchy_40` | Explicit urban MV/LV network with 24 customers | `opendss/Master.dss` |
| `transmission_139` | Meshed 500 kV transmission network | `case_generated.m` |
| `transmission_237` | 500/220 kV transmission network | `case_generated.m` |
| `transmission_223` | 220 kV, 640 MW, 20 generators, automatic repairs disabled | `case_generated.m` |

The first four models come from the current release gallery. The 223-bus export is the accepted artifact identified in the latest language evaluation. Their source and content hashes are recorded in `manifest.json`. No figure layout replaces the electrical connectivity.

From the repository root, execute `python scripts/verify_release_results.py --solvers` to reload these exports, check fidelity and rerun the electrical acceptance checks in temporary directories. For new models, use the YAML examples and CLI commands in the main README.
