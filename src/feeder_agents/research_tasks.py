"""Compile research purposes into frozen response and finite-experiment contracts."""
import copy
import fcntl
import json
import re
import shutil
import zipfile
from pathlib import Path

import networkx as nx

from .artifacts import atomic_json,digest,file_digest
from .research_schema import ResearchTaskPlan
from .response_schema import ResponseDesignPlan,ResponseProbe,ResponseSearch
from .response_measurement import create_model,resolve_probes
from .response_design import run_response_design,read_response_result,_manifest


def effective_search(task):
    if task.search is not None:return task.search
    spec=task.base_spec;transmission=task.task=='transmission_transfer';actions=[];bounds={}
    if not task.preserve_equipment:
        if transmission:
            if 'parallel_line' in spec.allowed_repairs:actions.append('parallel_line')
        else:actions.append('replace_conductor')
    if not transmission and not task.preserve_geometry and spec.scenario.positions_km is None:
        actions.append('scale_layout');bounds['max_layout_scale_change']=.75
    return ResponseSearch(allowed_actions=actions,max_rounds=4,candidates_per_round=12,**bounds)


def compile_research_task(payload):
    task=payload if isinstance(payload,ResearchTaskPlan) else ResearchTaskPlan.model_validate(payload)
    # Normalize default numeric types exactly as the serialized response plan
    # will be reloaded, so the frozen reference hash is stable across entry points.
    spec=type(task.base_spec).model_validate(task.base_spec.model_dump())
    model=create_model(spec);transmission=task.task=='transmission_transfer'
    injections=list(task.injection_buses);withdrawals=list(task.withdrawal_buses)
    if not injections:
        if transmission:
            graph=nx.Graph((str(int(e[0])),str(int(e[1]))) for e in model['case']['branch'] if e[10]>0)
            distances=dict(nx.all_pairs_shortest_path_length(graph))
            ids=sorted(str(int(b[0])) for b in model['case']['bus'] if int(b[1])!=3)
            a,b=max(((a,b) for a in ids for b in ids if a<b),key=lambda pair:(distances[pair[0]][pair[1]],pair))
            injections,withdrawals=[a],[b]
        else:
            feeder=model['feeder'];graph=nx.Graph()
            graph.add_weighted_edges_from((e.bus1,e.bus2,e.length_km) for e in feeder.lines)
            distances=nx.single_source_dijkstra_path_length(graph,feeder.source_bus)
            eligible=[l.bus for l in feeder.loads if task.task!='static_pv_impact' or l.pv_kw>0]
            injections=[max(eligible,key=lambda b:(distances[b],b))]
    if task.task=='static_pv_impact':
        pv_buses={l.bus for l in model['feeder'].loads if l.pv_kw>0}
        if not set(injections)<=pv_buses:raise ValueError('PV task probe must use an actual PV bus')
    kind={'voltage_control':'voltage_q','static_pv_impact':'voltage_p','transmission_transfer':'transfer_p'}[task.task]
    probe=ResponseProbe(id='task_response',kind=kind,injection_buses=injections,withdrawal_buses=withdrawals,
        monitor_buses=task.monitor_buses,monitor_branches=task.monitor_branches,phases=task.phases,
        step_mw=task.probe_step or (.5 if transmission else .01),aggregation='max_abs' if transmission else 'mean_abs')
    resolved=resolve_probes(model,[probe])[0]
    search=effective_search(task)
    lo,hi=task.response_lower,task.response_upper;default_interval=lo is None and hi is None
    if default_interval:lo,hi=(.5,.98) if transmission else (1.1,1.3)
    note='Task protocol defaults are research settings, not calibrated engineering classes. Ports and original response reference are frozen before search.'
    plan=ResponseDesignPlan(research_question=task.research_question,base_spec=spec,probes=[probe],
        targets=[dict(probe_id=probe.id,reference=task.response_reference,lower=lo,upper=hi)],search=search,
        assumptions=task.assumptions+[note])
    origins=dict(response_interval='research_protocol_default' if default_interval else 'user',
        ports='user' if task.injection_buses else ('maximum_graph_distance_nonreference_pair' if transmission else 'farthest_eligible_load_by_source_path'),
        search='user' if task.search is not None else 'fresh_synthesis_protocol_default',
        support_mvar='user' if 'support_mvar' in (payload if isinstance(payload,dict) else payload.model_fields_set) else 'research_protocol_default',
        transfer_mw='user' if 'transfer_mw' in (payload if isinstance(payload,dict) else payload.model_fields_set) else 'research_protocol_default')
    context=dict(version='research_task_v1',task=task.task,probe=resolved,support_mvar=task.support_mvar,
        transfer_mw=task.transfer_mw,original_model_hash=_model_hash(model),origins=origins)
    return json.loads(json.dumps(dict(task_plan=task.model_dump(),response_plan=plan.model_dump(),context=context,origins=origins,notice=note)))


def _model_hash(model):
    if model['kind']=='distribution':return digest(model['feeder'].model_dump())
    from .transmission import case_payload
    return digest(dict(case=case_payload(model['case']),metadata=model['metadata']))


def read_research_task_result(root):
    return read_response_result(root)


def run_research_task(payload,workspace,design_id,*,model=None):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',design_id):raise ValueError('Invalid research design ID')
    compiled=compile_research_task(payload);plan=ResponseDesignPlan.model_validate(compiled['response_plan'])
    root=Path(workspace).resolve()/'research_tasks'/design_id;root.mkdir(parents=True,exist_ok=True)
    manifest=dict(compiled=compiled,response_source=_manifest(plan))
    with (root/'.lock').open('w') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (root/'manifest.json').exists():
            if json.loads((root/'manifest.json').read_text())!=manifest:raise ValueError('Research configuration/code changed; use a new ID')
            if not (root/'result.json').exists():raise ValueError('Interrupted research run; use a new ID')
            return read_research_task_result(root)
        atomic_json(root/'manifest.json',manifest);atomic_json(root/'compiled_plan.json',compiled)
        result=run_response_design(plan,root,'response',model=model,task_context=compiled['context'])
        inner=Path(result['directory'])
        shutil.copytree(inner/'selected',root/'selected')
        task_validation=result['selected']['task_validation']
        suitability=dict(task=compiled['context']['task'],accepted=result['target_met'],
            electrical_base_accepted=result['selected'].get('electrical_base_accepted',False),
            original_electrical_base_accepted=result['baseline'].get('electrical_base_accepted',False),
            response_targets=result['selected']['targets'],task_validation=task_validation,
            independent_verification_passed=result['verification_passed'],origins=compiled['origins'],
            scope='Acceptance certifies only the recorded response contract and finite static task experiment; no algorithm ranking or general task guarantee.')
        atomic_json(root/'suitability.json',suitability)
        report=(f"Research task: {compiled['context']['task']}. Status: {'accepted' if result['target_met'] else 'task not satisfied'}.\n\n"
            f"Original-model electrical feasibility: {suitability['original_electrical_base_accepted']}; "
            f"delivered-model reference feasibility: {suitability['electrical_base_accepted']}; finite task experiment: {task_validation['accepted']}; "
            f"independent response verification: {result['verification_passed']}.\n\n"
            f"Response targets: {json.dumps(result['selected']['targets'])}\n\n"
            f"Task metrics: {json.dumps(task_validation['metrics'])}\n\n{compiled['notice']}\n\n{suitability['scope']}")
        (root/'report.md').write_text(report)
        with zipfile.ZipFile(root/'dataset.zip','w',zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob('*')):
                if path.is_file() and path.name not in ('.lock','dataset.zip','result.json'):
                    archive.write(path,path.relative_to(root))
        diagnosis=copy.deepcopy(result['diagnosis'])
        diagnosis['task_checks']=dict(accepted=task_validation['accepted'],failed=[k for k,v in task_validation['checks'].items() if not v],
            error=task_validation.get('error'),scope=task_validation.get('scope'))
        if not task_validation['accepted']:
            diagnosis.update(code='finite_task_experiment_failed',summary='The specified finite task experiment failed: '+', '.join(diagnosis['task_checks']['failed'])+'. The intervention budget was not reduced and the response target was not relaxed.')
        output=dict(status='completed' if result['target_met'] else 'task_not_satisfied',directory=str(root),task=compiled['context']['task'],
            target_met=result['target_met'],verification_passed=result['verification_passed'],
            electrical_base_accepted=suitability['electrical_base_accepted'],original_electrical_base_accepted=suitability['original_electrical_base_accepted'],protected_contract_preserved=result['protected_contract_preserved'],
            accepted_steps=result['accepted_steps'],stop_reason=result['stop_reason'],diagnosis=diagnosis,
            selected=result['selected'],task_validation=task_validation,compiled_plan=compiled,verified_report=report,
            artifacts={str(p.relative_to(root)):file_digest(p) for p in sorted(root.rglob('*')) if p.is_file() and p.name!='.lock'})
        output['result_hash']=digest(output);atomic_json(root/'result.json',output)
        return output
