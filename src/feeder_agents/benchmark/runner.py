"""Sequential, isolated, resumable benchmark runner with evidence-linked scoring."""
import csv
import fcntl
import html
import json
import re
import time
from collections import defaultdict
from pathlib import Path

from ..artifacts import atomic_json,digest,file_digest
from ..hierarchy import HierarchicalSpec,HierarchicalFeeder
from ..hierarchy_workflow import run_hierarchy
from ..workflow import _runtime_fingerprint
from .schema import BenchmarkSuite
from .adapters import public_task,plan_public,plan_oracle,LLM_ADAPTERS
from .evaluate import score_requirements


def _read_row(root):
    row=json.loads((root/'row.json').read_text())
    if digest({k:v for k,v in row.items() if k!='row_hash'})!=row.get('row_hash'):raise ValueError('Trial row checksum changed')
    for name,expected in row['artifacts'].items():
        path=(root/name).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file() or file_digest(path)!=expected:
            raise ValueError('Trial artifact checksum changed: '+name)
    return row


def _trial(suite,case,adapter,seed,root,model):
    root.mkdir(parents=True,exist_ok=True)
    if (root/'row.json').exists():return _read_row(root)
    start=time.monotonic();feeder=None;validation={};status='failed';issues=[];stage='planning'
    row=dict(case_id=case.id,group_id=case.group_id,stratum=case.stratum,adapter=adapter,seed=seed,expected=case.expected,
             privileged=adapter=='oracle',trial_path=str(root),model=getattr(model,'model_name',None) if adapter in LLM_ADAPTERS else None)
    try:
        planpath=root/'plan.json'
        if planpath.exists():
            envelope=json.loads(planpath.read_text());plan=envelope['plan']
            if digest(plan)!=envelope['plan_hash']:raise ValueError('Plan checksum changed')
        else:
            plan=plan_oracle(case) if adapter=='oracle' else plan_public(adapter,public_task(case),root/'planner',model,suite.fixed_spec)
            atomic_json(planpath,dict(plan=plan,plan_hash=digest(plan)))
        status=plan['status'];issues=plan.get('issues',[])
        if status=='ready':
            stage='generation'
            spec=HierarchicalSpec.model_validate(plan['spec'])
            if spec.count!=1:raise ValueError('Benchmark supports single-case requests only; count must be1')
            # Same experimental seed, separate from model inference randomness.
            payload=spec.model_dump();payload['seed']=seed
            spec=HierarchicalSpec.model_validate(payload)
            outcome=run_hierarchy(spec,root,'case')
            sample=outcome['samples'][0]
            if sample.get('error'):
                status='failed';issues=[sample['error']]
            else:
                folder=Path(outcome['directory'])/'sample_00000'
                feeder=HierarchicalFeeder.model_validate(json.loads((folder/'feeder.json').read_text()))
                validation=json.loads((folder/'validation.json').read_text())
                status='generated'
    except Exception as exc:
        status='failed';issues=[f'{type(exc).__name__}: {str(exc)[:1200]}']
    score=score_requirements(case,feeder)
    checks=validation.get('checks',{})
    electrical_keys=('converged','measurements_complete','voltage_bases','power_balance','mv_voltage','lv_voltage','line_capacity','transformer_capacity','unbalance')
    electrical=bool(checks) and all(checks.get(k,False) for k in electrical_keys)
    generated=status=='generated'
    success=(generated and score['all_satisfied'] and validation.get('accepted',False)) if case.expected=='generate' else any(isinstance(issue,str) and issue.strip() for issue in issues) and status in (('unsupported','needs_clarification') if case.expected=='abstain' else ('unsupported',))
    row.update(status=status,issues=issues,failure_stage=stage if status=='failed' else None,
        model_generated=generated,converged=checks.get('converged',False),electrical_valid=electrical,
        pipeline_accepted=validation.get('accepted',False),requirements=score,
        end_to_end_success=bool(success),false_acceptance=case.expected!='generate' and generated,
        response_review_required=case.expected!='generate',response_rubric=case.response_rubric,
        elapsed_seconds=round(time.monotonic()-start,6),validation=validation)
    # Include artifacts used for scoring, not mutable SQLite sidecars.
    artifacts=[root/'plan.json']
    artifacts.extend((root/'planner'/'model_calls').rglob('*.json'))
    artifacts.extend(p for p in (root/'hierarchical_experiments').rglob('*') if p.is_file() and p.name!='.lock')
    row['artifacts']={str(p.relative_to(root)):file_digest(p) for p in artifacts if p.exists()}
    row['row_hash']=digest(row);atomic_json(root/'row.json',row)
    return row


def aggregate(rows):
    result={}
    for adapter in sorted({r['adapter'] for r in rows}):
        selected=[r for r in rows if r['adapter']==adapter];generation=[r for r in selected if r['expected']=='generate'];reject=[r for r in selected if r['expected']=='reject'];abstain=[r for r in selected if r['expected']=='abstain']
        def rate(items,key):return sum(bool(r[key]) for r in items)/len(items) if items else None
        grouped=defaultdict(list)
        for r in selected:grouped[r['group_id']].append(r['end_to_end_success'])
        result[adapter]=dict(privileged=adapter=='oracle',trials=len(selected),generation_trials=len(generation),rejection_trials=len(reject),abstention_trials=len(abstain),
            abstention_handling_rate=rate(abstain,'end_to_end_success'),abstention_false_acceptance_rate=rate(abstain,'false_acceptance'),
            end_to_end_rate=rate(selected,'end_to_end_success'),generation_success_rate=rate(generation,'end_to_end_success'),
            electrical_valid_rate=rate(generation,'electrical_valid'),convergence_rate=rate(generation,'converged'),
            rejection_handling_rate=rate(reject,'end_to_end_success'),false_acceptance_rate=rate(reject,'false_acceptance'),
            group_macro_success=sum(sum(v)/len(v) for v in grouped.values())/len(grouped),groups=len(grouped),
            requirement_satisfaction_rate=sum(r['requirements']['satisfied'] for r in generation)/sum(r['requirements']['total'] for r in generation) if generation else None,
            failures=sum(r['status']=='failed' for r in selected),mean_elapsed_seconds=sum(r['elapsed_seconds'] for r in selected)/len(selected))
        paraphrases=defaultdict(list)
        strata=defaultdict(list)
        for r in selected:
            paraphrases[(r['group_id'],r['seed'])].append(r)
            strata[r.get('stratum','scalar')].append(r)
        paired=[items for items in paraphrases.values() if len({r['case_id'] for r in items})>1]
        result[adapter].update(
            paraphrase_joint_success_rate=sum(all(r['end_to_end_success'] for r in items) for items in paired)/len(paired) if paired else None,
            paraphrase_group_seed_count=len(paired),
            by_stratum={name:dict(trials=len(items),end_to_end_rate=rate(items,'end_to_end_success'),
                failures=sum(r['status']=='failed' for r in items)) for name,items in sorted(strata.items())})
    return result


def _reports(root,rows,summary):
    columns=['adapter','case_id','group_id','stratum','seed','expected','status','model_generated','converged','electrical_valid','pipeline_accepted','end_to_end_success','false_acceptance','elapsed_seconds']
    with (root/'trials.csv').open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=columns);writer.writeheader()
        writer.writerows({k:r[k] for k in columns} for r in rows)
    detail=[]
    for r in rows:
        rel=Path(r['trial_path']).relative_to(root)
        detail.append(f'<tr><td>{html.escape(r["adapter"])}</td><td>{html.escape(r["case_id"])}</td><td>{r["seed"]}</td><td>{r["status"]}</td><td>{r["electrical_valid"]}</td><td>{r["requirements"]["satisfied"]}/{r["requirements"]["total"]}</td><td>{r["end_to_end_success"]}</td><td><a href="{rel}/row.json">Scoring evidence</a></td></tr>')
    (root/'index.html').write_text('<meta charset="utf-8"><title>Feeder benchmark</title><h1>Unified feeder benchmark</h1><p>All attempts count in the denominator. The oracle is a capability reference with supplied answers. This runner covers individual multi-voltage generation, not inverse tasks or open-ended semantic judgment.</p><p><a href="summary.json">Summary</a> · <a href="trials.csv">CSV</a></p><table border="1"><tr><th>Method</th><th>Request</th><th>Seed</th><th>Status</th><th>Electrically valid</th><th>Requirements satisfied</th><th>End-to-end success</th><th>Evidence</th></tr>'+''.join(detail)+'</table>')
    atomic_json(root/'summary.json',summary)


def run_benchmark(suite,workspace,run_id,allow_llm=False,model=None):
    suite=BenchmarkSuite.model_validate(suite.model_dump() if isinstance(suite,BenchmarkSuite) else suite)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',run_id):raise ValueError('Invalid benchmark run ID')
    needs_model=any(a in LLM_ADAPTERS for a in suite.adapters)
    if needs_model and not allow_llm and model is None:raise ValueError('This suite needs model calls; set --allow-llm explicitly')
    if needs_model and model is None:
        from ..agent import configured_model
        model=configured_model(timeout=40,max_retries=0,max_tokens=6000,disable_thinking=True)
    root=Path(workspace).resolve()/'benchmarks'/run_id;root.mkdir(parents=True,exist_ok=True)
    manifest=dict(schema_version=4,suite=suite.model_dump(),runtime=_runtime_fingerprint(),
        model_name=getattr(model,'model_name','injected_model') if needs_model else None,
        model_policy='Configured .env backend; temperature0, timeout40s, network retries0, max tokens6000; Agent may correct once' if needs_model and allow_llm else 'offline or injected model',
        protocol='All seeds are paired generation seeds, not LLM sampling seeds. Scoring keys private to runner. No persistent cross-trial memory.')
    with (root/'.lock').open('w') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (root/'manifest.json').exists():
            if json.loads((root/'manifest.json').read_text())!=manifest:raise ValueError('Benchmark configuration/code/model changed; use a new ID')
        else:atomic_json(root/'manifest.json',manifest)
        rows=[];total=len(suite.cases)*len(suite.adapters)*len(suite.seeds)
        for case in suite.cases:
            for adapter in suite.adapters:
                for seed in suite.seeds:
                    rows.append(_trial(suite,case,adapter,seed,root/'trials'/adapter/case.id/f'seed_{seed}',model))
                    atomic_json(root/'progress.json',dict(completed=len(rows),total=total,last_case=case.id,last_adapter=adapter))
        summary=dict(suite_id=suite.id,split=suite.split,run_id=run_id,directory=str(root),metrics=aggregate(rows),
                     interpretation='Development suites and smoke results are not held-out evidence. Rates describe annotated requirements only; group macro is not a confidence interval.')
        _reports(root,rows,summary)
        return dict(**summary,rows=rows)
