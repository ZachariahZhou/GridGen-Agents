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
| Research task | `research_tasks/<id>/` | `selected/`, `suitability.json`, frozen contract, task evidence and `dataset.zip` |
| Natural-language design | `projects/<project>/designs/<id>/` | Plan and result index; `outcome.directory` locates generated models |

Keep models, validation JSON, HTML views and metadata together. MATPOWER exports represent balanced models; phase-resolved unbalanced distribution uses OpenDSS. Consult each run's artifact and validation records.

## Research tasks

```bash
feeder-agents --workspace workspace/tasks research-design --plan examples/research_voltage_control.yaml --id voltage
feeder-agents --workspace workspace/tasks research-design --plan examples/research_static_pv_impact.yaml --id pv
feeder-agents --workspace workspace/tasks research-design --plan examples/research_transmission_transfer.yaml --id transfer
```

These structured examples run without an API key. `research-design --request '...' --id task` uses the configured model; `--draft` compiles without physical generation. Supply a new ID when changing the request, plan or runtime code. `selected/` contains the callable model; `response_designs/response/` holds the original reference, candidate history and physical experiments. The finite task budget and response target remain fixed throughout the loop. Inspect `target_met`, `verification_passed`, `task_validation` and `diagnosis`; a stopped search does not prove physical impossibility.

```bash
python scripts/run_research_tasks.py --output workspace/task_reproduction
python scripts/plot_research_tasks.py --results workspace/task_reproduction/results.json --output docs/assets/research_tasks
```

The runner uses three task specifications and three seeds; it records every attempt. The figure separates local response targets from finite-intervention effects. Full defaults, measurements, failure records and interpretation are documented in the [task workflow report](research-task-workflows.md).

`python scripts/check_examples.py` executes the seven base generation examples and validates the schemas of the five response and three research-task specifications. The latter eight are not physically executed by that checker; use their dedicated commands and experiment runners for numerical validation.

## Model configuration

Copy `.env.example` to `.env`. Set `FEEDER_MODEL`, `OPENAI_API_KEY` and `OPENAI_BASE_URL` for an available compatible service. Process environment variables override the file in the launch directory. The template contains no API key.

Create a draft without electrical generation:

```bash
feeder-agents design 'Generate a rural 10 kV feeder with exactly 37 buses, three villages and 480 kW total demand.' --id rural_draft --draft
```

`--draft` still calls the LLM. `--nodes 37` explicitly supplies the total bus count. Resolve contradictions between the text and this option rather than silently overriding either.

The public examples use English. The request parser also retains Chinese-language recognition, with its patterns and regression inputs represented by Unicode escapes in source files. This representation preserves decoded input text and does not change the configured model's language capabilities.

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
