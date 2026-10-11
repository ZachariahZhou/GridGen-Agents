"""Broad, constructive-witness response evaluation without search-answer leakage."""
import copy
from contextlib import contextmanager
import csv
import json
import math
from pathlib import Path
import random
import time

import networkx as nx

from ..artifacts import atomic_json, digest, file_digest
from ..response_design import apply_response_action, research_contract_preserved, score_response
from ..response_measurement import create_model, measure, resolve_probes
from ..response_schema import ResponseDesignPlan
from ..response_search import physical_fingerprint
from ..solver_accounting import SolverMeter, solver_phase
from .response_suite import select_ports, witness_actions, ATTEMPTS

PUBLIC = {'network_id', 'task_id', 'response_kind', 'coupling', 'witness_family', 'plan'}


def target_intervals(ratios, fraction):
    rows = []
    for key, center in ratios.items():
        if not math.isfinite(center) or center <= 0:
            raise ValueError('Invalid witness response')
        radius = max(.0005, fraction * abs(center - 1))
        rows.append(dict(probe_id=key, reference='baseline_ratio', lower=max(0., center-radius), upper=center+radius))
    if all(t['lower'] <= 1 <= t['upper'] for t in rows):
        raise ValueError('Witness task is trivial at the baseline')
    return rows


def components_agree(full, half, tolerance):
    for key, first in full['probes'].items():
        second = half['probes'].get(key, {})
        if not first.get('valid') or not second.get('valid'):
            return False
        a, b = first['signed_response'], second['signed_response']
        if set(a) != set(b):
            return False
        if any(not math.isfinite(v) or not math.isfinite(b[k]) or
               abs(v-b[k]) > 1e-7+tolerance*max(abs(v), abs(b[k])) for k, v in a.items()):
            return False
    return True


def public_payload(network_id, task_id, response_kind, coupling, witness_family, plan):
    return dict(network_id=network_id, task_id=task_id, response_kind=response_kind,
                coupling=coupling, witness_family=witness_family, plan=copy.deepcopy(plan))


def validate_public(task):
    if set(task) != PUBLIC:
        raise ValueError('Unexpected private or unknown task fields')
    return True


def core_artifacts(folder):
    folder = Path(folder)
    roots = ('selected', 'verification', 'baseline', 'baseline_model')
    files = {p for name in roots for p in (folder/name).rglob('*') if p.is_file()}
    files.update(folder/name for name in ('manifest.json', 'resolved_probes.json', 'history.json',
                                        'evaluations.json', 'solver_accounting.json', 'result.json') if (folder/name).is_file())
    return {str(p.relative_to(folder)): file_digest(p) for p in sorted(files)}


def verify_artifacts(folder, inventory):
    folder = Path(folder).resolve()
    for name, sha in inventory.items():
        path = (folder/name).resolve()
        if not path.is_relative_to(folder) or not path.is_file() or file_digest(path) != sha:
            raise ValueError('Changed or missing core artifact: '+name)


def network_designs():
    designs = []
    for n in (23, 37, 59, 79):
        for scene in ('urban', 'rural'):
            for replicate, kv in enumerate((6, 10, 20)):
                i = len(designs)
                designs.append(dict(network_id=f'd{i:02d}_{n}_{scene}_{kv}kv', domain='distribution',
                    spec=dict(network_kind='distribution', n_buses=n, voltage_kv=kv,
                        total_kw_min=10.*(n-1), total_kw_max=10.*(n-1), pv_ratio=.4, seed=97101101+71*i,
                        scenario=dict(kind=scene, engineering_profile=scene, layout='spatial_mst'),
                        phase_design=dict(mode='balanced' if replicate == 0 else 'unbalanced',
                                          single_phase_laterals=0, two_phase_laterals=0),
                        repair_policy=dict(strategy='none'))))
    for n in (57, 139, 237):
        for family in ('regional', 'corridor'):
            for replicate in range(2):
                i = len(designs)
                designs.append(dict(network_id=f't{i-24:02d}_{n}_{family}_{replicate}', domain='transmission',
                    spec=dict(network_kind='transmission', n_buses=n, n_generators=max(4, round(n/12)),
                        total_mw=8.*n, voltage_kv=110 if n == 57 else 220 if n == 139 else 500,
                        topology='meshed', mesh_family=family, radius_km=25 if n == 57 else 40,
                        seed=97101101+71*i, allowed_repairs=['parallel_line'])))
    return designs


def save_model(model, folder):
    folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
    if model['kind'] == 'distribution':
        atomic_json(folder/'feeder.json', model['feeder'].model_dump(mode='json'))
    else:
        from ..transmission import case_payload
        atomic_json(folder/'case.json', case_payload(model['case']))
        atomic_json(folder/'metadata.json', model['metadata'])


def load_model(folder, domain):
    folder = Path(folder)
    if domain == 'distribution':
        from ..schemas import Feeder
        return dict(kind=domain, feeder=Feeder.model_validate_json((folder/'feeder.json').read_text()))
    import numpy as np
    case = json.loads((folder/'case.json').read_text())
    for key in ('bus', 'gen', 'branch', 'gencost'):
        if key in case:
            case[key] = np.asarray(case[key], dtype=float)
    return dict(kind=domain, case=case, metadata=json.loads((folder/'metadata.json').read_text()))


def transmission_probes(model, count):
    graph = nx.Graph((str(int(e[0])), str(int(e[1]))) for e in model['case']['branch'] if e[10] > 0)
    pairs = []
    for a, distances in nx.all_pairs_shortest_path_length(graph):
        pairs.extend((distance, a, b) for b, distance in distances.items() if int(a) < int(b))
    pairs.sort(key=lambda x: (-x[0], int(x[1]), int(x[2])))
    selected = [pairs[0]]
    if count == 2:
        selected.append(next(p for p in pairs if not {p[1], p[2]} & {selected[0][1], selected[0][2]}))
    return [dict(id=chr(97+i), kind='transfer_p', injection_buses=[a], withdrawal_buses=[b],
                 step_mw=.5, aggregation='max_abs' if i == 0 else 'mean_abs')
            for i, (_, a, b) in enumerate(selected)]


def plans_for_network(design):
    domain = design['domain']
    from ..schemas import ExperimentSpec
    from ..transmission import TransmissionSpec
    spec = (ExperimentSpec if domain == 'distribution' else TransmissionSpec).model_validate(design['spec'])
    original = create_model(spec)
    plans = []
    if domain == 'distribution':
        search = dict(allowed_actions=['scale_layout', 'scale_subtree', 'replace_conductor'],
                      max_layout_scale_change=.5, max_branch_length_change=.5,
                      max_node_displacement_km=.2, max_changed_conductors=4)
        for ki, kind in enumerate(('voltage_q', 'voltage_p')):
            for coupling in ('low', 'high'):
                ports = select_ports(original, coupling == 'high')
                family = ('geometry', 'equipment', 'mixed')[(int(design['network_id'][1:3])+ki+(coupling == 'high')) % 3]
                probes = [dict(id=key, kind=kind, injection_buses=[ports[key]], monitor_buses=[ports[key]],
                               aggregation='mean_abs', step_mw=.01) for key in ('a', 'b')]
                plans.append((kind, coupling, family, probes, search, ports))
    else:
        for count in (1, 2):
            plans.append(('transfer_p', f'{count}_ports', 'circuits', transmission_probes(original, count),
                          dict(allowed_actions=['parallel_line']), None))
    out = []
    for kind, coupling, family, probes, search, ports in plans:
        plan = ResponseDesignPlan.model_validate(dict(research_question='Recover a response request certified feasible before search.',
            base_spec=spec.model_dump(mode='json'), probes=probes,
            targets=[dict(probe_id=p['id'], reference='baseline_ratio', lower=.9, upper=1.1) for p in probes],
            search=dict(search, max_rounds=32, candidates_per_round=8, max_joint_actions=2 if domain == 'distribution' else 1,
                        max_search_ac_calls=160, strategy='archive', archive_width=4)))
        task_id = design['network_id']+'_'+kind+'_'+coupling
        out.append((public_payload(design['network_id'], task_id, kind, coupling, family, plan.model_dump(mode='json')), ports))
    return original, out


def certify_task(original, task, ports, folder):
    folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
    plan = ResponseDesignPlan.model_validate(task['plan'])
    probes = resolve_probes(original, plan.probes)
    meter = SolverMeter(); attempts = []; certified = None
    with meter:
        baseline = measure(original, plan.base_spec, probes, folder/'baseline')
        valid = baseline['base_accepted'] and baseline['valid']
        if valid:
            for index in range(len(ATTEMPTS)):
                path = folder/f'attempt_{index:02d}'; entry = dict(attempt=index, accepted=False)
                try:
                    if original['kind'] == 'distribution':
                        trial, actions = witness_actions(original, plan, ports, task['witness_family'], index)
                    else:
                        edges = [i for i, e in enumerate(original['metadata']['branch_evidence'])
                                 if e['kind'] == 'line' and e['circuits'] < 4]
                        offset = round((.0, .33, .66, .95)[index]*(len(edges)-1))
                        actions = [dict(kind='parallel_line', index=edges[offset],
                                        value=original['metadata']['branch_evidence'][edges[offset]]['circuits']+1)]
                        if task['coupling'] == '2_ports':
                            other = edges[(offset+max(1, len(edges)//3)) % len(edges)]
                            actions.append(dict(kind='parallel_line', index=other,
                                                value=original['metadata']['branch_evidence'][other]['circuits']+1))
                        trial = original
                        for action in actions:
                            trial = apply_response_action(trial, plan.base_spec, action)
                    atomic_json(path/'actions.json', actions); save_model(trial, path/'model')
                    preserved = research_contract_preserved(original, trial, plan)
                    if not preserved:
                        raise ValueError('Witness violates the original modification contract')
                    measured = measure(trial, plan.base_spec, probes, path/'full')
                    half = measure(trial, plan.base_spec, probes, path/'half', step_factor=.5)
                    ratios = {p['id']: measured['probes'][p['id']]['value']/baseline['probes'][p['id']]['value'] for p in probes}
                    payload = copy.deepcopy(task['plan']); payload['targets'] = target_intervals(ratios, .15)
                    parsed = ResponseDesignPlan.model_validate(payload)
                    full_score = score_response(measured, parsed.targets, baseline)
                    half_score = score_response(half, parsed.targets, baseline)
                    agreement = components_agree(measured, half, parsed.verification_relative_tolerance)
                    entry.update(accepted=bool(full_score['target_met'] and half_score['target_met'] and agreement),
                                 ratios=ratios, preserved=preserved, consistent=agreement,
                                 full_targets=full_score['targets'], half_targets=half_score['targets'],
                                 witness_fingerprint=physical_fingerprint(trial))
                    if entry['accepted']:
                        certified = dict(task, plan=payload)
                except Exception as exc:
                    entry.update(error_type=type(exc).__name__, error=str(exc))
                attempts.append(entry); atomic_json(folder/'attempts.json', attempts)
                if certified is not None:
                    break
    certificate = dict(task_id=task['task_id'], certified=certified is not None, baseline_valid=valid,
                       original_fingerprint=physical_fingerprint(original), attempts=attempts,
                       accounting=meter.snapshot(), public_task_hash=digest(certified) if certified else None)
    atomic_json(folder/'certificate.json', certificate)
    return certified, certificate


@contextmanager
def response_adapter(arm, seed, domain):
    if domain == 'distribution':
        from .response_proposals import proposal_adapter
        with proposal_adapter(arm, seed):
            yield
        return
    import feeder_agents.response_design as runner
    previous = runner.propose_response_actions
    if arm == 'random':
        rng = random.Random(seed)
        def sampler(model, spec, probes, assessment, search, original=None, audit=None, **kwargs):
            actions = [dict(kind='parallel_line', index=i, value=e['circuits']+1,
                            diagnosis='Independent legal random circuit proposal')
                       for i, e in enumerate(model['metadata']['branch_evidence']) if e['kind'] == 'line' and e['circuits'] < 4]
            rng.shuffle(actions)
            return [dict(a, action_id=digest(a)[:16]) for a in actions[:search.candidates_per_round]]
        runner.propose_response_actions = sampler
    elif arm not in {'full', 'greedy'}:
        raise ValueError('Unsupported transmission arm')
    try:
        yield
    finally:
        runner.propose_response_actions = previous


def run_trial(task, arm, seed, output, budget=160):
    from ..response_design import run_response_design
    validate_public(task)
    payload = copy.deepcopy(task['plan']); domain = payload['base_spec']['network_kind']
    payload['search'].update(max_search_ac_calls=budget, compensation_mode='fixed' if arm == 'fixed' else 'bounded',
                             max_joint_actions=1 if arm == 'single' or domain == 'transmission' else 2,
                             selector='greedy' if arm == 'greedy' else 'heuristic')
    identity = digest(dict(task=task, arm=arm, seed=seed, budget=budget))[:24]
    root = Path(output)/'trials'/identity; root.mkdir(parents=True, exist_ok=False)
    atomic_json(root/'request.json', dict(public_task=task, arm=arm, seed=seed, budget=budget))
    row = {k: v for k, v in task.items() if k != 'plan'}
    row.update(job_id=identity, arm=arm, seed=seed, budget=budget, success=False,
               feasible=False, contract_preserved=False, directory=str(root), outcome='execution_error')
    start = time.monotonic(); meter = SolverMeter()
    try:
        with meter, response_adapter(arm, seed, domain):
            result = run_response_design(payload, root, 'design')
        row.update(success=result['target_met'], feasible=bool(result['selected']['base_accepted'] and result['selected']['valid']),
                   contract_preserved=result['protected_contract_preserved'], outcome=result['stop_reason'],
                   deficits=result['selected']['deficits'], deficit=sum(result['selected']['deficits']),
                   target_values=[t['observed'] for t in result['selected']['targets']],
                   changes=result['diagnosis']['design_space']['actual_changes'], result_hash=result['result_hash'])
        row['original_fingerprint'] = physical_fingerprint(load_model(root/'response_designs/design/baseline/model', domain))
    except Exception as exc:
        row.update(error_type=type(exc).__name__, error=str(exc))
    row.update(accounting=meter.snapshot(), seconds=time.monotonic()-start)
    atomic_json(root/'solver_accounting.json', dict(meter.snapshot(), events=meter.events))
    row['evidence_root'] = str(root/'response_designs/design')
    row['core_artifacts'] = core_artifacts(row['evidence_root'])
    row['search_ac_calls'] = row['accounting']['by_phase'].get('search', 0)
    row['total_ac_calls'] = row['accounting']['total_ac_calls']
    atomic_json(root/'row.json', row); atomic_json(root/'row_checksum.json', dict(sha256=digest(row)))
    return row
