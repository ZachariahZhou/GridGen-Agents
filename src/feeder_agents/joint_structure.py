"""Joint branch/tap priors and constrained local MV construction edits.

These are transparent research priors, not learned utility population statistics
or an LLM feedback loop. Cached blueprints contain immutable values only.
"""
from functools import lru_cache
import math
import random

import networkx as nx

from .corridor_geometry import diverse_corridor_blueprint


def default_terminals(n, transformer_count, scene, ties=None):
    reserved=min(transformer_count-1,math.ceil(transformer_count/3))
    ceiling=min(transformer_count,(n+1)//2)
    terminals=min(ceiling,max(1,round(n*(.22 if scene=='urban' else .17))),transformer_count-reserved)
    # A requested tie requires separate branches; its hard contract precedes
    # the soft reserve. An impossible T=1 request is rejected by construction.
    if ties:terminals=min(ceiling,max(2,terminals))
    return terminals


def degree4_target(n,terminals):
    return min(8,(terminals-1)//2,round(.015*(n+1)))


def _structure_valid(graph,ties,terminals,n):
    if not nx.is_tree(graph) or graph.degree(0)!=1:return False
    paths=nx.single_source_shortest_path(graph,0)
    for a,b in ties:
        if a in paths[b] or b in paths[a] or nx.shortest_path_length(graph,a,b)<3:return False
    if terminals>=4 and n>=16:
        trunk=set(max(paths.values(),key=len))
        if not any(d>=3 and b not in trunk for b,d in graph.degree):return False
    return True


@lru_cache(maxsize=128)
def joint_corridor_blueprint(n,terminals,tie_count,seed,lo,hi):
    edges,ties,coords,trunk,attempts=diverse_corridor_blueprint(n,terminals,tie_count,seed,lo,hi)
    graph=nx.Graph(edges);trace=[]
    target=degree4_target(n,terminals)
    rng=random.Random(f'joint-local-structure-v4:{seed}:{n}:{terminals}')
    for _ in range(target):
        paths=nx.single_source_shortest_path(graph,0)
        oriented=list(nx.bfs_edges(graph,0))
        junctions=[b for b,d in graph.degree if b!=0 and d==3]
        candidates=[]
        for parent,child in oriented:
            if parent==0 or graph.degree(parent)!=3:continue
            for other in junctions:
                if other in (parent,child) or child in paths[other]:continue
                distance=math.dist(coords[child],coords[other])
                if not lo-1e-10<=distance<=hi+1e-10:continue
                # All options are physically local. Random ordering prevents
                # node numbering from becoming a structural selection rule.
                candidates.append((rng.random(),parent,child,other))
        changed=False
        for _,parent,child,other in sorted(candidates):
            proposal=graph.copy();proposal.remove_edge(parent,child);proposal.add_edge(other,child)
            if not _structure_valid(proposal,ties,terminals,n):continue
            graph=proposal;trace.append((parent,child,other));changed=True;break
        if not changed:break
    paths=nx.single_source_shortest_path(graph,0)
    trunk=tuple(max(paths.values(),key=len))
    return tuple(nx.bfs_edges(graph,0)),ties,coords,trunk,attempts,tuple(trace)


def spread_transformer_taps(graph,terminals,count,seed):
    selected=set(terminals);remaining=set(graph)-{0}-selected
    rng=random.Random(f'joint-taps-v4:{seed}')
    priorities={b:rng.random() for b in sorted(remaining)}
    while len(selected)<count:
        distance=nx.multi_source_dijkstra_path_length(graph,selected,weight=None)
        chosen=max(remaining,key=lambda b:(distance[b],graph.degree(b)==2,priorities[b]))
        selected.add(chosen);remaining.remove(chosen)
    return sorted(selected)


def joint_design_evidence(spec,cfg,edges,trace):
    graph=nx.Graph(edges);achieved=sum(d==4 for _,d in graph.degree)
    target=degree4_target(spec.mv_buses-1,cfg.terminal_count)
    return dict(terminal_selection='explicit' if spec.mv_topology.terminal_count is not None else 'joint_default',
        terminals=cfg.terminal_count,transformers=spec.transformer_count,
        preferred_inline_reserve=min(spec.transformer_count-1,math.ceil(spec.transformer_count/3)),
        inline_transformers=spec.transformer_count-cfg.terminal_count,
        inline_reserve_met=spec.transformer_count-cfg.terminal_count>=min(spec.transformer_count-1,math.ceil(spec.transformer_count/3)),
        reserve_override='explicit_terminal_count' if spec.mv_topology.terminal_count is not None else
            'explicit_tie_feasibility' if cfg.terminal_count>default_terminals(spec.mv_buses-1,spec.transformer_count,spec.scene) else None,
        degree4_target=target,degree4_achieved=achieved,degree4_target_met=achieved>=target,
        constructor_edits=len(trace),is_llm_feedback=False,
        edits=[dict(operation='reattach_mv_branch',old_parent=a,branch_root=b,new_parent=c) for a,b,c in trace],
        tap_selection='Terminal coverage followed by graph-distance spread; fixed transformer budget',
        basis='Joint synthetic prior; one-third preferred inline reserve, sparse degree-4 soft target, explicit terminal/tie counts take precedence',
        limitations=['Not fitted to a utility population; scene density ceiling retained.',
            'Degree-4 target is soft and constrained by existing geometry and open ties.',
            'Transformer cap, lattice directions and single-source scope remain.'])
