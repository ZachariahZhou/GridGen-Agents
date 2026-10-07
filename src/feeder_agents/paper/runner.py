"""Dual-domain, paired, resumable paper trials; never mutate old benchmarks."""
import csv
import fcntl
import json
import re
import shutil
import sqlite3
import time
from pathlib import Path
import numpy as np
from ..artifacts import atomic_json,digest,file_digest
from ..workflow import _runtime_fingerprint
from ..hierarchy import HierarchicalSpec,generate_hierarchy,export_hierarchy,evaluate_hierarchy
from ..transmission import TransmissionSpec,generate_case,case_payload,export_case,validate_case
from .schema import Protocol
from .adapters import plan,FixedRepair
from .evaluate import observations,score,aggregate


def memory_inventory(protocol):
    inventory={}
    for domain,filename in protocol.memory_snapshots.items():
        path=Path(filename).resolve()
        if not path.is_file():raise ValueError('Missing memory snapshot: '+str(path))
        if Path(str(path)+'-wal').exists():raise ValueError('Memory snapshot must be a closed/checkpointed database')
        inventory[str(path)]=file_digest(path)
        provenance_path=path.with_suffix('.provenance.json')
        provenance=json.loads(provenance_path.read_text())
        if provenance['domain']!=domain or set(provenance['seeds'])&set(protocol.seeds):raise ValueError('Memory training/evaluation domain or seed separation violated')
        inventory[str(provenance_path)]=file_digest(provenance_path)
        sources={**provenance['sources'],provenance['training_manifest']:provenance['manifest_hash']}
        for name,h in sources.items():
            if file_digest(Path(name))!=h:raise ValueError('Memory training provenance changed')
            inventory[name]=h
        table='episodes' if domain=='distribution' else 'transmission_episodes'
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
            rows=db.execute(f'SELECT payload FROM {table} WHERE invalid_reason IS NULL').fetchall()
        for raw, in rows:
            item=json.loads(raw);root=Path(item['root']).resolve()
            for relative,expected in item.get('files',item.get('hashes',{})).items():
                evidence=(root/relative).resolve()
                if not evidence.is_relative_to(root) or file_digest(evidence)!=expected:raise ValueError('Memory evidence is missing or changed')
                inventory[str(evidence)]=expected
    return inventory


def _memory(protocol,domain,root,spec):
    spec=type(spec).model_validate(spec.model_dump())
    snapshot=protocol.memory_snapshots[domain];destination=root/'memory.sqlite';shutil.copyfile(snapshot,destination)
    if domain=='distribution':
        from ..verified_memory import VerifiedMemory
        store=VerifiedMemory(destination)
    else:
        from ..transmission_memory import TransmissionMemory
        store=TransmissionMemory(destination)
    return store,store.retrieve(spec)


def physical_trial(task,spec,method,protocol,root,model):
    from ..simulation import simulate
    repair_model=FixedRepair() if method=='fixed_repair' else model
    use_feedback=method in ('fixed_repair','agent','agent_memory','adaptive');memory=None;hints=[]
    if method=='agent_memory':memory,hints=_memory(protocol,task.domain,root,spec)
    if task.domain=='distribution':
        from ..hierarchy_feedback import run_feedback
        network=generate_hierarchy(spec,spec.seed)
        if task.fault=='undersized_transformer':network.transformers[0].kva=5.
        atomic_json(root/'initial.json',network.model_dump());initial_hash=digest(network.model_dump())
        sim=simulate(export_hierarchy(network,root/'initial_dss'));feedback={}
        if use_feedback:
            out=run_feedback(network,spec,sim,root/'feedback',model=repair_model,max_rounds=protocol.rounds,request=task.request,experience_store=memory)
            network=out['feeder'];feedback=out['report']
        atomic_json(root/'model.json',network.model_dump())
        # Independent final export reload; benchmark bands cannot be relaxed by LLM.
        sim=simulate(export_hierarchy(network,root/'opendss'))
        limits=spec.model_copy(update=dict(mv_voltage_min_pu=max(.93,spec.mv_voltage_min_pu),
            mv_voltage_max_pu=min(1.07,spec.mv_voltage_max_pu),lv_voltage_min_pu=max(.9,spec.lv_voltage_min_pu),
            lv_voltage_max_pu=min(1.1,spec.lv_voltage_max_pu),max_vuf_percent=min(2.,spec.max_vuf_percent)))
        validation=evaluate_hierarchy(network,limits,sim)
        obs=observations(task.domain,network,validation=validation);reload=bool(sim.get('converged'))
        atomic_json(root/'reloaded_simulation.json',sim)
    else:
        from ..transmission_feedback import run_feedback
        from ..transmission_model import refresh
        from ..matpower import parse_matpower
        network,meta=generate_case(spec,spec.seed)
        if task.fault=='undercompensated':meta['shunt_fraction']=0.;refresh(network,meta)
        initial=dict(case=case_payload(network),metadata=meta);atomic_json(root/'initial.json',initial);initial_hash=digest(initial);feedback={}
        if use_feedback:
            out=run_feedback(network,meta,spec,root/'feedback',model=repair_model,max_rounds=protocol.rounds,request=task.request,memory=memory)
            network=out['case'];meta=out['metadata'];feedback=out['report']
        atomic_json(root/'model.json',case_payload(network));atomic_json(root/'metadata.json',meta);export_case(network,root/'case_generated.m')
        parsed=parse_matpower(root/'case_generated.m')
        reloaded=dict(version='2',baseMVA=parsed['base_mva'],**{k:np.asarray(parsed[k],dtype=float) for k in ('bus','gen','branch')})
        limits=spec.model_copy(update=dict(voltage_min_pu=max(.95,spec.voltage_min_pu),voltage_max_pu=min(1.05,spec.voltage_max_pu)))
        validation,_=validate_case(reloaded,limits,meta);reload=validation['converged']
        obs=observations(task.domain,reloaded,meta,validation)
    atomic_json(root/'independent_validation.json',validation)
    return dict(observed=obs,electrical_valid=bool(validation['accepted']),export_reload=bool(reload),initial_hash=initial_hash,
        feedback_steps=feedback.get('accepted_steps',0),feedback_stop=feedback.get('stop_reason'),memory_groups=len(hints),memory_eligible=bool(hints))


def read_row(root):
    row=json.loads((root/'row.json').read_text())
    if digest({k:v for k,v in row.items() if k!='row_hash'})!=row['row_hash']:raise ValueError('Trial row changed')
    for name,expected in row['artifacts'].items():
        p=(root/name).resolve()
        if not p.is_relative_to(root.resolve()) or file_digest(p)!=expected:raise ValueError('Trial artifact changed')
    return row


def trial(protocol,task,track,method,seed,root,model,proposal_override=None):
    root.mkdir(parents=True,exist_ok=True)
    if (root/'row.json').exists():return read_row(root)
    start=time.monotonic();row=dict(task_id=task.id,group=task.group,domain=task.domain,track=track,method=method,seed=seed,expected=task.expected,
        status='error',generated=False,electrical_valid=False,export_reload=False,requirements_met=False,benchmark_success=False,joint_success=False,status_match=False,
        manual_review_required=task.expected!='generate',response_rubric=task.response_rubric,issues=[])
    try:
        if protocol.execution=='end_to_end':
            from .end_to_end import execute_request
            row.update(execute_request(dict(request=task.request,domain=task.domain),method,root,seed,
                                       protocol.rounds,protocol.recovery_rounds,model,proposal_override))
        else:
            cls=HierarchicalSpec if task.domain=='distribution' else TransmissionSpec
            if track=='structured':proposal=dict(status='ready',spec=cls().model_dump() if method=='template' else task.reference_spec)
            elif proposal_override is not None:
                import copy
                proposal=copy.deepcopy(proposal_override)
            else:proposal=plan(method,dict(request=task.request,domain=task.domain),root/'planner',model)
            atomic_json(root/'proposal.json',proposal);row['status']=proposal['status'];row['issues']=proposal.get('issues',[])
            if proposal['status']=='ready':
                spec=cls.model_validate(proposal['spec'])
                if spec.count!=1:raise ValueError('Only count=1 is allowed; batch requests need a separate protocol')
                spec=cls.model_validate(spec.model_dump()).model_copy(update={'seed':seed})
                row.update(physical_trial(task,spec,method,protocol,root,model));row.update(status='generated',generated=True)
        scoring=score(task,row.get('observed',{}));row['requirement_scores']=scoring;row['requirements_met']=scoring['all_satisfied']
        row['benchmark_success']=task.expected=='generate' and row['generated'] and row['requirements_met'] and row['electrical_valid'] and row['export_reload']
        row['joint_success']=row['benchmark_success'] and row.get('product_accepted',True)
        row['status_match']=task.expected!='generate' and bool(row['issues']) and row['status']==('needs_clarification' if task.expected=='clarify' else 'unsupported')
    except Exception as exc:row.update(status='error',issues=[f'{type(exc).__name__}: {exc}'])
    calls=[json.loads(p.read_text()) for p in root.rglob('attempt_*.json')]
    external=[c for c in calls if 'raw' in c or c.get('status')=='call_error']
    row['model_calls']=len(external);row['reported_tokens']=sum((c.get('raw',{}).get('usage_metadata') or {}).get('total_tokens',0) for c in external)
    row['elapsed_seconds']=time.monotonic()-start
    row['artifacts']={str(p.relative_to(root)):file_digest(p) for p in root.rglob('*') if p.is_file() and p.suffix not in ('.sqlite',) and '-wal' not in p.name and '-shm' not in p.name and p.name not in ('row.json','.lock')}
    row['row_hash']=digest(row);atomic_json(root/'row.json',row);return row


def run(protocol,workspace,run_id,allow_llm=False,model=None,freeze_path=None):
    protocol=Protocol.model_validate(protocol.model_dump() if isinstance(protocol,Protocol) else protocol)
    if protocol.split=='candidate':raise ValueError('Candidate tasks require human review and a frozen release before evaluation')
    release=None
    if protocol.split=='heldout':
        if freeze_path is None:raise ValueError('Held-out execution requires a frozen release')
        from .freeze import verify
        release=verify(protocol,freeze_path)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,60}',run_id):raise ValueError('Invalid run ID')
    needs_model=any(m in ('agent','agent_memory','adaptive') or ('language' in protocol.tracks and m!='template') for m in protocol.methods)
    if needs_model and model is None:
        if not allow_llm:raise ValueError('Explicit --allow-llm is required')
        from ..agent import configured_model
        model=configured_model(timeout=30,max_retries=0,max_tokens=6000,disable_thinking=True)
    root=Path(workspace).resolve()/run_id;root.mkdir(parents=True,exist_ok=True)
    manifest=dict(protocol=protocol.model_dump(),release=release,runtime=_runtime_fingerprint(),memory_evidence=memory_inventory(protocol),model=getattr(model,'model_name',None),
        electrical_limits=dict(distribution=dict(mv=[.93,1.07],lv=[.9,1.1],vuf=2.),transmission=dict(voltage=[.95,1.05])),
        interpretation='Structured track is privileged specification input for all non-template methods. Final exports are independently reloaded. Boundary status matches require separate human semantic review.')
    if protocol.execution=='end_to_end':
        manifest['delivery_policy']=dict(entry='feeder_agents.delivery.execute_delivery',memory=False,
            feedback_rounds=protocol.rounds,recovery_rounds=protocol.recovery_rounds,
            fixed_repair_shares_one_shot_plan='one_shot' in protocol.methods,
            seed_schedule='sha256(protocol_seed:sample_index), same production schedule for all arms')
    with (root/'.lock').open('w') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        path=root/'manifest.json'
        if path.exists() and json.loads(path.read_text())!=manifest:raise ValueError('Protocol/runtime/memory/model changed; use a new run ID')
        atomic_json(path,manifest);rows=[]
        for task in protocol.tasks:
            for track in protocol.tracks:
                if track=='structured' and task.expected!='generate':continue
                methods=sorted(protocol.methods,key=lambda m:m!='one_shot') if protocol.execution=='end_to_end' else protocol.methods
                for method in methods:
                    for seed in protocol.seeds:
                        override=None
                        if protocol.execution=='end_to_end' and method=='fixed_repair' and 'one_shot' in protocol.methods:
                            baseline=root/'trials'/task.id/track/'one_shot'/str(seed)
                            checked=read_row(baseline)
                            proposal_path=baseline/'proposal.json'
                            override=json.loads(proposal_path.read_text()) if proposal_path.exists() else dict(status='error',issues=checked['issues'])
                        rows.append(trial(protocol,task,track,method,seed,root/'trials'/task.id/track/method/str(seed),model,proposal_override=override))
                        atomic_json(root/'progress.json',dict(completed=len(rows),last_task=task.id,method=method))
        metrics=aggregate(rows);atomic_json(root/'summary.json',dict(metrics=metrics,split=protocol.split,trials=len(rows)))
        cols=['task_id','domain','track','method','seed','expected','status','requirements_met','electrical_valid','export_reload','joint_success','status_match','elapsed_seconds']
        with (root/'trials.csv').open('w',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=cols);writer.writeheader();writer.writerows({k:r.get(k) for k in cols} for r in rows)
        with (root/'response_review.csv').open('w',newline='') as f:
            writer=csv.writer(f);writer.writerow(['task','method','seed','expected','actual_status','issues','rubric','reviewer','semantic_correct','comment'])
            for r in rows:
                if r['manual_review_required']:writer.writerow([r['task_id'],r['method'],r['seed'],r['expected'],r['status'],json.dumps(r['issues'],ensure_ascii=False),json.dumps(r['response_rubric'],ensure_ascii=False),'','',''])
        pairs={}
        for r in rows:
            if r['track']=='structured' and r['method']!='template' and r.get('initial_hash'):pairs.setdefault((r['task_id'],r['seed']),set()).add(r['initial_hash'])
        pairing_ok=all(len(v)==1 for v in pairs.values())
        pairing=dict(passed=pairing_ok,groups=len(pairs))
        if protocol.execution=='end_to_end':
            from .end_to_end import audit_pairs
            pairing=audit_pairs(root,rows);pairing_ok=pairing['passed']
        atomic_json(root/'pairing_audit.json',pairing)
        if not pairing_ok:raise ValueError('Structured ablation arms did not start from identical networks')
        report=['# 输配电科研模型评测（'+protocol.split+'）','', '执行模式：'+protocol.execution+'。结构化输入用于隔离生成/修复能力；不作为自然语言理解成绩。边界任务须人工复核。', '', '|网络|输入|方法|生成尝试|联合成功率|独立评测成功率|电气有效率|导出重载率|执行错误|规划失败|未成功生成任务|','|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|']
        for m in metrics:
            pct=lambda x: '—' if x is None else f'{100*x:.1f}%'
            report.append('|'+ '|'.join([m['domain'],m['track'],m['method'],str(m['generation_trials']),pct(m['joint_success_rate']),pct(m['benchmark_success_rate']),pct(m['electrical_valid_rate']),pct(m['export_reload_rate']),str(m['execution_errors']),str(m['planning_failures']),str(m['unsuccessful_generation'])])+'|')
        report+=['','所有尝试计入各自分母；可用配对检查：'+str(pairing_ok)+'，组数：'+str(pairing['groups'])+'。','开发结果不能当作独立测试结果；该试跑不证明LLM、记忆或泛化收益。']
        (root/'report.md').write_text('\n'.join(report)+'\n')
        return dict(directory=str(root),metrics=metrics,trials=len(rows))
