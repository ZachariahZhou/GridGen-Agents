"""Natural-language entry point for the explicitly scoped response workflow."""
import fcntl
import json
import math
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
    return dict(version='response_v5',
        permission_revision=dict(tool='revise_electrical_response_permissions',
            tiers=['equipment','equipment_topology','equipment_geometry','geometry'],default='draft',
            required='Explicit user grant and numeric original-reference edit limits; targets, ports, baseline construction and other budgets remain fixed'),
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
        search=['Auto: bounded archive for multiple targets, monotone for one target',
                'Actual AC evaluation accounting and optional search-only cap',
                'Feasible alternative paths, immutable targets and final independent-step verification',
                'AC-calibrated incremental distribution-response proposals'],
        schema=ResponseDesignPlan.model_json_schema())


PROMPT = '''Compile the complete user request into an electrical response design plan.
Preserve every explicit requirement, especially exact total bus count, voltage, loads, PV, scene and topology. Do not drop unsupported requests to produce a ready plan.
Return only necessary fields; do not fill optional fields with guessed defaults. For all_nodes and n_buses=N, omit n_loads_min/n_loads_max or set BOTH to N-1. Do not output the independent 20/30 defaults with an explicit bus count. Phase weights must be positive fractions summing to 1; the actual default is [0.5,0.3,0.2], never [1,1,1]. These are research assumptions, not calibrated typical population values. Omit equipment_design.reference_feeders unless requested; its full default is [J1,K1,Ckt5,Ckt24].
The response workflow owns candidate previews and edits. Set the unused base_spec.repair_policy.strategy to none; search.selector controls the actual LLM/heuristic response loop. Do not mention the old repair_policy.preview_limit as the response preview count; search.candidates_per_round controls that count.
search.strategy defaults to auto: bounded archive exploration for multiple targets, monotone for one. Archive exploration may temporarily trade response deficits but never relaxes engineering constraints, edit permissions or final targets. max_search_ac_calls, if explicitly requested, limits actual search AC evaluations; construction, reference, export and final verification are separately counted. Otherwise omit it. Do not claim that a candidate equals one AC call.
Use distribution for single-voltage balanced/unbalanced feeders, transmission for balanced AC networks. Hierarchical MV-transformer-LV response is currently unsupported.
voltage_p and voltage_q measure bus-phase voltage magnitude pu per MW/MVAr of total positive injection. Empty injection_buses chooses the farthest load by source-path length and freezes it before search; empty monitor_buses uses the same buses. Explicit villages need explicit bus IDs; never invent village-to-bus mappings. Phase defaults use actual phases.
transfer_p measures signed from-terminal branch MW change per balanced MW transfer; explicit disjoint injection/withdrawal bus groups are required. monitor_branches are zero-based MATPOWER row indices. aggregation=mean_abs or max_abs.
Targets use nonnegative lower/upper bounds on aggregated absolute sensitivities. baseline_ratio means original frozen baseline, not a moving reference. Increase by 10%-30% becomes [1.1,1.3]. Changes in voltage itself are not sensitivity targets. Explain any default probe magnitude and assumptions. Vague high/medium/low requires clarification; do not fabricate calibrated bands.
search.max_changed_conductors, when supplied with replace_conductor, caps final changed conductor assignments against the original grid across all rounds; preserve an explicit user cap. Permission revisions use a new plan and run ID, never a changed reference denominator.
New grid synthesis may use compatible conductor selection (replace_conductor) or parallel_line unless the user freezes equipment. Source settings, total demand P/Q and each PV injection remain fixed. Other fields stay fixed unless the user explicitly grants the following numerical permissions; never invent a grant to attain a target or change a conductor-only request.
scale_layout requires explicit max_layout_scale_change (75% is 0.75; software ceiling 0.90) and no fixed coordinates. scale_subtree requires max_branch_length_change (at most 0.90) and max_node_displacement_km. Combined global/local geometry uses the larger per-line percentage bound, not compounded percentages; the displacement limit applies to the total change. rewire_branch requires max_rewired_lines, max_rewire_length_ratio, max_node_degree; no ties or explicit topology family. redistribute_load requires max_load_redistribution_fraction (half-L1 kW change / original total, at most 0.5) and max_load_bus_change (relative change of each original load, at most 0.9); retain each local power factor and phase fractions. These three local actions require spatial_mst; redistribution requires heterogeneous loads. Fixed coordinates forbid both geometry actions. All numerical budgets refer to the ORIGINAL model, not the last round. A request preserving node loads or topology forbids the corresponding action.
Missing explicit local-action bounds require needs_clarification, not invented limits. max_joint_actions=2 allows joint previews of two already permitted moves; it grants no additional freedom. max_rounds is at most 32 and candidates_per_round at most 64. These are software/research settings, not engineering standards. Empty allowed_actions measures only. If the user explicitly requests an LLM repair agent choose selector=agent, otherwise heuristic previews are available and must not be described as LLM decisions.
For new 6/10/20 kV feeders use scenario.engineering_profile matching scenario.kind (urban/rural) so auto equipment uses the compatible reference catalogue; explain this selection. Do not change an explicitly requested legacy equipment policy.
If explicit constraints conflict return needs_clarification with the conflicting requirements. If a requested function is unsupported return unsupported. No claim of infeasibility from bounded search failure. Static network models are the product; probes are internal validation, not time series.
Distribution exports OpenDSS, transmission exports MATPOWER. count must be 1; ask for separate runs for ensembles. The base reference condition must be normal and electrically feasible. Keep the original research question in research_question. Use English artifact assumptions and clear explanations in the user's language.
'''


def _layout_permission(request):
    """Require a clear, numeric grant; ambiguous grants are clarified, not guessed."""
    if re.search(r'(?:do not|don[\x27’]t|cannot|can[\x27’]t|never)\s+(?:allow|authorize)[^.;\n]{0,35}(?:spatial|layout|coordinate|geometry)',request,re.I):return None
    if re.search('\u56fa\u5b9a\u5750\u6807|\u4fdd\u6301\u5750\u6807|\u5750\u6807\u4e0d\u53d8|\u4e0d(?:\u5141\u8bb8|\u6539\u53d8|\u8c03\u6574).{0,8}(?:\u5750\u6807|\u7a7a\u95f4|\u5e03\u5c40|\u51e0\u4f55)|fixed coordinates|preserve coordinates|do not.{0,10}(?:layout|coordinates)',request,re.I):
        return None
    patterns = (
        '\u5141\u8bb8\\s*(?:\u6574\u4f53|\u7edf\u4e00)\\s*(?:\u7a7a\u95f4|\u5e03\u5c40|\u51e0\u4f55)\\s*(?:\u5c3a\u5ea6)?\\s*(?:\u7f29\u653e|\u8c03\u6574)[^\u3002\uff1b;\\n%\uff05]{0,20}?(\\d+(?:\\.\\d+)?)\\s*[%\uff05]',
        r'(?:allow|authorize)\s+(?:uniform\s+)?(?:layout|spatial|geometry)\s+scaling[^.;\n%]{0,24}?(\d+(?:\.\d+)?)\s*%',
    )
    values = [float(m.group(1))/100 for pattern in patterns for m in re.finditer(pattern,request,re.I)]
    # A single affirmative grant may enumerate equipment and geometry actions.
    # Keep that authority within its source sentence, not arbitrary later prose.
    for clause in _affirmative_permission_clauses(request):
        values.extend(float(m[1])/100 for m in re.finditer(
            r'(?:uniform\s+)?(?:layout|spatial|geometry)\s+scaling[^.;\n%]{0,24}?(\d+(?:\.\d+)?)\s*%',clause,re.I))
    return min(values) if values else None


def _affirmative_permission_clauses(request):
    from .research_source import source_sentences
    for clause in source_sentences(request):
        if not re.search('\\b(?:allow(?:ed)?|authorize(?:d)?)\\b|\u5141\u8bb8',clause,re.I):continue
        if re.search('(?:do not|don[\\x27\u2019]t|cannot|can[\\x27\u2019]t|never)\\s+(?:allow|authorize)|\\b(?:not|never)\\s+(?:allowed|authorized)\\b|\u4e0d\u5141\u8bb8|\u7981\u6b62|\u4e0d\u5f97',clause,re.I):continue
        yield clause


def _exclusive_layout_permission(request):
    """Return an explicit uniform-only grant, independently of construction repair."""
    bound=_layout_permission(request)
    if bound is None:return None
    for clause in _affirmative_permission_clauses(request):
        if re.search('(?:\u4ec5|\u53ea)\u5141\u8bb8\\s*(?:\u6574\u4f53|\u7edf\u4e00)\\s*(?:\u7a7a\u95f4|\u5e03\u5c40|\u51e0\u4f55)\\s*(?:\u5c3a\u5ea6)?\\s*(?:\u7f29\u653e|\u8c03\u6574)'
                     r'|\bonly\s+(?:(?:allow|authorize)\s+)?(?:uniform\s+)?(?:layout|spatial|geometry)\s+scaling',clause,re.I):
            return bound,clause.strip()
    return None


def _conductor_limits(request):
    words=dict(zip(('zero','one','two','three','four','five','six','seven','eight','nine','ten'),range(11)))
    number=r'(?:\d+|'+'|'.join(words)+r')'
    pattern=r'(?:up to|at most|no more than)\s+('+number+')\\s+(?:line\\s+)?conductors?(?:\\s+replacements)?|(?:\u6700\u591a\u66f4\u6362|\u5141\u8bb8\u66f4\u6362\u6700\u591a)\\s*(\\d+)\\s*[\u6761\u6839](?:\u7ebf\u8def)?\u5bfc\u7ebf'
    limits=[]
    for match in re.finditer(pattern,request,re.I):
        value=(match[1] or match[2]).lower()
        limits.append(int(value) if value.isdigit() else words[value])
    return limits


def protected_permission_issues(request, search):
    """Never let a compiled action override an explicit source prohibition."""
    subjects={
        'geometry':'geometry|coordinates?|node positions|\u7a7a\u95f4|\u5750\u6807|\u5e03\u5c40',
        'equipment':'equipment|conductors?|\u8bbe\u5907|\u5bfc\u7ebf',
        'topology':'topology|connections?|\u62d3\u6251|\u8fde\u63a5',
        'loads':'(?:nodal )?loads?|demand|\u8d1f\u8377',
    }
    actions={'geometry':{'scale_layout','scale_subtree'},'equipment':{'replace_conductor','parallel_line'},
             'topology':{'rewire_branch'},'loads':{'redistribute_load'}}
    issues=[]
    from .research_source import source_sentences
    for kind,subject in subjects.items():
        frozen=any(re.search('(?:preserve|fixed|unchanged|do not change|do not move|do not replace|no changes? to|\u4e0d\u6539\u53d8|\u56fa\u5b9a|\u4fdd\u6301)[^.;\\n]{0,60}(?:'+subject+r')',clause,re.I)
                   or re.search(r'(?:'+subject+')[^.;\\n]{0,30}(?:forbidden|unchanged|\u4e0d\u53d8)',clause,re.I)
                   for clause in source_sentences(request))
        if kind=='equipment':frozen=frozen or bool(re.search(r'no conductor replacement|do not[^.;\n]{0,55}replace (?:any )?conductors?',request,re.I))
        if frozen and set(search.allowed_actions)&actions[kind]:issues.append('Input freezes '+kind+' but the compiled search permits changes.')
    return issues


def response_source_issues(request, plan, node_count=None):
    """Validate a response plan against the immutable user source."""
    from .research_source import construction_source_issues,response_constraints,response_interval_issues
    from .source_review import exact_bus_counts
    issues=construction_source_issues(request,plan.base_spec)+response_interval_issues(request)
    counts=exact_bus_counts(request)
    if node_count is not None:counts.add(node_count)
    if len(counts)>1 or (counts and plan.base_spec.n_buses!=next(iter(counts))):
        issues.append('Compiled plan does not preserve the exact bus count.')
    mentions=response_constraints(request)
    if mentions:
        references={ref for ref,_,_ in mentions}
        lower=[v for _,op,v in mentions if op in ('eq','ge')]
        upper=[v for _,op,v in mentions if op in ('eq','le')]
        lo=max(lower) if lower else None;hi=min(upper) if upper else None
        def same(a,b):return a is b if a is None or b is None else math.isclose(a,b,rel_tol=1e-8,abs_tol=1e-10)
        if len(plan.probes)==1 and any(references!={t.reference} or not same(t.lower,lo) or not same(t.upper,hi) for t in plan.targets):
            issues.append('Explicit response interval differs from the compiled response plan.')
    issues.extend(protected_permission_issues(request,plan.search))
    issues.extend(_local_permission_issues(request,plan.search))
    if 'scale_layout' in plan.search.allowed_actions:
        bound=_layout_permission(request)
        if bound is None or plan.search.max_layout_scale_change>bound+1e-9:
            issues.append('Uniform geometry scaling requires an explicit numerical permission within the source limit.')
    return issues


def _local_permission_issues(request, search):
    def number(pattern, scale=1.):
        matches=re.findall(pattern,request,re.I)
        return min(map(float,matches))*scale if matches else None
    limits={}
    equipment_limits=_conductor_limits(request)
    if 'replace_conductor' in search.allowed_actions:
        if equipment_limits:
            cap=min(equipment_limits)
            if search.max_changed_conductors is None or search.max_changed_conductors>cap:
                return ['The conductor-change cap is missing or exceeds the explicitly requested limit.']
        elif any(re.search(r'(?:conductors?|conductor replacements|\u5bfc\u7ebf)',clause,re.I) and re.search(r'(?:at most|up to|no more than|maximum(?: conductor)? (?:changes|replacements)|conductor(?: changes| replacements)? limit|\u6700\u591a|\u4e0d\u8d85\u8fc7|\u4e0a\u9650)',clause,re.I) for clause in re.split(r'[.;\n\u3002\uff1b]',request)):
            return ['An explicit conductor limit could not be verified; use an unambiguous numeric conductor-change cap.']
    negative=lambda subject:bool(re.search('(?:\u4e0d\u5141\u8bb8|\u7981\u6b62|\u4e0d\u5f97|do not allow|don[\\x27\u2019]t allow|never allow|forbid)\\s*'+subject,request,re.I))
    if 'scale_subtree' in search.allowed_actions:
        limits['max_branch_length_change']=number('(?:\u5141\u8bb8\u5b50\u6811\u7f29\u653e|allow subtree scaling)[^\u3002\uff1b;\\n%\uff05]{0,24}?(\\d+(?:\\.\\d+)?)\\s*[%\uff05]',.01)
        branch_bounds=[float(m[1])/100 for clause in _affirmative_permission_clauses(request)
            for m in re.finditer(r'per[- ]branch length change[^。；;\n%％]{0,24}?(\d+(?:\.\d+)?)\s*[%％]',clause,re.I)]
        if branch_bounds:
            if limits['max_branch_length_change'] is not None:branch_bounds.append(limits['max_branch_length_change'])
            limits['max_branch_length_change']=min(branch_bounds)
        limits['max_node_displacement_km']=number('(?:\u8282\u70b9\u4f4d\u79fb|node displacement)[^\u3002\uff1b;\\n]{0,12}?(\\d+(?:\\.\\d+)?)\\s*(?:km|\u5343\u7c73|\u516c\u91cc)')
        if negative('(?:\u5b50\u6811\u7f29\u653e|subtree scaling)') or re.search('\u56fa\u5b9a\u5750\u6807|\u4fdd\u6301\u5750\u6807|\u5750\u6807\u4e0d\u53d8|fixed coordinates|preserve coordinates',request,re.I):
            limits['max_branch_length_change']=None
    if 'rewire_branch' in search.allowed_actions:
        limits['max_rewired_lines']=number('(?:\u5141\u8bb8\u91cd\u63a5|allow reconnecting)[^\u3002\uff1b;\\n\\d]{0,12}(\\d+)\\s*(?:\u6761|branches)')
        limits['max_rewire_length_ratio']=number('(?:\u65b0\u7ebf\u957f|new line length)[^\u3002\uff1b;\\n\\d]{0,20}(\\d+(?:\\.\\d+)?)\\s*(?:\u500d|times)')
        limits['max_node_degree']=number('(?:\u6700\u5927\u5ea6\u6570|maximum degree)[^\u3002\uff1b;\\n\\d]{0,12}(\\d+)')
        words=dict(zip(('zero','one','two','three','four','five','six','seven','eight','nine','ten'),range(11)))
        numeric=r'(\d+(?:\.\d+)?|'+ '|'.join(words)+r')\b'
        patterns={
            'max_rewired_lines':r'(?:at most|up to|no more than)\s+'+numeric+r'\s+(?:branch\s+)?(?:reconnections?|rewires?)',
            'max_rewire_length_ratio':r'(?:new/old(?:\s+(?:rewire|line))?\s+length ratio|rewire length ratio)\s+(?:at most|up to|no more than)\s+'+numeric,
            'max_node_degree':r'maximum\s+(?:node\s+)?degree\s+'+numeric,
        }
        for field,pattern in patterns.items():
            values=[]
            for clause in _affirmative_permission_clauses(request):
                for match in re.finditer(pattern,clause,re.I):
                    token=match[1].lower()
                    values.append(float(words[token]) if token in words else float(token))
            if values:
                if limits[field] is not None:values.append(limits[field])
                limits[field]=min(values)
        if negative('(?:\u91cd\u63a5|reconnecting)') or re.search('\u62d3\u6251\u4e0d\u53d8|\u8fde\u63a5\u4e0d\u53d8|\u4e0d\u6539\u53d8\u62d3\u6251|fixed topology',request,re.I):limits['max_rewired_lines']=None
        if re.search(r'\bno\s+(?:branch\s+)?reconnections?\b|\breconnections?\b[^.;\n]{0,24}\bforbidden\b',request,re.I):
            limits['max_rewired_lines']=None
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
    from .research_language import is_research_request,design_research_from_request
    if is_research_request(request):
        return design_research_from_request(request,workspace,design_id,execute=execute,model=model,node_count=node_count)
    from .request_contract import request_guard
    from .research_source import response_interval_issues
    from .source_review import exact_bus_counts
    blocked = request_guard(request)
    if issues:=response_interval_issues(request):blocked=dict(status='needs_clarification',issues=issues)
    counts = exact_bus_counts(request)
    if node_count is not None: counts.add(node_count)
    if len(counts) > 1: blocked = dict(status='needs_clarification', issues=['Conflicting exact bus counts: '+str(sorted(counts))])
    root = Path(workspace).resolve()/'designs'/design_id; root.mkdir(parents=True, exist_ok=True)
    with (root/'.lock').open('w') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        identity = dict(request=request, node_count=node_count, compiler_version='response_v4.1', prompt_hash=digest(PROMPT))
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
            for attempt in range(2):
                try:
                    brief = planner.invoke(messages)
                    issues=response_source_issues(request,brief.plan,node_count) if brief.status=='ready' else []
                    if not issues:
                        planner.valid()
                        break
                    planner.invalid('; '.join(issues))
                except StructuredPlanningError as exc:
                    issues=[str(exc)]
                if attempt==1:
                    output=dict(status='planning_failed',plan_type='electrical_response',request=request,
                        interpreter_model=interpreter,directory=str(root),plan=None,
                        failure_origin='compiler',issues=issues,
                        verified_report='Response compilation failed after two recorded attempts. '
                        'No model was generated. Candidate schema or source violations are not '
                        'proof of contradictory or infeasible user requirements.')
                    atomic_json(root/'result.json',output)
                    return output
                messages.append(dict(role='user',content=json.dumps(dict(original_request=request,
                    exact_total_buses=node_count,previous_candidate=planner.candidate_payload(),
                    validation_findings=issues,instruction='Correct this candidate using the original '
                    'request. Preserve its quantities, response bounds, reference and prohibitions. '
                    'Remove ungranted actions; do not request expanded authority merely to retain '
                    'your candidate. Return a complete ResponseBrief. A compiler mistake is not '
                    'a conflict in the user request.'),ensure_ascii=False)))
        if not brief_path.exists():
            saved = dict(identity=identity, **brief.model_dump(), request=request,
                         plan_type='electrical_response', interpreter_model=interpreter)
            atomic_json(brief_path, dict(saved, brief_hash=digest(saved)))
        output = dict(status=brief.status, plan_type='electrical_response', request=request,
                      interpreter_model=interpreter, directory=str(root), **{'plan': brief.plan.model_dump() if brief.plan else None})
        if brief.status != 'ready':
            output.update(issues=brief.issues, verified_report='Design not executed: '+'；'.join(brief.issues))
        elif not execute:
            output.update(status='draft', verified_report='Electrical-response design drafted; power-flow measurements and target search have not run.')
        else:
            from .response_design import run_response_design
            result = run_response_design(brief.plan, workspace, design_id, model=model)
            output.update(status=result['status'], verified_report=result['verified_report'],
                outcome={k: result[k] for k in ('status', 'directory', 'target_met', 'verification_passed', 'protected_contract_preserved', 'winner', 'accepted_steps', 'stop_reason', 'diagnosis')})
        atomic_json(root/'result.json', output)
        return output
