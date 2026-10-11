"""Frozen, metered numerical trials with public-only task inputs."""
import copy
import importlib.metadata
import json
from pathlib import Path
import shutil
import time

from ..artifacts import atomic_json,digest,file_digest
from ..response_schema import ResponseDesignPlan
from ..solver_accounting import SolverMeter
from .response_proposals import proposal_adapter

ARMS=('full','fixed','single','random')
PUBLIC_KEYS={'network_id','task_id','layer','space','plan'}


def validate_public_task(task):
    if set(task)!=PUBLIC_KEYS:raise ValueError('Only public network/task metadata and plan may enter search')
    if task['layer'] not in {'research','witness'} or task['space'] not in {'G','GE'}:
        raise ValueError('Unknown task layer or permission space')
    plan=ResponseDesignPlan.model_validate(task['plan'])
    expected={'scale_layout','scale_subtree'}|({'replace_conductor'} if task['space']=='GE' else set())
    if set(plan.search.allowed_actions)!=expected:raise ValueError('Task actions do not match the declared G/GE space')
    if task['space']=='GE' and plan.search.max_changed_conductors!=4:raise ValueError('GE requires the declared four-assignment cap')
    return plan


def trial_plan(public_task,arm,budget):
    if arm not in ARMS:raise ValueError('Unknown arm')
    plan=validate_public_task(public_task).model_dump(mode='json')
    plan['search'].update(max_search_ac_calls=budget,max_rounds=32,candidates_per_round=8,
        max_joint_actions=1 if arm=='single' else 2,compensation_mode='fixed' if arm=='fixed' else 'bounded',
        calibrate_proxies=True,selector='heuristic',strategy='archive',archive_width=4,exploration_deficit_allowance=.15)
    return ResponseDesignPlan.model_validate(plan).model_dump(mode='json')


def job_identity(task,arm,seed,budget):
    return digest(dict(task=task,arm=arm,seed=seed,budget=budget))[:24]


def extract_evidence(result,folder):
    from ..schemas import Feeder
    from ..response_search import physical_fingerprint
    baseline=Feeder.model_validate_json((folder/'baseline/model/feeder.json').read_text())
    probes=json.loads((folder/'resolved_probes.json').read_text())
    manifest=json.loads((folder/'manifest.json').read_text())
    return dict(baseline_hash=physical_fingerprint(dict(kind='distribution',feeder=baseline)),
        probes_hash=digest(probes),source_hash=manifest['source_hash'],
        baseline_probes_hash=digest(result['baseline']['probes']),
        baseline_valid=bool(result['baseline']['valid'] and result['baseline']['base_accepted']))


def run_response_trial(public_task,arm,random_seed,budget,output,reference=None):
    from ..response_design import run_response_design
    plan=trial_plan(public_task,arm,budget)
    if (arm=='random') != (random_seed is not None):raise ValueError('Only the random arm has a proposal seed')
    identity=job_identity(public_task,arm,random_seed,budget)
    root=Path(output).resolve()/'trials'/identity
    root.mkdir(parents=True,exist_ok=False)
    folder=root/'response_designs'/'design'
    _,files=source_inventory()
    request=dict(public_task=public_task,arm=arm,random_seed=random_seed,budget=budget,plan=plan,source_hash=digest(files))
    atomic_json(root/'trial.json',request)
    row=dict(job_id=identity,network_id=public_task['network_id'],task_id=public_task['task_id'],
        layer=public_task['layer'],space=public_task['space'],arm=arm,random_seed=random_seed,
        budget=budget,task_hash=digest(public_task),plan_hash=digest(plan),directory=str(folder),
        success=False,feasible=False,contract_preserved=False,outcome='execution_error')
    started=time.perf_counter();meter=SolverMeter()
    try:
        with meter,proposal_adapter(arm,random_seed):
            result=run_response_design(plan,root,'design')
        row.update(success=result['target_met'],feasible=bool(result['selected']['base_accepted'] and result['selected']['valid']),
            contract_preserved=result['protected_contract_preserved'],
            search_target_met=result['selected']['target_met'],
            outcome='accepted' if result['target_met'] else result['stop_reason'],
            deficit=sum(result['selected']['deficits']),deficits=result['selected']['deficits'],
            response_values=[t['observed'] for t in result['selected']['targets']],
            changes=result['diagnosis']['design_space']['actual_changes'],result_hash=result['result_hash'],
            historical_solver_accounting=result['solver_accounting'])
        row.update(extract_evidence(result,folder))
        if reference is not None:
            row['reference_matches']=all(row.get(k)==reference[k] for k in ('baseline_hash','baseline_probes_hash'))
            if not row['reference_matches']:raise ValueError('Search baseline differs from certified construction')
    except Exception as exc:
        row.update(success=False,outcome='execution_error',error=f'{type(exc).__name__}: {exc}')
    finally:
        accounting=meter.snapshot()
        row.update(search_ac_calls=accounting['by_phase'].get('search',0),total_ac_calls=accounting['total_ac_calls'],
            actual_solver_accounting=accounting,seconds=round(time.perf_counter()-started,6))
        atomic_json(root/'row.json',row)
        atomic_json(root/'row_checksum.json',dict(sha256=digest(row)))
    return row


def source_inventory():
    import feeder_agents
    package=Path(feeder_agents.__file__).parent
    files={str(p.relative_to(package)):file_digest(p) for p in sorted(package.rglob('*')) if p.suffix in ('.py','.json')}
    return package,files


def runtime_versions():
    import platform
    import opendssdirect as dss
    versions=dict(python=platform.python_version(),OpenDSS_engine=dss.Basic.Version())
    for name in ('numpy','scipy','pydantic','opendssdirect.py','pypower','langchain','langgraph'):
        try:versions[name]=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:versions[name]=None
    return versions


def seal_suite(output,suite):
    atomic_json(Path(output)/'suite.json',suite)
    atomic_json(Path(output)/'suite_checksum.json',dict(sha256=digest(suite)))


def bind_construction(output,source,suite):
    from ..schemas import Feeder
    from ..response_search import physical_fingerprint
    output=Path(output);source=Path(source)
    records=json.loads((source/'networks.json').read_text())['networks']
    bundle=output/'construction_evidence';files={}
    for p in sorted(source.rglob('*.json')):
        relative=p.relative_to(source)
        if relative.parts[0]=='source':continue
        dest=bundle/relative;dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(p,dest);files[str(relative)]=file_digest(dest)
    public=json.loads((bundle/'public_tasks.json').read_text())['tasks']
    if public!=suite['tasks']:raise ValueError('Public suite differs from constructed task manifest')
    references={}
    for record in records:
        folder=bundle/'private'/record['network_id']
        feeder=Feeder.model_validate_json((folder/'original.json').read_text())
        fingerprint=physical_fingerprint(dict(kind='distribution',feeder=feeder))
        if fingerprint!=record['physical_hash']:raise ValueError('Construction fingerprint mismatch')
        baseline=json.loads((folder/'baseline/response.json').read_text())
        references[record['network_id']]=dict(baseline_hash=fingerprint,baseline_probes_hash=digest(baseline['probes']))
    if set(references)!={t['network_id'] for t in public}:raise ValueError('Missing construction network references')
    suite.update(construction_files=files,construction_hash=digest(files),reference_bindings=references)
    seal_suite(output,suite)


def verify_frozen_suite(output,check_runtime=True):
    output=Path(output);suite=json.loads((output/'suite.json').read_text())
    checksum=json.loads((output/'suite_checksum.json').read_text())['sha256']
    if digest(suite)!=checksum:raise ValueError('Frozen suite checksum changed')
    if check_runtime and runtime_versions()!=suite['versions']:raise ValueError('Frozen runtime versions changed')
    reconstructed=[]
    for budget in suite['budgets']:
        for task in suite['tasks']:
            for arm in suite['arms']:
                trial_plan(task,arm,budget)
                for seed in suite['random_seeds'] if arm=='random' else [None]:
                    reconstructed.append(dict(job_id=job_identity(task,arm,seed,budget),public_task=task,arm=arm,random_seed=seed,budget=budget))
    if reconstructed!=suite['jobs']:raise ValueError('Frozen jobs differ from declared factors')
    if digest(suite['source_files'])!=suite['source_hash']:raise ValueError('Inconsistent source manifest')
    for name,sha in suite['source_files'].items():
        if file_digest(output/'source/feeder_agents'/name)!=sha:raise ValueError('Frozen source changed')
    for name,sha in suite.get('construction_files',{}).items():
        if file_digest(output/'construction_evidence'/name)!=sha:raise ValueError('Frozen construction evidence changed')
    if suite.get('construction_files') and digest(suite['construction_files'])!=suite['construction_hash']:
        raise ValueError('Inconsistent construction manifest')
    for name,sha in suite.get('script_hashes',{}).items():
        if file_digest(output/'execution_scripts'/name)!=sha:raise ValueError('Frozen execution script changed')
    return suite


def freeze_response_suite(tasks,arms,budgets,random_seeds,output):
    if not tasks or not arms or not budgets or not random_seeds:raise ValueError('Empty experiment factors')
    if len({t['task_id'] for t in tasks})!=len(tasks):raise ValueError('Duplicate task IDs')
    for task in tasks:validate_public_task(task)
    jobs=[]
    for budget in budgets:
        for task in tasks:
            for arm in arms:
                trial_plan(task,arm,budget)
                for seed in random_seeds if arm=='random' else [None]:
                    jobs.append(dict(job_id=job_identity(task,arm,seed,budget),public_task=task,
                        arm=arm,random_seed=seed,budget=budget))
    if len({j['job_id'] for j in jobs})!=len(jobs):raise ValueError('Duplicate experiment factors')
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=False)
    package,files=source_inventory()
    for relative in files:
        dest=output/'source/feeder_agents'/relative;dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(package/relative,dest)
    suite=dict(protocol='m96_response_mechanisms_v1',tasks=copy.deepcopy(tasks),arms=arms,budgets=budgets,
        random_seeds=random_seeds,jobs=jobs,source_files=files,source_hash=digest(files),versions=runtime_versions(),
        scope='Frozen G/GE numerical proposal experiment. No LLM calls; all targets, contracts, acceptance and work accounting are shared. Analyze paired outcomes by original network.')
    seal_suite(output,suite)
    return suite
