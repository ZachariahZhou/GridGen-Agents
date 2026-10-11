"""Audit a frozen numerical batch with an explicit artifact-integrity scope.

No search, AC solves, numerical source changes or shortened integrity checks.
Full mode checks every intermediate artifact. Core mode checks all nontrial
artifacts and the winning trial; it does not claim complete auxiliary integrity.
Completed full-audit records may be reused with matching source/row provenance.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor,as_completed
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import time

from feeder_agents.artifacts import atomic_json,digest,file_digest
from feeder_agents.benchmark.response_trials import verify_frozen_suite,source_inventory
from feeder_agents.benchmark.response_summary import validate_rows


def load_auditor(parent):
    spec=importlib.util.spec_from_file_location('frozen_m96_auditor',Path(parent)/'execution_scripts/summarize_m96_response_mechanisms.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def read_core_result(root):
    root=Path(root).resolve();saved=json.loads((root/'result.json').read_text())
    checksum=saved.pop('result_hash')
    if digest(saved)!=checksum or Path(saved['directory']).resolve()!=root:
        raise ValueError('Response result or directory changed')
    winner=saved['winner'];checked=0
    required={name:sha for name,sha in saved['artifacts'].items()
        if not name.startswith('trials/') or name.startswith(f'trials/{winner}/')}
    if winner!='baseline' and not any(name.startswith(f'trials/{winner}/') for name in required):
        raise ValueError('Missing winning-trial artifacts')
    for name,sha in required.items():
        path=(root/name).resolve()
        if not path.is_relative_to(root) or not path.is_file() or file_digest(path)!=sha:
            raise ValueError('Response artifacts changed')
        checked+=1
    coverage=dict(scope='core',checked=checked,total=len(saved['artifacts']))
    read_core_result.last_coverage=coverage
    return dict(saved,result_hash=checksum,_artifact_coverage=coverage)


def check(payload,parent,mode):
    row,job,source_hash,reference=payload;module=load_auditor(parent)
    if mode=='core':module.read_response_result=read_core_result
    record=module.audit_record(row,job,source_hash,reference)
    result=None if row['outcome'] in ('execution_error','worker_error') else module.audit_trial(row,job,source_hash,reference)
    return dict(job_id=row['job_id'],row_hash=digest(row),invocation=record,result=result,audited_unix=time.time(),
        artifact_scope=mode,artifact_coverage=getattr(read_core_result,'last_coverage',None) if mode=='core' else None)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--input',required=True)
    parser.add_argument('--output',required=True);parser.add_argument('--workers',type=int,default=16)
    parser.add_argument('--mode',choices=['full','core'],default='full')
    parser.add_argument('--reuse-full-audit')
    args=parser.parse_args();parent=Path(args.input).resolve();output=Path(args.output).resolve()
    suite=verify_frozen_suite(parent)
    if digest(source_inventory()[1])!=suite['source_hash']:raise ValueError('Use frozen numerical source')
    data=json.loads((parent/'results.json').read_text());rows=data['rows'];validate_rows(suite['jobs'],rows)
    jobs={j['job_id']:j for j in suite['jobs']}
    output.mkdir(parents=True,exist_ok=False)
    # Immutable numerical snapshots are shared; audit outputs have their own paths.
    for name in ('source','execution_scripts','construction_evidence'):(output/name).symlink_to(parent/name,target_is_directory=True)
    for name in ('suite.json','suite_checksum.json','execution_scripts.json','results.json','recovery_plan.json','execution_complete.json'):
        if (parent/name).exists():shutil.copyfile(parent/name,output/name)
    atomic_json(output/'audit_execution.json',dict(parent=str(parent),workers=args.workers,source_hash=suite['source_hash'],
        runner_sha256=file_digest(Path(__file__)),frozen_auditor_sha256=file_digest(parent/'execution_scripts/summarize_m96_response_mechanisms.py'),
        result_ledger_sha256=file_digest(parent/'results.json'),started_unix=time.time(),
        artifact_scope=args.mode,reuse_full_audit=args.reuse_full_audit,
        scope='Numerical acceptance, contract and accounting checks are unchanged. Core mode omits unselected candidate artifact bytes and explicitly discloses incomplete auxiliary integrity.'))
    shutil.copyfile(__file__,output/'audit_m96_parallel.py')
    audited={}
    if args.reuse_full_audit:
        previous=Path(args.reuse_full_audit);execution=json.loads((previous.parent/'audit_execution.json').read_text())
        if execution['source_hash']!=suite['source_hash'] or execution['result_ledger_sha256']!=file_digest(parent/'results.json') or execution['frozen_auditor_sha256']!=file_digest(parent/'execution_scripts/summarize_m96_response_mechanisms.py'):
            raise ValueError('Prior audit source or input ledger differs')
        by_id={row['job_id']:row for row in rows}
        for value in json.loads(previous.read_text())['rows']:
            if value['job_id'] not in by_id or value['row_hash']!=digest(by_id[value['job_id']]):raise ValueError('Prior audit row differs')
            if value.get('artifact_scope','full')!='full':raise ValueError('Only complete per-trial artifact audits may be reused')
            audited[value['job_id']]=dict(value,artifact_scope='full')
        print('Retained',len(audited),'completed full-artifact audits',flush=True)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures={pool.submit(check,(row,jobs[row['job_id']],suite['source_hash'],suite['reference_bindings'][row['network_id']]),str(parent),args.mode):row['job_id'] for row in rows if row['job_id'] not in audited}
        for future in as_completed(futures):
            identity=futures[future]
            try:value=future.result()
            except Exception as exc:
                atomic_json(output/'audit_exception.json',dict(job_id=identity,error=f'{type(exc).__name__}: {exc}'));raise
            audited[identity]=value
            if len(audited)%32==0 or len(audited)==len(rows):
                atomic_json(output/'audit_checkpoint.json',dict(expected=len(rows),completed=len(audited),rows=list(audited.values())))
                print('Audited',len(audited),'/',len(rows),flush=True)
    module=load_auditor(parent)
    def recorded(which):
        def result(row,job,source_hash,reference=None):
            value=audited[row['job_id']]
            if value['row_hash']!=digest(row) or job!=jobs[row['job_id']] or source_hash!=suite['source_hash'] or reference!=suite['reference_bindings'][row['network_id']]:
                raise ValueError('Audit inputs changed after verification')
            return value[which]
        return result
    # The existing aggregator still checks every returned flag and all group,
    # manifest and pairing conditions. Only duplicate per-trial I/O is avoided.
    module.audit_record=recorded('invocation');module.audit_trial=recorded('result')
    result=module.summarize(output)
    scope=dict(core_trials_verified=len(audited),full_artifact_trials_verified=sum(v['artifact_scope']=='full' for v in audited.values()),
        all_auxiliary_artifacts_verified=all(v['artifact_scope']=='full' for v in audited.values()),
        excluded_in_core_mode='Raw artifacts of unselected candidate trials; their history/proposal/call ledgers and result manifest remain checked.',
        numeric_acceptance_unchanged=True,search_rerun=False)
    atomic_json(output/'verification_scope.json',scope)
    audit=json.loads((output/'evidence_audit.json').read_text());audit['verification_scope']=scope
    audit['all_passed_scope']='Recorded per-trial scope; not a full auxiliary-artifact integrity claim'
    atomic_json(output/'evidence_audit.json',audit)
    if (output/'recovery_plan.json').exists():
        recovery=json.loads((output/'recovery_plan.json').read_text())
        atomic_json(output/'campaign_accounting.json',dict(completed_trial_search_ac_calls=result['search_ac_calls'],
            completed_trial_total_ac_calls=result['total_ac_calls'],replaced_completed_total_ac_calls=recovery['replaced_completed_total_ac_calls'],
            known_completed_attempt_total_ac_calls=result['total_ac_calls']+recovery['replaced_completed_total_ac_calls'],
            interrupted_attempts=len(recovery['interrupted_ids']),interrupted_actual_ac_calls=None,exact_campaign_total_ac_calls=None))
    atomic_json(output/'audit_complete.json',dict(completed=len(audited),all_passed=True,verification_scope=scope,finished_unix=time.time()))
    print('Final audit and plots complete:',result['trials'],flush=True)


if __name__=='__main__':main()
