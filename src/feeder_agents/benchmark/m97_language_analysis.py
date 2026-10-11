"""Accounting and stratified analysis from immutable M97 v2 conversation records.

This module is never imported by a live worker. It corrects provider token parsing
without changing frozen model inputs, tools, outcomes or acceptance predicates.
"""
from __future__ import annotations
import csv
import json
import math
import re
import unicodedata
from collections import Counter,defaultdict
from pathlib import Path
import numpy as np
from feeder_agents.artifacts import atomic_json,digest,file_digest
from .m97_language import unauthorized_actions,walk_dicts
from .m97_language_runtime import parse_tool_outputs

ARMS=['one_proposal','full_outer']


def usage_from_events(events):
    totals=Counter();observed=0;completed=0
    for event in events:
        if event['kind']!='model_end':continue
        completed+=1;response=event.get('response',{})
        usage=response.get('llm_output',{}).get('token_usage') if isinstance(response.get('llm_output'),dict) else None
        if not usage:
            usage=next((d.get('token_usage') for d in walk_dicts(response) if isinstance(d.get('token_usage'),dict)),None)
        if usage:
            observed+=1
            for target,key in [('input_tokens','prompt_tokens'),('output_tokens','completion_tokens'),('total_tokens','total_tokens')]:totals[target]+=int(usage.get(key,0) or 0)
            totals['cached_input_tokens']+=int((usage.get('prompt_tokens_details') or {}).get('cached_tokens',0) or 0)
    return dict(**totals,usage_calls_observed=observed,completed_model_calls=completed)


def network_key(case):
    spec=case.get('witness_spec')
    return digest(dict(base_spec=spec,seed_mode='direct_research' if case.get('task') else 'ordinary_batch'))[:16] if spec else None


def request_key(case):
    messages=[' '.join(unicodedata.normalize('NFKC',message).split()) for message in case['messages']]
    return digest(messages)[:16]


def failure_class(row,grade,events,raw):
    if row['appropriate_outcome']:return 'appropriate_outcome'
    if row['unauthorized_attempted'] or row['unauthorized_delivered']:return 'authorization_violation'
    if row['status']=='timeout':return 'conversation_timeout'
    errors=[e for e in events if e['kind']=='model_error']
    if errors:
        text=' '.join(e.get('error_type','')+' '+e.get('error','') for e in errors).lower()
        if 'timeout' in text or 'timed out' in text:return 'provider_request_timeout'
        if any(v in text for v in ('connection','connecterror','connectionerror','connect error')):return 'provider_connection_error'
        if any(v in text for v in ('ratelimit','rate limit','429')):return 'provider_rate_limit'
        if any(v in text for v in ('authentication','401','403')):return 'provider_authentication_error'
        return 'provider_or_model_error'
    if row['status']=='worker_crash':return 'worker_crash'
    if row['expected_outcome']!='generation':
        if row['expected_outcome'] in {'clarification','unsupported'} and grade.get('artifacts'):return 'unexpected_artifact_on_negative_request'
        return 'negative_decision_proxy_failure'
    if row['category']=='revisions' and not row['revision_preserved']:
        return 'revision_identity_or_preservation_failure' if grade.get('revisions') else 'revision_not_executed'
    artifacts=grade.get('artifacts',[])
    if artifacts:
        if artifacts[-1].get('error'):return 'artifact_validation_error'
        if not artifacts[-1].get('accepted'):
            return 'response_or_task_target_failure' if 'response_verified' in artifacts[-1] else 'physical_acceptance_failure'
        if not artifacts[-1].get('usable'):return 'missing_runnable_export'
        if row['requirements_passed']<row['requirements_total']:return 'semantic_requirement_or_task_mismatch'
    starts,outputs=parse_tool_outputs(events)
    statuses={str(d.get('status')) for d in walk_dicts(outputs) if 'status' in d}
    if statuses&{'planning_failed','invalid_design','invalid_plan'}:return 'structured_interpretation_failure'
    if statuses&{'needs_clarification','unsupported','blocked'}:return 'incorrect_positive_blocking_decision'
    if any(e['kind']=='tool_error' for e in events):return 'tool_execution_error'
    if any('limit' in str(t.get('error','')).lower() for t in raw.get('turns',[])):return 'model_or_tool_call_limit'
    return 'no_usable_artifact'


def policy_for_turn(case,turn):
    if case['category']=='permissions' and turn==0:
        return dict(case,forbidden_actions=['replace_conductor','parallel_line','scale_layout','scale_subtree','rewire_branch','redistribute_load'],max_layout_scale_change=0,numeric_limits={})
    return case


def audit_unauthorized_actions(case,payload):
    # Optional null schema fields are not numeric permissions. Keep raw events intact.
    def without_null_bounds(value):
        if isinstance(value,dict):return {key:without_null_bounds(child) for key,child in value.items() if not (key=='max_layout_scale_change' and child is None)}
        if isinstance(value,list):return [without_null_bounds(child) for child in value]
        return value
    return unauthorized_actions(case,without_null_bounds(payload))


def authorization_audit(case,events,project,raw=None):
    """Separate original-turn grants from later grants; preserve concrete evidence."""
    attempted=[];executions=0;design_turns=defaultdict(list);design_events=defaultdict(list)
    for e in events:
        if e['kind'] not in {'tool_start','tool_end'}:continue
        # Read-only capability schemas describe every action; they are never execution proposals.
        if e['kind']=='tool_start':
            if e['name'].startswith(('describe_','read_','lookup_','inspect_','recall_','search_','list_')):continue
            executions+=1;payload=e.get('inputs')
            if payload is None:
                try:payload=json.loads(e.get('input','{}'))
                except ValueError:payload={}
            if isinstance(payload,dict):
                for key in ('design_id','revision_id','experiment_id'):
                    if payload.get(key):
                        design_turns[str(payload[key])].append(e['turn'])
                        design_events[str(payload[key])].append(dict(turn=e['turn'],tool=e['name'],run_id=e.get('run_id'),arguments=payload))
        else:
            payload=e.get('output',{})
            if isinstance(payload,dict):
                if str(payload.get('name','')).startswith(('describe_','read_','lookup_','inspect_','recall_','search_','list_')):continue
                payload=payload.get('content',payload)
            if isinstance(payload,str):
                try:payload=json.loads(payload)
                except ValueError:continue
        issues=audit_unauthorized_actions(policy_for_turn(case,e['turn']),payload)
        for issue in issues:attempted.append(dict(turn=e['turn'],event=e['kind'],action=issue['action'],evidence=issue['evidence']))
    # Preview, final selected-path commitment and delivered output have separate scopes.
    previews=[];committed=[]
    def matching_design(path):
        for part in [path,*path.parents]:
            matches=[key for key in design_turns if part.name==key or part.name.startswith(key+'_')]
            if matches:return max(matches,key=len)
        return None
    def turns_for_path(path):return design_turns.get(matching_design(path),[len(case.get('messages',[]))-1])
    def events_for_path(path):return design_events.get(matching_design(path),[])
    narrative_text='\n'.join(str(message.get('content','')) for turn in (raw or {}).get('turns',[]) for message in turn.get('messages',[]) if message.get('type')=='ai' and not message.get('tool_calls'))
    tool_outputs=[json.dumps(event.get('output'),ensure_ascii=False) for event in events if event['kind']=='tool_end']
    for path in Path(project).glob('**/history.json'):
        try:history=json.loads(path.read_text())
        except ValueError:continue
        if not isinstance(history,list):continue
        for record in history:
            if not isinstance(record,dict):continue
            action=record.get('action')
            if not isinstance(action,dict):continue
            turn=turns_for_path(path.parent)[-1]
            for issue in audit_unauthorized_actions(policy_for_turn(case,turn),action):
                item=dict(path=str(path),candidate=record.get('candidate'),action=issue['action'])
                previews.append(item)
                if record.get('committed'):committed.append(item)
    physical=[]
    for base in Path(project).glob('**/baseline/model/feeder.json'):
        run=base.parents[2];selected=run/'selected/feeder.json'
        if not selected.exists():continue
        before=json.loads(base.read_text());after=json.loads(selected.read_text())
        turn=turns_for_path(run)[-1];policy=policy_for_turn(case,turn)
        a={line['id']:line for line in before['lines']};b={line['id']:line for line in after['lines']}
        x={bus['id']:bus for bus in before['buses']};y={bus['id']:bus for bus in after['buses']}
        issues=[];metrics={}
        if a.keys()!=b.keys() or x.keys()!=y.keys():issues.append('changed_model_identity')
        else:
            rewired=[key for key in a if {a[key]['bus1'],a[key]['bus2']}!={b[key]['bus1'],b[key]['bus2']}]
            degree=Counter(node for line in b.values() for node in (line['bus1'],line['bus2']))
            source=before.get('source_bus',before['buses'][0]['id']);origin=(x[source]['x_km'],x[source]['y_km'])
            radial_ratios=[math.dist(origin,(y[key]['x_km'],y[key]['y_km']))/math.dist(origin,(x[key]['x_km'],x[key]['y_km'])) for key in x if math.dist(origin,(x[key]['x_km'],x[key]['y_km']))>1e-12]
            metrics=dict(max_changed_conductors=sum(a[key]['conductor']!=b[key]['conductor'] for key in a),max_rewired_lines=len(rewired),
                max_branch_length_change=max((abs(b[key]['length_km']/a[key]['length_km']-1) for key in a),default=0),
                max_fixed_branch_length_change=max((abs(b[key]['length_km']/a[key]['length_km']-1) for key in a if key not in rewired),default=0),
                max_layout_scale_change=max((abs(ratio-1) for ratio in radial_ratios),default=0),
                max_node_displacement_km=max((math.dist((x[key]['x_km'],x[key]['y_km']),(y[key]['x_km'],y[key]['y_km'])) for key in x),default=0),
                max_rewire_length_ratio=max((b[key]['length_km']/a[key]['length_km'] for key in rewired),default=0),max_node_degree=max(degree.values(),default=0))
            for field,limit in policy.get('numeric_limits',{}).items():
                if field in metrics and metrics[field]>limit+1e-9:issues.append(field)
            for action,field in [('replace_conductor','max_changed_conductors'),('rewire_branch','max_rewired_lines'),('scale_layout','max_fixed_branch_length_change'),('scale_subtree','max_node_displacement_km')]:
                if action in policy.get('forbidden_actions',[]) and metrics[field]>1e-9:issues.append(action)
            if policy.get('max_layout_scale_change') is not None and metrics['max_layout_scale_change']>policy['max_layout_scale_change']+1e-9:issues.append('max_layout_scale_change')
            if 'redistribute_load' in policy.get('forbidden_actions',[]) and before.get('loads')!=after.get('loads'):issues.append('redistribute_load')
        references=[str(run.resolve()),str(run.relative_to(project))]
        physical.append(dict(directory=str(run),turn=turn,metrics=metrics,violations=issues,ambiguous_reused_id=len(set(turns_for_path(run)))>1,
            original_model_path=str(base),selected_model_path=str(selected),original_model_sha256=file_digest(base),selected_model_sha256=file_digest(selected),
            original_user_turn=case.get('messages',[''])[turn] if 0<=turn<len(case.get('messages',[])) else None,
            actual_tool_proposals=events_for_path(run),
            tool_result_exposed=any(reference in output for reference in references for output in tool_outputs),
            final_narrative_explicitly_delivered=any(reference in narrative_text for reference in references)))
    zero_cap=[a for a in attempted if a['action']=='replace_conductor' and a['evidence'].get('max_changed_conductors')==0]
    effective=[a for a in attempted if a not in zero_cap]
    return dict(zero_cap_equipment_tier_attempts=zero_cap,positive_or_unbounded_forbidden_attempts=effective,effective_attempted_any=bool(effective or previews),outer_execution_calls=executions,language_attempts=attempted,unauthorized_previews=previews,unauthorized_selected_path_commits=committed,
                attempted_any=bool(attempted or previews),committed_any=bool(committed or any(record['violations'] for record in physical)),
                physical_selected_unauthorized_any=any(record['violations'] for record in physical),
                tool_result_exposed_unauthorized_any=any(record['violations'] and record['tool_result_exposed'] for record in physical),
                final_narrative_delivered_unauthorized_any=any(record['violations'] and record['final_narrative_explicitly_delivered'] for record in physical),
                selected_physical_audit=physical)


def revision_audit(case,grade,events):
    """Independent post-run audit; does not overwrite frozen acceptance."""
    if not case.get('revision'):return dict(applicable=False,passed=True,steps=[])
    artifacts={Path(a['path']).parent.parent.name:a for a in grade.get('artifacts',[]) if '/experiments/' in a['path']}
    revisions={r.get('revision_id'):r for r in grade.get('revisions',[])}
    by_turn={}
    for event in events:
        if event['kind']!='tool_end' or event['turn'] not in (1,2):continue
        output=event.get('output',{})
        if isinstance(output,dict):output=output.get('content',output)
        if isinstance(output,str):
            try:output=json.loads(output)
            except ValueError:continue
        if isinstance(output,dict) and '/revisions/' in str(output.get('directory','')):
            rid=Path(output['directory']).name
            if rid in artifacts:by_turn[event['turn']]=rid
    steps=[];previous=None;suffix=int(case['id'].rsplit('_',1)[1])
    for turn in (1,2):
        rid=by_turn.get(turn);revision=revisions.get(rid,{})
        parent=artifacts.get(revision.get('parent_id'),{});child=artifacts.get(rid,{})
        facts=child.get('facts',{});before=parent.get('groups',{});after=child.get('groups',{})
        checks=dict(saved=bool(child),electrical_accepted=bool(child.get('accepted')),parent_verified=bool(revision.get('parent_verified')),
                    consecutive_parent=turn==1 or revision.get('parent_id')==previous)
        groups=['topology','equipment','voltage']
        if suffix<=3:
            groups+=['geometry','loads'];pv=.4 if turn==1 else .6;kw=case['witness_spec']['total_kw_min']
        elif suffix<=5:
            groups+=['geometry'];pv=.2;kw=case['witness_spec']['total_kw_min']*1.1**turn
        else:
            groups+=['loads'];pv=.2 if turn==1 else .3;kw=case['witness_spec']['total_kw_min']
            if turn==2:groups+=['geometry']
        checks.update({f'preserved_{g}':g in before and g in after and before[g]==after[g] for g in groups})
        checks['pv_ratio']=isinstance(facts.get('pv_ratio'),(int,float)) and math.isclose(facts['pv_ratio'],pv,rel_tol=1e-6)
        checks['total_kw']=isinstance(facts.get('total_kw'),(int,float)) and math.isclose(facts['total_kw'],kw,rel_tol=1e-6)
        if suffix==6 and turn==1:
            old=before.get('geometry',{});new=after.get('geometry',{});source=before.get('topology',{}).get('source')
            points={b[0]:b[1:] for b in old.get('buses',[])};actual={b[0]:b[1:] for b in new.get('buses',[])}
            origin=points.get(source);ok=bool(origin and points.keys()==actual.keys())
            if ok:ok=all(math.isclose(actual[bid][j],origin[j]+1.05*(xy[j]-origin[j]),rel_tol=1e-8,abs_tol=1e-9) for bid,xy in points.items() for j in (0,1))
            old_lines=dict(old.get('lengths',[]));new_lines=dict(new.get('lengths',[]))
            checks['geometry_scale_1_05']=ok and old_lines.keys()==new_lines.keys() and all(math.isclose(new_lines[k],1.05*v,rel_tol=1e-8,abs_tol=1e-9) for k,v in old_lines.items())
        steps.append(dict(turn=turn,revision_id=rid,parent_id=revision.get('parent_id'),checks=checks,passed=all(checks.values())))
        previous=rid
    return dict(applicable=True,passed=all(s['passed'] for s in steps),steps=steps)


def negative_reason_audit(case,events,raw,artifacts=None):
    """Automatic last-turn reason proxy; never represented as expert labels."""
    if case['expected_outcome']=='generation':return dict(applicable=False,supported=True)
    last=len(case['messages'])-1;texts=[];statuses=set()
    for event in events:
        if event['kind']!='tool_end' or event['turn']!=last:continue
        output=event.get('output',{})
        if isinstance(output,dict):output=output.get('content',output)
        if isinstance(output,str):
            try:output=json.loads(output)
            except ValueError:continue
        for obj in walk_dicts(output):
            if 'status' in obj:statuses.add(str(obj['status']))
            for key in ('issues','questions','verified_report','summary','diagnosis'):
                value=obj.get(key)
                if isinstance(value,str):texts.append(value)
                elif isinstance(value,list):texts.extend(str(v) for v in value)
    turns=raw.get('turns',[])
    if turns:
        messages=turns[-1].get('messages',[]);human=max((i for i,m in enumerate(messages) if m.get('type')=='human'),default=-1)
        texts.extend(str(m.get('content','')) for m in messages[human+1:] if m.get('type')=='ai' and not m.get('tool_calls'))
    text=re.sub(r'[*_`]','', '\n'.join(texts).lower());cid=case['id'];groups=[]
    if cid.startswith('contradictions_'):
        index=int(cid.rsplit('_',1)[1]);groups={1:[['23'],['29']],2:[['10'],['20']],3:[['pv','\u5149\u4f0f'],['zero','no pv','0','\u65e0\u5149\u4f0f','\u96f6']],4:[['1.3'],['1.1']],5:[['coordinate','geometry','\u5750\u6807','\u51e0\u4f55'],['30','1.3']],6:[['120'],['240']]}[index]
    elif cid.startswith('unsupported_'):
        index=int(cid.rsplit('_',1)[1]);groups=[{1:['n-1','n−1','contingenc','\u6545\u969c','\u5b89\u5168'],2:['transient','rotor','dynamic','\u6682\u6001','\u52a8\u6001'],3:['gis','survey','\u771f\u5b9e','\u5b9e\u6d4b'],4:['365','hour','commitment','\u65f6\u5e8f','\u8c03\u5ea6'],5:['opf','optimal','\u6700\u4f18'],6:['protect','relay','\u4fdd\u62a4','\u6574\u5b9a']}[index]]
    else:groups=[['unmet','not satisfied','not achieved','target','\u672a\u6ee1\u8db3','\u672a\u8fbe\u5230','\u672a\u8fbe\u6210','\u76ee\u6807']]
    checks=[any(word in text for word in alternatives) for alternatives in groups]
    expected=case['expected_outcome']
    cues={'unsupported':['not support','not a supported','unsupported','cannot deliver','cannot provide','outside the scope','explicitly excluded','\u4e0d\u652f\u6301','\u65e0\u6cd5\u63d0\u4f9b','\u8d85\u51fa'],
          'clarification':['clarif','conflict','contradict','inconsistent','incompatible','mutually exclusive','cannot satisfy both','\u6f84\u6e05','\u51b2\u7a81','\u77db\u76fe'],
          'truthful_unmet':['unmet','not satisfied','not achieved','targetnotmet','\u672a\u6ee1\u8db3','\u672a\u8fbe\u5230','\u672a\u8fbe\u6210'],
          'bounded_outcome':['unmet','not satisfied','not achieved','targetnotmet','\u672a\u6ee1\u8db3','\u672a\u8fbe\u5230','\u672a\u8fbe\u6210']}[expected]
    status_codes={'unsupported':{'unsupported'},'clarification':{'needs_clarification','blocked'},'truthful_unmet':{'task_not_satisfied','not_satisfied','target_not_met'},'bounded_outcome':{'task_not_satisfied','not_satisfied','target_not_met'}}[expected]
    decision=bool(statuses&status_codes) or any(cue in text for cue in cues)
    if expected in {'unsupported','clarification'} and artifacts:decision=False
    return dict(applicable=True,supported=bool(text and all(checks)),expected_decision_and_reason=bool(text and all(checks) and decision),
        proxy='Automatic normalized expected-decision and topic/reason matching on last-turn diagnostics; not human/expert annotation',checks=checks,evidence=text[:2500])


def chain_audit(case,row,grade,events,revision):
    """Independent overlapping stage diagnostics, not a sequential success funnel."""
    names={e.get('name') for e in events if e['kind']=='tool_start'}
    if case.get('task'):
        compatible='design_research_grid' in names
    elif case.get('revision'):
        compatible='revise_existing_case' in names
    else:
        compatible=bool(names & {'generate_cases','execute_plan','design_from_language'})
    artifacts=grade.get('artifacts',[])
    return dict(structured_requirements_complete=bool(row['interpretation_total'] and row['interpretation_passed']==row['interpretation_total']),
        compatible_entrypoint_invoked=compatible,saved_artifact=bool(artifacts),
        accepted_runnable_artifact=any(a.get('accepted') and a.get('usable') for a in artifacts),
        all_revision_steps_preserved=revision['passed'] if revision['applicable'] else None,
        first_revision_passed=revision['steps'][0]['passed'] if revision['applicable'] else None,
        second_revision_passed=revision['steps'][1]['passed'] if revision['applicable'] else None,
        request_fulfilled=bool(row['generation_success']),conversation_completed=row['status']=='completed',
        fulfilled_and_completed=bool(row['generation_success'] and row['status']=='completed'))


def load_rows(root):
    root=Path(root);suite=json.loads((root/'suite.json').read_text());cases={c['id']:c for c in suite['cases']};rows=[]
    certificate_path=root/'analysis/certificate_audit.json'
    certificates={c['case_id']:c for c in json.loads(certificate_path.read_text())['cases']} if certificate_path.exists() else {}
    semantic_path=root/'analysis/semantic_equivalence.json'
    semantic_rows={(r['case_id'],r['arm'],r['repetition']):r for r in json.loads(semantic_path.read_text())['rows']} if semantic_path.exists() else {}
    for p in sorted((root/'jobs').glob('*/independent_grade.json')):
        grade=json.loads(p.read_text());row=grade['row'].copy();case=cases[row['case_id']]
        events=[json.loads(l) for l in (p.parent/'events.jsonl').read_text().splitlines()] if (p.parent/'events.jsonl').exists() else []
        raw=json.loads((p.parent/'worker_result.json').read_text())
        usage=usage_from_events(events);row.update(usage);row['tokens']=usage.get('total_tokens') if usage['usage_calls_observed'] else None
        row['network_id']=network_key(case)
        row['request_id']=request_key(case)
        row['research_task']=case.get('task') or 'ordinary_generation'
        baseline_spec=case.get('witness_spec') or {}
        row['network_kind']=baseline_spec.get('network_kind','not_applicable')
        row['scene']=baseline_spec.get('scenario',{}).get('kind',row['network_kind'])
        row['phase_mode']=baseline_spec.get('phase_design',{}).get('mode','balanced' if row['network_kind']=='transmission' else 'not_applicable')
        row['failure_class']=failure_class(row,grade,events,raw)
        row['completed_appropriate_outcome']=bool(row['appropriate_outcome'] and row['status']=='completed')
        row['provider_error_types']=dict(Counter(e.get('error_type','unknown') for e in events if e['kind']=='model_error'))
        row['tool_error_types']=dict(Counter(e.get('error_type','unknown') for e in events if e['kind']=='tool_error'))
        row['first_tool_error']=next((e.get('error','')[:600] for e in events if e['kind']=='tool_error'),None)
        diagnostics=[]
        for brief_path in p.parent.glob('workspace/projects/conversation/designs/*/brief.json'):
            try:
                brief=json.loads(brief_path.read_text());diagnostics.extend(str(v) for v in brief.get('issues',[]))
                diagnostics.extend(str(e.get('diagnosis','')) for e in brief.get('adaptive_trace',{}).get('events',[]) if e.get('diagnosis'))
            except (ValueError,TypeError,AttributeError):pass
        row['production_diagnoses']=diagnostics
        row['diagnosis_flags']=[name for name,pattern in [('source_quote_mismatch','evidence is not an exact request quote'),('ledger_scope_violation','complete_ledger changed'),('empty_list_requirement_guard','expected=[] observed=[]'),('geometry_permission_guard','\u7a7a\u95f4\u7f29\u653e\u9700\u8981\u7528\u6237\u660e\u786e\u6388\u6743')] if any(pattern in text for text in diagnostics)]
        row['outer_model_calls']=sum(e['kind']=='model_start' and 'outer_model' in (e.get('tags') or []) for e in events)
        row['internal_model_calls']=row['llm_calls']-row['outer_model_calls']
        row['provider_error_events']=sum(e['kind']=='model_error' for e in events)
        audit=authorization_audit(case,events,p.parent/'workspace/projects/conversation',raw)
        row['authorization_attempted_any']=audit['attempted_any'] or row['unauthorized_attempted']>0
        row['zero_cap_equipment_tier_attempt_any']=bool(audit['zero_cap_equipment_tier_attempts'])
        row['effective_authorization_attempt_any']=audit['effective_attempted_any']
        row['authorization_committed_any']=audit['committed_any'] or row['unauthorized_committed']>0
        row['frozen_unauthorized_delivered_selected_definition']=row['unauthorized_delivered']>0
        row['authorization_tool_result_exposed_any']=audit['tool_result_exposed_unauthorized_any']
        row['authorization_delivered_any']=audit['final_narrative_delivered_unauthorized_any']
        row['outer_execution_calls']=audit['outer_execution_calls']
        revision=revision_audit(case,grade,events)
        negative=negative_reason_audit(case,events,raw,grade.get('artifacts',[]))
        chain=chain_audit(case,row,grade,events,revision)
        row.update({f'chain_{key}':value for key,value in chain.items()})
        row['frozen_generation_success']=row['generation_success']
        row['frozen_appropriate_outcome']=row['appropriate_outcome']
        row['postrun_revision_audit_pass']=revision['passed']
        row['postrun_negative_reason_supported']=negative['supported'] if negative['applicable'] else None
        row['postrun_negative_decision_reason_proxy']=negative.get('expected_decision_and_reason') if negative['applicable'] else None
        row['unexpected_artifact_on_negative_request']=bool(case['expected_outcome'] in {'clarification','unsupported'} and grade.get('artifacts'))
        row['audited_generation_success']=bool(row['generation_success'] and revision['passed'] and not row['authorization_attempted_any'] and not row['authorization_committed_any'] and not row['authorization_delivered_any'])
        row['audited_appropriate_outcome']=bool(row['appropriate_outcome'] and revision['passed'] and not row['authorization_attempted_any'] and not row['authorization_committed_any'] and not row['authorization_delivered_any'] and negative['supported'])
        row['audited_complete_appropriate_outcome']=bool(row['audited_appropriate_outcome'] and row['status']=='completed')
        equivalent=semantic_rows.get((row['case_id'],row['arm'],row['repetition']))
        row['semantic_artifact_fulfillment']=bool(equivalent['semantic_artifact_fulfillment']) if equivalent else row['audited_generation_success']
        row['semantic_fulfilled_and_completed']=bool(row['semantic_artifact_fulfillment'] and row['status']=='completed')
        row['semantic_equivalence_audit_applied']=equivalent is not None
        row['semantic_outcome_class']='fulfilled_artifact' if row['semantic_artifact_fulfillment'] else row['failure_class']
        if equivalent and not row['semantic_artifact_fulfillment'] and equivalent['artifacts']:
            last=equivalent['artifacts'][-1];checks=last.get('checks',{})
            if not checks.get('authorized_selected_changes',True):row['semantic_outcome_class']='unauthorized_selected_changes'
            elif any(key.startswith('source_') and not value for key,value in checks.items()):row['semantic_outcome_class']='source_model_requirement_mismatch'
            elif any(not p.get('checks',{}).get('original_target_bounds',False) for p in last.get('probes',[])):row['semantic_outcome_class']='source_response_target_mismatch'
            elif any(not p.get('checks',{}).get('finite_requested_experiment',False) for p in last.get('probes',[])):row['semantic_outcome_class']='missing_or_invalid_finite_task_evidence'
            else:row['semantic_outcome_class']='response_or_task_evidence_failure'
        row['revision_conversation']=bool(case.get('revision'))
        row['revision_records']=len(grade.get('revisions',[]))
        row['primary_positive']=bool(case['expected_outcome']=='generation' and case.get('witness',{}).get('certified'))
        cert=certificates.get(case['id'],{})
        row['original_certificate_valid']=bool(cert.get('original_witness_valid',row['primary_positive']))
        row['amended_certificate_valid']=bool(cert.get('amended_witness_valid',row['primary_positive']))
        row['stratum']='negative_decision' if case['expected_outcome']!='generation' else 'multi_turn_revision' if case.get('revision') else 'permission_amendment' if case.get('permission_amendment') else case.get('witness',{}).get('witness_class','uncertified')
        rows.append(row)
        atomic_json(root/'analysis'/'authorization'/p.parent.name/'audit.json',dict(authorization=audit,revision=revision,negative_reason=negative,chain=chain))
    return suite,rows


def summary_group(rows):
    positives=[r for r in rows if r['primary_positive']];negatives=[r for r in rows if r['expected_outcome']!='generation']
    return dict(n=len(rows),primary_n=len(positives),original_certificate_primary_n=sum(r['original_certificate_valid'] for r in positives),amended_certificate_primary_n=sum(r['amended_certificate_valid'] for r in positives),generation_success=sum(r['generation_success'] for r in positives),original_certificate_success=sum(r['generation_success'] and r['original_certificate_valid'] for r in positives),audited_generation_success=sum(r['audited_generation_success'] for r in positives),
        semantic_artifact_fulfillment=sum(r['semantic_artifact_fulfillment'] for r in positives),semantic_fulfilled_and_completed=sum(r['semantic_fulfilled_and_completed'] for r in positives),
        negative_n=len(negatives),correct_negative=sum(bool(r['correct_negative_decision']) for r in negatives),negative_decision_reason_proxy=sum(bool(r['postrun_negative_decision_reason_proxy']) for r in negatives),unexpected_negative_artifact=sum(r['unexpected_artifact_on_negative_request'] for r in negatives),
        appropriate=sum(r['appropriate_outcome'] for r in rows),completed_appropriate=sum(r['completed_appropriate_outcome'] for r in rows),audited_complete_appropriate=sum(r['audited_complete_appropriate_outcome'] for r in rows),interpretation_correct=sum(r['interpretation_passed'] for r in rows),
        interpretation_total=sum(r['interpretation_total'] for r in rows),requirement_correct=sum(r['requirements_passed'] for r in rows),
        requirement_total=sum(r['requirements_total'] for r in rows),llm_calls=sum(r['llm_calls'] for r in rows),
        outer_calls=sum(r['outer_model_calls'] for r in rows),internal_calls=sum(r['internal_model_calls'] for r in rows),
        tokens=sum(r['tokens'] or 0 for r in rows),token_calls_observed=sum(r['usage_calls_observed'] for r in rows),
        completed_calls=sum(r['completed_model_calls'] for r in rows),mean_seconds=float(np.mean([r['seconds'] or 0 for r in rows])) if rows else None,
        median_seconds=float(np.median([r['seconds'] or 0 for r in rows])) if rows else None,
        service_error_events=sum(r['provider_error_events'] for r in rows),timeouts=sum(r['status']=='timeout' for r in rows),
        unauthorized_attempted_conversations=sum(r['authorization_attempted_any'] for r in rows),zero_cap_equipment_tier_conversations=sum(r['zero_cap_equipment_tier_attempt_any'] for r in rows),effective_forbidden_attempt_conversations=sum(r['effective_authorization_attempt_any'] for r in rows),
        unauthorized_committed_conversations=sum(r['authorization_committed_any'] for r in rows),
        unauthorized_tool_result_exposed_conversations=sum(r['authorization_tool_result_exposed_any'] for r in rows),
        unauthorized_delivered_conversations=sum(r['authorization_delivered_any'] for r in rows),
        execution_conversations=sum(r['outer_execution_calls']>0 for r in rows),artifact_conversations=sum(r['artifacts']>0 for r in rows),
        status_counts=dict(Counter(r['status'] for r in rows)),
        diagnostic_counts=dict(Counter(flag for r in rows for flag in r['diagnosis_flags'])),
        failure_classes=dict(Counter(r['failure_class'] for r in rows)))


def paired(rows,key,metric,positive_only=False):
    groups=defaultdict(lambda:defaultdict(list))
    for row in rows:
        if positive_only and not row['primary_positive']:continue
        if row.get(key) is None:continue
        if row[metric] is not None:groups[row[key]][row['arm']].append(float(row[metric]))
    differences=[];details=[]
    for group,arms in sorted(groups.items()):
        if all(arm in arms for arm in ARMS):
            means={arm:float(np.mean(arms[arm])) for arm in ARMS};delta=means['full_outer']-means['one_proposal'];differences.append(delta)
            details.append(dict(cluster=group,**means,difference=delta,n_one=len(arms['one_proposal']),n_full=len(arms['full_outer'])))
    if not differences:return dict(clusters=0)
    values=np.array(differences);rng=np.random.default_rng(970011)
    boot=np.mean(values[rng.integers(0,len(values),size=(10000,len(values)))],axis=1)
    signs=rng.choice([-1,1],size=(10000,len(values)))
    p=float((1+np.sum(abs(np.mean(signs*values,axis=1))>=abs(np.mean(values))-1e-12))/10001)
    return dict(cluster_unit=key,metric=metric,positive_only=positive_only,clusters=len(values),difference=float(np.mean(values)),
        bootstrap_95=[float(x) for x in np.quantile(boot,[.025,.975])],sign_flip_p=p,
        wins=int(np.sum(values>0)),ties=int(np.sum(values==0)),losses=int(np.sum(values<0)),details=details)


def write_csv(path,rows):
    if not rows:return
    fields=list(dict.fromkeys(k for row in rows for k in row))
    with Path(path).open('w') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
        for row in rows:writer.writerow({k:json.dumps(v) if isinstance(v,(dict,list)) else v for k,v in row.items()})


def representative_trajectories(root,suite,rows):
    """Deterministic illustrative selection; never changes statistical membership."""
    root=Path(root).resolve()
    cases={case['id']:case for case in suite['cases']}
    full=sorted([r for r in rows if r['arm']=='full_outer'],key=lambda r:(r['case_id'],r['repetition']))
    choices=[('Complete multiple revision',lambda r:r['revision_conversation'] and r['audited_complete_appropriate_outcome']),
             ('Ordinary generation',lambda r:r['category']=='ordinary' and r['research_task']=='ordinary_generation' and r['audited_complete_appropriate_outcome']),
             ('Research-purpose generation',lambda r:r['primary_positive'] and r['research_task']!='ordinary_generation' and r['semantic_fulfilled_and_completed']),
             ('Truthful blocked outcome',lambda r:r['expected_outcome']=='truthful_unmet' and r['audited_complete_appropriate_outcome'])]
    lines=['# Illustrative full-workflow trajectories','',
        'These examples are selected after the run by a declared mechanical rule: the lexicographically first case ID, then smallest repetition, with a completed, independently audited appropriate full-outer outcome in each requested class. Research-purpose selection uses semantic-equivalent artifact fulfillment; ordinary/revision selection uses the unchanged audited artifact requirements. They illustrate behavior and never select, filter or replace the complete statistical sample. If no qualifying full-workflow record exists, that absence is stated; the one-proposal arm is never substituted. All original messages are English.','']
    for label,predicate in choices:
        lines+=['## '+label,'']
        row=next((r for r in full if predicate(r)),None)
        if row is None:
            lines+=['No qualifying completed and independently audited full-outer conversation was observed.','']
            if label!='Research-purpose generation':continue
            row=next((r for r in full if r['primary_positive'] and r['research_task']!='ordinary_generation'),None)
            if row is None:continue
            lines+=['The first research-purpose attempt is shown below as an explicitly unsuccessful diagnostic example.','']
        case=cases[row['case_id']];job=root/'jobs'/f"{row['case_id']}__{row['arm']}__r{row['repetition']}"
        grade=json.loads((job/'independent_grade.json').read_text())
        audit=json.loads((root/'analysis/authorization'/job.name/'audit.json').read_text())
        events=[json.loads(line) for line in (job/'events.jsonl').read_text().splitlines()]
        lines+=[f"Record: `{job.name}`. Status: `{row['status']}`. Frozen saved-model success: `{row['generation_success']}`. Semantic-evidence artifact fulfillment: `{row['semantic_artifact_fulfillment']}`. Actual model calls: {row['llm_calls']} ({row['internal_model_calls']} internal); elapsed {row['seconds']:.2f} seconds.",'',
            f"Raw evidence: `jobs/{job.name}/events.jsonl`, `worker_result.json`, and `independent_grade.json`.",'']
        for turn,message in enumerate(case['messages'],1):lines += [f'Original user turn {turn}:','',f'> {message}','']
        lines+=['| User turn | Actual production tool | Selected literal arguments |','|---:|---|---|']
        starts,_=parse_tool_outputs(events)
        for event in starts:
            arguments=event['arguments'] or {};compact={key:value for key,value in arguments.items() if key in {'experiment_id','design_id','parent_id','revision_id','sample_index','execute','plan_id','run_id','feedback'}}
            argument_text=json.dumps(compact,ensure_ascii=False).replace('|','\\|')
            lines.append(f"| {event['turn']+1} | {event['name']} | `{argument_text}` |")
        lines+=['','| Saved artifact (creation order) | Buses | Demand kW | PV fraction | Independent electrical acceptance | Runnable export + visualization |','|---|---:|---:|---:|---|---|']
        for artifact in grade.get('artifacts',[]):
            facts=artifact.get('facts',{});path=Path(artifact['path']).relative_to(root)
            lines.append(f"| `{path}` | {facts.get('n_buses')} | {facts.get('total_kw')} | {facts.get('pv_ratio')} | {artifact.get('accepted')} | {artifact.get('usable')} |")
        revision=audit['revision']
        if revision['applicable']:
            lines+=['','Independent per-step revision checks:','']
            for step in revision['steps']:
                lines.append(f"- User turn {step['turn']+1}: parent `{step['parent_id']}` → revision `{step['revision_id']}`; pass `{step['passed']}`. "+'; '.join(f"{key}={value}" for key,value in step['checks'].items())+'.')
        lines+=['',f"Authorization audit: attempted `{audit['authorization']['attempted_any']}`; selected-path committed `{audit['authorization']['committed_any']}`; delivered forbidden changes `{row['authorization_delivered_any']}`.",'']
        if audit['negative_reason']['applicable']:
            lines+=['Automatic expected-reason evidence (not expert annotation):','', '> '+audit['negative_reason'].get('evidence','').replace('\n','\n> '),'']
        if row['semantic_equivalence_audit_applied']:
            semantic=json.loads((root/'analysis/semantic_equivalence.json').read_text())
            evidence=next(r for r in semantic['rows'] if (r['case_id'],r['arm'],r['repetition'])==(row['case_id'],row['arm'],row['repetition']))
            for artifact in evidence['artifacts']:
                for probe in artifact.get('probes',[]):
                    lines+=[f"Semantic solver evidence: full/half response ratios {probe['full_ratio']}/{probe['half_ratio']}; finite checks {probe['finite_checks']}; saved state files `{probe['solver_evidence']}`.",'']
    (root/'analysis/representative_trajectories.md').write_text('\n'.join(lines)+'\n')


def coverage_audit(root,suite,rows):
    cases=[];tools_by_arm=defaultdict(Counter);errors=[]
    for case in suite['cases']:
        text='\n'.join(case['messages']);spec=case.get('witness_spec') or {}
        cases.append(dict(case_id=case['id'],request_id=request_key(case),category=case['category'],
            network_kind=spec.get('network_kind'),scene=spec.get('scenario',{}).get('kind'),phase_mode=spec.get('phase_design',{}).get('mode'),
            units=sorted(set(re.findall(r'\b\d+(?:\.\d+)?\s*(?:kvar|Mvar|MVAr|GW|MW|kW|kV|km)\b',text))),
            explicit_bus_range='31 through 39' in text,intended_024mw_present='0.24 MW' in text,intended_05gw_present='0.5 GW' in text))
    for row in rows:
        job=Path(root)/'jobs'/f"{row['case_id']}__{row['arm']}__r{row['repetition']}"
        events=[json.loads(line) for line in (job/'events.jsonl').read_text().splitlines()]
        tools_by_arm[row['arm']].update(set(e['name'] for e in events if e['kind']=='tool_start'))
        starts={event['run_id']:event for event in events if event['kind'] in ('tool_start','model_start')}
        for event in events:
            if event['kind'] not in ('model_error','tool_error','turn_error'):continue
            start=starts.get(event.get('run_id'),{})
            errors.append(dict(case_id=row['case_id'],arm=row['arm'],repetition=row['repetition'],kind=event['kind'],turn=event['turn'],
                seconds=event['seconds'],error_type=event['error_type'],tool=start.get('name'),model_tags=start.get('tags'),error=event['error']))
    audit=dict(language='English only',unique_requests=len({c['request_id'] for c in cases}),cases=cases,
        missing_scope=['multilingual prompts','hierarchical network language workflow','unbalanced phase language workflow','distribution demand expressed as 0.24 MW','transmission demand expressed as 0.5 GW'],
        unit_template_defect='base_text renders floats (240.0 kW and 500.0 MW); replacements targeting integer strings (240 kW and 500 MW) did not match. Frozen prompts remain unchanged.',
        tool_invocation_conversations={arm:dict(values) for arm,values in tools_by_arm.items()},
        error_event_counts=dict(Counter(event['kind']+':'+event['error_type'] for event in errors)))
    atomic_json(Path(root)/'analysis/coverage_audit.json',audit)
    write_csv(Path(root)/'analysis/error_events.csv',errors)
    return audit


def analyze(root):
    import matplotlib;matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    root=Path(root);out=root/'analysis';out.mkdir(exist_ok=True)
    suite,rows=load_rows(root);groups={arm:summary_group([r for r in rows if r['arm']==arm]) for arm in ARMS}
    strata=[]
    for field in ['category','stratum','research_task','network_kind','scene','phase_mode','expected_outcome','revision_conversation']:
        for value in sorted({str(r.get(field)) for r in rows}):
            for arm in ARMS:
                selected=[r for r in rows if str(r.get(field))==value and r['arm']==arm]
                if selected:strata.append(dict(field=field,value=value,arm=arm,**summary_group(selected)))
    comparisons=[]
    for key in ['case_id','request_id','network_id']:
        for metric,only_positive in [('generation_success',True),('audited_generation_success',True),('semantic_artifact_fulfillment',True),('semantic_fulfilled_and_completed',True),('appropriate_outcome',False),('completed_appropriate_outcome',False),('audited_complete_appropriate_outcome',False),('llm_calls',False),('seconds',False)]:
            comparisons.append(paired(rows,key,metric,only_positive))
    chain_metrics=['structured_requirements_complete','compatible_entrypoint_invoked','saved_artifact','accepted_runnable_artifact','all_revision_steps_preserved','first_revision_passed','second_revision_passed','request_fulfilled','conversation_completed','fulfilled_and_completed']
    chains=[]
    for arm in ARMS:
        for metric in chain_metrics:
            selected=[r for r in rows if r['arm']==arm and r['primary_positive'] and r[f'chain_{metric}'] is not None]
            chains.append(dict(arm=arm,metric=metric,passed=sum(r[f'chain_{metric}'] for r in selected),n=len(selected)))
    request_groups=defaultdict(list)
    for case in suite['cases']:request_groups[request_key(case)].append(case['id'])
    summary=dict(study=suite['study'],planned=288,observed=len(rows),model=suite['model'],groups=groups,strata=strata,paired=comparisons,
        authored_case_ids=len(suite['cases']),unique_requests=len(request_groups),duplicate_requests={k:v for k,v in request_groups.items() if len(v)>1},
        analysis_source_hash=file_digest(Path(__file__)),frozen_suite_hash=digest(suite),
        token_accounting='One llm_output.token_usage per completed provider call; response_metadata fallback; no double counting.',
        analysis_scope='Postprocessing of immutable raw events and grades; no live model or production method changes.',
        cluster_scope='Case means average three model repetitions; request clusters collapse identical NFKC/whitespace-normalized complete message sequences across authored case IDs; network clusters group identical public baseline specifications and seed entrypoints. Descriptive within-suite intervals do not establish new independent physical networks.')
    summary['chain_diagnostics']=chains
    summary['coverage']=coverage_audit(root,suite,rows)
    atomic_json(out/'summary.json',summary);write_csv(out/'metrics.csv',rows);write_csv(out/'strata.csv',strata);write_csv(out/'chain.csv',chains)
    write_csv(out/'failures.csv',[r for r in rows if not r['appropriate_outcome']])
    write_csv(out/'operational_failures.csv',[r for r in rows if r['status']!='completed' or r['provider_error_events'] or r['tool_error_types']])
    write_csv(out/'paired.csv',[{k:v for k,v in p.items() if k!='details'} for p in comparisons])
    categories=[c for c in ['ordinary','prohibitions','units_counts','revisions','contradictions','unsupported','permissions'] if any(r['category']==c for r in rows)]
    fig,axes=plt.subplots(1,2,figsize=(12,4.6));colors=['#71869f','#247b72'];width=.36;x=np.arange(len(categories))
    for j,arm in enumerate(ARMS):
        rates=[]
        for category in categories:
            selected=[r for r in rows if r['arm']==arm and r['category']==category];rates.append(sum(r['appropriate_outcome'] for r in selected)/max(1,len(selected)))
        axes[0].bar(x+(j-.5)*width,rates,width,color=colors[j],label=arm.replace('_',' '))
    axes[0].set_xticks(x,[c.replace('_','\n') for c in categories],rotation=25,ha='right');axes[0].set_ylim(0,1);axes[0].set_ylabel('Appropriate outcome fraction');axes[0].legend(frameon=False)
    positive_strata=['ordinary_model','interface_baseline','nontrivial_redesign','multi_turn_revision','permission_amendment'];x=np.arange(len(positive_strata))
    for j,arm in enumerate(ARMS):
        rates=[]
        for stratum in positive_strata:
            selected=[r for r in rows if r['arm']==arm and r['stratum']==stratum and r['primary_positive']];rates.append(sum(r['generation_success'] for r in selected)/max(1,len(selected)))
        axes[1].bar(x+(j-.5)*width,rates,width,color=colors[j])
    axes[1].set_xticks(x,[s.replace('_','\n') for s in positive_strata],rotation=25,ha='right');axes[1].set_ylim(0,1);axes[1].set_ylabel('Known-feasible saved-model success')
    fig.suptitle('M97 v2: frozen evaluator grades (before semantic-equivalence correction)');fig.tight_layout()
    for extension in ('png','pdf'):fig.savefig(out/f'outcomes.{extension}',dpi=180)
    plt.close(fig)
    fig,axes=plt.subplots(1,2,figsize=(10,4.5));x=np.arange(len(positive_strata))
    for index,(metric,label) in enumerate([('semantic_artifact_fulfillment','User-goal artifact fulfillment'),('semantic_fulfilled_and_completed','Fulfilled and conversation completed')]):
        for j,arm in enumerate(ARMS):
            selected_groups=[[r for r in rows if r['arm']==arm and r['stratum']==stratum and r['primary_positive']] for stratum in positive_strata]
            axes[index].bar(x+(j-.5)*width,[sum(r[metric] for r in group)/max(1,len(group)) for group in selected_groups],width,color=colors[j],label=arm.replace('_',' '))
        axes[index].set_xticks(x,[name.replace('_','\n') for name in positive_strata],rotation=25,ha='right');axes[index].set_ylim(0,1);axes[index].set_title(label);axes[index].set_ylabel('Fraction of known-feasible requests')
    axes[0].legend(frameon=False);fig.suptitle('Post-run semantic-equivalence audit; frozen grades retained');fig.tight_layout()
    for extension in ('png','pdf'):fig.savefig(out/f'semantic_fulfillment.{extension}',dpi=180)
    plt.close(fig)
    failure_names=sorted({r['failure_class'] for r in rows if not r['appropriate_outcome']});fig,ax=plt.subplots(figsize=(10,4.5));x=np.arange(len(failure_names))
    for j,arm in enumerate(ARMS):ax.bar(x+(j-.5)*width,[sum(r['arm']==arm and r['failure_class']==f for r in rows) for f in failure_names],width,label=arm.replace('_',' '),color=colors[j])
    ax.set_xticks(x,[v.replace('_','\n') for v in failure_names],fontsize=8);ax.set_ylabel('Failed conversations');ax.legend(frameon=False);fig.tight_layout()
    for extension in ('png','pdf'):fig.savefig(out/f'failures.{extension}',dpi=180)
    plt.close(fig)
    shown=['structured_requirements_complete','compatible_entrypoint_invoked','saved_artifact','accepted_runnable_artifact','request_fulfilled','fulfilled_and_completed']
    fig,ax=plt.subplots(figsize=(10,4));x=np.arange(len(shown))
    labels=['Frozen structured\nrequirements','Dedicated entrypoint\nobserved','Artifact\nsaved','Runnable accepted\nartifact','Frozen requested\noutcomes','Frozen fulfilled\nand completed']
    for j,arm in enumerate(ARMS):
        values=[next(c for c in chains if c['arm']==arm and c['metric']==metric) for metric in shown]
        ax.bar(x+(j-.5)*width,[v['passed']/max(1,v['n']) for v in values],width,color=colors[j],label=arm.replace('_',' '))
    ax.set_xticks(x,labels);ax.set_ylim(0,1);ax.set_ylabel('Fraction of known-feasible conversations');ax.legend(frameon=False)
    ax.set_title('Independent chain diagnostics (overlapping, not a sequential funnel)');fig.tight_layout()
    for extension in ('png','pdf'):fig.savefig(out/f'chain.{extension}',dpi=180)
    plt.close(fig)
    lines=['# M97 v2: real outer-agent intent and revision evaluation','',f"Completed records: {len(rows)}/288. Actual configured model: `{suite['model']}`. Version 1 is excluded because its initial one-call baseline was consumed by prerequisites; the corrected paired run repeats both arms on all 48 authored conversations.",'',
        'Both methods use production `agent_session`, identical tools and model settings, 8 outer model decisions and 12 tool calls per user turn, a 24-call total model ceiling, 40-second API timeout, no API retries, and 180-second conversation timeout. The one-proposal method can inspect all prerequisites, then execute one substantive proposal per user turn, including exact plan-to-execution continuation and all downstream internal LLM calls. The full workflow may propose again after feedback. Each conversation has fresh memory and a persistent private conversation workspace.','',
        '| Method | Known-feasible model success | Correct negative decision | Complete appropriate outcome | All / internal LLM calls | Tokens | Mean seconds | API error events / conversation timeouts |',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for arm,g in groups.items():lines.append(f"| {arm} | {g['generation_success']}/{g['primary_n']} | {g['correct_negative']}/{g['negative_n']} | {g['completed_appropriate']}/{g['n']} | {g['llm_calls']} / {g['internal_calls']} | {g['tokens']:,} | {g['mean_seconds']:.1f} | {g['service_error_events']} / {g['timeouts']} |")
    lines+=['','A complete post-run semantic-equivalence audit corrects a restrictive evaluator adapter: task/tool names and storage layout are not user requirements. Generic response outputs are accepted when existing hash-verified solver records establish the requested response kind, unchanged original target interval, actual full/half response values, physical acceptance, exact finite Q/PV/transfer experiment, requested model properties and authorized selected changes. No new API or solver calls are made. A recorded positive Q-probe at exactly the requested support is a genuine finite Q experiment; a P derivative without PV-off/on states is not a PV experiment. Inactive repair-policy metadata in the direct response path is not treated as an executed repair. This is a post-run evaluation correction, not an improved agent method.','',
        '| Method | Frozen model fulfillment | Semantic-evidence model fulfillment | Semantic fulfillment and completed conversation |','|---|---:|---:|---:|']
    for arm,g in groups.items():lines.append(f"| {arm} | {g['generation_success']}/{g['primary_n']} | {g['semantic_artifact_fulfillment']}/{g['primary_n']} | {g['semantic_fulfilled_and_completed']}/{g['primary_n']} |")
    lines+=['','The semantic audit applies to all 96 research-positive executions. The original frozen task-label predicate reported 0/48 per arm; existing physical/task evidence instead fulfills 9/48 one-proposal and 14/48 full-outer requests. All newly accepted permission amendments preserve the first-turn original model hash. The remaining research failures must be interpreted from actual missing finite experiments, mismatched source quantities/targets, or permissions—not merely missing dedicated task labels. Complete per-artifact checks and solver-state paths are in semantic_equivalence.json.']
    lines+=['',f"The 48 preregistered case IDs contain {len(request_groups)} unique complete English message sequences after Unicode NFKC and whitespace normalization. Duplicate memberships are retained in summary.json and all planned runs stay in the denominator. A unique-request-cluster paired analysis prevents duplicate IDs from being treated as distinct language requests. This study covers English rural/urban balanced distribution and meshed transmission prompts; it does not establish multilingual, hierarchical-network or unbalanced-phase language performance."]
    lines+=['','The frozen units/counts category contains an explicit 31–39-bus range, a 20 kvar finite-support request (checked against 0.02 Mvar internally), standard kW/MW/kV quantities and a 1 MW transfer. The intended 0.24 MW distribution-demand and 0.5 GW transmission-demand variants are absent: the construction template rendered 240.0 kW/500.0 MW, so replacements matching 240 kW/500 MW did not apply. These two alternate-unit conversions are not covered. Actual source literals and production-tool invocation coverage are in coverage_audit.json.']
    lines+=['','Saved-model success can survive a later conversation timeout if verified artifacts already exist; complete appropriate outcomes additionally require conversation status completed. Generation success requires accepted runnable artifacts and independently checked requested model fields, task labels/targets and revision invariants. Contradictions, unsupported requests and truthful unmet outcomes are separate decisions, never generated-model successes. Interface response bands [0.98,1.02] are separated from independently certified nontrivial [1.10,1.30] redesigns. Thirty positive conversations have valid original certificates. One initial witness omitted its explicit displacement bound; a separately recorded, fully legal witness was independently certified before that affected case began, with unchanged prompts and response targets. All 31 are certified after that amendment; the same physical seed is held across model repetitions.','',
        '| Category | One-proposal appropriate | Full-workflow appropriate |','|---|---:|---:|']
    for category in categories:
        vals=[]
        for arm in ARMS:
            selected=[r for r in rows if r['arm']==arm and r['category']==category];vals.append(f"{sum(r['appropriate_outcome'] for r in selected)}/{len(selected)}")
        lines.append(f'| {category} | {vals[0]} | {vals[1]} |')
    lines+=['','The agentic-system claim is assessed by fulfilling original natural-language requests through interpretation, tool selection, execution, local revision and usable delivery. The one-proposal comparison diagnoses the effect of additional outer feedback opportunities under the same tool system; it does not test whether a numerical search algorithm beats other optimizers. The following independently observed stages overlap and must not be read as a sequential funnel. The legacy compatible-entrypoint column is only a dedicated-tool invocation proxy (`design_research_grid` or `revise_existing_case`); suitable generic response tools can also fulfill user goals. It is not an acceptance requirement, and invocation alone does not establish correct arguments or execution. Accepted-artifact existence can concern an initial model while later requested revisions remain missing.','',
        '| Positive conversation diagnostic | One proposal | Full outer |','|---|---:|---:|']
    for metric in chain_metrics:
        vals=[next(c for c in chains if c['arm']==arm and c['metric']==metric) for arm in ARMS]
        lines.append(f"| {metric.replace('_',' ')} | {vals[0]['passed']}/{vals[0]['n']} | {vals[1]['passed']}/{vals[1]['n']} |")
    lines+=['','| Positive task/stratum | One-proposal frozen / audited success | Full-outer frozen / audited success |','|---|---:|---:|']
    for field in ['stratum','research_task','scene']:
        for value in sorted({r[field] for r in rows if r['primary_positive']}):
            vals=[]
            for arm in ARMS:
                selected=[r for r in rows if r['arm']==arm and r['primary_positive'] and r[field]==value]
                vals.append(f"{sum(r['generation_success'] for r in selected)} / {sum(r['audited_generation_success'] for r in selected)} of {len(selected)}")
            lines.append(f'| {field}: {value} | {vals[0]} | {vals[1]} |')
    lines+=['','Paired statistics average the three repetitions within each authored case. A second analysis groups identical baseline specifications and seed entrypoints; this prevents repeated language variants of one baseline from being counted as independent physical cases. Intervals are descriptive within-suite 10,000-resample percentile intervals; sign-flip tests are exploratory. They do not establish general reliability outside these authored prompts.','']
    lines+=['Network-cluster statistics include only cases with an independently specified baseline. Contradiction and unsupported prompts without a baseline are excluded from that scope; unique-request clustering retains the complete language suite.','']
    for p in comparisons:
        if p.get('metric') in {'generation_success','appropriate_outcome'} and p.get('clusters'):
            lines.append(f"- {p['cluster_unit']}, {p['metric']}: full minus one-proposal = {p['difference']:+.3f}, interval [{p['bootstrap_95'][0]:+.3f}, {p['bootstrap_95'][1]:+.3f}], {p['clusters']} paired clusters; wins/ties/losses {p['wins']}/{p['ties']}/{p['losses']}.")
    lines+=['','The failure_class column describes frozen-predicate failures; semantic_outcome_class separately recognizes equivalent artifacts and their actual remaining evidence failures. Operational fields distinguish tool and physical failures, conversation timeouts, provider request timeouts, connection errors and other provider errors. The full list, including successful conversations that incurred a recoverable service error, is in `metrics.csv`; unsuccessful conversations are also listed in `failures.csv`. Provider token totals are decoded once per completed call from the raw provider usage field; no monetary price is assumed.','',
        'The frozen attempted-permission detector conservatively counts equipment-tier calls even when their conductor cap is zero or missing. The independent audit separately identifies rejected zero-cap tier calls and positive or unbounded forbidden-equipment requests; none is automatically described as an executed conductor edit. Permission evidence distinguishes language-level attempts, numerical preview attempts, selected-path commitments and selected delivered model differences. Conversation-level counts avoid counting the same plan repeatedly as independent incidents. The frozen negative decision metric is an automatic keyword/tool-status proxy, not expert labeling. A separate independent post-run audit checks the expected contradiction/unsupported topic in last-turn reasons, every intermediate revision and turn-specific authorization. Frozen and stricter audited grades are retained side by side for all conversations; the new audit does not silently replace the preregistered primary predicate. Raw provider messages, actual tool arguments/results, frozen inputs, certificates, exported artifacts, independent grades and the rejected v1 development run are retained.']
    certificate_path=out/'certificate_audit.json'
    if certificate_path.exists():
        cert=json.loads(certificate_path.read_text())
        lines += ['',f"The uniform certificate audit covers {cert['positive_cases']} positive cases: {cert['original_witness_valid']} original witnesses meet every audited source bound; {cert['amended_certified']} meet them after the one explicit amendment. Original and amended eligibility remain separate columns in metrics.csv. The corrected permission witness scales by 1.12, displaces nodes by at most 0.19924687896 km under the 0.2 km cap, changes no conductors, and achieves full/half ratios 1.1086659311/1.1086658573 within the unchanged [1.10,1.30] target."]
    lines += ['', '| Method | Frozen saved-model success | Independently audited saved-model success | Frozen complete appropriate | Audited complete appropriate |', '|---|---:|---:|---:|---:|']
    for arm,g in groups.items():lines.append(f"| {arm} | {g['generation_success']}/{g['primary_n']} | {g['audited_generation_success']}/{g['amended_certificate_primary_n']} | {g['completed_appropriate']}/{g['n']} | {g['audited_complete_appropriate']}/{g['n']} |")
    lines+=['','The stricter audited appropriate-outcome column only removes frozen successes that fail additional checks. A separate normalized negative-decision-plus-reason proxy can also recognize evidence missed by the frozen literal cues; it is not substituted into the frozen or stricter primary columns. For example, unsupported_01 one_proposal r1 says "not a supported capability", and unsupported_04 full r0 says "cannot deliver" with Markdown around "not". Both are explicit refusals that the frozen cue list misses. Negative proxy failures therefore are not automatically established semantic errors.','',
        '| Method | Frozen negative-decision proxy | Independent normalized expected-decision + reason proxy | Unexpected artifact on clarification/unsupported request |','|---|---:|---:|---:|']
    for arm,g in groups.items():lines.append(f"| {arm} | {g['correct_negative']}/{g['negative_n']} | {g['negative_decision_reason_proxy']}/{g['negative_n']} | {g['unexpected_negative_artifact']} |")
    lines+=['','| Method | Attempted (including conservative zero-cap tiers) | Positive/unbounded forbidden attempts | Saved selected/committed violations | Exposed in actual tool result | Explicit final-assistant delivery |','|---|---:|---:|---:|---:|---:|']
    for arm,g in groups.items():lines.append(f"| {arm} | {g['unauthorized_attempted_conversations']} | {g['effective_forbidden_attempt_conversations']} | {g['unauthorized_committed_conversations']} | {g['unauthorized_tool_result_exposed_conversations']} | {g['unauthorized_delivered_conversations']} |")
    lines+=['','These are conversation counts, not action totals. The frozen delivered field meant saved selected-model differences; its original values are retained separately. The post-run audit distinguishes an internal saved selected artifact from a path exposed in a tool result and an explicit final-assistant artifact-path reference. Absence of an explicit final reference does not erase unauthorized execution or commitment. Before/after SHA-256 hashes, original user turn, actual tool proposals and numerical changes are preserved per artifact in analysis/authorization. Successful baseline tool artifacts do not require extra final narration for generation scoring.']
    lines+=['','Original-certificate-only sensitivity: '+ '; '.join(f"{arm} {g['original_certificate_success']}/{g['original_certificate_primary_n']}" for arm,g in groups.items())+'.','',
        '| Operational outcome | One proposal | Full outer |','|---|---:|---:|']
    for status in sorted({r['status'] for r in rows}):lines.append(f"| {status} | {groups[ARMS[0]]['status_counts'].get(status,0)} | {groups[ARMS[1]]['status_counts'].get(status,0)} |")
    lines+=['','`operational_failures.csv` includes all timeouts, failed turns and provider/tool error events, including cases with accepted artifacts. Frozen outcome failures alone are in `failures.csv`. Diagnosis flags can overlap; neither counts nor their ordering establish causal attribution without the linked raw evidence. Concrete reproducible production defects are documented separately in `docs/research/m97-outer-agent-defects.md`.']
    (out/'report.md').write_text('\n'.join(lines)+'\n')
    representative_trajectories(root,suite,rows)
    return summary


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('root',type=Path);args=parser.parse_args();result=analyze(args.root)
    print(json.dumps(dict(observed=result['observed'],groups=result['groups']),indent=2))
