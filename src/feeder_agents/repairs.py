"""Bounded repair candidates. LLM preferences never replace electrical validation."""
import json
import math
import time

import networkx as nx

from .artifacts import digest
from .rules import data, load_rules
from .knowledge import effective_limits, applies
from .schemas import ExperimentSpec, Feeder, RepairCandidate, RepairDecision


def _bus_voltage_limits(feeder, spec=None):
    research=load_rules(spec)['research.voltage']['parameters']
    base=(research['min_pu'],research['max_pu'])
    lower,upper,_=effective_limits(spec)
    loads={l.bus for l in feeder.loads}
    return {b.id: ((lower,upper) if b.id in loads else base) for b in feeder.buses}


def severity(feeder: Feeder, result: dict, spec=None) -> dict:
    rules = load_rules()
    lower,upper,loading_limit = effective_limits(spec)
    limits = _bus_voltage_limits(feeder,spec)
    voltage = max((max(limits[bus][0]-v, v-limits[bus][1], 0)
                   for bus, values in result.get('bus_voltage_pu', {}).items()
                   if bus in limits for v in values), default=0)
    catalogue = data('conductors.json')
    ampacity = max((max(result.get('line_current_a', {}).get(e.id, 0) /
        catalogue[e.conductor]['normamps'] - loading_limit, 0)
        for e in feeder.lines), default=0)
    violations = {'voltage': voltage, 'ampacity': ampacity}
    for bus, values in result.get('bus_voltage_pu', {}).items():
        if bus in limits:
            for phase, value in enumerate(values, 1):
                excess = max(limits[bus][0]-value, value-limits[bus][1], 0)
                if excess:
                    violations[f'voltage:{bus}:{phase}'] = excess
    for line in feeder.lines:
        excess = max(result.get('line_current_a', {}).get(line.id,0)/catalogue[line.conductor]['normamps'] -
                     loading_limit, 0)
        if excess:
            violations[f'ampacity:{line.id}'] = excess
    if feeder.phase_mode == 'unbalanced':
        limit = spec.phase_design.max_vuf_percent if spec else 2.0
        for bus, value in result.get('bus_vuf_percent', {}).items():
            if value is not None and value > limit:
                violations[f'unbalance:{bus}'] = value-limit
    return violations


def diagnose(feeder: Feeder, result: dict, checks: list, spec=None) -> dict:
    catalogue = data('conductors.json')
    return {'failed_rules': [c['rule_id'] for c in checks if c['status'] == 'fail'],
            'severity': {k:v for k,v in severity(feeder, result, spec).items() if ':' not in k},
            'lowest_voltage_buses': sorted([
                {'bus': bus, 'min_pu': min(values), 'max_pu': max(values)}
                for bus, values in result.get('bus_voltage_pu', {}).items() if values],
                key=lambda x: x['min_pu'])[:8],
            'most_loaded_lines': sorted([
                {'line': e.id, 'loading_ratio': result.get('line_current_a', {}).get(e.id, 0)/catalogue[e.conductor]['normamps'],
                 'conductor': e.conductor, 'length_km': e.length_km}
                for e in feeder.lines], key=lambda x: -x['loading_ratio'])[:8],
            'scope': f'{feeder.phase_mode} single-voltage snapshot; aggregate PF is not customer PCC compliance.'}


def _tree(feeder):
    graph = nx.Graph()
    graph.add_nodes_from(b.id for b in feeder.buses)
    graph.add_edges_from((e.bus1, e.bus2, {'weight': e.length_km, 'line_id': e.id}) for e in feeder.lines)
    if not nx.is_tree(graph):
        raise ValueError('Repair requires a connected radial network')
    return graph


def candidates(feeder: Feeder, spec: ExperimentSpec, result: dict, checks: list) -> list[RepairCandidate]:
    if spec.mode == 'stress' or any(c['category'] == 'validity' and c['status'] == 'fail' for c in checks):
        return []
    failed = {c['rule_id'] for c in checks if c['status'] == 'fail'}
    lower,upper,loading_limit = effective_limits(spec)
    voltage_ids={r.rule_id for r in spec.document_rules if 'voltage' in r.metric and applies(r,spec)}
    loading_ids={r.rule_id for r in spec.document_rules if r.metric=='max_loading_ratio' and applies(r,spec)}
    voltage_bad = bool(failed & ({'research.voltage', 'cn.voltage'} | voltage_ids))
    overload_bad = bool(failed & ({'research.ampacity'} | loading_ids))
    if not (voltage_bad or overload_bad):
        return []
    graph = _tree(feeder)
    directed = nx.bfs_tree(graph, feeder.source_bus)
    distances = nx.single_source_dijkstra_path_length(graph, feeder.source_bus)
    catalogue = data('conductors.json')
    grades = list(catalogue)
    model_hash = digest(feeder.model_dump())
    options = []

    def add(**kwargs):
        payload = {'model_hash': model_hash, **kwargs}
        options.append(RepairCandidate(candidate_id=digest(payload)[:20], **payload))

    # Electrical-drop proxy I*length*|Z| is a ranking only, not a solved voltage.
    relevant_lines = set()
    if voltage_bad:
        for bus,(low,high) in _bus_voltage_limits(feeder,spec).items():
            if any(v < low or v > high for v in result.get('bus_voltage_pu', {}).get(bus, [])):
                path = nx.shortest_path(graph, feeder.source_bus, bus)
                relevant_lines.update(graph[a][b]['line_id'] for a, b in zip(path, path[1:]))
    for edge in feeder.lines:
        current = result.get('line_current_a', {}).get(edge.id, 0)
        rating = catalogue[edge.conductor]['normamps']
        from .equipment import next_upgrade
        upgrade=next_upgrade(edge,spec.equipment_design.reference_feeders)
        if upgrade is None or not (current > rating*loading_limit or edge.id in relevant_lines):
            continue
        z = catalogue[edge.conductor]
        priority = max(current/rating - loading_limit, 0)*100 + current*edge.length_km*math.hypot(z['r1'], z['x1'])/100
        add(action='upgrade_conductor', line_id=edge.id, conductor=upgrade,
            rationale='Local single-grade upgrade on overloaded line or upstream path of voltage violation; verify with DSS.',
            priority=priority)
    upgrades = sorted(options, key=lambda c: (-c.priority, c.candidate_id))
    options = []
    if spec.repair_policy.allow_rewire:
        positions = {b.id: (b.x_km, b.y_km) for b in feeder.buses}
        # Bound candidate enumeration by choosing up to eight electrically
        # stressed cuts, then scanning alternative parents for those cuts.
        cuts = sorted(directed.edges(), key=lambda ab: -distances[ab[1]])
        cuts = [(a,b) for a,b in cuts if graph[a][b]['line_id'] in relevant_lines or
                result.get('line_current_a', {}).get(graph[a][b]['line_id'],0) >
                catalogue[next(e.conductor for e in feeder.lines if e.id == graph[a][b]['line_id'])]['normamps']*loading_limit][:8]
        for old_parent, child in cuts:
            subtree = nx.descendants(directed, child) | {child}
            for parent in sorted(set(graph)-subtree-{old_parent}):
                length = math.dist(positions[parent], positions[child])
                improvement = distances[child] - (distances[parent]+length)
                if spec.segment_km_min-1e-10 <= length <= spec.segment_km_max+1e-10 and improvement > 1e-9:
                    add(action='reconnect_branch', line_id=graph[old_parent][child]['line_id'],
                        parent_bus=parent, child_bus=child,
                        rationale=f'Cross-cut reconnect shortens source path by {improvement:.6g} km; electrical benefit unverified.',
                        priority=improvement)
    rewires = sorted(options, key=lambda c: (-c.priority, c.candidate_id))
    # Reserve space for both action types, rather than letting a different-unit
    # heuristic score suppress an entire family of candidates.
    limit = spec.repair_policy.max_candidates
    selected = upgrades[:limit//2] + rewires[:limit//2]
    selected += [c for c in upgrades+rewires if c not in selected][:limit-len(selected)]
    return selected


def apply_candidate(feeder: Feeder, spec: ExperimentSpec, choice: RepairCandidate) -> Feeder:
    if choice.model_hash != digest(feeder.model_dump()):
        raise ValueError('Cannot apply stale candidate')
    trial = feeder.model_copy(deep=True)
    line = next((e for e in trial.lines if e.id == choice.line_id), None)
    if line is None:
        raise ValueError('Unknown candidate line')
    if choice.action == 'upgrade_conductor':
        from .equipment import next_upgrade
        if choice.conductor != next_upgrade(line,spec.equipment_design.reference_feeders):
            raise ValueError('Only a local single-grade upgrade is allowed')
        line.conductor = choice.conductor
    else:
        if not spec.repair_policy.allow_rewire:
            raise ValueError('Topology changes were not enabled')
        graph = _tree(feeder)
        rooted = nx.bfs_tree(graph, feeder.source_bus)
        old_parent, child = (line.bus1, line.bus2) if rooted.has_edge(line.bus1,line.bus2) else (line.bus2,line.bus1)
        subtree = nx.descendants(rooted, child) | {child}
        if choice.child_bus != child or choice.parent_bus not in set(graph)-subtree-{old_parent}:
            raise ValueError('Reconnect must cross the cut without a cycle')
        positions = {b.id:(b.x_km,b.y_km) for b in feeder.buses}
        length = math.dist(positions[choice.parent_bus],positions[child])
        distances = nx.single_source_dijkstra_path_length(graph, feeder.source_bus)
        if not spec.segment_km_min-1e-10 <= length <= spec.segment_km_max+1e-10:
            raise ValueError('Reconnect violates distance bounds')
        if distances[choice.parent_bus]+length >= distances[child]-1e-9:
            raise ValueError('Reconnect does not shorten source path')
        line.bus1, line.bus2, line.length_km = choice.parent_bus, child, length
        _tree(trial)
    return trial


def improves(before_checks, after_checks, before, after) -> bool:
    if any(c['category'] == 'validity' and c['status'] != 'pass' for c in after_checks):
        return False
    failures = lambda cs: {c['rule_id'] for c in cs if c['status'] == 'fail'}
    if not failures(after_checks).issubset(failures(before_checks)):
        return False
    return (all(after.get(key,0) <= before.get(key,0)+1e-9 for key in before.keys() | after.keys()) and
            any(after.get(key,0) < before.get(key,0)-1e-9 for key in before))


def selector_model():
    from .agent import configured_model
    return configured_model(timeout=20, max_retries=0, max_tokens=600, disable_thinking=True)


def selector_identity(model):
    return {'model': model.model_name, 'base_url': str(getattr(model, 'openai_api_base', '') or ''),
            'timeout_seconds': 20, 'max_retries': 0, 'max_tokens': 600}


def select_candidate(options, diagnosis, model=None):
    model = selector_model() if model is None else model
    start = time.perf_counter()
    payload = model.with_structured_output(RepairDecision, method='function_calling', include_raw=True).invoke([
        ('system', '你是配网科研修复决策节点。只选择给出的candidate_id或stop，解释依据。保持负荷/PV/坐标/规则阈值。'
                   'experience_hints是同版本/场景的历史观察，不能替代当前DSS结果，也不保证本次有效。'
                   '优先处理主导越限，比较局部增容与缩短路径。候选priority是启发式，不是已验证电气收益。'
                   'candidate_previews是实际DSS试算证据，优先选择would_commit=true且accepted=true的已验证候选，未试算不等于失败。'
                   '每轮只能执行一个动作，随后DSS复验。不得发明动作。'),
        ('human', json.dumps({'diagnosis': diagnosis, 'candidates': [c.model_dump() for c in options]}, ensure_ascii=False))])
    if isinstance(payload, dict) and 'parsed' in payload:
        if payload.get('parsing_error') or payload['parsed'] is None:
            raise ValueError('Invalid structured repair decision')
        decision = RepairDecision.model_validate(payload['parsed'])
        usage = getattr(payload.get('raw'), 'usage_metadata', None)
    else:
        decision = RepairDecision.model_validate(payload)
        usage = None
    choice = next((c for c in options if c.candidate_id == decision.candidate_id), None)
    if choice is None and decision.candidate_id != 'stop':
        raise ValueError('Decision references an unknown candidate')
    return choice, {'selector': 'agent', **selector_identity(model), 'decision': decision.model_dump(),
                    'elapsed_seconds': round(time.perf_counter()-start,3), 'usage': usage, 'request_attempted': True}


def _simulate_preview(master):
    from .simulation import simulate
    return simulate(master)


def preview_candidates(feeder, spec, simulation, checks, options, directory):
    from .simulation import export_dss
    from .evaluation import evaluate, verdict
    from .artifacts import atomic_json
    chosen = []
    for action in ('upgrade_conductor', 'reconnect_branch'):
        first = next((c for c in options if c.action == action), None)
        if first:
            chosen.append(first)
    chosen += [c for c in options if c not in chosen]
    rows = []
    for index, choice in enumerate(chosen[:spec.repair_policy.preview_limit]):
        trial = apply_candidate(feeder, spec, choice)
        folder = directory / f'candidate_{index:02d}'
        atomic_json(folder / 'feeder.json', trial.model_dump())
        master = export_dss(trial, folder / 'opendss')
        try:
            result = _simulate_preview(master)
            json.dumps(result, allow_nan=False)
        except Exception as exc:
            result = {'converged': False, 'error': type(exc).__name__}
        after_checks = evaluate(trial, spec, result)
        status = verdict(after_checks, spec.mode)
        atomic_json(folder / 'simulation.json', result)
        atomic_json(folder / 'validation.json', {'checks': after_checks, **status})
        row = {'candidate_id': choice.candidate_id, 'action': choice.action, **status,
               'would_commit': improves(checks, after_checks, severity(feeder, simulation, spec), severity(trial, result, spec)),
               'min_voltage_pu': result.get('min_voltage_pu'),
               'severity': {k:v for k,v in severity(trial, result, spec).items() if ':' not in k}}
        atomic_json(folder / 'preview.json', row)
        rows.append(row)
    return rows
