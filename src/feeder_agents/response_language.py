"""Natural-language entry point for the explicitly scoped response workflow."""
import fcntl
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .artifacts import atomic_json, digest
from .response_schema import ResponseDesignPlan
from .schemas import StrictModel


class ResponseBrief(StrictModel):
    status: Literal['ready', 'needs_clarification', 'unsupported']
    plan: ResponseDesignPlan | None = None
    issues: list[str] = Field(default_factory=list, max_length=12)

    @model_validator(mode='after')
    def readiness(self):
        if self.status == 'ready' and (self.plan is None or self.issues):
            raise ValueError('Ready requires a complete plan and no unresolved issues')
        if self.status != 'ready' and not self.issues: raise ValueError('Explain unresolved requirements')
        return self


def is_response_request(request):
    for clause in re.split(r'[。；;\n]', request):
        if re.search('(?:\u4e0d\u9700\u8981|\u65e0\u9700|\u4e0d\u8981\u6c42|\u4e0d\u8981|without|do not need)\\s*(?:\u5206\u6790|\u8ba1\u7b97|\u8003\u8651|\u63a7\u5236)?\\s*(?:\u7535\u6c14\u54cd\u5e94|\u7075\u654f\u5ea6|\u654f\u611f\u5ea6|sensitivity|electrical response)', clause, re.I): continue
        if re.search('\u7535\u6c14\u54cd\u5e94|\u7075\u654f\u5ea6|\u7535\u538b\u654f\u611f|\u7535\u538b.{0,10}\u654f\u611f|\u529f\u7387\u8f6c\u79fb\u54cd\u5e94|sensitivity|electrical[ -]response|voltage[ -]sensitive', clause, re.I): return True
    return False


def response_capabilities():
    return dict(version='response_v3',
        distribution=['Single-voltage balanced or unbalanced voltage_p and voltage_q finite differences', 'OpenDSS export'],
        transmission=['Balanced AC transfer_p finite differences between explicit bus groups', 'MATPOWER export'],
        targets=['Absolute pu/MW, pu/MVAr or MW/MW intervals', 'Ratios to the original fixed baseline response'],
        actions=['Single or coordinated compatible whole-tuple conductor replacement', 'Parallel transmission circuit with fixed compensation fraction',
                 'Explicitly bounded uniform geometry scaling', 'Local subtree scaling', 'Bounded cross-cut branch reconnection',
                 'Total-P/Q-preserving demand redistribution', 'Joint preview of two permitted actions'],
        preservation=['Buses', 'Topology unless rewire_branch is permitted', 'Coordinates unless geometry changes are permitted',
                      'Total load P/Q and each PV injection', 'Load allocations unless redistribute_load is permitted',
                      'Phases', 'Nominal voltages', 'Source/control policy'],
        exclusions=['Multi-voltage distribution response design', 'Annual time series', 'Dynamics', 'N-1 certification', 'Arbitrary response-matrix realization'],
        qualitative_targets='Uncalibrated high/medium/low requests require a numerical or baseline-relative definition',
        schema=ResponseDesignPlan.model_json_schema())


PROMPT = '''Compile the complete user request into an electrical response design plan.
Preserve every explicit requirement, especially exact total bus count, voltage, loads, PV, scene and topology. Do not drop unsupported requests to produce a ready plan.
Return only necessary fields; do not fill optional fields with guessed defaults. For all_nodes and n_buses=N, omit n_loads_min/n_loads_max or set BOTH to N-1. Do not output the independent 20/30 defaults with an explicit bus count. Phase weights must be positive fractions summing to 1; the actual default is [0.5,0.3,0.2], never [1,1,1]. These are research assumptions, not calibrated typical population values. Omit equipment_design.reference_feeders unless requested; its full default is [J1,K1,Ckt5,Ckt24].
The response workflow owns candidate previews and edits. Set the unused base_spec.repair_policy.strategy to none; search.selector controls the actual LLM/heuristic response loop. Do not mention the old repair_policy.preview_limit as the response preview count; search.candidates_per_round controls that count.
Use distribution for single-voltage balanced/unbalanced feeders, transmission for balanced AC networks. Hierarchical MV-transformer-LV response is currently unsupported.
voltage_p and voltage_q measure bus-phase voltage magnitude pu per MW/MVAr of total positive injection. Empty injection_buses chooses the farthest load by source-path length and freezes it before search; empty monitor_buses uses the same buses. Explicit villages need explicit bus IDs; never invent village-to-bus mappings. Phase defaults use actual phases.
transfer_p measures signed from-terminal branch MW change per balanced MW transfer; explicit disjoint injection/withdrawal bus groups are required. monitor_branches are zero-based MATPOWER row indices. aggregation=mean_abs or max_abs.
Targets use nonnegative lower/upper bounds on aggregated absolute sensitivities. baseline_ratio means original frozen baseline, not a moving reference. Increase by 10%-30% becomes [1.1,1.3]. Changes in voltage itself are not sensitivity targets. Explain any default probe magnitude and assumptions. Vague high/medium/low requires clarification; do not fabricate calibrated bands.
New grid synthesis may use compatible conductor selection (replace_conductor) or parallel_line unless the user freezes equipment. Source settings, total demand P/Q and each PV injection remain fixed. Other fields stay fixed unless the user explicitly grants the following numerical permissions; never invent a grant to attain a target or change a conductor-only request.
scale_layout requires explicit max_layout_scale_change (75% is 0.75; software ceiling 0.90) and no fixed coordinates. scale_subtree requires max_branch_length_change (at most 0.90) and max_node_displacement_km. Combined global/local geometry uses the larger per-line percentage bound, not compounded percentages; the displacement limit applies to the total change. rewire_branch requires max_rewired_lines, max_rewire_length_ratio, max_node_degree; no ties or explicit topology family. redistribute_load requires max_load_redistribution_fraction (half-L1 kW change / original total, at most 0.5) and max_load_bus_change (relative change of each original load, at most 0.9); retain each local power factor and phase fractions. These three local actions require spatial_mst; redistribution requires heterogeneous loads. Fixed coordinates forbid both geometry actions. All numerical budgets refer to the ORIGINAL model, not the last round. A request preserving node loads or topology forbids the corresponding action.
Missing explicit local-action bounds require needs_clarification, not invented limits. max_joint_actions=2 allows joint previews of two already permitted moves; it grants no additional freedom. max_rounds is at most 32 and candidates_per_round at most 64. These are software/research settings, not engineering standards. Empty allowed_actions measures only. If the user explicitly requests an LLM repair agent choose selector=agent, otherwise heuristic previews are available and must not be described as LLM decisions.
For new 6/10/20 kV feeders use scenario.engineering_profile matching scenario.kind (urban/rural) so auto equipment uses the compatible reference catalogue; explain this selection. Do not change an explicitly requested legacy equipment policy.
If explicit constraints conflict return needs_clarification with the conflicting requirements. If a requested function is unsupported return unsupported. No claim of infeasibility from bounded search failure. Static network models are the product; probes are internal validation, not time series.
Distribution exports OpenDSS, transmission exports MATPOWER. count must be 1; ask for separate runs for ensembles. The base reference condition must be normal and electrically feasible. Keep the original research question in research_question. Use English artifact assumptions and clear explanations in the user's language.
'''


def _layout_permission(request):
    """Require a clear, numeric grant; ambiguous grants are clarified, not guessed."""
    if re.search(r'(?:do not|don[\x27’]t|never)\s+allow[^.;\n]{0,35}(?:spatial|layout|coordinate)',request,re.I):return None
    if re.search('\u56fa\u5b9a\u5750\u6807|\u4fdd\u6301\u5750\u6807|\u5750\u6807\u4e0d\u53d8|\u4e0d(?:\u5141\u8bb8|\u6539\u53d8|\u8c03\u6574).{0,8}(?:\u5750\u6807|\u7a7a\u95f4|\u5e03\u5c40)|fixed coordinates|preserve coordinates|do not.{0,10}(?:layout|coordinates)',request,re.I):
        return None
    patterns = (
        '\u5141\u8bb8\\s*(?:\u6574\u4f53|\u7edf\u4e00)\\s*(?:\u7a7a\u95f4|\u5e03\u5c40)\\s*(?:\u5c3a\u5ea6)?\\s*(?:\u7f29\u653e|\u8c03\u6574)[^\u3002\uff1b;\\n%\uff05]{0,20}?(\\d+(?:\\.\\d+)?)\\s*[%\uff05]',
        r'allow\s+(?:uniform\s+)?(?:layout|spatial)\s+scaling[^.;\n%]{0,24}?(\d+(?:\.\d+)?)\s*%',
    )
    values = [float(m.group(1))/100 for pattern in patterns for m in re.finditer(pattern,request,re.I)]
    return min(values) if values else None


def _local_permission_issues(request, search):
    def number(pattern, scale=1.):
        matches=re.findall(pattern,request,re.I)
        return min(map(float,matches))*scale if matches else None
    limits={}
    negative=lambda subject:bool(re.search('(?:\u4e0d\u5141\u8bb8|\u7981\u6b62|\u4e0d\u5f97|do not allow|don[\\x27\u2019]t allow|never allow|forbid)\\s*'+subject,request,re.I))
    if 'scale_subtree' in search.allowed_actions:
        limits['max_branch_length_change']=number('(?:\u5141\u8bb8\u5b50\u6811\u7f29\u653e|allow subtree scaling)[^\u3002\uff1b;\\n%\uff05]{0,24}?(\\d+(?:\\.\\d+)?)\\s*[%\uff05]',.01)
        limits['max_node_displacement_km']=number('(?:\u8282\u70b9\u4f4d\u79fb|node displacement)[^\u3002\uff1b;\\n]{0,12}?(\\d+(?:\\.\\d+)?)\\s*(?:km|\u5343\u7c73|\u516c\u91cc)')
        if negative('(?:\u5b50\u6811\u7f29\u653e|subtree scaling)') or re.search('\u56fa\u5b9a\u5750\u6807|\u4fdd\u6301\u5750\u6807|\u5750\u6807\u4e0d\u53d8|fixed coordinates|preserve coordinates',request,re.I):
            limits['max_branch_length_change']=None
    if 'rewire_branch' in search.allowed_actions:
        limits['max_rewired_lines']=number('(?:\u5141\u8bb8\u91cd\u63a5|allow reconnecting)[^\u3002\uff1b;\\n\\d]{0,12}(\\d+)\\s*(?:\u6761|branches)')
        limits['max_rewire_length_ratio']=number('(?:\u65b0\u7ebf\u957f|new line length)[^\u3002\uff1b;\\n\\d]{0,20}(\\d+(?:\\.\\d+)?)\\s*(?:\u500d|times)')
        limits['max_node_degree']=number('(?:\u6700\u5927\u5ea6\u6570|maximum degree)[^\u3002\uff1b;\\n\\d]{0,12}(\\d+)')
        if negative('(?:\u91cd\u63a5|reconnecting)') or re.search('\u62d3\u6251\u4e0d\u53d8|\u8fde\u63a5\u4e0d\u53d8|\u4e0d\u6539\u53d8\u62d3\u6251|fixed topology',request,re.I):limits['max_rewired_lines']=None
    if 'redistribute_load' in search.allowed_actions:
        limits['max_load_redistribution_fraction']=number('(?:\u5141\u8bb8\u8d1f\u8377\u91cd\u5206\u914d|allow load redistribution)[^\u3002\uff1b;\\n%\uff05]{0,30}?(\\d+(?:\\.\\d+)?)\\s*[%\uff05]',.01)
        limits['max_load_bus_change']=number('(?:\u5355\u8282\u70b9\u8d1f\u8377\u53d8\u5316|per-bus load change)[^\u3002\uff1b;\\n%\uff05]{0,20}?(\\d+(?:\\.\\d+)?)\\s*[%\uff05]',.01)
        if negative('(?:\u8d1f\u8377\u91cd\u5206\u914d|load redistribution)') or re.search('\u8d1f\u8377\u5206\u5e03\u4e0d\u53d8|\u5404\u8282\u70b9\u8d1f\u8377\u4e0d\u53d8|fixed nodal loads',request,re.I):limits['max_load_redistribution_fraction']=None
    return ['The input does not authorize this edit bound, or the plan exceeds it: '+field for field,bound in limits.items()
            if bound is None or getattr(search,field)>bound+1e-9]


def design_response_from_request(request, workspace, design_id, *, execute=True, model=None, node_count=None):
    if not request.strip() or len(request) > 12000: raise ValueError('Provide 1-12000 characters')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', design_id): raise ValueError('Invalid response design ID')
    if node_count is not None and (type(node_count) is not int or not 3 <= node_count <= 2001): raise ValueError('Invalid exact bus count')
    from .request_contract import request_guard
    from .source_review import exact_bus_counts
    blocked = request_guard(request)
    counts = exact_bus_counts(request)
    if node_count is not None: counts.add(node_count)
    if len(counts) > 1: blocked = dict(status='needs_clarification', issues=['Conflicting exact bus counts: '+str(sorted(counts))])
    root = Path(workspace).resolve()/'designs'/design_id; root.mkdir(parents=True, exist_ok=True)
    with (root/'.lock').open('w') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        identity = dict(request=request, node_count=node_count, compiler_version='response_v3', prompt_hash=digest(PROMPT))
        brief_path = root/'brief.json'
        if brief_path.exists():
            saved = json.loads(brief_path.read_text()); checksum = saved.pop('brief_hash')
            if digest(saved) != checksum or saved['identity'] != identity: raise ValueError('Response request changed; use a new ID')
            brief = ResponseBrief.model_validate({k: saved[k] for k in ('status', 'plan', 'issues')})
            interpreter = saved['interpreter_model']
        elif blocked:
            brief = ResponseBrief(status=blocked['status'], issues=blocked['issues']); interpreter = None
        else:
            from .structured_planning import StructuredPlanner, StructuredPlanningError
            if model is None:
                from .agent import configured_model
                model = configured_model(timeout=40, max_retries=0, max_tokens=6000, disable_thinking=True)
            interpreter = getattr(model, 'model_name', type(model).__name__)
            planner = StructuredPlanner(model, ResponseBrief, root)
            messages = [dict(role='system', content=PROMPT), dict(role='user', content=request+(
                f'\nExact total bus count supplied by user: {node_count}.' if node_count is not None else ''))]
            try:
                brief = planner.invoke(messages)
            except StructuredPlanningError as exc:
                messages.append(dict(role='user', content='Correct the schema error without changing user requirements. Previous candidate: '
                    +json.dumps(planner.correction_payload(),ensure_ascii=False)+'\nValidation error: '+str(exc)))
                try:
                    brief = planner.invoke(messages)
                except StructuredPlanningError as final_error:
                    output=dict(status='planning_failed',plan_type='electrical_response',request=request,
                        interpreter_model=interpreter,directory=str(root),issues=[str(final_error)],
                        verified_report='The model could not compile a valid electrical-response design plan. Both calls and validation records are saved; no grid was generated. This does not establish a contradiction in the user request.')
                    atomic_json(root/'result.json',output)
                    return output
            planner.valid()
            if brief.status == 'ready' and counts and brief.plan.base_spec.n_buses != next(iter(counts)):
                brief = ResponseBrief(status='needs_clarification', issues=['The compiled plan did not preserve the requested exact total bus count.'])
            if brief.status == 'ready' and 'scale_layout' in brief.plan.search.allowed_actions:
                bound = _layout_permission(request)
                if bound is None or brief.plan.search.max_layout_scale_change > bound+1e-10:
                    brief = ResponseBrief(status='needs_clarification', issues=['Uniform spatial scaling requires explicit user permission and a numerical upper bound; the input does not support the proposed coordinate edits.'])
            if brief.status=='ready':
                issues=_local_permission_issues(request,brief.plan.search)
                if issues:brief=ResponseBrief(status='needs_clarification',issues=issues)
        if not brief_path.exists():
            saved = dict(identity=identity, **brief.model_dump(), request=request,
                         plan_type='electrical_response', interpreter_model=interpreter)
            atomic_json(brief_path, dict(saved, brief_hash=digest(saved)))
        output = dict(status=brief.status, plan_type='electrical_response', request=request,
                      interpreter_model=interpreter, directory=str(root), **{'plan': brief.plan.model_dump() if brief.plan else None})
        if brief.status != 'ready':
            output.update(issues=brief.issues, verified_report='Design not executed: '+'; '.join(brief.issues))
        elif not execute:
            output.update(status='draft', verified_report='Electrical-response design drafted; power-flow measurements and target search have not run.')
        else:
            from .response_design import run_response_design
            result = run_response_design(brief.plan, workspace, design_id, model=model)
            output.update(status=result['status'], verified_report=result['verified_report'],
                outcome={k: result[k] for k in ('status', 'directory', 'target_met', 'verification_passed', 'protected_contract_preserved', 'winner', 'accepted_steps', 'stop_reason', 'diagnosis')})
        atomic_json(root/'result.json', output)
        return output
