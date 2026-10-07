"""Parameterized single-source radial research families; synthetic geometry."""
import math
import random
from collections import defaultdict
from .schemas import Bus,Line


def topology_edges(n,settings,seed):
    """Integer bus 0 is source. Returns directed edges and declared trunk."""
    family=settings.family
    if family in {'long_trunk','open_ring'}:
        return [(i-1,i) for i in range(1,n+1)],list(range(n+1))
    if family=='multi_branch':
        count=settings.branch_count or min(3,n)
        if count>n:raise ValueError('branch_count exceeds available non-source nodes')
        edges=[];next_id=1
        for j in range(count):
            parent=0
            for _ in range(n//count+(j<n%count)):
                edges.append((parent,next_id));parent=next_id;next_id+=1
        return edges,[]
    if family=='comb':
        trunk=max(1,min(n-1,math.ceil(n*(settings.trunk_fraction or .4))))
        count=settings.branch_count or min(4,trunk,n-trunk)
        if count>min(trunk,n-trunk):raise ValueError('comb branch_count needs enough distinct trunk anchors and lateral nodes')
        edges=[(i-1,i) for i in range(1,trunk+1)];next_id=trunk+1
        for j in range(count):
            parent=(j+1)*trunk//count
            for _ in range((n-trunk)//count+(j<(n-trunk)%count)):
                edges.append((parent,next_id));parent=next_id;next_id+=1
        return edges,list(range(trunk+1))
    factor=settings.branching_factor or 2
    if family=='balanced_tree':
        return [((i-1)//factor,i) for i in range(1,n+1)],[]
    rng=random.Random(f'irregular-parents:{seed}')
    available=[0];children=defaultdict(int);edges=[]
    for i in range(1,n+1):
        parent=rng.choice(available)
        edges.append((parent,i));children[parent]+=1
        if children[parent]>=factor:available.remove(parent)
        available.append(i)
    return edges,[]


def design_topology(feeder,spec):
    from .settlements import size_conductors
    cfg=spec.scenario.topology;n=len(feeder.buses)-1
    edges,trunk=topology_edges(n,cfg,feeder.seed)
    step=random.Random(f'topology-step:{feeder.seed}').uniform(spec.segment_km_min,spec.segment_km_max)
    positions={0:(0.,0.)};children=defaultdict(list)
    for a,b in edges:children[a].append(b)
    if cfg.family=='open_ring':
        radius=step/(2*math.sin(math.pi/(n+1)))
        positions={i:(radius*(1-math.cos(2*math.pi*i/(n+1))),radius*math.sin(2*math.pi*i/(n+1))) for i in range(n+1)}
    elif cfg.family=='long_trunk':positions={i:(i*step,0.) for i in range(n+1)}
    elif cfg.family=='comb':
        positions.update({i:(i*step,0.) for i in trunk})
        arm=0
        for a,b in edges:
            if b in positions:continue
            if a in trunk:arm+=1
            sign=1 if arm%2 else -1
            positions[b]=(positions[a][0],positions[a][1]+sign*step)
    elif cfg.family=='multi_branch':
        angle=0.
        for a,b in edges:
            if a==0:angle=2*math.pi*children[0].index(b)/len(children[0])
            positions[b]=(positions[a][0]+step*math.cos(angle),positions[a][1]+step*math.sin(angle))
    else:
        sectors={0:(-math.pi/2+.1,math.pi/2-.1)}
        for a,b in edges:
            low,high=sectors[a];count=len(children[a]);j=children[a].index(b)
            left=low+(high-low)*j/count;right=low+(high-low)*(j+1)/count
            angle=(left+right)/2
            positions[b]=(positions[a][0]+step*math.cos(angle),positions[a][1]+step*math.sin(angle))
            sectors[b]=(left,right)
    if len(set(positions.values()))!=n+1:raise ValueError('Topology embedding produced coincident buses')
    name=lambda i:'source' if i==0 else f'b{i}'
    feeder.buses=[Bus(id=name(i),x_km=positions[i][0],y_km=positions[i][1]) for i in range(n+1)]
    feeder.lines=[Line(id=f'l{i}',bus1=name(a),bus2=name(b),length_km=math.dist(positions[a],positions[b]),
                       conductor='research_small') for i,(a,b) in enumerate(edges,1)]
    if cfg.family=='open_ring':
        feeder.tie_lines=[Line(id='tie1',bus1=name(n),bus2='source',length_km=math.dist(positions[n],positions[0]),conductor='research_small')]
    feeder.design_evidence={'method':'structured_radial_v1','topology_design':{
        'family':cfg.family,'parameters':cfg.model_dump(),'generation_step_km':step,
        'parent_seed':feeder.seed,'operating_topology':'single_source_radial',
        'physical_topology':'one_open_ring' if cfg.family=='open_ring' else 'radial_before_optional_ties',
        'scope':'Synthetic structural research family, not population fit or GIS'},
        'trunk_buses':[name(i) for i in trunk],
        'initial_conductor_sizing':size_conductors(feeder)}
    feeder.assumptions += ['Topology family '+cfg.family+' uses synthetic geometric embedding and one sampled common segment length; spatial crossings do not create junctions.',
        'All non-source nodes are aggregate load sites in this mode. Multiple arms share one equivalent source; no independent multi-source operation.',
        'Normally-open ties, if requested, are physical branches excluded from the energized radial tree. No switching sequence, restoration or N-1 certification.']
    return feeder


def topology_matches(feeder,spec):
    if spec.scenario.layout!='structured_radial':return True
    cfg=spec.scenario.topology;n=len(feeder.buses)-1
    # Revision batch seeds can differ from the generation identity; store it.
    seed=feeder.design_evidence.get('topology_design',{}).get('parent_seed',feeder.seed)
    edges,trunk=topology_edges(n,cfg,seed)
    name=lambda i:'source' if i==0 else f'b{i}'
    expected={frozenset((name(a),name(b))) for a,b in edges}
    actual={frozenset((e.bus1,e.bus2)) for e in feeder.lines}
    if actual!=expected:return False
    if feeder.design_evidence.get('trunk_buses',[])!=[name(i) for i in trunk]:return False
    if cfg.family=='open_ring':
        return len(feeder.tie_lines)==1 and frozenset((feeder.tie_lines[0].bus1,feeder.tie_lines[0].bus2))==frozenset((name(n),'source'))
    return True
