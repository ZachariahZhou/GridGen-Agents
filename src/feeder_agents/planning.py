"""Deterministic validation of the Agent's structured experiment proposal."""
from .schemas import ExperimentPlan, ExperimentSpec


def build_plan(research_question: str, parameters: dict, inferred_fields: list[str] | None = None) -> ExperimentPlan:
    spec = ExperimentSpec.model_validate(parameters)
    inferred = set(inferred_fields or [])
    nested = {key:parameters.get(key,{}) for key in ('scenario','repair_policy','phase_design','equipment_design')}
    supplied = set(parameters) | {f'{group}.{key}' for group,value in nested.items() for key in value}
    if not inferred.issubset(supplied):
        raise ValueError('Inferred fields must be present in the supplied parameters')
    origins = {key: ('inferred' if key in inferred else 'user') if key in parameters else 'default'
               for key in ExperimentSpec.model_fields}
    for group,value in nested.items():
        for key in type(getattr(spec,group)).model_fields:
            origins[f'{group}.{key}'] = ('inferred' if group in inferred or f'{group}.{key}' in inferred else 'user') if key in value else 'default'
    return ExperimentPlan(research_question=research_question, spec=spec, parameter_origins=origins,
        assumptions=[
            'Scenario labels do not imply calibrated electrical values; empirical_tree calibrates only named marginals and retains explicit transfer priors.',
            f'Layout={spec.scenario.layout}; kind={spec.scenario.kind}; '+(
                f'village_count={spec.scenario.village_count or "automatic"}; 80% settlement load share; downstream-current conductor sizing.'
                if spec.scenario.layout == 'rural_villages' else
                f'profile={spec.scenario.calibration_profile}; empirical_weight={spec.scenario.empirical_weight}.'
                if spec.scenario.layout == 'empirical_tree' else
                f'topology={spec.scenario.topology.model_dump()}; normally_open_ties={spec.scenario.tie_count}; load_shape={spec.scenario.load_shape}.'
                if spec.scenario.layout == 'structured_radial' else
                f'effective aspect ratio={spec.scenario.aspect_ratio or (1 if spec.scenario.kind == "urban" else 6)}.'),
            f'Local km coordinates, {spec.phase_design.mode} {spec.voltage_kv:g} kV single-voltage snapshot, equipment provenance recorded; selected voltage-profile defaults are research assumptions.',
            f'Load placement={spec.scenario.load_placement}; reference={spec.scenario.reference_case_id}; planned total buses={planned_bus_range(spec)}. Node-count and positive-load-count defaults are separate; reference-derived defaults are transfer assumptions.',
            'Document rules are experiment-scoped candidate interpretations, not certified regulatory clauses.',
            'Independent seeded cases; controlled multi-factor or paired sweeps require separate explicit plans.',
        ])


def planned_bus_range(spec):
    if spec.n_buses is not None:
        return spec.n_buses,spec.n_buses
    if spec.scenario.load_placement=='reference_conditioned':
        import math
        from .load_placement import reference_occupancy
        fraction=reference_occupancy(spec.scenario.reference_case_id)['fraction']
        return tuple(math.ceil(n/fraction-1e-12)+1 for n in (spec.n_loads_min,spec.n_loads_max))
    return spec.n_loads_min+1,spec.n_loads_max+1
