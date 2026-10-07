"""Run the seven included specifications locally, with no external LLM calls."""
import argparse
import json
import tempfile
from pathlib import Path

import yaml


def check_all(directory):
    from feeder_agents.schemas import ExperimentSpec
    from feeder_agents.workflow import run_experiment
    from feeder_agents.hierarchy_workflow import run_hierarchy
    from feeder_agents.transmission import run_transmission

    root = Path(__file__).resolve().parents[1]
    records = []
    for path in sorted((root / 'examples').glob('*.yaml')):
        config = yaml.safe_load(path.read_text())
        if path.stem.startswith('distribution_'):
            result = run_experiment(ExperimentSpec.model_validate(config), directory, path.stem)
        elif path.stem.startswith('hierarchical_'):
            result = run_hierarchy(config, directory, path.stem)
        elif path.stem.startswith('transmission_'):
            result = run_transmission(config, directory, path.stem)
        else:
            raise ValueError(f'Unregistered example family: {path.name}')
        expected = config.get('count', 1)
        row = dict(example=path.name, expected=expected, accepted=result['accepted'])
        records.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        assert result['accepted'] == expected, f'Example did not pass: {row}'
    assert len(records) == 7, 'Review the example registry after changing the selection'
    return dict(examples=records, all_accepted=True, external_llm_calls=0)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Optional JSON summary; case files use a temporary directory')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='feeder_examples_') as folder:
        report = check_all(Path(folder))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
