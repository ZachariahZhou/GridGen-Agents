"""Isolated paired replay of M97 public conversations, with unchanged evaluators.

No live worker reads suite cases, certificates, answer keys or private witnesses.
The existing M97 recorder runs against the separately frozen production package.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from feeder_agents.artifacts import atomic_json, digest, file_digest

EVALUATORS = (
    'benchmark/m97_language.py', 'benchmark/m97_language_runtime.py',
    'benchmark/m97_language_analysis.py', 'benchmark/m97_language_semantic.py',
)
SETTINGS = dict(timeout=40, max_retries=0, max_tokens=6500, disable_thinking=True)


def read(path):
    return json.loads(Path(path).read_text())


def key(row):
    return row['case_id'], row['arm'], int(row['repetition'])


def job_name(case_id, arm, repetition):
    return f'{case_id}__{arm}__r{repetition}'


def validate_public(cases, public):
    expected = [dict(id=case['id'], messages=case['messages']) for case in cases]
    if public != expected:
        raise ValueError('Frozen public inputs differ or contain non-public fields')


def select_jobs(suite, scope='all', case_ids=None, repetitions=None, arms=None):
    identifiers = {case['id'] for case in suite['cases']}
    if case_ids and set(case_ids) - identifiers:
        raise ValueError(f'Unknown case IDs: {sorted(set(case_ids) - identifiers)}')
    repetitions = suite['repetitions'] if repetitions is None else repetitions
    if not repetitions or set(repetitions) - set(suite['repetitions']):
        raise ValueError('Unknown or empty repetition selection')
    arms = ['full_outer'] if arms is None else arms
    if not arms or set(arms) - {'full_outer', 'one_proposal'}:
        raise ValueError('Unknown or empty arm selection')
    if scope not in {'all', 'positives', 'negatives'}:
        raise ValueError('Unknown scope')
    cases = [case for case in suite['cases'] if not case_ids or case['id'] in case_ids]
    cases = [case for case in cases if scope == 'all' or
             (scope == 'positives') == (case['expected_outcome'] == 'generation')]
    if not cases:
        raise ValueError('Selection contains no cases')
    return [(case['id'], arm, rep) for case in cases for rep in sorted(set(repetitions))
            for arm in dict.fromkeys(arms)]


def verify(root, check_model=False):
    root = Path(root).resolve()
    suite = read(root / 'suite.json')
    if digest(suite) != read(root / 'suite_checksum.json')['sha256']:
        raise ValueError('M98 suite checksum changed')
    validate_public(suite['cases'], read(root / 'public_cases.json'))
    if file_digest(root / 'public_cases.json') != suite['paired_public_file_sha256']:
        raise ValueError('Public input file changed')
    for relative, expected in suite['source_inventory'].items():
        if file_digest(root / 'source/feeder_agents' / relative) != expected:
            raise ValueError(f'Frozen source changed: {relative}')
    for relative, expected in suite['paired_files'].items():
        if file_digest(Path(suite['paired_root']) / relative) != expected:
            raise ValueError(f'Paired evidence changed: {relative}')
    if file_digest(root / 'execution_scripts/run_m98_outer_retest.py') != suite['execution_script_hash']:
        raise ValueError('Frozen launch script changed')
    if check_model:
        model = configured_model()
        if model.model_name != suite['model'] or digest(str(model.openai_api_base)) != suite['endpoint_hash']:
            raise ValueError('Configured model or endpoint differs from the paired M97 settings')
    return suite


def configured_model():
    from feeder_agents.agent import configured_model as factory
    return factory(**SETTINGS)


def freeze(root, paired_root, source_root):
    """Copy source and exact public prompts; never construct new witnesses."""
    from .response_trials import runtime_versions
    root, paired_root, source_root = map(lambda p: Path(p).resolve(), (root, paired_root, source_root))
    if root == paired_root or paired_root in root.parents or root.exists():
        raise ValueError('M98 destination must be a new folder outside the immutable paired root')
    original = read(paired_root / 'suite.json')
    if digest(original) != read(paired_root / 'suite_checksum.json')['sha256']:
        raise ValueError('Paired suite checksum changed')
    validate_public(original['cases'], read(paired_root / 'public_cases.json'))
    if original['model_settings'] != SETTINGS or original['conversation_timeout_seconds'] != 180:
        raise ValueError('M97 settings differ from the shared recorder settings')
    model = configured_model()  # Construction reads .env; this performs no API call.
    if model.model_name != original['model'] or digest(str(model.openai_api_base)) != original['endpoint_hash']:
        raise ValueError('Actual .env/process model or endpoint differs from M97')
    package = source_root / 'src/feeder_agents'
    if not package.exists():
        package = source_root / 'feeder_agents'
    if not (package / 'agent.py').exists():
        raise ValueError('Source root must contain src/feeder_agents or feeder_agents')
    inventory = {str(path.relative_to(package)): file_digest(path)
                 for path in sorted(package.rglob('*')) if path.suffix in {'.py', '.json'}}
    paired_files = {name: file_digest(paired_root / name) for name in (
        'suite.json', 'suite_checksum.json', 'public_cases.json',
        'analysis/metrics.csv', 'analysis/semantic_equivalence.json',
        'analysis/certificate_audit.json')}
    root.mkdir(parents=True)
    for relative in inventory:
        destination = root / 'source/feeder_agents' / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(package / relative, destination)
    script = Path(__file__).resolve().parents[3] / 'scripts/run_m98_outer_retest.py'
    (root / 'execution_scripts').mkdir()
    shutil.copyfile(script, root / 'execution_scripts' / script.name)
    shutil.copyfile(paired_root / 'public_cases.json', root / 'public_cases.json')
    (root / 'analysis').mkdir()
    shutil.copyfile(paired_root / 'analysis/certificate_audit.json', root / 'analysis/certificate_audit.json')
    suite = dict(original, study='m98_outer_retest', arms=['full_outer'],
        frozen_at=datetime.now(timezone.utc).isoformat(), source_root=str(source_root),
        retest_runtime_versions=runtime_versions(),
        paired_root=str(paired_root), paired_files=paired_files,
        paired_public_file_sha256=file_digest(paired_root / 'public_cases.json'),
        source_inventory=inventory, source_hash=digest(inventory),
        execution_script_hash=file_digest(script),
        evaluator_hashes={name: inventory[name] for name in EVALUATORS},
        replay_policy='Identical frozen public messages; original paired labels are evaluator-only; no private witness files copied; every selected outcome is retained.')
    atomic_json(root / 'suite.json', suite)
    atomic_json(root / 'suite_checksum.json', {'sha256': digest(suite)})
    return {'root': str(root), 'source_hash': suite['source_hash'], 'model': suite['model'],
            'cases': len(suite['cases']), 'default_jobs': len(select_jobs(suite))}


def record_worker_exit(folder, returncode, elapsed):
    folder = Path(folder)
    path = folder / 'worker_result.json'
    if path.exists():
        return read(path)
    result = dict(status='timeout' if returncode == -9 else 'worker_crash',
                  seconds=elapsed, returncode=returncode)
    atomic_json(path, result)
    return result


def worker(root, case_id, arm, repetition):
    # Do not open suite.json here: the worker receives only exact public messages.
    from .m97_language_runtime import worker as recorded_worker
    return recorded_worker(root, case_id, arm, repetition)


def launch(root, workers=8, scope='all', case_ids=None, repetitions=None, arms=None, dry_run=False):
    root = Path(root).resolve()
    suite = verify(root, check_model=not dry_run)
    jobs = select_jobs(suite, scope, case_ids, repetitions, arms)
    if workers < 1 or workers > suite['workers']:
        raise ValueError('Worker count must be between 1 and the frozen M97 limit of 8')
    if dry_run:
        return {'selected': len(jobs), 'jobs': [job_name(*job) for job in jobs], 'live_api_calls': 0}
    selection = dict(started_at=datetime.now(timezone.utc).isoformat(), jobs=jobs,
                     selected=len(jobs), workers=workers, timeout=180)
    launch_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    atomic_json(root / 'launches' / f'{launch_id}.json', selection)
    cases = {case['id']: case for case in suite['cases']}

    def execute(job):
        case_id, arm, rep = job
        folder = root / 'jobs' / job_name(*job)
        folder.mkdir(parents=True, exist_ok=True)
        if not (folder / 'worker_result.json').exists():
            if (folder / 'events.jsonl').exists():
                # Interrupted jobs are outcomes. Never append a second live run to a trace.
                atomic_json(folder / 'worker_result.json', dict(status='interrupted', seconds=None))
            else:
                started = time.monotonic()
                with (folder / 'worker.log').open('w') as stream:
                    process = subprocess.Popen([sys.executable,
                        str(root / 'execution_scripts/run_m98_outer_retest.py'), '--root', str(root),
                        '--worker', case_id, arm, str(rep)], stdout=stream,
                        stderr=subprocess.STDOUT, start_new_session=True)
                    try:
                        code = process.wait(timeout=180)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                        code = -9
                record_worker_exit(folder, code, time.monotonic() - started)
        try:
            from .m97_language_runtime import collect_job
            if not (folder / 'independent_grade.json').exists():
                collect_job(root, cases[case_id], arm, rep)
            outcome = dict(case_id=case_id, arm=arm, repetition=rep,
                           status=read(folder / 'worker_result.json')['status'])
        except Exception as exc:
            outcome = dict(case_id=case_id, arm=arm, repetition=rep,
                           status=read(folder / 'worker_result.json')['status'],
                           evaluation_error=f'{type(exc).__name__}: {exc}')
        atomic_json(folder / 'job_outcome.json', outcome)
        print(json.dumps(outcome), flush=True)
        return outcome

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(execute, job): job for job in jobs}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                case_id, arm, rep = futures[future]
                folder = root / 'jobs' / job_name(case_id, arm, rep)
                failure = dict(status='launcher_error', seconds=None, error_type=type(exc).__name__, error=str(exc))
                atomic_json(folder / 'launcher_error.json', failure)
                if not (folder / 'worker_result.json').exists():
                    atomic_json(folder / 'worker_result.json', failure)
                atomic_json(folder / 'job_outcome.json', dict(case_id=case_id, arm=arm, repetition=rep,
                    status='launcher_error', evaluation_error=f'{type(exc).__name__}: {exc}'))
    return report(root)


def semantic_rows(root, suite, job_paths=None):
    """Apply the existing last-artifact semantic policy to every recorded research job."""
    from .m97_language_semantic import audit_artifact
    cases = {case['id']: case for case in suite['cases']}
    rows = []
    paths = sorted((Path(root) / 'jobs').glob('*/independent_grade.json')) if job_paths is None else job_paths
    for path in paths:
        row = read(path)['row']
        case = cases[row['case_id']]
        if not case.get('task') or case['expected_outcome'] != 'generation':
            continue
        authorization = read(Path(root) / 'analysis/authorization' / path.parent.name / 'audit.json')['authorization']
        records = []
        for response in sorted(path.parent.glob('workspace/projects/conversation/**/selected/response.json'),
                               key=lambda p: p.stat().st_mtime_ns):
            folder = response.parent.parent
            if not (folder / 'manifest.json').exists() or 'plan' not in read(folder / 'manifest.json'):
                continue
            try:
                records.append(audit_artifact(case, folder, authorization))
            except Exception as exc:
                records.append(dict(directory=str(folder), passed=False, error=f'{type(exc).__name__}: {exc}'))
        fulfilled = bool(records and records[-1]['passed'])
        rows.append(dict(case_id=row['case_id'], arm=row['arm'], repetition=row['repetition'],
            status=row['status'], task=case['task'], frozen_generation_success=row['generation_success'],
            semantic_artifact_fulfillment=fulfilled, semantic_fulfilled_and_completed=fulfilled and row['status']=='completed',
            any_semantic_artifact=any(record['passed'] for record in records),
            response_finite_evidence=any(record.get('response_finite_evidence', False) for record in records), artifacts=records))
    return rows


def csv_rows(path):
    with Path(path).open() as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        for field, value in row.items():
            if value in {'True', 'False'}:
                row[field] = value == 'True'
        row['repetition'] = int(row['repetition'])
    return rows


def paired_summary(rows, prior):
    old = {key(row): row for row in prior}
    positives = [row for row in rows if row.get('primary_positive')]
    pairs = [(old[key(row)], row) for row in positives if key(row) in old]
    unknown = [(before, after) for before, after in pairs if after.get('semantic_artifact_fulfillment') is None]
    complete_pairs = [(before, after) for before, after in pairs if after.get('semantic_artifact_fulfillment') is not None]
    negatives = [row for row in rows if not row.get('primary_positive')]
    return dict(observed=len(rows), positive_n=len(positives), negative_n=len(negatives),
        artifact_fulfilled=sum(bool(row.get('semantic_artifact_fulfillment')) for row in positives),
        fulfilled_and_completed=sum(bool(row.get('semantic_fulfilled_and_completed')) for row in positives),
        evaluation_errors=sum(bool(row.get('evaluation_error')) for row in rows),
        matched_positive_n=len(pairs), artifact_unknown_pairs=len(unknown),
        paired_prior_artifact_fulfilled=sum(bool(before['semantic_artifact_fulfillment']) for before, after in pairs),
        paired_prior_fulfilled_and_completed=sum(bool(before['semantic_fulfilled_and_completed']) for before, after in pairs),
        artifact_improvements=sum(not before['semantic_artifact_fulfillment'] and bool(after['semantic_artifact_fulfillment']) for before, after in complete_pairs),
        artifact_regressions=sum(bool(before['semantic_artifact_fulfillment']) and not after['semantic_artifact_fulfillment'] for before, after in complete_pairs),
        negative_decision_reason_proxy=sum(bool(row.get('postrun_negative_decision_reason_proxy')) for row in negatives),
        unauthorized_committed=sum(bool(row.get('authorization_committed_any')) for row in rows),
        statuses=dict(Counter(row['status'] for row in rows)),
        model_calls=sum(int(row.get('llm_calls') or 0) for row in rows),
        tokens=sum(int(row.get('tokens') or 0) for row in rows))


def report(root):
    from .m97_language_analysis import (authorization_audit, revision_audit,
        negative_reason_audit, usage_from_events, network_key, request_key)
    root = Path(root).resolve()
    suite = verify(root)
    cases = {case['id']: case for case in suite['cases']}
    rows, equivalents = [], []
    for folder in sorted((root / 'jobs').glob('*')):
        if not folder.is_dir(): continue
        case_id, arm, repeat = folder.name.split('__')
        case = cases[case_id]
        row = dict(case_id=case_id, arm=arm, repetition=int(repeat[1:]),
            primary_positive=bool(case['expected_outcome']=='generation' and case.get('witness',{}).get('certified')),
            expected_outcome=case['expected_outcome'])
        events, malformed = [], []
        if (folder / 'events.jsonl').exists():
            for number, line in enumerate((folder / 'events.jsonl').read_text().splitlines(), 1):
                try: events.append(json.loads(line))
                except ValueError: malformed.append(number)
        usage = usage_from_events(events)
        row.update(status='worker_crash', semantic_artifact_fulfillment=None,
            semantic_fulfilled_and_completed=False, llm_calls=sum(event['kind']=='model_start' for event in events),
            tokens=usage.get('total_tokens'), **usage)
        try:
            raw = read(folder / 'worker_result.json')
            row['status'] = raw['status']
            if malformed:
                raise ValueError(f'Malformed preserved event lines: {malformed}')
            grade = read(folder / 'independent_grade.json')
            row.update(grade['row'])
            row.update(usage, tokens=usage.get('total_tokens') if usage['usage_calls_observed'] else None,
                network_id=network_key(case), request_id=request_key(case),
                research_task=case.get('task') or 'ordinary_generation',
                outer_model_calls=sum(event['kind']=='model_start' and 'outer_model' in (event.get('tags') or []) for event in events),
                internal_model_calls=sum(event['kind']=='model_start' and 'outer_model' not in (event.get('tags') or []) for event in events),
                provider_error_events=sum(event['kind']=='model_error' for event in events))
            authorization = authorization_audit(case, events, folder / 'workspace/projects/conversation', raw)
            revision = revision_audit(case, grade, events)
            negative = negative_reason_audit(case, events, raw, grade.get('artifacts', []))
            atomic_json(root / 'analysis/authorization' / folder.name / 'audit.json',
                dict(authorization=authorization, revision=revision, negative_reason=negative))
            attempted = authorization['attempted_any'] or row['unauthorized_attempted'] > 0
            committed = authorization['committed_any'] or row['unauthorized_committed'] > 0
            delivered = authorization['final_narrative_delivered_unauthorized_any']
            audited = bool(row['generation_success'] and revision['passed'] and not attempted and not committed and not delivered)
            row.update(frozen_generation_success=row['generation_success'], audited_generation_success=audited,
                postrun_revision_audit_pass=revision['passed'], authorization_attempted_any=attempted,
                effective_authorization_attempt_any=authorization['effective_attempted_any'],
                authorization_committed_any=committed, authorization_delivered_any=delivered,
                authorization_tool_result_exposed_any=authorization['tool_result_exposed_unauthorized_any'],
                postrun_negative_decision_reason_proxy=negative.get('expected_decision_and_reason'),
                unexpected_artifact_on_negative_request=bool(case['expected_outcome'] in {'clarification','unsupported'} and grade.get('artifacts')),
                semantic_artifact_fulfillment=audited)
            equivalent = semantic_rows(root, suite, [folder / 'independent_grade.json'])
            if equivalent:
                equivalents.extend(equivalent)
                row['semantic_artifact_fulfillment'] = equivalent[0]['semantic_artifact_fulfillment']
            row['semantic_equivalence_audit_applied'] = bool(equivalent)
            row['semantic_fulfilled_and_completed'] = bool(row['semantic_artifact_fulfillment'] and row['status']=='completed')
        except Exception as exc:
            row.update(evaluation_error=f'{type(exc).__name__}: {exc}',
                       semantic_artifact_fulfillment=None, semantic_fulfilled_and_completed=False)
        rows.append(row)
    atomic_json(root / 'analysis/semantic_equivalence.json', dict(
        scope='Existing M97 semantic-equivalence/post-run evaluators, uniformly applied to every observed M98 job.',
        evaluator_hashes=suite['evaluator_hashes'], rows=equivalents))
    rows.sort(key=key)
    fields = sorted({field for row in rows for field in row})
    with (root / 'analysis/metrics.csv').open('w') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({name: json.dumps(value) if isinstance(value, (dict, list)) else value for name, value in row.items()} for row in rows)
    previous = csv_rows(Path(suite['paired_root']) / 'analysis/metrics.csv')
    summary = paired_summary(rows, previous)
    atomic_json(root / 'analysis/summary.json', summary)
    atomic_json(root / 'analysis/paired_outcomes.json', [dict(
        case_id=row['case_id'], arm=row['arm'], repetition=row['repetition'], current=row,
        previous=next((old for old in previous if key(old)==key(row)), None)) for row in rows])
    return summary


def trace_causes(row, events, raw, grade, semantic):
    """Overlapping observations from events/physical checks, never frozen failure_class."""
    causes = set(row.get('diagnosis_flags') or [])
    model_errors = [event for event in events if event['kind']=='model_error']
    tool_errors = [event for event in events if event['kind']=='tool_error']
    if any('timeout' in (event.get('error_type','') + event.get('error','')).lower() or
           'timed out' in event.get('error','').lower() for event in model_errors):
        causes.add('provider_timeout')
    if row.get('status') == 'timeout':
        causes.add('conversation_timeout')
        finished = {event.get('run_id') for event in events if event['kind'] in {'model_end','model_error','tool_end','tool_error'}}
        pending = [event for event in events if event['kind']=='model_start' and event.get('run_id') not in finished]
        if any('outer_model' in (event.get('tags') or []) for event in pending):
            causes.add('pending_outer_model_at_timeout')
        if any('internal_model' in (event.get('tags') or []) for event in pending):
            causes.add('pending_internal_model_at_timeout')
    if any(event.get('error_type')=='FileNotFoundError' for event in tool_errors):
        causes.add('result_reader_missing_file')
    if any(event.get('error_type')=='ValidationError' for event in tool_errors):
        causes.add('schema_validation_error')
    if any('Invalid response design ID' in event.get('error','') for event in tool_errors):
        causes.add('invalid_result_identifier')
    if any('Limit' in turn.get('error_type','') or 'limits exceeded' in turn.get('error','') for turn in raw.get('turns', [])):
        causes.add('outer_call_limit')
    if row.get('effective_authorization_attempt_any'):
        causes.add('forbidden_action_attempt')
    if row.get('authorization_committed_any'):
        causes.add('unauthorized_selected_change')
    if not row.get('semantic_artifact_fulfillment'):
        artifacts = grade.get('artifacts', [])
        if not artifacts:
            causes.add('no_saved_artifact')
        if artifacts and not any(artifact.get('accepted') and artifact.get('usable') for artifact in artifacts):
            causes.add('no_accepted_runnable_artifact')
        if row.get('revision_conversation') and not row.get('postrun_revision_audit_pass'):
            causes.add('required_revision_not_completed')
        records = semantic.get('artifacts', [])
        if row.get('research_task') not in {None, 'ordinary_generation'} and artifacts and not records:
            causes.add('saved_model_without_response_experiment')
        if records:
            last = records[-1]
            for probe in last.get('probes', []):
                checks = probe.get('checks', {})
                finite = probe.get('finite_checks', {})
                if not checks.get('original_target_bounds', True): causes.add('original_target_bounds_changed')
                if not checks.get('actual_full_half_targets', True): causes.add('actual_response_target_not_met')
                if not finite.get('recorded_full_pv_off_on_experiment', True): causes.add('finite_pv_off_on_missing')
                if not checks.get('finite_requested_experiment', True) and finite.get('recorded_full_pv_off_on_experiment', True): causes.add('finite_task_other_missing_or_invalid')
    return sorted(causes)
