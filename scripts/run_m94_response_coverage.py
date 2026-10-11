"""Unchanged M93 tasks and separately certified, witness-hidden search tasks."""
import argparse
import copy
import csv
import hashlib
import json
import shutil
from concurrent.futures import ProcessPoolExecutor,as_completed
from pathlib import Path

from feeder_agents.artifacts import atomic_json,digest
from feeder_agents.response_design import apply_response_action,research_contract_preserved,score_response
from feeder_agents.response_measurement import create_model,measure,resolve_probes
from feeder_agents.response_schema import ResponseDesignPlan
from feeder_agents.solver_accounting import SolverMeter
from run_m91_response_search import execute


def read(path):return json.loads(Path(path).read_text())


def source_record(output):
    import feeder_agents
    package=Path(feeder_agents.__file__).parent
    hashes={str(p.relative_to(package)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(package.rglob('*')) if p.suffix in ('.py','.json')}
    for relative in hashes:
        target=output/'source/feeder_agents'/relative;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(package/relative,target)
    return dict(source_hash=digest(hashes),files=hashes)


def build_witness_suite(output,template):
    from feeder_agents.response_geometry import compensation_scale_interval
    networks=((23,1601,'urban'),(39,1613,'rural'),(57,1621,'urban'),(71,1637,'rural'))
    protocol=dict(networks=networks,primary_factor_fraction=.7,secondary_factor_fraction=.6,
        widths=[.10,.30],absolute_width_floor=.0005,geometry_budget=.5,displacement_km=.2,
        actions=['scale_subtree:b1:increase','scale_subtree:b9:decrease'],
        purpose='Construct feasible interval requests around predetermined legal edits; never pass witness models or actions to the search.')
    atomic_json(output/'witness_construction_protocol.json',protocol)
    tasks=[];certificates=[]
    for buses,seed,scene in networks:
        plan=copy.deepcopy(template);plan['base_spec'].update(n_buses=buses,seed=seed)
        plan['base_spec']['scenario'].update(kind=scene,engineering_profile=scene)
        plan['search'].update(max_layout_scale_change=.5,max_branch_length_change=.5,max_node_displacement_km=.2)
        plan['research_question']='Reconstruct an attainable coupled Q-voltage response from the original grid under fixed permissions.'
        parsed=ResponseDesignPlan.model_validate(plan)
        folder=output/'witnesses'/f'{buses}_{seed}';folder.mkdir(parents=True,exist_ok=True)
        with SolverMeter() as meter:
            original=create_model(parsed.base_spec);probes=resolve_probes(original,parsed.probes)
            baseline=measure(original,parsed.base_spec,probes,folder/'baseline')
            trial=original;moves=[]
            for child,fraction,increase in [('b1',.7,True),('b9',.6,False)]:
                move=dict(kind='scale_subtree',child_bus=child)
                interval=compensation_scale_interval(trial,original,parsed.base_spec,parsed.search,move)
                if interval['empty']:raise ValueError(f'Predeclared witness direction has an empty interval: {buses}, {child}')
                move['factor']=1+fraction*((interval['upper'] if increase else interval['lower'])-1)
                trial=apply_response_action(trial,parsed.base_spec,move);moves.append(move)
            preserved=research_contract_preserved(original,trial,parsed)
            measured=measure(trial,parsed.base_spec,probes,folder/'candidate')
            half=measure(trial,parsed.base_spec,probes,folder/'half_step',step_factor=.5)
            ratios={key:measured['probes'][key]['value']/baseline['probes'][key]['value'] for key in ('a','b')}
            for width in protocol['widths']:
                task=copy.deepcopy(plan)
                for target in task['targets']:
                    center=ratios[target['probe_id']];radius=max(protocol['absolute_width_floor'],width*abs(center-1))
                    target.update(lower=max(0.,center-radius),upper=center+radius)
                typed=ResponseDesignPlan.model_validate(task)
                scored=score_response(measured,typed.targets,baseline);verified=score_response(half,typed.targets,baseline)
                consistency=all(all(abs(v-half['probes'][key]['signed_response'][port]) <=
                    1e-7+typed.verification_relative_tolerance*max(abs(v),abs(half['probes'][key]['signed_response'][port]))
                    for port,v in measured['probes'][key]['signed_response'].items()) for key in ('a','b'))
                certificate=dict(case_id=f'witness_{buses}_{seed}_w{round(width*100)}',plan_hash=digest(task),
                    contract_preserved=preserved,witness_target_met=scored['target_met'],
                    half_step_target_met=verified['target_met'],step_consistency=consistency,
                    nontrivial=not score_response(baseline,typed.targets,baseline)['target_met'],
                    response_ratios=ratios,half_step_ratios=[r['observed'] for r in verified['targets']],
                    original_hash=digest(original['feeder'].model_dump()),witness_hash=digest(trial['feeder'].model_dump()),
                    witness_actions=moves,witness_directory=str(folder))
                certificates.append(certificate)
                atomic_json(output/'witness_certificates.json',dict(rows=certificates))
                # Invalid constructions abort transparently; never replace a
                # seed or discard a hard task according to a search outcome.
                if not (preserved and scored['target_met'] and verified['target_met'] and consistency and certificate['nontrivial']):
                    raise ValueError('Predetermined witness did not validate; inspect certificate, do not silently replace the task')
                tasks.append(dict(case_id=certificate['case_id'],plan=task))
            atomic_json(folder/'original.json',original['feeder'].model_dump(mode='json'))
            atomic_json(folder/'witness.json',trial['feeder'].model_dump(mode='json'))
            atomic_json(folder/'construction_accounting.json',meter.snapshot())
    return tasks


def run_job(job):
    row=execute(job)
    if row.get('directory'):
        root=Path(row['directory']);result=read(root/'result.json')
        row['source_hash']=read(root/'manifest.json')['source_hash']
        row['duplicates_before_preview']=sum(entry.get('reason')=='duplicate_before_preview'
            for path in (root/'proposals').glob('*.json') for entry in read(path)['screened'])
        row['authority_warning_probes']=[p['probe_id'] for p in result['diagnosis'].get('adjustment_authority',{}).get('probes',[])
            if p.get('geometry_only_target_outside_linear_scenario')]
    row['plan_hash']=digest(job[2])
    return row


def evaluate(tasks,arm,output,workers):
    # Check before copying runtime source or writing any evidence. This runner
    # has no resume protocol; even identical requests need a fresh directory.
    if output.exists() and any(output.iterdir()):
        raise ValueError('Use a new output directory to preserve prior benchmark evidence')
    output.mkdir(parents=True,exist_ok=True)
    manifest=dict(arm=arm,tasks=tasks,**source_record(output))
    atomic_json(output/'suite.json',manifest)
    rows=[]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures=[pool.submit(run_job,(t['case_id'],arm,t['plan'],str(output))) for t in tasks]
        for future in as_completed(futures):
            row=future.result();rows.append(row);rows.sort(key=lambda r:r['case_id'])
            atomic_json(output/'results.json',dict(rows=rows))
            print(arm,len(rows),'/',len(tasks),row['case_id'],row['success'],row.get('search_ac_calls'),row.get('error',''),flush=True)
    fields=list(dict.fromkeys(k for r in rows for k in r))
    with (output/'results.csv').open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader();writer.writerows(rows)
    return rows


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='experiments/m94_response_coverage')
    parser.add_argument('--m93',default='experiments/m93_bounded_compensation_final')
    parser.add_argument('--workers',type=int,default=2)
    parser.add_argument('--evaluate-tasks',help='Read task-only JSON; no witness is loaded by this mode')
    parser.add_argument('--arm',default='m94')
    args=parser.parse_args();output=Path(args.output).resolve()
    if args.evaluate_tasks:
        evaluate(read(args.evaluate_tasks)['tasks'],args.arm,output,args.workers);return
    if output.exists():raise ValueError('Use a new directory to preserve prior benchmark evidence')
    output.mkdir(parents=True)
    historical=read(Path(args.m93)/'suite.json')
    originals=[dict(case_id=j['case_id'],plan=j['plan']) for j in historical['jobs'] if j['arm']=='bounded']
    atomic_json(output/'protocol.json',dict(original_suite_hash=digest(historical),
        original_tasks=originals,scope='Unchanged 16-task rerun plus separate witness-defined tasks, including all outcomes',
        runtime=source_record(output)))
    witnesses=build_witness_suite(output,originals[0]['plan'])
    # Search input contains only requests, never a witness action/model/hash.
    atomic_json(output/'witness_tasks.json',dict(tasks=witnesses))
    atomic_json(output/'original_tasks.json',dict(tasks=originals))
    evaluate(originals,'m94',output/'original',args.workers)
    evaluate(witnesses,'m94',output/'known_feasible',args.workers)


if __name__=='__main__':main()
