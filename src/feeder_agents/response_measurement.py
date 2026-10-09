"""Solver-derived central differences; probes never alter the delivered model."""
import copy
import math
from pathlib import Path

import networkx as nx
import numpy as np

from .artifacts import atomic_json


def create_model(spec):
    if spec.network_kind == 'distribution':
        from .generation import generate_feeder
        return dict(kind='distribution', feeder=generate_feeder(spec, spec.seed))
    from .transmission import generate_case
    case, metadata = generate_case(spec, spec.seed)
    return dict(kind='transmission', case=case, metadata=metadata)


def resolve_probes(model, probes):
    """Freeze named ports once, including the optional distal-load selection."""
    resolved = []
    for probe in probes:
        p = probe.model_dump()
        if model['kind'] == 'distribution':
            feeder = model['feeder']; buses = {b.id: b for b in feeder.buses}
            graph = nx.Graph()
            graph.add_weighted_edges_from((e.bus1, e.bus2, e.length_km) for e in feeder.lines)
            if not nx.is_tree(graph): raise ValueError('Voltage response requires a connected radial feeder')
            if not p['injection_buses']:
                distances = nx.single_source_dijkstra_path_length(graph, feeder.source_bus)
                p['injection_buses'] = [max((l.bus for l in feeder.loads), key=lambda b: (distances[b], b))]
                p['port_selection'] = 'Farthest load bus by source-path length, frozen before search'
            p['monitor_buses'] = p['monitor_buses'] or list(p['injection_buses'])
            for bus in p['injection_buses'] + p['monitor_buses']:
                if bus not in buses or bus == feeder.source_bus: raise ValueError('Unknown or source voltage-probe bus: '+bus)
                if p['phases'] and not set(p['phases']) <= set(buses[bus].phases):
                    raise ValueError('Requested phase is absent at bus '+bus)
            p['injection_ports'] = [(b, ph) for b in p['injection_buses'] for ph in (p['phases'] or buses[b].phases)]
            p['monitor_ports'] = [(b, ph) for b in p['monitor_buses'] for ph in (p['phases'] or buses[b].phases)]
            p['protocol'] = dict(balance='Finite source equivalent: 1000/500 MVA three/single-phase short-circuit levels',
                controls='OpenDSS snapshot, controls off; fixed PQ loads and PV',
                allocation='Equal total group injection over selected bus-phase ports',
                sign='Positive means additional injection', reference_frequency_hz=feeder.frequency_hz)
        else:
            case = model['case']; ids = {str(int(b)) for b in case['bus'][:, 0]}
            if not set(p['injection_buses']+p['withdrawal_buses']) <= ids:
                raise ValueError('Unknown transmission probe bus')
            p['monitor_branches'] = p['monitor_branches'] or [i for i, e in enumerate(case['branch']) if e[10] > 0]
            if any(i < 0 or i >= len(case['branch']) or case['branch'][i, 10] <= 0 for i in p['monitor_branches']):
                raise ValueError('Unknown or inactive monitored branch')
            p['protocol'] = dict(balance='Equal MW injection and withdrawal; reference generator covers incremental AC losses',
                controls='Fixed PV-bus voltage settings and bus types; Q limits checked at reference condition',
                allocation='Equal MW per bus within each group', sign='From-terminal branch MW, fixed row orientation')
        resolved.append(p)
    return resolved


def _distribution_solve(feeder, folder, probe=None, delta=0):
    from .simulation import export_dss, simulate
    master = export_dss(feeder, folder/'opendss')
    if probe is not None:
        ports = probe['injection_ports']; amount = delta*1000/len(ports)
        commands = []
        for i, (bus, phase) in enumerate(ports):
            kw, kvar = (-amount, 0) if probe['kind'] == 'voltage_p' else (0, -amount)
            commands.append(f'New Load.response_{i} bus1={bus}.{phase}.0 phases=1 conn=wye '
                f'kv={feeder.voltage_kv/math.sqrt(3):.12g} kw={kw:.12g} kvar={kvar:.12g} '
                'model=1 vminpu=0.01 vmaxpu=2')
        master.write_text(master.read_text().replace('\nSolve\n', '\n'+'\n'.join(commands)+'\nSolve\n'))
    result = simulate(master)
    atomic_json(folder/'simulation.json', result)
    if not result['converged']: raise ValueError('OpenDSS probe did not converge')
    return result


def _transmission_solve(model, folder, probe, delta):
    from .transmission import dependencies, case_payload
    case = copy.deepcopy(model['case'])
    index = {str(int(row[0])): i for i, row in enumerate(case['bus'])}
    for group, sign in ((probe['injection_buses'], -1), (probe['withdrawal_buses'], 1)):
        for bus in group: case['bus'][index[bus], 2] += sign*delta/len(group)
    runpf, options, _ = dependencies()
    solved, ok = runpf(case, options(VERBOSE=0, OUT_ALL=0, PF_TOL=1e-9, PF_MAX_IT=30, ENFORCE_Q_LIMS=0))
    if not ok or not all(np.isfinite(solved[k]).all() for k in ('bus', 'gen', 'branch')):
        raise ValueError('AC transfer probe did not converge to finite values')
    atomic_json(folder/'simulation.json', dict(converged=True, delta_mw=delta, solution=case_payload(solved)))
    return solved


def measure(model, spec, probes, folder, step_factor=1):
    folder = Path(folder); responses = {}
    if model['kind'] == 'distribution':
        from .evaluation import evaluate, verdict
        from .simulation import export_dss, simulate
        base = simulate(export_dss(model['feeder'], folder/'base/opendss'))
        checks = evaluate(model['feeder'], spec, base)
        validation = dict(verdict(checks, spec.mode), checks=checks)
        atomic_json(folder/'base/simulation.json', base)
    else:
        from .transmission import validate_case, case_payload
        validation, base = validate_case(model['case'], spec, model['metadata'])
        if validation['converged']: atomic_json(folder/'base/simulation.json', case_payload(base))
    atomic_json(folder/'base/validation.json', validation)
    for p in probes:
        step = p['step_mw']*step_factor
        try:
            vectors = []
            for name, sign in (('plus', 1), ('minus', -1)):
                directory = folder/'probes'/p['id']/name
                if model['kind'] == 'distribution':
                    result = _distribution_solve(model['feeder'], directory, p, sign*step)
                    vector = {f'{bus}.{phase}': result['bus_phase_voltage_pu'][bus][str(phase)] for bus, phase in p['monitor_ports']}
                else:
                    result = _transmission_solve(model, directory, p, sign*step)
                    vector = {str(i): float(result['branch'][i, 13]) for i in p['monitor_branches']}
                if not vector or not all(math.isfinite(v) for v in vector.values()):
                    raise ValueError('Incomplete or nonfinite probe response')
                vectors.append(vector)
            response = {k: (vectors[0][k]-vectors[1][k])/(2*step) for k in vectors[0]}
            values = list(map(abs, response.values()))
            value = max(values) if p['aggregation'] == 'max_abs' else sum(values)/len(values)
            if not math.isfinite(value): raise ValueError('Nonfinite response sensitivity')
            unit = {'voltage_p': 'pu/MW', 'voltage_q': 'pu/MVAr', 'transfer_p': 'MW/MW'}[p['kind']]
            responses[p['id']] = dict(valid=True, value=value, signed_response=response,
                step=step, unit=unit, probe=p)
        except (ValueError, KeyError, RuntimeError) as exc:
            responses[p['id']] = dict(valid=False, value=None, error=f'{type(exc).__name__}: {exc}', probe=p)
    out = dict(base_accepted=bool(validation['accepted']), base_validation=validation,
        valid=all(p['valid'] for p in responses.values()), probes=responses)
    if model['kind'] == 'distribution': out['base_simulation'] = base
    atomic_json(folder/'response.json', out)
    return out
