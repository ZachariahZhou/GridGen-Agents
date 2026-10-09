"""Reproducible research-task workflows; every planned attempt remains in the data."""
import argparse
import copy
import csv
import json
from pathlib import Path
import time

import yaml

from feeder_agents.artifacts import atomic_json,file_digest
from feeder_agents.research_tasks import run_research_task


def run(output,seeds):
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    examples=Path(__file__).resolve().parents[1]/'examples';plans=[]
    for task in ('voltage_control','static_pv_impact','transmission_transfer'):
        base=yaml.safe_load((examples/f'research_{task}.yaml').read_text())
        for seed in seeds:
            plan=copy.deepcopy(base);plan['base_spec']['seed']=seed
            plans.append(dict(case_id=f'{task}_{seed}',plan=plan))
    atomic_json(output/'suite.json',dict(protocol='Three research tasks, fixed seeds, unchanged standard response objectives; finite task experiments gate every candidate. No replacement of failed seeds. Local derivative targets and finite downstream effects are separately recorded.',plans=plans))
    rows=[]
    for item in plans:
        plan=item['plan'];start=time.monotonic()
        row=dict(case_id=item['case_id'],task=plan['task'],seed=plan['base_spec']['seed'],n_buses=plan['base_spec']['n_buses'])
        try:
            result=run_research_task(plan,output,item['case_id']);root=Path(result['directory']);inner=root/'response_designs/response'
            before=json.loads((inner/'baseline/task/task_validation.json').read_text())
            initial=json.loads((inner/'baseline/response.json').read_text())
            selected=result['selected'];target=selected['targets'][0];probe='task_response'
            row.update(target_met=result['target_met'],electrical_base_accepted=result['electrical_base_accepted'],
                task_experiment_passed=result['task_validation']['accepted'],verification_passed=result['verification_passed'],
                contract_preserved=result['protected_contract_preserved'],response_ratio=target['observed'],
                response_lower=target['target']['lower'],response_upper=target['target']['upper'],
                original_response=initial['probes'][probe]['value'],selected_response=selected['probes'][probe]['value'],
                response_unit=selected['probes'][probe]['unit'],
                original_task_metrics=before['metrics'],selected_task_metrics=result['task_validation']['metrics'],
                accepted_steps=result['accepted_steps'],stop_reason=result['stop_reason'],
                source_hash=json.loads((inner/'manifest.json').read_text())['source_hash'],
                baseline_hash=file_digest(inner/('baseline/model/case.json' if plan['task']=='transmission_transfer' else 'baseline/model/feeder.json')),
                result_hash=result['result_hash'])
        except Exception as exc:row.update(target_met=False,error=f'{type(exc).__name__}: {exc}')
        row['seconds']=round(time.monotonic()-start,3);rows.append(row)
        atomic_json(output/'results.json',dict(rows=rows))
        print(row['case_id'],row['target_met'],row.get('response_ratio'),row.get('stop_reason',row.get('error')),flush=True)
    fields=list(dict.fromkeys(k for row in rows for k in row if not k.endswith('_metrics')))
    with (output/'results.csv').open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
        writer.writerows({k:r.get(k) for k in fields} for r in rows)
    return rows


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--seeds',type=int,nargs='+',default=[41,42,43]);args=parser.parse_args();run(args.output,args.seeds)
