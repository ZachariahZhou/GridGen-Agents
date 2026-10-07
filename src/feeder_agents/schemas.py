from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class PhaseDesign(StrictModel):
    mode: Literal['balanced','unbalanced'] = 'balanced'
    load_phase_weights: list[float] = Field(default_factory=lambda:[.5,.3,.2],min_length=3,max_length=3)
    pv_phase_weights: list[float] | None = Field(default=None,min_length=3,max_length=3)
    single_phase_laterals: int | None = Field(default=None,ge=0,le=2000)
    two_phase_laterals: int | None = Field(default=None,ge=0,le=2000)
    max_vuf_percent: float = Field(default=2,gt=0,le=20,description='Research negative/positive-sequence voltage ratio limit, not regulatory certification.')

    @model_validator(mode='after')
    def weights(self):
        import math
        for weights in (self.load_phase_weights,self.pv_phase_weights):
            if weights is not None and (any(not math.isfinite(x) or x<=0 for x in weights) or not math.isclose(sum(weights),1,abs_tol=1e-9)):
                raise ValueError('Phase weights must be three positive fractions summing to1')
        if self.mode=='balanced' and (self.pv_phase_weights is not None or self.load_phase_weights!=[.5,.3,.2]
                or self.single_phase_laterals not in (None,0) or self.two_phase_laterals not in (None,0)):
            raise ValueError('Explicit unequal phase weights or reduced-phase laterals require unbalanced mode')
        return self


class TopologySettings(StrictModel):
    family: Literal['long_trunk','comb','multi_branch','balanced_tree','irregular_tree','open_ring']
    branch_count: int | None = Field(default=None, ge=2, le=12)
    branching_factor: int | None = Field(default=None, ge=2, le=4)
    trunk_fraction: float | None = Field(default=None, ge=.15, le=.8)

    @model_validator(mode='after')
    def scoped_parameters(self):
        if self.branch_count is not None and self.family not in {'comb','multi_branch'}:
            raise ValueError('branch_count only applies to comb/multi_branch')
        if self.branching_factor is not None and self.family not in {'balanced_tree','irregular_tree'}:
            raise ValueError('branching_factor only applies to balanced_tree/irregular_tree')
        if self.trunk_fraction is not None and self.family!='comb':
            raise ValueError('topology.trunk_fraction only applies to comb')
        return self


class ScenarioProfile(StrictModel):
    load_placement: Literal['all_nodes', 'reference_conditioned'] = 'all_nodes'
    reference_case_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_]{1,64}$',
        description='MATPOWER reference for conditional load occupancy/weights, not copying geography.')
    kind: Literal['urban', 'rural'] = 'urban'
    layout: Literal['spatial_mst', 'legacy_random', 'rural_villages', 'empirical_tree', 'structured_radial'] = 'spatial_mst'
    engineering_profile: Literal['generic','urban','rural'] = 'generic'
    topology: TopologySettings | None = None
    tie_count: int = Field(default=0,ge=0,le=10,description='Normally-open physical ties; energized network remains radial.')
    load_shape: Literal['uniform','heterogeneous','downstream_heavy','upstream_heavy'] = 'heterogeneous'
    load_concentration: float = Field(default=2,ge=0,le=4)
    calibration_profile: str | None = Field(default=None, pattern=r'^[a-z0-9_]{1,64}$',
        description='Packaged real-feeder reference profile; empirical_tree only.')
    empirical_weight: float = Field(default=.7,ge=0,le=1,
        description='Empirical/prior mixture weight for empirical_tree; an explicit transfer assumption, not a fitted confidence.')
    village_count: int | None = Field(default=None, ge=1, le=50,
        description='Optional settlement count for rural_villages; synthetic clusters, not GIS villages.')
    trunk_fraction: float = Field(default=.3, ge=.1, le=.8,
        description='Rural template trunk-node fraction; clipped to leave at least one node per village. Research assumption.')
    aspect_ratio: float | None = Field(default=None, ge=1, le=20)
    positions_km: list[tuple[float, float]] | None = Field(default=None, min_length=3, max_length=2001,
        description='Local Cartesian km; source first, then all non-source buses including junctions. Not latitude/longitude.')


class RepairPolicy(StrictModel):
    strategy: Literal['none', 'fixed', 'heuristic', 'agent'] = 'fixed'
    allow_rewire: bool = False
    preview_limit: int = Field(default=2, ge=0, le=4, description='Agent-only extra DSS candidate previews per round; zero disables')
    max_candidates: int = Field(default=8, ge=2, le=8)


class RepairCandidate(StrictModel):
    candidate_id: str
    model_hash: str
    action: Literal['upgrade_conductor', 'reconnect_branch']
    line_id: str
    conductor: str | None = None
    parent_bus: str | None = None
    child_bus: str | None = None
    rationale: str
    priority: float


class RepairDecision(StrictModel):
    candidate_id: str = Field(description='Choose an offered candidate_id, or stop')
    reason: str = Field(min_length=1, max_length=1000)


class DocumentClause(StrictModel):
    voltage_levels_kv: list[float] = Field(default_factory=lambda: [10.0], min_length=1, max_length=16,
        description='Explicit applicability; omitted legacy rules apply only to 10 kV.')
    metric: Literal['min_voltage_pu', 'max_voltage_pu', 'max_loading_ratio', 'max_line_km', 'total_line_km']
    operator: Literal['ge', 'le']
    threshold: float = Field(gt=0)
    unit: Literal['pu', 'ratio', 'km']
    quote: str = Field(min_length=5, max_length=2000)
    locator: str = Field(min_length=1, max_length=200)
    scenario: Literal['any', 'urban', 'rural'] = 'any'
    mode: Literal['any', 'normal', 'stress'] = 'any'
    load_semantics: Literal['any', 'aggregated', 'high_voltage_users'] = 'any'

    @model_validator(mode='after')
    def check_dimensions(self):
        unit = 'pu' if 'voltage' in self.metric else 'ratio' if self.metric == 'max_loading_ratio' else 'km'
        operator = 'ge' if self.metric == 'min_voltage_pu' else 'le'
        if self.unit != unit or self.operator != operator:
            raise ValueError('Unsupported metric/operator/unit combination')
        return self


class DocumentRule(DocumentClause):
    rule_id: str = Field(pattern=r'^doc\.[0-9a-f]{20}$')
    document_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_title: str
    source_url: str = ''
    chunk_index: int = Field(ge=0)
    status: Literal['evidence_checked_candidate'] = 'evidence_checked_candidate'


class ClauseExtraction(StrictModel):
    rules: list[DocumentClause] = Field(default_factory=list, max_length=16)
    unsupported: list[str] = Field(default_factory=list, max_length=32)


class EquipmentDesign(StrictModel):
    mode: Literal['auto','reference','legacy'] = 'auto'
    reference_feeders: list[Literal['J1','K1','Ckt5','Ckt24']] = Field(default_factory=lambda:['J1','K1','Ckt5','Ckt24'],min_length=1,max_length=4)
    source_weighting: Literal['equal_feeder','segment_frequency'] = 'equal_feeder'
    loading_margin: float = Field(default=.8,gt=0,le=1)
    capacity_band: float = Field(default=2,ge=1,le=5)
    conditioning: Literal['role','length_voltage'] = 'length_voltage'
    length_bandwidth: float = Field(default=.65,ge=.1,le=3,description='Log-length kernel bandwidth; research smoothing assumption.')
    voltage_drop_budget_pu: float = Field(default=.05,gt=0,le=.3,description='Soft path-drop surrogate budget, not an AC feasibility guarantee.')
    selection_reason: str = Field(default='Scenario construction, phase count, downstream endpoint capacity and observed usage frequency.',max_length=2000)


class ExperimentSpec(StrictModel):
    """Explicit research conditions; count means attempts, not guaranteed successes."""

    network_kind: Literal['distribution'] = 'distribution'
    document_rules: list[DocumentRule] = Field(default_factory=list, max_length=32)
    repair_policy: RepairPolicy = Field(default_factory=RepairPolicy)
    scenario: ScenarioProfile = Field(default_factory=ScenarioProfile)
    phase_design: PhaseDesign = Field(default_factory=PhaseDesign)
    equipment_design: EquipmentDesign = Field(default_factory=EquipmentDesign)
    count: int = Field(default=1, ge=1, le=10000)
    n_buses: int | None = Field(default=None, ge=3, le=2001, description='Optional total buses including source, load sites and zero-load junctions.')
    n_loads_min: int = Field(default=20, ge=2, le=2000)
    n_loads_max: int = Field(default=30, ge=2, le=2000)
    total_kw_min: float = Field(default=1000, gt=0, le=50000)
    total_kw_max: float = Field(default=2000, gt=0, le=50000)
    voltage_kv: float = 10
    frequency_hz: Literal[50] = 50
    power_factor: float = Field(default=0.95, ge=0.5, le=1)
    pv_ratio: float = Field(default=0, ge=0, le=3,
                            description='PV kW / coincident peak load kW; fixed unity-PF injection')
    segment_km_min: float = Field(default=0.05, ge=0.005, le=5)
    segment_km_max: float = Field(default=0.25, ge=0.005, le=5)
    seed: int = Field(default=42, ge=0, le=2**32-1)
    mode: Literal['normal', 'stress'] = 'normal'
    load_semantics: Literal['aggregated', 'high_voltage_users'] = 'aggregated'
    special_pf_agreement: bool = False
    max_repairs: int = Field(default=2, ge=0, le=2)

    @model_validator(mode='before')
    @classmethod
    def voltage_defaults(cls, value):
        if not isinstance(value, dict):
            return value
        from .voltage import voltage_profile
        value = dict(value)
        profile = voltage_profile(value.get('voltage_kv', 10))
        # Never override an explicitly supplied bound, even if it conflicts.
        for key, default in profile['defaults'].items():
            value.setdefault(key, default)
        scenario = value.get('scenario', {})
        if isinstance(scenario, ScenarioProfile):
            scenario = scenario.model_dump()
        if isinstance(scenario, dict):
            placement = scenario.get('load_placement', 'all_nodes')
            if placement == 'reference_conditioned' and scenario.get('positions_km') is not None:
                if value.get('n_buses') is None:
                    value['n_buses'] = len(scenario['positions_km'])
            count = value.get('n_buses')
            if isinstance(count, int) and not isinstance(count, bool) and 3 <= count <= 2001:
                if placement == 'reference_conditioned' and scenario.get('reference_case_id'):
                    from .load_placement import reference_occupancy
                    fraction = reference_occupancy(scenario['reference_case_id'])['fraction']
                    default_loads = max(2, min(count-1, round((count-1)*fraction)))
                else:
                    default_loads = count-1
                value.setdefault('n_loads_min', default_loads)
                value.setdefault('n_loads_max', default_loads)
        return value

    @model_validator(mode='after')
    def ordered_ranges(self):
        from .voltage import voltage_profile
        profile = voltage_profile(self.voltage_kv)
        if profile['tier'] == 'lv' and self.load_semantics == 'high_voltage_users':
            raise ValueError('Low-voltage cases cannot use high_voltage_users semantics')
        if profile['tier'] != 'mv' and self.scenario.layout in {'empirical_tree', 'rural_villages'}:
            raise ValueError('This calibrated/settlement layout currently requires a 6/10/20 kV MV profile')

        if self.equipment_design.mode=='reference' and (self.voltage_kv not in (6,10,20) or self.scenario.engineering_profile=='generic'):
            raise ValueError('Reference equipment requires6/10/20kV and urban/rural engineering profile')
        if self.phase_design.mode=='unbalanced':
            if self.voltage_kv not in (6,10,20):
                raise ValueError('Unbalanced grounded-equivalent model currently supports6/10/20kV; LV neutral and transformer models unavailable')
            if self.repair_policy.allow_rewire:
                raise ValueError('Unbalanced phase continuity forbids automatic rewiring')
        if self.scenario.engineering_profile!='generic' and self.scenario.engineering_profile!=self.scenario.kind:
            raise ValueError('Engineering profile must match urban/rural scenario kind')
        scene=self.scenario
        if scene.layout=='structured_radial':
            if scene.topology is None:raise ValueError('structured_radial requires topology settings')
            if scene.positions_km is not None or scene.aspect_ratio is not None:
                raise ValueError('structured_radial defines its own geometry; explicit coordinates/aspect_ratio unsupported')
            if self.repair_policy.allow_rewire:raise ValueError('structured_radial family must not be changed by automatic rewiring')
            from .topologies import topology_edges
            structural_count=self.n_buses-1 if self.n_buses is not None else self.n_loads_min
            if self.n_buses is None and scene.load_placement=='reference_conditioned' and scene.reference_case_id:
                import math
                from .load_placement import reference_occupancy
                structural_count=math.ceil(self.n_loads_min/reference_occupancy(scene.reference_case_id)['fraction']-1e-12)
            topology_edges(structural_count,scene.topology,0)
            if scene.topology.family=='open_ring' and scene.tie_count!=1:
                raise ValueError('open_ring requires exactly tie_count=1; closed-loop operation unsupported')
        elif scene.topology is not None:raise ValueError('topology settings require structured_radial')
        if scene.load_shape!='heterogeneous' and (scene.layout in {'rural_villages','empirical_tree'} or scene.load_placement=='reference_conditioned'):
            raise ValueError('Custom load_shape conflicts with calibrated/settlement/conditional load allocation')
        if scene.load_shape not in {'downstream_heavy','upstream_heavy'} and scene.load_concentration!=2:
            raise ValueError('Nondefault load_concentration requires a directional load_shape')
        if scene.tie_count and self.repair_policy.allow_rewire:
            raise ValueError('Tie-equipped physical topology cannot be changed by automatic rewiring')
        conditional = self.scenario.load_placement == 'reference_conditioned'
        if conditional:
            import math
            from .load_placement import reference_occupancy
            if not self.scenario.reference_case_id:
                raise ValueError('reference_conditioned requires reference_case_id')
            reference = reference_occupancy(self.scenario.reference_case_id)
            if profile['tier'] != 'mv' or self.scenario.layout not in {'spatial_mst', 'legacy_random', 'structured_radial'}:
                raise ValueError('reference_conditioned requires MV spatial_mst, legacy_random or structured_radial; village/empirical allocation conflicts')
            if self.repair_policy.allow_rewire:
                raise ValueError('reference_conditioned preserves topology-dependent placement; rewiring unsupported')
            if self.n_buses is None and math.ceil(self.n_loads_max/reference['fraction']-1e-12)+1 > 2001:
                raise ValueError('Inferred total node count exceeds 2001; supply compatible n_buses/load counts')
        elif self.scenario.reference_case_id is not None:
            raise ValueError('reference_case_id requires reference_conditioned load placement')
        if self.n_buses is not None:
            if self.n_loads_max > self.n_buses-1:
                raise ValueError('Positive load count exceeds available non-source buses')
            if not conditional and not self.n_loads_min == self.n_loads_max == self.n_buses-1:
                raise ValueError('all_nodes requires n_loads_min=n_loads_max=n_buses-1')

        if len({r.rule_id for r in self.document_rules}) != len(self.document_rules):
            raise ValueError('Duplicate document rule IDs')
        for field in ('n_loads', 'total_kw', 'segment_km'):
            if getattr(self, field + '_min') > getattr(self, field + '_max'):
                raise ValueError(f'{field}_min must not exceed {field}_max')
        if self.scenario.layout!='rural_villages' and self.scenario.trunk_fraction!=.3:
            raise ValueError('Nondefault trunk_fraction requires rural_villages')
        points = self.scenario.positions_km
        if self.scenario.layout == 'empirical_tree':
            if not self.scenario.calibration_profile:
                raise ValueError('empirical_tree requires calibration_profile')
            if points is not None or self.scenario.aspect_ratio is not None or self.scenario.village_count is not None:
                raise ValueError('empirical_tree does not support explicit positions, aspect ratio or village count')
        elif self.scenario.calibration_profile is not None:
            raise ValueError('calibration_profile requires empirical_tree layout')
        if self.scenario.layout!='empirical_tree' and self.scenario.empirical_weight!=.7:
            raise ValueError('Nondefault empirical_weight requires empirical_tree layout')
        if self.scenario.layout == 'rural_villages':
            if self.scenario.kind != 'rural':
                raise ValueError('rural_villages requires rural scenario')
            if self.scenario.aspect_ratio is not None or points is not None:
                raise ValueError('rural_villages defines its own geometry; aspect_ratio/positions are not supported')
            if self.scenario.village_count is not None and 2*self.scenario.village_count > self.n_loads_min:
                raise ValueError('rural_villages requires at least two load points per requested village')
            if self.repair_policy.allow_rewire:
                raise ValueError('rural_villages currently preserves the trunk/village hierarchy; rewiring is unsupported')
        elif self.scenario.village_count is not None:
            raise ValueError('village_count requires rural_villages layout')
        if points is not None:
            if self.scenario.layout != 'spatial_mst':
                raise ValueError('Explicit positions require spatial_mst')
            expected = self.n_buses if conditional else self.n_loads_min+1
            if len(points) != expected or (not conditional and self.n_loads_min != self.n_loads_max):
                raise ValueError('Explicit positions must match all buses, including zero-load junctions')
            if len(set(points)) != len(points):
                raise ValueError('Duplicate positions are unsupported')
        return self


class ExperimentPlan(StrictModel):
    export_formats: list[Literal['opendss','matpower']] = Field(default_factory=lambda:['opendss'],min_length=1,max_length=2)

    @model_validator(mode='after')
    def valid_export_formats(self):
        if len(set(self.export_formats))!=len(self.export_formats) or 'opendss' not in self.export_formats:
            raise ValueError('Export formats must be unique and retain the OpenDSS primary model')
        if 'matpower' in self.export_formats and self.spec.phase_design.mode!='balanced':
            raise ValueError('MATPOWER adapter requires balanced distribution; unbalanced information cannot be silently discarded')
        return self

    research_question: str = Field(min_length=1, max_length=4000)
    spec: ExperimentSpec
    parameter_origins: dict[str, Literal['user', 'inferred', 'default']]
    assumptions: list[str]
    metrics: list[str] = Field(default_factory=lambda: [
        'total_line_km', 'max_source_path_km', 'max_degree', 'max_depth',
        'min_voltage_pu', 'max_voltage_pu', 'solver_converged'])
    sampling: Literal['seeded_independent_cases'] = 'seeded_independent_cases'


class StudyPlan(StrictModel):
    research_question: str = Field(min_length=1, max_length=4000)
    base_spec: ExperimentSpec
    pv_ratios: list[float] = Field(min_length=1, max_length=16,
        description='PV capacity / unscaled baseline peak kW, independent of load scale')
    load_scales: list[float] = Field(min_length=1, max_length=16)
    design: Literal['paired_factorial'] = 'paired_factorial'

    @model_validator(mode='after')
    def validate_design(self):
        if self.base_spec.repair_policy.strategy != 'none' or self.base_spec.max_repairs != 0:
            raise ValueError('Paired study requires repair strategy none and max_repairs=0')
        if self.base_spec.repair_policy.allow_rewire:
            raise ValueError('Paired study cannot enable repair rewiring')
        if len(set(self.pv_ratios))!=len(self.pv_ratios) or len(set(self.load_scales))!=len(self.load_scales):
            raise ValueError('Duplicate factor levels')
        combinations=len(self.pv_ratios)*len(self.load_scales)
        if combinations>64 or combinations*self.base_spec.count>10000:
            raise ValueError('Study exceeds64 combinations or10000 variants')
        for pv in self.pv_ratios:
            for scale in self.load_scales:
                if not 0<=pv<=3 or not 0<scale<=5:
                    raise ValueError('PV ratio must be0–3 and load scale >0 and <=5')
                payload=self.base_spec.model_dump()
                payload.update(total_kw_min=self.base_spec.total_kw_min*scale,
                               total_kw_max=self.base_spec.total_kw_max*scale,pv_ratio=pv/scale)
                ExperimentSpec.model_validate(payload)
        return self


class DesignAssignment(StrictModel):
    field: str = Field(description='ExperimentSpec field or nested scenario/repair_policy path')
    value: JsonValue
    origin: Literal['user','inferred']
    evidence: str = Field(default='',max_length=1000,description='Exact substring of request, required for user origin')
    reason: str = Field(min_length=1,max_length=600)


class DesignIntent(StrictModel):
    summary: str = Field(min_length=1,max_length=1000)
    task: Literal['feeder','paired_study','inverse_design','hierarchical','hierarchical_inverse','transmission'] = 'feeder'
    inverse: dict[str, JsonValue] | None = Field(default=None,
        description='Inverse-design conditions and search only; base_spec comes from assignments.')
    inverse_evidence: str = Field(default='',max_length=12000,
        description='Exact request quote grounding inverse conditions, goals and permitted search range.')
    assignments: list[DesignAssignment] = Field(default_factory=list,max_length=40)
    pv_ratios: list[float] | None = None
    load_scales: list[float] | None = None
    rule_ids: list[str] = Field(default_factory=list,max_length=32)
    blocking_questions: list[str] = Field(default_factory=list,max_length=5)
    unsupported: list[str] = Field(default_factory=list,max_length=12)
    assumptions: list[str] = Field(default_factory=list,max_length=12)

    @field_validator('inverse',mode='before')
    @classmethod
    def decode_inverse_object(cls,value):
        # Some OpenAI-compatible providers encode a nested tool argument as
        # JSON text. Decode once; Pydantic and InverseDesignPlan still enforce
        # object shape, allowed fields, units and bounds afterwards.
        if isinstance(value,str):
            import json
            return json.loads(value)
        return value


class PhaseBearing(StrictModel):
    phases: list[Literal[1,2,3]] = Field(default_factory=lambda:[1,2,3],min_length=1,max_length=3)

    @field_validator('phases')
    @classmethod
    def phase_set(cls,value):
        if len(set(value))!=len(value):raise ValueError('Duplicate phase identifiers')
        return sorted(value)


class PhasePower(StrictModel):
    phase: Literal[1,2,3]
    kw: float = Field(ge=0)
    kvar: float = Field(ge=0)
    pv_kw: float = Field(ge=0)


class Bus(PhaseBearing):
    id: str
    x_km: float
    y_km: float


class Line(PhaseBearing):
    id: str
    bus1: str
    bus2: str
    length_km: float = Field(gt=0)
    conductor: str
    construction: Literal['illustrative','overhead','cable'] = 'illustrative'


class Load(PhaseBearing):
    id: str
    bus: str
    kw: float = Field(gt=0)
    kvar: float = Field(ge=0)
    pv_kw: float = Field(ge=0)
    contract_kva: float = Field(gt=0)
    category: Literal['aggregated','residential','commercial','agricultural'] = 'aggregated'
    phase_powers: list[PhasePower] = Field(default_factory=list,max_length=3)


class Feeder(StrictModel):
    phase_mode: Literal['balanced','unbalanced'] = 'balanced'
    seed: int
    voltage_kv: float
    frequency_hz: int
    source_bus: str = 'source'
    total_kw: float
    buses: list[Bus]
    lines: list[Line]
    loads: list[Load]
    tie_lines: list[Line] = Field(default_factory=list, description="Physical normally-open ties, excluded from energized radial lines.")
    assumptions: list[str]
    design_evidence: dict[str, JsonValue] = Field(default_factory=dict,
        description='Initial design choices and estimates, not final power-flow measurements.')
