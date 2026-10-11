"""Live outer-agent runner; instrumentation changes no production implementation."""
from __future__ import annotations
import copy
import csv
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from unittest.mock import patch

from feeder_agents.artifacts import atomic_json, digest, file_digest
from .m97_language import build_cases, public_case, required_checks, grade_evidence, unauthorized_actions, walk_dicts

SETTINGS=dict(timeout=40,max_retries=0,max_tokens=6500,disable_thinking=True)
ARMS=['one_proposal','full_outer']
_GRADE_LOCK = threading.Lock()


def plain(value):
    if hasattr(value,'model_dump'):return plain(value.model_dump())
    if isinstance(value,dict):return {str(k):plain(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [plain(v) for v in value]
    if value is None or isinstance(value,(str,int,float,bool)):return value
    return str(value)


def certify(case, root):
    """Independent construction before any language-model outcomes, with no search."""
    from feeder_agents.schemas import ExperimentSpec
    from feeder_agents.transmission import TransmissionSpec, validate_case
    from feeder_agents.response_measurement import create_model, resolve_probes, measure
    from feeder_agents.response_schema import ResponseDesignPlan
    from feeder_agents.response_design import apply_response_action, score_response, research_contract_preserved
    from feeder_agents.research_tasks import compile_research_task
    from feeder_agents.research_measurement import evaluate_research_task
    from feeder_agents.evaluation import evaluate, verdict
    from feeder_agents.simulation import export_dss, simulate
    spec=case.get('witness_spec')
    if not spec or case['expected_outcome']!='generation':return dict(required=False,certified=False)
    folder=Path(root)/case['id'];folder.mkdir(parents=True,exist_ok=True)
    try:
        typed=TransmissionSpec.model_validate(spec) if spec['network_kind']=='transmission' else ExperimentSpec.model_validate(spec)
        if case.get('task'):
            lower,upper=case['response_interval']
            task=dict(task=case['task'],research_question=case['messages'][0],base_spec=spec,
                      response_lower=lower,response_upper=upper,preserve_equipment=True,
                      support_mvar=case.get('support_mvar',.01),
                      search=(dict(allowed_actions=['scale_layout'],max_layout_scale_change=.3) if lower>1 else dict(allowed_actions=[])))
            compiled=compile_research_task(task);plan=ResponseDesignPlan.model_validate(compiled['response_plan'])
            original=create_model(plan.base_spec);probes=resolve_probes(original,plan.probes)
            reference=measure(original,plan.base_spec,probes,folder/'reference')
            action=dict(kind='scale_layout',factor=1.2,original_scale=1.2) if lower>1 else None
            witness=apply_response_action(original,plan.base_spec,action) if action else original
            full=measure(witness,plan.base_spec,probes,folder/'full')
            half=measure(witness,plan.base_spec,probes,folder/'half',step_factor=.5)
            scored=score_response(full,plan.targets,reference);hscore=score_response(half,plan.targets,reference)
            finite=evaluate_research_task(witness,plan.base_spec,compiled['context'],folder/'finite_task')
            legal=research_contract_preserved(original,witness,plan)
            record=dict(required=True,certified=bool(scored['target_met'] and hscore['target_met'] and finite['accepted'] and legal),
                witness_class=case.get('witness_class','interface_baseline'),baseline_meets=score_response(reference,plan.targets,reference)['target_met'],
                targets=scored['targets'],half_targets=hscore['targets'],finite_task=finite['accepted'],legal=legal,
                compiled_plan=compiled,witness_action=action,reference_hash=digest(plain(original)),witness_hash=digest(plain(witness)))
        else:
            # Ordinary run_experiment hashes seed:index; certify that same physical construction.
            actual_seed=int.from_bytes(hashlib.sha256(f'{typed.seed}:0'.encode()).digest()[:4],'big')
            typed=typed.model_copy(update={'seed':actual_seed});model=create_model(typed)
            if model['kind']=='distribution':
                if case.get('revision'):
                    from feeder_agents.revisions import apply_revision,RevisionPlan
                    suffix=int(case['id'].rsplit('_',1)[1])
                    for turn,text in enumerate(case['messages'][1:],1):
                        actions=([dict(kind='pv_ratio',value=.4 if turn==1 else .6,evidence=text)] if suffix<=3 else
                                 [dict(kind='load_scale',value=1.1,evidence=text),dict(kind='pv_ratio',value=.2,evidence=text)] if suffix<=5 else
                                 [dict(kind='geometry_scale',value=1.05,evidence=text)] if turn==1 else [dict(kind='pv_ratio',value=.3,evidence=text)])
                        feeder,typed,_=apply_revision(model['feeder'],typed,RevisionPlan(feedback=text,actions=actions,preserve=['topology','voltage']))
                        model['feeder']=feeder
                simulation=simulate(export_dss(model['feeder'],folder/'opendss'))
                accepted=verdict(evaluate(model['feeder'],typed,simulation),typed.mode)['accepted']
            else:accepted=validate_case(model['case'],typed,model['metadata'])[0]['accepted']
            record=dict(required=True,certified=bool(accepted),witness_class='ordinary_model',model_hash=digest(plain(model)))
        atomic_json(folder/'certificate.json',record);return record
    except Exception as exc:
        record=dict(required=True,certified=False,error=f'{type(exc).__name__}: {exc}')
        atomic_json(folder/'certificate.json',record);return record


def freeze(root):
    from feeder_agents.agent import configured_model
    from .response_trials import source_inventory,runtime_versions
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    if (root/'suite.json').exists():raise ValueError('Suite already frozen')
    cases=build_cases()
    for case in cases:
        case['witness']=certify(case,root/'private_witnesses')
        print(f"witness {case['id']}: {case['witness'].get('certified')}",flush=True)
    model=configured_model(**SETTINGS)
    package,inventory=source_inventory()
    # Snapshot only the executed production and this study's adapters, not credentials.
    import shutil
    for name in inventory:
        destination=root/'source'/'feeder_agents'/name;destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(package/name,destination)
    suite=dict(study='m97_outer_agent_v2',cases=cases,arms=ARMS,repetitions=[0,1,2],
        model=model.model_name,model_settings=SETTINGS,temperature=0,global_model_call_ceiling=24,
        outer_call_ceiling_per_turn=dict(one_proposal=8,full_outer=8),tool_call_ceiling_per_turn=12,
        conversation_timeout_seconds=180,workers=8,memory='fresh project per conversation; planning memory off',
        versions=runtime_versions(),source_hash=digest(inventory),source_inventory=inventory,
        endpoint_hash=digest(str(model.openai_api_base)),
        public_hash=digest([public_case(c) for c in cases]),
        interpretation='One actionable proposal after prerequisites versus full outer workflow; common8 outer/24 total model-call limits, identical downstream tools with their internal LLM workflows. Repetitions hold physical seed fixed.',
        negatives='Correct decisions, unmet objectives and generated artifacts are separate denominators.',
        witness_rule='One fixed independent 1.2 scale witness for nontrivial distribution tasks; no search, no outcome-driven task selection. Failed certificates remain recorded and outside primary known-feasible denominator.')
    script=Path.cwd()/'scripts/run_m97_outer_agent.py'
    (root/'execution_scripts').mkdir(exist_ok=True)
    shutil.copyfile(script,root/'execution_scripts'/script.name)
    suite['execution_script_hash']=file_digest(script)
    installed=subprocess.run([sys.executable,'-m','pip','freeze'],capture_output=True,text=True,check=False)
    (root/'installed_versions.txt').write_text(installed.stdout)
    atomic_json(root/'suite.json',suite);atomic_json(root/'suite_checksum.json',dict(sha256=digest(suite)))
    atomic_json(root/'public_cases.json',[public_case(c) for c in cases])
    return suite


def worker(root, case_id, arm, repetition):
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain.agents.middleware import ModelCallLimitMiddleware,ToolCallLimitMiddleware,AgentMiddleware
    from langchain.agents.middleware.types import hook_config
    from langchain_core.messages import ToolMessage
    from .m97_language_gate import ProposalGate
    import feeder_agents.agent as production
    import feeder_agents.design as design
    root=Path(root).resolve();public=next(c for c in json.loads((root/'public_cases.json').read_text()) if c['id']==case_id)
    out=root/'jobs'/f'{case_id}__{arm}__r{repetition}';out.mkdir(parents=True,exist_ok=True)
    log=out/'events.jsonl';lock=threading.Lock();started=time.monotonic()
    events=[];calls=0;turn=0
    def emit(kind,**fields):
        row=dict(kind=kind,turn=turn,seconds=time.monotonic()-started,**plain(fields))
        with lock:
            events.append(row)
            with log.open('a') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')
    class Recorder(BaseCallbackHandler):
        raise_error=True
        def on_chat_model_start(self,serialized,messages,**kwargs):
            nonlocal calls
            if calls>=24:raise RuntimeError('M97 shared total model-call ceiling 24')
            calls+=1
            emit('model_start',call=calls,run_id=str(kwargs.get('run_id')),tags=kwargs.get('tags'),messages=messages)
        def on_llm_end(self,response,**kwargs):emit('model_end',run_id=str(kwargs.get('run_id')),response=response)
        def on_llm_error(self,error,**kwargs):emit('model_error',run_id=str(kwargs.get('run_id')),error_type=type(error).__name__,error=str(error)[:1000])
        def on_tool_start(self,serialized,input_str,**kwargs):emit('tool_start',name=serialized.get('name'),run_id=str(kwargs.get('run_id')),input=input_str,inputs=kwargs.get('inputs'))
        def on_tool_end(self,output,**kwargs):emit('tool_end',run_id=str(kwargs.get('run_id')),output=output)
        def on_tool_error(self,error,**kwargs):emit('tool_error',run_id=str(kwargs.get('run_id')),error_type=type(error).__name__,error=str(error)[:1000])
    recorder=Recorder();real_factory=production.configured_model;real_create=production.create_agent;real_design=design.design_from_request
    def factory(**kwargs):return real_factory(**SETTINGS).model_copy(update={'callbacks':[recorder],'tags':['internal_model']})
    class OneProposal(AgentMiddleware):
        def __init__(self):
            self.gate=ProposalGate();self.key=None;self.lock=threading.Lock()
        @hook_config(can_jump_to=['end'])
        def before_model(self,state,runtime):
            human=next((m for m in reversed(state['messages']) if getattr(m,'type',None)=='human'),None)
            key=getattr(human,'id',None)
            if key!=self.key:self.key=key;self.gate.reset()
            if self.gate.done:
                emit('one_proposal_stop',reason='One substantive proposal and its actual tool result retained')
                return {'jump_to':'end'}
            return None
        def wrap_tool_call(self,request,handler):
            call=request.tool_call;name=call['name'];args=call['args']
            with self.lock:
                allowed=self.gate.start(name,args)
            if not allowed:
                emit('proposal_limit_block',name=name,arguments=args)
                return ToolMessage(content='Study baseline permits one actionable proposal per user turn; no second proposal was executed.',tool_call_id=call['id'],name=name)
            try:
                output=handler(request)
            except Exception:
                with self.lock:self.gate.done=True
                raise
            with self.lock:self.gate.finish(name,args,plain(output))
            return output
    def create(**kwargs):
        # Keep production source/permission protection identical in both arms.
        # Archived M97/M98 snapshots retain their original recorder unchanged.
        if arm=='one_proposal':kwargs['middleware']=[OneProposal(),*kwargs.get('middleware',[])]
        return real_create(**kwargs)
    def without_memory(*args,**kwargs):
        kwargs['planning_memory_mode']='off';return real_design(*args,**kwargs)
    model=real_factory(**SETTINGS).model_copy(update={'callbacks':[recorder],'tags':['outer_model']})
    result=dict(case_id=case_id,arm=arm,repetition=repetition,model=model.model_name,status='completed',messages=public['messages'],turns=[])
    atomic_json(out/'public_input.json',public)
    try:
        with patch.object(production,'configured_model',factory),patch.object(production,'create_agent',create),patch.object(design,'design_from_request',without_memory):
            with production.agent_session(out/'workspace',project_id='conversation',model=model,workers=1) as agent:
                for turn,message in enumerate(public['messages']):
                    begin=time.monotonic();emit('user_message',message=message)
                    try:
                        state=agent.invoke({'messages':[{'role':'user','content':message}]},
                            config={'configurable':{'thread_id':'conversation'},'callbacks':[recorder],'recursion_limit':60})
                        messages=plain(state['messages']);atomic_json(out/f'turn_{turn:02d}_messages.json',messages)
                        result['turns'].append(dict(turn=turn,status='completed',seconds=time.monotonic()-begin,messages=messages))
                    except Exception as exc:
                        result['turns'].append(dict(turn=turn,status='error',seconds=time.monotonic()-begin,error_type=type(exc).__name__,error=str(exc)[:2000]))
                        result['status']='turn_error'
                        # Continue the preregistered revision message; no outcome-specific prompt is invented.
                        emit('turn_error',error_type=type(exc).__name__,error=str(exc)[:2000])
    except Exception as exc:result.update(status='error',error_type=type(exc).__name__,error=str(exc)[:2000])
    result.update(seconds=time.monotonic()-started,llm_calls=calls)
    atomic_json(out/'worker_result.json',result)
    return result


def artifact_evidence(project):
    """Read saved models, recompute physical acceptance, and compare saved revisions."""
    from feeder_agents.schemas import Feeder,ExperimentSpec
    from feeder_agents.evaluation import evaluate,verdict
    from feeder_agents.simulation import export_dss,simulate
    from feeder_agents.revisions import _groups
    import numpy as np
    project=Path(project);artifacts=[];revisions=[];audit=project.parent.parent/'independent_grade'
    paths=list(project.glob('experiments/*/sample_*/feeder.json'))+list(project.glob('transmission_experiments/*/sample_*/case.json'))
    paths+=list(project.glob('research_tasks/*/selected/feeder.json'))+list(project.glob('research_tasks/*/selected/case.json'))
    # Response-only outputs are retained as artifacts but cannot satisfy a task label without finite-task evidence.
    paths+=list(project.glob('response_designs/*/selected/feeder.json'))+list(project.glob('response_designs/*/selected/case.json'))
    for index,path in enumerate(sorted(paths,key=lambda p:p.stat().st_mtime_ns)):
        record=dict(path=str(path),accepted=False,usable=False,facts={})
        try:
            folder=path.parent;run=folder.parent
            if 'sample_' in folder.name:
                if path.name=='feeder.json':
                    from feeder_agents.workflow import read_verified_experiment
                    saved=read_verified_experiment(run);spec=saved['specification']
                else:spec=json.loads((run/'manifest.json').read_text())['spec']
                task=None
            else:
                result=json.loads((run/'result.json').read_text())
                if (run/'compiled_plan.json').exists():
                    compiled=json.loads((run/'compiled_plan.json').read_text());task=compiled['task_plan'];spec=task['base_spec']
                else:
                    spec=json.loads((run/'manifest.json').read_text())['plan']['base_spec'];task=None
                record['response_verified']=bool(result.get('target_met') and result.get('verification_passed'))
            if path.name=='feeder.json':
                feeder=Feeder.model_validate_json(path.read_text());typed=ExperimentSpec.model_validate(spec)
                sim=simulate(export_dss(feeder,audit/f'model_{index}'/'opendss'))
                record['accepted']=verdict(evaluate(feeder,typed,sim),typed.mode)['accepted']
                record['facts']=dict(n_buses=len(feeder.buses),voltage_kv=feeder.voltage_kv,total_kw=sum(l.kw for l in feeder.loads),
                    pv_ratio=sum(l.pv_kw for l in feeder.loads)/sum(l.kw for l in feeder.loads),phase_mode=feeder.phase_mode,scene=spec['scenario']['kind'])
                record['usable']=bool(list(folder.glob('opendss/*')) and (folder/'visualization.html').exists())
                record['groups']=_groups(feeder)
            else:
                from feeder_agents.transmission import TransmissionSpec,validate_case
                case=json.loads(path.read_text());case={k:np.array(v) if k in ('bus','gen','branch') else v for k,v in case.items()}
                metadata=json.loads((folder/'metadata.json').read_text())
                record['accepted']=validate_case(case,TransmissionSpec.model_validate(spec),metadata)[0]['accepted']
                record['facts']=dict(n_buses=len(case['bus']),voltage_kv=float(case['bus'][0,9]),total_mw=float(case['bus'][:,2].sum()),n_generators=len(case['gen']))
                record['usable']=(folder/'case_generated.m').exists() and (folder/'visualization.html').exists()
            if 'response_verified' in record:record['accepted']=record['accepted'] and record['response_verified']
            if task:
                record['facts'].update(task=task['task'],response_lower=task.get('response_lower'),response_upper=task.get('response_upper'),support_mvar=task['support_mvar'])
                record['accepted']=record['accepted'] and result['task_validation']['accepted']
            record['facts'].update(seed=spec.get('seed'),count=spec.get('count'),repair_strategy=spec.get('repair_policy',{}).get('strategy'))
            record['model_sha256']=file_digest(path)
        except Exception as exc:record['error']=f'{type(exc).__name__}: {exc}'
        artifacts.append(record)
    lookup={Path(a['path']).parent.parent.name:a for a in artifacts if '/experiments/' in a['path']}
    for path in sorted(project.glob('revisions/*/result.json'),key=lambda p:p.stat().st_mtime_ns):
        try:
            result=json.loads(path.read_text());parent=result['parent'];before=lookup.get(parent['experiment_id']);after=lookup.get(path.parent.name)
            a=before['groups'] if before else {};b=after['groups'] if after else {}
            revisions.append(dict(path=str(path),parent_verified=bool(before and after and before['model_sha256'] and parent['model_hash']==digest(json.loads(Path(before['path']).read_text()))),
                parent_id=parent['experiment_id'],revision_id=path.parent.name,
                preserved={g:a.get('loads' if g=='load' else g)==b.get('loads' if g=='load' else g) for g in ('topology','geometry','equipment','load','voltage')}))
        except Exception as exc:revisions.append(dict(path=str(path),parent_verified=False,error=str(exc)))
    return artifacts,revisions


def parse_tool_outputs(events):
    outputs=[];starts=[]
    for e in events:
        if e['kind']=='tool_start':
            v=e.get('inputs')
            if v is None:
                try:v=json.loads(e['input'])
                except (TypeError,ValueError):v={}
            starts.append(dict(name=e['name'],turn=e['turn'],arguments=v))
        if e['kind']=='tool_end':
            v=e['output']
            if isinstance(v,dict) and 'content' in v:v=v['content']
            if isinstance(v,str):
                try:v=json.loads(v)
                except ValueError:v={'text':v}
            outputs.append(v)
    return starts,outputs


def collect_job(root,case,arm,repetition):
    job=Path(root)/'jobs'/f"{case['id']}__{arm}__r{repetition}"
    raw=json.loads((job/'worker_result.json').read_text()) if (job/'worker_result.json').exists() else dict(status='not_started')
    events=[json.loads(line) for line in (job/'events.jsonl').read_text().splitlines()] if (job/'events.jsonl').exists() else []
    starts,outputs=parse_tool_outputs(events)
    with _GRADE_LOCK:
        artifacts,revisions=artifact_evidence(job/'workspace/projects/conversation')
    final=[]
    for turn in raw.get('turns',[]):
        for m in turn.get('messages',[]):
            if m.get('type')=='ai' and not m.get('tool_calls'):final.append(str(m.get('content','')))
    grade=grade_evidence(case,artifacts,outputs,'\n'.join(final),revisions)
    plans=[];interpreted={}
    for start in starts:
        args=start['arguments'] or {};spec=args.get('spec',args.get('parameters',{}))
        if isinstance(spec,dict) and spec:
            interpreted=dict(n_buses=spec.get('n_buses'),voltage_kv=spec.get('voltage_kv'),total_kw=spec.get('total_kw_min'),
                total_mw=spec.get('total_mw'),n_generators=spec.get('n_generators'),pv_ratio=spec.get('pv_ratio'),
                scene=spec.get('scenario',{}).get('kind'),phase_mode=spec.get('phase_design',{}).get('mode'),
                seed=spec.get('seed'),count=spec.get('count'),repair_strategy=spec.get('repair_policy',{}).get('strategy'))
            plans.append(spec)
    project=job/'workspace/projects/conversation'
    plan_paths=list(project.glob('designs/*/brief.json'))+list(project.glob('research_tasks/*/compiled_plan.json'))+list(project.glob('revisions/*/result.json'))
    for p in sorted(plan_paths,key=lambda p:p.stat().st_mtime_ns):
        try:
            obj=json.loads(p.read_text());plan=obj.get('task_plan',obj.get('plan',{}));plans.append(plan)
            spec=plan.get('base_spec',plan.get('spec',obj.get('specification',{})))
            if spec:
                f=dict(n_buses=spec.get('n_buses'),voltage_kv=spec.get('voltage_kv'),total_kw=spec.get('total_kw_min'),
                       total_mw=spec.get('total_mw'),n_generators=spec.get('n_generators'),pv_ratio=spec.get('pv_ratio'),
                       scene=spec.get('scenario',{}).get('kind'),phase_mode=spec.get('phase_design',{}).get('mode'),
                       seed=spec.get('seed'),count=spec.get('count'),repair_strategy=spec.get('repair_policy',{}).get('strategy'))
                f.update({k:plan.get(k) for k in ('task','response_lower','response_upper','support_mvar')})
                interpreted=f
        except (ValueError,TypeError,AttributeError):pass
    interpretation=required_checks(case.get('requirements',{}),interpreted)
    attempted=unauthorized_actions(case,[s['arguments'] for s in starts]+plans)
    # Committed/delivered physical violations compare original and selected feeder evidence, not narratives.
    physical=[]
    for base_path in (job/'workspace/projects/conversation').glob('**/baseline/model/feeder.json'):
        selected_path=base_path.parents[2]/'selected/feeder.json'
        if selected_path.exists():
            before=json.loads(base_path.read_text());after=json.loads(selected_path.read_text())
            if 'replace_conductor' in case.get('forbidden_actions',[]) and [(e['id'],e['conductor']) for e in before['lines']] != [(e['id'],e['conductor']) for e in after['lines']]:physical.append('replace_conductor')
            if case.get('max_layout_scale_change')==0 and [(b['id'],b['x_km'],b['y_km']) for b in before['buses']] != [(b['id'],b['x_km'],b['y_km']) for b in after['buses']]:physical.append('geometry')
    if attempted or physical:grade['appropriate_outcome']=False;grade['generation_success']=False
    tokens=0
    for e in events:
        if e['kind']=='model_end':
            for obj in walk_dicts(e.get('response',{})):
                if isinstance(obj.get('usage_metadata'),dict):tokens+=obj['usage_metadata'].get('total_tokens',0)
    row=dict(case_id=case['id'],category=case['category'],arm=arm,repetition=repetition,
        expected_outcome=case['expected_outcome'],known_feasible=case.get('witness',{}).get('certified',False),
        witness_class=case.get('witness',{}).get('witness_class'),status=raw['status'],seconds=raw.get('seconds'),
        llm_calls=sum(e['kind']=='model_start' for e in events),llm_completed=sum(e['kind']=='model_end' for e in events),
        service_failures=sum(e['kind']=='model_error' for e in events),tool_calls=len(starts),tokens=tokens or None,
        unauthorized_attempted=len(attempted),unauthorized_committed=len(physical),unauthorized_delivered=len(physical),
        interpretation_total=len(interpretation),interpretation_passed=sum(interpretation.values()),interpretation_checks=interpretation,**grade)
    atomic_json(job/'independent_grade.json',dict(row=row,artifacts=artifacts,revisions=revisions,unauthorized_attempts=attempted,physical_violations=physical))
    return row


def launch(root,workers=8,limit=None):
    root=Path(root).resolve();suite=json.loads((root/'suite.json').read_text())
    if digest(suite)!=json.loads((root/'suite_checksum.json').read_text())['sha256']:raise ValueError('Suite checksum changed')
    jobs=[(c,a,r) for c in suite['cases'] for r in suite['repetitions'] for a in ARMS]
    if limit is not None:jobs=jobs[:limit]
    atomic_json(root/'launch.json',dict(planned=len(suite['cases'])*6,selected=len(jobs),workers=workers,timeout=180))
    def run(job):
        case,arm,rep=job;folder=root/'jobs'/f"{case['id']}__{arm}__r{rep}"
        if (folder/'independent_grade.json').exists():return json.loads((folder/'independent_grade.json').read_text())['row']
        folder.mkdir(parents=True,exist_ok=True)
        if not (folder/'worker_result.json').exists():
            started=time.monotonic()
            with (folder/'worker.log').open('w') as stream:
                process=subprocess.Popen([sys.executable,str(Path.cwd()/'scripts/run_m97_outer_agent.py'),
                    '--root',str(root),'--worker',case['id'],arm,str(rep)],stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
                try:code=process.wait(timeout=180)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid,signal.SIGKILL);process.wait();code=-9
                if not (folder/'worker_result.json').exists():atomic_json(folder/'worker_result.json',dict(status='timeout' if code==-9 else 'worker_crash',seconds=time.monotonic()-started,returncode=code))
        row=collect_job(root,case,arm,rep)
        print(json.dumps({k:row[k] for k in ('case_id','arm','repetition','status','generation_success','appropriate_outcome','llm_calls','seconds')}),flush=True)
        return row
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in as_completed([pool.submit(run,j) for j in jobs]):future.result()
    return report(root)


def report(root):
    import matplotlib;matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    root=Path(root);rows=[json.loads(p.read_text())['row'] for p in sorted((root/'jobs').glob('*/independent_grade.json'))]
    if not rows:return []
    fields=list(rows[0]);
    with (root/'metrics.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
        for row in rows:writer.writerow({k:json.dumps(v) if isinstance(v,(dict,list)) else v for k,v in row.items()})
    groups=[]
    for arm in ARMS:
        subset=[r for r in rows if r['arm']==arm];positive=[r for r in subset if r['expected_outcome']=='generation' and r['known_feasible']]
        negative=[r for r in subset if r['expected_outcome']!='generation']
        groups.append(dict(arm=arm,completed=len(subset),known_feasible_positive=len(positive),generation_success=sum(r['generation_success'] for r in positive),
            negative_cases=len(negative),correct_negative=sum(bool(r['correct_negative_decision']) for r in negative),
            appropriate=sum(r['appropriate_outcome'] for r in subset),llm_calls=sum(r['llm_calls'] for r in subset),
            service_failures=sum(r['service_failures'] for r in subset),timeouts=sum(r['status']=='timeout' for r in subset),
            unauthorized_attempts=sum(r['unauthorized_attempted'] for r in subset),unauthorized_delivered=sum(r['unauthorized_delivered'] for r in subset)))
    atomic_json(root/'summary.json',dict(groups=groups,rows=rows))
    fig,ax=plt.subplots(figsize=(7,4));x=range(len(groups));ax.bar(x,[g['generation_success']/max(1,g['known_feasible_positive']) for g in groups],color=['#71869f','#247b72'])
    ax.set_xticks(list(x),['One-proposal workflow','Full outer workflow']);ax.set_ylim(0,1);ax.set_ylabel('Known-feasible generation success');ax.set_title('M97 real outer-agent study')
    fig.tight_layout();fig.savefig(root/'outer_agent_success.png',dpi=180);plt.close(fig)
    lines=['# M97 real outer-agent evaluation','',f'Observed conversations: {len(rows)} / 288; model qwen3.7-plus. All raw user messages, model/tool events, generated models and independent grades are retained.','',
           'The one-proposal arm permits prerequisite reads followed by one substantive proposal and its execution per user turn. Both arms permit eight outer decisions and execute identical production tools, including their internal LLM workflows. Both share a 24-call total ceiling, 40-second API timeout, zero API retries and 180-second conversation timeout. Eight subprocesses are scheduled concurrently; each case uses a fresh project.','',
           '| Arm | Known-feasible model success | Correct negative decision | Appropriate outcome | LLM calls | Service failures | Timeouts | Unauthorized attempts / delivered |','|---|---:|---:|---:|---:|---:|---:|---:|']
    for g in groups:lines.append(f"| {g['arm']} | {g['generation_success']}/{g['known_feasible_positive']} | {g['correct_negative']}/{g['negative_cases']} | {g['appropriate']}/{g['completed']} | {g['llm_calls']} | {g['service_failures']} | {g['timeouts']} | {g['unauthorized_attempts']} / {g['unauthorized_delivered']} |")
    lines+=['','Task-response interface cases use [0.98,1.02] and are distinct from independently certified nontrivial [1.10,1.30] geometry redesigns. Failed witness construction is retained and excluded from the known-feasible primary denominator. Repeated conversations are correlated within authored case and physical seed; counts are descriptive, not independent-network confidence claims.','',
            'Negative text-only decisions use explicit bilingual clarification/unsupported/unmet cues and remain separately labeled for audit. Artifact fulfillment requires saved model fields, runnable export, and independently repeated AC acceptance. Narrative success cannot substitute for artifacts. Revision checks compare original saved identities and untouched physical fields. Monetary costs are not estimated without provider billing data; token counts are reported when returned.']
    (root/'report.md').write_text('\n'.join(lines)+'\n');return groups
