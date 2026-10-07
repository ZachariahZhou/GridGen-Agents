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
    if re.search(r'(?:保持|保留|固定|不改变|禁止改变|不得改变).{0,24}(?:位置|坐标|拓扑|连接|相别)|(?:位置|坐标|拓扑|连接|相别).{0,8}不变|preserv|keep.{0,24}(?:position|topology|coordinate)|fixed.{0,12}topology',request,re.I):
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
                      instruction='根据最终文件验收诊断重新设计，不能更改验收台账或放宽阈值。仅返回确有改善依据的参数方案。')
        if proposal_error:feedback['previous_proposal_error']=proposal_error
        if tie_construction:
            feedback.update(construction_failures=construction,construction_recovery_allowed_fields=sorted(allowed_structure),
                instruction='常开联络线构造失败：生成器已遍历所有非相邻母线对，当前长度区间内候选不足，尚未形成可交付模型。'
                '依据候选数量、最近未占用母线对距离与原长度区间，仅调整construction_recovery_allowed_fields列出的未固定线段长度设计参数。'
                'segment_km_min/max也影响重新生成的几何间距，不能假设扩大上限必然找到候选；提交后必须由真实生成与原台账验收。'
                '逐项保留previous_spec其他字段，特别是母线数、功率、PV、场景、拓扑、相别、电压、设备、载流参数、验收阈值、联络线数量、导出格式与seed。'
                '用户固定的长度条件不可改；候选不足只表示当前工具设计未找到合格方案，不代表数学不可行。')
        elif construction_mode:
            feedback.update(construction_failures=construction,construction_recovery_allowed_fields=sorted(allowed_structure),
                instruction='构造阶段导线目录无法满足给定负荷分布，尚未形成可交付模型。依据失败部件、需求电流和目录最大可支持电流重新分配结构容量。'
                '支线超限可尝试均衡的customer_allocation或branch_star的lv_topology，二者均保留每台配变的分支节点数；单用户接户线超限可尝试调整未指定的users。'
                '只能修改construction_recovery_allowed_fields中的字段；用户明确固定的用户数、配变数、分支数和拓扑不可改，其他字段逐项保留previous_spec。'
                '总负荷、PV比例、相别分配、电压、场景、敷设、设备目录、载流折减、裕度及压降预算不能放松。'
                '改变users/mv_buses/合法分支数时按母线公式同步计算n_buses，并遵守原文母线数范围；不能沿用旧的派生总数。所有候选必须由生成和求解工具验证。'
                '如果所有有效结构参数都被用户固定，说明当前工具候选范围未找到合格方案，不代表数学不可行；不能减少负荷或切换legacy_epri绕开目录。')
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
                    proposal_error='上一轮返回了已失败的相同设计，未改变任何设计自由度。依据construction_failures诊断仅调整construction_recovery_allowed_fields中未被用户固定的字段；保留其他所有参数与验收阈值。'
                    trace['rounds'].append(dict(round=index+1,status='repeated_design',selected=False,diagnosis=proposal_error))
                    continue
                return finish('repeated_design')
            seen.add(key)
            # Always score with the original contract, never the new proposal's ledger.
            candidate['requirement_ledger']=ledger.model_dump()
        except ValueError as exc:
            trace['rounds'].append(dict(round=index+1,status='invalid_redesign',diagnosis=str(exc)))
            if (construction_mode or brief['plan_type']=='feeder') and index+1<max_rounds:
                proposal_error=str(exc)+'. 上一候选未执行，原方案仍被选中。完整保留原台账与protected_settings；从previous_spec复制所有未修改值，仅修正允许的未固定设计项，再提交。'
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
