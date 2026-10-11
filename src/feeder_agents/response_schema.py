"""Explicit contracts for static electrical-response-conditioned synthesis."""
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .schemas import ExperimentSpec, StrictModel
from .transmission import TransmissionSpec


class ResponseProbe(StrictModel):
    id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,39}$')
    kind: Literal['voltage_p', 'voltage_q', 'transfer_p']
    injection_buses: list[str] = Field(default_factory=list, max_length=300)
    withdrawal_buses: list[str] = Field(default_factory=list, max_length=300)
    monitor_buses: list[str] = Field(default_factory=list, max_length=300)
    monitor_branches: list[int] = Field(default_factory=list, max_length=700,
        description='Zero-based MATPOWER branch row indices; empty means all active branches.')
    phases: list[Literal[1, 2, 3]] | None = Field(default=None, min_length=1, max_length=3)
    step_mw: float = Field(default=.01, gt=0, le=10,
        description='Total group perturbation in MW, or MVAr for voltage_q; equally split over requested bus-phase ports.')
    aggregation: Literal['mean_abs', 'max_abs'] = 'mean_abs'

    @model_validator(mode='after')
    def ports(self):
        for name in ('injection_buses', 'withdrawal_buses', 'monitor_buses', 'monitor_branches', 'phases'):
            values = getattr(self, name) or []
            if len(values) != len(set(values)): raise ValueError('Duplicate '+name)
        if set(self.injection_buses) & set(self.withdrawal_buses):
            raise ValueError('Injection and withdrawal groups must be disjoint')
        if self.kind == 'transfer_p':
            if not self.injection_buses or not self.withdrawal_buses:
                raise ValueError('Transfer requires explicit injection and withdrawal groups')
            if self.phases or self.monitor_buses: raise ValueError('Transfer observes branches, not bus phases')
        elif self.withdrawal_buses or self.monitor_branches:
            raise ValueError('Voltage probes balance at the source and observe bus voltages')
        return self


class ResponseTarget(StrictModel):
    probe_id: str
    reference: Literal['absolute', 'baseline_ratio'] = 'absolute'
    lower: float | None = Field(default=None, ge=0)
    upper: float | None = Field(default=None, ge=0)

    @model_validator(mode='after')
    def interval(self):
        if self.lower is None and self.upper is None: raise ValueError('A response target needs a bound')
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValueError('Conflicting response target bounds')
        return self


class ResponseSearch(StrictModel):
    allowed_actions: list[Literal['replace_conductor', 'parallel_line', 'scale_layout', 'scale_subtree', 'rewire_branch', 'redistribute_load']] = Field(default_factory=list, max_length=6)
    max_changed_conductors: int | None = Field(default=None, ge=1, le=2000,
        description='Maximum final conductor assignments differing from the ORIGINAL model, across all rounds and joint edits. Omitted retains legacy uncapped catalogue selection.')
    max_layout_scale_change: float | None = Field(default=None, gt=0, le=.9,
        description='Explicit permission for uniform spatial scaling relative to the ORIGINAL geometry, e.g. 0.15 means factors [0.85, 1.15]. Not an engineering standard.')
    max_branch_length_change: float | None = Field(default=None, gt=0, le=.9)
    max_node_displacement_km: float | None = Field(default=None, gt=0, le=10)
    max_rewired_lines: int | None = Field(default=None, ge=1, le=20)
    max_rewire_length_ratio: float | None = Field(default=None, gt=1, le=3)
    max_node_degree: int | None = Field(default=None, ge=2, le=8)
    max_load_redistribution_fraction: float | None = Field(default=None, gt=0, le=.5,
        description='Moved kW = half the L1 change of nodal kW, divided by original total kW.')
    max_load_bus_change: float | None = Field(default=None, gt=0, le=.9,
        description='Each load kW change relative to its original kW; total P/Q and each PV remain fixed.')
    max_joint_actions: int = Field(default=1, ge=1, le=2)
    max_rounds: int = Field(default=4, ge=0, le=32)
    candidates_per_round: int = Field(default=8, ge=1, le=64)
    selector: Literal['heuristic', 'agent', 'greedy', 'uniform'] = 'heuristic'
    strategy: Literal['auto', 'monotone', 'archive'] = Field(default='auto',
        description='Auto uses bounded archive exploration for multiple targets, monotone search otherwise.')
    archive_width: int = Field(default=4, ge=1, le=16)
    calibrate_proxies: bool = Field(default=True,
        description='Use measured AC candidate changes to calibrate incremental distribution-response proposal gains; does not change targets or acceptance.')
    compensation_mode: Literal['fixed', 'conditional', 'bounded'] = Field(default='bounded',
        description='Fixed offset proposals, conditional affine sizing, or conditional sizing with original-reference geometric interval endpoints. All modes retain identical final contract and AC checks.')
    exploration_deficit_allowance: float = Field(default=.15, ge=0, le=1,
        description='Maximum temporary sum of normalized response deficits above the best incumbent. Hard engineering constraints are never relaxed.')
    max_search_ac_calls: int | None = Field(default=None, ge=1, le=100000,
        description='Hard limit on actual search AC evaluation attempts; baseline, construction, final verification and export are separately counted.')

    @model_validator(mode='after')
    def permissions(self):
        if len(self.allowed_actions) != len(set(self.allowed_actions)): raise ValueError('Duplicate actions')
        if self.max_changed_conductors is not None and 'replace_conductor' not in self.allowed_actions:
            raise ValueError('max_changed_conductors requires replace_conductor permission')
        if ('scale_layout' in self.allowed_actions) != (self.max_layout_scale_change is not None):
            raise ValueError('scale_layout requires explicit max_layout_scale_change; omit the bound when layout changes are forbidden')
        for action, fields in {'scale_subtree':['max_branch_length_change','max_node_displacement_km'],
                'rewire_branch':['max_rewired_lines','max_rewire_length_ratio','max_node_degree'],
                'redistribute_load':['max_load_redistribution_fraction','max_load_bus_change']}.items():
            if any((action in self.allowed_actions) != (getattr(self,field) is not None) for field in fields):
                raise ValueError(action+' requires explicit '+', '.join(fields)+'; omit these bounds when the action is forbidden')
        return self


class ResponseDesignPlan(StrictModel):
    research_question: str = Field(min_length=1, max_length=4000)
    base_spec: Annotated[ExperimentSpec | TransmissionSpec, Field(discriminator='network_kind')]
    probes: list[ResponseProbe] = Field(min_length=1, max_length=8)
    targets: list[ResponseTarget] = Field(min_length=1, max_length=16)
    search: ResponseSearch = Field(default_factory=ResponseSearch)
    verification_step_factor: float = Field(default=.5, gt=0, lt=1)
    verification_relative_tolerance: float = Field(default=.03, gt=0, le=.2)
    assumptions: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode='after')
    def compatible(self):
        if self.base_spec.count != 1: raise ValueError('Response design uses one fixed baseline; repeat plans for ensembles')
        ids = [p.id for p in self.probes]
        if len(set(ids)) != len(ids): raise ValueError('Duplicate probe IDs')
        bounds = {}
        for target in self.targets:
            if target.probe_id not in ids: raise ValueError('Target references unknown probe')
            key = (target.probe_id, target.reference)
            lo, hi = bounds.get(key, (0, float('inf')))
            lo = max(lo, target.lower if target.lower is not None else 0)
            hi = min(hi, target.upper if target.upper is not None else float('inf'))
            if lo > hi: raise ValueError('Conflicting response target bounds')
            bounds[key] = lo, hi
        distribution = self.base_spec.network_kind == 'distribution'
        if any((p.kind != 'transfer_p') != distribution for p in self.probes):
            raise ValueError('Distribution supports voltage_p/voltage_q; transmission supports transfer_p')
        permitted = {'replace_conductor', 'scale_layout', 'scale_subtree', 'rewire_branch', 'redistribute_load'} if distribution else {'parallel_line'}
        if not set(self.search.allowed_actions) <= permitted: raise ValueError('Unsupported response action for this domain')
        if distribution and set(self.search.allowed_actions)&{'scale_layout','scale_subtree'}:
            if self.base_spec.scenario.positions_km is not None:
                raise ValueError('Explicit coordinates forbid geometry scaling')
        if distribution and set(self.search.allowed_actions)&{'scale_subtree','rewire_branch','redistribute_load'}:
            if self.base_spec.scenario.layout != 'spatial_mst':
                raise ValueError('Local response changes currently require spatial_mst geometry')
        if distribution and 'rewire_branch' in self.search.allowed_actions and self.base_spec.scenario.tie_count:
            raise ValueError('Response rewiring currently excludes normally-open ties')
        if distribution and 'redistribute_load' in self.search.allowed_actions and self.base_spec.scenario.load_shape != 'heterogeneous':
            raise ValueError('Load redistribution requires heterogeneous placement without a prescribed shape')
        if distribution and self.base_spec.mode != 'normal':
            raise ValueError('Response synthesis requires a feasible normal reference condition')
        if not distribution and 'parallel_line' in self.search.allowed_actions and 'parallel_line' not in self.base_spec.allowed_repairs:
            raise ValueError('Parallel circuits forbidden by base specification')
        return self
