"""Recover an interrupted frozen batch in a new directory with paired replay.

Numerical sources, factors, random seeds, contracts and solver caps are unchanged.
All interrupted artifacts remain in the parent. Their unavailable actual work is
reported as unknown; it is never fabricated as zero or folded into solver timing.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor,as_completed
import fcntl
import json
from pathlib import Path
import shutil
import sys
import time

from feeder_agents.artifacts import atomic_json,digest,file_digest
from feeder_agents.benchmark.response_trials import verify_frozen_suite,source_inventory,run_response_trial


def recovery_selection(jobs,completed,started):
    expected={j['job_id']:j for j in jobs};ids=[r['job_id'] for r in completed];started=set(started)
    if len(ids)!=len(set(ids)) or not set(ids)<=started or not started<=set(expected):
        raise ValueError('Duplicate, unknown or untracked attempt identities')
    interrupted=started-set(ids)
    blocks={(expected[k]['public_task']['network_id'],expected[k]['budget']) for k in interrupted}
    replay={k for k,j in expected.items() if k not in ids or (j['public_task']['network_id'],j['budget']) in blocks}
    return dict(interrupted_ids=sorted(interrupted),affected_network_budgets=sorted(blocks),
        replay_ids=sorted(replay),retained_ids=sorted(set(ids)-replay),replaced_completed_ids=sorted(set(ids)&replay))


def execute(job,output,reference):
    return run_response_trial(job['public_task'],job['arm'],job['random_seed'],job['budget'],output,reference=reference)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original',required=True);parser.add_argument('--output',required=True)
    parser.add_argument('--workers',type=int,default=16)
    args=parser.parse_args();original=Path(args.original).resolve();output=Path(args.output).resolve()
    suite=verify_frozen_suite(original)
    if digest(source_inventory()[1])!=suite['source_hash']:raise ValueError('Use the original frozen source on PYTHONPATH')
    if output.exists():raise ValueError('Use a new recovery directory; preserve earlier attempts')
    before=file_digest(original/'results.json')
    saved=json.loads((original/'results.json').read_text())['rows']
    complete={r['job_id']:r for r in saved};started=set(complete)
    for folder in (original/'trials').iterdir():
        started.add(folder.name)
        lock=folder/'response_designs/design/.lock'
        if lock.exists():
            with lock.open('r+') as stream:
                try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise ValueError('An original worker is still active; do not duplicate it')
        if (folder/'row.json').exists():
            row=json.loads((folder/'row.json').read_text())
            if row['job_id']!=folder.name:raise ValueError('Directory/row identity mismatch')
            if row['job_id'] in complete and complete[row['job_id']]!=row:raise ValueError('Conflicting saved row')
            complete[row['job_id']]=row
    rows=list(complete.values());plan=recovery_selection(suite['jobs'],rows,started)
    if file_digest(original/'results.json')!=before:raise ValueError('Original ledger is still changing')
    if not plan['replay_ids']:raise ValueError('No missing or interrupted block to recover')
    output.mkdir(parents=True)
    for name in ('source','execution_scripts','construction_evidence'):shutil.copytree(original/name,output/name)
    for name in ('suite.json','suite_checksum.json','execution_scripts.json'):shutil.copyfile(original/name,output/name)
    runner=output/'execution_scripts'/Path(__file__).name;shutil.copyfile(__file__,runner)
    replaced=[complete[k] for k in plan['replaced_completed_ids']]
    recovery=dict(**plan,parent=str(original),parent_results_sha256=before,source_hash=suite['source_hash'],
        recovery_runner_sha256=file_digest(runner),original_completed_rows=len(rows),workers=args.workers,
        original_completed_search_ac_calls=sum(r['search_ac_calls'] for r in rows),
        original_completed_total_ac_calls=sum(r['total_ac_calls'] for r in rows),
        replaced_completed_search_ac_calls=sum(r['search_ac_calls'] for r in replaced),
        replaced_completed_total_ac_calls=sum(r['total_ac_calls'] for r in replaced),
        interrupted_actual_ac_calls=None,
        reason='Execution session unavailable, stale ledger, and no live attempt locks. No numerical source change.',
        accounting_note='Report final completed-trial work separately from campaign work. Interrupted attempt totals are unavailable. All original outcomes and partial files remain preserved.')
    atomic_json(output/'recovery_plan.json',recovery)
    atomic_json(output/'interrupted_parent_results.json',dict(rows=rows))
    atomic_json(output/'execution_started.json',dict(source_hash=suite['source_hash'],jobs=len(plan['replay_ids']),
        retained=len(plan['retained_ids']),workers=args.workers,started_unix=time.time(),recovery=True))
    selected=[complete[k] for k in plan['retained_ids']]
    atomic_json(output/'results.json',dict(expected=len(suite['jobs']),completed=len(selected),rows=selected))
    print('Recovery frozen:',len(selected),'retained,',len(plan['replay_ids']),'replayed/new;',
        len(plan['interrupted_ids']),'interrupted attempts preserved',flush=True)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for budget in suite['budgets']:
            jobs=[j for j in suite['jobs'] if j['job_id'] in plan['replay_ids'] and j['budget']==budget]
            futures={pool.submit(execute,j,str(output),suite['reference_bindings'][j['public_task']['network_id']]):j for j in jobs}
            for future in as_completed(futures):
                job=futures[future]
                try:row=future.result()
                except Exception as exc:
                    # A killed worker has unknown actual work; the final audit must fail.
                    atomic_json(output/('worker_error_'+job['job_id']+'.json'),dict(job=job,error=f'{type(exc).__name__}: {exc}',actual_ac_calls=None))
                    raise
                selected.append(row)
                atomic_json(output/'results.json',dict(expected=len(suite['jobs']),completed=len(selected),rows=sorted(selected,key=lambda r:r['job_id'])))
                print(len(selected),'/',len(suite['jobs']),row['task_id'],row['arm'],row['random_seed'],row['success'],row.get('error',''),flush=True)
    atomic_json(output/'execution_complete.json',dict(completed=len(selected),expected=len(suite['jobs']),
        execution_errors=sum(r['outcome'] in ('execution_error','worker_error') for r in selected),
        infrastructure_interrupted_attempts=len(plan['interrupted_ids']),completed_unix=time.time()))
    sys.path.insert(0,str(output/'execution_scripts'))
    from summarize_m96_response_mechanisms import summarize
    result=summarize(output)
    atomic_json(output/'campaign_accounting.json',dict(completed_trial_search_ac_calls=result['search_ac_calls'],
        completed_trial_total_ac_calls=result['total_ac_calls'],
        replaced_completed_total_ac_calls=recovery['replaced_completed_total_ac_calls'],
        known_completed_attempt_total_ac_calls=result['total_ac_calls']+recovery['replaced_completed_total_ac_calls'],
        interrupted_attempts=len(plan['interrupted_ids']),interrupted_actual_ac_calls=None,exact_campaign_total_ac_calls=None))
    print('Final audit and figures complete:',result['trials'],'completed trials. Campaign interruption work remains unknown.',flush=True)


if __name__=='__main__':main()
