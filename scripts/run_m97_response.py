"""Freeze, certify, execute and summarize a broad witnessed response cohort."""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import copy
import csv
import json
import os
from pathlib import Path
import shutil
import sys

from feeder_agents.artifacts import atomic_json, digest, file_digest
from feeder_agents.benchmark.m97_response import network_designs, plans_for_network, certify_task, run_trial
from feeder_agents.benchmark.m97_continuous import run_continuous
from feeder_agents.benchmark.response_trials import runtime_versions


def freeze(root):
    import feeder_agents
    root.mkdir(parents=True, exist_ok=False)
    package = Path(feeder_agents.__file__).parent
    files = {}
    for path in sorted(package.rglob('*')):
        if not path.is_file() or path.suffix not in {'.py', '.json'}:
            continue
        name = str(path.relative_to(package))
        if name.startswith('benchmark/m97_') and path.name not in {'m97_response.py', 'm97_continuous.py'}:
            continue
        dest = root/'source/feeder_agents'/name; dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest); files[name] = file_digest(dest)
    script = root/'execution_scripts'/Path(__file__).name; script.parent.mkdir(parents=True)
    shutil.copyfile(__file__, script)
    designs = network_designs()
    protocol = dict(version='m97_broad_witness_v2', date='2026-10-11', networks=designs,
        intended_response_tasks=120, intended_continuous_tasks=24, source_files=files, source_hash=digest(files),
        script_hash=file_digest(script), runtime=runtime_versions(), response_budget=160,
        random_seeds=[97001, 97013, 97031], target_radius='max(0.0005,0.15*abs(witness_ratio-1))',
        max_witness_attempts=4, task_count_rule='24 distribution networks x Q/P x low/high overlap;12 transmission x1/2probes',
        coverage_caution='Designed coverage, not a fully crossed factorial. Voltage/phase and some transmission size/voltage levels co-vary.',
        positive_denominator='Search tasks certified with nontrivial private legal witnesses before search. Certification failures remain separately recorded.',
        transmission_comparison='All arms use one circuit edit per preview; full DC two-step lookahead, greedy one-step, random target-independent legal moves.',
        continuous=dict(variables='Two topology-selected subtree scales', bounds=[[.5, 1.5], [.5, 1.5]],
                        arms=['guided', 'cobyqa', 'uniform'], budget=160,
                        cobyqa=dict(initial_tr_radius=.1, final_tr_radius=1e-5, maxfev=1000, feasibility_tol=1e-9),
                        scope='Restricted production proposer and independent optimizer share parameterization, geometry and physical acceptance.'),
        scope='No LLM in this numerical package. M96 independent-target results are unchanged and not pooled.')
    atomic_json(root/'protocol.json', protocol); atomic_json(root/'protocol_checksum.json', dict(sha256=digest(protocol)))
    print(json.dumps(dict(frozen=True, networks=len(designs), candidate_tasks=120, continuous=24)), flush=True)


def verify(root):
    import feeder_agents
    if Path(feeder_agents.__file__).resolve().parent != (root/'source/feeder_agents').resolve():
        raise ValueError('Active imported package is not this experiment frozen source')
    if Path(__file__).resolve() != (root/'execution_scripts/run_m97_response.py').resolve():
        raise ValueError('Active runner is not this experiment frozen script')
    protocol = json.loads((root/'protocol.json').read_text())
    assert digest(protocol) == json.loads((root/'protocol_checksum.json').read_text())['sha256']
    for name, sha in protocol['source_files'].items():
        assert file_digest(root/'source/feeder_agents'/name) == sha, name
    assert runtime_versions() == protocol['runtime']
    assert file_digest(root/'execution_scripts/run_m97_response.py') == protocol['script_hash']
    return protocol


def construct_network(design, root):
    from feeder_agents.response_schema import ResponseDesignPlan
    from feeder_agents.response_search import physical_fingerprint
    from feeder_agents.solver_accounting import SolverMeter
    from feeder_agents.benchmark.response_suite import select_ports
    from feeder_agents.benchmark.m97_response import save_model
    from feeder_agents.benchmark.m97_continuous import recover_coordinates
    folder = Path(root)/'construction'/design['network_id']; folder.mkdir(parents=True, exist_ok=False)
    meter = SolverMeter(); tasks = []; continuous = []; certificates = []; error = None
    try:
        with meter:
            original, candidates = plans_for_network(design)
            save_model(original, folder/'original')
            fingerprint = physical_fingerprint(original)
            for candidate, ports in candidates:
                task, certificate = certify_task(original, candidate, ports, folder/'private'/candidate['task_id'])
                certificates.append(certificate)
                if task is not None:
                    tasks.append(task)
            if design['domain'] == 'distribution':
                coupling = 'high' if int(design['network_id'][1:3]) % 2 else 'low'
                candidate = copy.deepcopy(next(t for t, _ in candidates if t['response_kind'] == 'voltage_q' and t['coupling'] == coupling))
                candidate['task_id'] = design['network_id']+'_continuous'
                candidate['witness_family'] = 'geometry'
                search = candidate['plan']['search']
                search.update(allowed_actions=['scale_subtree'], max_layout_scale_change=None, max_changed_conductors=None)
                ports = select_ports(original, coupling == 'high')
                task, certificate = certify_task(original, candidate, ports, folder/'private'/candidate['task_id'])
                if task is not None:
                    winner = next(a for a in certificate['attempts'] if a['accepted'])
                    from feeder_agents.schemas import Feeder
                    model_path = folder/'private'/candidate['task_id']/f'attempt_{winner["attempt"]:02d}'/'model/feeder.json'
                    witness = dict(kind='distribution', feeder=Feeder.model_validate_json(model_path.read_text()))
                    coordinates, residual = recover_coordinates(original, witness, ports['children'])
                    if residual > 1e-8 or any(not .5 <= x <= 1.5 for x in coordinates):
                        certificate.update(certified=False, restricted_domain_failed=True)
                        atomic_json(folder/'private'/candidate['task_id']/'certificate.json', certificate)
                    else:
                        continuous.append(dict(task=task, children=ports['children'], bounds=[[.5, 1.5], [.5, 1.5]]))
                certificates.append(certificate)
    except Exception as exc:
        fingerprint = None
        error = dict(error_type=type(exc).__name__, error=str(exc))
    result = dict(network_id=design['network_id'], fingerprint=fingerprint, tasks=tasks, continuous=continuous,
                  certificates=certificates, error=error, accounting=meter.snapshot())
    atomic_json(folder/'record.json', result)
    return result


def build(root, workers):
    protocol = verify(root)
    records = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(construct_network, design, str(root)) for design in protocol['networks']]
        for i, future in enumerate(as_completed(futures), 1):
            record = future.result(); records.append(record)
            with (root/'construction_ledger.jsonl').open('a') as stream:
                stream.write(json.dumps(record)+'\n')
            print(f'certified networks {i}/36: {record["network_id"]} tasks={len(record["tasks"])} continuous={len(record["continuous"])} error={record["error"]}', flush=True)
    records.sort(key=lambda r: r['network_id'])
    fingerprints = [r['fingerprint'] for r in records if r['fingerprint']]
    assert len(fingerprints) == len(set(fingerprints))
    tasks = [t for r in records for t in r['tasks']]
    continuous = [t for r in records for t in r['continuous']]
    jobs = []
    for task in tasks:
        domain = task['plan']['base_spec']['network_kind']
        for arm in (['full', 'fixed', 'single', 'random'] if domain == 'distribution' else ['full', 'greedy', 'random']):
            for seed in protocol['random_seeds'] if arm == 'random' else [None]:
                jobs.append(dict(kind='response', task=task, arm=arm, seed=seed, budget=protocol['response_budget']))
    for task in continuous:
        for arm in ['guided', 'cobyqa', 'uniform']:
            for seed in protocol['random_seeds'] if arm == 'uniform' else [None]:
                jobs.append(dict(kind='continuous', task=task, arm=arm, seed=seed, budget=160))
    summary = dict(planned_networks=36, constructed_networks=len(fingerprints),
        intended_response_tasks=120, certified_response_tasks=len(tasks), intended_continuous_tasks=24,
        certified_continuous_tasks=len(continuous), planned_jobs=len(jobs),
        construction_ac_calls=sum(r['accounting']['total_ac_calls'] for r in records),
        errors=[dict(network_id=r['network_id'], **r['error']) for r in records if r['error']],
        failed_certificates=[c for r in records for c in r['certificates'] if not c['certified']])
    atomic_json(root/'construction_summary.json', summary)
    suite = dict(tasks=tasks, continuous=continuous, jobs=jobs, protocol_hash=digest(protocol),
                 certification_hashes={r['network_id']:file_digest(root/'construction'/r['network_id']/'record.json') for r in records})
    atomic_json(root/'suite.json', suite); atomic_json(root/'suite_checksum.json', dict(sha256=digest(suite)))
    print(json.dumps(summary), flush=True)


def execute_job(job, root):
    if job['kind'] == 'continuous':
        return run_continuous(job['task'], job['arm'], job['seed'], root, job['budget'])
    return run_trial(job['task'], job['arm'], job['seed'], root, job['budget'])


def expected_job_identity(job):
    key = 'public' if job['kind'] == 'continuous' else 'task'
    return digest({key: job['task'], 'arm': job['arm'], 'seed': job['seed'], 'budget': job['budget']})[:24]


def validate_suite(root):
    protocol = verify(root)
    suite = json.loads((root/'suite.json').read_text())
    if digest(suite) != json.loads((root/'suite_checksum.json').read_text())['sha256']:
        raise ValueError('Suite checksum mismatch')
    if suite['protocol_hash'] != digest(protocol):
        raise ValueError('Suite is not bound to this protocol')
    records = []
    for network_id, sha in suite['certification_hashes'].items():
        path = root/'construction'/network_id/'record.json'
        if file_digest(path) != sha:
            raise ValueError('Private construction record changed')
        record = json.loads(path.read_text()); records.append(record)
        for certificate in record['certificates']:
            saved = json.loads((root/'construction'/network_id/'private'/certificate['task_id']/'certificate.json').read_text())
            if saved != certificate:
                raise ValueError('Persisted feasibility certificate differs from construction record')
    records.sort(key=lambda r: r['network_id'])
    if suite['tasks'] != [t for r in records for t in r['tasks']] or suite['continuous'] != [t for r in records for t in r['continuous']]:
        raise ValueError('Public tasks differ from certified construction tasks')
    certificates = {c['task_id']: c for r in records for c in r['certificates']}
    for task in suite['tasks'] + [c['task'] for c in suite['continuous']]:
        certificate = certificates[task['task_id']]
        if not certificate['certified'] or certificate['public_task_hash'] != digest(task):
            raise ValueError('Public task lacks matching feasibility certificate')
    expected = []
    for task in suite['tasks']:
        arms = ['full', 'fixed', 'single', 'random'] if task['plan']['base_spec']['network_kind'] == 'distribution' else ['full', 'greedy', 'random']
        for arm in arms:
            for seed in protocol['random_seeds'] if arm == 'random' else [None]:
                expected.append(dict(kind='response', task=task, arm=arm, seed=seed, budget=protocol['response_budget']))
    for task in suite['continuous']:
        for arm in ['guided', 'cobyqa', 'uniform']:
            for seed in protocol['random_seeds'] if arm == 'uniform' else [None]:
                expected.append(dict(kind='continuous', task=task, arm=arm, seed=seed, budget=160))
    if suite['jobs'] != expected:
        raise ValueError('Frozen jobs differ from the declared complete factors')
    return suite, certificates


def run(root, workers):
    suite, _ = validate_suite(root)
    if (root/'execution_started.json').exists():
        raise ValueError('Execution already attempted; preserve it and explicitly audit recovery before resuming')
    atomic_json(root/'execution_started.json', dict(workers=workers, jobs=len(suite['jobs'])))
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(execute_job, job, str(root)): job for job in suite['jobs']}
        for i, future in enumerate(as_completed(futures), 1):
            job = futures[future]
            row = future.result(); row['package'] = job['kind']
            with (root/'results.jsonl').open('a') as stream:
                stream.write(json.dumps(row)+'\n')
            print(f'{i}/{len(futures)} {row["task_id"]} {row["arm"]} success={row["success"]} calls={row["search_ac_calls"]}', flush=True)
    atomic_json(root/'execution_complete.json', dict(jobs=len(suite['jobs'])))


def summarize(root):
    from feeder_agents.benchmark.m97_response import verify_artifacts
    suite, certificates = validate_suite(root)
    rows = [json.loads(s) for s in (root/'results.jsonl').read_text().splitlines() if s]
    if len(rows) != len(suite['jobs']) or len({r['job_id'] for r in rows}) != len(rows):
        raise ValueError('Incomplete or duplicate outcome denominator')
    expected = {expected_job_identity(j): j for j in suite['jobs']}
    if set(expected) != {r['job_id'] for r in rows}:
        raise ValueError('Observed jobs do not match the frozen tasks and arms')
    for row in rows:
        folder = Path(row['directory']); stored = json.loads((folder/'row.json').read_text())
        assert digest(stored) == json.loads((folder/'row_checksum.json').read_text())['sha256']
        assert dict(stored, package=row['package']) == row
        verify_artifacts(row['evidence_root'], row['core_artifacts'])
        if row.get('result_hash'):
            result = json.loads((Path(row['evidence_root'])/'result.json').read_text())
            checksum = result.pop('result_hash')
            if checksum != row['result_hash'] or digest(result) != checksum:
                raise ValueError('Selected production result checksum differs')
        job = expected[row['job_id']]
        request = json.loads((folder/'request.json').read_text())
        if request.get('public', request.get('public_task')) != job['task'] or any(request[k] != job[k] for k in ('arm', 'seed', 'budget')):
            raise ValueError('Actual request differs from frozen job')
        if row['outcome'] != 'execution_error' and row.get('original_fingerprint') != certificates[row['task_id']]['original_fingerprint']:
            raise ValueError('Executed baseline differs from certified witness baseline')
    fields = sorted({key for row in rows for key in row})
    with (root/'trial_metrics.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader()
        writer.writerows({k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in row.items()} for row in rows)
    groups = {}
    for row in rows:
        key = (row['package'], row.get('response_kind', 'voltage_q'), row['arm'])
        groups.setdefault(key, []).append(row)
    summary = []
    for (package, kind, arm), members in sorted(groups.items()):
        summary.append(dict(package=package, response_kind=kind, arm=arm, attempted=len(members),
            accepted=sum(r['success'] for r in members), feasible=sum(r['feasible'] for r in members),
            preserved=sum(r['contract_preserved'] for r in members),
            execution_errors=sum(r['outcome']=='execution_error' for r in members),
            mean_search_ac=sum(r['search_ac_calls'] for r in members)/len(members),
            mean_total_ac=sum(r['total_ac_calls'] for r in members)/len(members)))
    atomic_json(root/'summary.json', dict(groups=summary, jobs=len(rows),
        construction=json.loads((root/'construction_summary.json').read_text()),
        source_hash=json.loads((root/'protocol.json').read_text())['source_hash']))
    with (root/'group_summary.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(summary[0])); writer.writeheader(); writer.writerows(summary)
    print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('experiments/m97_response_v2'))
    parser.add_argument('--stage', choices=['freeze', 'build', 'run', 'summarize'], required=True)
    parser.add_argument('--workers', type=int, default=6)
    args = parser.parse_args(); root = args.root.resolve()
    if args.stage == 'freeze': freeze(root)
    elif args.stage == 'build': build(root, args.workers)
    elif args.stage == 'run': run(root, args.workers)
    else: summarize(root)
