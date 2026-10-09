"""Multi-voltage condition evaluation and guarded local inverse-design steps."""
import fcntl
import hashlib
import html
import json
import math
from pathlib import Path
import re
from typing import Literal
import zipfile

from pydantic import Field,model_validator

from .artifacts import atomic_json,digest,file_digest
from .schemas import StrictModel
from .hierarchy import HierarchicalSpec,HierarchicalFeeder,generate_hierarchy,export_hierarchy,evaluate_hierarchy
from .hierarchy_workflow import solve_isolated,visualization
from .hierarchy_actions import apply_action,propose_actions,pv_weights,contract_preserved

Metric=Literal['mv_min_voltage_pu','mv_max_voltage_pu','lv_min_voltage_pu','lv_max_voltage_pu',
               'max_line_loading_ratio','max_transformer_loading_ratio','max_vuf_percent']


class HierarchyGoal(StrictModel):
    metric: Metric
    operator: Literal['ge','le']
    threshold: float=Field(gt=0,allow_inf_nan=False)


class HierarchyCondition(StrictModel):
    name: str=Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$')
    pv_ratio: float=Field(ge=0,le=3,description='PV capacity divided by unscaled baseline load.')
    load_scale: float=Field(default=1,gt=0,le=5)
    goals: list[HierarchyGoal]=Field(min_length=1,max_length=12)


class HierarchySearch(StrictModel):
    allowed_actions: list[Literal['upgrade_transformer','upgrade_mv_line','upgrade_lv_line','relocate_pv']]=Field(default_factory=list,max_length=4)
    proposal_policy: Literal['violation_order','diverse']='diverse'
    max_rounds: int=Field(default=6,ge=0,le=12)
    candidates_per_round: int=Field(default=3,ge=1,le=6)
    pv_transfer_fraction: float=Field(default=.25,gt=0,le=.5)

    @model_validator(mode='after')
    def unique(self):
        if len(set(self.allowed_actions))!=len(self.allowed_actions):raise ValueError('Duplicate local actions')
        return self


class HierarchyInversePlan(StrictModel):
    research_question: str=Field(min_length=1,max_length=4000)
    base_spec: HierarchicalSpec
    conditions: list[HierarchyCondition]=Field(min_length=2,max_length=12)
    verification_conditions: list[HierarchyCondition]=Field(default_factory=list,max_length=12,description='Post-search probes only; never used for proposal selection or candidate acceptance.')
    search: HierarchySearch=Field(default_factory=HierarchySearch)

    @model_validator(mode='after')
    def compatible(self):
        if self.base_spec.count!=1:raise ValueError('Hierarchical inverse design requires count=1')
        all_conditions=self.conditions+self.verification_conditions
        if len({c.name for c in all_conditions})!=len(all_conditions):raise ValueError('Duplicate condition names')
        for c in all_conditions:condition_spec(self.base_spec,c)
        return self


def condition_spec(base,condition):
    payload=base.model_dump()
    payload.update(total_kw=base.total_kw*condition.load_scale,pv_ratio=condition.pv_ratio/condition.load_scale)
    return HierarchicalSpec.model_validate(payload)


def hierarchy_fingerprint(feeder):
    return digest({'buses':[b.model_dump() for b in feeder.buses],
        'lines':[e.model_dump() for e in feeder.lines],'tie_lines':[e.model_dump() for e in feeder.tie_lines],
        'transformers':[t.model_dump() for t in feeder.transformers],'equipment_catalog':feeder.equipment_catalog,
        'connections':[(l.id,l.bus,l.contract_kva,l.phases,l.category) for l in feeder.loads],
        'voltage_kv':feeder.voltage_kv,'frequency_hz':feeder.frequency_hz,'source_bus':feeder.source_bus,'phase_mode':feeder.phase_mode})


def hierarchy_variant(base,condition):
    variant=base.model_copy(deep=True);weights=pv_weights(base)
    variant.total_kw=base.total_kw*condition.load_scale
    for load,original in zip(variant.loads,base.loads):
        load.kw=original.kw*condition.load_scale;load.kvar=original.kvar*condition.load_scale
        load.pv_kw=base.total_kw*condition.pv_ratio*weights[load.id]
        for phase,original_phase in zip(load.phase_powers,original.phase_powers):
            phase.kw=original_phase.kw*condition.load_scale;phase.kvar=original_phase.kvar*condition.load_scale
            phase.pv_kw=load.pv_kw  # Hierarchical customers are explicitly single phase.
    return variant


def _finite(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def metric_observations(feeder,result):
    observed={key:[] for key in ('mv_min_voltage_pu','mv_max_voltage_pu','lv_min_voltage_pu','lv_max_voltage_pu',
                               'max_line_loading_ratio','max_transformer_loading_ratio','max_vuf_percent')}
    def add(key,value,witness,phase=None):
        observed[key].append({'value':value if _finite(value) else None,'witness':witness,'phase':phase})
    for bus in feeder.buses:
        prefix='mv' if bus.voltage_kv>1 else 'lv'
        for phase in bus.phases:
            value=result.get('bus_phase_voltage_pu',{}).get(bus.id,{}).get(str(phase))
            for suffix in ('min','max'):add(prefix+'_'+suffix+'_voltage_pu',value,bus.id,phase)
        if len(bus.phases)==3:add('max_vuf_percent',result.get('bus_vuf_percent',{}).get(bus.id),bus.id)
    for line in feeder.lines:
        current=result.get('line_current_a',{}).get(line.id)
        add('max_line_loading_ratio',current/feeder.equipment_catalog[line.conductor]['normamps'] if _finite(current) else None,line.id)
    for tx in feeder.transformers:
        measured=result.get('transformer_metrics',{}).get(tx.id,{})
        values=[measured.get('max_phase_loading_ratio')]
        terminals=measured.get('terminal_kva',[])
        values.extend(v/tx.kva if _finite(v) else None for v in terminals)
        add('max_transformer_loading_ratio',max(values) if len(terminals)==2 and all(_finite(v) for v in values) else None,tx.id)
    return observed


def _condition_assessment(feeder,variant,spec,condition,result):
    checked=evaluate_hierarchy(variant,spec,result)
    operational={'mv_voltage','lv_voltage','line_capacity','transformer_capacity','unbalance'}
    observations=metric_observations(variant,result)
    complete=all(items and all(v['value'] is not None and v['value']>=0 for v in items) for items in observations.values())
    valid=complete and all(value for key,value in checked['checks'].items() if key not in operational)
    paired=hierarchy_fingerprint(feeder)==hierarchy_fingerprint(variant) and variant.loads==hierarchy_variant(feeder,condition).loads
    constraints=[];metrics={}
    for key,items in observations.items():
        metrics[key]=(min if '_min_' in key else max)(v['value'] for v in items) if all(v['value'] is not None for v in items) and items else None
    def add(metric,op,threshold,items,label):
        for index,item in enumerate(items):
            value=item['value'];signed=(threshold-value if op=='ge' else value-threshold) if value is not None else None
            constraints.append(dict(key=f'{condition.name}:{label}:{index}',condition=condition.name,metric=metric,
                operator=op,threshold=threshold,actual=value,witness=item['witness'],phase=item['phase'],
                deficit=max(0,signed)/threshold if signed is not None else 1e6,met=signed is not None and signed<=0))
    # Existing per-tier acceptance bounds are hard requirements in every case.
    protected=[('mv_min_voltage_pu','ge',spec.mv_voltage_min_pu),('mv_max_voltage_pu','le',spec.mv_voltage_max_pu),
        ('lv_min_voltage_pu','ge',spec.lv_voltage_min_pu),('lv_max_voltage_pu','le',spec.lv_voltage_max_pu),
        ('max_line_loading_ratio','le',1.),('max_transformer_loading_ratio','le',1.),('max_vuf_percent','le',spec.max_vuf_percent)]
    for metric,op,threshold in protected:add(metric,op,threshold,observations[metric],'protected:'+metric)
    for index,goal in enumerate(condition.goals):
        items=observations[goal.metric]
        universal=(('_min_' in goal.metric and goal.operator=='ge') or ('_min_' not in goal.metric and goal.operator=='le'))
        if not universal:
            select=min if '_min_' in goal.metric else max
            items=[select(items,key=lambda v:v['value'])] if all(v['value'] is not None for v in items) else [dict(value=None,witness='missing',phase=None)]
        add(goal.metric,goal.operator,goal.threshold,items,f'goal:{index}')
    return dict(name=condition.name,valid=bool(valid and paired),paired_verified=paired,accepted=checked['accepted'],
        target_met=bool(valid and paired and all(c['met'] for c in constraints)),metrics=metrics,checks=checked['checks'],constraints=constraints)


def _aggregate(rows):
    constraints=[c for row in rows for c in row.get('constraints',[])]
    valid=bool(rows) and all(r['valid'] for r in rows)
    return dict(valid=valid,target_met=valid and all(r['target_met'] for r in rows),conditions=rows,
        constraints=constraints,deficits={c['key']:c['deficit'] for c in constraints},
        score=sum(c['deficit'] for c in constraints) if valid else 1e9)


def evaluate_candidate(plan,feeder,root,candidate_id,verification=False):
    folder=root/('verification' if verification else 'candidates')/candidate_id;folder.mkdir(parents=True,exist_ok=True)
    atomic_json(folder/'feeder.json',feeder.model_dump())
    rows=[]
    for condition in (plan.verification_conditions if verification else plan.conditions):
        path=folder/condition.name;path.mkdir(exist_ok=True)
        variant=hierarchy_variant(feeder,condition);spec=condition_spec(plan.base_spec,condition)
        atomic_json(path/'feeder.json',variant.model_dump())
        try:
            master=export_hierarchy(variant,path/'opendss');result=solve_isolated(master)
            row=_condition_assessment(feeder,variant,spec,condition,result)
            atomic_json(path/'simulation.json',result);atomic_json(path/'validation.json',row)
            (path/'visualization.html').write_text(visualization(variant,result,{'accepted':row['accepted']}))
        except Exception as exc:
            row=dict(name=condition.name,valid=False,target_met=False,paired_verified=False,
                     error=f'{type(exc).__name__}: {exc}',constraints=[])
            atomic_json(path/'error.json',row)
        rows.append({**row,'relative_path':str(path.relative_to(root))})
    output={'candidate_id':candidate_id,'network_hash':hierarchy_fingerprint(feeder),**_aggregate(rows)}
    atomic_json(folder/'assessment.json',output)
    return output


def acceptable_step(current,trial):
    return (trial['valid'] and current['valid'] and set(trial['deficits'])==set(current['deficits'])
        and all(trial['deficits'][k]<=v+1e-10 for k,v in current['deficits'].items())
        and trial['score']<current['score']-1e-9)


def verify_action_replay(root,plan,trace,winner_id):
    root=Path(root)
    current=HierarchicalFeeder.model_validate_json((root/'baseline.json').read_text())
    parent='candidate_0000'
    for entry in trace:
        if not entry['selected']:continue
        if entry['parent']!=parent or not entry['admissible']:
            raise ValueError('Selected action chain has an invalid parent or acceptance')
        current=apply_action(current,entry['action'],plan.search.allowed_actions)
        candidate_id=entry['candidate']
        if not re.fullmatch(r'candidate_[0-9]{4,}',candidate_id):raise ValueError('Invalid candidate ID')
        saved=HierarchicalFeeder.model_validate_json((root/'candidates'/candidate_id/'feeder.json').read_text())
        if saved!=current:raise ValueError('Saved candidate differs from authorized action replay')
        before=json.loads((root/'candidates'/parent/'assessment.json').read_text())
        after=json.loads((root/'candidates'/candidate_id/'assessment.json').read_text())
        if not acceptable_step(before,after):raise ValueError('Selected action violates cross-condition acceptance')
        parent=candidate_id
    if parent!=winner_id:raise ValueError('Winner is not the replayed authorized action chain')
    saved=HierarchicalFeeder.model_validate_json((root/'candidates'/parent/'feeder.json').read_text())
    if saved!=current:raise ValueError('Saved winner differs from authorized action replay')
    return True


def verify_saved_conditions(root,plan,feeder,assessment,conditions,namespace):
    folder=Path(root)/namespace/assessment['candidate_id'];rows=[]
    if len(assessment['conditions'])!=len(conditions):raise ValueError('Saved condition count differs')
    for condition,saved in zip(conditions,assessment['conditions']):
        if saved['name']!=condition.name:raise ValueError('Saved condition order differs')
        if saved.get('error'):rows.append(saved);continue
        path=folder/condition.name
        variant=HierarchicalFeeder.model_validate_json((path/'feeder.json').read_text())
        checked=_condition_assessment(feeder,variant,condition_spec(plan.base_spec,condition),condition,json.loads((path/'simulation.json').read_text()))
        if not variant.design_evidence.get('topology_policy_version') and 'mv_topology' not in variant.design_evidence.get('spec',{}):
            # Pre-extension assessments lack the two new checks. Recompute and
            # require them before projecting to the authenticated legacy schema.
            for key in ('mv_topology','lv_topology'):
                if checked['checks'].get(key) is not True:raise ValueError('Legacy inverse topology differs from its original contract')
                if key not in saved['checks']:checked['checks'].pop(key)
            # An older electrical check also predates some archives. Its effect
            # on accepted/valid is retained, so any changed verdict still fails.
            if 'delivered_element_power' not in saved['checks']:checked['checks'].pop('delivered_element_power',None)
        if ('customer_connection' not in variant.design_evidence.get('spec',{})
                and 'customer_connection' not in saved['checks']):
            # Authenticated pre-M77 records used dedicated terminal services.
            # Require that actual contract before comparing their older schema.
            if plan.base_spec.customer_connection!='service_star' or checked['checks'].get('customer_connection') is not True:
                raise ValueError('Legacy inverse customer connection differs from its original contract')
            checked['checks'].pop('customer_connection')
        if checked!={k:v for k,v in saved.items() if k!='relative_path'}:raise ValueError('Saved inverse assessment differs from current verification')
        rows.append(checked)
    if _aggregate(rows)['target_met']!=assessment['target_met']:raise ValueError('Saved inverse target differs from current verification')
    return assessment['target_met']


def read_hierarchy_inverse(root):
    root=Path(root).resolve();result=json.loads((root/'result.json').read_text())
    if digest({k:v for k,v in result.items() if k!='result_hash'})!=result.get('result_hash'):raise ValueError('Inverse result checksum changed')
    for name,sha in result['artifacts'].items():
        path=(root/name).resolve()
        if not path.is_relative_to(root) or not path.is_file() or file_digest(path)!=sha:raise ValueError('Inverse artifact missing or changed: '+name)
    from .hierarchy import historical_hierarchy_spec
    payload=json.loads((root/'plan.json').read_text())
    payload['base_spec']=historical_hierarchy_spec(payload['base_spec']).model_dump()
    plan=HierarchyInversePlan.model_validate(payload)
    winner=result['winner'];folder=root/'candidates'/winner['candidate_id']
    feeder=HierarchicalFeeder.model_validate_json((folder/'feeder.json').read_text())
    baseline=HierarchicalFeeder.model_validate_json((root/'baseline.json').read_text())
    if not contract_preserved(baseline,feeder):raise ValueError('Saved candidate violates protected design fields')
    verify_action_replay(root,plan,result['trace'],winner['candidate_id'])
    if verify_saved_conditions(root,plan,feeder,winner,plan.conditions,'candidates')!=result['target_met']:
        raise ValueError('Saved target status differs')
    if plan.verification_conditions:
        if verify_saved_conditions(root,plan,feeder,result['verification'],plan.verification_conditions,'verification')!=result['verification_passed']:
            raise ValueError('Saved post-search verification differs')
    return result


def run_hierarchy_inverse(plan,workspace,design_id):
    from .workflow import _runtime_fingerprint
    plan=HierarchyInversePlan.model_validate(plan.model_dump() if isinstance(plan,HierarchyInversePlan) else plan)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',design_id):raise ValueError('Invalid design ID')
    root=Path(workspace).resolve()/'hierarchical_inverse_designs'/design_id;root.mkdir(parents=True,exist_ok=True)
    manifest={'plan':plan.model_dump(),'runtime':_runtime_fingerprint()}
    with (root/'.lock').open('w') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (root/'manifest.json').exists():
            if json.loads((root/'manifest.json').read_text())!=manifest:raise ValueError('Hierarchy inverse configuration/code changed; use a new ID')
            if (root/'result.json').exists():return read_hierarchy_inverse(root)
        atomic_json(root/'manifest.json',manifest);atomic_json(root/'plan.json',plan.model_dump())
        seed=int.from_bytes(hashlib.sha256(f'{plan.base_spec.seed}:0'.encode()).digest()[:4],'big')
        current=generate_hierarchy(plan.base_spec,seed);atomic_json(root/'baseline.json',current.model_dump())
        baseline=evaluate_candidate(plan,current,root,'candidate_0000');assessment=baseline
        trace=[];accepted_steps=0;count=1;stop_reason='round_limit'
        # Physical equality across conditions deliberately ignores PV allocation.
        # Search identity must include it, without changing historical replay hashes.
        def search_key(model):return digest(dict(network=hierarchy_fingerprint(model),pv_weights=pv_weights(model)))
        seen={search_key(current)}
        for round_index in range(plan.search.max_rounds):
            if assessment['target_met']:stop_reason='targets_satisfied';break
            if not assessment['valid']:stop_reason='invalid_initial_or_current_model';break
            def eligible(action):
                candidate=apply_action(current,action,plan.search.allowed_actions)
                return contract_preserved(current,candidate) and search_key(candidate) not in seen
            actions=propose_actions(current,assessment,plan.search,eligible=eligible)
            if not actions:
                stop_reason='no_untried_candidates';break
            trials=[]
            for action in actions:
                candidate=apply_action(current,action,plan.search.allowed_actions)
                identity=search_key(candidate)
                if identity in seen:continue
                seen.add(identity)
                row=evaluate_candidate(plan,candidate,root,f'candidate_{count:04d}');count+=1
                allowed=acceptable_step(assessment,row)
                entry=dict(round=round_index,parent=assessment['candidate_id'],candidate=row['candidate_id'],action=action,
                    admissible=allowed,selected=False,before_score=assessment['score'],after_score=row['score'],
                    regressions=[dict(constraint=k,before=v,after=row['deficits'].get(k)) for k,v in assessment['deficits'].items()
                                 if row['deficits'].get(k,1e6)>v+1e-10],
                    invalid_conditions=[c['name'] for c in row['conditions'] if not c['valid']],
                    reason='All constraint deficits non-increasing; total deficit reduced.' if allowed else 'Rejected: invalid model, regression in another constraint, or no strict improvement.')
                trace.append(entry)
                if allowed:trials.append((row['score'],row['candidate_id'],candidate,row,entry))
                atomic_json(root/'progress.json',dict(status='running',evaluated_candidates=count,accepted_steps=accepted_steps))
            if not trials:
                # Rejected candidates are retained; refill from unseen choices on
                # the unchanged parent in the next bounded round.
                continue
            _,_,current,assessment,entry=min(trials,key=lambda x:(x[0],x[1]))
            entry['selected']=True;accepted_steps+=1
        if assessment['target_met']:stop_reason='targets_satisfied'
        elif not plan.search.allowed_actions:stop_reason='no_authorized_actions'
        replay_verified=verify_action_replay(root,plan,trace,assessment['candidate_id'])
        verification=evaluate_candidate(plan,current,root,assessment['candidate_id'],verification=True) if plan.verification_conditions else None
        verification_passed=verification['target_met'] if verification else None
        status='target_met' if assessment['target_met'] else 'target_unmet'
        report=(f'Multi-voltage inverse design across conditions `{design_id}`: {status}. Evaluated {count} candidates with {len(plan.conditions)} conditions each; accepted {accepted_steps} local edits.\n\n'
            'Bus identities, topology, coordinates, base demand, contracts and customer phases remain fixed. Transformer and MV/LV conductor upgrades use complete sourced equipment tuples; PV capacity shares move only between customers on the same phase. '
            'Network and equipment are fixed across the conditions of each candidate; PV ratios use unscaled demand. Voltage-tier limits, line and transformer ratings, and VUF remain protected constraints.\n\n'
            'Measured violation locations guide proposals. Every edit reruns all conditions; increased deficits or invalid models trigger rollback. '
            'This is bounded local search, not a causal proof, global optimization or infeasibility certificate. LV models use the selected catalogue and recorded electrical equivalents, without an explicit neutral conductor.\n\n'
            f'Selected model: {assessment["candidate_id"]}. See trace.json for accepted/rejected edits and index.html for models and views.')
        report+=f'\n\nStop reason: {stop_reason}; authorized-action replay verified: {replay_verified}; independent verification conditions passed: {verification_passed} (None means not configured). Verification results do not feed back into this search.'
        output=dict(design_id=design_id,status=status,target_met=assessment['target_met'],directory=str(root),
            baseline=baseline,winner=assessment,stop_reason=stop_reason,authorization_replay_verified=replay_verified,
            verification=verification,verification_passed=verification_passed,
            all_requested_conditions_met=bool(assessment['target_met'] and (verification_passed is not False)),
            accepted_steps=accepted_steps,evaluated_candidates=count,trace=trace,verified_report=report)
        atomic_json(root/'trace.json',trace);atomic_json(root/'search.json',output);(root/'report.md').write_text(report)
        links=''.join(f'<li>{html.escape(c["name"])}: target={c["target_met"]} · <a href="{c["relative_path"]}/visualization.html">View</a> · <a href="{c["relative_path"]}/opendss/Master.dss">OpenDSS</a></li>' for c in assessment['conditions']+(verification['conditions'] if verification else []))
        (root/'index.html').write_text(f'<meta charset="utf-8"><h1>{html.escape(design_id)}: {status}</h1><p>Search target: {assessment["target_met"]}; post-search verification: {verification_passed}; all requested conditions: {output["all_requested_conditions_met"]}</p><ul>{links}</ul><p><a href="report.md">Report</a> · <a href="trace.json">Edit evidence and rollback history</a> · <a href="dataset.zip">Download model package</a></p>')
        atomic_json(root/'progress.json',dict(status=status,evaluated_candidates=count,accepted_steps=accepted_steps))
        with zipfile.ZipFile(root/'dataset.zip','w',zipfile.ZIP_DEFLATED) as archive:
            files=[root/n for n in ('manifest.json','plan.json','baseline.json','search.json','trace.json','report.md','index.html')]
            files.extend((root/'candidates'/assessment['candidate_id']).rglob('*'))
            files.extend((root/'verification').rglob('*'))
            for path in sorted(files):
                if path.is_file():archive.write(path,path.relative_to(root))
        output['artifacts']={str(p.relative_to(root)):file_digest(p) for p in root.rglob('*') if p.is_file() and p.name not in ('.lock','result.json')}
        output['result_hash']=digest(output);atomic_json(root/'result.json',output)
        return output
