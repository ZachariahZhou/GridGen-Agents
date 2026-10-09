"""Bounded deterministic inverse design with shared-network condition verification."""
import fcntl
import hashlib
import html
import json
import math
import re
import zipfile
import itertools
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .artifacts import atomic_json, digest
from .generation import generate_feeder
from .rules import data, load_rules
from .schemas import ExperimentSpec, Feeder, StrictModel
from .studies import make_variant, network_fingerprint
from .workflow import run_experiment, _runtime_fingerprint


class MetricGoal(StrictModel):
    metric: Literal['min_voltage_pu', 'max_voltage_pu', 'max_loading_ratio']
    operator: Literal['ge', 'le']
    threshold: float = Field(gt=0)


class OperatingCondition(StrictModel):
    name: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$')
    pv_ratio: float = Field(ge=0, le=3, description='PV capacity / unscaled baseline load kW')
    load_scale: float = Field(default=1, gt=0, le=5)
    goals: list[MetricGoal] = Field(min_length=1, max_length=12)


class InverseSearch(StrictModel):
    seed_candidates: list[int] = Field(min_length=1, max_length=16)
    geometry_scale_min: float = Field(gt=0, le=100)
    geometry_scale_max: float = Field(gt=0, le=100)
    grid_points: int = Field(default=9, ge=2, le=33)
    refinement_rounds: int = Field(default=2, ge=0, le=6)
    equipment_policies: list[Literal['frozen','conditional']] = Field(default_factory=lambda:['frozen'],min_length=1,max_length=2,
        description='conditional explicitly authorizes whole-tuple equipment reselection sized to all conditions.')
    pv_allocations: list[Literal['proportional','downstream','upstream']] = Field(default_factory=lambda:['proportional'],min_length=1,max_length=3,
        description='Authorized PV spatial capacity patterns; total PV and phase weights preserved in each condition.')

    @model_validator(mode='after')
    def ordered(self):
        if self.geometry_scale_min > self.geometry_scale_max:
            raise ValueError('Geometry scale minimum exceeds maximum')
        if len(set(self.seed_candidates)) != len(self.seed_candidates) or any(not 0 <= s < 2**32 for s in self.seed_candidates):
            raise ValueError('Seeds must be unique unsigned 32-bit integers')
        if len(set(self.equipment_policies))!=len(self.equipment_policies) or len(set(self.pv_allocations))!=len(self.pv_allocations):
            raise ValueError('Search policies must be unique')
        return self


class InverseDesignPlan(StrictModel):
    research_question: str = Field(min_length=1, max_length=4000)
    base_spec: ExperimentSpec
    conditions: list[OperatingCondition] = Field(min_length=2, max_length=16)
    search: InverseSearch
    protected_rule_ids: list[str] = Field(default_factory=lambda: ['cn.frequency', 'cn.power_factor', 'research.ampacity'])

    @model_validator(mode='after')
    def constraints(self):
        s = self.base_spec
        if s.count != 1 or s.max_repairs != 0 or s.repair_policy.strategy != 'none' or s.repair_policy.allow_rewire:
            raise ValueError('Inverse design requires count=1, repairs disabled and no rewiring')
        if len({c.name for c in self.conditions}) != len(self.conditions):
            raise ValueError('Condition names must be unique')
        if s.scenario.positions_km is not None and (self.search.geometry_scale_min != 1 or self.search.geometry_scale_max != 1):
            raise ValueError('User-fixed positions prohibit geometry scaling')
        known = set(load_rules()) | {r.rule_id for r in s.document_rules}
        if set(self.protected_rule_ids) - known:
            raise ValueError('Unknown protected rule ID')
        for condition in self.conditions:
            condition_spec(s, condition)
        if 'conditional' in self.search.equipment_policies:
            from .equipment import uses_reference
            if not uses_reference(s):raise ValueError('Conditional inverse equipment search requires reference equipment')
        return self


def condition_spec(base, condition):
    payload = base.model_dump()
    payload.update(total_kw_min=base.total_kw_min*condition.load_scale,
                   total_kw_max=base.total_kw_max*condition.load_scale,
                   pv_ratio=condition.pv_ratio/condition.load_scale)
    return ExperimentSpec.model_validate(payload)


def scale_geometry(base, factor):
    """Only coordinates and physical lengths are mutable; equipment remains frozen."""
    scaled = base.model_copy(deep=True)
    source = next(b for b in base.buses if b.id == base.source_bus)
    for bus in scaled.buses:
        bus.x_km = source.x_km + (bus.x_km-source.x_km)*factor
        bus.y_km = source.y_km + (bus.y_km-source.y_km)*factor
    for line in [*scaled.lines,*scaled.tie_lines]:
        line.length_km *= factor
    if scaled.design_evidence:
        scaled.design_evidence['inverse_geometry_provenance'] = {
            'scale': factor, 'base_model_hash': digest(base.model_dump()),
            'note': 'Other design evidence describes the unscaled initial seed design, not current geometry or measured power flow.'}
    scaled.assumptions.append(f'Inverse-design authorized uniform geometry scale: {factor:.12g}; equipment and demands frozen.')
    return scaled


def prepare_candidate(plan,base,factor,equipment_policy='frozen',pv_allocation='proportional'):
    if equipment_policy not in plan.search.equipment_policies or pv_allocation not in plan.search.pv_allocations:
        raise ValueError('Candidate changes are not authorized by search plan')
    candidate=scale_geometry(base,factor)
    if pv_allocation!='proportional':
        import networkx as nx
        graph=nx.Graph()
        graph.add_weighted_edges_from((e.bus1,e.bus2,e.length_km) for e in candidate.lines)
        distances=nx.single_source_dijkstra_path_length(graph,candidate.source_bus)
        maximum=max(distances.values())
        sign=1 if pv_allocation=='downstream' else -1
        raw={load.id:load.kw*math.exp(sign*2*distances[load.bus]/maximum) for load in candidate.loads}
        total=sum(raw.values())
        candidate.design_evidence['inverse_pv_allocation']={'policy':pv_allocation,
            'weights':{key:value/total for key,value in raw.items()},
            'method':'Baseline kW times exp(+/-2 * source path distance / maximum path distance), normalized; explicit research spatial prior, not fitted adoption statistics.'}
    if equipment_policy=='conditional':
        from .equipment import assign_equipment
        variants=[make_variant(candidate,c.pv_ratio,c.load_scale) for c in plan.conditions]
        assign_equipment(candidate,plan.base_spec,operating_feeders=variants)
        candidate.design_evidence['final_equipment_assignments']=[{'line_id':e.id,'equipment_id':e.conductor} for e in candidate.lines+candidate.tie_lines]
    candidate.design_evidence['inverse_decisions']={'equipment_policy':equipment_policy,'pv_allocation':pv_allocation,
        'geometry_scale':factor,'conditions_used_for_sizing':[c.name for c in plan.conditions] if equipment_policy=='conditional' else [],
        'changed_equipment':[e.id for e,b in zip(candidate.lines+candidate.tie_lines,base.lines+base.tie_lines) if e.conductor!=b.conductor]}
    return candidate


def score_goals(goals, metrics, eligible):
    constraints = []
    for goal in goals:
        value = metrics.get(goal.metric)
        finite = isinstance(value, (int, float)) and math.isfinite(value)
        deficit = ((goal.threshold-value) if goal.operator == 'ge' else (value-goal.threshold)) if finite else None
        constraints.append({**goal.model_dump(), 'actual': value if finite else None,
                            'signed_deficit': deficit, 'met': finite and deficit <= 0})
    return {'target_met': bool(eligible and all(c['met'] for c in constraints)),
            'constraints': constraints,
            'positive_deficit': sum(max(0, c['signed_deficit']) if c['signed_deficit'] is not None else 1e6 for c in constraints),
            'normalized_deficit': sum(max(0,c['signed_deficit'])/c['threshold'] if c['signed_deficit'] is not None else 1e6 for c in constraints)}


def _preserved(base, candidate, factor, equipment_policy='frozen'):
    expected = scale_geometry(base, factor)
    if equipment_policy=='conditional':
        for original,actual in zip(expected.lines+expected.tie_lines,candidate.lines+candidate.tie_lines):
            original.conductor=actual.conductor
    return candidate.model_dump(exclude={'design_evidence','assumptions'}) == expected.model_dump(exclude={'design_evidence','assumptions'})


def _candidate(plan, root, index, seed, factor, base, workers, equipment_policy='frozen', pv_allocation='proportional'):
    candidate_id = f'candidate_{index:04d}'
    folder = root/'candidates'/candidate_id
    folder.mkdir(parents=True, exist_ok=True)
    try:
        candidate=prepare_candidate(plan,base,factor,equipment_policy,pv_allocation)
    except ValueError as exc:
        row={'candidate_id':candidate_id,'seed':seed,'geometry_scale':factor,'equipment_policy':equipment_policy,
             'pv_allocation':pv_allocation,'conditions':[],'eligible':False,'target_met':False,
             'positive_deficit':None,'rejection_reason':str(exc)}
        atomic_json(folder/'result.json',row)
        return row
    geometry_legal = all(plan.base_spec.segment_km_min-1e-10 <= e.length_km <= plan.base_spec.segment_km_max+1e-10 for e in [*candidate.lines,*candidate.tie_lines])
    row = {'candidate_id': candidate_id, 'seed': seed, 'geometry_scale': factor,
           'equipment_policy':equipment_policy,'pv_allocation':pv_allocation,
           'design_decisions':candidate.design_evidence['inverse_decisions'],
           'model_hash': digest(candidate.model_dump()), 'network_hash': network_fingerprint(candidate),
           'preservation_verified': _preserved(base,candidate,factor,equipment_policy), 'geometry_legal': geometry_legal,
           'conditions': [], 'eligible': False, 'target_met': False, 'positive_deficit': None}
    if plan.base_spec.scenario.layout == 'empirical_tree':
        from .calibration import load_profile, distribution_distance
        profile = load_profile(plan.base_spec.scenario.calibration_profile)
        observations = [v for v in profile['distributions']['mv_segment_length_km']['values']
            if plan.base_spec.segment_km_min <= v <= plan.base_spec.segment_km_max]
        row['calibration_distance'] = {
            'metric': 'conditioned_segment_length_wasserstein_km',
            'baseline': distribution_distance([e.length_km for e in base.lines], observations),
            'candidate': distribution_distance([e.length_km for e in candidate.lines], observations),
            'reference_count': len(observations),
            'interpretation': 'Calibration applies to initialization only; authorized geometry scaling changes the length distribution. Electrical target success does not imply retained empirical realism.'}
    atomic_json(folder/'feeder.json', candidate.model_dump())
    if not geometry_legal:
        row['rejection_reason'] = 'Authorized scale violates original segment bounds; bounds were not relaxed.'
        atomic_json(folder/'result.json', row)
        return row
    protected = set(plan.protected_rule_ids) | {r.rule_id for r in plan.base_spec.document_rules}
    for condition in plan.conditions:
        variant = make_variant(candidate, condition.pv_ratio, condition.load_scale)
        spec = condition_spec(plan.base_spec, condition).model_copy(update={'seed': seed})
        outcome = run_experiment(spec, folder, condition.name, workers, initial_feeders=[variant])
        sample = outcome['samples'][0]
        directory = folder/'experiments'/condition.name/sample['sample_id']
        metrics = {}; checks = []; paired = False
        if sample.get('model_hash'):
            final = Feeder.model_validate_json((directory/'feeder.json').read_text())
            metrics = json.loads((directory/'simulation.json').read_text())
            checks = json.loads((directory/'validation.json').read_text())['checks']
            paired = network_fingerprint(final) == network_fingerprint(candidate) and final.loads == variant.loads
            catalogue = data('conductors.json')
            currents = metrics.get('line_current_a', {})
            metrics['max_loading_ratio'] = max((currents[e.id]/catalogue[e.conductor]['normamps'] for e in final.lines), default=None) if all(e.id in currents for e in final.lines) else None
        protected_pass = protected.issubset({c['rule_id'] for c in checks}) and all(c['status'] in {'pass', 'not_applicable'} for c in checks if c['rule_id'] in protected)
        eligible = bool(sample['valid'] and sample['solver_converged'] and paired and protected_pass and row['preservation_verified'])
        scored = score_goals(condition.goals, metrics, eligible)
        row['conditions'].append({'name': condition.name, 'pv_ratio': condition.pv_ratio,
            'load_scale': condition.load_scale, 'model_hash': sample.get('model_hash'),
            'network_hash': network_fingerprint(candidate), 'paired_verified': paired,
            'valid': sample['valid'], 'accepted': sample['accepted'], 'operational_pass': sample['operational_pass'],
            'solver_converged': sample['solver_converged'], 'protected_checks_pass': protected_pass,
            'eligible': eligible, 'checks': checks, **scored,
            'metrics': {k: metrics.get(k) for k in ('min_voltage_pu','max_voltage_pu','max_loading_ratio','loss_kw','source_kw','load_kw','generation_kw')},
            'relative_path': str(directory.relative_to(root))})
    row['eligible'] = all(c['eligible'] for c in row['conditions'])
    row['target_met'] = all(c['target_met'] for c in row['conditions'])
    row['positive_deficit'] = sum(c['positive_deficit'] for c in row['conditions'])
    row['normalized_deficit'] = sum(c['normalized_deficit'] for c in row['conditions'])
    from .joint_equipment import length_likelihood
    catalogue=data('conductors.json');cohorts=data('equipment_joint.json')['cohorts']
    from .equipment import required_currents
    roles=required_currents(candidate)
    support=[length_likelihood(catalogue[e.conductor].get('projection_from',e.conductor),roles[e.id][1],e.length_km,cohorts,plan.base_spec.equipment_design.length_bandwidth)
             for e in candidate.lines if 'source_feeder' in catalogue[e.conductor]]
    row['joint_reference_support']={'observed_length_fraction':sum(e['within_observed_range'] is True for e in support)/len(support) if support else None,
        'scope':'Descriptive support only; not a calibrated joint likelihood or proof of realism.',
        'equipment_evidence_geometry':'current candidate'}
    atomic_json(folder/'result.json', row)
    return row


def _rank(row):
    return (not row['target_met'], not row['eligible'], row.get('normalized_deficit',row['positive_deficit']) if row['positive_deficit'] is not None else 1e9,
            len(row.get('design_decisions',{}).get('changed_equipment',[])),row['candidate_id'])


def run_inverse_design(plan, workspace, design_id, workers=1):
    plan = InverseDesignPlan.model_validate(plan.model_dump() if isinstance(plan, InverseDesignPlan) else plan)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', design_id):
        raise ValueError('Invalid design_id')
    if not 1 <= workers <= 16:
        raise ValueError('workers must be between 1 and 16')
    root = Path(workspace).resolve()/'inverse_designs'/design_id
    root.mkdir(parents=True, exist_ok=True)
    with (root/'.lock').open('w') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('Inverse design already running') from exc
        manifest = {'plan': plan.model_dump(), 'runtime': _runtime_fingerprint(), 'rules': load_rules(plan.base_spec), 'conductors': data('conductors.json')}
        manifest['configuration_hash'] = digest(manifest)
        if (root/'manifest.json').exists() and json.loads((root/'manifest.json').read_text()) != manifest:
            raise ValueError('Inverse configuration/software changed; use a new design_id')
        atomic_json(root/'manifest.json', manifest)
        atomic_json(root/'plan.json', plan.model_dump())
        search = plan.search
        rows = []
        for seed in search.seed_candidates:
            sample_seed = int.from_bytes(hashlib.sha256(f'{seed}:0'.encode()).digest()[:4], 'big')
            base = generate_feeder(plan.base_spec.model_copy(update={'seed': seed}), sample_seed)
            atomic_json(root/'baselines'/f'{seed}.json', base.model_dump())
            lo, hi = search.geometry_scale_min, search.geometry_scale_max
            factors = [lo+(hi-lo)*i/(search.grid_points-1) for i in range(search.grid_points)]
            seen = set(); seed_rows = []
            for round_index in range(search.refinement_rounds+1):
                for factor in factors:
                    key = round(factor, 12)
                    if key in seen:
                        continue
                    seen.add(key)
                    for equipment_policy,pv_allocation in itertools.product(search.equipment_policies,search.pv_allocations):
                        row = _candidate(plan, root, len(rows), seed, factor, base, workers,equipment_policy,pv_allocation)
                        row['search_round']=round_index
                        atomic_json(root/'candidates'/row['candidate_id']/'result.json',row)
                        rows.append(row); seed_rows.append(row)
                        atomic_json(root/'progress.json', {'status': 'running', 'evaluated_candidates': len(rows), 'target_found': any(r['target_met'] for r in rows)})
                if any(r['target_met'] for r in seed_rows):
                    break
                best = min(seed_rows, key=_rank)['geometry_scale']
                ordered = sorted(seen)
                left = max((v for v in ordered if v < best), default=lo)
                right = min((v for v in ordered if v > best), default=hi)
                factors = [(left+best)/2, (best+right)/2]
        winner = min(rows, key=_rank)
        status = 'target_met' if winner['target_met'] else 'target_unmet'
        report = (f'Inverse design across conditions `{design_id}`: **{"target met" if winner["target_met"] else "target unmet"}**. '
            f'Evaluated {len(rows)} candidates and {len(plan.conditions)} operating conditions.\n\n'
            f'Inverse design `{design_id}`: **{status}** across {len(plan.conditions)} conditions. '
            f'Evaluated {len(rows)} candidates; selected `{winner["candidate_id"]}` (seed {winner["seed"]}, geometry scale {winner["geometry_scale"]:.6g}).\n\n'
            'Deterministic bounded grid search with local refinement; no learned or LLM search claim. '
            'A target is met only with a valid converged model, protected checks, frozen condition networks, and every explicit goal satisfied. '
            'Stress acceptance and operational compliance are reported separately. All document rules are protected.\n\n'
            f'Selected equipment policy: {winner["equipment_policy"]}; PV allocation: {winner["pv_allocation"]}. '
            'Only explicitly listed geometry, seed, equipment and PV spatial policies are searched; original segment bounds remain enforced. '
            'Conditional equipment candidates use whole observed tuples sized against the full operating-condition current envelope. '
            'Load allocation, total baseline demand and contracts are frozen within each seed. '
            'Across operating conditions only requested load/PV injections change. '
            'Search exhaustion does not prove global infeasibility; candidate count is an execution limit, not a scientific objective.\n\n'
            f'Artifacts: `{root}`. Selected condition links are in `index.html`; all metrics and signed deficits are in `result.json`.')
        if winner.get('calibration_distance'):
            distance = winner['calibration_distance']
            report += f'\n\nThe empirical length distribution is used only for initialization. Wasserstein distances before/after scaling, relative to reference observations within the original length bounds, are {distance["baseline"]:.6g} / {distance["candidate"]:.6g} km. Electrical target attainment does not imply that the final distribution still matches real feeders.'
        for condition in winner['conditions']:
            report += f'\n\nCondition `{condition["name"]}`: target met={condition["target_met"]}; model valid={condition["valid"]}; operational checks passed={condition["operational_pass"]}; pairing verified={condition["paired_verified"]}.\n'
            for constraint in condition['constraints']:
                operator = '>=' if constraint['operator'] == 'ge' else '<='
                report += f'\n- `{constraint["metric"]}` measured {constraint["actual"]}; required {operator} {constraint["threshold"]}; met={constraint["met"]}.'
        report += '\n\n`dataset.zip` includes the selected base model, DSS models/views/evidence for all conditions, the plan and report. The original directory retains the complete search history.'
        result = {'design_id': design_id, 'status': status, 'target_met': winner['target_met'], 'directory': str(root),
                  'configuration_hash': manifest['configuration_hash'], 'winner': winner, 'candidates': rows, 'verified_report': report}
        atomic_json(root/'result.json', result)
        atomic_json(root/'summary.json', result)
        (root/'report.md').write_text(report, encoding='utf-8')
        links = ''.join(f'<li>{html.escape(c["name"])}: target={c["target_met"]}, valid={c["valid"]}, operational={c["operational_pass"]} — <a href="{c["relative_path"]}/visualization.html">network</a> · <a href="{c["relative_path"]}/opendss/Master.dss">DSS</a></li>' for c in winner['conditions'])
        (root/'index.html').write_text(f'<!doctype html><meta charset="utf-8"><title>Inverse feeder design</title><h1>{html.escape(design_id)}: {status}</h1><p>Selected {winner["candidate_id"]}</p><ul>{links}</ul><a href="dataset.zip">Download selected dataset</a> · <a href="result.json">Evidence and constraints</a> · <a href="report.md">Report</a>', encoding='utf-8')
        with zipfile.ZipFile(root/'dataset.zip.tmp', 'w', zipfile.ZIP_DEFLATED) as archive:
            selected = [root/name for name in ('manifest.json', 'plan.json', 'result.json', 'summary.json', 'report.md', 'index.html')]
            selected.append(root/'baselines'/f'{winner["seed"]}.json')
            selected.extend((root/'candidates'/winner['candidate_id']).rglob('*'))
            for path in sorted(selected):
                if path.is_file() and path.name not in {'.lock', 'dataset.zip', 'dataset.zip.tmp'}:
                    archive.write(path, path.relative_to(root))
        (root/'dataset.zip.tmp').replace(root/'dataset.zip')
        atomic_json(root/'progress.json', {'status': status, 'evaluated_candidates': len(rows)})
        return result
