"""Spatial mesh construction with an explicit edge budget and no forced global ring."""
import math
import networkx as nx
import numpy as np


def resolved_family(spec):
    return ('ring_chords' if spec.topology=='ring' else 'regional') if spec.mesh_family=='auto' else spec.mesh_family


def radial_allocations(spec):
    sizes=[v.buses for v in spec.voltage_layers] if spec.voltage_layers else [spec.n_buses]
    counts=[0]*len(sizes);remaining=spec.radial_bus_count or 0
    if remaining>sum(n-3 for n in sizes):raise ValueError('Too many radial peripheral buses for the voltage-layer cores')
    while remaining:
        k=min((i for i,n in enumerate(sizes) if counts[i]<n-3),key=lambda i:(counts[i]/sizes[i],i))
        counts[k]+=1;remaining-=1
    return counts


def graph_contract(graph,spec,*,legacy_global=False):
    expected=spec.n_buses+spec.extra_edges+2*(len(spec.voltage_layers or [0])-1)
    if set(graph)!=set(range(1,spec.n_buses+1)) or not nx.is_connected(graph) or graph.number_of_edges()!=expected:return False
    if nx.number_of_selfloops(graph):return False
    if spec.connectivity=='bridgeless' and list(nx.bridges(graph)):return False
    if spec.radial_bus_count is not None and sum(d==1 for _,d in graph.degree())!=spec.radial_bus_count:return False
    if legacy_global:return True
    sizes=[v.buses for v in spec.voltage_layers] if spec.voltage_layers else [spec.n_buses]
    offset=1
    for n in sizes:
        layer=graph.subgraph(range(offset,offset+n));offset+=n
        if not nx.is_connected(layer) or layer.number_of_edges()<n:return False
        if spec.topology=='ring' and any(d!=2 for _,d in layer.degree()):return False
        if spec.connectivity=='bridgeless' and list(nx.bridges(layer)):return False
    return True


def _legacy(spec,rng):
    layers=[(v.kv,v.buses) for v in spec.voltage_layers] if spec.voltage_layers else [(spec.voltage_kv,spec.n_buses)]
    positions={};groups=[];nominal=[];offset=0;edges=[]
    for k,(kv,count) in enumerate(layers):
        group=list(range(offset+1,offset+count+1));groups.append(group);nominal.extend([kv]*count)
        radius=spec.radius_km/max(1,math.sqrt(len(layers)))
        for j,b in enumerate(group):
            angle=j*2*math.pi/count+rng.uniform(-.08,.08)/count;r=radius*rng.uniform(.9,1.1)
            positions[b]=(float(k*radius*2.6+r*math.cos(angle)),float(r*math.sin(angle)))
        edges.extend((group[j],group[(j+1)%count]) for j in range(count));offset+=count
    present={frozenset(e) for e in edges}
    pairs=sorted((math.dist(positions[a],positions[b]),a,b) for group in groups for a in group for b in group if a<b and frozenset((a,b)) not in present)
    if spec.mesh_style=='long_distance':pairs=list(reversed(pairs))
    elif spec.mesh_style=='mixed':pairs=[p for pair in zip(pairs[:(len(pairs)+1)//2],reversed(pairs[(len(pairs)+1)//2:])) for p in pair]+(pairs[len(pairs)//2:len(pairs)//2+1] if len(pairs)%2 else [])
    edges.extend((a,b) for _,a,b in pairs[:spec.extra_edges])
    return positions,groups,nominal,edges


def _ear_mesh(nodes,positions,budget):
    """Sparse-budget fallback: ears preserve two-edge connectivity at every step."""
    available=list(nodes);first=available.pop(0)
    second=min(available,key=lambda b:math.dist(positions[first],positions[b]));available.remove(second)
    third=min(available,key=lambda b:math.dist(positions[first],positions[b])+math.dist(positions[second],positions[b]));available.remove(third)
    g=nx.Graph([(first,second),(second,third),(third,first)])
    extra=budget-len(nodes)
    while available:
        v=min(available,key=lambda b:min(math.dist(positions[b],positions[u]) for u in g));available.remove(v)
        if extra and g.number_of_edges()<len(g)*(len(g)-1)//2:
            pairs=[(math.dist(positions[a],positions[v])+math.dist(positions[b],positions[v]),a,b) for a in g for b in g if a<b and not g.has_edge(a,b)]
            _,a,b=min(pairs);g.add_edges_from([(a,v),(v,b)]);extra-=1
        else:
            _,a,b=min((math.dist(positions[a],positions[v])+math.dist(positions[v],positions[b])-math.dist(positions[a],positions[b]),a,b) for a,b in g.edges())
            g.remove_edge(a,b);g.add_edges_from([(a,v),(v,b)])
    return g


def _spatial_mesh(nodes,positions,budget,style):
    complete=nx.Graph();complete.add_nodes_from(nodes)
    complete.add_weighted_edges_from((a,b,math.dist(positions[a],positions[b])) for i,a in enumerate(nodes) for b in nodes[i+1:])
    tree=nx.minimum_spanning_tree(complete);graph=tree.copy()
    paths=dict(nx.all_pairs_shortest_path(tree));tree_edges={frozenset(e) for e in tree.edges()}
    candidates=[]
    for a,b,d in complete.edges(data='weight'):
        if tree.has_edge(a,b):continue
        path=paths[a][b];covered={frozenset(e) for e in zip(path,path[1:])}
        candidates.append((a,b,d,covered))
    uncovered=set(tree_edges);method='spatial_tree_bridge_augmentation'
    while uncovered and graph.number_of_edges()<budget:
        eligible=[e for e in candidates if not graph.has_edge(e[0],e[1]) and e[3]&uncovered]
        a,b,d,covered=min(eligible,key=lambda e:(-len(e[3]&uncovered)**2/max(e[2],1e-9),e[2],e[0],e[1]))
        graph.add_edge(a,b);uncovered-=covered
    if uncovered:
        graph=_ear_mesh(nodes,positions,budget);method='spatial_ear_budget_fallback'
    # Additional corridors obey the remaining budget; length preference remains explicit.
    while graph.number_of_edges()<budget:
        degrees=dict(graph.degree())
        options=[(a,b,d) for a,b,d in complete.edges(data='weight') if not graph.has_edge(a,b)]
        long=style=='long_distance' or (style=='mixed' and graph.number_of_edges()%2==0)
        _,a,b=min(((1/max(d,1e-9) if long else d)*(1+.15*(degrees[a]+degrees[b])),a,b) for a,b,d in options)
        graph.add_edge(a,b)
    assert len(graph)==len(nodes) and graph.number_of_edges()==budget and not list(nx.bridges(graph))
    return list(graph.edges()),method


def build_topology(spec,rng,seed):
    # Consume the same RNG draws as the historical layout so load/P allocations
    # stay paired when only mesh_family changes. Topology uses a separate stream.
    positions,groups,nominal,edges=_legacy(spec,rng);family=resolved_family(spec)
    methods=[];regions={};radial=[]
    peripheral_counts=radial_allocations(spec)
    if family!='ring_chords':
        spatial=np.random.default_rng(np.random.SeedSequence([seed,77]))
        positions={};edges=[];remaining=spec.extra_edges
        budgets=[0]*len(groups)
        caps=[(len(g)-r)*(len(g)-r-1)//2-(len(g)-r) for g,r in zip(groups,peripheral_counts)]
        while remaining:
            k=min((k for k in range(len(groups)) if budgets[k]<caps[k]),key=lambda k:(budgets[k]/len(groups[k]),k))
            budgets[k]+=1;remaining-=1
        radius=spec.radius_km/math.sqrt(len(groups))
        for k,group in enumerate(groups):
            count=min(6,max(2,round(math.sqrt(len(group))/2)))
            centres=spatial.uniform(-.65,.65,(count,2));membership=spatial.permutation(np.arange(len(group))%count)
            for node,region in zip(group,membership):
                xy=centres[region]+spatial.normal(0,.20,2)
                if family=='corridor':xy[1]*=.25
                positions[node]=(float(radius*xy[0]+k*radius*2.6),float(radius*xy[1]));regions[node]=int(region)
            r=peripheral_counts[k];core=group[:-r] if r else group;periphery=group[-r:] if r else []
            layer,method=_spatial_mesh(core,positions,len(core)+budgets[k],spec.mesh_style)
            layer += [(min(core,key=lambda a:(math.dist(positions[a],positions[b]),a)),b) for b in periphery]
            radial.extend(periphery);edges+=layer;methods.append(method)
    return positions,groups,nominal,edges,dict(family=family,layer_methods=methods or ['ring_plus_chords'],
        region_by_bus=regions,radial_buses=radial,connectivity_requirement=spec.connectivity,basis='Synthetic spatial construction prior; not a fitted real-grid population',
        connectivity='Connected voltage layers with optional radial periphery; explicit bridgeless policy applies to each layer and whole graph. No AC contingency guarantee')


def observed_family(case,metadata):
    """Expose a construction policy only while the delivered edge inventory agrees."""
    design=metadata.get('topology_design',{})
    edges=sorted([sorted(map(int,e[:2])) for e in case['branch'] if e[10]>0])
    if edges==design.get('verified_edges'):return design.get('family')
    return None
