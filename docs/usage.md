# Usage and outputs

Install the package following the [README](../README.md), then run commands from the release directory. Examples do not require the original research workspace or raw reference downloads.

## Entry points

`--workspace` is a global option and precedes the subcommand:

```bash
feeder-agents --workspace workspace/demo generate --spec examples/distribution_villages.yaml --id villages
feeder-agents --workspace workspace/demo hierarchy --spec examples/hierarchical_rural.yaml --id rural_lv
feeder-agents --workspace workspace/demo transmission --spec examples/transmission_139.yaml --id tx139
feeder-agents --workspace workspace/demo transmission --spec examples/transmission_237.yaml --id tx237
```

Commands return JSON summaries. A completed execution may still contain unaccepted cases: inspect `accepted`, individual checks and recorded errors.

| Family | Path inside the workspace | Model artifacts |
|---|---|---|
| Single-voltage distribution | `experiments/<id>/sample_00000/` | `feeder.json`, `opendss/Master.dss` |
| MV/LV distribution | `hierarchical_experiments/<id>/sample_00000/` | `feeder.json`, OpenDSS files |
| Transmission | `transmission_experiments/<id>/sample_00000/` | `case.json`, `case_generated.m` |
| Natural-language design | `projects/<project>/designs/<id>/` | Plan and result index; `outcome.directory` locates generated models |

Keep models, validation JSON, HTML views and metadata together. MATPOWER exports represent balanced models; phase-resolved unbalanced distribution uses OpenDSS. Consult each run's artifact and validation records.

## Model configuration

Copy `.env.example` to `.env`. Set `FEEDER_MODEL`, `OPENAI_API_KEY` and `OPENAI_BASE_URL` for an available compatible service. Process environment variables override the file in the launch directory. The template contains no API key.

Create a draft without electrical generation:

```bash
feeder-agents design 'Generate a rural 10 kV feeder with exactly 37 buses, three villages and 480 kW total demand.' --id rural_draft --draft
```

`--draft` still calls the LLM. `--nodes 37` explicitly supplies the total bus count. Resolve contradictions between the text and this option rather than silently overriding either.

## Feedback and experience

```bash
feeder-agents hierarchy --spec examples/hierarchical_urban.yaml --id urban_feedback --agent-feedback
feeder-agents transmission --spec examples/transmission_37.yaml --id tx_feedback --agent-feedback
```

These options enable external model participation in diagnosis and permitted repair. For hierarchical distribution, `--local-topology` additionally permits constrained customer reconnections within one transformer service area. Transmission topology permissions are controlled by its specification.

Single-voltage distribution selects `none`, `fixed`, `heuristic` or `agent` through `repair_policy.strategy`; only `agent` uses a live LLM for repair selection. Planning memory uses `design --planning-memory learn|read_only|off`, with `learn` as the default.

New workspaces begin without learned records. Experiences require validation before reuse as successful repairs. User workspaces contain requests, memory and models and are excluded from the release repository.

## References and rules

```bash
feeder-agents references
feeder-agents styles
feeder-agents rules --query voltage
```

Reference cases and derived parameters are packaged as JSON. Original extraction scripts and downloads are not included. Source paths, URLs, commits and hashes are provenance identifiers, not local runtime dependencies. See [THIRD_PARTY.md](../THIRD_PARTY.md).

## Reproduce the figures

```bash
python scripts/build_gallery.py
```

Each run creates a fresh directory below `workspace/gallery/`, executes all seven specifications and writes PNG/PDF figures, a CSV table and a JSON provenance manifest to `docs/assets/`. The overview selection is declared before generation. No historical case or alternative seed is substituted based on appearance or acceptance. The hierarchy detail uses the same newly generated urban model as the overview.

## Troubleshooting

- Import failure: check the active environment and install the `transmission` extra for transmission cases.
- Authentication failure: check `.env`, environment overrides and the provider model identifier.
- Unaccepted case: distinguish requirement conflicts, unsupported capabilities, service errors and unresolved numerical targets using the validation evidence.
- Different plotted positions: topology layouts change display coordinates. MV/LV view filtering does not construct an equivalent electrical model.
