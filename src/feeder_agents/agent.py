import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain.tools import tool
from langgraph.checkpoint.sqlite import SqliteSaver

from .memory import MemoryStore, ExperienceStore, experience_context, experience_signature
from .rules import load_rules, search_sources, data
from .schemas import ExperimentPlan, ExperimentSpec, StudyPlan
from .studies import build_study_plan, run_study
from .planning import build_plan
from .artifacts import atomic_json, digest
from .workflow import run_experiment, read_verified_experiment, _runtime_fingerprint
from .knowledge import DocumentStore, extract_rules
from .response_permissions import ResponsePermissionGrant


from .agent_prompt import SYSTEM_PROMPT

def configured_model(*, timeout=90, max_retries=2, max_tokens=3000, disable_thinking=False):
    from dotenv import dotenv_values
    from langchain_openai import ChatOpenAI
    # Read on each invocation so edits take effect in the running UI. Explicit
    # process environment wins; do not persist dotenv values into os.environ.
    settings = dotenv_values(Path.cwd() / '.env')
    def setting(name):
        return os.environ.get(name, settings.get(name))

    model = setting('FEEDER_MODEL')
    if not model:
        raise ValueError('Set FEEDER_MODEL and OPENAI_API_KEY (optional OPENAI_BASE_URL) for the live Agent; structured generation needs no key.')
    return ChatOpenAI(model=model, api_key=setting('OPENAI_API_KEY'),
                      base_url=setting('OPENAI_BASE_URL'), temperature=0, timeout=timeout, max_retries=max_retries,
                      max_tokens=max_tokens,
                      extra_body={'enable_thinking': False} if disable_thinking and model.lower().startswith('qwen') else None)


def make_tools(project_root: Path, project_id: str, workers: int = 1):
    memory = MemoryStore(project_root / 'memory.sqlite')
    documents = DocumentStore(project_root)

    @tool
    def describe_research_tasks() -> dict:
        """Inspect executable voltage-control, static-PV-impact and transmission-transfer task contracts and disclosed defaults."""
        from .research_language import research_task_capabilities
        return research_task_capabilities()

    @tool
    def design_research_grid(request: str,design_id: str,execute: bool=True) -> dict:
        """Generate a grid for a supported research purpose, with response search and finite task experiments. Pass the entire user request."""
        from .design import design_from_request
        from .agent_results import design_output
        return design_output(project_root, design_from_request(request,project_root,design_id,
            execute=execute, normalize_requirements=True, planning_mode='adaptive'))

    @tool
    def describe_electrical_response_capabilities() -> dict:
        """Inspect voltage/transfer response targets, fixed-model probe protocols and permitted synthesis actions."""
        from .response_language import response_capabilities
        return response_capabilities()

    @tool
    def design_electrical_response(request: str, design_id: str, execute: bool = True) -> dict:
        """Generate a research grid with specified voltage sensitivity or AC transfer response; retain complete user requirements."""
        from .design import design_from_request
        from .agent_results import design_output
        return design_output(project_root, design_from_request(request, project_root, design_id,
            execute=execute, normalize_requirements=True, planning_mode='adaptive'))

    @tool
    def revise_electrical_response_permissions(permissions: ResponsePermissionGrant, design_id: str, plan: dict | None = None,
            execute: bool = False, followup_permissions: list[ResponsePermissionGrant] | None = None, parent_id: str | None = None) -> dict:
        """Draft or execute a separately authorized response plan with broader equipment/topology permissions.

        Use only after the user allows the added actions. permissions requires
        tier=geometry, equipment, equipment_topology or equipment_geometry. Prefer parent_id
        from the previous tool result; its exact saved plan is loaded automatically. Geometry
        alone must use max_changed_conductors=0 and max_layout_scale_change. The
        topology tier also requires max_rewired_lines, max_rewire_length_ratio
        and max_node_degree. Uniform geometry needs max_layout_scale_change;
        optional subtree edits require both max_branch_length_change and
        max_node_displacement_km. Optional followup_permissions
        are preauthorized stages tried only after failure; stop at first verified success
        and report all stage work. Retain the complete original plan. Explain limits
        before execution; defaults to draft with zero solver calls. Use a new
        design_id for execution and never change original targets or baselines.
        """
        from .response_permission_workflow import prepare_permission_ladder,run_permission_ladder
        from .agent_results import response_parent
        task_context = None
        if parent_id:
            saved_plan, parent_result = response_parent(project_root, parent_id)
            task_context = parent_result.get('compiled_plan', {}).get('context')
            if task_context is None:
                manifest_path = Path(parent_result['directory'])/'manifest.json'
                task_context = json.loads(manifest_path.read_text()).get('task_context')
            if plan is not None and plan != saved_plan:
                raise ValueError('Supplied plan differs from the verified parent; pass parent_id alone')
            plan = saved_plan
        if plan is None:
            raise ValueError('Provide parent_id for a verified prior response, or its complete unchanged plan')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',design_id):raise ValueError('Invalid response design ID')
        grants=[grant.model_dump(exclude_none=True) for grant in [permissions]+(followup_permissions or [])]
        if execute:
            output = run_permission_ladder(plan,grants,project_root,design_id,task_context=task_context)
            winner = output.get('result')
            if winner:
                from .agent_results import design_output
                ready = design_output(project_root, dict(status=output['status'],
                    outcome={'directory': winner['directory']}, verified_report=winner['verified_report']))
                return ready | {key: output[key] for key in ('selected_stage','solver_accounting','directory','target_met')}
            return {key:value for key,value in output.items() if key not in {'authorized_stages','result'}}
        return prepare_permission_ladder(plan,grants,task_context=task_context)

    @tool
    def describe_hierarchy_capabilities() -> dict:
        """Inspect MV-transformer-LV-customer generation scope and sourced equipment. Use design_from_language with complete request for natural-language hierarchical designs."""
        from .hierarchy import hierarchy_capabilities
        return hierarchy_capabilities()

    @tool
    def read_hierarchical_result(experiment_id: str) -> dict:
        """Read a saved MV-transformer-LV-customer experiment after hash and acceptance verification; distinct from single-voltage experiment storage."""
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',experiment_id):
            raise ValueError('Invalid hierarchy experiment ID')
        from .hierarchy_workflow import read_hierarchy_result
        result=read_hierarchy_result(project_root/'hierarchical_experiments'/experiment_id)
        return {k:v for k,v in result.items() if k not in ('artifacts','samples')} | {'samples':result['samples'][:10]}

    @tool
    def read_hierarchical_inverse_result(design_id: str) -> dict:
        """Verify a saved multi-voltage inverse design, frozen-condition checks and local edit trace. Not a single-voltage experiment."""
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',design_id):raise ValueError('Invalid design ID')
        from .hierarchy_inverse import read_hierarchy_inverse
        result=read_hierarchy_inverse(project_root/'hierarchical_inverse_designs'/design_id)
        return {k:result[k] for k in ('status','target_met','directory','accepted_steps','evaluated_candidates','verified_report')}|{
            'conditions':[{k:c[k] for k in ('name','valid','target_met','paired_verified','metrics') if k in c} for c in result['winner']['conditions']],
            'trace':result['trace']}

    @tool
    def describe_taxonomy_references(family: str = 'all', construction: str = 'all', limit: int = 12) -> dict:
        """Inspect PNNL/PG&E representative GLM feeder style priors. family: all/pnnl_taxonomy/pge_prototypes; construction: all/overhead/underground (majority by line count, not urban/rural labels). Static lengths/phases/topology and inventory; NOT runnable DSS or selectable equipment."""
        from .taxonomy import describe_taxonomy_references as describe
        return describe(family, construction, limit)

    @tool
    def describe_equipment_profiles() -> dict:
        """Inspect sourced joint equipment distributions, phase/construction coverage, current-rating ranges and transfer boundaries before selecting reference feeders or sizing policy."""
        from .equipment import describe_equipment_profiles as describe
        return describe()

    @tool
    def describe_topology_styles() -> dict:
        """List executable research topology families, scoped parameters, load shapes, normally-open tie operation and unsupported combinations. Templates are hypotheses, not construction or statistical certification."""
        return data('topology_styles.json')

    @tool
    def list_reference_feeders(voltage_kv: float | None = None, min_buses: int = 1,
                               max_buses: int = 10000, evidence_class: str = 'any') -> dict:
        """Find packaged MATPOWER distribution references by native kV/bus count and provenance (any,benchmark,actual_derived). Return honest statistics and export blockers, not generic real-feeder claims."""
        from .references import list_references
        cases=list_references(voltage_kv,min_buses,max_buses,evidence_class)
        return {'cases':cases,'count':len(cases),'notice':'Separate literature benchmarks from actual-derived models; missing coordinates/lengths/ratings remain unknown.'}

    @tool
    def generate_reference_case(case_id: str, run_id: str, load_scale: float = 1.0) -> dict:
        """Run a selected MATPOWER reference with optional 0.05–5 load multiplier. Preserve all buses, native voltage, impedance and branch states. Returns actual DSS validation and source/model/diagram archive. No geometry reconstruction or automatic PV insertion."""
        from .references import run_reference_case
        result=run_reference_case(case_id,project_root,run_id,load_scale)
        output={k:v for k,v in result.items() if k!='artifacts'}
        output['statistics']={k:v for k,v in result['statistics'].items() if k not in {'branches','positive_load_weights','offspring_counts','joint_features'}}
        return output

    @tool
    def describe_transmission_capabilities() -> dict:
        """Inspect transmission voltage layers, physical model scope, repair permissions, targets and exclusions before planning."""
        from .transmission import transmission_capabilities
        return transmission_capabilities()

    @tool
    def revise_existing_transmission(parent_id: str, revision_id: str, feedback: str, sample_index: int=0,
                                     targets: list[dict] | None=None, allowed_repairs: list[str] | None=None,
                                     allow_topology_changes: bool=False) -> dict:
        """Repair/revise an existing transmission sample in a NEW run. Preserve loads, coordinates, voltage layers and generator identities. targets are metric/operator/threshold constraints; allowed_repairs restricts tools. Pass ALL verbatim feedback. Only enable topology changes when explicitly requested. Changes to loads, node counts or voltage layers require a new design, not this revision tool."""
        from .transmission import revise_transmission
        result=revise_transmission(project_root,parent_id,revision_id,feedback,sample_index,targets,allowed_repairs,allow_topology_changes)
        return {k:result[k] for k in ('directory','attempted','accepted','failed_or_unaccepted','verified_report')}

    @tool
    def read_transmission_result(run_id: str) -> dict:
        """Read hash-verified transmission artifacts and actual AC acceptance in this project."""
        from .transmission import read_transmission_result as read
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',run_id):raise ValueError('Invalid run ID')
        result=read(project_root/'transmission_experiments'/run_id)
        return {k:result[k] for k in ('directory','accepted','attempted','verified_report')}

    @tool
    def design_from_language(request: str, design_id: str, execute: bool = True, node_count: int | None = None) -> dict:
        """Design feeders directly from a complete natural-language request. Preserves user evidence, exposes assumptions, blocks unsupported/ambiguous requirements, then executes feeder or paired-study plans. execute=false creates a draft. node_count is optional total buses including one source; omit when unspecified. Pass all current requirements, never silently omit unsupported ones."""
        from .design import design_from_request
        try:
            result=design_from_request(request,project_root,design_id,execute,workers,node_count=node_count,normalize_requirements=True,planning_mode='adaptive')
            from .agent_results import design_output
            return design_output(project_root, result)
        except ValueError as exc:
            return {'status':'invalid_design','error':str(exc)}

    @tool
    def inspect_planning_memory() -> dict:
        """Inspect this project's automatically recorded diagnostic experience. Planning and electrical verification are distinct; these observations never override current requirements. Similar experiences are retrieved automatically only when a planning failure occurs."""
        from .planning_memory import PlanningMemory
        path=project_root/'planning_experiences.sqlite'
        return dict(exists=path.exists(),statistics=PlanningMemory(path,mode='read_only').stats() if path.exists() else {},
            meaning='Persisted historical counts; invalid, altered or incompatible evidence is excluded from actual retrieval.')

    @tool
    def revise_existing_case(parent_id: str, revision_id: str, feedback: str,
                             sample_index: int = 0, execute: bool = True) -> dict:
        """Revise a verified existing research feeder using complete verbatim engineer feedback and a NEW revision ID. Preserve parent and voltage. Local actions preserve graph; explicit layout redesign can change graph/geometry/equipment while preserving node roles and aggregate load/PV. Supports spatial MST/rural villages/structured radial families, load shapes, normally-open ties, morphology checks, and original local scaling/PV/conductor actions. execute=false saves a draft; same ID/feedback can then execute. Pass all preservation requirements verbatim."""
        from .revisions import revise_case
        result=revise_case(project_root,parent_id,revision_id,sample_index,feedback=feedback,execute=execute)
        from .agent_results import design_output
        return design_output(project_root, result) | {k:result[k] for k in
            ('parent','plan','diff','operation','style_target_checks','style_targets_met') if k in result}

    @tool
    def import_design_document(title: str, text: str, source_url: str = '') -> dict:
        """Import user-provided plain text/Markdown into project knowledge; preserve immutable source text and line chunks. No PDF/OCR or URL fetching."""
        return documents.import_text(title,text,source_url)

    @tool
    def search_project_documents(query: str = '') -> dict:
        """Search imported document chunks lexically; use document_id and chunk_index for extraction. Snippets are not proof of whole-document coverage."""
        matches=documents.search(query)
        return {'matches':[{**m,'text':m['text'][:1200]} for m in matches], 'limit':8}

    @tool
    def extract_document_rules(document_id: str, chunk_index: int = 0) -> dict:
        """Use configured LLM to extract numeric limits from one document chunk with exact quotes; unsupported conditions remain explicit. Returns candidate rule IDs for plan_experiment, not certified standards."""
        try:
            result=extract_rules(documents,document_id,chunk_index)
            result['rules']=[documents.get_rule(rule_id) for rule_id in result['rule_ids']]
            return result
        except Exception as exc:
            return {'status':'extraction_failed','error_type':type(exc).__name__,
                    'notice':'No automatic fallback or rule activation; verify document/chunk and model configuration.'}

    @tool
    def list_project_rules(offset: int = 0, limit: int = 32) -> dict:
        """Browse already extracted document rules and exact evidence; reuse IDs without another model extraction. Returns pagination information."""
        return documents.list_rules(offset,limit)

    @tool
    def describe_calibration_profile() -> dict:
        """Read the packaged real-feeder marginal profile and its provenance/limits before planning empirical_tree cases."""
        from .calibration import load_profile
        profile=load_profile('epri_dpv_j1_k1')
        return {key:value for key,value in profile.items() if key not in {'templates','distributions'}} | {
            'distribution_counts':{key:len(value['values']) for key,value in profile['distributions'].items()},
            'usage':{'scenario':{'layout':'empirical_tree','calibration_profile':'epri_dpv_j1_k1'}},
            'notice':'Conditional marginal calibration only; no Chinese rural/urban representativeness or full joint geometry/equipment calibration.'}

    @tool
    def plan_paired_study(research_question: str, base_parameters: dict, pv_ratios: list[float],
                         load_scales: list[float], rule_ids: list[str] = []) -> dict:
        """Plan a factorial PV/load study on frozen paired networks. base_parameters.count is base-network count; pv_ratios are PV/unscaled base kW; load_scales leave PV capacity fixed. Repairs default to none/max_repairs0; explicit conflicting repair settings are rejected. Rule IDs reuse project evidence."""
        try:
            parameters=dict(base_parameters)
            if rule_ids or parameters.get('document_rules'):
                ids=rule_ids or [r['rule_id'] for r in parameters['document_rules']]
                parameters['document_rules']=[documents.get_rule(rid) for rid in ids]
            plan=build_study_plan(research_question,parameters,pv_ratios,load_scales)
        except (ValueError,OSError,KeyError) as exc:
            return {'status':'invalid_study','error':str(exc)}
        payload=plan.model_dump();plan_id=digest(payload)
        atomic_json(project_root/'study_plans'/f'{plan_id}.json',payload)
        return {'study_plan_id':plan_id,'plan':payload,
                'variant_count':plan.base_spec.count*len(plan.pv_ratios)*len(plan.load_scales),
                'baseline_runs':plan.base_spec.count,
                'notice':'Frozen topology/equipment/contracts; repairs disabled; PV uses unscaled baseline peak.'}

    @tool
    def execute_paired_study(study_plan_id: str, study_id: str) -> dict:
        """Execute a saved content-addressed paired study; return real paired checks, comparison CSV and OpenDSS/visualization archive."""
        if not re.fullmatch(r'[0-9a-f]{64}',study_plan_id):
            raise ValueError('Invalid study_plan_id')
        payload=json.loads((project_root/'study_plans'/f'{study_plan_id}.json').read_text())
        if digest(payload)!=study_plan_id:
            raise ValueError('Study plan hash mismatch')
        result=run_study(StudyPlan.model_validate(payload),project_root,study_id,workers)
        return {k:result[k] for k in ('study_id','directory','base_cases','variant_count','combinations','verified_report')}

    @tool
    def recall_repair_experience(spec: ExperimentSpec) -> dict:
        """Retrieve observed repairs for matching research conditions and code/rule/equipment versions. Suggestions only, never override current validation."""
        manifest={'spec':spec.model_dump(),'rules':load_rules(spec),'conductors':data('conductors.json'),'runtime':_runtime_fingerprint()}
        hints=ExperienceStore(project_root/'experiences.sqlite').lookup(experience_signature(manifest),experience_context(spec))
        return {'hints':hints,'notice':'Up to100 recent matched observations; improvement is separate from final acceptance.'}

    @tool
    def lookup_design_rules(query: str = '', voltage_kv: float = 10) -> dict:
        """Search curated source summaries; return all executable rules so hard constraints cannot be missed. This is not web search."""
        from .rule_system import describe_rule_system
        spec = ExperimentSpec(voltage_kv=voltage_kv)
        return {'sources': search_sources(query), 'executable_rules': load_rules(spec),
                'design_rule_system': describe_rule_system(spec)}

    @tool
    def read_project_memory() -> dict:
        """Read explicit saved project facts/preferences; never override current user constraints."""
        return memory.list(project_id)

    @tool
    def remember_project_fact(key: str, value: str) -> dict:
        """Save a fact/preference only when the user explicitly asks to remember it; never save secrets or guesses."""
        memory.put(project_id, key, value)
        return {'saved_key': key, 'project': project_id}

    @tool
    def plan_experiment(research_question: str, parameters: dict, inferred_fields: list[str] = [], rule_ids: list[str] = []) -> dict:
        """Validate and persist a structured proposal before execution. parameters contains ExperimentSpec fields including scenario {kind: urban/rural, aspect_ratio, positions_km}. Mark only explicitly supplied model-inferred fields; nested scenario.aspect_ratio paths supported. Omitted fields are defaults, not inferred_fields. rule_ids attaches exact stored document rules to this experiment. Returns immutable content-addressed plan ID, resolved defaults and assumptions."""
        try:
            parameters = dict(parameters)
            if rule_ids or parameters.get('document_rules'):
                ids = rule_ids or [r['rule_id'] for r in parameters['document_rules']]
                parameters['document_rules'] = [documents.get_rule(rid) for rid in ids]
            plan = build_plan(research_question, parameters, inferred_fields)
        except (ValueError, OSError, KeyError) as exc:
            return {'status': 'invalid_plan', 'error': str(exc),
                    'instruction': 'Correct parameters and retry. inferred_fields must reference supplied keys (nested scenario paths allowed); omitted values are defaults.'}
        payload = plan.model_dump()
        plan_id = digest(payload)
        atomic_json(project_root / 'plans' / f'{plan_id}.json', payload)
        return {'plan_id': plan_id, 'plan': payload}

    @tool
    def execute_plan(plan_id: str, experiment_id: str) -> dict:
        """Execute a previously validated plan unchanged, using its content-addressed ID; save plan, models, spatial metrics and DSS validation in the experiment package."""
        if not re.fullmatch(r'[0-9a-f]{64}', plan_id):
            raise ValueError('Invalid plan_id')
        payload = json.loads((project_root / 'plans' / f'{plan_id}.json').read_text())
        if digest(payload) != plan_id:
            raise ValueError('Plan content hash mismatch')
        plan = ExperimentPlan.model_validate(payload)
        result = run_experiment(plan.spec, project_root, experiment_id, workers=workers, plan=plan)
        return {'plan_id': plan_id, 'experiment_id': experiment_id, 'verified_report': result['verified_report'], 'attempted': result['attempted'], 'accepted': result['accepted'],
                'failed_or_unaccepted': result['failed_or_unaccepted'],
                'directory': str(project_root / 'experiments' / experiment_id),
                'samples': [{k: v for k, v in s.items() if k != 'artifacts'} for s in result['samples'][:10]],
                'limitations': result['limitations']}

    @tool
    def generate_cases(spec: ExperimentSpec, experiment_id: str) -> dict:
        """Generate and simulate real research cases. count=attempts; n_loads counts load points, not buses. Supports 0.38/6/10/20/35 kV balanced snapshots and 6/10/20 kV unbalanced phase snapshots; single-voltage radial research models, 50 Hz; no transformer-coupled levels. pv_ratio=PV kW/peak load kW. Returns artifact directory and actual acceptance statistics."""
        for rule in spec.document_rules:
            if documents.get_rule(rule.rule_id) != rule.model_dump():
                raise ValueError('Document rule differs from its project evidence snapshot')
        result = run_experiment(spec, project_root, experiment_id, workers=workers)
        return {'experiment_id': experiment_id, 'verified_report': result['verified_report'], 'attempted': result['attempted'],
                'accepted': result['accepted'], 'operational_pass': result['operational_pass'],
                'failed_or_unaccepted': result['failed_or_unaccepted'],
                'directory': str(project_root / 'experiments' / experiment_id),
                'limitations': result['limitations'], 'specification': spec.model_dump()}

    @tool
    def read_experiment_result(experiment_id: str) -> dict:
        """Read a saved model by exact returned ID, automatically choosing its verified artifact reader."""
        from .agent_results import read_grid_result, summarize_result
        return summarize_result(read_grid_result(project_root, experiment_id))

    @tool
    def read_grid_result(result_id: str, family: str | None = None) -> dict:
        """Read verified model/response/task evidence. Optional family: experiment, electrical_response,
        research_task, transmission, hierarchical, hierarchical_inverse. Use exact returned result_id;
        no need to read again after a tool has returned delivery.ready=true.
        """
        from .agent_results import read_grid_result as read, summarize_result
        return summarize_result(read(project_root, result_id, family))

    return [describe_research_tasks,design_research_grid,describe_electrical_response_capabilities,design_electrical_response,revise_electrical_response_permissions,inspect_planning_memory,describe_transmission_capabilities,revise_existing_transmission,read_transmission_result,describe_hierarchy_capabilities,read_hierarchical_result,read_hierarchical_inverse_result,describe_taxonomy_references,describe_equipment_profiles,describe_topology_styles, revise_existing_case, list_reference_feeders, generate_reference_case, design_from_language, describe_calibration_profile, import_design_document, search_project_documents, extract_document_rules, recall_repair_experience,
            list_project_rules, plan_paired_study, execute_paired_study,
            lookup_design_rules, read_project_memory, remember_project_fact,
            plan_experiment, execute_plan, generate_cases, read_experiment_result, read_grid_result]


@contextmanager
def agent_session(workspace: Path, project_id: str = 'default', model=None, workers: int = 1):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', project_id):
        raise ValueError('Invalid project_id')
    root = Path(workspace).resolve() / 'projects' / project_id
    root.mkdir(parents=True, exist_ok=True)
    llm = configured_model() if model is None else model
    with sqlite3.connect(root / 'conversations.sqlite', check_same_thread=False) as db:
        checkpointer = SqliteSaver(db)
        from .agent_boundary import OriginalRequestMiddleware
        agent = create_agent(
            model=llm, tools=make_tools(root, project_id, workers),
            system_prompt=SYSTEM_PROMPT, checkpointer=checkpointer,
            middleware=[OriginalRequestMiddleware(root), ModelCallLimitMiddleware(run_limit=8, exit_behavior='error'),
                        ToolCallLimitMiddleware(run_limit=12, exit_behavior='error')])
        yield agent
