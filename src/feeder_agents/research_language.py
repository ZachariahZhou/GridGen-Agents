"""Natural-language research purposes compiled into executable, scoped tasks."""
import fcntl
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import Field,model_validator

from .artifacts import atomic_json,digest
from .research_schema import ResearchTaskPlan
from .schemas import StrictModel


PATTERNS={
    'voltage_control':'\u7535\u538b\u63a7\u5236|\u65e0\u529f(?:\u7535\u538b)?\u63a7\u5236|\u65e0\u529f\u652f\u6491|voltage[ -]control|reactive[ -]power[ -]support',
    'static_pv_impact':'(?:\u5149\u4f0f|PV|photovoltaic).{0,20}(?:\u63a5\u5165\u5f71\u54cd|\u7535\u538b\u5f71\u54cd|\u63a5\u5165\u7814\u7a76|integration impact|voltage impact)|(?:\u7814\u7a76|\u5206\u6790).{0,8}\u5149\u4f0f\u63a5\u5165',
    'transmission_transfer':'\u8f93\u7535\u529f\u7387\u8f6c\u79fb|\u533a\u57df\u95f4\u529f\u7387\u8f6c\u79fb|\u8f93\u7535.{0,8}(?:\u4f20\u8f93|\u8f6c\u79fb|\u62e5\u585e)|(?:transmission|interarea|inter-area)[ -](?:power[ -])?transfer'}


def research_task_names(request):
    names=set()
    for clause in re.split(r'[。；;，,\n]',request):
        for name,pattern in PATTERNS.items():
            for match in re.finditer(pattern,clause,re.I):
                prefix=clause[:match.start()]
                if prefix.endswith('\u4e0d') and match.group().startswith(('\u7814\u7a76','\u5206\u6790')):continue
                if re.search('(?:\u4e0d\u7814\u7a76|\u4e0d\u9700\u8981(?:\u7814\u7a76|\u5206\u6790)?|\u65e0\u9700(?:\u7814\u7a76|\u5206\u6790)?|do not study|don[\\x27\u2019]t study|without|\\bnot)\\s*[^\u3002\uff1b;\uff0c,\\n]{0,25}$',prefix,re.I):continue
                names.add(name)
    return names


def is_research_request(request):
    return bool(research_task_names(request))


def research_task_capabilities():
    return dict(version='research_task_v1',tasks={
        'voltage_control':'Design d|V|/dQ plus finite external Q-support validation; not a controller',
        'static_pv_impact':'Design d|V|/dP at actual PV ports plus same-network PV-off/on contrast',
        'transmission_transfer':'Design peak branch MW/MW response plus fixed-Q balanced MW transfer validation'},
        defaults='Disclosed baseline-relative research protocols, not calibrated high/medium/low bands',
        outputs=['OpenDSS or MATPOWER model','Visualization','Frozen task contract','Suitability metrics','Candidate feedback','Verified dataset zip'],
        schema=ResearchTaskPlan.model_json_schema())


class ResearchBrief(StrictModel):
    status: Literal['ready','needs_clarification','unsupported']
    plan: ResearchTaskPlan | None = None
    issues: list[str] = Field(default_factory=list,max_length=16)

    @model_validator(mode='after')
    def ready(self):
        if self.status=='ready' and (self.plan is None or self.issues):raise ValueError('Ready requires a plan and no unresolved issues')
        if self.status!='ready' and not self.issues:raise ValueError('Explain unresolved research requirements')
        return self


PROMPT='''Compile the full research request into one ResearchTaskPlan. Preserve every explicit count, voltage, load, PV, scene, phase, target and restriction. Never omit unsupported requirements to make a ready plan.
Tasks: voltage_control designs d|V|/dQ and verifies a finite external positive Q-support injection; static_pv_impact designs d|V|/dP at actual PV nodes and verifies full existing PV-off/on on unchanged topology; transmission_transfer designs peak branch MW/MW sensitivity and verifies a finite balanced P transfer at fixed Q. These deliver reusable static grid models and test evidence, not installed controllers, hosting-capacity optimization, OPF, N-1 certification, dynamics or time-series data.
For unspecified distribution design use 37 total buses, 10 kV, 360 kW fixed demand, rural spatial_mst, engineering_profile matching urban/rural, unbalanced with single_phase_laterals=0 and two_phase_laterals=0. For PV task only, default pv_ratio=0.6 if unspecified; explicit zero PV conflicts with this task. For unspecified transmission use 139 buses, 12 generators, 220 kV and 1200 MW. Omit these defaults when user supplies values. Nodes need not be specified; record inferred defaults as assumptions. Exact n_buses includes source; omit n_loads_min/max or set BOTH to n_buses-1. Phase weights are fractions [.5,.3,.2], not [1,1,1]. count=1; multiple grids need separate task runs and should be clarified, not silently reduced.
Omit response_lower/upper if no response interval is requested: standard task protocol supplies original-baseline ratio [1.10,1.30] for voltage/PV and [0.50,0.98] for transfer. These are disclosed research assumptions, not calibrated difficulty levels. Explicit vague high/low sensitivity without definition needs clarification. Explicit absolute targets need units and response_reference=absolute. Bus IDs must exist; leave port fields empty unless explicitly named. Automatic ports are frozen before measurement. kW vs MW, kvar vs MVAr must be converted. probe_step is a differential test; support_mvar (default .01) and transfer_mw (default 1) are separate finite-intervention budgets and must preserve any specified values.
For new synthesis, omit search to use disclosed protocol defaults: compatible conductor selection and generated uniform geometry scaling up to 75%, or legal transmission parallel circuits; 4 rounds,12 previews. Explicit geometry freezes set preserve_geometry=true; equipment freezes set preserve_equipment=true. Set base_spec.repair_policy.strategy=none for distribution: the response loop owns edits. Never change explicit coordinates, demand, PV, phases, nominal voltages or source/control policy. Local geometry/rewire/load actions require explicit numeric permissions, same ResponseSearch scope as the response API. If search supplied, preserve all limits and include only authorized actions; exact fixed geometry/equipment overrides defaults. Explicit LLM selection uses search.selector=agent; otherwise deterministic selection is allowed and must not be called LLM decisions.
Conflicting requirements -> needs_clarification; unsupported deliverables or multiple simultaneous task families -> unsupported or clarification with explanation. Retain the complete research purpose and English assumptions. Do not invent installed Q-control hardware; support_mvar is the declared external test actuator budget.
Field placement example (adapt values to the request): {"status":"ready","plan":{"task":"voltage_control","research_question":"Voltage-control research","base_spec":{"network_kind":"distribution","n_buses":37,"voltage_kv":10,"total_kw_min":360,"total_kw_max":360,"pv_ratio":0,"count":1,"scenario":{"kind":"rural","layout":"spatial_mst","engineering_profile":"rural"},"phase_design":{"mode":"unbalanced","single_phase_laterals":0,"two_phase_laterals":0},"repair_policy":{"strategy":"none"}},"support_mvar":0.02,"assumptions":["Use disclosed response and search protocol defaults."]},"issues":[]}.
In particular, phase_design, n_buses, voltage_kv, total_kw_min/max and count belong directly under base_spec, NEVER under scenario. When default search or default targets are requested, OMIT search and response_lower/upper entirely: do not create an empty-action search or copy numeric protocol defaults into user target fields.
'''


def _source_issues(request,plan,node_count):
    from .source_review import exact_bus_counts
    counts=exact_bus_counts(request)
    if node_count is not None:counts.add(node_count)
    from .research_source import task_source_issues
    issues=task_source_issues(request,plan)
    if len(counts)>1 or (counts and plan.base_spec.n_buses!=next(iter(counts))):issues.append('Compiled plan does not preserve the exact bus count.')
    names=research_task_names(request)
    if len(names)>1 or (names and plan.task not in names):issues.append('Research task family conflicts with the request; separate simultaneous task families.')
    if re.search('\u56fa\u5b9a\u5750\u6807|\u4fdd\u6301\u5750\u6807|\u5750\u6807\u4e0d\u53d8|\u4e0d\u5141\u8bb8.{0,8}(?:\u7f29\u653e|\u7a7a\u95f4|\u5e03\u5c40)|fixed coordinates|preserve coordinates|(?:do not|don[\\x27\u2019]t) allow.{0,15}(?:scal|geometr)',request,re.I) and not plan.preserve_geometry:
        issues.append('Input freezes geometry but the compiled plan does not.')
    if re.search('\u8bbe\u5907\u4e0d\u53d8|\u4e0d(?:\u66f4\u6362|\u6539\u53d8)\u8bbe\u5907|fixed equipment|preserve equipment',request,re.I) and not plan.preserve_equipment:
        issues.append('Input freezes equipment but the compiled plan does not.')
    if re.search('\u9ad8\u7075\u654f\u5ea6|\u4f4e\u7075\u654f\u5ea6|\u9ad8\u654f\u611f\u5ea6|\u4f4e\u654f\u611f\u5ea6|(?:high|low)[ -]sensitivity',request,re.I) and plan.response_lower is None and plan.response_upper is None:
        issues.append('Qualitative response level requires a numerical or reference-relative definition.')
    if plan.search:
        from .response_language import _local_permission_issues,_layout_permission
        issues.extend(_local_permission_issues(request,plan.search))
        if 'scale_layout' in plan.search.allowed_actions:
            bound=_layout_permission(request)
            if bound is None or plan.search.max_layout_scale_change>bound+1e-9:
                issues.append('Explicit custom geometry search needs its numerical permission; omit search for declared fresh-synthesis defaults.')
    return issues


def design_research_from_request(request,workspace,design_id,*,execute=True,model=None,node_count=None):
    if not request.strip() or len(request)>12000:raise ValueError('Provide 1-12000 characters')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',design_id):raise ValueError('Invalid research design ID')
    if node_count is not None and (type(node_count) is not int or not 3<=node_count<=2001):raise ValueError('Invalid exact bus count')
    from .request_contract import request_guard
    blocked=request_guard(request)
    root=Path(workspace).resolve()/'designs'/design_id;root.mkdir(parents=True,exist_ok=True)
    identity=dict(request=request,node_count=node_count,compiler_version='research_task_v1.1',prompt_hash=digest(PROMPT))
    with (root/'.lock').open('w') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        path=root/'brief.json'
        if path.exists():
            saved=json.loads(path.read_text());checksum=saved.pop('brief_hash')
            if digest(saved)!=checksum or saved['identity']!=identity:raise ValueError('Research request changed; use a new ID')
            brief=ResearchBrief.model_validate({k:saved[k] for k in ('status','plan','issues')});interpreter=saved['interpreter_model']
        elif blocked:
            brief=ResearchBrief(status=blocked['status'],issues=blocked['issues']);interpreter=None
        else:
            from .structured_planning import StructuredPlanner,StructuredPlanningError
            if model is None:
                from .agent import configured_model
                model=configured_model(timeout=40,max_retries=0,max_tokens=6500,disable_thinking=True)
            interpreter=getattr(model,'model_name',type(model).__name__)
            planner=StructuredPlanner(model,ResearchBrief,root)
            messages=[dict(role='system',content=PROMPT),dict(role='user',content=request+(f'\nExact total buses: {node_count}.' if node_count is not None else ''))]
            try:
                try:brief=planner.invoke(messages)
                except StructuredPlanningError as exc:
                    messages.append(dict(role='user',content='Correct the schema without changing requirements. Previous candidate: '+json.dumps(planner.correction_payload(),ensure_ascii=False)+'\nError: '+str(exc)))
                    brief=planner.invoke(messages)
                issues=_source_issues(request,brief.plan,node_count) if brief.status=='ready' else []
                if issues:
                    planner.invalid('; '.join(issues))
                    messages.append(dict(role='user',content='Correct the compiled plan using the original request and these source-validation findings. Do not relax, discard or invent requirements. Omit response_lower/upper when using the default task protocol; they are filled by the compiler. Preserve one-sided numeric bounds. If requirements truly conflict, explain them. Previous candidate: '+json.dumps(brief.model_dump(exclude_unset=True),ensure_ascii=False)+'\nFindings: '+json.dumps(issues,ensure_ascii=False)))
                    brief=planner.invoke(messages)
                    issues=_source_issues(request,brief.plan,node_count) if brief.status=='ready' else []
                if issues:
                    planner.invalid('; '.join(issues))
                    brief=ResearchBrief(status='needs_clarification',issues=issues)
                else:planner.valid()
            except StructuredPlanningError as exc:
                output=dict(status='planning_failed',plan_type='research_task',interpreter_model=interpreter,issues=[str(exc)],verified_report='Research task compilation failed; no grid was generated.')
                atomic_json(root/'result.json',output);return output
        if not path.exists():
            saved=dict(identity=identity,status=brief.status,plan=brief.plan.model_dump(exclude_unset=True) if brief.plan else None,
                issues=brief.issues,request=request,plan_type='research_task',interpreter_model=interpreter)
            atomic_json(path,dict(saved,brief_hash=digest(saved)))
        output=dict(status=brief.status,plan_type='research_task',request=request,interpreter_model=interpreter,
            directory=str(root),plan=brief.plan.model_dump() if brief.plan else None)
        if brief.status!='ready':output.update(issues=brief.issues,verified_report='Research task not executed: '+'; '.join(brief.issues))
        else:
            from .research_tasks import compile_research_task,run_research_task
            try:
                compiled=compile_research_task(brief.plan)
            except ValueError as exc:
                output.update(status='needs_clarification',issues=[str(exc)],verified_report='Research task could not resolve its model/ports: '+str(exc))
            else:
                output['compiled_plan']=compiled
                if not execute:output.update(status='draft',verified_report='Research task draft compiled; physical search and task experiments have not run.')
                else:
                    result=run_research_task(brief.plan,workspace,design_id,model=model)
                    output.update(status=result['status'],verified_report=result['verified_report'],outcome={k:result[k] for k in ('status','directory','target_met','verification_passed','task','accepted_steps','stop_reason')})
        atomic_json(root/'result.json',output);return output
