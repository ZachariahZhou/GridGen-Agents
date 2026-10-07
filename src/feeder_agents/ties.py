"""Bounded normally-open links; the operating graph remains a radial tree."""

from itertools import combinations
from math import hypot, isclose, isfinite

import networkx as nx

from .artifacts import digest
from .rules import data
from .schemas import ExperimentSpec, Feeder, Line


class TieConstructionError(ValueError):
    """Exhaustive geometric candidate search failed before publishing a model."""

    def __init__(self, diagnostic):
        self.diagnostic = diagnostic
        super().__init__('Insufficient eligible normally-open tie links within segment bounds; bounds were not relaxed')


def tie_contract_matches(feeder: Feeder, spec: ExperimentSpec, result: dict | None = None) -> bool:
    """Check physical links separately from the energized topology and solve state."""
    buses = {bus.id: bus for bus in feeder.buses}
    if (len({name.casefold() for name in buses}) != len(feeder.buses)
            or feeder.source_bus not in buses):
        return False
    if len(feeder.tie_lines) != spec.scenario.tie_count:
        return False
    catalogue = data('conductors.json')
    ids, edges = set(), set()
    active = nx.Graph()
    active.add_nodes_from(buses)
    for line in [*feeder.lines, *feeder.tie_lines]:
        key = line.id.casefold()  # OpenDSS device names are case insensitive.
        edge = frozenset((line.bus1, line.bus2))
        if (key in ids or edge in edges or len(edge) != 2
                or not edge.issubset(buses) or line.conductor not in catalogue
                or not isfinite(line.length_km) or line.length_km <= 0):
            return False
        ids.add(key)
        edges.add(edge)
    for line in feeder.lines:
        active.add_edge(line.bus1, line.bus2)
    if not nx.is_tree(active):
        return False
    for line in feeder.tie_lines:
        a, b = buses[line.bus1], buses[line.bus2]
        distance = hypot(a.x_km-b.x_km, a.y_km-b.y_km)
        if (not spec.segment_km_min-1e-12 <= line.length_km <= spec.segment_km_max+1e-12
                or not isclose(line.length_km, distance, rel_tol=1e-9, abs_tol=1e-12)):
            return False
    if result is not None:
        statuses = result.get('line_open_status', {})
        phase_statuses = result.get('line_phase_open_status', {})
        currents = result.get('line_current_a', {})
        if (not isinstance(statuses, dict) or not isinstance(phase_statuses, dict)
                or not isinstance(currents, dict)):
            return False
        tie_ids = {line.id for line in feeder.tie_lines}
        for line in [*feeder.lines, *feeder.tie_lines]:
            status = statuses.get(line.id.casefold())
            expected = line.id in tie_ids
            if (not isinstance(status, dict)
                    or status.get('terminal_1') is not expected
                    or status.get('terminal_2') is not expected):
                return False
            phases = phase_statuses.get(line.id.casefold())
            if not isinstance(phases, dict):
                return False
            for terminal in ('terminal_1', 'terminal_2'):
                states = phases.get(terminal)
                if (not isinstance(states, list) or len(states) != 3
                        or any(state is not expected for state in states)):
                    return False
            if expected:
                current = currents.get(line.id.casefold())
                if (not isinstance(current, (int, float)) or not isfinite(current)
                        or abs(current) > 1e-6):
                    return False
    return True


def attach_ties(feeder: Feeder, spec: ExperimentSpec) -> Feeder:
    """Greedily cover uncovered supply paths within the original geometric bounds."""
    output = feeder.model_copy(deep=True)
    remaining = spec.scenario.tie_count-len(output.tie_lines)
    if remaining < 0:
        raise ValueError('Existing normally-open ties exceed requested tie_count')
    # Validate existing physical ties before extending them, using their count.
    existing_spec = spec.model_copy(deep=True)
    existing_spec.scenario.tie_count = len(output.tie_lines)
    if not tie_contract_matches(output, existing_spec):
        raise ValueError('Invalid existing active topology or normally-open tie contract')
    occupied = {frozenset((line.bus1, line.bus2)) for line in [*output.lines, *output.tie_lines]}

    evidence = dict(code='tie_candidates_exhausted', spec_hash=digest(spec.model_dump()),
                    seed=feeder.seed, requested_tie_count=spec.scenario.tie_count,
                    existing_tie_count=len(output.tie_lines), required_new_ties=remaining,
                    eligible_tie_count=0, unoccupied_pair_count=0, nearest_unoccupied_km=None,
                    segment_km_min=spec.segment_km_min, segment_km_max=spec.segment_km_max)

    def candidates():
        for a, b in combinations(sorted(output.buses, key=lambda bus: bus.id), 2):
            if frozenset((a.id, b.id)) in occupied:
                continue
            length = hypot(a.x_km-b.x_km, a.y_km-b.y_km)
            evidence['unoccupied_pair_count'] += 1
            nearest = evidence['nearest_unoccupied_km']
            if nearest is None or length < nearest:
                evidence['nearest_unoccupied_km'] = length
            if spec.segment_km_min <= length <= spec.segment_km_max:
                evidence['eligible_tie_count'] += 1
                yield length, a.id, b.id

    if not remaining:return output
    pool=list(candidates())
    if len(pool)<remaining:raise TieConstructionError(evidence)
    graph=nx.Graph((e.bus1,e.bus2) for e in output.lines)
    # Shared root-path bit masks avoid retaining a full edge-set per candidate.
    bfs=list(nx.bfs_edges(graph,output.source_bus))
    downstream={node:0. for node in graph}
    for load in output.loads:downstream[load.bus]+=load.kw
    for a,b in reversed(bfs):downstream[a]+=downstream[b]
    masks={output.source_bus:0};bit_child=[]
    for i,(a,b) in enumerate(bfs):masks[b]=masks[a]|(1<<i);bit_child.append(b)
    covered=0
    for edge in output.tie_lines:covered|=masks[edge.bus1]^masks[edge.bus2]
    selected=[];selection=[]
    for _ in range(remaining):
        weighted_root={output.source_bus:0.}
        for i,(a,b) in enumerate(bfs):weighted_root[b]=weighted_root[a]+(0. if covered&(1<<i) else downstream[b])
        def score(candidate):
            length,a,b=candidate;common=masks[a]&masks[b]
            ancestor=bit_child[common.bit_length()-1] if common else output.source_bus
            gain=weighted_root[a]+weighted_root[b]-2*weighted_root[ancestor]
            newly=(masks[a]^masks[b])&~covered
            return (-gain,-newly.bit_count(),length,a,b)
        choice=min(pool,key=score);length,a,b=choice;gain=-score(choice)[0]
        new=(masks[a]^masks[b])&~covered
        selection.append(dict(bus1=a,bus2=b,length_km=length,newly_covered_active_edges=new.bit_count(),newly_covered_disconnected_load_sum_kw=gain))
        selected.append(choice);covered|=masks[a]^masks[b];pool.remove(choice)
    output.design_evidence['tie_selection']=dict(policy='marginal_load_weighted_cut_coverage_v1',
        eligible_pairs=evidence['eligible_tie_count'],covered_active_edges=covered.bit_count(),active_edges=len(output.lines),
        covered_edge_fraction=covered.bit_count()/len(output.lines),selected=selection,
        basis='Sum of disconnected downstream kW over newly bridged tree-edge outages, then count, then shorter length. No failure probabilities or electrical restoration guarantee.')
    ids = {line.id.casefold() for line in [*output.lines, *output.tie_lines]}
    from .scene_profiles import scene_profile
    construction=scene_profile(spec.scenario.engineering_profile)['construction']
    serial = 1
    for length, a, b in selected:
        while f'tie_{serial}' in ids:
            serial += 1
        name = f'tie_{serial}'
        ids.add(name)
        output.tie_lines.append(Line(id=name, bus1=a, bus2=b, length_km=length,
                                     conductor='research_small',construction=construction))
    return output
