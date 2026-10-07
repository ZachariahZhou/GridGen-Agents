"""Scene-conditioned MV skeletons; physical loops are operated radially.

These are synthetic structural families, not a GIS layout or a fitted urban/rural
population. Explicit family choices always take precedence over scene defaults.
"""
import math
import random
from collections import defaultdict
from typing import Literal

import networkx as nx
from pydantic import Field, model_validator

from .schemas import Bus, Line, StrictModel, TopologySettings
from .topologies import topology_edges


class MVTopologySettings(StrictModel):
    family: Literal['auto', 'long_trunk', 'comb', 'multi_branch', 'balanced_tree',
                    'irregular_tree', 'open_ring', 'ring_laterals', 'multi_open_ring', 'branched_network'] = 'auto'
    branch_count: int | None = Field(default=None, ge=2, le=12)
    branching_factor: int | None = Field(default=None, ge=2, le=4)
    trunk_fraction: float | None = Field(default=None, ge=.15, le=.8)
    ring_count: int | None = Field(default=None, ge=2, le=4,
        description='Only multi_open_ring: number of physical loops, each with one normally-open tie.')
    terminal_count: int | None = Field(default=None, ge=1, le=32,
        description='Only branched_network: exact non-source MV terminal count, no greater than transformer_count.')
    local_tie_count: int | None = Field(default=None, ge=0, le=8,
        description='Only branched_network: exact normally-open local ties satisfying spatial length bounds; V3 permits varied fundamental cycle sizes.')

    @model_validator(mode='after')
    def parameter_scope(self):
        allowed = {'branch_count': {'comb', 'multi_branch', 'ring_laterals'},
                   'branching_factor': {'balanced_tree', 'irregular_tree'},
                   'trunk_fraction': {'comb', 'ring_laterals'},
                   'ring_count': {'multi_open_ring'},
                   'terminal_count': {'branched_network'}, 'local_tie_count': {'branched_network'}}
        for key, families in allowed.items():
            if getattr(self, key) is not None and self.family not in families:
                raise ValueError(f'{key} only applies to {sorted(families)}; choose an explicit MV family')
        return self


def resolve_mv_topology(spec):
    cfg = spec.mv_topology.model_copy(deep=True)
    n = spec.mv_buses - 1
    if cfg.family == 'auto':
        t=spec.transformer_count
        if spec.mv_topology_policy in ('branched_v2','branched_v3','branched_v4'):
            cfg.family = 'branched_network'
        else:
            cfg.family = ('ring_laterals' if t >= 4 else 'open_ring' if t >= 2 else 'long_trunk') if spec.scene == 'urban' else ('comb' if t >= 4 else 'long_trunk')
    if cfg.family == 'branched_network':
        if cfg.terminal_count is None:
            density = .22 if spec.scene == 'urban' else .17
            if spec.mv_topology_policy=='branched_v4':
                from .joint_structure import default_terminals
                cfg.terminal_count=default_terminals(n,spec.transformer_count,spec.scene,cfg.local_tie_count)
            else:
                cfg.terminal_count = min(spec.transformer_count, (n+1)//2, max(1, round(n*density)))
        if spec.mv_topology.terminal_count is None and spec.mv_topology_policy=='branched_v4':
            from .structure_targets import required_terminals
            cfg.terminal_count=max(cfg.terminal_count,required_terminals(spec))
        if cfg.terminal_count > spec.transformer_count:
            raise ValueError('terminal_count cannot exceed transformer_count: every MV terminal needs a transformer')
        if cfg.local_tie_count is None:
            cfg.local_tie_count = min(3, max(1, n//24)) if spec.scene == 'urban' and n >= 16 and cfg.terminal_count >= 3 else 0
        if spec.mv_topology_policy=='branched_v4':
            from .joint_structure import joint_corridor_blueprint
            candidates=[cfg.terminal_count]
            if spec.mv_topology.terminal_count is None and spec.mv_topology.local_tie_count:
                # Only a soft default may yield to an explicit switch budget.
                candidates=range(cfg.terminal_count,min(spec.transformer_count,(n+1)//2)+1)
            last_error=None
            for terminals in candidates:
                try:
                    joint_corridor_blueprint(n,terminals,cfg.local_tie_count,spec.seed,
                        spec.mv_segment_km_min,spec.mv_segment_km_max)
                except ValueError as exc:
                    last_error=exc
                else:
                    cfg.terminal_count=terminals
                    break
            else:
                raise last_error
        else:
            from .composite_topology import corridor_blueprint
            corridor_blueprint(n, cfg.terminal_count, cfg.local_tie_count, spec.seed)
    elif cfg.family == 'multi_open_ring':
        cfg.ring_count = cfg.ring_count or 2
        if n < 2 * cfg.ring_count:
            raise ValueError('multi_open_ring requires at least two MV transformer taps per ring; increase transformer_count or reduce ring_count')
    elif cfg.family == 'ring_laterals':
        if n < 3:
            raise ValueError('ring_laterals requires at least three MV transformer taps')
        cfg.trunk_fraction = cfg.trunk_fraction or .6
        ring_size=max(2,min(n-1,math.ceil(n*cfg.trunk_fraction)))
        remaining=n-ring_size
        if cfg.branch_count is None and spec.mv_topology.family=='auto' and spec.mv_buses>spec.transformer_count+1:
            cfg.branch_count=min(4,remaining,max(2,math.isqrt(remaining))) if remaining>=2 else None
        if cfg.branch_count is not None and cfg.branch_count>min(ring_size,remaining):
            raise ValueError('ring_laterals branch_count needs enough distinct ring anchors and lateral nodes')
    elif cfg.family == 'open_ring' and n < 2:
        raise ValueError('open_ring requires at least two MV transformer taps')
    else:
        base = TopologySettings(**cfg.model_dump(exclude={'ring_count','terminal_count','local_tie_count'}))
        if cfg.family == 'comb' and n < 2:
            raise ValueError('comb requires at least two MV transformer taps')
        topology_edges(n, base, spec.seed)
    return cfg


def resolve_lv_topology(spec):
    return ('radial_chain' if spec.scene == 'rural' else 'mixed_radial') if spec.lv_topology == 'auto' else spec.lv_topology


def _blueprint(n, cfg, seed):
    """Integer node 0 is source. Return oriented tree, open edges and rings."""
    if cfg.family == 'branched_network':
        from .composite_topology import corridor_blueprint
        edges, ties, _, trunk, _ = corridor_blueprint(n, cfg.terminal_count, cfg.local_tie_count, seed)
        return list(edges), list(ties), [], list(trunk)
    if cfg.family not in {'open_ring', 'ring_laterals', 'multi_open_ring'}:
        edges, trunk = topology_edges(n, cfg, seed)
        return edges, [], [], trunk
    if cfg.family == 'multi_open_ring':
        sizes = [n // cfg.ring_count + (j < n % cfg.ring_count) for j in range(cfg.ring_count)]
    else:
        sizes = [max(2, min(n - 1, math.ceil(n * cfg.trunk_fraction))) if cfg.family == 'ring_laterals' else n]
    physical = nx.Graph(); physical.add_nodes_from(range(n + 1))
    ties, rings = [], []
    next_id = 1
    for size in sizes:
        ring = [0, *range(next_id, next_id + size)]; next_id += size
        rings.append(ring)
        # Interior opening creates two energized arms from the equivalent source.
        gap = size // 2
        tie = (ring[gap], ring[gap + 1]); ties.append(tie)
        physical.add_edges_from(zip(ring, ring[1:] + ring[:1]))
        physical.remove_edge(*tie)
    if cfg.family == 'ring_laterals':
        anchors = rings[0][1:]
        if cfg.branch_count is None:
            # Preserve the original explicit family and frozen source models.
            for j, node in enumerate(range(next_id, n + 1)):
                physical.add_edge(anchors[j % len(anchors)] if j < len(anchors) else node - len(anchors), node)
        else:
            from .hierarchy_structure import allocate_counts
            rng=random.Random(f'ring-laterals:{seed}')
            count=cfg.branch_count
            sizes=allocate_counts(n-next_id+1,count,1,rng,True)
            # Choose one attachment from each sector, then give the lateral
            # its own number of line sections; avoid a row of identical teeth.
            for j,size in enumerate(sizes):
                sector=anchors[j*len(anchors)//count:(j+1)*len(anchors)//count]
                parent=rng.choice(sector)
                for _ in range(size):
                    physical.add_edge(parent,next_id);parent=next_id;next_id+=1
    return list(nx.bfs_edges(physical, 0)), ties, rings, rings[0] if len(rings) == 1 else []


def resolved_blueprint(spec,seed):
    """Shared operating graph for construction, transformer taps and contracts."""
    cfg=resolve_mv_topology(spec)
    if cfg.family=='branched_network' and spec.mv_topology_policy=='branched_v4':
        from .joint_structure import joint_corridor_blueprint
        edges,ties,_,trunk,_,_=joint_corridor_blueprint(spec.mv_buses-1,cfg.terminal_count,
            cfg.local_tie_count,seed,spec.mv_segment_km_min,spec.mv_segment_km_max)
        return list(edges),list(ties),[],list(trunk)
    return _blueprint(spec.mv_buses-1,cfg,seed)


def design_mv_topology(feeder, spec):
    """Replace only the MV skeleton; keep aggregate load sites and hard counts."""
    cfg = resolve_mv_topology(spec); n = spec.mv_buses - 1
    edges, ties, rings, trunk = resolved_blueprint(spec,feeder.seed)
    rng = random.Random(f'hierarchy-topology-v1:{feeder.seed}')
    lo, hi = spec.mv_segment_km_min, spec.mv_segment_km_max
    positions = {0: (0., 0.)}
    construction = {}
    if cfg.family == 'branched_network':
        from .composite_topology import corridor_blueprint
        if spec.mv_topology_policy=='branched_v4':
            from .joint_structure import joint_corridor_blueprint, joint_design_evidence
            edges,ties,coords,trunk,attempts,edits=joint_corridor_blueprint(n,cfg.terminal_count,cfg.local_tie_count,feeder.seed,lo,hi)
            positions=dict(enumerate(coords))
            construction['joint_design']=joint_design_evidence(spec,cfg,edges,edits)
        elif spec.mv_topology_policy=='branched_v3':
            from .corridor_geometry import diverse_corridor_blueprint
            edges,ties,coords,trunk,attempts=diverse_corridor_blueprint(n,cfg.terminal_count,cfg.local_tie_count,feeder.seed,lo,hi)
            positions=dict(enumerate(coords))
        else:
            _, _, coords, _, attempts = corridor_blueprint(n,cfg.terminal_count,cfg.local_tie_count,feeder.seed)
            step = (lo + hi) / 2
            jitter = min(step-lo,hi-step) / 8
            for node,(x,y) in enumerate(coords):
                positions[node] = (x*step + (rng.uniform(-jitter,jitter) if node else 0.),
                                   y*step + (rng.uniform(-jitter,jitter) if node else 0.))
        graph = nx.Graph(edges)
        construction.update(algorithm={'branched_v4':'joint_corridor_v4','branched_v3':'corridor_spatial_v3'}.get(spec.mv_topology_policy,'corridor_tree_v2'),attempts=attempts,
            terminal_count=cfg.terminal_count, local_tie_cycle_sizes=[nx.shortest_path_length(graph,a,b)+1 for a,b in ties],
            spatial_basis='Synthetic corridor lattice with bounded jitter; not actual street GIS')
    elif rings:
        # Keep a margin for small nonuniform spatial variation, including ties.
        step = rng.uniform(lo + .2 * (hi - lo), hi - .2 * (hi - lo))
        for j, ring in enumerate(rings):
            radius = step / (2 * math.sin(math.pi / len(ring)))
            rotation = 2 * math.pi * j / len(rings)
            for i, node in enumerate(ring[1:], 1):
                theta = 2 * math.pi * i / len(ring)
                x, y = radius * (1 - math.cos(theta)), radius * math.sin(theta)
                positions[node] = (x * math.cos(rotation) - y * math.sin(rotation),
                                   x * math.sin(rotation) + y * math.cos(rotation))
        # Ring laterals grow outward from their anchor, not across the ring.
        center = (step / (2 * math.sin(math.pi / len(rings[0]))), 0.)
        for a, b in edges:
            if b in positions:
                continue
            angle = math.atan2(positions[a][1] - center[1], positions[a][0] - center[0])
            positions[b] = (positions[a][0] + step * math.cos(angle), positions[a][1] + step * math.sin(angle))
        jitter = min(step - lo, hi - step) / 5
        for node in range(1, n + 1):
            x, y = positions[node]
            positions[node] = (x + rng.uniform(-jitter, jitter), y + rng.uniform(-jitter, jitter))
    else:
        children = defaultdict(list)
        for a, b in edges:
            children[a].append(b)
        sectors = {0: (-math.pi / 2 + .1, math.pi / 2 - .1)}
        arm, angles = 0, {}
        for a, b in edges:
            if cfg.family == 'long_trunk' or (cfg.family == 'comb' and b in trunk):
                angle = 0.
            elif cfg.family == 'comb':
                if a in trunk:
                    arm += 1
                angle = math.pi / 2 * (1 if arm % 2 else -1)
            elif cfg.family == 'multi_branch':
                angle = 2 * math.pi * children[0].index(b) / len(children[0]) if a == 0 else angles[a]
            else:
                low, high = sectors[a]; count = len(children[a]); j = children[a].index(b)
                left, right = low + (high - low) * j / count, low + (high - low) * (j + 1) / count
                sectors[b] = (left, right); angle = (left + right) / 2
            angles[b] = angle
            length = rng.uniform(lo, hi)
            positions[b] = (positions[a][0] + length * math.cos(angle), positions[a][1] + length * math.sin(angle))
    if len(set(positions.values())) != n + 1:
        raise ValueError('MV topology embedding produced coincident buses')
    name = lambda i: feeder.source_bus if i == 0 else f'b{i}'
    feeder.buses = [Bus(id=name(i), x_km=positions[i][0], y_km=positions[i][1]) for i in range(n + 1)]
    def line(a, b, identifier):
        length = math.dist(positions[a], positions[b])
        if not lo - 1e-10 <= length <= hi + 1e-10:
            raise ValueError('MV embedding violates segment bounds; bounds were not relaxed')
        return Line(id=identifier, bus1=name(a), bus2=name(b), length_km=length, conductor='research_small')
    feeder.lines = [line(a, b, f'l{i}') for i, (a, b) in enumerate(edges, 1)]
    feeder.tie_lines = [line(a, b, f'tie_{i}') for i, (a, b) in enumerate(ties, 1)]
    feeder.design_evidence = {'topology_design': dict(family=cfg.family, parameters=cfg.model_dump(),
        parent_seed=feeder.seed, operating_topology='single_source_radial',
        physical_cycle_rank=len(ties), selection='scene_default' if spec.mv_topology.family == 'auto' else 'explicit',
        policy_version=spec.mv_topology_policy, construction=construction,
        scope='Synthetic structural prior; not GIS, population calibration or N-1 certification'),
        'trunk_buses': [name(i) for i in trunk]}
    return feeder


def mv_view(feeder):
    """A pure MV model for topology and OpenDSS switch-state auditing."""
    from .schemas import Feeder
    ids = {b.id for b in feeder.buses if b.voltage_kv > 1}
    return Feeder(seed=feeder.seed, source_bus=feeder.source_bus, total_kw=feeder.total_kw,
        voltage_kv=feeder.voltage_kv, frequency_hz=feeder.frequency_hz, phase_mode=feeder.phase_mode,
        buses=[Bus(**{k: v for k, v in b.model_dump().items() if k in Bus.model_fields}) for b in feeder.buses if b.id in ids],
        lines=[e for e in feeder.lines if e.bus1 in ids and e.bus2 in ids],
        tie_lines=feeder.tie_lines, loads=[], assumptions=[])


def topology_contract_matches(feeder, spec, result=None):
    from types import SimpleNamespace
    from .ties import tie_contract_matches
    cfg = resolve_mv_topology(spec)
    edges, ties, _, _ = resolved_blueprint(spec,feeder.seed)
    if cfg.family=='branched_network' and spec.mv_topology_policy=='branched_v3':
        from .corridor_geometry import diverse_corridor_blueprint
        edges,ties,_,_,_=diverse_corridor_blueprint(spec.mv_buses-1,cfg.terminal_count,cfg.local_tie_count,
            feeder.seed,spec.mv_segment_km_min,spec.mv_segment_km_max)
    name = lambda i: feeder.source_bus if i == 0 else f'b{i}'
    expected = lambda pairs: {frozenset((name(a), name(b))) for a, b in pairs}
    mv = mv_view(feeder)
    actual = lambda lines: {frozenset((e.bus1, e.bus2)) for e in lines}
    active_ok=actual(mv.lines)==expected(edges)
    if feeder.design_evidence.get('mv_structure_actions'):
        from .mv_structure_feedback import lineage_matches
        active_ok=lineage_matches(feeder,spec,edges)
    if not active_ok or actual(mv.tie_lines) != expected(ties):
        return False
    contract = SimpleNamespace(scenario=SimpleNamespace(tie_count=len(ties)),
                               segment_km_min=spec.mv_segment_km_min, segment_km_max=spec.mv_segment_km_max)
    return tie_contract_matches(mv, contract, result)


def lv_topology_matches(feeder, spec):
    from .hierarchy_structure import lv_chain_for_transformer
    if 'structure_resolution' not in feeder.design_evidence and spec.lv_topology=='auto':
        resolved=feeder.design_evidence.get('resolved_lv_topology')
        if resolved in ('branch_star','radial_chain'):
            spec=spec.model_copy(update={'lv_topology':resolved})
    expected = {(f'lv_{t}_branch_{b-1}' if lv_chain_for_transformer(spec,feeder.seed,t-1) and b > 1 else f'lv_{t}', f'lv_{t}_branch_{b}')
                for t in range(1, spec.transformer_count + 1) for b in range(1, spec.lv_branches + 1)}
    roles = {b.id: b.role for b in feeder.buses}
    actual = {(e.bus1, e.bus2) for e in feeder.lines if roles.get(e.bus2) == 'lv_branch'}
    return actual == expected


def topology_observations(feeder):
    """Measurements from edges; a family label is exposed only after graph verification."""
    mv = mv_view(feeder)
    active = nx.Graph(); active.add_nodes_from(b.id for b in mv.buses)
    active.add_edges_from((e.bus1, e.bus2) for e in mv.lines)
    physical = active.copy(); physical.add_edges_from((e.bus1, e.bus2) for e in mv.tie_lines)
    distances = nx.single_source_shortest_path_length(active, feeder.source_bus) if feeder.source_bus in active else {}
    out = {'mv_buses':len(mv.buses),'mv_tie_count': len(mv.tie_lines), 'mv_source_count':sum(b.role=='source' for b in feeder.buses),
           'mv_physical_cycle_rank': physical.number_of_edges() - physical.number_of_nodes() + nx.number_connected_components(physical),
           'mv_terminal_count': sum(node != feeder.source_bus and degree == 1 for node, degree in active.degree()),
           'mv_active_max_degree': max(dict(active.degree()).values(), default=0),
           'mv_physical_max_degree': max(dict(physical.degree()).values(), default=0),
           'mv_max_depth': max(distances.values(), default=0), 'mv_source_branches': active.degree(feeder.source_bus),
           'mv_operating_topology': 'radial' if nx.is_tree(active) else 'nonradial'}
    from .structure_targets import structure_measurements
    out.update({'structure_targets.'+k:v for k,v in structure_measurements(feeder).items() if k!='witnesses'})
    from .hierarchy import historical_hierarchy_spec
    spec = historical_hierarchy_spec(feeder.design_evidence['spec'])
    if topology_contract_matches(feeder, spec):
        cfg = resolve_mv_topology(spec)
        out.update({'mv_topology.' + k: v for k, v in cfg.model_dump().items() if v is not None})
    if lv_topology_matches(feeder, spec):
        out['lv_topology'] = resolve_lv_topology(spec)
    from collections import Counter
    from .hierarchy_structure import customer_groups
    from .customer_connections import branch_ancestors,connection_matches
    if connection_matches(feeder,spec.customer_connection):out['customer_connection']=spec.customer_connection
    ancestors=branch_ancestors(feeder)
    counts=Counter(ancestors.get(load.bus) for load in feeder.loads)
    actual=[[counts[f'lv_{t+1}_branch_{b+1}'] for b in range(spec.lv_branches)]
            for t in range(spec.transformer_count)]
    out['users_per_transformer']=[sum(group) for group in actual]
    # A recorded policy cannot certify a delivery with a different allocation.
    # Authorized service reconnections are allowed, but may leave this exact
    # policy contract unverified if explicitly requested by the user.
    if sum(out['users_per_transformer'])==len(feeder.loads) and actual==customer_groups(spec,feeder.seed):
        out['customer_allocation']=spec.customer_allocation
    return out
