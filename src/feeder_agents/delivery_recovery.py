"""Bounded post-delivery recovery with fixed acceptance and separate artifacts."""
import json
import re
from pathlib import Path
from .adaptive_planning import adaptive_plan
from .artifacts import atomic_json,digest
from .requirements import RequirementLedger,check_ledger_plan
from .requirement_contract import audit_delivery,canonical


def generate(brief,root,run_id,model,request,local_topology,feedback=True,rounds=None,memory=True,workers=1):
    options=dict(agent_feedback=feedback,feedback_model=model,request=request,use_memory=memory)
    if rounds is not None:options['feedback_rounds']=rounds
    if brief['plan_type']=='hierarchical':
        from .hierarchy import historical_hierarchy_spec
        from .hierarchy_workflow import run_hierarchy
        protection=None
        if brief.get('requirement_ledger') is not None:
            ledger=RequirementLedger.model_validate(brief['requirement_ledger'])
            protection=any(r.priority=='hard' and canonical(r.target_field or '')=='customer_allocation' for r in ledger.requirements)
        elif isinstance(brief.get('intent'),dict) and 'assignments' in brief['intent']:
            protection=any(a.get('origin')=='user' and canonical(a.get('field',''))=='customer_allocation' for a in brief['intent']['assignments'])
        return run_hierarchy(historical_hierarchy_spec(brief['plan']['spec']),root,run_id,
                             local_topology=local_topology,protect_customer_allocation=protection,**options)
    if brief['plan_type']=='feeder':
        from .schemas import ExperimentPlan
        from .workflow import run_experiment
        plan=ExperimentPlan.model_validate(brief['plan'])
        return run_experiment(plan.spec,root,run_id,workers=workers,plan=plan)
    if brief['plan_type']=='transmission':
        from .transmission import run_transmission
        return run_transmission(brief['plan']['spec'],root,run_id,**options)
    raise ValueError('Unsupported delivery model family: '+brief['plan_type'])



def delivery_diagnostics(brief, output):
    """Ground feedback in saved solver measurements and failed rule evidence."""
    from .artifacts import file_digest
    root=Path(output['directory']).resolve()
    rows=[]
    for row in output.get('samples',[])[:12]:
        name=row.get('sample_id','')
        if not re.fullmatch(r'sample_[0-9]{5}',name):continue
        folder=root/name
        if not folder.resolve().is_relative_to(root):continue
        facts=dict(sample_id=name,model_family=brief['plan_type'],metrics={},failed_checks=[],evidence_files={})
        for filename in ('validation.json','simulation.json'):
            path=folder/filename
            try:
                content=json.loads(path.read_text())
                facts['evidence_files'][str(path)]=file_digest(path)
                for key in ('converged','min_voltage_pu','max_voltage_pu','max_vuf_percent','max_loading_ratio',
                            'loss_kw','load_kw','generation_kw','source_kw'):
                    if key in content:facts['metrics'][key]=content[key]
                facts['metrics'].update(content.get('metrics',{}))
                checks=content.get('checks',[])
                if isinstance(checks,dict):
                    checks=[dict(rule_id=k,status='fail') for k,v in checks.items() if v is False]
                for check in checks:
                    if check.get('status')!='fail':continue
                    fact=dict(check)
                    if isinstance(fact.get('evidence'),dict):
                        fact['evidence']={k:(v[:16] if isinstance(v,list) else v) for k,v in fact['evidence'].items()}
                    facts['failed_checks'].append(fact)
            except (OSError,ValueError,TypeError) as exc:
                facts.setdefault('unavailable_evidence',[]).append(dict(file=filename,error_type=type(exc).__name__))
        if row.get('error'):facts['generation_error']=row['error']
        facts['scope']='Measurements from saved artifacts; samples limited to first 12 and list evidence to first 16. Full evidence files retained.'
        rows.append(facts)
    return rows


def _passed(audit):
    return {(s['sample_id'],c['id']) for s in audit['samples'] for c in s['checks'] if c['status']=='passed'}


def _rank(audit):
    return (audit['jointly_accepted'],sum(s['electrical_accepted'] for s in audit['samples']),
            -sum(s['failed']+s['unverified'] for s in audit['samples']))


def construction_failures(brief,output):
    """Only a known generator failure before any model exists permits this path.

    Missing files on an otherwise generated result still require review.
    Mixed successful/failed batches keep the conservative existing behavior.
    """
    samples=output.get('samples',[])
    family=brief['plan_type']
    codes={'hierarchical':'lv_catalogue_exhausted','feeder':'tie_candidates_exhausted'}
    if family not in codes or not samples or len(samples)!=output.get('attempted'):return []
    diagnostics=[]
    for sample in samples:
        detail=sample.get('construction_failure',{})
        if sample.get('accepted') or not sample.get('error') or detail.get('code')!=codes[family]:return []
        if (Path(output['directory'])/sample['sample_id']/'feeder.json').exists():return []
        if family=='feeder':
            spec=brief['plan']['spec']
            if (detail.get('spec_hash')!=digest(spec) or detail.get('seed')!=sample.get('seed')
                    or detail.get('requested_tie_count')!=spec['scenario']['tie_count']
                    or detail.get('segment_km_min')!=spec['segment_km_min']
                    or detail.get('segment_km_max')!=spec['segment_km_max']):return []
            required=detail.get('required_new_ties');eligible=detail.get('eligible_tie_count')
            if (type(required) is not int or type(eligible) is not int
                    or not 0<=eligible<required):return []
        diagnostics.append(detail)
    return diagnostics


def hierarchical_construction_fields(ledger, failures, request):
    """Design freedoms for an LV failure, never permissions from a diagnostic."""
    from .requirement_contract import canonical
    aliases={'transformers':'transformer_count'}
    fixed={aliases.get(field,field) for item in ledger.requirements
           if item.priority=='hard' and item.disposition=='supported' and item.operator=='eq'
           for field in [canonical(item.target_field or '')]}
    from .source_review import exact_bus_counts
    if exact_bus_counts(request):fixed.add('n_buses')
    candidates={'users','mv_buses','n_buses','transformer_count','lv_branches'}
    if any(row.get('connection')=='lv_branch' for row in failures):
        candidates.update({'customer_allocation','lv_topology'})
    return candidates-fixed


def recover_delivery(brief,output,request,root,design_id,model,local_topology=False,max_rounds=2,generation_options=None,planning_memory=None):
    if not 0<=max_rounds<=2:raise ValueError('At most two delivery redesigns')
    root=Path(root)
    directory=root/'designs'/design_id/'recovery'
    ledger=RequirementLedger.model_validate(brief['requirement_ledger'])
    selected=brief;best=output;audit=audit_delivery(ledger,brief,output)
    context_hash=digest(dict(request=request,ledger=ledger.model_dump(),initial=brief['plan']))
    trace=dict(version='m41',source_hash=digest(request),contract_hash=context_hash,initial_directory=output['directory'],
               rounds=[],stop_reason=None)

    def finish(reason):
        trace['stop_reason']=reason
        trace['selected_directory']=best['directory']
        atomic_json(directory/'recovery.json',trace)
        atomic_json(directory/'selected_brief.json',selected)
        return dict(brief=selected,output=best,audit=audit,trace=trace)

    if audit['all_satisfied']:return finish('accepted')
    construction=construction_failures(brief,output)
    construction_mode=bool(construction)
    allowed_structure=(hierarchical_construction_fields(ledger,construction,request)
                       if construction_mode and brief['plan_type']=='hierarchical'
                       else {'users','lv_branches','transformer_count','n_buses'})
    tie_construction=construction_mode and brief['plan_type']=='feeder'
    if tie_construction:
        fixed={canonical(item.target_field or '') for item in ledger.requirements if item.priority=='hard'}
        allowed_structure={'segment_km_min','segment_km_max'}-fixed
        if not allowed_structure:return finish('construction_settings_fixed')
    if construction_mode and brief['plan_type']=='hierarchical' and not allowed_structure:
        return finish('construction_settings_fixed')
    previous_spec=selected['plan']['spec']
    if (any(s['unverified'] for s in audit['samples']) or len(audit['samples'])!=audit['attempted']) and not construction_mode:
        return finish('missing_evidence_requires_review')
    # The existing local feedback path runs before this controller. Regenerating
    # an entire case cannot guarantee preservation of an existing geometry.
    if re.search('(?:\u4fdd\u6301|\u4fdd\u7559|\u56fa\u5b9a|\u4e0d\u6539\u53d8|\u7981\u6b62\u6539\u53d8|\u4e0d\u5f97\u6539\u53d8).{0,24}(?:\u4f4d\u7f6e|\u5750\u6807|\u62d3\u6251|\u8fde\u63a5|\u76f8\u522b)|(?:\u4f4d\u7f6e|\u5750\u6807|\u62d3\u6251|\u8fde\u63a5|\u76f8\u522b).{0,8}\u4e0d\u53d8|preserv|keep.{0,24}(?:position|topology|coordinate)|fixed.{0,12}topology',request,re.I):
        return finish('preservation_requires_local_revision')
    initial=brief['plan']['spec']
    limits={k:v for k,v in initial.items() if ('voltage_min' in k or 'voltage_max' in k or k in ('max_vuf_percent','count','targets','validation_conditions'))}
    if brief['plan_type']=='feeder':
        # Replanning cannot turn a normal case into a permissive stress case,
        # change rule applicability, remove document rules or relax VUF limits.
        for key in ('mode','document_rules','load_semantics','special_pf_agreement','frequency_hz','voltage_kv','pv_ratio','power_factor'):
            limits[key]=initial[key]
        limits['phase_design.max_vuf_percent']=initial['phase_design']['max_vuf_percent']
        limits['phase_design.mode']=initial['phase_design']['mode']
        limits['scenario.kind']=initial['scenario']['kind']
        limits['repair_policy']=initial['repair_policy']
        limits['max_repairs']=initial['max_repairs']
    seen={digest(initial)}
    proposal_error=None
    for index in range(max_rounds):
        folder=directory/f'round_{index+1:02}'
        feedback=dict(original_contract=ledger.model_dump(),previous_spec=previous_spec,
                      failed_delivery=audit,physical_diagnostics=delivery_diagnostics(selected,best),protected_settings=limits,
                      instruction='Redesign based on final-file acceptance diagnostics. Do not change the acceptance ledger or relax thresholds. Return only parameter proposals supported by evidence of improvement.')
        if proposal_error:feedback['previous_proposal_error']=proposal_error
        if tie_construction:
            feedback.update(construction_failures=construction,construction_recovery_allowed_fields=sorted(allowed_structure),
                instruction='Normally open tie construction failed: the generator exhausted all nonadjacent bus pairs, and too few candidates satisfy the current length interval. No deliverable model has been constructed. '
                'Use candidate counts, the distance to the nearest unused bus pair, and the original length interval to adjust only unfixed segment-length design parameters listed in construction_recovery_allowed_fields. '
                'segment_km_min/max also affects regenerated geometric spacing; do not assume increasing the upper bound guarantees candidates. Submissions must undergo actual generation and acceptance against the original ledger. '
                'Preserve all other previous_spec fields individually, especially bus count, power, PV, scenario, topology, phases, voltage, equipment, ampacity parameters, acceptance thresholds, tie count, export formats, and seed. '
                'Do not change user-fixed length conditions. Insufficient candidates means the current tool design found no acceptable solution, not mathematical infeasibility. ')
        elif construction_mode:
            feedback.update(construction_failures=construction,construction_recovery_allowed_fields=sorted(allowed_structure),
                instruction='During construction, the conductor catalog could not serve the specified load distribution, so no deliverable model exists. Reallocate structural capacity using the failed component, required current, and maximum catalog-supported current. '
                'For overloaded branches, try balanced customer_allocation or branch_star lv_topology; both preserve each transformer branch-node count. For an overloaded individual service connection, try adjusting users if unspecified. '
                'Modify only fields in construction_recovery_allowed_fields. Do not change user-fixed customer counts, transformer counts, branch counts, or topology. Preserve every other previous_spec field. '
                'Do not relax total load, PV ratio, phase allocation, voltage, scenario, installation, equipment catalogs, ampacity derating, margins, or voltage-drop budgets. '
                'When changing users/mv_buses/legal branch counts, recalculate n_buses using the bus-count formula and respect the source bus-count range; do not retain stale derived totals. Every candidate must be verified by generation and solver tools. '
                'If all effective structural parameters are user-fixed, the current tool candidate range has not yielded an acceptable solution; this does not prove mathematical infeasibility. Do not reduce load or switch to legacy_epri to bypass the catalog. ')
        atomic_json(folder/'feedback.json',feedback)
        try:
            path=folder/'candidate.json'
            if path.exists():
                saved=json.loads(path.read_text())
                if saved['context_hash']!=digest(feedback) or saved['hash']!=digest(saved['brief']):
                    raise ValueError('Recovery cache changed; use a new design ID')
                candidate=saved['brief']
            else:
                candidate=adaptive_plan(request,folder,model,feedback=feedback,memory=planning_memory)
                atomic_json(path,dict(brief=candidate,hash=digest(candidate),context_hash=digest(feedback)))
            if candidate['status']!='ready':
                trace['rounds'].append(dict(round=index+1,status=candidate['status'],issues=candidate.get('issues',[])))
                return finish('planning_not_ready')
            if candidate['plan_type']!=brief['plan_type']:raise ValueError('Redesign changed model family')
            spec=candidate['plan']['spec']
            # Replanning is not permission to change the randomization control.
            spec['seed']=initial['seed']
            def setting(values,key):
                for part in key.split('.'):
                    if not isinstance(values,dict):return None
                    values=values.get(part)
                return values
            changed_limits=[k for k,v in limits.items() if setting(spec,k)!=v]
            if changed_limits:raise ValueError('Redesign changed protected acceptance settings: '+', '.join(changed_limits))
            if brief['plan_type']=='feeder' and candidate['plan'].get('export_formats',['opendss'])!=brief['plan'].get('export_formats',['opendss']):
                raise ValueError('Redesign changed protected export formats')
            if construction_mode:
                changed={k for k in set(initial)|set(spec) if initial.get(k)!=spec.get(k)}
                if changed-allowed_structure:raise ValueError('Construction redesign changed protected non-structural fields: '+', '.join(sorted(changed-allowed_structure)))
            check_ledger_plan(ledger,candidate)
            if construction_mode and brief['plan_type']=='hierarchical':
                from .source_review import check_source_numbers
                check_source_numbers(request,candidate)
            key=digest(spec)
            if key in seen:
                if construction_mode and index+1<max_rounds:
                    proposal_error='The previous round returned the same failed design without changing any design freedom. Use construction_failures diagnostics to adjust only fields in construction_recovery_allowed_fields that are not user-fixed; preserve all other parameters and acceptance thresholds.'
                    trace['rounds'].append(dict(round=index+1,status='repeated_design',selected=False,diagnosis=proposal_error))
                    continue
                return finish('repeated_design')
            seen.add(key)
            # Always score with the original contract, never the new proposal's ledger.
            candidate['requirement_ledger']=ledger.model_dump()
        except ValueError as exc:
            trace['rounds'].append(dict(round=index+1,status='invalid_redesign',diagnosis=str(exc)))
            if (construction_mode or brief['plan_type']=='feeder') and index+1<max_rounds:
                proposal_error=str(exc)+'. The previous candidate was not executed; the original design remains selected. Preserve the original ledger and protected_settings in full. Copy all unchanged values from previous_spec, correct only permitted unfixed design items, and resubmit.'
                continue
            return finish('invalid_redesign')
        except Exception as exc:
            trace['rounds'].append(dict(round=index+1,status='planning_error',error_type=type(exc).__name__))
            return finish('planning_error')
        try:
            run_id=f'{design_id[:45]}_recovery_{index+1}'
            delivered=generate(candidate,root,run_id,model,request,local_topology,**(generation_options or {}))
            checked=audit_delivery(ledger,candidate,delivered)
            previously_electrical={s['sample_id'] for s in audit['samples'] if s['electrical_accepted']}
            now_electrical={s['sample_id'] for s in checked['samples'] if s['electrical_accepted']}
            improved=(_passed(audit)<=_passed(checked) and _rank(checked)>_rank(audit)
                      and checked['attempted']==audit['attempted'] and previously_electrical<=now_electrical)
            trace['rounds'].append(dict(round=index+1,directory=delivered['directory'],selected=improved,
                                        audit=checked,diagnosis='requirement_or_electrical_failure'))
            if improved:selected,best,audit=candidate,delivered,checked
            if audit['all_satisfied']:return finish('accepted')
            new_construction=construction_failures(candidate,delivered) if construction_mode else []
            if new_construction:
                # Failed proposals remain unselected. Their diagnostic may
                # guide one further bounded attempt; no false progress claim.
                construction=new_construction;previous_spec=spec
                continue
            if not improved:return finish('no_improvement')
            previous_spec=selected['plan']['spec']
        except Exception as exc:
            trace['rounds'].append(dict(round=index+1,status='execution_error',error_type=type(exc).__name__))
            return finish('execution_error')
    return finish('round_limit')
