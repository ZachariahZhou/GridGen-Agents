import fcntl
import hashlib
import importlib.metadata
import json
import re
import statistics
import shutil
import time
import zipfile
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .artifacts import atomic_json, digest, file_digest, render_html
from .evaluation import evaluate, verdict
from .generation import generate_feeder, upgrade_conductors, spatial_metrics
from .rules import data, load_rules
from .reporting import render_report
from .memory import ExperienceStore, experience_context, experience_signature, collect_experiences
from .schemas import ExperimentPlan, ExperimentSpec, Feeder
from .simulation import export_dss, simulate
from .ties import TieConstructionError
from .bounded_execution import ExecutionLimits, INTERRUPTED_STATUSES, run_bounded_jobs
from .repairs import candidates, apply_candidate, diagnose, severity, improves, select_candidate, selector_model, selector_identity, preview_candidates


class SampleState(TypedDict, total=False):
    spec: dict
    seed: int
    directory: str
    feeder: dict
    simulation: dict
    checks: list
    history: list
    repairs: int
    outcome: dict
    previous: dict | None
    seen_models: list
    excluded_candidates: list
    stopped: str | None
    selector_config: dict | None
    initial_feeder: dict | None
    experience_hints: list
    preview_runs: int
    export_formats: list[str]


def _generate(state: SampleState):
    feeder = (Feeder.model_validate(state['initial_feeder']) if state.get('initial_feeder') is not None
              else generate_feeder(ExperimentSpec(**state['spec']), state['seed']))
    if feeder.seed != state['seed']:
        raise ValueError('Frozen model seed does not match sample seed')
    atomic_json(Path(state['directory']) / 'baseline' / 'feeder.json', feeder.model_dump())
    return {'feeder': feeder.model_dump(), 'repairs': 0, 'history': [], 'previous': None,
            'preview_runs': 0, 'seen_models': [digest(feeder.model_dump())], 'excluded_candidates': [], 'stopped': None}


def _simulate(state: SampleState):
    folder = Path(state['directory']) / ('baseline' if state['repairs'] == 0 else f'trials/{state["repairs"]:02d}')
    atomic_json(folder / 'feeder.json', state['feeder'])
    master = export_dss(Feeder(**state['feeder']), folder / 'opendss')
    try:
        result = simulate(master)
        # Fail clearly on nonfinite engine outputs, never serialize NaN as valid JSON.
        json.dumps(result, allow_nan=False)
    except Exception as exc:
        result = {'converged': False, 'error': f'{type(exc).__name__}: {exc}'}
    atomic_json(folder / 'simulation.json', result)
    return {'simulation': result}


def _evaluate(state: SampleState):
    spec = ExperimentSpec(**state['spec'])
    checks = evaluate(Feeder(**state['feeder']), spec, state['simulation'])
    status = verdict(checks, spec.mode)
    folder = Path(state['directory']) / ('baseline' if state['repairs'] == 0 else f'trials/{state["repairs"]:02d}')
    atomic_json(folder / 'validation.json', {'checks': checks, **status})
    history = state['history'] + [{'attempt': state['repairs'], 'checks': checks,
                                  'model_hash': digest(state['feeder']), **status}]
    previous = state.get('previous')
    update = {'checks': checks, 'outcome': status, 'history': history, 'previous': None}
    if previous:
        before = severity(Feeder(**previous['feeder']), previous['simulation'], spec)
        after = severity(Feeder(**state['feeder']), state['simulation'], spec)
        committed = improves(previous['checks'], checks, before, after)
        history.append({'action': 'commit' if committed else 'rollback',
                        'attempt': state['repairs'], 'before_severity': before, 'after_severity': after,
                        'reason': 'Valid strict improvement without worsened/new violations' if committed else
                                  'No strict improvement, new/worsened violation, or invalid simulation'})
        if not committed:
            update.update(previous)
    return update


def _route(state: SampleState):
    violations = {x['rule_id'] for x in state['checks'] if x['status'] == 'fail'}
    doc_fixable = {r['rule_id'] for r in state['spec'].get('document_rules',[])
                   if r['metric'] in {'min_voltage_pu','max_voltage_pu','max_loading_ratio'}}
    fixable = bool(violations & ({'research.voltage', 'cn.voltage', 'research.ampacity'} | doc_fixable))
    if (not state['outcome']['accepted'] and state['outcome']['valid'] and fixable and
            state['repairs'] < state['spec']['max_repairs'] and not state.get('stopped') and
            state['spec']['repair_policy']['strategy'] != 'none'):
        return 'repair'
    return 'publish'


def _repair(state: SampleState):
    spec = ExperimentSpec(**state['spec'])
    feeder = Feeder(**state['feeder'])
    diagnostic = diagnose(feeder, state['simulation'], state['checks'], spec)
    diagnostic['experience_hints'] = state.get('experience_hints',[])
    policy = spec.repair_policy.strategy
    log = {'action': 'repair_decision', 'strategy': policy, 'diagnosis': diagnostic}
    if policy == 'fixed':
        repaired = upgrade_conductors(feeder)
        log['selection'] = {'selector': 'fixed', 'action': 'upgrade_all_conductors'}
    else:
        options = [c for c in candidates(feeder, spec, state['simulation'], state['checks'])
                   if c.candidate_id not in state['excluded_candidates']]
        log['candidates'] = [c.model_dump() for c in options]
        if not options:
            return {'stopped': 'no_legal_candidates', 'history': state['history']+[log]}
        if policy == 'agent':
            started = time.perf_counter()
            request_attempted = False
            previews = []
            try:
                model = selector_model()
                if selector_identity(model) != state.get('selector_config'):
                    raise ValueError('Repair model settings changed during experiment')
                previews = preview_candidates(feeder, spec, state['simulation'], state['checks'], options,
                    Path(state['directory']) / 'previews' / f'{state["repairs"]+1:02d}')
                diagnostic['candidate_previews'] = previews
                request_attempted = True
                choice, trace = select_candidate(options, diagnostic, model=model)
            except Exception as exc:
                # Exception bodies from remote providers may contain credentials.
                # Retain the class, not the raw remote error text.
                log['selection'] = {'selector': 'agent', 'status': 'error', 'error_type': type(exc).__name__,
                                    **(state.get('selector_config') or {}),
                                    'request_attempted': request_attempted, 'usage': None,
                                    'elapsed_seconds': round(time.perf_counter()-started,3)}
                return {'stopped': 'agent_selection_failed', 'history': state['history']+[log],
                        'preview_runs': state.get('preview_runs',0)+len(previews)}
            log['selection'] = trace
            if choice is None:
                return {'stopped': 'agent_requested_stop', 'history': state['history']+[log],
                        'preview_runs': state.get('preview_runs',0)+len(previews)}
        else:
            choice = options[0]
            log['selection'] = {'selector': 'heuristic', 'candidate_id': choice.candidate_id,
                                'reason': 'First ranked legal candidate; no LLM used'}
        repaired = apply_candidate(feeder, spec, choice)
        log['chosen_candidate'] = choice.model_dump()
    model_hash = digest(repaired.model_dump())
    if model_hash in state['seen_models']:
        return {'stopped': 'repeated_model', 'history': state['history']+[log],
                'preview_runs': state.get('preview_runs',0)+(len(previews) if policy == 'agent' else 0)}
    previous = {key: state[key] for key in ('feeder', 'simulation', 'checks', 'outcome')}
    excluded = state['excluded_candidates'] + ([choice.candidate_id] if policy != 'fixed' else [])
    return {'feeder': repaired.model_dump(), 'repairs': state['repairs']+1, 'previous': previous,
            'excluded_candidates': excluded, 'seen_models': state['seen_models']+[model_hash],
            'history': state['history']+[log],
            'preview_runs': state.get('preview_runs',0)+(len(previews) if policy == 'agent' else 0)}


def _publish(state: SampleState):
    path = Path(state['directory'])
    feeder = Feeder(**state['feeder'])
    from .style import style_contract
    from .phase_operations import refresh_phase_evidence
    refresh_phase_evidence(feeder, ExperimentSpec(**state['spec']))
    feeder.design_evidence['style_contract'] = style_contract(ExperimentSpec(**state['spec']), feeder)
    feeder.design_evidence['final_equipment_assignments']=[{'line_id':line.id,'equipment_id':line.conductor} for line in feeder.lines+feeder.tie_lines]
    state['feeder'] = feeder.model_dump()
    used_catalog={key:value for key,value in data('conductors.json').items() if key in {line.conductor for line in feeder.lines+feeder.tie_lines}}
    atomic_json(path/'equipment_catalog.json',used_catalog)
    export_dss(feeder, path / 'opendss')
    atomic_json(path / 'feeder.json', state['feeder'])
    if feeder.design_evidence:
        atomic_json(path / 'design_evidence.json', feeder.design_evidence)
    atomic_json(path / 'simulation.json', state['simulation'])
    atomic_json(path / 'validation.json', {'checks': state['checks'], **state['outcome'],
                                          'certification': 'research_checks_only'})
    atomic_json(path / 'history.json', state['history'])
    export_report=None
    if 'matpower' in state.get('export_formats',[]):
        from .distribution_matpower import export_distribution_matpower
        try:
            export_report=export_distribution_matpower(feeder,ExperimentSpec(**state['spec']),state['simulation'],path/'matpower')
        except (ValueError,ImportError) as exc:
            export_report=dict(verified=False,error_type=type(exc).__name__,reason=str(exc))
            atomic_json(path/'matpower'/'validation.json',export_report)
    (path / 'visualization.html').write_text(render_html(feeder, state['simulation'], state['outcome']['accepted']), encoding='utf-8')
    stop_reason = state.get('stopped')
    if stop_reason is None:
        if state['outcome']['operational_pass']:
            stop_reason = 'operational_checks_passed'
        elif state['outcome']['accepted'] and state['spec']['mode'] == 'stress':
            stop_reason = 'stress_violations_preserved'
        elif not state['outcome']['valid']:
            stop_reason = 'invalid_model_or_solver'
        elif state['spec']['repair_policy']['strategy'] == 'none':
            stop_reason = 'repair_disabled'
        elif state['repairs'] >= state['spec']['max_repairs']:
            stop_reason = 'repair_budget_exhausted'
        else:
            stop_reason = 'no_supported_repair'
    summary = {**state['outcome'], 'sample_id': path.name, 'seed': state['seed'],
               'model_hash': digest(state['feeder']), 'repairs': state['repairs'],
               'repair_strategy': state['spec']['repair_policy']['strategy'],
               'repair_stop_reason': stop_reason,
               'repair_commits': sum(h.get('action') == 'commit' for h in state['history']),
               'repair_rollbacks': sum(h.get('action') == 'rollback' for h in state['history']),
               'solver_runs': 1+state['repairs']+state.get('preview_runs',0),
               'preview_solver_runs': state.get('preview_runs',0),
               'rule_status': {c['rule_id']: c['status'] for c in state['checks']},
               'solver_converged': bool(state['simulation'].get('converged')),
               'voltage_kv': feeder.voltage_kv, 'frequency_hz': feeder.frequency_hz,
               'phase_mode':feeder.phase_mode,
               'equipment_sources':sorted({c['source_feeder'] for c in used_catalog.values() if 'source_feeder' in c}),
               'equipment_type_count':len(used_catalog),
               'max_vuf_percent':state['simulation'].get('max_vuf_percent'),
               'vuf_eligible_bus_count':state['simulation'].get('vuf_eligible_bus_count'),
               'vuf_ineligible_bus_count':state['simulation'].get('vuf_ineligible_bus_count'),
               'voltage_profile': feeder.design_evidence.get('rule_system', {}).get('voltage_profile'),
               'bus_count':len(feeder.buses),
               'tie_count':len(feeder.tie_lines),
               'topology_family':state['spec']['scenario'].get('topology'),
               'load_shape':state['spec']['scenario'].get('load_shape','heterogeneous'),
               'junction_count':len({b.id for b in feeder.buses}-{feeder.source_bus}-{l.bus for l in feeder.loads}),
               'load_count': len(feeder.loads), 'total_kw': feeder.total_kw,
               **spatial_metrics(feeder),
               'scenario': state['spec']['scenario'],
               'design_method': feeder.design_evidence.get('method'),
               'village_count': len(feeder.design_evidence.get('villages',[])),
               'min_voltage_pu': state['simulation'].get('min_voltage_pu'),
               'max_voltage_pu': state['simulation'].get('max_voltage_pu'),
               'relative_path': str(Path('experiments') / path.parent.name / path.name)}
    if export_report is not None:summary['matpower_export']=export_report
    summary['artifacts'] = {str(p.relative_to(path)): file_digest(p)
                            for p in sorted(path.rglob('*')) if p.is_file() and p.name not in {'outcome.json', 'completion.json'}}
    _write_outcome(path, summary)
    return {'outcome': summary}


def sample_graph():
    graph = StateGraph(SampleState)
    for name, fn in [('generate', _generate), ('simulate', _simulate), ('evaluate', _evaluate),
                     ('repair', _repair), ('publish', _publish)]:
        graph.add_node(name, fn)
    graph.add_edge(START, 'generate')
    graph.add_edge('generate', 'simulate')
    graph.add_edge('simulate', 'evaluate')
    graph.add_conditional_edges('evaluate', _route)
    graph.add_conditional_edges('repair', lambda state: 'publish' if state.get('stopped') else 'simulate')
    graph.add_edge('publish', END)
    return graph.compile()


def _run_sample(payload):
    spec, seed, directory, selector_config, *extras = payload
    directory = Path(directory)
    try:
        return sample_graph().invoke({'spec': spec, 'seed': seed, 'directory': str(directory), 'selector_config': selector_config,
                                     'experience_hints': extras[0] if extras else [],
                                     'initial_feeder': extras[1] if len(extras)>1 else None,
                                     'export_formats':extras[2] if len(extras)>2 else ['opendss']},
                                     {'recursion_limit': 30})['outcome']
    except Exception as exc:
        error = {'sample_id': directory.name, 'seed': seed, 'accepted': False, 'valid': False,
                 'operational_pass': False, 'solver_converged': False,
                 'model_hash': None, 'error': f'{type(exc).__name__}: {exc}',
                 'relative_path': str(Path('experiments') / directory.parent.name / directory.name)}
        if isinstance(exc, TieConstructionError):
            error['construction_failure'] = exc.diagnostic
        _write_outcome(directory, error)
        return error


def _write_outcome(directory, outcome):
    atomic_json(directory / 'outcome.json', outcome)
    atomic_json(directory / 'completion.json', {'outcome_sha256': file_digest(directory / 'outcome.json')})


def _completed_sample(path, seed):
    """Validate the commit marker and payload before trusting a resumed result."""
    try:
        marker = json.loads((path / 'completion.json').read_text())
        if marker['outcome_sha256'] != file_digest(path / 'outcome.json'):
            return None
        outcome = json.loads((path / 'outcome.json').read_text())
        if outcome['seed'] != seed or outcome['sample_id'] != path.name:
            return None
        if outcome.get('error'):
            return outcome if outcome.get('accepted') is False and outcome.get('valid') is False else None
        artifacts = outcome['artifacts']
        required = {'feeder.json', 'simulation.json', 'validation.json', 'history.json',
                    'visualization.html', 'opendss/Master.dss', 'opendss/Lines.dss',
                    'opendss/Loads.dss', 'opendss/Generation.dss', 'opendss/LineCodes.dss', 'opendss/BusCoords.csv'}
        if not required.issubset(artifacts):
            return None
        for name, expected in artifacts.items():
            target = (path / name).resolve()
            if not target.is_relative_to(path.resolve()) or not target.is_file() or file_digest(target) != expected:
                return None
        feeder = json.loads((path / 'feeder.json').read_text())
        validation = json.loads((path / 'validation.json').read_text())
        if digest(feeder) != outcome['model_hash'] or feeder['seed'] != seed:
            return None
        if any(outcome[key] != validation[key] for key in ('accepted', 'valid', 'operational_pass')):
            return None
        return outcome
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _runtime_fingerprint(matpower: bool = False):
    package = Path(__file__).parent
    source_hashes = {str(p.relative_to(package)): file_digest(p) for p in sorted(package.rglob('*.py'))}
    versions = {name: importlib.metadata.version(name) for name in
                ('langchain', 'langgraph', 'opendssdirect.py', 'dss-python-backend', 'pydantic', 'networkx')}
    if matpower:
        for name in ('pypower', 'scipy', 'numpy'):
            try:
                versions[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                versions[name] = 'not installed'
    calibration_hashes = {p.name:file_digest(p) for p in sorted((package/'data').glob('*.json'))}
    return {'source_hashes': source_hashes, 'versions': versions, 'calibration_hashes':calibration_hashes}


def run_experiment(spec: ExperimentSpec, workspace: Path, experiment_id: str, workers: int = 1, plan: ExperimentPlan | None = None, initial_feeders: list[Feeder] | None = None, *, execution_limits: ExecutionLimits | dict | None = None, cancel_event=None) -> dict:
    batch_started = time.monotonic()
    limits = execution_limits if isinstance(execution_limits, ExecutionLimits) else ExecutionLimits(**(execution_limits or {}))
    spec = ExperimentSpec.model_validate(spec.model_dump())
    if plan is not None:
        plan = ExperimentPlan.model_validate(plan.model_dump())
        if plan.spec != spec:
            raise ValueError('Plan specification does not match execution specification')
    if initial_feeders is not None:
        initial_feeders = [Feeder.model_validate(f.model_dump()) for f in initial_feeders]
        if len(initial_feeders) != spec.count:
            raise ValueError('Frozen model count differs from experiment count')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', experiment_id):
        raise ValueError('Invalid experiment_id: use 1-64 letters, digits, underscore or hyphen')
    if not 1 <= workers <= 16:
        raise ValueError('workers must be between 1 and 16')
    root = Path(workspace).resolve() / 'experiments' / experiment_id
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.lock').open('w') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('Experiment is already running') from exc
        return _execute_batch(spec, root, workers, plan, initial_feeders, limits=limits, cancel_event=cancel_event, batch_started=batch_started)


def _execute_batch(spec: ExperimentSpec, root: Path, workers: int, plan=None, initial_feeders=None, *, limits=None, cancel_event=None, batch_started=None):
    limits = limits or ExecutionLimits()
    selector_config = selector_identity(selector_model()) if spec.repair_policy.strategy == 'agent' else None
    manifest = {'spec': spec.model_dump(), 'rules': load_rules(spec), 'sources': data('sources.json'),
                'conductors': data('conductors.json'),
                'runtime': _runtime_fingerprint(matpower=plan is not None and 'matpower' in plan.export_formats),
                'plan': plan.model_dump() if plan else None, 'repair_selector': selector_config,
                'initial_model_hashes': [digest(f.model_dump()) for f in initial_feeders] if initial_feeders is not None else None}
    manifest_path = root / 'manifest.json'
    experience_store = ExperienceStore(root.parent.parent / 'experiences.sqlite')
    signature = experience_signature(manifest)
    context = experience_context(spec)
    old_snapshot = json.loads(manifest_path.read_text()).get('experience_snapshot',[]) if manifest_path.exists() else None
    hints = old_snapshot if old_snapshot is not None else experience_store.lookup(signature,context)
    manifest['experience_snapshot'] = hints
    manifest['configuration_hash'] = digest(manifest)
    if manifest_path.exists():
        old = json.loads(manifest_path.read_text())
        if old != manifest:
            raise ValueError('Existing experiment configuration/rules/software differ; use a new experiment_id')
    else:
        atomic_json(manifest_path, manifest)
    atomic_json(root / 'specification.json', spec.model_dump())
    if plan is not None:
        atomic_json(root / 'plan.json', plan.model_dump())
    execution_root = root / 'execution_runs'
    execution_root.mkdir(exist_ok=True)
    run_number = max((int(p.stem[4:]) for p in execution_root.glob('run_*.json') if p.stem[4:].isdigit()), default=-1) + 1
    run_id = f'run_{run_number:05d}'
    execution = dict(run_id=run_id, policy=limits.payload(), status='running', samples=[])
    execution_path = execution_root / (run_id + '.json')
    atomic_json(execution_path, execution)
    outcomes, pending = [], []
    for index in range(spec.count):
        path = root / f'sample_{index:05d}'
        seed = int.from_bytes(hashlib.sha256(f'{spec.seed}:{index}'.encode()).digest()[:4], 'big')
        outcome = _completed_sample(path, seed)
        if outcome is not None:
            retry = limits.resume_interrupted and outcome.get('execution_status') in INTERRUPTED_STATUSES
            if not retry:
                outcomes.append(outcome)
                continue
            # Retrying is explicit, and prior failure evidence remains immutable.
            archive = execution_root / run_id / 'retried' / path.name
            archive.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(archive))
        elif path.exists():
            archive = execution_root / run_id / 'incomplete' / path.name
            archive.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(archive))
        pending.append((spec.model_dump(), seed, str(path), selector_config, hints,
                        initial_feeders[index].model_dump() if initial_feeders is not None else None,
                        plan.export_formats if plan is not None else ['opendss']))
    # Even a one-worker batch lives outside the Agent/UI process. Native DSS
    # contexts must not share a lifetime with LangChain/Streamlit worker threads.
    if pending:
        def record_execution(record):
            item = pending[record['index']]
            path = Path(item[2])
            # A complete atomic outcome wins even if its worker stopped immediately
            # before sending a completion notification to the supervisor.
            outcome = _completed_sample(path, item[1])
            if outcome is None:
                status = record['status'] if record['status'] != 'completed' else 'worker_error'
                outcome = dict(sample_id=path.name, seed=item[1], accepted=False, valid=False,
                    operational_pass=False, solver_converged=False, model_hash=None,
                    error=f'Execution stopped: {status}', execution_status=status,
                    execution_started=record['started'], execution_run_id=run_id,
                    worker_exitcode=record.get('worker_exitcode'),
                    relative_path=str(Path('experiments') / root.name / path.name))
                _write_outcome(path, outcome)
            outcomes.append(outcome)
            execution['samples'].append(dict(sample_id=path.name, **record))
            atomic_json(execution_path, execution)
        run_bounded_jobs(_run_sample, pending, workers, limits, cancel_event,
                         batch_started=batch_started, on_result=record_execution)
    execution['status'] = 'interrupted' if any(o.get('execution_status') in INTERRUPTED_STATUSES for o in outcomes) else 'completed'
    atomic_json(execution_path, execution)
    outcomes.sort(key=lambda x: x['sample_id'])
    successful = [x for x in outcomes if x.get('model_hash')]
    hashes = [x['model_hash'] for x in successful]
    counts = [x['load_count'] for x in successful]
    summary = {'experiment_id': root.name, 'directory': str(root), 'attempted': len(outcomes),
               'accepted': sum(x['accepted'] for x in outcomes),
               'operational_pass': sum(x['operational_pass'] for x in outcomes),
               'failed_or_unaccepted': sum(not x['accepted'] for x in outcomes),
               'configuration_hash': manifest['configuration_hash'], 'samples': outcomes,
               'execution_status': execution['status'], 'execution_run_id': run_id,
               'execution_started_count': sum(o.get('execution_started', True) for o in outcomes),
               'statistics': {'unique_exact_models': len(set(hashes)),
                              'load_count_min': min(counts, default=None),
                              'load_count_max': max(counts, default=None),
                              'load_count_mean': statistics.mean(counts) if counts else None},
               'limitations': ['Single-voltage peak snapshot; phase mode recorded per sample; no explicit neutral or transformers.',
                               'Equipment provenance and transfer assumptions recorded; no full design-standard certification.',
                               'Exact hash counts do not prove structural/statistical realism.',
                               'Stress accepts valid cases with recorded operational violations; no target-violation guarantee.']}
    collect_experiences(experience_store,signature,context,root,outcomes,manifest['configuration_hash'])
    summary['experience_hints_used'] = hints
    summary['verified_report'] = render_report(summary)
    (root / 'report.md').write_text(summary['verified_report'], encoding='utf-8')
    atomic_json(root / 'summary.json', summary)
    _batch_index(root, summary)
    archive = root / 'dataset.zip'
    with zipfile.ZipFile(root / 'dataset.zip.tmp', 'w', zipfile.ZIP_DEFLATED) as z:
        for path in sorted(root.rglob('*')):
            if path.is_file() and path.name not in {'.lock', 'dataset.zip', 'dataset.zip.tmp'}:
                z.write(path, path.relative_to(root))
    (root / 'dataset.zip.tmp').replace(archive)
    return summary


def _batch_index(root, summary):
    import html
    rows = []
    for s in summary['samples']:
        name = s['sample_id']
        rows.append(f'<tr><td><a href="{name}/visualization.html">{name}</a></td>'
                    f'<td>{s["accepted"]}</td><td>{s["operational_pass"]}</td>'
                    f'<td>{s.get("load_count", "—")}</td><td>{s.get("min_voltage_pu", "—")}</td></tr>')
    (root / 'index.html').write_text('<!doctype html><meta charset="utf-8"><title>馈线实验</title>'
        '<style>body{font:16px system-ui;max-width:1100px;margin:40px auto}td,th{padding:10px;border-bottom:1px solid #ddd}</style>'
        f'<h1>实验 {html.escape(root.name)}</h1><p>接受 {summary["accepted"]} / 尝试 {summary["attempted"]}；研究检查，不代表完整规范认证。</p>'
        '<a href="dataset.zip">下载数据集 ZIP</a><table><tr><th>样本</th><th>接受</th><th>运行合格</th><th>负荷点</th><th>最低电压 pu</th></tr>'
        + ''.join(rows) + '</table>', encoding='utf-8')


def read_verified_experiment(root: Path) -> dict:
    """Reconstruct a report from committed artifacts, ignoring editable summary prose."""
    root = Path(root).resolve()
    manifest = json.loads((root / 'manifest.json').read_text())
    expected = manifest.pop('configuration_hash')
    if digest(manifest) != expected:
        raise ValueError('Experiment manifest hash mismatch')
    spec = ExperimentSpec.model_validate(manifest['spec'])
    samples = []
    for index in range(spec.count):
        seed = int.from_bytes(hashlib.sha256(f'{spec.seed}:{index}'.encode()).digest()[:4], 'big')
        directory = root / f'sample_{index:05d}'
        sample = _completed_sample(directory, seed)
        if sample is None:
            raise ValueError(f'Unverified or incomplete sample: {directory.name}')
        if not sample.get('error'):
            validation = json.loads((directory / 'validation.json').read_text())
            sample['rule_status'] = {c['rule_id']: c['status'] for c in validation['checks']}
        samples.append(sample)
    summary = {'experiment_id': root.name, 'directory': str(root), 'configuration_hash': expected,
               'attempted': len(samples), 'accepted': sum(s['accepted'] for s in samples),
               'operational_pass': sum(s['operational_pass'] for s in samples),
               'failed_or_unaccepted': sum(not s['accepted'] for s in samples), 'samples': samples,
               'execution_status': 'interrupted' if any(s.get('execution_status') in INTERRUPTED_STATUSES for s in samples) else 'completed',
               'execution_started_count': sum(s.get('execution_started', True) for s in samples),
               'specification': manifest['spec']}
    summary['verified_report'] = render_report(summary)
    return summary
