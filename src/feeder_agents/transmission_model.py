"""Physical-unit transmission equipment and explicit voltage-layer topology."""
import math
import numpy as np
import networkx as nx
from .artifacts import digest
from .rules import data
from .transmission_equipment import LINE_POLICY, select_line_profile, matches_line_profile

TRANSFORMER_RATINGS=(63.,100.,160.,250.,400.,630.,1000.,1600.,2500.)


def line_record(a,b,positions,kv,circuits=1):
    profile=data('transmission_lines.json')['profiles'][str(kv)]
    return dict(kind='line',from_bus=a,to_bus=b,length_km=math.dist(positions[a],positions[b]),circuits=circuits,voltage_kv=kv,**profile)


def equipment_row(e,base=100.):
    if e['kind']=='transformer':
        rating=e['rating_mva'];r=e['r_pu']*base/rating;x=e['x_pu']*base/rating
        return [e['from_bus'],e['to_bus'],r,x,0,rating,rating,rating,e['tap'],0,1,-360,360]
    z=e['voltage_kv']**2/base;l=e['length_km'];n=e['circuits']
    rating=math.sqrt(3)*e['voltage_kv']*e['max_i_ka']*n
    return [e['from_bus'],e['to_bus'],e['r_ohm_km']*l/z/n,e['x_ohm_km']*l/z/n,
        2*math.pi*50*e['c_nf_km']*1e-9*l*z*n,rating,rating,rating,0,0,1,-360,360]


def refresh(case,meta):
    case['branch']=np.asarray([equipment_row(e,case['baseMVA']) for e in meta['branch_evidence']],dtype=float)
    case['bus'][:,5]=0.
    for row in case['branch']:
        for end in row[:2]:case['bus'][int(end)-1,5]-=row[4]*case['baseMVA']*.5*meta['shunt_fraction']


def dc_flows(case):
    """Lossless screening on the actual current branch reactances."""
    n=len(case['bus']);incidence=np.zeros((len(case['branch']),n))
    for i,row in enumerate(case['branch']):
        incidence[i,int(row[0])-1]=1;incidence[i,int(row[1])-1]=-1
    sus=1/case['branch'][:,3]
    inj=-case['bus'][:,2].copy()
    for row in case['gen']:inj[int(row[0])-1]+=row[1]
    lap=incidence.T@(sus[:,None]*incidence);theta=np.zeros(n)
    theta[1:]=np.linalg.solve(lap[1:,1:],inj[1:]/case['baseMVA'])
    return case['baseMVA']*sus*(incidence@theta)


def generate(spec,seed):
    rng=np.random.default_rng(seed);n=spec.n_buses
    from .transmission_topology import build_topology
    positions,groups,nominal,edges,topology_design=build_topology(spec,rng,seed)
    bus=np.zeros((n,13));bus[:,0]=np.arange(1,n+1);bus[:,1]=1;bus[:,6]=1;bus[:,7]=1;bus[:,9]=nominal;bus[:,10]=1;bus[:,11]=spec.voltage_max_pu;bus[:,12]=spec.voltage_min_pu
    weights=rng.lognormal(0,.25 if spec.load_pattern=='dispersed' else 1.,n);bus[:,2]=spec.total_mw*weights/weights.sum();bus[-1,2]+=spec.total_mw-bus[:,2].sum();bus[:,3]=bus[:,2]*math.tan(math.acos(spec.power_factor))
    sites=spec.generator_buses or [int(i*n/spec.n_generators)+1 for i in range(spec.n_generators)]
    weights=np.asarray(spec.generator_weights or [1.]*spec.n_generators,dtype=float);weights/=weights.sum()
    gen=np.zeros((spec.n_generators,21))
    for i,b in enumerate(sites):
        bus[b-1,1]=3 if i==0 else 2;share=spec.total_mw*weights[i]
        gen[i,:10]=[b,share,0,share*.9,-share*.9,1.,100.,1.,share*1.6,0.]
    evidence=[line_record(a,b,positions,int(bus[a-1,9])) for a,b in edges]
    rating=next((x for x in TRANSFORMER_RATINGS if x>=spec.total_mw/spec.n_generators*2),TRANSFORMER_RATINGS[-1])
    for left,right in zip(groups,groups[1:]):
        peripheral=set(topology_design['radial_buses'])
        available=sorted((math.dist(positions[a],positions[b]),a,b) for a in left for b in right if a not in peripheral and b not in peripheral);used=set()
        for _ in range(2):
            _,a,b=next(p for p in available if p[1] not in used and p[2] not in used);used.update((a,b))
            evidence.append(dict(kind='transformer',from_bus=a,to_bus=b,primary_kv=int(bus[a-1,9]),secondary_kv=int(bus[b-1,9]),rating_mva=rating,r_pu=.003,x_pu=.12,tap=1.,basis='Synthetic two-winding engineering template; impedance on transformer MVA base; no site geometry claim'))
    meta=dict(network_kind='transmission',phase_model='balanced positive sequence',positions_km=positions,generator_types=spec.generator_types,
        branch_evidence=evidence,topology_design=topology_design,shunt_fraction=spec.shunt_compensation,parameter_policy='engineering_plausibility',frequency_hz=50,
        catalogue_hash=digest(data('transmission_lines.json')),reference='Physical-unit line profiles and declared transformer engineering templates',
        assumptions=['50Hz balanced steady-state research model; no dynamic, OPF or N-1 certification.',
            'Each voltage layer uses the recorded spatial mesh policy or an explicitly requested ring; two transformer links connect adjacent layers. No empirical topology-distribution match is claimed.',
            'Generator Pmax=1.6 times allocated demand and Q limits=+/-0.9 times demand are engineering assumptions, not manufacturer capability curves.',
            'Complete voltage-compatible overhead-line R/X/C/rating tuples are drawn using declared engineering weights, not fitted real-grid frequencies; 110/220 kV use published standard types and 330/500/750 kV use engineering families.',
            'Up to four equivalent line circuits are initially sized by lossless DC screening after equipment selection; AC limits remain authoritative.'])
    case=dict(version='2',baseMVA=100.,bus=bus,gen=gen,branch=np.zeros((0,13)));refresh(case,meta)
    # Independent stream: adding equipment choices must not change topology,
    # bus locations, load allocation or generator placement for a fixed seed.
    equipment_rng=np.random.default_rng(np.random.SeedSequence([int(seed),87]))
    families=data('transmission_line_families.json')
    meta.update(line_parameter_policy=LINE_POLICY,line_family_catalogue_hash=digest(families))
    for e,flow in zip(evidence,dc_flows(case)):
        if e['kind']=='line':
            profile,selection=select_line_profile(e['voltage_kv'],e['length_km'],flow,spec.power_factor,equipment_rng,families)
            e.update(profile);e['selection']=selection
    refresh(case,meta)
    flows=dc_flows(case)
    for e,row,flow in zip(evidence,case['branch'],flows):
        e['dc_screening_mw']=float(flow)
        if e['kind']=='line':e['circuits']=min(4,max(1,math.ceil(abs(flow)/spec.power_factor/(.8*row[5]))))
    refresh(case,meta)
    meta['topology_design']['verified_edges']=sorted([sorted(map(int,e[:2])) for e in case['branch'] if e[10]>0])
    return case,meta


def parameter_checks(case,spec,meta):
    catalogue=data('transmission_lines.json');positions={int(k):v for k,v in meta['positions_km'].items()}
    families=data('transmission_line_families.json')
    policy=meta.get('line_parameter_policy')
    family_valid=policy is None or (policy==LINE_POLICY and meta.get('line_family_catalogue_hash')==digest(families))
    evidence=meta['branch_evidence'];lines=True;transformers=True
    expected_shunts=np.zeros(spec.n_buses)
    for row,e in zip(case['branch'],evidence):
        consistent=np.allclose(row[:13],equipment_row(e,case['baseMVA']),rtol=1e-10,atol=1e-12)
        if e['kind']=='line':
            profile=catalogue['profiles'][str(e['voltage_kv'])]
            match=(matches_line_profile(e,families) if policy==LINE_POLICY else
                   policy is None and 'profile_id' not in e and all(e.get(k)==v for k,v in profile.items()))
            lines=lines and consistent and match and e['circuits'] in (1,2,3,4) and e['length_km']>0 and math.isclose(e['length_km'],math.dist(positions[e['from_bus']],positions[e['to_bus']]),rel_tol=1e-10)
            lines=lines and all(case['bus'][int(b)-1,9]==e['voltage_kv'] for b in row[:2])
        else:
            transformers=transformers and consistent and e['rating_mva'] in TRANSFORMER_RATINGS and e['r_pu']==.003 and e['x_pu']==.12 and .9<=e['tap']<=1.1
            transformers=transformers and case['bus'][e['from_bus']-1,9]==e['primary_kv'] and case['bus'][e['to_bus']-1,9]==e['secondary_kv'] and e['primary_kv']!=e['secondary_kv']
        for end in row[:2]:expected_shunts[int(end)-1]-=row[4]*case['baseMVA']*.5*meta['shunt_fraction']
    nominal=[v.kv for v in spec.voltage_layers for _ in range(v.buses)] if spec.voltage_layers else [spec.voltage_kv]*spec.n_buses
    return dict(line_unit_consistency=bool(lines and len(evidence)==len(case['branch'])),transformer_consistency=bool(transformers),
        shunt_consistency=bool(0<=meta['shunt_fraction']<=1 and np.allclose(case['bus'][:,5],expected_shunts)),
        nominal_voltage_consistency=bool(np.array_equal(case['bus'][:,9],nominal)),parameter_catalogue_consistency=bool(meta['catalogue_hash']==digest(catalogue) and family_valid))
