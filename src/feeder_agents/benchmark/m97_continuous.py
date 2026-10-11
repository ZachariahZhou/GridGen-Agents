"""Common two-subtree domain for guided proposals and independent optimizers."""
import copy
import math
from contextlib import contextmanager
from pathlib import Path
import time

import networkx as nx
import numpy as np

from ..response_design import apply_response_action


def apply_coordinates(original, spec, children, coordinates):
    state = original
    for child, value in zip(children, coordinates):
        state = apply_response_action(state, spec, dict(kind='scale_subtree', child_bus=child, factor=float(value)))
    return state


def recover_coordinates(original, candidate, children):
    tree = nx.bfs_tree(nx.Graph((e.bus1, e.bus2) for e in original['feeder'].lines), original['feeder'].source_bus)
    columns = []
    for child in children:
        moved = nx.descendants(tree, child)|{child}
        columns.append([int(e.bus1 in moved or e.bus2 in moved) for e in original['feeder'].lines])
    matrix = np.asarray(columns, dtype=float).T
    ratios = np.log([new.length_km/old.length_km for old, new in zip(original['feeder'].lines, candidate['feeder'].lines)])
    values, _, rank, _ = np.linalg.lstsq(matrix, ratios, rcond=None)
    if rank != len(children):
        raise ValueError('Dependent subtree variables')
    return np.exp(values).tolist(), float(np.max(np.abs(matrix@values-ratios)))


def action_uses_variables(action, children):
    if action['kind'] == 'compound':
        return all(action_uses_variables(a, children) for a in action['actions'])
    return action['kind'] == 'scale_subtree' and action.get('child_bus') in children


class EvaluationCache:
    def __init__(self, evaluate):
        self.evaluate = evaluate
        self.values = {}
        self.hits = 0

    def __call__(self, coordinates):
        key = tuple(float(x) for x in coordinates)
        if not all(math.isfinite(x) for x in key):
            raise ValueError('Nonfinite coordinates')
        if key in self.values:
            self.hits += 1
        else:
            self.values[key] = self.evaluate(key)
        return self.values[key]


@contextmanager
def restricted_proposer(children):
    import feeder_agents.response_design as runner
    import feeder_agents.response_flexibility as flexibility
    import feeder_agents.response_compensation as compensation
    previous = runner.propose_response_actions
    previous_local, previous_compensation = flexibility.distribution_actions, compensation.compensation_actions
    flexibility.distribution_actions = lambda *args, **kwargs: [a for a in previous_local(*args, **kwargs) if action_uses_variables(a, children)]
    compensation.compensation_actions = lambda *args, **kwargs: [a for a in previous_compensation(*args, **kwargs) if action_uses_variables(a, children)]
    def propose(model, spec, probes, assessment, search, original=None, audit=None, **kwargs):
        def eligible(action):
            if not action_uses_variables(action, children):
                return False
            trial = apply_response_action(model, spec, action)
            coordinates, residual = recover_coordinates(original, trial, children)
            return residual < 1e-8 and all(.5-1e-10 <= x <= 1.5+1e-10 for x in coordinates)
        return previous(model, spec, probes, assessment, search, original, audit,
                        **dict(kwargs, candidate_filter=eligible))
    runner.propose_response_actions = propose
    try:
        yield
    finally:
        runner.propose_response_actions = previous
        flexibility.distribution_actions, compensation.compensation_actions = previous_local, previous_compensation


def geometry_margin(original, trial, spec, search):
    margins = []
    for old, new in zip(original['feeder'].lines, trial['feeder'].lines):
        ratio = new.length_km/old.length_km
        margins.extend([ratio-(1-search.max_branch_length_change), 1+search.max_branch_length_change-ratio,
                        (new.length_km-spec.segment_km_min)/old.length_km,
                        (spec.segment_km_max-new.length_km)/old.length_km])
    for old, new in zip(original['feeder'].buses, trial['feeder'].buses):
        margins.append(1-math.hypot(new.x_km-old.x_km, new.y_km-old.y_km)/search.max_node_displacement_km)
    return min(margins)


def run_continuous(public, arm, seed, output, budget=160):
    from scipy.optimize import Bounds, NonlinearConstraint, minimize
    from ..artifacts import atomic_json, digest
    from ..response_schema import ResponseDesignPlan
    from ..response_design import run_response_design, score_response, research_contract_preserved, _export
    from ..response_measurement import create_model, measure, resolve_probes
    from ..response_search import physical_fingerprint, response_priority
    from ..solver_accounting import SolverMeter, solver_phase, ACSearchBudgetExceeded
    from .m97_response import components_agree, load_model, save_model, validate_public, core_artifacts

    if set(public) != {'task', 'children', 'bounds'} or public['bounds'] != [[.5, 1.5], [.5, 1.5]]:
        raise ValueError('Common continuous parameterization changed')
    task = public['task']; children = public['children']; payload = copy.deepcopy(task['plan'])
    validate_public(task)
    payload['search']['max_search_ac_calls'] = budget
    plan = ResponseDesignPlan.model_validate(payload)
    identity = digest(dict(public=public, arm=arm, seed=seed, budget=budget))[:24]
    root = Path(output)/'continuous_trials'/identity; root.mkdir(parents=True, exist_ok=False)
    atomic_json(root/'request.json', dict(public=public, arm=arm, seed=seed, budget=budget))
    row = dict(job_id=identity, task_id=task['task_id'], network_id=task['network_id'], arm=arm, seed=seed,
               budget=budget, success=False, feasible=False, contract_preserved=False, directory=str(root))
    started = time.monotonic(); meter = SolverMeter(budget); evaluations = []; cache_hits = 0
    try:
        with meter:
            if arm == 'guided':
                with restricted_proposer(children):
                    result = run_response_design(plan, root, 'design')
                row.update(success=result['target_met'], feasible=bool(result['selected']['base_accepted'] and result['selected']['valid']),
                           contract_preserved=result['protected_contract_preserved'], deficit=sum(result['selected']['deficits']),
                           target_values=[t['observed'] for t in result['selected']['targets']], outcome=result['stop_reason'],
                           result_hash=result['result_hash'])
                row['original_fingerprint'] = physical_fingerprint(load_model(root/'response_designs/design/baseline/model', 'distribution'))
            else:
                with solver_phase('construction'):
                    original = create_model(plan.base_spec)
                row['original_fingerprint'] = physical_fingerprint(original)
                save_model(original, root/'baseline_model')
                probes = resolve_probes(original, plan.probes)
                with solver_phase('baseline'):
                    base = measure(original, plan.base_spec, probes, root/'baseline')
                incumbent = dict(model=original, score=score_response(base, plan.targets, base), coordinates=[1., 1.])
                seen = {physical_fingerprint(original): incumbent['score']}

                class TargetFound(Exception):
                    pass

                def evaluate(coordinates):
                    nonlocal incumbent, cache_hits
                    model = apply_coordinates(original, plan.base_spec, children, coordinates)
                    margin = geometry_margin(original, model, plan.base_spec, plan.search)
                    preserved = research_contract_preserved(original, model, plan)
                    if not preserved:
                        return dict(objective=1e3+max(0., -margin), constraints=[min(margin, -1e-9), 1.])
                    identity = physical_fingerprint(model)
                    if identity in seen:
                        score = seen[identity]; cache_hits += 1
                    else:
                        with solver_phase('search'):
                            measured = measure(model, plan.base_spec, probes, root/'evaluations'/f'{len(evaluations):04d}')
                        score = score_response(measured, plan.targets, base); seen[identity] = score
                    physical = bool(score['base_accepted'] and score['valid'])
                    evaluations.append(dict(coordinates=list(coordinates), physical=physical,
                                            deficit=sum(score['deficits']), target_met=score['target_met'],
                                            calls=meter.snapshot()['by_phase'].get('search', 0)))
                    if response_priority(score) < response_priority(incumbent['score']):
                        incumbent = dict(model=model, score=score, coordinates=list(coordinates))
                    if score['target_met']:
                        raise TargetFound()
                    return dict(objective=sum(score['deficits']) if physical else 1e3,
                                constraints=[margin, 1. if physical else -1.])

                cached = EvaluationCache(evaluate)
                try:
                    if arm == 'cobyqa':
                        result = minimize(lambda x: cached(x)['objective'], np.ones(2), method='COBYQA',
                            bounds=Bounds([.5, .5], [1.5, 1.5]),
                            constraints=NonlinearConstraint(lambda x: cached(x)['constraints'], [0., 0.], [np.inf, np.inf]),
                            options=dict(maxfev=1000, maxiter=1000, initial_tr_radius=.1, final_tr_radius=1e-5,
                                         feasibility_tol=1e-9, disp=False))
                        row['optimizer_status'] = str(result.message)
                    elif arm == 'uniform':
                        rng = np.random.default_rng(seed)
                        for _ in range(5000):
                            cached(rng.uniform(.5, 1.5, 2))
                    else:
                        raise ValueError('Unknown continuous arm')
                except TargetFound:
                    row['optimizer_status'] = 'Target found; independent final verification follows'
                except ACSearchBudgetExceeded:
                    row['optimizer_status'] = 'Actual search AC cap exhausted'
                with solver_phase('verification'):
                    half = measure(incumbent['model'], plan.base_spec, probes, root/'verification', step_factor=.5)
                hs = score_response(half, plan.targets, base)
                score = incumbent['score']; preserved = research_contract_preserved(original, incumbent['model'], plan)
                accepted = bool(score['target_met'] and hs['target_met'] and preserved and
                                components_agree(score, half, plan.verification_relative_tolerance))
                with solver_phase('export'):
                    _export(incumbent['model'], plan.base_spec, root/'selected', score, accepted=accepted)
                atomic_json(root/'selected/assessment.json', score)
                atomic_json(root/'verification/checks.json', dict(accepted=accepted, contract_preserved=preserved,
                    full_target_met=score['target_met'], half_target_met=hs['target_met'],
                    component_agreement=components_agree(score, half, plan.verification_relative_tolerance)))
                row.update(success=accepted, feasible=bool(score['base_accepted'] and score['valid']),
                           contract_preserved=preserved, deficit=sum(score['deficits']), coordinates=incumbent['coordinates'],
                           target_values=[t['observed'] for t in score['targets']], outcome='accepted' if accepted else 'target_unmet',
                           cache_hits=cache_hits+cached.hits)
                atomic_json(root/'evaluations.json', evaluations)
    except Exception as exc:
        row.update(outcome='execution_error', error_type=type(exc).__name__, error=str(exc))
    accounting = meter.snapshot()
    atomic_json(root/'solver_accounting.json', dict(accounting, events=meter.events))
    row['evidence_root'] = str(root/'response_designs/design' if arm == 'guided' else root)
    row['core_artifacts'] = core_artifacts(row['evidence_root'])
    row.update(accounting=accounting, total_ac_calls=accounting['total_ac_calls'],
               search_ac_calls=accounting['by_phase'].get('search', 0), seconds=time.monotonic()-started)
    atomic_json(root/'row.json', row); atomic_json(root/'row_checksum.json', dict(sha256=digest(row)))
    return row
