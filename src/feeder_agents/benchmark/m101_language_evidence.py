"""Fresh same-source outer-agent evidence with explicit, evaluator-only oracles.

Live workers reuse the production agent_session recorder and read public messages
only. Certificates, observations, turn requirements and scoring stay outside it.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from feeder_agents.artifacts import atomic_json, digest, file_digest
from .m97_language import public_case, required_checks, walk_dicts
from .m97_language_runtime import SETTINGS

ARMS = ['one_proposal', 'full_outer']
OUTCOMES = {'generation', 'clarification', 'unsupported', 'truthful_unmet', 'bounded_outcome'}
_GRADE_LOCK = threading.Lock()


def read(path):
    return json.loads(Path(path).read_text())


def model_payload(model):
    """Canonical arrays rather than the recorder's display-only string fallback."""
    if hasattr(model, 'model_dump'):
        return model_payload(model.model_dump())
    if hasattr(model, 'tolist'):
        return model.tolist()
    if isinstance(model, dict):
        return {key: model_payload(value) for key, value in model.items()}
    if isinstance(model, (list, tuple)):
        return [model_payload(value) for value in model]
    return model


def domain_for(case):
    spec = case.get('witness_spec') or {}
    return case.get('domain') or ('hierarchy' if case.get('hierarchy') or 'transformer_count' in spec
        else 'transmission' if spec.get('network_kind') == 'transmission' else 'distribution')


def validate_cases(cases):
    cases = copy.deepcopy(cases.get('cases') if isinstance(cases, dict) else cases)
    if not isinstance(cases, list) or not cases:
        raise ValueError('Cases must be a nonempty list')
    seen = set()
    for case in cases:
        for field in ('id', 'category', 'messages', 'expected_outcome', 'witness_spec', 'requirements'):
            if field not in case:
                raise ValueError(f'Missing case field: {field}')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', case['id']) or '__' in case['id'] or case['id'] in seen:
            raise ValueError('Case IDs must be unique safe identifiers without double underscores')
        seen.add(case['id'])
        if case['expected_outcome'] not in OUTCOMES:
            raise ValueError('Unknown expected_outcome')
        if not isinstance(case['messages'], list) or not case['messages'] or not all(isinstance(s, str) and s.strip() for s in case['messages']):
            raise ValueError('messages must contain complete nonempty user turns')
        if not isinstance(case['requirements'], dict):
            raise ValueError('requirements must be an independently authored object')
        if case['expected_outcome'] == 'generation' and not case['witness_spec']:
            raise ValueError('Positive cases require witness_spec')
        case['domain'] = domain_for(case)
        if case['domain'] not in {'distribution', 'hierarchy', 'transmission'}:
            raise ValueError('Unknown domain')
        if case.get('revision_actions'):
            if case['domain'] != 'distribution':
                raise ValueError('Explicit local revision oracle supports single-voltage distribution only')
            if len(case['revision_actions']) != len(case['messages']) - 1:
                raise ValueError('revision_actions must align with messages[1:]')
            if len(case.get('turn_requirements', [])) != len(case['messages']):
                raise ValueError('turn_requirements must include initial and every revision turn')
            if not case.get('preserve_groups'):
                raise ValueError('Revision oracle requires explicit preserve_groups')
            if isinstance(case['preserve_groups'][0], list) and len(case['preserve_groups']) != len(case['revision_actions']):
                raise ValueError('Per-turn preserve_groups must align with revision_actions')
            if any(not isinstance(actions, list) or not actions for actions in case['revision_actions']):
                raise ValueError('Each revision_actions entry must contain explicit actions')
            case['revision'] = True
        elif 'turn_requirements' in case and len(case['turn_requirements']) != len(case['messages']):
            raise ValueError('turn_requirements must align with all messages')
        case.setdefault('cluster_id', case['id'])
        case.setdefault('source_spans', [{'turn': i, 'text': message} for i, message in enumerate(case['messages'])])
    return cases


def check_requirements(expected, observed):
    checks = required_checks({key: value for key, value in expected.items() if key != 'radius_km'}, observed)
    if 'radius_km' in expected:
        actual, bound = observed.get('radius_km'), expected['radius_km']
        checks['radius_km'] = isinstance(actual, (int, float)) and math.isfinite(actual) and actual <= bound + 1e-7
    return checks


def observations(model, spec, domain, metadata=None):
    """Measured model fields; scene/seed provenance is explicitly configuration evidence."""
    if domain == 'transmission':
        import numpy as np
        buses, generators = np.asarray(model['bus']), np.asarray(model['gen'])
        positions = (metadata or {}).get('positions_km', {})
        valid = (len(positions) == len(buses) and {str(int(b[0])) for b in buses} == set(map(str, positions))
            and all(isinstance(xy, (list, tuple)) and len(xy) == 2 and all(isinstance(v, (int, float)) and math.isfinite(v) for v in xy) for xy in positions.values()))
        return dict(n_buses=len(buses), n_generators=len(generators), total_mw=float(buses[:, 2].sum()),
            voltage_kv=float(buses[0, 9]) if len(set(buses[:, 9])) == 1 else None,
            radius_km=max((math.hypot(*xy) for xy in positions.values()), default=None) if valid else None,
            phase_mode='balanced', seed=spec.get('seed'), count=spec.get('count', 1),
            allowed_repairs=spec.get('allowed_repairs'))
    model = model.model_dump() if hasattr(model, 'model_dump') else model
    total = sum(load['kw'] for load in model['loads'])
    facts = dict(n_buses=len(model['buses']), voltage_kv=model['voltage_kv'], total_kw=total,
        pv_ratio=sum(load['pv_kw'] for load in model['loads']) / total if total else None,
        phase_mode=model['phase_mode'], n_loads=len(model['loads']), seed=spec.get('seed'), count=spec.get('count', 1),
        repair_strategy=spec.get('repair_policy', {}).get('strategy'),
        scene=spec.get('scene') if domain == 'hierarchy' else spec.get('scenario', {}).get('kind'))
    facts['phase_resolved'] = bool(model['loads']) and all(load.get('phase_powers') and
        set(p['phase'] for p in load['phase_powers']) == set(load['phases']) for load in model['loads'])
    if domain == 'hierarchy':
        import networkx as nx
        lv = {bus['voltage_kv'] for bus in model['buses'] if bus['voltage_kv'] < 1}
        counts = Counter(load['phases'][0] for load in model['loads'] if len(load['phases']) == 1)
        buses = {bus['id']: bus for bus in model['buses']}
        graph = nx.Graph()
        graph.add_nodes_from(buses)
        graph.add_edges_from((line['bus1'], line['bus2']) for line in model['lines'])
        branch_counts = [sum(buses[node].get('role') == 'lv_branch'
            for node in nx.node_connected_component(graph, transformer['bus2'])) for transformer in model['transformers']]
        facts.update(lv_voltage_kv=next(iter(lv)) if len(lv) == 1 else None,
            transformer_count=len(model['transformers']), users=len(model['loads']),
            mv_buses=sum(bus['voltage_kv'] >= 1 for bus in model['buses']),
            lv_branches=branch_counts[0] if branch_counts and len(set(branch_counts)) == 1 else None,
            lv_branch_counts=branch_counts, phase_customer_counts=[counts[p] for p in (1, 2, 3)])
    return facts


def protected_groups(case, turn):
    groups = case.get('preserve_groups', [])
    if groups and isinstance(groups[0], list):
        groups = groups[turn - 1]
    return ['loads' if group == 'load' else group for group in groups]


def revision_plan(case, turn):
    from feeder_agents.revisions import RevisionPlan
    actions = copy.deepcopy(case['revision_actions'][turn - 1])
    for action in actions:
        action.setdefault('evidence', case['messages'][turn])
    return RevisionPlan(feedback=case['messages'][turn], actions=actions, preserve=protected_groups(case, turn))


def revision_checks(case, before, after, parent_spec, turn):
    from feeder_agents.revisions import _groups
    original, observed = _groups(before), _groups(after)
    checks = {f'preserved_{group}': group in original and group in observed and original[group] == observed[group]
              for group in protected_groups(case, turn)}
    actions = {action['kind']: action for action in case['revision_actions'][turn - 1]}
    supported = {'load_scale', 'pv_ratio', 'geometry_scale'}
    checks['supported_explicit_actions'] = bool(actions) and set(actions) <= supported
    old_loads, new_loads = ({load.id: load for load in model.loads} for model in (before, after))
    checks['load_identities'] = old_loads.keys() == new_loads.keys()
    same = lambda a, b: math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-8)
    factor = actions.get('load_scale', {}).get('value', 1.)
    if checks['load_identities']:
        checks['per_load_pq_scale'] = all(same(getattr(new_loads[key], field), getattr(load, field) * factor)
            for key, load in old_loads.items() for field in ('kw', 'kvar'))
        old_phases = {(key, power.phase): power for key, load in old_loads.items() for power in load.phase_powers}
        new_phases = {(key, power.phase): power for key, load in new_loads.items() for power in load.phase_powers}
        checks['phase_power_identities'] = old_phases.keys() == new_phases.keys()
        checks['per_phase_pq_scale'] = checks['phase_power_identities'] and all(
            same(getattr(new_phases[key], field), getattr(power, field) * factor)
            for key, power in old_phases.items() for field in ('kw', 'kvar'))
        if 'pv_ratio' in actions:
            ratio = actions['pv_ratio']['value']
            checks['per_load_pv_ratio'] = all(same(load.pv_kw, load.kw * ratio) for load in new_loads.values())
            # The authored combined scale operation preserves the prior PV ratio;
            # its source explicitly multiplies each phase PV, not just total PV.
            if 'load_scale' in actions and same(ratio, parent_spec['pv_ratio']):
                checks['per_phase_pv_scale'] = checks['phase_power_identities'] and all(
                    same(new_phases[key].pv_kw, power.pv_kw * factor) for key, power in old_phases.items())
        else:
            checks['per_load_pv_preserved'] = all(same(new_loads[key].pv_kw, load.pv_kw) for key, load in old_loads.items())
            checks['per_phase_pv_preserved'] = checks['phase_power_identities'] and all(
                same(new_phases[key].pv_kw, power.pv_kw) for key, power in old_phases.items())
    if 'geometry_scale' in actions:
        scale = actions['geometry_scale']['value']
        old_buses, new_buses = ({bus.id: bus for bus in model.buses} for model in (before, after))
        origin = old_buses[before.source_bus]
        checks['coordinate_scaling'] = old_buses.keys() == new_buses.keys() and all(
            same(getattr(new_buses[key], coordinate), getattr(origin, coordinate) + scale * (getattr(bus, coordinate) - getattr(origin, coordinate)))
            for key, bus in old_buses.items() for coordinate in ('x_km', 'y_km'))
        old_lines, new_lines = ({line.id: line for line in [*model.lines, *model.tie_lines]} for model in (before, after))
        checks['line_length_scaling'] = old_lines.keys() == new_lines.keys() and all(
            same(new_lines[key].length_km, line.length_km * scale) for key, line in old_lines.items())
    return checks


def typed_spec(case):
    from feeder_agents.schemas import ExperimentSpec
    from feeder_agents.transmission import TransmissionSpec
    from feeder_agents.hierarchy import HierarchicalSpec
    cls = {'distribution': ExperimentSpec, 'transmission': TransmissionSpec, 'hierarchy': HierarchicalSpec}[domain_for(case)]
    return cls.model_validate(case['witness_spec'])


def physical_check(model, spec, domain, folder, metadata=None, saved_export=None, response=False):
    """Fresh solver acceptance of the actual export, with actual injection checks."""
    folder = Path(folder)
    if domain == 'transmission':
        from .m97_construction import audit_transmission_export
        from feeder_agents.transmission import export_case
        folder.mkdir(parents=True, exist_ok=True)
        path = Path(saved_export) if saved_export else folder / 'case_generated.m'
        if not saved_export:
            export_case(model, path)
        result = audit_transmission_export(path, model_payload(model), metadata, spec)
        return dict(accepted=result['physical_pass'] and result['export_pass'], checks=result['checks'],
                    metrics=result['descriptors'], export=str(path))
    from feeder_agents.simulation import simulate, export_dss
    from feeder_agents.injection_contract import element_injections_match
    from .m97_construction import audit_model
    if domain == 'hierarchy':
        from feeder_agents.hierarchy import export_hierarchy, evaluate_hierarchy, HierarchicalSpec
        master = Path(saved_export) if saved_export else export_hierarchy(model, folder / 'opendss')
        simulation = simulate(master)
        physical = evaluate_hierarchy(model, HierarchicalSpec.model_validate(spec), simulation)
    else:
        from feeder_agents.evaluation import evaluate, verdict
        from feeder_agents.schemas import ExperimentSpec
        master = Path(saved_export) if saved_export else export_dss(model, folder / 'opendss')
        simulation = simulate(master)
        typed = ExperimentSpec.model_validate(spec)
        physical = verdict(evaluate(model, typed, simulation), typed.mode)
    static = audit_model(model.model_dump(), spec, domain)
    if response:
        # A permitted response edit may change default segment lengths. Source
        # permissions are audited separately; defaults are not user constraints.
        static['checks'].pop('line_lengths', None)
    checks = dict(static['checks'], physical_accepted=bool(physical['accepted']),
        exported_element_injections=all(element_injections_match(model, simulation,
            split_phases=domain == 'distribution' and model.phase_mode == 'unbalanced').values()))
    checks.update(export_identity_checks(master, model))
    atomic_json(folder / 'fresh_simulation.json', simulation)
    return dict(accepted=all(checks.values()), checks=checks, export=str(master),
                metrics={key: simulation.get(key) for key in ('min_voltage_pu', 'max_voltage_pu', 'max_vuf_percent', 'loss_kw')})


def export_identity_checks(master, model):
    """Inspect real DSS objects using an explicitly owned native context."""
    from feeder_agents.simulation import _fresh_dss_context
    from feeder_agents.solver_accounting import record_ac_solve
    with _fresh_dss_context() as engine:
        engine.Basic.AllowChangeDir(False)
        record_ac_solve('opendss')
        engine.Text.Command(f'Compile "{Path(master).resolve()}"')
        checks = dict(export_bus_ids=set(engine.Circuit.AllBusNames()) == {bus.id.casefold() for bus in model.buses})
        checks['export_bus_phases'] = True
        for bus in model.buses:
            engine.Circuit.SetActiveBus(bus.id)
            checks['export_bus_phases'] &= set(engine.Bus.Nodes()) - {0} == set(bus.phases)
        def terminal(value):
            parts = value.casefold().split('.')
            return parts[0], tuple(sorted(int(node) for node in parts[1:] if node != '0'))
        expected_loads, expected_pv = {}, {}
        split = model.phase_mode == 'unbalanced' and not hasattr(model, 'transformers')
        for load in model.loads:
            parts = [(f'{load.id}_p{power.phase}', [power.phase], power.pv_kw) for power in load.phase_powers] if split else [
                (load.id, load.phases, load.pv_kw)]
            for name, phases, pv in parts:
                target = (load.bus.casefold(), tuple(sorted(phases)), len(phases))
                expected_loads[name.casefold()] = target
                if pv > 0:
                    expected_pv['pv_' + name.casefold()] = target
        actual_loads, actual_pv = {}, {}
        for load in engine.Loads:
            bus, phases = terminal(engine.CktElement.BusNames()[0])
            actual_loads[load.Name()] = (bus, phases, engine.CktElement.NumPhases())
        for generator in engine.Generators:
            bus, phases = terminal(engine.CktElement.BusNames()[0])
            actual_pv[generator.Name()] = (bus, phases, engine.CktElement.NumPhases())
        checks['export_load_locations_phases'] = actual_loads == expected_loads
        checks['export_pv_locations_phases'] = actual_pv == expected_pv
        expected = {line.id.casefold(): line for line in [*model.lines, *model.tie_lines]}
        actual = {}
        for line in engine.Lines:
            actual[line.Name()] = dict(buses=[bus.split('.')[0] for bus in engine.CktElement.BusNames()],
                terminal_phases=[terminal(bus)[1] for bus in engine.CktElement.BusNames()],
                phases=engine.CktElement.NumPhases(), length=line.Length())
        checks['export_line_connections_lengths_phases'] = actual.keys() == expected.keys() and all(
            actual[key]['buses'] == [line.bus1.casefold(), line.bus2.casefold()] and
            actual[key]['terminal_phases'] == [tuple(sorted(line.phases))] * 2 and
            actual[key]['phases'] == len(line.phases) and math.isclose(actual[key]['length'], line.length_km, rel_tol=1e-9, abs_tol=1e-10)
            for key, line in expected.items())
        transformers = getattr(model, 'transformers', [])
        checks['export_transformer_ids'] = set(engine.Transformers.AllNames()) - {'none'} == {item.id.casefold() for item in transformers}
        expected_transformers = {item.id.casefold(): [item.bus1.casefold(), item.bus2.casefold()] for item in transformers}
        actual_transformers = {}
        for transformer in engine.Transformers:
            actual_transformers[transformer.Name()] = [terminal(bus)[0] for bus in engine.CktElement.BusNames()]
        checks['export_transformer_terminals'] = actual_transformers == expected_transformers
        return checks


def certify(case, root):
    """Fixed independent construction/actions before any compared model output."""
    folder = Path(root) / case['id']
    folder.mkdir(parents=True, exist_ok=True)
    if case['expected_outcome'] != 'generation':
        result = dict(required=False, certified=False)
        atomic_json(folder / 'certificate.json', result)
        return result
    try:
        if case.get('task'):
            result = certify_research(case, folder)
        else:
            spec = typed_spec(case)
            actual_seed = int.from_bytes(hashlib.sha256(f'{spec.seed}:0'.encode()).digest()[:4], 'big')
            domain = domain_for(case)
            metadata = None
            if domain == 'hierarchy':
                from feeder_agents.hierarchy import generate_hierarchy
                model = generate_hierarchy(spec, actual_seed)
            else:
                from feeder_agents.response_measurement import create_model
                built = create_model(spec.model_copy(update={'seed': actual_seed}))
                model, metadata = built.get('feeder', built.get('case')), built.get('metadata')
            steps = []
            expectations = case.get('turn_requirements') or [case['requirements']]
            for turn, expected in enumerate(expectations):
                parent = None
                if turn:
                    from feeder_agents.revisions import apply_revision
                    parent, parent_spec = model, spec.model_dump()
                    model, spec, _ = apply_revision(parent, spec, revision_plan(case, turn))
                checked = physical_check(model, spec.model_dump(), domain, folder / f'turn_{turn:02d}', metadata)
                facts = observations(model, spec.model_dump(), domain, metadata)
                checks = dict(checked['checks'], **{'source_' + k: v for k, v in check_requirements(expected, facts).items()})
                if parent is not None:
                    checks.update(revision_checks(case, parent, model, parent_spec, turn))
                atomic_json(folder / f'turn_{turn:02d}/model.json', model_payload(model))
                steps.append(dict(turn=turn, facts=facts, checks=checks, passed=all(checks.values())))
            result = dict(required=True, certified=all(step['passed'] for step in steps), steps=steps,
                physical_seed=actual_seed, method='Fixed authored construction and explicit revision actions; no search or live-model selection.')
    except Exception as exc:
        result = dict(required=True, certified=False, error=f'{type(exc).__name__}: {exc}')
    atomic_json(folder / 'certificate.json', result)
    return result


def certify_research(case, folder):
    from feeder_agents.research_tasks import compile_research_task
    from feeder_agents.research_measurement import evaluate_research_task
    from feeder_agents.response_schema import ResponseDesignPlan
    from feeder_agents.response_measurement import create_model, resolve_probes, measure
    from feeder_agents.response_design import apply_response_action, score_response, research_contract_preserved
    from .m97_language import unauthorized_actions
    spec = typed_spec(case)
    lower, upper = case['response_interval']
    action = case.get('witness_action', dict(kind='scale_layout', factor=1.2, original_scale=1.2) if lower > 1 else None)
    search = case.get('research_search', dict(allowed_actions=['scale_layout'] if action else []))
    if action and 'research_search' not in case:
        search['max_layout_scale_change'] = case.get('max_layout_scale_change', .3)
    task = dict(task=case['task'], research_question=case['messages'][0], base_spec=spec.model_dump(),
        response_lower=lower, response_upper=upper, preserve_equipment=True,
        support_mvar=case.get('support_mvar', .01), transfer_mw=case.get('transfer_mw', 1.), search=search)
    compiled = compile_research_task(task)
    plan = ResponseDesignPlan.model_validate(compiled['response_plan'])
    original = create_model(plan.base_spec)
    probes = resolve_probes(original, plan.probes)
    reference = measure(original, plan.base_spec, probes, folder / 'reference')
    witness = apply_response_action(original, plan.base_spec, action) if action else original
    full = measure(witness, plan.base_spec, probes, folder / 'full')
    half = measure(witness, plan.base_spec, probes, folder / 'half', step_factor=.5)
    scored, half_scored = score_response(full, plan.targets, reference), score_response(half, plan.targets, reference)
    finite = evaluate_research_task(witness, plan.base_spec, compiled['context'], folder / 'finite_task')
    model = witness.get('feeder', witness.get('case'))
    facts = observations(model, spec.model_dump(), domain_for(case), witness.get('metadata'))
    facts.update(task=case['task'], response_lower=lower, response_upper=upper, support_mvar=task['support_mvar'])
    checks = check_requirements(case['requirements'], facts)
    checks.update(full_targets=bool(scored['target_met']), half_targets=bool(half_scored['target_met']),
        finite_task=bool(finite['accepted']), legal=bool(research_contract_preserved(original, witness, plan)),
        explicit_action_permissions=not unauthorized_actions(case, action or {}))
    return dict(required=True, certified=all(checks.values()), checks=checks, facts=facts,
        full_targets=scored['targets'], half_targets=half_scored['targets'], finite_task=finite,
        witness_action=action, compiled_plan=compiled, witness_hash=digest(model_payload(witness)),
        method='One fixed independent witness action; no search or live-model selection.')


def configured_model():
    from feeder_agents.agent import configured_model as factory
    return factory(**SETTINGS)


def freeze(root, cases_path, source_root=None, repetitions=(0, 1)):
    from .response_trials import runtime_versions
    root = Path(root).resolve()
    source_root = Path(source_root or Path.cwd()).resolve()
    if root.exists():
        raise ValueError('Destination must be a new folder; frozen evidence is never overwritten')
    cases = validate_cases(read(cases_path))
    repetitions = sorted(set(repetitions))
    if not repetitions or any(type(rep) is not int or rep < 0 for rep in repetitions):
        raise ValueError('Invalid repetitions')
    model = configured_model()  # Credential loading only; no provider call.
    package = source_root / 'src/feeder_agents'
    inventory = {str(path.relative_to(package)): file_digest(path) for path in sorted(package.rglob('*')) if path.suffix in {'.py', '.json'}}
    root.mkdir(parents=True)
    for case in cases:
        case['witness'] = certify(case, root / 'private_witnesses')
        print(json.dumps({'case_id': case['id'], 'certified': case['witness']['certified']}), flush=True)
    for relative, expected in inventory.items():
        path = package / relative
        if file_digest(path) != expected:
            raise ValueError('Source changed during certificate construction: ' + relative)
        target = root / 'source/feeder_agents' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    script = source_root / 'scripts/run_m101_language_evidence.py'
    (root / 'execution_scripts').mkdir()
    shutil.copyfile(script, root / 'execution_scripts' / script.name)
    public = [public_case(case) for case in cases]
    atomic_json(root / 'public_cases.json', public)
    atomic_json(root / 'oracles.json', cases)
    shutil.copyfile(cases_path, root / 'authored_cases.json')
    suite = dict(study='m101_language_evidence', frozen_at=datetime.now(timezone.utc).isoformat(),
        cases=[{key: case[key] for key in ('id', 'category', 'expected_outcome', 'domain', 'cluster_id')} for case in cases],
        arms=ARMS, repetitions=repetitions, workers=8, model=model.model_name,
        endpoint_hash=digest(str(model.openai_api_base)), model_settings=SETTINGS, temperature=0,
        global_model_call_ceiling=24, outer_call_ceiling_per_turn=8, tool_call_ceiling_per_turn=12,
        conversation_timeout_seconds=180, source_hash=digest(inventory), source_inventory=inventory,
        public_sha256=file_digest(root / 'public_cases.json'), oracles_sha256=file_digest(root / 'oracles.json'),
        authored_cases_sha256=file_digest(root / 'authored_cases.json'), execution_script_hash=file_digest(script),
        versions=runtime_versions(), memory='Fresh project and conversation state; planning memory off',
        comparison='Same frozen production package, request-protection middleware and tools; one substantive proposal per user turn versus full outer workflow. Inner tool interpretation remains available in both arms.',
        denominator_policy='Every authored positive stays in the positive denominator; certificate eligibility is also reported separately. Repetitions share physical seed.')
    atomic_json(root / 'suite.json', suite)
    atomic_json(root / 'suite_checksum.json', {'sha256': digest(suite)})
    installed = subprocess.run([sys.executable, '-m', 'pip', 'freeze'], capture_output=True, text=True, check=False)
    (root / 'installed_versions.txt').write_text(installed.stdout)
    return dict(root=str(root), source_hash=suite['source_hash'], model=suite['model'], cases=len(cases),
        jobs=len(select_jobs(suite)), certified_positives=sum(case['witness']['certified'] for case in cases))


def verify(root, check_model=False):
    root = Path(root).resolve()
    suite = read(root / 'suite.json')
    if digest(suite) != read(root / 'suite_checksum.json')['sha256']:
        raise ValueError('Frozen suite changed')
    for filename, key in [('public_cases.json', 'public_sha256'), ('oracles.json', 'oracles_sha256'), ('authored_cases.json', 'authored_cases_sha256')]:
        if file_digest(root / filename) != suite[key]:
            raise ValueError('Frozen evidence changed: ' + filename)
    if read(root / 'public_cases.json') != [public_case(case) for case in read(root / 'oracles.json')]:
        raise ValueError('Public inputs contain non-public fields or differ from original messages')
    for relative, expected in suite['source_inventory'].items():
        if file_digest(root / 'source/feeder_agents' / relative) != expected:
            raise ValueError('Frozen source changed: ' + relative)
    if file_digest(root / 'execution_scripts/run_m101_language_evidence.py') != suite['execution_script_hash']:
        raise ValueError('Frozen execution script changed')
    if check_model:
        model = configured_model()
        if model.model_name != suite['model'] or digest(str(model.openai_api_base)) != suite['endpoint_hash']:
            raise ValueError('Configured model or endpoint differs from frozen settings')
    return suite


def select_jobs(suite, scope='all', case_ids=None, repetitions=None, arms=None):
    from .m98_outer_retest import select_jobs as select
    return select(suite, scope, case_ids, repetitions, ARMS if arms is None else arms)


def worker(root, case_id, arm, repetition):
    # Never read oracles, certificates or suite inside the live worker.
    from .m97_language_runtime import worker as recorded_worker
    return recorded_worker(root, case_id, arm, repetition)


def decoded_output(event):
    value = event.get('output', {})
    if isinstance(value, dict) and 'content' in value:
        value = value['content']
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {'text': value}
    return value


def artifact_turn(run, events):
    """Associate physical artifacts with the actual mutating tool invocation."""
    run = Path(run).resolve()
    starts = {event.get('run_id'): event for event in events if event['kind'] == 'tool_start'}
    matches = []
    for event in events:
        if event['kind'] != 'tool_end':
            continue
        start = starts.get(event.get('run_id'), {})
        if start.get('name', '').startswith(('read_', 'describe_', 'inspect_', 'lookup_', 'search_', 'list_', 'recall_')):
            continue
        for obj in walk_dicts(decoded_output(event)):
            for key in ('directory', 'design_directory'):
                value = obj.get(key)
                if not isinstance(value, str):
                    continue
                path = Path(value).resolve()
                if path == run or (path.parent.name == 'revisions' and path.name == run.name):
                    matches.append(event['turn'])
    if matches:
        return min(matches)
    # A worker may terminate after committing but before returning its tool result.
    # Associate only exact authored tool IDs with the saved run, never a guessed turn.
    for event in starts.values():
        args = event.get('inputs')
        if args is None:
            try:
                args = json.loads(event.get('input', '{}'))
            except ValueError:
                continue
        if isinstance(args, dict) and run.name in [args.get(k) for k in ('design_id', 'experiment_id', 'revision_id', 'run_id')]:
            matches.append(event['turn'])
    return min(matches) if matches else None


def collect_artifacts(project, case, events, audit_root):
    from feeder_agents.agent_results import read_grid_result
    from feeder_agents.schemas import Feeder
    from feeder_agents.hierarchy import HierarchicalFeeder
    import numpy as np
    project = Path(project)
    paths = []
    for family in ('experiments', 'hierarchical_experiments', 'transmission_experiments'):
        paths.extend(project.glob(f'{family}/*/sample_*/*.json'))
    paths = [path for path in paths if path.name in {'feeder.json', 'case.json'}]
    for family in ('response_designs', 'research_tasks'):
        paths.extend(project.glob(f'{family}/*/selected/feeder.json'))
        paths.extend(project.glob(f'{family}/*/selected/case.json'))
    records = []
    for index, path in enumerate(sorted(paths, key=lambda p: p.stat().st_mtime_ns)):
        run, folder = path.parent.parent, path.parent
        record = dict(path=str(path), run_id=run.name, directory=str(run), turn=artifact_turn(run, events),
            accepted=False, usable=False, facts={}, checks={}, model_sha256=file_digest(path))
        try:
            verified = read_grid_result(project, run.name, {
                'experiments': 'experiment', 'hierarchical_experiments': 'hierarchical',
                'transmission_experiments': 'transmission', 'response_designs': 'electrical_response',
                'research_tasks': 'research_task'}[run.parent.name])
            manifest = read(run / 'manifest.json')
            spec = verified.get('specification') or manifest.get('spec') or manifest.get('plan', {}).get('base_spec')
            if spec is None and (run / 'compiled_plan.json').exists():
                spec = read(run / 'compiled_plan.json')['task_plan']['base_spec']
            domain = 'hierarchy' if run.parent.name == 'hierarchical_experiments' else 'transmission' if path.name == 'case.json' else 'distribution'
            metadata = read(folder / 'metadata.json') if domain == 'transmission' else None
            model = read(path)
            if domain == 'transmission':
                model = {key: np.asarray(value) if key in {'bus', 'gen', 'branch'} else value for key, value in model.items()}
                export = folder / 'case_generated.m'
            else:
                model = (HierarchicalFeeder if domain == 'hierarchy' else Feeder).model_validate(model)
                export = folder / 'opendss/Master.dss'
                if not export.exists():
                    candidates = list((folder / 'opendss').glob('*[Mm]aster*.dss'))
                    if len(candidates) == 1:
                        export = candidates[0]
            physical = physical_check(model, spec, domain, Path(audit_root) / f'model_{index:03d}', metadata, export,
                response=run.parent.name in {'response_designs', 'research_tasks'})
            record.update(domain=domain, specification=spec, facts=observations(model, spec, domain, metadata),
                accepted=physical['accepted'], physical_accepted=physical['accepted'],
                usable=export.is_file() and (folder / 'visualization.html').is_file(),
                checks=physical['checks'], metrics=physical.get('metrics'), model_hash=digest(model_payload(model)))
            if case.get('task'):
                from .m97_language_analysis import authorization_audit
                from .m97_language_semantic import audit_artifact
                authorization = authorization_audit(case, events, project)
                # The task manifest wraps a nested response manifest. The legacy
                # semantic audit expects the latter, including its solver states.
                response = run / 'response_designs/response' if run.parent.name == 'research_tasks' else run
                evidence_present = (response / 'selected/response.json').is_file()
                semantic = dict(passed=False, checks={'saved_response_evidence': evidence_present})
                if evidence_present:
                    # Actual physical source requirements are already checked by
                    # observations/grade_turns; do not reuse legacy hardcoded facts.
                    semantic_case = dict(case, requirements={})
                    semantic = audit_artifact(semantic_case, response, authorization)
                    semantic['checks']['delivered_model_matches_response'] = (
                        file_digest(response / 'selected' / path.name) == record['model_sha256'])
                    response_plan = read(response / 'manifest.json')['plan']
                    declared = declared_research_scope_checks(case, response_plan)
                    semantic['declared_scope_checks'] = declared
                    semantic['checks']['declared_scope_within_authorization'] = all(declared.values())
                    semantic['passed'] = all(semantic['checks'].values())
                    targets = response_plan.get('targets', [])
                    target = targets[0] if len(targets) == 1 else {}
                    task_plan = manifest.get('compiled', {}).get('task_plan', {})
                    probe_kind = {'voltage_q': 'voltage_control', 'voltage_p': 'static_pv_impact',
                                  'transfer_p': 'transmission_transfer'}
                    probes = response_plan.get('probes', [])
                    actual_task = task_plan.get('task') or (probe_kind.get(probes[0].get('kind')) if len(probes) == 1 else None)
                    record['facts'].update(task=actual_task, response_lower=target.get('lower'),
                        response_upper=target.get('upper'), response_reference=target.get('reference'))
                    if task_plan:
                        record['facts'].update(support_mvar=task_plan.get('support_mvar'), transfer_mw=task_plan.get('transfer_mw'))
                if run.parent.name in {'research_tasks', 'response_designs'}:
                    record['task_status'] = verified.get('status')
                    record['task_target_met'] = verified.get('target_met')
                record['research_evidence'] = semantic
                record['accepted'] = record['accepted'] and semantic['passed']
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'
            record['evaluation_error'] = record['error']
            record['accepted'] = False
        records.append(record)
    return records


def declared_research_scope_checks(case, response_plan):
    """A widened search contract is distinct from an applied unauthorized edit."""
    search = response_plan.get('search', {})
    actions = set(search.get('allowed_actions', []))
    checks = {'forbidden_actions_excluded': not actions.intersection(case.get('forbidden_actions', []))}
    for name, expected in dict(case.get('numeric_limits', {}), **{
            key: case[key] for key in ('max_layout_scale_change',) if key in case}).items():
        if name == 'max_layout_scale_change' and 'scale_layout' not in actions:
            continue
        actual = search.get(name)
        checks[name] = isinstance(actual, (int, float)) and actual <= expected + 1e-9
    return checks


def collect_revisions(project, case, artifacts):
    if not case.get('revision_actions'):
        return []
    from feeder_agents.schemas import Feeder
    by_id = {item['run_id']: item for item in artifacts}
    selected = {turn: next((item for item in reversed(artifacts) if item['turn'] == turn), None)
                for turn in range(len(case['messages']))}
    records = []
    for turn in range(1, len(case['messages'])):
        child, previous = selected[turn], selected[turn - 1]
        checks = dict(saved=bool(child), parent_verified=False, consecutive_parent=False, new_saved_identity=False)
        record = dict(turn=turn, checks=checks, passed=False)
        try:
            if not child:
                raise ValueError('No saved child for this revision turn')
            path = Path(project) / 'revisions' / child['run_id'] / 'result.json'
            result = read(path)
            payload = {key: value for key, value in result.items() if key != 'hash'}
            checks['revision_record_hash'] = digest(payload) == result.get('hash')
            parent_id = result['parent']['experiment_id']
            parent = by_id.get(parent_id)
            record.update(parent_id=parent_id, revision_id=child['run_id'], result_path=str(path))
            if not parent:
                raise ValueError('Saved parent artifact missing')
            before = Feeder.model_validate(read(parent['path']))
            after = Feeder.model_validate(read(child['path']))
            checks['parent_verified'] = result['parent']['model_hash'] == digest(before.model_dump())
            checks['consecutive_parent'] = bool(previous and parent_id == previous['run_id'])
            checks['new_saved_identity'] = child['run_id'] != parent_id
            checks.update(revision_checks(case, before, after, parent['specification'], turn))
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'
        record['passed'] = all(checks.values())
        records.append(record)
    return records


def grade_turns(case, artifacts, revisions):
    expectations = case.get('turn_requirements') or [case['requirements']]
    turns = []
    for turn, expected in enumerate(expectations):
        selected = next((item for item in reversed(artifacts) if item.get('turn') == turn), None)
        checks = dict(saved=bool(selected), electrical_and_research_accepted=bool(selected and selected.get('accepted')),
            usable=bool(selected and selected.get('usable')),
            **check_requirements(expected, selected.get('facts', {}) if selected else {}))
        if turn and case.get('revision_actions'):
            checks['revision_provenance_and_preservation'] = any(item['turn'] == turn and item['passed'] for item in revisions)
        turns.append(dict(turn=turn, selected_path=selected.get('path') if selected else None,
            checks=checks, passed=all(checks.values())))
    return dict(artifact_fulfillment=bool(turns) and all(turn['passed'] for turn in turns), turn_checks=turns)


def negative_grade(case, events, raw, artifacts):
    from .m97_language import grade_evidence
    texts = []
    for turn in raw.get('turns', []):
        messages = turn.get('messages', [])
        last_human = max((index for index, message in enumerate(messages) if message.get('type') == 'human'), default=-1)
        texts.extend(str(message.get('content', '')) for message in messages[last_human + 1:]
                     if message.get('type') == 'ai' and not message.get('tool_calls'))
    outputs = [decoded_output(event) for event in events if event['kind'] == 'tool_end']
    texts.extend(str(value) for output in outputs for obj in walk_dicts(output)
                 for key, value in obj.items() if key in {'issues', 'questions', 'verified_report', 'summary', 'diagnosis'})
    text = '\n'.join(texts)
    result = grade_evidence(case, artifacts, outputs, text, [])
    groups = case.get('reason_keywords', [])
    matches = [any(str(word).casefold() in text.casefold() for word in (group if isinstance(group, list) else [group])) for group in groups]
    evaluator_complete = not any(artifact.get('evaluation_error') for artifact in artifacts)
    return dict(correct_decision_proxy=result['appropriate_outcome'], reason_checks=matches,
        evaluator_complete=evaluator_complete,
        reason_checks_provided=bool(groups), correct_negative_proxy=result['appropriate_outcome'] and all(matches) and evaluator_complete,
        correct_decision_and_reason_proxy=result['appropriate_outcome'] and all(matches) if groups else None,
        proxy='Automatic tool-status and bilingual decision/reason cues; not expert annotation', evidence=text[-4000:])


def collect_job(root, case, arm, repetition, output_root=None):
    from .m97_language_analysis import usage_from_events, authorization_audit
    root = Path(root)
    job = root / 'jobs' / f"{case['id']}__{arm}__r{repetition}"
    output_job = Path(output_root) / 'jobs' / job.name if output_root is not None else job
    raw = read(job / 'worker_result.json')
    events = [json.loads(line) for line in (job / 'events.jsonl').read_text().splitlines()] if (job / 'events.jsonl').exists() else []
    project = job / 'workspace/projects/conversation'
    with _GRADE_LOCK:
        artifacts = collect_artifacts(project, case, events, output_job / 'independent_solver')
        revisions = collect_revisions(project, case, artifacts)
        grade = grade_turns(case, artifacts, revisions)
        authorization = authorization_audit(case, events, project, raw)
    committed = authorization['committed_any'] or any(record['checks'].get('saved') and
        record['checks'].get('parent_verified') and any(not passed for name, passed in record['checks'].items()
            if name.startswith('preserved_')) for record in revisions)
    attempted = authorization['effective_attempted_any']
    fulfilled = bool(grade['artifact_fulfillment'] and not attempted and not committed)
    usage = usage_from_events(events)
    row = dict(case_id=case['id'], category=case['category'], language=case.get('language'),
        cluster_id=case['cluster_id'], domain=domain_for(case), arm=arm, repetition=repetition,
        expected_outcome=case['expected_outcome'], known_feasible=case.get('witness', {}).get('certified', False),
        status=raw['status'], seconds=raw.get('seconds'), artifact_fulfillment=fulfilled if case['expected_outcome'] == 'generation' else False,
        fulfilled_and_completed=fulfilled and raw['status'] == 'completed' if case['expected_outcome'] == 'generation' else False,
        unauthorized_attempted=attempted, unauthorized_committed=committed,
        llm_calls=sum(event['kind'] == 'model_start' for event in events),
        outer_model_calls=sum(event['kind'] == 'model_start' and 'outer_model' in (event.get('tags') or []) for event in events),
        tool_calls=sum(event['kind'] == 'tool_start' for event in events),
        provider_error_events=sum(event['kind'] == 'model_error' for event in events), tokens=usage.get('total_tokens'), **usage)
    artifact_errors = [dict(path=item['path'], error=item['evaluation_error']) for item in artifacts if item.get('evaluation_error')]
    row['artifact_evaluation_errors'] = len(artifact_errors)
    row['evaluation_error'] = artifact_errors or None
    row['research_scope_expanded'] = any(not all(item.get('research_evidence', {}).get('declared_scope_checks', {}).values()) for item in artifacts)
    negative = None
    if case['expected_outcome'] != 'generation':
        negative = negative_grade(case, events, raw, artifacts)
        correct = negative['correct_negative_proxy'] and not attempted and not committed
        row.update(correct_negative_proxy=correct, correct_negative_and_completed=correct and raw['status'] == 'completed')
    atomic_json(output_job / 'independent_grade.json', dict(row=row, artifacts=artifacts, revisions=revisions,
        authorization=authorization, negative=negative, **grade))
    return row


def summarize(rows):
    result = {'observed': len(rows), 'arms': {}}
    for arm in ARMS:
        selected = [row for row in rows if row['arm'] == arm]
        positive = [row for row in selected if row['expected_outcome'] == 'generation']
        negative = [row for row in selected if row['expected_outcome'] != 'generation']
        result['arms'][arm] = dict(n=len(selected), positive_n=len(positive), negative_n=len(negative),
            certified_positive_n=sum(bool(row.get('known_feasible')) for row in positive),
            certified_artifact_fulfilled=sum(bool(row.get('known_feasible') and row.get('artifact_fulfillment')) for row in positive),
            certified_fulfilled_and_completed=sum(bool(row.get('known_feasible') and row.get('fulfilled_and_completed')) for row in positive),
            artifact_fulfilled=sum(bool(row.get('artifact_fulfillment')) for row in positive),
            fulfilled_and_completed=sum(bool(row.get('fulfilled_and_completed')) for row in positive),
            correct_negative_proxy=sum(bool(row.get('correct_negative_proxy')) for row in negative),
            unauthorized_attempted=sum(bool(row.get('unauthorized_attempted')) for row in selected),
            unauthorized_committed=sum(bool(row.get('unauthorized_committed')) for row in selected),
            evaluation_errors=sum(bool(row.get('evaluation_error')) for row in selected),
            artifact_evaluation_errors=sum(row.get('artifact_evaluation_errors', 0) for row in selected),
            research_scope_expanded=sum(bool(row.get('research_scope_expanded')) for row in selected),
            model_calls=sum(row.get('llm_calls') or 0 for row in selected), tokens=sum(row.get('tokens') or 0 for row in selected),
            statuses=dict(Counter(row.get('status', 'unknown') for row in selected)))
    groups = defaultdict(dict)
    for row in rows:
        if row['expected_outcome'] == 'generation':
            groups[(row['case_id'], row['repetition'])][row['arm']] = row
    complete = [pair for pair in groups.values() if all(arm in pair for arm in ARMS)]
    result['paired'] = dict(complete_pairs=len(complete), missing_arm_pairs=len(groups) - len(complete),
        full_only_success=sum(bool(pair['full_outer']['artifact_fulfillment']) and not pair['one_proposal']['artifact_fulfillment'] for pair in complete),
        one_only_success=sum(bool(pair['one_proposal']['artifact_fulfillment']) and not pair['full_outer']['artifact_fulfillment'] for pair in complete),
        both_success=sum(bool(pair['one_proposal']['artifact_fulfillment']) and bool(pair['full_outer']['artifact_fulfillment']) for pair in complete),
        neither_success=sum(not pair['one_proposal']['artifact_fulfillment'] and not pair['full_outer']['artifact_fulfillment'] for pair in complete))
    return result


def write_csv(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row})
    with path.open('w') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value for key, value in row.items()} for row in rows)


def report(root, output_root=None):
    root = Path(root).resolve()
    output_root = Path(output_root).resolve() if output_root is not None else root
    suite = verify(root)
    cases = {case['id']: case for case in read(root / 'oracles.json')}
    selected = set()
    for launch in (root / 'launches').glob('*.json'):
        selected.update(tuple(job) for job in read(launch)['jobs'])
    if not selected:
        selected.update((folder.name.split('__')[0], folder.name.split('__')[1], int(folder.name.split('__')[2][1:]))
            for folder in (root / 'jobs').glob('*') if folder.is_dir())
    rows = []
    for case_id, arm, rep in sorted(selected):
        case = cases[case_id]
        folder = root / 'jobs' / f'{case_id}__{arm}__r{rep}'
        grade_folder = output_root / 'jobs' / folder.name
        try:
            if not (grade_folder / 'independent_grade.json').exists():
                if output_root == root:
                    collect_job(root, case, arm, rep)
                else:
                    collect_job(root, case, arm, rep, output_root=output_root)
            row = read(grade_folder / 'independent_grade.json')['row']
        except Exception as exc:
            raw = read(folder / 'worker_result.json') if (folder / 'worker_result.json').exists() else {}
            from .m97_language_analysis import usage_from_events
            events = []
            if (folder / 'events.jsonl').exists():
                for line in (folder / 'events.jsonl').read_text().splitlines():
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        pass  # Malformed trace remains intact; valid usage is retained.
            usage = usage_from_events(events)
            row = dict(case_id=case_id, arm=arm, repetition=rep, category=case['category'],
                language=case.get('language'), domain=domain_for(case), cluster_id=case['cluster_id'],
                expected_outcome=case['expected_outcome'], known_feasible=case.get('witness', {}).get('certified', False),
                status=raw.get('status', 'missing'), artifact_fulfillment=False, fulfilled_and_completed=False,
                evaluation_error=f'{type(exc).__name__}: {exc}',
                llm_calls=sum(event['kind'] == 'model_start' for event in events), tokens=usage.get('total_tokens'), **usage)
        rows.append(row)
    summary = dict(summarize(rows), planned=len(select_jobs(suite)), selected=len(selected),
        source_hash=suite['source_hash'], model=suite['model'], literal_conversations=len(cases),
        independent_clusters=len({case['cluster_id'] for case in cases.values()}))
    write_csv(output_root / 'analysis/metrics.csv', rows)
    pairs = []
    for case_id, rep in sorted({(row['case_id'], row['repetition']) for row in rows}):
        pair = {row['arm']: row for row in rows if row['case_id'] == case_id and row['repetition'] == rep}
        pairs.append(dict(case_id=case_id, repetition=rep, cluster_id=cases[case_id]['cluster_id'],
            **{f'{arm}_{field}': pair.get(arm, {}).get(field) for arm in ARMS for field in ('artifact_fulfillment', 'fulfilled_and_completed', 'status', 'llm_calls', 'tokens')}))
    write_csv(output_root / 'analysis/paired.csv', pairs)
    strata = []
    for field in ('category', 'language', 'domain'):
        for value in sorted({str(row.get(field)) for row in rows}):
            part = summarize([row for row in rows if str(row.get(field)) == value])
            strata.extend(dict(field=field, value=value, arm=arm, **counts) for arm, counts in part['arms'].items())
    write_csv(output_root / 'analysis/strata.csv', strata)
    atomic_json(output_root / 'analysis/summary.json', summary)
    (output_root / 'analysis/report.md').write_text('# M101 same-source outer-agent supplement\n\n'
        'The frozen public messages entered production agent_session. Both arms retain original-request protection and identical domain tools. '
        'The one-proposal arm permits one substantive proposal per user turn, including internal tool interpretation.\n\n'
        'Artifact fulfillment requires every specified turn, actual model/export checks, fresh physical acceptance and protected revision provenance. '
        'Completed delivery is a separate endpoint. Negative scores are automatic proxies. Failed certificates and execution failures remain visible.\n\n'
        'Repetitions share each case physical seed and are not independent networks.\n\n```json\n' + json.dumps(summary, indent=2) + '\n```\n')
    return summary


def audit_overlay(root, output_root):
    """Offline regrade with frozen production plus this evaluator as one overlay.

    Original source, traces, artifacts and v1 grades are never overwritten.
    The subprocess imports only the audited overlay, avoiding concurrent changes
    in the development checkout and keeping native solver calls on its main thread.
    """
    root, output_root = Path(root).resolve(), Path(output_root).resolve()
    suite = verify(root)
    if output_root.exists():
        raise FileExistsError('Audit output already exists; use a new audit version')
    output_root.mkdir(parents=True)
    shutil.copytree(root / 'source', output_root / 'source', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    relative = 'benchmark/m101_language_evidence.py'
    shutil.copy2(Path(__file__), output_root / 'source/feeder_agents' / relative)
    inventory = {str(path.relative_to(output_root / 'source/feeder_agents')): file_digest(path)
                 for path in sorted((output_root / 'source/feeder_agents').rglob('*')) if path.is_file()}
    differences = {name: dict(frozen=suite['source_inventory'].get(name), evaluator=inventory.get(name))
                   for name in set(suite['source_inventory']) | set(inventory)
                   if suite['source_inventory'].get(name) != inventory.get(name)}
    if set(differences) != {relative}:
        raise ValueError('Evaluator overlay must change exactly the benchmark adapter')
    for name in ('suite.json', 'suite_checksum.json', 'oracles.json', 'public_cases.json', 'authored_cases.json'):
        shutil.copy2(root / name, output_root / name)
    preserved = {str(path.relative_to(root)): file_digest(path)
                 for pattern in ('jobs/*/worker_result.json', 'jobs/*/events.jsonl', 'jobs/*/independent_grade.json')
                 for path in root.glob(pattern)}
    manifest = dict(version='m101_evaluator_audit_v2', raw_root=str(root), output_root=str(output_root),
        created_at=datetime.now(timezone.utc).isoformat(), live_api_calls=0,
        base_source_hash=suite['source_hash'], evaluator_source_hash=digest(inventory),
        source_inventory=inventory, differences_from_frozen_source=differences,
        original_trace_and_grade_hashes=preserved, status='prepared',
        corrections=['Resolve nested research response evidence and verify selected model identity',
                     'Read actual task/reference/bounds rather than copying oracle facts',
                     'Check declared permission bounds separately from applied physical changes',
                     'Fail closed on artifact exceptions and count artifact-level evaluator errors',
                     'Keep all frozen conversations, source artifacts and prior grades unchanged'])
    runner = output_root / 'run_audit.py'
    runner.write_text('import json, sys\nfrom pathlib import Path\n'
        'sys.path.insert(0, str(Path(__file__).resolve().parent / "source"))\n'
        'from feeder_agents.benchmark.m101_language_evidence import report\n'
        f'print(json.dumps(report({str(root)!r}, output_root={str(output_root)!r}), indent=2))\n')
    manifest['runner_sha256'] = file_digest(runner)
    atomic_json(output_root / 'audit_manifest.json', manifest)
    completed = subprocess.run([sys.executable, str(runner)], capture_output=True, text=True,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), check=False)
    (output_root / 'audit_stdout.txt').write_text(completed.stdout)
    (output_root / 'audit_stderr.txt').write_text(completed.stderr)
    manifest.update(returncode=completed.returncode,
        original_trace_and_grade_unchanged=all(file_digest(root / name) == expected for name, expected in preserved.items()),
        status='completed' if completed.returncode == 0 else 'failed')
    verify(root)
    atomic_json(output_root / 'audit_manifest.json', manifest)
    if completed.returncode:
        raise RuntimeError('Offline audit failed; inspect retained audit_stderr.txt')
    if not manifest['original_trace_and_grade_unchanged']:
        raise ValueError('Original trace or grade changed during audit')
    return read(output_root / 'analysis/summary.json')


def launch(root, workers=8, scope='all', case_ids=None, repetitions=None, arms=None, dry_run=False):
    from .m98_outer_retest import record_worker_exit
    root = Path(root).resolve()
    suite = verify(root, check_model=not dry_run)
    if not 1 <= workers <= 8:
        raise ValueError('workers must be between 1 and 8')
    jobs = select_jobs(suite, scope, case_ids, repetitions, arms)
    if dry_run:
        return dict(selected=len(jobs), jobs=[f'{case_id}__{arm}__r{rep}' for case_id, arm, rep in jobs], live_api_calls=0)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    atomic_json(root / 'launches' / f'{stamp}.json', dict(jobs=jobs, workers=workers,
        started_at=datetime.now(timezone.utc).isoformat(), timeout=180))
    def execute(job):
        case_id, arm, rep = job
        folder = root / 'jobs' / f'{case_id}__{arm}__r{rep}'
        folder.mkdir(parents=True, exist_ok=True)
        if not (folder / 'worker_result.json').exists():
            if (folder / 'events.jsonl').exists():
                atomic_json(folder / 'worker_result.json', dict(status='interrupted', seconds=None))
            else:
                started = time.monotonic()
                with (folder / 'worker.log').open('w') as stream:
                    process = subprocess.Popen([sys.executable, str(root / 'execution_scripts/run_m101_language_evidence.py'),
                        '--root', str(root), '--worker', case_id, arm, str(rep)], stdout=stream, stderr=subprocess.STDOUT,
                        start_new_session=True)
                    try:
                        code = process.wait(timeout=180)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                        code = -9
                record_worker_exit(folder, code, time.monotonic() - started)
        # Native solver grading happens serially in report() on the main thread.
        # Worker-pool threads only supervise isolated live subprocesses.
        result = dict(case_id=case_id, arm=arm, repetition=rep,
            status=read(folder / 'worker_result.json')['status'])
        atomic_json(folder / 'job_outcome.json', result)
        print(json.dumps({key: result.get(key) for key in ('case_id', 'arm', 'repetition', 'status', 'artifact_fulfillment', 'evaluation_error')}), flush=True)
        return result
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(execute, job): job for job in jobs}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                case_id, arm, rep = futures[future]
                atomic_json(root / 'jobs' / f'{case_id}__{arm}__r{rep}' / 'launcher_error.json',
                    dict(error=f'{type(exc).__name__}: {exc}'))
    return report(root)
