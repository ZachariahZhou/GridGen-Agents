"""Strict data-only MATPOWER import and balanced reference export.

No MATLAB, eval, subprocess, or general expression interpreter is used. Only
literal version-2 matrices and two exact distribution-case conversion tails
are understood. Unknown code is rejected, including otherwise benign metadata.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re

import networkx as nx


_OHM_TAIL = """
[PQ, PV, REF, NONE, BUS_I, BUS_TYPE, PD, QD, GS, BS, BUS_AREA, VM,
VA, BASE_KV, ZONE, VMAX, VMIN, LAM_P, LAM_Q, MU_VMAX, MU_VMIN] = idx_bus;
[F_BUS, T_BUS, BR_R, BR_X, BR_B, RATE_A, RATE_B, RATE_C,
TAP, SHIFT, BR_STATUS, PF, QF, PT, QT, MU_SF, MU_ST,
ANGMIN, ANGMAX, MU_ANGMIN, MU_ANGMAX] = idx_brch;
Vbase = mpc.bus(1, BASE_KV) * 1e3;
Sbase = mpc.baseMVA * 1e6;
mpc.branch(:, [BR_R BR_X]) = mpc.branch(:, [BR_R BR_X]) / (Vbase^2 / Sbase);
mpc.bus(:, [PD, QD]) = mpc.bus(:, [PD, QD]) / 1e3;
"""
_PF_TAIL = """
pf = 0.85;
mpc.bus(:, QD) = mpc.bus(:, PD) * sin(acos(pf));
mpc.bus(:, PD) = mpc.bus(:, PD) * pf;
"""
_NUMBER = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eEdD][+-]?\d+)?'


def _compact(text: str) -> str:
    return re.sub(r'\s+', '', text)


def _matrix(text: str, name: str) -> list[list[float]]:
    rows = []
    for line in re.split(r';|\n', text):
        if not line.strip():
            continue
        tokens = re.split(r'[\s,]+', line.strip())
        if any(not re.fullmatch(_NUMBER, token) for token in tokens):
            raise ValueError(f'Unsupported numeric expression in {name}')
        row = [float(re.sub('[dD]', 'e', token)) for token in tokens]
        if not all(math.isfinite(value) for value in row):
            raise ValueError(f'Nonfinite value in {name}')
        rows.append(row)
    if rows and len({len(row) for row in rows}) != 1:
        raise ValueError(f'Inconsistent columns in {name}')
    return rows


def parse_matpower(path: Path) -> dict:
    """Read canonical MW/MVAr, kV, per-unit tables; reject unknown code."""
    path = Path(path)
    data = path.read_bytes()
    text = data.decode('utf-8-sig')
    text = re.sub(r'%[^\n]*', '', text)
    text = re.sub(r'\.\.\.\s*\n', ' ', text).strip()
    function = re.match(r'function\s+mpc\s*=\s*([A-Za-z]\w*)\s*(?:\(\s*\))?\s*\n', text)
    if not function:
        raise ValueError('Unsupported MATPOWER function declaration')
    name = function.group(1)
    text = text[function.end():].strip()
    tables: dict = {}
    while text:
        match = re.match(r"mpc\.(version|baseMVA|bus|branch|gen|gencost)\s*=\s*(\[[\s\S]*?\]|'2'|" + _NUMBER + r')\s*;', text)
        if not match:
            break
        field, value = match.groups()
        if field in tables:
            raise ValueError(f'Unsupported duplicate assignment: {field}')
        if field in {'bus', 'branch', 'gen', 'gencost'}:
            if not value.startswith('['):
                raise ValueError(f'Expected matrix for {field}')
            tables[field] = _matrix(value[1:-1], field)
        elif field == 'version':
            if value != "'2'":
                raise ValueError('Only MATPOWER version 2 is supported')
            tables[field] = '2'
        else:
            tables[field] = float(value)
        text = text[match.end():].strip()
    for key in ('version', 'baseMVA', 'bus', 'branch', 'gen'):
        if key not in tables:
            raise ValueError(f'Missing MATPOWER {key}')
    if not math.isfinite(tables['baseMVA']) or tables['baseMVA'] <= 0:
        raise ValueError('baseMVA must be positive and finite')
    for key, width in [('bus', 13), ('branch', 13), ('gen', 10)]:
        if not tables[key] or any(len(row) < width for row in tables[key]):
            raise ValueError(f'{key} requires nonempty rows with at least {width} columns')
    notes = []
    tail = _compact(text)
    if tail.endswith('end'):
        tail = tail[:-3]
    if tail in {_compact(_OHM_TAIL), _compact(_OHM_TAIL + _PF_TAIL)}:
        kv = tables['bus'][0][9]
        if kv <= 0:
            raise ValueError('Conversion requires positive base kV')
        zbase = kv * kv / tables['baseMVA']
        for row in tables['branch']:
            row[2] /= zbase
            row[3] /= zbase
        for row in tables['bus']:
            row[2] /= 1000
            row[3] /= 1000
        notes.extend(['Applied explicit Ohm-to-pu conversion using first bus base kV.', 'Applied explicit kW/kVAr-to-MW/MVAr division by 1000.'])
        if tail == _compact(_OHM_TAIL + _PF_TAIL):
            for row in tables['bus']:
                row[3] = row[2] * math.sqrt(1 - .85**2)
                row[2] *= .85
            notes.append('Applied explicit apparent-load conversion with power factor 0.85.')
    elif tail:
        raise ValueError(f'Unsupported executable MATPOWER content: {text[:100]}')
    case = dict(name=name, base_mva=tables['baseMVA'], bus=tables['bus'], branch=tables['branch'], gen=tables['gen'], conversion_notes=notes, source_sha256=hashlib.sha256(data).hexdigest())
    _validate(case)
    return case


def _validate(case: dict) -> None:
    if not math.isfinite(case['base_mva']) or case['base_mva'] <= 0:
        raise ValueError('base_mva must be positive and finite')
    for key, width in [('bus', 13), ('branch', 13), ('gen', 10)]:
        if not case[key] or any(len(row) < width for row in case[key]):
            raise ValueError(f'Invalid {key} matrix shape')
        if any(not math.isfinite(value) for row in case[key] for value in row):
            raise ValueError(f'All {key} values must be finite')
    buses = case['bus']
    ids = [row[0] for row in buses]
    if any(value != int(value) or value <= 0 for value in ids) or len(set(ids)) != len(ids):
        raise ValueError('Bus IDs must be unique positive integers')
    if any(row[1] not in (1, 2, 3, 4) or row[9] <= 0 for row in buses):
        raise ValueError('Invalid bus type or base kV')
    for row in case['branch']:
        if row[0] not in ids or row[1] not in ids or row[10] not in (0, 1):
            raise ValueError('Invalid branch endpoint or status')
    for row in case['gen']:
        if row[0] not in ids or row[7] not in (0, 1):
            raise ValueError('Invalid generator bus or status')


def analyze_case(case: dict) -> dict:
    """Return JSON-safe quality, topology, empirical statistics and export gates."""
    _validate(case)
    buses = {int(row[0]): row for row in case['bus']}
    refs = [int(row[0]) for row in case['bus'] if row[1] == 3]
    active = [row for row in case['branch'] if row[10] == 1]
    graph = nx.MultiGraph()
    graph.add_nodes_from(buses)
    graph.add_edges_from((int(row[0]), int(row[1])) for row in active)
    connected = nx.is_connected(graph)
    radial = connected and len(active) == len(buses) - 1
    kv = sorted({row[9] for row in buses.values()})
    loads = [row[2] for row in buses.values() if row[2] > 0]
    nonsource = [row for key, row in buses.items() if key not in refs]
    generators = [row for row in case['gen'] if row[7] == 1]
    blockers = []
    checks = [
        (len(kv) != 1, 'single voltage level required'),
        (len(refs) != 1, 'exactly one REF bus required'),
        (len(generators) != 1 or any(row[0] not in refs for row in generators), 'exactly one active source generator required; non-source generators unsupported'),
        (any(row[5] <= 0 for row in generators), 'positive generator voltage setpoint required'),
        (any(row[1] < 0 for row in generators), 'negative generator active-power dispatch unsupported'),
        (not connected, 'active network must be connected'),
        (not radial, 'active network must be radial'),
        (any(row[1] == 2 for row in buses.values()), 'PV buses unsupported'),
        (any(row[1] == 4 for row in buses.values()), 'isolated bus types unsupported'),
        (any(row[2] < 0 or row[3] < 0 for row in buses.values()), 'negative loads unsupported'),
        (any(row[4] != 0 or row[5] != 0 for row in buses.values()), 'bus shunts unsupported'),
        (any(row[4] != 0 for row in case['branch']), 'branch charging unsupported'),
        (any(row[8] not in (0, 1) or row[9] != 0 for row in case['branch']), 'transformer taps or phase shifts unsupported'),
        (any(row[2] < 0 or row[3] < 0 or row[2] == row[3] == 0 for row in active), 'negative or zero series impedance unsupported'),
        (any(row[7] <= 0 for row in buses.values()), 'positive voltage magnitudes required'),
    ]
    blockers.extend(message for failed, message in checks if failed)
    branches = []
    for index, row in enumerate(case['branch']):
        zbase = buses[int(row[0])][9] ** 2 / case['base_mva']
        branches.append(dict(index=index, from_bus=int(row[0]), to_bus=int(row[1]), active=bool(row[10]), r_ohm=row[2] * zbase, x_ohm=row[3] * zbase, rate_a_mva=row[5] if row[5] > 0 else None))
    offspring = None
    if radial and len(refs) == 1:
        tree = nx.bfs_tree(graph, refs[0])
        offspring = [tree.out_degree(node) for node in sorted(tree)]
    return dict(name=case['name'], bus_count=len(buses), branch_count=len(case['branch']), active_branch_count=len(active), inactive_branch_count=len(case['branch'])-len(active), reference_buses=refs, voltage_levels_kv=kv, connected=connected, radial=radial, component_count=nx.number_connected_components(graph), total_p_mw=sum(row[2] for row in buses.values()), total_q_mvar=sum(row[3] for row in buses.values()), positive_load_count=len(loads), positive_load_weights=[value / sum(loads) for value in loads], zero_load_non_source_fraction=sum(row[2] == 0 and row[3] == 0 for row in nonsource) / len(nonsource) if nonsource else 0.0, offspring_counts=offspring, branches=branches, export_supported=not blockers, export_blockers=blockers)


def export_reference_dss(case: dict, directory: Path) -> Path:
    """Write a standalone, balanced positive-sequence-equivalent DSS reference.

    No geometric length or conductor type is inferred. Unit-length line objects
    hold the complete branch impedance. Zero MATPOWER ratings stay unspecified.
    """
    summary = analyze_case(case)
    if not summary['export_supported']:
        raise ValueError('Unsupported reference export: ' + '; '.join(summary['export_blockers']))
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    source_id = summary['reference_buses'][0]
    source = next(row for row in case['bus'] if row[0] == source_id)
    kv = source[9]
    source_voltage = next(row[5] for row in case['gen'] if row[7] == 1 and row[0] == source_id)
    lines = ['clear', 'set defaultbasefrequency=50', '! Balanced positive-sequence equivalent; no geometry or conductor inference.', '! Source short-circuit strength 1e9 MVA approximates the MATPOWER fixed slack.', f'new circuit.reference phases=3 bus1=b{source_id} basekv={kv:.12g} pu={source_voltage:.12g} angle={source[8]:.12g} frequency=50 mvasc3=1e9 mvasc1=1e9']
    for branch in summary['branches']:
        row = case['branch'][branch['index']]
        lines.append(f'new line.l{branch["index"] + 1} phases=3 bus1=b{int(row[0])} bus2=b{int(row[1])} r1={branch["r_ohm"]:.12g} x1={branch["x_ohm"]:.12g} r0={branch["r_ohm"]:.12g} x0={branch["x_ohm"]:.12g} c1=0 c0=0 length=1 units=none enabled={"yes" if branch["active"] else "no"}')
        # DSS defaults are not evidence of a thermal rating. Only source ratings
        # are exposed in reference_metadata.json and downstream loading analysis.
        if branch['rate_a_mva'] is not None:
            amps = branch['rate_a_mva'] * 1000 / (math.sqrt(3) * kv)
            lines.append(f'edit line.l{branch["index"] + 1} normamps={amps:.12g} emergamps={amps:.12g}')
    for row in case['bus']:
        if row[2] != 0 or row[3] != 0:
            lines.append(f'new load.load{int(row[0])} phases=3 bus1=b{int(row[0])} conn=wye model=1 kv={kv:.12g} kw={row[2]*1000:.12g} kvar={row[3]*1000:.12g} vminpu=0.01 vmaxpu=2')
    lines.extend([f'set voltagebases=[{kv:.12g}]', 'calcvoltagebases', 'set maxiterations=100', 'solve'])
    master = directory / 'master.dss'
    master.write_text('\n'.join(lines) + '\n')
    metadata = dict(summary, source_sha256=case['source_sha256'], conversion_notes=case['conversion_notes'], assumptions=['Balanced three-phase positive-sequence equivalent; not an unbalanced feeder model.', 'Source strength 1e9 MVA approximates ideal fixed MATPOWER slack.', '50 Hz bookkeeping frequency; explicit series impedances, no charging.', 'Line length=1 units=none is a branch-impedance carrier, not a physical length.', 'Zero-sequence impedance copied from positive sequence only to define DSS objects; no sequence-zero analysis supported.', 'Missing thermal ratings remain unknown; DSS default ratings must not be interpreted as data.'])
    (directory / 'reference_metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')
    return master
