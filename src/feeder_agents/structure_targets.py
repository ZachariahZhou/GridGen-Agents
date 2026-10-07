"""Explicit, measurable MV graph goals; not a universal realism score."""
import math

import networkx as nx
from pydantic import Field,model_validator

from .schemas import StrictModel


class StructureTargets(StrictModel):
    max_mv_depth: int | None=Field(default=None,ge=1,le=1000,
        description='Explicit upper bound on energized MV edges from source to any MV bus.')
    max_tap_distance_hops: int | None=Field(default=None,ge=0,le=1000,
        description='Explicit upper bound on MV graph distance from each non-source MV bus to its nearest transformer primary tap.')

    @model_validator(mode='after')
    def nonempty(self):
        if self.max_mv_depth is None and self.max_tap_distance_hops is None:
            raise ValueError('Specify at least one structural target, or omit structure_targets')
        return self


def validate_target_scope(spec):
    target=spec.structure_targets
    if target is None:return
    if spec.mv_topology_policy!='branched_v4' or spec.mv_topology.family not in ('auto','branched_network'):
        raise ValueError('Structural targets currently require branched_v4 / branched_network')
    n=spec.mv_buses-1
    if target.max_mv_depth is not None:
        depth=target.max_mv_depth;leaves=spec.mv_topology.terminal_count or spec.transformer_count
        capacity=1;layer=1
        for _ in range(depth):
            capacity+=layer;layer*=3
            if capacity>=spec.mv_buses:break
        if n>leaves*depth or capacity<spec.mv_buses:
            raise ValueError('Depth target conflicts with node/terminal budget and single-source degree<=4 representation')
    if target.max_tap_distance_hops==0 and spec.transformer_count<n:
        raise ValueError('Zero tap distance requires every non-source MV bus to have a transformer')


def required_terminals(spec):
    target=getattr(spec,'structure_targets',None)
    return math.ceil((spec.mv_buses-1)/target.max_mv_depth) if target and target.max_mv_depth else 1


def mv_graph(feeder):
    graph=nx.Graph();graph.add_nodes_from(b.id for b in feeder.buses if b.voltage_kv>1)
    graph.add_edges_from((e.bus1,e.bus2) for e in feeder.lines if e.bus1 in graph and e.bus2 in graph)
    return graph


def structure_node_measurements(feeder):
    graph=mv_graph(feeder)
    if not graph or feeder.source_bus not in graph or not nx.is_tree(graph):
        return dict(max_mv_depth={},max_tap_distance_hops={})
    paths=nx.single_source_shortest_path_length(graph,feeder.source_bus)
    taps={t.bus1 for t in feeder.transformers}
    distance=nx.multi_source_dijkstra_path_length(graph,taps,weight=None) if taps and taps<=set(graph) else {}
    return dict(max_mv_depth=paths,max_tap_distance_hops={b:d for b,d in distance.items() if b!=feeder.source_bus})


def structure_measurements(feeder):
    values=structure_node_measurements(feeder)
    paths=values['max_mv_depth'];nonsource=values['max_tap_distance_hops']
    depth=max(paths.values(),default=None);coverage=max(nonsource.values(),default=None)
    return dict(max_mv_depth=depth,max_tap_distance_hops=coverage,
        witnesses=dict(max_mv_depth=sorted(b for b,v in paths.items() if v==depth),
            max_tap_distance_hops=sorted(b for b,v in nonsource.items() if v==coverage)))


def structural_constraints(feeder,spec):
    if spec.structure_targets is None:return []
    observed=structure_measurements(feeder);nodes=structure_node_measurements(feeder);rows=[]
    for metric,threshold in spec.structure_targets.model_dump(exclude_none=True).items():
        value=observed[metric]
        rows.append(dict(key='structure:'+metric,condition='explicit_structure_target',metric=metric,
            scope='maximum',operator='le',threshold=threshold,actual=value,witness=','.join(observed['witnesses'].get(metric,[])[:8]),phase=None,
            deficit=max(0,value-threshold)/max(threshold,1) if value is not None else 1e6))
        # Keep stable, per-node constraints so a repair of one tied worst node
        # can improve the score before the global maximum falls. Do not trade
        # one violating node for another. Normalize for different graph sizes.
        count=max(len(nodes[metric]),1)
        for bus,hops in sorted(nodes[metric].items()):
            rows.append(dict(key='structure:'+metric+':'+bus,condition='explicit_structure_target',metric=metric,
                scope='node',operator='le',threshold=threshold,actual=hops,witness=bus,phase=None,
                deficit=max(0,hops-threshold)/max(threshold,1)/count))
    return rows
