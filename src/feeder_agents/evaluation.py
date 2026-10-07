import math

import networkx as nx

from .rules import data, load_rules
from .schemas import ExperimentSpec, Feeder


def evaluate(feeder: Feeder, spec: ExperimentSpec, result: dict) -> list[dict]:
    checks = []
    rules = load_rules(spec)

    def add(rule_id, passed, evidence, applicable=True):
        rule = rules[rule_id]
        checks.append({
            'rule_id': rule_id, 'rule_version': rule['version'],
            'category': rule['category'], 'kind': rule['kind'],
            'source_id': rule['source_id'], 'locator': rule['locator'],
            'status': ('pass' if passed else 'fail') if applicable else 'not_applicable',
            'evidence': evidence,
        })

    graph = nx.Graph()
    bus_ids = {b.id for b in feeder.buses}
    graph.add_nodes_from(bus_ids)
    graph.add_edges_from((x.bus1, x.bus2) for x in feeder.lines)
    topology_ok = (bool(bus_ids) and feeder.source_bus in bus_ids and
                   len(bus_ids) == len(feeder.buses) and
                   len({x.id for x in feeder.lines}) == len(feeder.lines) and
                   len({x.id for x in feeder.loads}) == len(feeder.loads) and
                   len(graph.edges) == len(feeder.lines) and
                   set(graph.nodes) == bus_ids and nx.is_tree(graph) and
                   all(x.bus in bus_ids for x in feeder.loads))
    from .ties import tie_contract_matches
    ties_ok=tie_contract_matches(feeder,spec,result if feeder.tie_lines else None)
    add('model.radial_connected', topology_ok and ties_ok,
        {'buses': len(bus_ids), 'lines': len(feeder.lines), 'loads': len(feeder.loads),
         'normally_open_ties':len(feeder.tie_lines),'tie_contract_matches':ties_ok})
    total = sum(x.kw for x in feeder.loads)
    pv = sum(x.pv_kw for x in feeder.loads)
    demand_ok = (spec.n_loads_min <= len(feeder.loads) <= spec.n_loads_max and
                 spec.total_kw_min-1e-7 <= total <= spec.total_kw_max+1e-7 and
                 math.isclose(total, feeder.total_kw, abs_tol=1e-7) and
                 math.isclose(pv, total*spec.pv_ratio, abs_tol=1e-7) and
                 feeder.voltage_kv == spec.voltage_kv and
                 all(spec.segment_km_min-1e-10 <= x.length_km <= spec.segment_km_max+1e-10 for x in feeder.lines) and
                 all(math.isclose(x.kw/math.hypot(x.kw, x.kvar), spec.power_factor, abs_tol=1e-9)
                     for x in feeder.loads))
    positions = {b.id: (b.x_km, b.y_km) for b in feeder.buses}
    geometry_ok = all(e.bus1 in positions and e.bus2 in positions and
        math.isclose(e.length_km, math.dist(positions[e.bus1], positions[e.bus2]), rel_tol=1e-9, abs_tol=1e-10)
        for e in feeder.lines)
    if spec.scenario.positions_km is not None:
        geometry_ok = geometry_ok and [(b.x_km, b.y_km) for b in feeder.buses] == spec.scenario.positions_km
    bus_count_ok = spec.n_buses is None or len(feeder.buses) == spec.n_buses
    load_bus_ids = [load.bus for load in feeder.loads]
    role_ok = (len(set(load_bus_ids)) == len(load_bus_ids) and
               feeder.source_bus not in load_bus_ids and set(load_bus_ids).issubset(bus_ids))
    if spec.scenario.load_placement == 'all_nodes':
        role_ok = role_ok and set(load_bus_ids) == bus_ids-{feeder.source_bus}
    elif spec.n_buses is None:
        from .load_placement import reference_occupancy
        fraction = reference_occupancy(spec.scenario.reference_case_id)['fraction']
        bus_count_ok = len(feeder.buses) == math.ceil(len(feeder.loads)/fraction-1e-12)+1
    demand_ok = demand_ok and geometry_ok and bus_count_ok and role_ok
    hierarchy_ok = None
    if spec.scenario.layout == 'rural_villages':
        from .settlements import hierarchy_matches
        hierarchy_ok = hierarchy_matches(feeder,spec)
        demand_ok = demand_ok and hierarchy_ok
    from .topologies import topology_matches
    from .load_shapes import load_shape_matches
    family_ok=topology_matches(feeder,spec)
    shape_ok=load_shape_matches(feeder,spec)
    demand_ok=demand_ok and family_ok and shape_ok
    from .phases import phase_contract_matches
    from .scene_profiles import scene_contract_matches
    phase_ok=phase_contract_matches(feeder,spec)
    scene_ok=scene_contract_matches(feeder,spec)
    from .equipment import equipment_contract_matches
    scene_ok=scene_ok and equipment_contract_matches(feeder,spec)
    demand_ok=demand_ok and phase_ok and scene_ok
    add('model.demand', demand_ok, {'total_kw': total, 'pv_kw': pv, 'count': len(feeder.loads),
        'bus_count':len(feeder.buses),'junction_count':len(bus_ids-{feeder.source_bus}-set(load_bus_ids)),
        'bus_count_matches':bus_count_ok,'node_roles_valid':role_ok,'geometry_matches': geometry_ok,'settlement_hierarchy_matches':hierarchy_ok,'topology_family_matches':family_ok,'load_shape_matches':shape_ok,'phase_contract_matches':phase_ok,'scene_profile_matches':scene_ok})
    add('cn.frequency', feeder.frequency_hz == rules['cn.frequency']['parameters']['hz'],
        {'frequency_hz': feeder.frequency_hz})

    vmap = result.get('bus_voltage_pu', {})
    imap = result.get('line_current_a', {})
    metrics = [result.get(k) for k in ('source_kw', 'load_kw', 'generation_kw', 'loss_kw')]
    complete = (all(b.id in vmap and len(vmap[b.id]) == 3 for b in feeder.buses) and
                all(x.id in imap for x in feeder.lines))
    if feeder.phase_mode=='unbalanced':
        nodes=result.get('bus_phase_nodes',{})
        phase_voltages=result.get('bus_phase_voltage_pu',{})
        phase_currents=result.get('line_phase_current_a',{})
        complete=(all(set(nodes.get(b.id,[]))==set(b.phases) and set(phase_voltages.get(b.id,{}))=={str(p) for p in b.phases}
            and len(vmap.get(b.id,[]))==len(b.phases) for b in feeder.buses)
            and all(set(phase_currents.get(l.id,{}))=={str(p) for p in l.phases} and l.id in imap for l in feeder.lines))
    numbers = metrics + [v for values in vmap.values() for v in values] + list(imap.values())
    if feeder.phase_mode=='unbalanced':
        numbers += [v for values in phase_voltages.values() for v in values.values()]
        numbers += [v for values in phase_currents.values() for v in values.values()]
    finite = bool(numbers) and all(isinstance(v, (int, float)) and math.isfinite(v) for v in numbers)
    positive_v = complete and all(v > 0 for values in vmap.values() for v in values)
    solver_ok = bool(result.get('converged')) and complete and finite and positive_v
    demand_error = generation_error = None
    if solver_ok:
        balance_error = abs(result['source_kw'] - (result['load_kw'] - result['generation_kw'] + result['loss_kw']))
        demand_error = abs(result['load_kw'] - total)
        generation_error = abs(result['generation_kw'] - pv)
        tolerance = max(0.1, total*1e-4)
        solver_ok = max(balance_error, demand_error, generation_error) <= tolerance
    else:
        balance_error = None
    from .injection_contract import element_injections_match
    injection_checks = element_injections_match(feeder, result, split_phases=feeder.phase_mode=='unbalanced')
    solver_ok = solver_ok and all(injection_checks.values())
    add('model.solver', solver_ok, {'converged': result.get('converged', False),
                                   'complete_finite': complete and finite,
                                   'power_balance_error_kw': balance_error,
                                   'delivered_load_error_kw': demand_error,
                                   'delivered_generation_error_kw': generation_error,
                                   'element_injection_checks': injection_checks,
                                   'error': result.get('error')})

    vuf=result.get('bus_vuf_percent',{})
    abc={b.id for b in feeder.buses if b.phases==[1,2,3]}
    vuf_complete=all(isinstance(vuf.get(b),(float,int)) and math.isfinite(vuf[b]) and vuf[b]>=0 for b in abc)
    add('research.unbalance',vuf_complete and all(vuf[b]<=spec.phase_design.max_vuf_percent+1e-9 for b in abc),
        {'maximum_percent':max((vuf[b] for b in abc),default=None) if vuf_complete else None,
         'limit_percent':spec.phase_design.max_vuf_percent,'eligible_buses':len(abc),'excluded_buses':len(feeder.buses)-len(abc),
         'definition':'100*abs(Vnegative)/abs(Vpositive); ABC buses only; research limit, not regulatory certification'},
        solver_ok and feeder.phase_mode=='unbalanced')

    pf_params = rules['cn.power_factor']['parameters']
    pf_bad = []
    for load in feeder.loads:
        threshold = pf_params['large_user_min_pf'] if load.contract_kva > pf_params['large_user_kva'] else pf_params['other_user_min_pf']
        pf = load.kw / math.hypot(load.kw, load.kvar)
        if pf < threshold-1e-9:
            pf_bad.append({'load': load.id, 'pf': pf, 'minimum': threshold})
    # With PV, gross load PF is not PCC exchange PF. Ownership/metering is not
    # represented in v0.1, so sourced customer clauses are not applicable.
    user_scope = (spec.voltage_kv >= 1 and spec.load_semantics == 'high_voltage_users' and
                  feeder.phase_mode=='balanced' and not spec.special_pf_agreement and all(x.pv_kw == 0 for x in feeder.loads))
    add('cn.power_factor', not pf_bad,
        {'violations': pf_bad, 'scope': spec.load_semantics,
         'note': 'Only non-agricultural users without PV/special agreement; PV PCC ownership is not represented; contract capacity is a research approximation'}, user_scope)

    for rule_id in ('research.voltage', 'cn.voltage'):
        p = rules[rule_id]['parameters']
        violations = []
        checked_buses = {b.id for b in feeder.buses} if rule_id=='research.voltage' else {l.bus for l in feeder.loads}
        for bus in sorted(checked_buses):
            phase_values=result.get('bus_phase_voltage_pu',{}).get(bus)
            phase_values=phase_values.items() if phase_values is not None else enumerate(vmap.get(bus,[]),1)
            for phase, value in phase_values:
                if value < p['min_pu'] or value > p['max_pu']:
                    violations.append({'bus': bus, 'phase': phase, 'voltage_pu': value})
        applicable = solver_ok and (rule_id == 'research.voltage' or
                                    (user_scope and spec.voltage_kv in (6, 10, 20) and not pf_bad and spec.mode == 'normal'))
        add(rule_id, not violations,
            {'limits': p, 'violations': violations,
             'note': 'Sourced voltage handler supports 6/10/20 kV receiving points with user/PF/normal-service prerequisites; research band is independent'}, applicable)
    catalogue = data('conductors.json')
    overloaded = []
    for line in feeder.lines:
        rating = catalogue[line.conductor]['normamps']
        current = imap.get(line.id, 0)
        if current > rating * rules['research.ampacity']['parameters']['max_loading_ratio']:
            overloaded.append({'line': line.id, 'current_a': current, 'rating_a': rating})
    add('research.ampacity', not overloaded, {'violations': overloaded}, solver_ok)
    missing = set(rules) - {check['rule_id'] for check in checks}
    if missing:
        raise ValueError(f'unimplemented rule handlers: {sorted(missing)}')
    from .knowledge import evaluate_document_rules
    checks.extend(evaluate_document_rules(feeder, spec, result))
    from .rule_system import evaluate_design_rules
    checks.extend(evaluate_design_rules(feeder, spec))
    return checks


def verdict(checks: list[dict], mode: str) -> dict:
    valid = all(c['status'] == 'pass' or
                (c['rule_id'].startswith('doc.') and c['status'] == 'not_applicable')
                for c in checks if c['category'] == 'validity')
    operational = valid and all(c['status'] != 'fail' for c in checks if c['category'] == 'operational')
    return {'valid': valid, 'operational_pass': operational,
            'accepted': valid and (operational or mode == 'stress')}
