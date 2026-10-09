"""Paired design-space experiments, with fixed inputs and every attempt retained."""
import argparse
import copy
import csv
import json
from itertools import combinations
from pathlib import Path
import time

import networkx as nx

from feeder_agents.artifacts import atomic_json, file_digest
from feeder_agents.response_schema import ResponseDesignPlan
from feeder_agents.response_measurement import create_model
from feeder_agents.response_design import run_response_design


def make_plan(seed):
    return dict(research_question='Electrical response control under bounded research freedoms',
        base_spec=dict(network_kind='distribution',n_buses=25,voltage_kv=10,total_kw_min=360,total_kw_max=360,
            seed=seed,scenario=dict(kind='urban',engineering_profile='urban',layout='spatial_mst'),
            phase_design=dict(mode='unbalanced',single_phase_laterals=0,two_phase_laterals=0),
            repair_policy=dict(strategy='none')),
        probes=[dict(id='a',kind='voltage_p',step_mw=.01)],
        targets=[dict(probe_id='a',reference='baseline_ratio',lower=1.3,upper=1.5)],
        search=dict(allowed_actions=['replace_conductor','scale_layout'],max_layout_scale_change=.15,
            max_rounds=4,candidates_per_round=16))


def separated_ports(plan):
    """Choose by graph-path separation, before measuring responses or outcomes."""
    model=create_model(ResponseDesignPlan.model_validate(plan).base_spec);f=model['feeder']
    graph=nx.Graph((e.bus1,e.bus2) for e in f.lines)
    paths={l.bus:nx.shortest_path(graph,f.source_bus,l.bus) for l in f.loads}
    def score(pair):
        a,b=map(lambda bus:paths[bus],pair);common=len(set(a)&set(b))-1
        return min((len(a)-1-common)/(len(a)-1),(len(b)-1-common)/(len(b)-1)),min(len(a),len(b)),pair
    return max(combinations(sorted(paths),2),key=score)


def suite(seeds):
    plans=[]
    for seed in seeds:
        base=make_plan(seed);a,b=separated_ports(base)
        for task in ('large_change','selective','opposed'):
            p=copy.deepcopy(base)
            if task!='large_change':
                p['probes']=[dict(id='a',kind='voltage_p',step_mw=.01,injection_buses=[a]),
                    dict(id='b',kind='voltage_p',step_mw=.01,injection_buses=[b])]
                p['targets']=[dict(probe_id='a',reference='baseline_ratio',lower=1.15,upper=1.35),
                    dict(probe_id='b',reference='baseline_ratio',lower=.99 if task=='selective' else .65,upper=1.01 if task=='selective' else .85)]
            for policy in ('restricted','expanded'):
                trial=copy.deepcopy(p)
                if policy=='expanded':
                    trial['search']['max_layout_scale_change']=.75
                    if task!='large_change':
                        trial['search']['allowed_actions'].append('scale_subtree')
                        trial['search'].update(max_branch_length_change=.75,max_node_displacement_km=.75,max_joint_actions=2)
                trial['research_question']=f'{task}: {policy} design permissions; preserve original response baseline and all requested targets.'
                plans.append(dict(case_id=f'{task}_{seed}_{policy}',task=task,policy=policy,seed=seed,plan=trial))
    return plans


def run(output,seeds):
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True);plans=suite(seeds)
    atomic_json(output/'suite.json',dict(protocol='Paired identical baselines; exact 25 buses, 360 kW, 10 kV; four rounds and 16 AC candidate previews per round in both methods. Graph-separated ports chosen before solving. Scope is action-space controllability, not LLM attribution or global feasibility.',plans=plans))
    rows=[]
    for item in plans:
        start=time.monotonic();row={k:v for k,v in item.items() if k!='plan'}
        try:
            r=run_response_design(item['plan'],output,item['case_id']);root=Path(r['directory'])
            trace=json.loads((root/'history.json').read_text())
            row.update(target_met=r['target_met'],base_accepted=r['selected']['base_accepted'],
                verification_passed=r['verification_passed'],contract_preserved=r['protected_contract_preserved'],
                ratios={v['target']['probe_id']:v['observed'] for v in r['selected']['targets']},
                targets=[v['target'] for v in r['selected']['targets']],steps=r['accepted_steps'],
                candidate_count=r['evaluated_candidates']-1,stop_reason=r['stop_reason'],
                committed_actions=[v['action'] for v in trace if v.get('committed')],
                baseline_sha256=file_digest(root/'baseline/model/feeder.json'),
                source_hash=json.loads((root/'manifest.json').read_text())['source_hash'],result_hash=r['result_hash'])
        except Exception as exc:row.update(target_met=False,error=f'{type(exc).__name__}: {exc}')
        row['seconds']=round(time.monotonic()-start,3);rows.append(row)
        atomic_json(output/'results.json',dict(rows=rows))
        print(row['case_id'],row['target_met'],row.get('ratios'),row.get('stop_reason',row.get('error')),flush=True)
    for seed in seeds:
        for task in ('large_change','selective','opposed'):
            pair=[r for r in rows if r['seed']==seed and r['task']==task]
            if len({r.get('baseline_sha256') for r in pair})!=1:raise ValueError('Paired baselines differ')
    fields=['case_id','task','policy','seed','target_met','base_accepted','verification_passed','contract_preserved','ratio_a','ratio_b','steps','candidate_count','seconds','stop_reason']
    with (output/'results.csv').open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
        for row in rows:
            record={k:row.get(k) for k in fields};record.update(ratio_a=row.get('ratios',{}).get('a'),ratio_b=row.get('ratios',{}).get('b'))
            writer.writerow(record)
    return rows


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--seeds',type=int,nargs='+',default=[41,42,43])
    args=parser.parse_args();run(args.output,args.seeds)
