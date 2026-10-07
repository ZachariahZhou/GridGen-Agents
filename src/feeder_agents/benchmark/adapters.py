"""Planning adapters share the same downstream generator; no scoring key in public input."""
import json
from pydantic import Field, model_validator, field_validator
from ..schemas import StrictModel
from ..hierarchy import HierarchicalSpec
from ..structured_planning import StructuredPlanner


def public_task(case):
    return dict(id=case.id,request=case.request)


class ExtractedSpec(StrictModel):
    unsupported: list[str]=Field(default_factory=list)
    parameters: dict=Field(default_factory=dict,description='Only explicitly requested scalar HierarchicalSpec fields; no regional or installation analysis')


class PlannedSpec(StrictModel):
    unsupported: list[str]=Field(default_factory=list)
    spec: HierarchicalSpec | None=None

    @field_validator('spec',mode='before')
    @classmethod
    def decode_spec(cls,value):
        # Some OpenAI-compatible backends encode nested tool arguments as JSON strings.
        # Decode only; all original schema/constraint checks still apply.
        return json.loads(value) if isinstance(value,str) else value

    @model_validator(mode='after')
    def exclusive_result(self):
        if bool(self.unsupported)==(self.spec is not None):
            raise ValueError('Return either a specification or unsupported reasons')
        return self


LLM_ADAPTERS=('llm_extract','llm_plan','full_agent')


def plan_public(name,task,workspace,model=None,fixed_spec=None):
    if set(task)!={'id','request'}:raise ValueError('Adapter input must not contain evaluation keys')
    if name=='fixed_template':
        return dict(status='ready',spec=fixed_spec.model_dump(),method='Constant structured template; does not interpret natural language')
    if name=='llm_extract':
        fields={k:v for k,v in HierarchicalSpec.model_json_schema()['properties'].items() if k not in ('lv_regions','lv_installation_decision')}
        planner=StructuredPlanner(model,ExtractedSpec,workspace)
        raw=planner.invoke([
            ('system','You extract requirements for a WORKING multivoltage distribution research-case generator: 6/10/20kV MV, explicit three-phase transformers, 0.38/0.4kV LV branches, single-phase customers, 50Hz grounded-equivalent unbalanced snapshots, fixed-power PV, overhead/direct-burial/duct LV products. Research feeders and grounded-equivalent models are supported descriptions, not unsupported features. voltage_kv is MV and lv_voltage_kv is LV; total_kw is total customer load; users is customers, n_buses includes all voltage tiers and can be omitted. Extract explicitly requested scalar feeder parameters only. Return unsupported only for actual requirements outside these capabilities and the schema (for example dynamic thermal generators, transmission grids, explicit neutral dynamics, or regional design for this baseline). Do not infer installation from urban/rural labels, do not create regional plans. Omit unspecified fields and use defaults. No generation or scoring tools are available.'),
            ('human',json.dumps(dict(request=task['request'],fields=fields),ensure_ascii=False))])
        value=ExtractedSpec.model_validate(raw)
        if value.unsupported:
            planner.valid()
            return dict(status='unsupported',issues=value.unsupported)
        if set(value.parameters)-set(fields):raise ValueError('Extraction baseline returned fields outside its permitted schema')
        spec=HierarchicalSpec.model_validate(value.parameters)
        planner.valid()
        return dict(status='ready',spec=spec.model_dump(),method='Single structured extraction call and fixed pipeline')
    if name=='llm_plan':
        from ..hierarchy import hierarchy_capabilities,hierarchy_input_defaults
        from ..installation_planning import validate_request_evidence
        planner=StructuredPlanner(model,PlannedSpec,workspace)
        raw=planner.invoke([
            ('system','Design one synthetic multivoltage distribution research feeder from the request. '
             'Use the complete specification schema, including regional installation decisions when needed. '
             'Choose installation from site conditions, not urban/rural stereotypes. '
             'Each alternatives list must contain ALL THREE installations including selected itself: aerial_bundle, buried_direct, buried_duct. '
             'Omit unspecified parameters to retain defaults; users may omit n_buses. '
             'n_buses = 1 + transformer_count * (2 + lv_branches) + users. '
             'User evidence must be exact request substrings; identify assumptions separately. '
             'Return unsupported reasons only for requirements outside the provided capabilities. '
             'If unsupported is nonempty, spec must be null; otherwise return spec and an empty unsupported list. '
             'You have one planning call, no feedback or correction tools.'),
            ('human',json.dumps(dict(request=task['request'],capabilities=hierarchy_capabilities(),
                defaults=hierarchy_input_defaults()),ensure_ascii=False))])
        value=PlannedSpec.model_validate(raw)
        if value.unsupported:
            planner.valid()
            return dict(status='unsupported',issues=value.unsupported)
        spec=value.spec
        decisions=[r.decision for r in spec.lv_regions]
        if spec.lv_installation_decision:decisions.append(spec.lv_installation_decision)
        for decision in decisions:validate_request_evidence(decision,task['request'])
        planner.valid()
        return dict(status='ready',spec=spec.model_dump(),method='Single full-schema planning call; no correction loop')
    if name=='full_agent':
        from ..design import interpret_request
        brief=interpret_request(task['request'],workspace,model=model,task_scope='hierarchical')
        if brief['status']!='ready':return dict(status=brief['status'],issues=brief.get('issues',brief.get('questions',[])),brief=brief)
        if brief['plan_type']!='hierarchical':
            # A supported other task is not a correct refusal of this benchmark.
            return dict(status='task_mismatch',issues=['Benchmark implements hierarchical generation only'],brief=brief)
        return dict(status='ready',spec=brief['plan']['spec'],brief=brief,method='Current structured analysis Agent; deterministic generation/validation tools')
    raise ValueError('Unknown public adapter')


def plan_oracle(case):
    # Privileged capability reference, deliberately separate from public adapters.
    if case.expected=='abstain':return dict(status='needs_clarification',issues=['Oracle answer key: conflict requires clarification or refusal'],privileged=True)
    if case.expected=='reject':return dict(status='unsupported',issues=['Oracle answer key: rejection expected'],privileged=True)
    return dict(status='ready',spec=case.oracle_spec.model_dump(),privileged=True)
