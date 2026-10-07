"""Explicit phase allocation for a radial, single-voltage research equivalent."""

import math
import random

import networkx as nx

from .rules import data
from .schemas import PhasePower

ABC = [1, 2, 3]


def _rooted(feeder):
    buses = {bus.id: bus for bus in feeder.buses}
    if len(buses) != len(feeder.buses) or feeder.source_bus not in buses:
        raise ValueError('Phase supply requires unique buses and a source')
    graph = nx.Graph()
    graph.add_nodes_from(buses)
    for line in feeder.lines:
        if line.bus1 not in buses or line.bus2 not in buses or graph.has_edge(line.bus1, line.bus2):
            raise ValueError('Invalid or repeated energized phase supply edge')
        graph.add_edge(line.bus1, line.bus2)
    if not nx.is_tree(graph):
        raise ValueError('Phase supply requires a connected radial tree')
    edges = list(nx.bfs_edges(graph, feeder.source_bus))
    parent = {child: upstream for upstream, child in edges}
    children = {bus: [] for bus in buses}
    for upstream, child in edges:
        children[upstream].append(child)
    protected = set()
    for tie in feeder.tie_lines:
        if tie.bus1 not in buses or tie.bus2 not in buses:
            raise ValueError('Unknown normally-open tie endpoint')
        protected.update((tie.bus1, tie.bus2))
    eligible = sorted(bus for bus in buses if bus != feeder.source_bus and not children[bus] and bus not in protected)
    return buses, parent, children, eligible


def _counts(spec, available):
    design = spec.phase_design
    rural = spec.scenario.engineering_profile == 'rural'
    one = design.single_phase_laterals
    two = design.two_phase_laterals
    if one is not None and two is not None and one+two > available:
        raise ValueError('Requested phase laterals exceed eligible leaf buses')
    if one is None:
        one = min(max(1, available//3), max(0, available-(two or 0))) if rural else 0
    if two is None:
        two = min(available//5, max(0, available-one)) if rural else 0
    if one+two > available:
        raise ValueError('Requested phase laterals exceed eligible leaf buses')
    return one, two


def _fractions(weights, phases):
    total = sum(weights[phase-1] for phase in phases)
    return [weights[phase-1]/total for phase in phases]


def _evidence(feeder, spec, one, two):
    totals = {field: [sum(getattr(power, field) for load in feeder.loads
                          for power in load.phase_powers if power.phase == phase)
                      for phase in ABC] for field in ('kw', 'kvar', 'pv_kw')}
    if feeder.phase_mode == 'balanced':
        totals = {field: [sum(getattr(load, field) for load in feeder.loads)/3]*3
                  for field in ('kw', 'kvar', 'pv_kw')}
    evidence = {'mode': feeder.phase_mode, 'engineering_profile': spec.scenario.engineering_profile,
                'single_phase_laterals': one, 'two_phase_laterals': two,
                'load_phase_weights': list(spec.phase_design.load_phase_weights),
                'pv_phase_weights': list(spec.phase_design.pv_phase_weights or spec.phase_design.load_phase_weights),
                'scope': 'Leaf-only synthetic phase assignment; local weight renormalization, not an exact global ratio or real circuit reconstruction.'}
    for field, values in totals.items():
        evidence[f'phase_totals_{field}'] = values
        total = sum(values)
        evidence[f'phase_ratios_{field}'] = [value/total if total else 0. for value in values]
    feeder.design_evidence['phase_design'] = evidence


def assign_phases(feeder, spec):
    """Assign phases in place and return the same feeder, preserving aggregate demand."""
    buses, parent, children, eligible = _rooted(feeder)
    design = spec.phase_design
    one, two = _counts(spec, len(eligible)) if design.mode == 'unbalanced' else (0, 0)
    # Validate requested counts before changing an existing model.
    for bus in feeder.buses:
        bus.phases = list(ABC)
    if design.mode == 'unbalanced':
        rng = random.Random(f'phase-design:{feeder.seed}')
        rng.shuffle(eligible)
        for bus in eligible[:one]:
            buses[bus].phases = [rng.choice(ABC)]
        for bus in eligible[one:one+two]:
            buses[bus].phases = sorted(rng.sample(ABC, 2))
    for line in feeder.lines:
        child = line.bus2 if parent.get(line.bus2) == line.bus1 else line.bus1
        line.phases = list(buses[child].phases)
    for tie in feeder.tie_lines:
        tie.phases = sorted(set(buses[tie.bus1].phases) & set(buses[tie.bus2].phases))
    for load in feeder.loads:
        load.phases = list(buses[load.bus].phases)
        load.phase_powers = []
        if design.mode == 'unbalanced':
            fractions = _fractions(design.load_phase_weights, load.phases)
            pv_fractions = _fractions(design.pv_phase_weights or design.load_phase_weights, load.phases)
            load.phase_powers = [PhasePower(phase=phase, kw=load.kw*fraction,
                kvar=load.kvar*fraction, pv_kw=load.pv_kw*pv_fraction)
                for phase, fraction, pv_fraction in zip(load.phases, fractions, pv_fractions)]
    feeder.phase_mode = design.mode
    _evidence(feeder, spec, one, two)
    return feeder


def _valid_phases(phases):
    return isinstance(phases, list) and bool(phases) and all(type(p) is int and p in ABC for p in phases) and len(set(phases)) == len(phases)


def phase_contract_matches(feeder, spec):
    """Check actual supply, scalar/vector conservation, local weights and lateral counts."""
    try:
        buses, parent, children, eligible = _rooted(feeder)
        if feeder.phase_mode != spec.phase_design.mode:
            return False
        if any(not _valid_phases(bus.phases) for bus in feeder.buses) or set(buses[feeder.source_bus].phases) != set(ABC):
            return False
        balanced = feeder.phase_mode == 'balanced'
        if any((balanced or children[bus.id]) and set(bus.phases) != set(ABC) for bus in feeder.buses):
            return False
        for line in feeder.lines:
            child = line.bus2 if parent.get(line.bus2) == line.bus1 else line.bus1
            upstream = parent[child]
            if (not _valid_phases(line.phases) or set(line.phases) != set(buses[child].phases)
                    or not set(line.phases).issubset(buses[upstream].phases)):
                return False
        for tie in feeder.tie_lines:
            if (not _valid_phases(tie.phases) or set(tie.phases) != set(ABC)
                    or any(set(buses[bus].phases) != set(ABC) for bus in (tie.bus1, tie.bus2))):
                return False
        if not balanced:
            one, two = _counts(spec, len(eligible))
            if (sum(len(bus.phases)==1 for bus in feeder.buses), sum(len(bus.phases)==2 for bus in feeder.buses)) != (one, two):
                return False
        for load in feeder.loads:
            if not _valid_phases(load.phases) or load.bus not in buses or not set(load.phases).issubset(buses[load.bus].phases):
                return False
            if balanced:
                if set(load.phases) != set(ABC) or load.phase_powers:
                    return False
                continue
            powers = {power.phase: power for power in load.phase_powers}
            if len(powers) != len(load.phase_powers) or set(powers) != set(load.phases):
                return False
            fractions = _fractions(spec.phase_design.load_phase_weights, load.phases)
            pv_fractions = _fractions(spec.phase_design.pv_phase_weights or spec.phase_design.load_phase_weights, load.phases)
            for field in ('kw', 'kvar', 'pv_kw'):
                scalar = getattr(load, field)
                values = [getattr(powers[phase], field) for phase in load.phases]
                weights = pv_fractions if field == 'pv_kw' else fractions
                if not math.isfinite(scalar) or scalar < 0 or any(not math.isfinite(value) or value < 0 for value in values):
                    return False
                if not math.isclose(sum(values), scalar, rel_tol=1e-8, abs_tol=1e-7):
                    return False
                if any(not math.isclose(value, scalar*weight, rel_tol=1e-8, abs_tol=1e-7) for value, weight in zip(values, weights)):
                    return False
        return True
    except (ValueError, KeyError, TypeError, AttributeError, ZeroDivisionError, nx.NetworkXException):
        return False


def scale_phase_load(load, load_scale, pv_scale):
    """Scale scalar demand/contract and phase vectors in place with independent PV."""
    if not math.isfinite(load_scale) or load_scale <= 0 or not math.isfinite(pv_scale) or pv_scale < 0:
        raise ValueError('Load scaling must be positive and PV scaling nonnegative')
    load.kw *= load_scale
    load.kvar *= load_scale
    load.contract_kva *= load_scale
    load.pv_kw *= pv_scale
    for power in load.phase_powers:
        power.kw *= load_scale
        power.kvar *= load_scale
        power.pv_kw *= pv_scale
    return load


def size_phase_conductors(feeder):
    """Size on the most loaded phase at gross-load/full-PV endpoints, excluding losses."""
    buses, parent, children, _ = _rooted(feeder)
    totals = {bus: {phase: [0., 0., 0.] for phase in ABC} for bus in buses}
    for load in feeder.loads:
        for power in load.phase_powers:
            for i, value in enumerate((power.kw, power.kvar, power.pv_kw)):
                totals[load.bus][power.phase][i] += value
        if not load.phase_powers:
            for phase in load.phases:
                for i, value in enumerate((load.kw, load.kvar, load.pv_kw)):
                    totals[load.bus][phase][i] += value/len(load.phases)
    for child in reversed(parent):
        for phase in ABC:
            totals[parent[child]][phase] = [a+b for a,b in zip(totals[parent[child]][phase], totals[child][phase])]
    catalogue = data('conductors.json')
    grades = sorted(('research_small','research_medium','research_large'), key=lambda grade: catalogue[grade]['normamps'])
    records = []
    for line in feeder.lines:
        child = line.bus2 if parent.get(line.bus2) == line.bus1 else line.bus1
        currents = {}
        for phase in line.phases:
            p,q,pv = totals[child][phase]
            currents[str(phase)] = max(math.hypot(p,q),math.hypot(p-pv,q))/(feeder.voltage_kv/math.sqrt(3))
        amps = max(currents.values())
        eligible = [grade for grade in grades if catalogue[grade]['normamps']*.8 >= amps]
        line.conductor = eligible[0] if eligible else grades[-1]
        records.append({'line_id':line.id, 'estimated_current_a':amps, 'phase_current_a':currents,
            'initial_conductor':line.conductor, 'planning_loading_limit':.8,
            'catalogue_margin_satisfied':bool(eligible),
            'scope':'Synthetic sequence-impedance conductor catalogue; maximum phase gross-load/full-PV endpoint currents, no losses or neutral sizing.'})
    return records
