"""Versioned local feedback edits of verified research cases."""
import fcntl
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Literal
from pydantic import Field, model_validator
from .schemas import StrictModel, ExperimentSpec, Feeder
from .artifacts import atomic_json, digest
from .style import style_contract
from .redesign import LayoutRequest,StyleTarget,regenerate_layout,check_style_targets

Group=Literal['topology','geometry','loads','pv','equipment','voltage','node_roles','aggregate_load','aggregate_pv','phase_connectivity','phase_loads','phase_pv']


class RevisionAction(StrictModel):
    kind: Literal['geometry_scale','load_scale','pv_ratio','conductor','layout_redesign','phase_balance']
    load_phase_weights: list[float] | None = None
    pv_phase_weights: list[float] | None = None
    layout: LayoutRequest | None = None
    value: float | None = Field(default=None,ge=0,le=100,description='For pv_ratio use a fraction: 60%=0.6. For scaling use a multiplier: increase by 10%=1.1.')
    line_ids: list[str] = Field(default_factory=list,max_length=2000)
    conductor: str | None = Field(default=None,max_length=100)
    evidence: str = Field(min_length=1,max_length=12000)

    @model_validator(mode='after')
    def consistent(self):
        if self.kind=='phase_balance':
            if self.load_phase_weights is None or self.value is not None or self.layout is not None or self.line_ids or self.conductor:
                raise ValueError('phase_balance requires load_phase_weights and optional pv_phase_weights only')
            from .schemas import PhaseDesign
            PhaseDesign(mode='unbalanced',load_phase_weights=self.load_phase_weights,pv_phase_weights=self.pv_phase_weights)
            return self
        if self.load_phase_weights is not None or self.pv_phase_weights is not None:
            raise ValueError('Phase weights require phase_balance')
        if self.kind=='layout_redesign':
            if self.layout is None or self.value is not None or self.line_ids or self.conductor:
                raise ValueError('layout_redesign requires only a layout request')
            return self
        if self.layout is not None:raise ValueError('layout parameters require layout_redesign')
        if self.kind=='conductor':
            if not self.line_ids or not self.conductor or self.value is not None:
                raise ValueError('conductor action requires line_ids and conductor only')
        elif self.value is None or self.line_ids or self.conductor is not None:
            raise ValueError('numeric action requires value only')
        elif self.kind!='pv_ratio' and self.value<=0:
            raise ValueError('scale must be positive')
        elif self.kind=='pv_ratio' and self.value>3:
            raise ValueError('pv_ratio must be <=3')
        return self


class RevisionPlan(StrictModel):
    feedback: str = Field(min_length=1,max_length=12000)
    actions: list[RevisionAction] = Field(default_factory=list,max_length=16)
    style_targets: list[StyleTarget] = Field(default_factory=list,max_length=16)
    preserve: list[Group] = Field(default_factory=lambda:['topology','voltage'])
    unsupported: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


def _groups(f):
    return {
        'topology':{'source':f.source_bus,'buses':sorted(b.id for b in f.buses),
            'edges':sorted((l.id,l.bus1,l.bus2) for l in f.lines),'sites':sorted((l.id,l.bus) for l in f.loads),
            'normally_open_ties':sorted((l.id,l.bus1,l.bus2) for l in f.tie_lines)},
        'geometry':{'buses':sorted((b.id,b.x_km,b.y_km) for b in f.buses),'lengths':sorted((l.id,l.length_km) for l in [*f.lines,*f.tie_lines])},
        'loads':sorted((l.id,l.kw,l.kvar,l.contract_kva) for l in f.loads),
        'pv':sorted((l.id,l.pv_kw) for l in f.loads),
        'equipment':sorted((l.id,*sorted((l.bus1,l.bus2)),l.conductor,l.construction,tuple(l.phases)) for l in [*f.lines,*f.tie_lines]),
        'node_roles':{'source':f.source_bus,'buses':sorted(b.id for b in f.buses),'load_sites':sorted((l.id,l.bus) for l in f.loads)},
        'aggregate_load':sum(l.kw for l in f.loads),
        'aggregate_pv':sum(l.pv_kw for l in f.loads),
        'phase_connectivity':{'mode':f.phase_mode,'buses':sorted((b.id,b.phases) for b in f.buses),'lines':sorted((l.id,l.phases) for l in [*f.lines,*f.tie_lines])},
        'phase_loads':sorted((l.id,p.phase,p.kw,p.kvar) for l in f.loads for p in l.phase_powers),
        'phase_pv':sorted((l.id,p.phase,p.pv_kw) for l in f.loads for p in l.phase_powers),
        'voltage':(f.voltage_kv,f.frequency_hz)}


def apply_revision(feeder, spec, plan):
    plan=RevisionPlan.model_validate(plan.model_dump())
    if plan.unsupported or plan.questions:
        raise ValueError('unsupported or unresolved feedback: '+ '; '.join(plan.unsupported+plan.questions))
    if not plan.actions: raise ValueError('No actionable feedback')
    if any(a.evidence not in plan.feedback for a in [*plan.actions,*plan.style_targets]):
        raise ValueError('Action evidence must be an exact quote from feedback')
    kinds=[a.kind for a in plan.actions]
    if len(set(kinds))!=len(kinds): raise ValueError('Combine each action kind once; duplicate kinds are ambiguous')
    updated=feeder.model_copy(deep=True)
    payload=spec.model_dump()
    payload.update(count=1,n_buses=len(feeder.buses),n_loads_min=len(feeder.loads),n_loads_max=len(feeder.loads),
                   total_kw_min=feeder.total_kw,total_kw_max=feeder.total_kw,max_repairs=0,
                   repair_policy={'strategy':'none','allow_rewire':False})
    redesign=next((a for a in plan.actions if a.kind=='layout_redesign'),None)
    if redesign and len(plan.actions)!=1:raise ValueError('layout_redesign must be the sole action; use a subsequent revision for local changes')
    if redesign:
        updated,target=regenerate_layout(feeder,spec,payload,redesign.layout,plan.preserve)
        payload=target.model_dump()
    # Geometry and equipment edits preserve actual parent loads/PV, not resampling.
    for action in plan.actions:
        if action.kind=='geometry_scale':
            factor=action.value
            source=next(b for b in updated.buses if b.id==updated.source_bus)
            sx,sy=source.x_km,source.y_km
            for bus in updated.buses:
                bus.x_km=sx+(bus.x_km-sx)*factor;bus.y_km=sy+(bus.y_km-sy)*factor
            for line in [*updated.lines,*updated.tie_lines]:line.length_km*=factor
            payload['segment_km_min']*=factor;payload['segment_km_max']*=factor
            if spec.scenario.positions_km is not None:
                payload['scenario']['positions_km']=[(b.x_km,b.y_km) for b in updated.buses]
        elif action.kind=='load_scale':
            for load in updated.loads:
                from .phases import scale_phase_load
                scale_phase_load(load,action.value,1)
            updated.total_kw*=action.value
            payload.update(total_kw_min=updated.total_kw,total_kw_max=updated.total_kw)
        elif action.kind=='conductor':
            lines={l.id:l for l in updated.lines}
            if set(action.line_ids)-set(lines):raise ValueError('Unknown line ID in conductor action')
            for line_id in action.line_ids:lines[line_id].conductor=action.conductor
    # Absolute PV ratio refers to the final load total, independent of action order.
    pv_action=next((a for a in plan.actions if a.kind=='pv_ratio'),None)
    if pv_action:
        for load in updated.loads:
            load.pv_kw=load.kw*pv_action.value
            from .phase_operations import set_phase_pv
            set_phase_pv(load,spec.phase_design.pv_phase_weights or spec.phase_design.load_phase_weights)
    load_action=next((a for a in plan.actions if a.kind=='load_scale'),None)
    payload['pv_ratio']=pv_action.value if pv_action else spec.pv_ratio/(load_action.value if load_action else 1)
    phase_action=next((a for a in plan.actions if a.kind=='phase_balance'),None)
    if phase_action:
        payload['phase_design']['load_phase_weights']=phase_action.load_phase_weights
        payload['phase_design']['pv_phase_weights']=(phase_action.pv_phase_weights or spec.phase_design.pv_phase_weights or spec.phase_design.load_phase_weights)
    target=ExperimentSpec.model_validate(payload)
    from .equipment import equipment_contract_matches
    if not equipment_contract_matches(updated,target):
        raise ValueError('Feedback equipment conflicts with selected source/phase/construction contract')
    if phase_action:
        from .phase_operations import rebalance_powers
        rebalance_powers(updated,target)
    before,after=_groups(feeder),_groups(updated)
    diff={key:{'preserved':before[key]==after[key],'before_hash':digest(before[key]),'after_hash':digest(after[key])} for key in before}
    for key in ('aggregate_load','aggregate_pv'):
        diff[key]['preserved']=math.isclose(before[key],after[key],rel_tol=1e-10,abs_tol=1e-8)
        diff[key].update(before=before[key],after=after[key],comparison='numeric_tolerance_1e-10_relative_1e-8_absolute')
    required={'voltage','node_roles','aggregate_load','aggregate_pv'} if redesign else {'topology','voltage'}
    for group in set(plan.preserve)|required:
        if not diff[group]['preserved']:raise ValueError(f'Feedback conflicts with preserved group: {group}')
    structural_evidence=updated.design_evidence
    updated.design_evidence={
        'method':'feedback_layout_redesign_v1' if redesign else 'feedback_local_revision_v1',
        'parent_evidence_hash':digest(feeder.design_evidence),
        'revision_actions':[a.model_dump() for a in plan.actions],
        'style_contract':style_contract(target,updated),
        'notice':'Parent generation estimates remain in parent artifacts; they are not new power-flow measurements.'}
    # Graph membership is structural evidence, unchanged by the supported actions.
    for key in ('trunk_buses','villages','redesign_policy','topology_design','load_shape','scene_profile','phase_design','equipment_selection'):
        if key in structural_evidence:
            import copy
            updated.design_evidence[key]=copy.deepcopy(structural_evidence[key])
    if redesign:
        updated.design_evidence['initial_conductor_sizing']=structural_evidence['initial_conductor_sizing']
    from .phase_operations import refresh_phase_evidence
    refresh_phase_evidence(updated,target)
    updated.design_evidence['style_contract']=style_contract(target,updated)
    checks=check_style_targets(spec,feeder,target,updated,plan.style_targets)
    if any(not check['passed'] for check in checks):
        raise ValueError('Style target unmet: '+json.dumps(checks,ensure_ascii=False))
    updated.design_evidence['style_target_checks']=checks
    from .rule_system import describe_rule_system
    updated.design_evidence['rule_system']=describe_rule_system(target)
    updated.assumptions.append('Feedback layout redesign: topology/geometry and equipment regenerated under fixed aggregate conditions; see redesign_policy.' if redesign else 'Feedback revision: preserved graph and node roles; no resampling or automatic electrical repair. Original generation assumptions describe the parent.')
    return updated,target,diff


def compile_feedback(feedback,spec,feeder,model=None):
    if not feedback.strip() or len(feedback)>12000:raise ValueError('Provide 1–12000 feedback characters')
    if model is None:
        from .agent import configured_model
        model=configured_model(timeout=25,max_retries=0,max_tokens=2200,disable_thinking=True)
    prompt=('Compile research-feeder feedback into a verifiable revision plan, not an actual engineering plan. Fully address every requirement; record unsupported requirements in unsupported rather than ignoring them. '
        'Local actions: geometry_scale (a multiplier for all spatial dimensions/line lengths, adjusting the length interval accordingly), load_scale (a multiplier for load/contract capacity, preserving absolute PV capacity), '
        'pv_ratio (PV as a fraction of the final total load), and conductor (specified line IDs and research_small/medium/large). '
        'phase_balance applies only to existing unbalanced models: load_phase_weights is a triple of positive values summing to 1; pv_phase_weights is optional. Preserve each node\'s total P/Q/PV, connected phases, and topology; omit PV weights to retain the original PV allocation. This action cannot move a single-phase node to another phase. It does not guarantee a lower VUF; power-flow verification is required. '
        'The global action layout_redesign must be used alone. layout contains layout=spatial_mst/rural_villages/structured_radial and kind=urban/rural; village_count, aspect_ratio, and trunk_fraction may be specified. '
        'structured_radial requires a topology object: family is long_trunk for a long chain, comb for a trunk with multiple laterals, multi_branch for multiple arms sharing a source, balanced_tree for a hierarchical tree, irregular_tree for irregular branches, or open_ring for a normally open ring. '
        'topology.branch_count is only for comb/multi_branch; branching_factor is only for balanced_tree/irregular_tree; topology.trunk_fraction is only for comb (distinct from the village trunk_fraction). '
        'open_ring requires tie_count=1; other structures may specify tie_count as 0–10 normally open ties. Closed-ring operation is unsupported and N-1 cannot be demonstrated; multiple arms still share one source. '
        'The new structures and spatial_mst may specify load_shape as uniform/heterogeneous/downstream_heavy/upstream_heavy and directional load_concentration from 0–4. Non-default shapes conflict with frozen per-node load/PV. '
        'All new structures can use the currently supported voltages. source_branches/max_children/mean_depth/max_degree/load_weighted_depth/tie_count may be style targets. '
        'rural_villages requires rural and 6/10/20kV, and does not support aspect_ratio; spatial_mst does not support village_count or a non-default trunk_fraction. '
        'Redesign preserves node identities/count/load points, total P, total PV, voltage, power factor, original line-segment length bounds, and operating conditions; it reconnects and repositions nodes and reselects conductors. '
        'The village template places 80% of the load in villages and may redistribute per-node load/PV. This is an assumption, not a normative requirement, and may conflict with preserving per-node loads. '
        'For layout_redesign, do not include topology/geometry/equipment in preserve by default unless the user explicitly requests their preservation; retain explicit preservation conflicts rather than dropping them. '
        'Use aggregate_load/aggregate_pv to preserve total load/total PV, respectively; use loads/pv to preserve load/PV at each node. For an ambiguous request to preserve loads, prefer loads; do not substitute aggregate preservation. '
        'Local actions preserve topology/voltage by default; record additional preservation requirements in preserve. '
        'Layout redesign is currently unsupported for reference_conditioned parent cases. Node addition/deletion, local node relocation, freezing part of a subnet, transformers, and objective search across operating conditions are unsupported. '
        'For a more prominent trunk, choose rural_villages and increase trunk_fraction (0.1–0.8, default 0.3), adding a trunk_length_share increase target. '
        'Propose an actual change from the parent parameters; for example, try 0.5 when the original is 0.3 and record this as an inference. Regenerating the same layout with the original 0.3 does not make the trunk more prominent. '
        'evidence must be a contiguous verbatim source span; do not use ellipses or paraphrase. The same complete sentence may be quoted for multiple targets. '
        'For more dispersed villages, use mean_village_anchor_distance_km increase; at least 2 villages are required. Increasing the fraction of trunk nodes may increase spacing, but this must be checked and cannot be guaranteed. '
        'Template parameters may be inferred from qualitative style requests, but list every unstated value in assumptions and use style_targets to record directions or thresholds requiring verification; these are not normative requirements. '
        'Use questions for ambiguous requirements without a clear action target/supported metric; do not fabricate attainment of electrical objectives. '
        'The evidence for each action and style_target must quote the original feedback verbatim. ge/le require threshold; increase/decrease must not have threshold and compare against the parent model. '
        'Style metrics count only explicitly labeled trunk/village structures. spatial_mst has no village labels; 0 does not mean no physical trunk exists. '
        'Except for topology-style template inference, do not choose local revision multipliers without authorization. At most one action of each kind is allowed. Feedback does not update general rules.\n'
        +json.dumps({'feedback':feedback,'spec':spec.model_dump(),'style':style_contract(spec,feeder),
                     'line_ids':[l.id for l in feeder.lines]},ensure_ascii=False))
    interpreter=model.with_structured_output(RevisionPlan,method='function_calling')
    correction=''
    previous=None
    for attempt in range(2):
        plan=None
        try:
            plan=RevisionPlan.model_validate(interpreter.invoke(prompt+correction))
            plan.feedback=feedback
            if plan.unsupported or plan.questions:return plan
            if previous is not None:
                goals=lambda p:{(t.metric,t.operator,t.threshold) for t in p.style_targets}
                if not set(previous.preserve)<=set(plan.preserve) or not goals(previous)<=goals(plan):
                    raise ValueError('Feedback correction cannot relax frozen groups or remove/change style targets')
            apply_revision(feeder,spec,plan)
            return plan
        except ValueError as exc:
            if attempt:raise ValueError(f'Feedback interpretation failed validation after one correction: {exc}') from exc
            if plan is not None:previous=plan
            correction='\nThe previous plan failed programmatic validation. Correct it while preserving the original requirements; do not relax preservation constraints or remove targets.\n'+json.dumps(
                {'previous_plan':plan.model_dump() if plan is not None else None,'validation_error':str(exc)},ensure_ascii=False)
    raise AssertionError('Unreachable')


def _identifier(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',value):raise ValueError('Invalid experiment/revision ID')
    return value


def revise_case(project_root,parent_id,revision_id,sample_index=0,*,feedback=None,plan=None,execute=True,model=None):
    from .workflow import read_verified_experiment,run_experiment
    root=Path(project_root).resolve()
    _identifier(parent_id);_identifier(revision_id)
    if parent_id==revision_id:raise ValueError('Revision must use a new ID')
    parent=root/'experiments'/parent_id
    verified=read_verified_experiment(parent)
    if isinstance(sample_index,bool) or not isinstance(sample_index,int) or not 0<=sample_index<len(verified['samples']):
        raise ValueError('Invalid parent sample index')
    sample=parent/f'sample_{sample_index:05d}'
    feeder=Feeder.model_validate_json((sample/'feeder.json').read_text())
    spec=ExperimentSpec.model_validate(verified['specification'])
    directory=root/'revisions'/revision_id
    directory.mkdir(parents=True,exist_ok=True)
    with (directory/'.lock').open('w') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        path=directory/'plan.json'
        identity={'experiment_id':parent_id,'sample_index':sample_index,'configuration_hash':verified['configuration_hash'],
                  'model_hash':digest(feeder.model_dump())}
        if path.exists():
            saved=json.loads(path.read_text());checksum=saved.pop('hash')
            if digest(saved)!=checksum or saved['parent']!=identity:raise ValueError('Revision plan/parent changed; use a new ID')
            parsed=RevisionPlan.model_validate(saved['plan'])
            if (feedback is not None and parsed.feedback!=feedback) or (plan is not None and parsed!=RevisionPlan.model_validate(plan)):
                raise ValueError('Revision feedback changed; use a new ID')
        else:
            if (root/'experiments'/revision_id).exists():raise ValueError('Revision experiment ID already exists')
            parsed=RevisionPlan.model_validate(plan) if plan is not None else compile_feedback(feedback or '',spec,feeder,model)
            if feedback is not None and parsed.feedback!=feedback:raise ValueError('Plan feedback differs from original request')
            saved={'parent':identity,'plan':parsed.model_dump()}
            atomic_json(path,{**saved,'hash':digest(saved)})
        if parsed.unsupported or parsed.questions:
            return {'status':'blocked','directory':str(directory),'plan':parsed.model_dump(),
                    'verified_report':'Revision not executed: '+'; '.join(parsed.unsupported+parsed.questions)}
        edited,target,diff=apply_revision(feeder,spec,parsed)
        # Batch seed is an execution identity; geometry was copied, not regenerated.
        edited.seed=int.from_bytes(hashlib.sha256(f'{target.seed}:0'.encode()).digest()[:4],'big')
        edited.design_evidence['parent']=identity
        output={'status':'draft','directory':str(directory),'parent':identity,'plan':parsed.model_dump(),
                'diff':diff,'operation':'layout_redesign' if any(a.kind=='layout_redesign' for a in parsed.actions) else 'local_edit',
                'style_target_checks':edited.design_evidence['style_target_checks'],
                'style_targets_met':True if parsed.style_targets else None,
                'before_style':style_contract(spec,feeder),'after_style':style_contract(target,edited),
                'specification':target.model_dump(),'verified_report':'Revision draft generated; power flow has not yet been run.'}
        if execute:
            result_path=directory/'result.json'
            if result_path.exists():
                old=json.loads(result_path.read_text());checksum=old.pop('hash')
                if digest(old)!=checksum:raise ValueError('Revision result hash mismatch')
                if old['status']=='completed':
                    current=read_verified_experiment(root/'experiments'/revision_id)
                    if current['configuration_hash']!=old['outcome']['configuration_hash']:raise ValueError('Revision experiment changed')
                    return old
            result=run_experiment(target,root,revision_id,initial_feeders=[edited])
            after=read_verified_experiment(root/'experiments'/revision_id)
            output.update(status='completed',outcome=after,
                comparison={'before':verified['samples'][sample_index],'after':after['samples'][0]},
                verified_report=('Layout redesign version saved; connectivity, geometry, and equipment may have changed. The original case was not overwritten. ' if output['operation']=='layout_redesign' else 'Separate local revision saved; the original case was not overwritten. ')+('The listed style targets were checked and met. ' if parsed.style_targets else 'No separate style acceptance targets were specified. ')+'Electrical acceptance is reported separately; violations were not automatically repaired.\n\n'+result['verified_report'])
        atomic_json(directory/'result.json',{**output,'hash':digest(output)})
        return output
