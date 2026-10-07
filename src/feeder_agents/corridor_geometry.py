"""Versioned nonuniform synthetic corridors and distance-feasible local ties."""
from collections import defaultdict
from functools import lru_cache
import math
import random

import networkx as nx

from .composite_topology import corridor_blueprint


@lru_cache(maxsize=128)
def diverse_corridor_blueprint(n, terminals, tie_count, seed, lo, hi):
    """Keep the v2 active skeleton, vary spacing and normally-open connections.

    Old orthogonal ties remain feasible. Diagonal neighbours are also eligible
    when their actual length fits. Matching prevents endpoint reuse. There is
    no eight-edge preference or graph-hop upper bound.
    """
    edges,old_ties,lattice,trunk,attempts=corridor_blueprint(n,terminals,tie_count,seed)
    rng=random.Random(f'corridor-spacing-v3:{seed}:{n}')
    def axis(values):
        positions={0:0.};margin=.03*(hi-lo)
        for i in range(1,max(values)+1):positions[i]=positions[i-1]+rng.uniform(lo+margin,hi-margin)
        for i in range(-1,min(values)-1,-1):positions[i]=positions[i+1]-rng.uniform(lo+margin,hi-margin)
        return positions
    xs=axis([p[0] for p in lattice]);ys=axis([p[1] for p in lattice]);jitter=.005*(hi-lo)
    coords=tuple((xs[x]+(rng.uniform(-jitter,jitter) if node else 0.),
                  ys[y]+(rng.uniform(-jitter,jitter) if node else 0.))
                 for node,(x,y) in enumerate(lattice))
    if not tie_count:return edges,(),coords,trunk,attempts
    graph=nx.Graph(edges);parents={b:a for a,b in edges};ancestors={0:set()}
    for node in nx.bfs_tree(graph,0):
        if node:ancestors[node]=ancestors[parents[node]]|{parents[node]}
    occupied={point:node for node,point in enumerate(lattice)}
    candidates=nx.Graph();rng=random.Random(f'local-ties-v3:{seed}:{n}')
    for a,(x,y) in enumerate(lattice):
        if a==0:continue
        for dx,dy in ((1,0),(-1,0),(0,1),(0,-1),(1,1),(1,-1),(-1,1),(-1,-1)):
            b=occupied.get((x+dx,y+dy))
            if b is None or b<=a or a in ancestors[b] or b in ancestors[a]:continue
            length=math.dist(coords[a],coords[b])
            if not lo-1e-10<=length<=hi+1e-10:continue
            hops=nx.shortest_path_length(graph,a,b)
            if hops<3:continue
            candidates.add_edge(a,b,weight=rng.randrange(1,1000000),cycle_size=hops+1)
    if not all(candidates.has_edge(a,b) for a,b in old_ties):
        raise ValueError('Spatial transformation lost a feasible tie; counts and bounds were not relaxed')
    matching=sorted(tuple(sorted(pair)) for pair in nx.max_weight_matching(candidates,maxcardinality=True))
    if len(matching)<tie_count:raise ValueError('Insufficient disjoint spatially feasible local ties')
    groups=defaultdict(list)
    for a,b in matching:groups[candidates[a][b]['cycle_size']].append((a,b))
    order=list(sorted(groups));rng.shuffle(order)
    for pairs in groups.values():rng.shuffle(pairs)
    selected=[]
    while len(selected)<tie_count:
        for size in order:
            if groups[size] and len(selected)<tie_count:selected.append(groups[size].pop())
    return edges,tuple(selected),coords,trunk,attempts
