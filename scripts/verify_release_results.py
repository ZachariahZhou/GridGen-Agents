"""Verify compact result tables and optionally rerun the shipped model exports."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / 'docs/results'


def read(path):
    return json.loads(path.read_text())


def rows(name):
    with (RESULTS / name).open() as stream:
        return list(csv.DictReader(stream))


def verify():
    manifest = read(RESULTS / 'manifest.json')
    for relative, expected in manifest['files'].items():
        path = RESULTS / relative
        assert path.resolve().is_relative_to(RESULTS.resolve())
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, relative
    deliveries = rows('final_deliveries.csv')
    assert len(deliveries) == 30 and len({r['case_id'] for r in deliveries}) == 15
    for row in deliveries:
        assert row['accepted'] == 'True'
        grade = read(RESULTS / row['grade_path'])
        assert grade['row']['artifact_fulfillment']
        assert grade['artifact_fulfillment']
        assert grade['turn_checks'] and all(turn['passed'] for turn in grade['turn_checks'])
        assert all(revision['passed'] for revision in grade['revisions'])
        assert not grade['authorization']['committed_any']
        assert grade['row']['case_id'] == row['case_id']
        assert int(grade['row']['repetition']) == int(row['repetition'])
    targeted = rows('targeted_deliveries.csv')
    assert len(targeted) == 8 and all(r['all_turn_pass'] == 'True' for r in targeted)
    response = rows('response_deliveries.csv')
    assert len(response) == 118 and len({r['network_id'] for r in response}) == 35
    assert all(r['success'] == r['protected_recomputed'] == 'True' for r in response)
    components = rows('response_components.csv')
    assert len(components) == 225
    for row in components:
        assert float(row['lower']) <= float(row['full_ratio']) <= float(row['upper'])
        assert float(row['lower']) <= float(row['half_ratio']) <= float(row['upper'])
    independent = rows('independent_response_ratios.csv')
    assert len(independent) == 48 and all(r['strict_target_pass'] == 'True' for r in independent)
    reuse = rows('static_reuse_runs.csv')
    assert len(reuse) == 144
    assert all(r['ac_converged'] == r['wrapper_ac_converged'] == 'True' for r in reuse)
    assert sum(r['status'] == 'iteration_cap' for r in reuse) == 12
    return dict(final_deliveries=30, response_tasks=118, response_components=225,
                independent_ratios=48, static_electrical_runs=144, integrity_files=len(manifest['files']))


def verify_models():
    import numpy as np
    from feeder_agents.schemas import ExperimentSpec, Feeder
    from feeder_agents.evaluation import evaluate, verdict
    from feeder_agents.hierarchy import HierarchicalSpec, HierarchicalFeeder, evaluate_hierarchy
    from feeder_agents.simulation import simulate
    from feeder_agents.transmission import TransmissionSpec, validate_case
    from feeder_agents.matpower import parse_matpower

    output = []
    manifest = read(RESULTS / 'models/manifest.json')
    for row in manifest['models']:
        source = RESULTS / 'models' / row['id']
        with tempfile.TemporaryDirectory(prefix='gridgen_export_check_') as temporary:
            target = Path(temporary) / row['id']
            shutil.copytree(source, target)
            spec = read(target / 'spec.json')
            if row['domain'] == 'transmission':
                parsed = parse_matpower(target / 'case_generated.m')
                case = dict(version='2', baseMVA=parsed['base_mva'],
                            **{key: np.asarray(parsed[key], dtype=float) for key in ('bus', 'gen', 'branch')})
                stored = read(target / 'case.json')
                for key in ('bus', 'gen', 'branch'):
                    assert np.allclose(case[key], stored[key], rtol=1e-10, atol=1e-10), key
                checked, _ = validate_case(case, TransmissionSpec.model_validate(spec), read(target / 'metadata.json'))
            else:
                schema = HierarchicalFeeder if row['domain'] == 'hierarchy' else Feeder
                model = schema.model_validate(read(target / 'feeder.json'))
                result = simulate(target / 'opendss/Master.dss')
                if row['domain'] == 'hierarchy':
                    checked = evaluate_hierarchy(model, HierarchicalSpec.model_validate(spec), result)
                else:
                    typed = ExperimentSpec.model_validate(spec)
                    checked = verdict(evaluate(model, typed, result), typed.mode)
            assert checked['accepted'], (row['id'], checked)
            output.append(dict(model=row['id'], accepted=True))
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--solvers', action='store_true')
    args = parser.parse_args()
    result = verify()
    if args.solvers:
        result['models'] = verify_models()
    print(json.dumps(result, indent=2))
