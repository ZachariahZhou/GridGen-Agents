"""Deterministic synthetic corridor trees with short, normally-open local ties.

The lattice is a construction device, not street/GIS evidence. Random corridor
continuations and nested branches replace the previous global-ring template.
Only immutable blueprints are cached; callers cannot change future generations.
"""
from functools import lru_cache
import random

import networkx as nx


@lru_cache(maxsize=128)
def corridor_blueprint(n, terminals, tie_count, seed):
    """Return edges, ties, unit coordinates, trunk and construction attempt count.

    n excludes the source. Exact requested counts are hard constraints. Branch
    degree is at most three, source degree one. Each tie joins nearby separate
    branches, closes a 4–12 edge fundamental cycle and uses distinct endpoints.
    """
    if not 1 <= terminals <= (n + 1) // 2:
        raise ValueError('branched_network terminal_count needs at least 2*terminal_count-1 non-source MV buses')
    if tie_count and (terminals < 2 or n < 5 or 2 * tie_count > n):
        raise ValueError('local_tie_count requires enough distinct endpoints on separate MV branches')
    directions = ((1, 0), (0, 1), (0, -1), (-1, 0))
    for attempt in range(32):
        rng = random.Random(f'corridor-v2:{seed}:{n}:{terminals}:{tie_count}:{attempt}')
        coords = [(0, 0), (1, 0)]
        occupied = set(coords)
        graph = nx.Graph([(0, 1)])
        forward = {1:(1, 0)}
        children = {0:1, 1:0}
        leaf_count, tip = 1, 1
        for node in range(2, n + 1):
            extensions, branches = [], []
            for parent in range(1, node):
                if children[parent] >= 2:
                    continue
                x, y = coords[parent]
                options = [d for d in directions if (x+d[0], y+d[1]) not in occupied]
                if options:
                    (extensions if children[parent] == 0 else branches).append((parent, options))
            deficit = terminals - leaf_count
            remaining = n - node + 1
            # Meet the branch budget progressively, leaving some long corridors
            # and some short laterals instead of allocating equal branch sizes.
            branch_now = deficit > 0 and (remaining <= deficit or not extensions or
                rng.random() < min(.75, 1.25 * deficit / remaining))
            candidates = branches if branch_now else extensions
            if not candidates:
                candidates = extensions if branch_now else (branches if deficit > 0 else [])
            if not candidates:
                break
            current = next((item for item in candidates if item[0] == tip), None)
            parent, options = current if current and rng.random() < .62 else rng.choice(candidates)
            weights = [6 if d == forward[parent] else 1 for d in options]
            direction = rng.choices(options, weights=weights, k=1)[0]
            x, y = coords[parent]
            point = (x+direction[0], y+direction[1])
            if children[parent] > 0:
                leaf_count += 1
            children[parent] += 1
            children[node] = 0
            forward[node] = direction
            coords.append(point); occupied.add(point)
            graph.add_edge(parent, node)
            tip = node
        if len(coords) != n + 1 or leaf_count != terminals:
            continue
        depths = nx.single_source_shortest_path_length(graph, 0)
        trunk = nx.shortest_path(graph, 0, max(depths, key=depths.get))
        branch_points = {node for node, degree in graph.degree() if degree == 3}
        if terminals >= 4 and n >= 16 and not branch_points.difference(trunk):
            continue  # Do not call a single-level comb a nested feeder.
        occupied_by = {point:node for node, point in enumerate(coords)}
        parents = dict((b,a) for a,b in nx.bfs_edges(graph,0))
        ancestors = {0:set()}
        for node in nx.bfs_tree(graph,0):
            if node:
                ancestors[node] = ancestors[parents[node]] | {parents[node]}
        candidates = []
        for a, (x,y) in enumerate(coords):
            if a == 0:
                continue
            for dx,dy in directions:
                b = occupied_by.get((x+dx,y+dy))
                if b is None or b <= a or graph.has_edge(a,b):
                    continue
                if a in ancestors[b] or b in ancestors[a]:
                    continue
                hops = nx.shortest_path_length(graph,a,b)
                if 3 <= hops <= 11:
                    candidates.append((a,b,hops))
        rng.shuffle(candidates)
        candidates.sort(key=lambda item: abs(item[2]-7))
        ties, used = [], set()
        for a,b,_ in candidates:
            if len(ties) == tie_count:
                break
            if a not in used and b not in used:
                ties.append((a,b)); used.update((a,b))
        if len(ties) == tie_count:
            return (tuple(nx.bfs_edges(graph,0)), tuple(ties), tuple(coords), tuple(trunk), attempt+1)
    raise ValueError('Could not construct branched_network with the requested terminal/local-tie counts '
                     'in 32 deterministic attempts; increase mv_buses or reduce local_tie_count. '
                     'No requested counts or length bounds were relaxed.')
