"""Observe LV connection semantics from actual edges, phases and station membership."""
from collections import Counter, defaultdict
import networkx as nx


def branch_ancestors(feeder):
    buses={b.id:b for b in feeder.buses};parents={e.bus2:e.bus1 for e in feeder.lines};out={}
    for load in feeder.loads:
        node=load.bus;seen=set()
        while node in parents and node not in seen:
            seen.add(node);node=parents[node]
            if buses.get(node) is None:break
            if buses[node].role=='lv_branch':out[load.bus]=node;break
    return out


def connection_matches(feeder,mode):
    buses={b.id:b for b in feeder.buses};incoming={};outdegree=Counter()
    for e in feeder.lines:
        if e.bus1 not in buses or e.bus2 not in buses:return False
        if e.bus2 in incoming:return False
        incoming[e.bus2]=e;outdegree[e.bus1]+=1
    customers={b.id for b in feeder.buses if b.role=='customer'}
    if customers!={l.bus for l in feeder.loads}:return False
    ancestors=branch_ancestors(feeder);groups=defaultdict(list)
    if mode not in ('service_star','distributed_taps','mixed_taps'):return False
    for node in customers:
        edge=incoming.get(node)
        if edge is None or node not in ancestors:return False
        bus=buses[node];parent=buses[edge.bus1];root=buses[ancestors[node]]
        groups[root.id].append(bus)
        if not bus.transformer_id or bus.transformer_id!=parent.transformer_id or bus.transformer_id!=root.transformer_id:return False
        if bus.region_id!=parent.region_id or bus.voltage_kv!=parent.voltage_kv:return False
        if mode=='service_star':
            if parent.role!='lv_branch' or len(bus.phases)!=1 or edge.phases!=bus.phases or outdegree[node]:return False
        elif mode=='distributed_taps':
            if parent.role not in ('customer','lv_branch') or bus.phases!=[1,2,3] or edge.phases!=[1,2,3] or outdegree[node]>1:return False
        elif mode=='mixed_taps':
            if parent.role not in ('customer','lv_branch') or parent.phases!=[1,2,3] or edge.phases!=bus.phases:return False
            if bus.phases==[1,2,3]:
                children=[buses[e.bus2] for e in feeder.lines if e.bus1==node]
                if sum(c.phases==[1,2,3] for c in children)>1 or sum(len(c.phases)==1 for c in children)>2:return False
            elif len(bus.phases)!=1 or outdegree[node]:return False
    if mode in ('distributed_taps','mixed_taps'):
        # Exactly one public lateral enters a customer chain from each occupied junction.
        starts=Counter(e.bus1 for e in feeder.lines if e.bus2 in customers and buses[e.bus1].role=='lv_branch' and buses[e.bus2].phases==[1,2,3])
        if any(v!=1 for v in starts.values()):return False
    if mode=='mixed_taps':
        for members in groups.values():
            if len(members)>1 and not (any(b.phases==[1,2,3] for b in members) and any(len(b.phases)==1 for b in members)):return False
    graph=nx.Graph((e.bus1,e.bus2) for e in feeder.lines)
    graph.add_nodes_from(buses);graph.add_edges_from((t.bus1,t.bus2) for t in feeder.transformers)
    return nx.is_tree(graph)


def allocation_protected(spec, override=None):
    return 'customer_allocation' in spec.model_fields_set if override is None else bool(override)


def allocation_preserved(base, trial):
    # Exact customer counts per original branch, including empty branches.
    return Counter(branch_ancestors(base).values())==Counter(branch_ancestors(trial).values())
