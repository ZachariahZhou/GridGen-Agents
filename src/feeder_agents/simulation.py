from math import sqrt
from pathlib import Path

from .rules import data
from .schemas import Feeder


def export_dss(feeder: Feeder, directory: Path) -> Path:
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    catalogue = data('conductors.json')
    codes = []
    for name in ('research_small','research_medium','research_large'):
        c=catalogue[name]
        codes.append(f'New LineCode.{name} nphases=3 units=km '
                     f'r1={c["r1"]} x1={c["x1"]} r0={c["r0"]} x0={c["x0"]} '
                     f'c1=0 c0=0 normamps={c["normamps"]} basefreq={feeder.frequency_hz}')
    unbalanced = feeder.phase_mode == 'unbalanced'
    profiles = None
    used_codes = set()
    lines = []
    for line in [*feeder.lines, *feeder.tie_lines]:
        phases = line.phases if unbalanced else [1, 2, 3]
        code = line.conductor
        if 'source_feeder' in catalogue[line.conductor]:
            c=catalogue[line.conductor]
            if c['nphases']!=len(phases) or c['construction']!=line.construction:
                raise ValueError('Sourced equipment phase/construction mismatch')
            if code not in used_codes:
                matrices=[]
                for quantity in ('r','x','c'):
                    matrix=c[f'{quantity}matrix'];n=len(phases)
                    factor=feeder.frequency_hz/c['source_frequency_hz'] if quantity=='x' else 1
                    rows=[' '.join(f'{matrix[i*n+j]*factor:.12g}' for j in range(i+1)) for i in range(n)]
                    matrices.append(f'{quantity}matrix=[{" | ".join(rows)}]')
                codes.append(f'! source={c["source_feeder"]}/{c["source_linecode"]}; sha256={c["source_sha256"]}; X frequency-scaled, R/C retained')
                codes.append(f'New LineCode.{code} nphases={len(phases)} units=km basefreq={feeder.frequency_hz} {" ".join(matrices)} normamps={c["normamps"]} rg=0 xg=0')
                used_codes.add(code)
        elif unbalanced or line.construction != 'illustrative':
            code = f'{line.conductor}_{line.construction}_{len(phases)}ph'
            if code not in used_codes:
                if profiles is None:
                    profiles = data('construction_profiles.json')
                profile = profiles[line.construction]
                conductor = catalogue[line.conductor]
                sequence = {key: conductor[key]*profile[f'{key}_factor']
                            for key in ('r1', 'r0', 'x1', 'x0')}
                sequence.update(c1=profile['c1_nf_per_km'], c0=profile['c0_nf_per_km'])
                matrices = []
                for quantity in ('r', 'x', 'c'):
                    positive, zero = sequence[f'{quantity}1'], sequence[f'{quantity}0']
                    diagonal, mutual = (zero+2*positive)/3, (zero-positive)/3
                    # DSS lower-triangular matrix syntax, with rows separated by |.
                    rows = [' '.join(f'{(diagonal if i == j else mutual):.12g}'
                                     for j in range(i+1)) for i in range(len(phases))]
                    matrices.append(f'{quantity}matrix=[{" | ".join(rows)}]')
                codes.append(f'New LineCode.{code} nphases={len(phases)} units=km '
                             f'basefreq={feeder.frequency_hz} {" ".join(matrices)} '
                             f'normamps={conductor["normamps"]}')
                used_codes.add(code)
        nodes = '.'.join(str(phase) for phase in phases)
        lines.append(f'New Line.{line.id} bus1={line.bus1}.{nodes} bus2={line.bus2}.{nodes} '
                     f'phases={len(phases)} linecode={code} length={line.length_km:.12g} units=km '
                     f'normamps={catalogue[line.conductor]["normamps"]}')
    # Explicitly open every conductor at both terminals before any solve.
    lines.extend(f'Open Line.{line.id} term={terminal} cond=0'
                 for line in feeder.tie_lines for terminal in (1, 2))
    loads, pvs = [], []
    for load in feeder.loads:
        if unbalanced:
            if not load.phase_powers:
                raise ValueError(f'Unbalanced load {load.id} requires explicit phase powers')
            connections = [(f'{load.id}_p{p.phase}', f'{load.bus}.{p.phase}.0', 1,
                            feeder.voltage_kv/sqrt(3), p.kw, p.kvar, p.pv_kw)
                           for p in load.phase_powers]
        else:
            connections = [(load.id, f'{load.bus}.1.2.3', 3, feeder.voltage_kv,
                            load.kw, load.kvar, load.pv_kw)]
        for name, bus, phases, kv, kw, kvar, pv_kw in connections:
            loads.append(f'New Load.{name} bus1={bus} phases={phases} conn=wye '
                         f'kv={kv:.12g} kw={kw:.12g} kvar={kvar:.12g} '
                         'model=1 vminpu=0.01 vmaxpu=2')
            if pv_kw > 0:
                pvs.append(f'New Generator.pv_{name} bus1={bus} phases={phases} conn=wye '
                           f'kv={kv:.12g} kw={pv_kw:.12g} kvar=0 model=1 '
                           'vminpu=0.01 vmaxpu=2')
    for filename, contents in [('LineCodes.dss', codes), ('Lines.dss', lines),
                               ('Loads.dss', loads), ('Generation.dss', pvs)]:
        (directory / filename).write_text('\n'.join(contents) + '\n', encoding='utf-8')
    (directory / 'BusCoords.csv').write_text('\n'.join(
        f'{b.id}, {b.x_km:.12g}, {b.y_km:.12g}' for b in feeder.buses), encoding='utf-8')
    master = directory / 'Master.dss'
    master.write_text('\n'.join([
        '! Synthetic research case; parameters are illustrative, not a construction design.',
        'Clear', f'Set DefaultBaseFrequency={feeder.frequency_hz}',
        f'New Circuit.feeder basekv={feeder.voltage_kv} pu=1 phases=3 '
        f'bus1={feeder.source_bus}.1.2.3 frequency={feeder.frequency_hz} mvasc3=1000 mvasc1=500',
        'Redirect LineCodes.dss', 'Redirect Lines.dss', 'Redirect Loads.dss',
        'Redirect Generation.dss', f'Set VoltageBases=[{feeder.voltage_kv}]',
        'CalcVoltageBases', 'BusCoords BusCoords.csv',
        'Set mode=snapshot controlmode=off maxiterations=100 tolerance=1e-8', 'Solve', ''
    ]), encoding='utf-8')
    return master


def simulate(master: Path) -> dict:
    """A fresh engine context per solve; no shared engine state or cwd mutation."""
    from opendssdirect import dss

    engine = dss.NewContext()
    engine.Basic.AllowChangeDir(False)
    engine.Text.Command(f'Compile "{Path(master).resolve()}"')
    converged = bool(engine.Solution.Converged())
    voltages = {}
    bus_bases = {}
    phase_nodes, phase_voltages, complex_voltages, vuf = {}, {}, {}, {}
    rotation = complex(-.5, sqrt(3)/2)
    for bus in engine.Circuit.AllBusNames():
        engine.Circuit.SetActiveBus(bus)
        bus_bases[bus] = engine.Bus.kVBase()
        nodes = engine.Bus.Nodes()
        pu = engine.Bus.puVmagAngle()[::2]
        raw = engine.Bus.Voltages()
        selected = [(i, node) for i, node in enumerate(nodes) if node in (1, 2, 3)]
        phase_nodes[bus] = [node for _, node in selected]
        phase_voltages[bus] = {str(node): pu[i] for i, node in selected}
        complex_voltages[bus] = {str(node): list(raw[2*i:2*i+2]) for i, node in selected}
        voltages[bus] = [pu[i] for i, _ in selected]
        vuf[bus] = None
        if set(phase_nodes[bus]) == {1, 2, 3}:
            a, b, c = (complex(*complex_voltages[bus][str(p)]) for p in (1, 2, 3))
            positive = (a+rotation*b+rotation**2*c)/3
            negative = (a+rotation**2*b+rotation*c)/3
            if abs(positive) > 1e-9:
                vuf[bus] = 100*abs(negative)/abs(positive)
    currents = {}
    phase_currents, terminal_phase_currents = {}, {}
    line_terminal_mva = {}
    line_open_status = {}
    line_phase_open_status = {}
    for line in engine.Lines:
        line_phase_open_status[line.Name()] = {
            f'terminal_{terminal}': [bool(engine.CktElement.IsOpen(terminal, phase))
                                    for phase in range(1, engine.CktElement.NumConductors()+1)]
            for terminal in (1, 2)}
        line_open_status[line.Name()] = {
            terminal: all(phases)
            for terminal, phases in line_phase_open_status[line.Name()].items()}
        currents[line.Name()] = max(engine.CktElement.CurrentsMagAng()[::2], default=0)
        powers = engine.CktElement.Powers()
        conductors = engine.CktElement.NumConductors()
        nodes = engine.CktElement.NodeOrder()
        magnitudes = engine.CktElement.CurrentsMagAng()[::2]
        terminal_phase_currents[line.Name()] = {
            f'terminal_{terminal+1}': {
                str(nodes[i]): magnitudes[i]
                for i in range(terminal*conductors, (terminal+1)*conductors)
                if nodes[i] in (1, 2, 3)}
            for terminal in range(engine.CktElement.NumTerminals())}
        per_phase = {}
        for terminal in terminal_phase_currents[line.Name()].values():
            for phase, magnitude in terminal.items():
                per_phase[phase] = max(per_phase.get(phase, 0), magnitude)
        phase_currents[line.Name()] = per_phase
        line_terminal_mva[line.Name()] = [
            (sum(powers[start:start+2*conductors:2])**2 + sum(powers[start+1:start+2*conductors:2])**2)**.5/1000
            for start in range(0,len(powers),2*conductors)]
    load_element_kw, load_element_kvar = {}, {}
    for load in engine.Loads:
        powers = engine.CktElement.Powers()[:2*engine.CktElement.NumConductors()]
        load_element_kw[load.Name()] = sum(powers[::2])
        load_element_kvar[load.Name()] = sum(powers[1::2])
    generation_element_kw, generation_element_kvar = {}, {}
    for generator in engine.Generators:
        powers = engine.CktElement.Powers()[:2*engine.CktElement.NumConductors()]
        generation_element_kw[generator.Name()] = -sum(powers[::2])
        generation_element_kvar[generator.Name()] = -sum(powers[1::2])
    load_kw = sum(load_element_kw.values())
    generation_kw = sum(generation_element_kw.values())
    transformer_metrics = {}
    for transformer in engine.Transformers:
        name = transformer.Name()
        conductors = engine.CktElement.NumConductors()
        currents_tx = engine.CktElement.CurrentsMagAng()[::2]
        powers_tx = engine.CktElement.Powers()
        terminals = engine.CktElement.NumTerminals()
        transformer_phases = engine.CktElement.NumPhases()
        nodes_tx = engine.CktElement.NodeOrder()
        terminal_kva, ratios = [], []
        for terminal in range(terminals):
            transformer.Wdg(terminal+1)
            rated = transformer.kVA()/((sqrt(3) if transformer_phases==3 else 1)*transformer.kV())
            start = terminal*conductors
            phase_currents_tx = [currents_tx[i] for i in range(start,start+conductors) if nodes_tx[i] in (1,2,3)]
            ratios.extend(current/rated for current in phase_currents_tx)
            pos = start*2
            terminal_kva.append(abs(complex(sum(powers_tx[pos:pos+2*conductors:2]),sum(powers_tx[pos+1:pos+2*conductors:2]))))
        transformer_metrics[name] = {'terminal_kva':terminal_kva,'max_phase_loading_ratio':max(ratios,default=0)}
    eligible_vuf = [value for value in vuf.values() if value is not None]
    flat_v = [v for values in voltages.values() for v in values]
    return {
        'converged': converged, 'engine_version': engine.Basic.Version(),
        'engine_family': 'DSS-Extensions / OpenDSSDirect.py',
        'scenario': 'coincident_peak_fixed_pv_snapshot',
        'iterations': engine.Solution.Iterations(),
        'bus_phase_nodes': phase_nodes,
        'bus_base_kv_ln': bus_bases,
        'transformer_metrics': transformer_metrics,
        'bus_phase_voltage_pu': phase_voltages,
        'bus_phase_voltage_complex': complex_voltages,
        'bus_vuf_percent': vuf,
        'max_vuf_percent': max(eligible_vuf, default=None),
        'vuf_eligible_bus_count': len(eligible_vuf),
        'vuf_ineligible_bus_count': len(vuf)-len(eligible_vuf),
        'line_phase_current_a': phase_currents,
        'line_terminal_phase_current_a': terminal_phase_currents,
        'load_element_kw': load_element_kw, 'generation_element_kw': generation_element_kw,
        'load_element_kvar': load_element_kvar, 'generation_element_kvar': generation_element_kvar,
        'bus_voltage_pu': voltages, 'line_current_a': currents, 'line_terminal_mva': line_terminal_mva,
        'line_open_status': line_open_status,
        'line_phase_open_status': line_phase_open_status,
        'min_voltage_pu': min(flat_v, default=0), 'max_voltage_pu': max(flat_v, default=0),
        'source_kw': -engine.Circuit.TotalPower()[0],
        'loss_kw': engine.Circuit.Losses()[0]/1000,
        'load_kw': load_kw, 'generation_kw': generation_kw,
        'load_kvar': sum(load_element_kvar.values()), 'generation_kvar': sum(generation_element_kvar.values()),
        'source_kvar': -engine.Circuit.TotalPower()[1], 'loss_kvar': engine.Circuit.Losses()[1]/1000,
        'measurement_contract_version': 'element_pq_v1',
    }
