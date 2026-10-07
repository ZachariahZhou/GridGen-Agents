"""Explicit layout regeneration and observable research-style acceptance targets."""
import math
from typing import Literal
from pydantic import Field, model_validator
from .schemas import StrictModel, ExperimentSpec, ScenarioProfile, TopologySettings
from .style import style_contract


class LayoutRequest(StrictModel):
    layout: Literal['spatial_mst','rural_villages','structured_radial']
    kind: Literal['urban','rural']
    topology: TopologySettings | None = None
    tie_count: int = Field(default=0,ge=0,le=10)
    load_shape: Literal['uniform','heterogeneous','downstream_heavy','upstream_heavy'] = 'heterogeneous'
    load_concentration: float = Field(default=2,ge=0,le=4)
    village_count: int | None = Field(default=None,ge=1,le=50)
    aspect_ratio: float | None = Field(default=None,ge=1,le=20)
    trunk_fraction: float = Field(default=.3,ge=.1,le=.8)


class StyleTarget(StrictModel):
    metric: Literal['trunk_length_share','village_count','mean_village_anchor_distance_km',
                    'extent_x_km','extent_y_km','max_depth','leaf_fraction','total_line_km',
                    'source_branches','max_children','mean_depth','max_degree','tie_count','load_weighted_depth']
    operator: Literal['ge','le','increase','decrease']
    threshold: float | None = Field(default=None,ge=0)
    evidence: str = Field(min_length=1,max_length=12000)

    @model_validator(mode='after')
    def consistent(self):
        if (self.operator in {'ge','le'}) != (self.threshold is not None):
            raise ValueError('ge/le require a threshold; increase/decrease compare against parent')
        return self


def check_style_targets(before_spec,before,after_spec,after,targets):
    a=style_contract(before_spec,before)['observed']
    b=style_contract(after_spec,after)['observed']
    checks=[]
    for t in targets:
        old,new=a[t.metric],b[t.metric]
        threshold=old if t.threshold is None else t.threshold
        tol=1e-9*max(1,abs(threshold))
        passed={'ge':new>=threshold-tol,'le':new<=threshold+tol,
                'increase':new>threshold+tol,'decrease':new<threshold-tol}[t.operator]
        checks.append({'metric':t.metric,'operator':t.operator,'before':old,'after':new,
                       'threshold':threshold,'passed':passed,'evidence':t.evidence})
    return checks


def regenerate_layout(feeder,spec,payload,request,preserve):
    from .generation import generate_feeder
    from .settlements import hierarchy_matches,size_conductors
    if spec.scenario.load_placement=='reference_conditioned':
        raise ValueError('reference_conditioned topology/load joint redesign is unsupported; junction roles cannot be silently discarded')
    layout_parameters=request.model_dump()
    layout_parameters['engineering_profile']=spec.scenario.engineering_profile
    if spec.scenario.engineering_profile!='generic' and request.kind!=spec.scenario.kind:
        raise ValueError('Changing urban/rural engineering profile requires a new explicit design; layout change cannot silently replace equipment assumptions')
    payload['scenario']=ScenarioProfile(**layout_parameters).model_dump()
    target=ExperimentSpec.model_validate(payload)
    candidate=generate_feeder(target,feeder.seed)
    # Spatial templates have no new allocation constraint. Rural templates keep
    # the 80/20 assumption; freeze load and PV fields independently.
    if request.layout!='rural_villages':
        if request.load_shape!='heterogeneous' and {'loads','pv'} & set(preserve):
            raise ValueError('Requested load_shape conflicts with frozen nodal loads/pv; use a separate revision')
        if request.load_shape=='heterogeneous':
            candidate.loads=[load.model_copy(deep=True) for load in feeder.loads]
    else:
        originals={load.id:load for load in feeder.loads}
        for load in candidate.loads:
            original=originals[load.id]
            if 'loads' in preserve:
                load.kw=original.kw;load.kvar=original.kvar;load.contract_kva=original.contract_kva
                load.pv_kw=load.kw*target.pv_ratio
            if 'pv' in preserve:load.pv_kw=original.pv_kw
        if not hierarchy_matches(candidate,target):
            raise ValueError('Frozen loads/pv conflict with rural_villages 80/20 allocation; cannot silently redistribute')
        if any(not math.isclose(l.pv_kw,l.kw*target.pv_ratio,rel_tol=1e-9,abs_tol=1e-8) for l in candidate.loads):
            raise ValueError('Frozen pv conflicts with the current uniform per-node PV/load ratio model')
    # Source position is preserved as origin; generated geometry may use zero.
    source=next(b for b in feeder.buses if b.id==feeder.source_bus)
    for bus in candidate.buses:
        bus.x_km+=source.x_km;bus.y_km+=source.y_km
    if request.load_shape=='heterogeneous':
        from .load_shapes import apply_load_shape
        apply_load_shape(candidate,target)
    if candidate.phase_mode=='unbalanced':
        from .phases import phase_contract_matches,size_phase_conductors
        if not phase_contract_matches(candidate,target):
            raise ValueError('Frozen per-node phase allocations conflict with redesigned phase connectivity')
        candidate.design_evidence['initial_conductor_sizing']=size_phase_conductors(candidate)
    else:
        candidate.design_evidence['initial_conductor_sizing']=size_conductors(candidate)
    from .equipment import assign_equipment
    assign_equipment(candidate,target)
    candidate.design_evidence['redesign_policy']={
        'operation':'regenerate_layout_not_local_edit',
        'parent_scenario':spec.scenario.model_dump(),
        'target_scenario':target.scenario.model_dump(),
        'load_allocation':'preserved_by_bus' if candidate.loads==feeder.loads else 'redistributed_with_fixed_totals',
        'equipment':'resized_by_downstream_load_and_pv',
        'length_bounds_preserved':True,
        'scope':'Synthetic layout hypothesis; not real roads, planning approval or statistical calibration.'}
    return candidate,target
