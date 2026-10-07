"""Conditional whole-equipment sampling from observed utility-derived models."""
import math
import random
from collections import Counter
import networkx as nx
from .rules import data

LEGACY=('research_small','research_medium','research_large')


def reference_catalog():
    catalog=data('equipment_epri.json')['catalog']
    catalog.update(data('equipment_epri_large.json')['catalog'])
    # Project only symmetric3ph cable equivalents; prefer observed2ph cohorts
    # during selection and use these records only if capacity/source gaps remain.
    for key,c in list(catalog.items()):
        if c.get('selectable',True) and c['construction']=='cable' and c['nphases']==3 and all(
            max(c[q+'matrix'][i*3+i] for i in range(3))-min(c[q+'matrix'][i*3+i] for i in range(3))<1e-7
            and max(c[q+'matrix'][i*3+j] for i in range(3) for j in range(3) if i!=j)-min(c[q+'matrix'][i*3+j] for i in range(3) for j in range(3) if i!=j)<1e-7 for q in ('r','x','c')):
            derived={**c,'nphases':2,'projection_from':key,'phase_mapping':'first two phase conductors of symmetric three-phase equivalent; source rating retained'}
            for name in ('rmatrix','xmatrix','cmatrix'):
                derived[name]=[c[name][i*3+j] for i in range(2) for j in range(2)]
            catalog[key+'_2ph']=derived
    return catalog


def uses_reference(spec):
    return spec.equipment_design.mode=='reference' or (spec.equipment_design.mode=='auto' and
        spec.voltage_kv in (6,10,20) and spec.scenario.engineering_profile in ('urban','rural'))


def describe_equipment_profiles():
    old=data('equipment_epri.json');large=data('equipment_epri_large.json')
    catalog={**old['catalog'],**large['catalog']};groups={}
    for c in catalog.values():
        key=f"{c['source_feeder']}:{c['construction']}:{c['nphases']}ph"
        group=groups.setdefault(key,{'types':0,'segments':0,'normamps_range':[float('inf'),0],
            'selectable':False,'selectable_types':0,'selectable_segments':0,'observed_voltage_kv':[]})
        group['types']+=1;group['segments']+=c['observation_count']
        if c.get('selectable',True):
            group['selectable']=True;group['selectable_types']+=1;group['selectable_segments']+=c['observation_count']
        group['normamps_range']=[min(group['normamps_range'][0],c['normamps']),max(group['normamps_range'][1],c['normamps'])]
        group['observed_voltage_kv']=sorted(set(group['observed_voltage_kv'])|set(c['observed_voltage_kv']))
    return {'profile_id':'epri_joint_equipment_v2','training_feeders':['J1','K1','Ckt5','Ckt24'],
        'inventory_only_feeders':['Ckt7'],'heldout_feeders':['M1'],
        'counts':{**old['counts'],**large['counts']},'groups':groups,
        'independent_circuit_models':5,'selectable_circuit_models':4,
        'observed_segments':sum(c['observation_count'] for c in catalog.values()),
        'selectable_segments':sum(c['observation_count'] for c in catalog.values() if c.get('selectable',True)),
        'observed_equipment_tuples':len(catalog),'selectable_equipment_tuples':sum(c.get('selectable',True) for c in catalog.values()),
        'scope':'Published actual-circuit models, not5 independent utilities or representative Chinese samples. Ckt7 construction is unknown; inventory only. M1 held out.',
        'source_urls':[old['source_url'],large['provenance']['source_url']],
        'selection':'Filter construction, phases, source, capacity; prefer actual phase cohorts; default length_voltage conditions whole tuples on observed length/role pairs and a soft current-impedance drop prior. equal_feeder gives eligible sources equal prior mass before conditioning; posterior source mass may differ. role mode retains the previous algorithm for ablation.',
        'conditional_profile':{'id':'epri_equipment_length_role_v1','observed_pairs':3904,'sources':['J1','K1','Ckt5','Ckt24'],
            'scope':'Observed length/role/equipment association plus explicit physical sizing prior; not a fitted full feeder distribution. Inverse conditional sizing uses all requested conditions.'},
        'two_phase_cable_policy':'Some Ckt24 observed2/3ph cable lines reuse1ph source definitions, quarantined rather than auto-selected. Explicit symmetric3ph principal-submatrix mapping remains the selectable2ph cable fallback.',
        'voltage_transfer':'Source operating voltage and any explicitly named voltage class retained. Ckt24 is34.5kV, its cable names specify35kV class; using at6/10/20kV is a disclosed research transfer, not target-region calibration.',
        'frequency_transfer':'Published60Hz to50Hz: X*50/60, R/C retained; no skin/earth-frequency reconstruction.'}


def required_currents(feeder):
    from .phases import _rooted
    buses,parent,children,_=_rooted(feeder)
    totals={b:{p:[0.,0.,0.] for p in (1,2,3)} for b in buses}
    for load in feeder.loads:
        powers=[(p.phase,p.kw,p.kvar,p.pv_kw) for p in load.phase_powers] if load.phase_powers else [(p,load.kw/len(load.phases),load.kvar/len(load.phases),load.pv_kw/len(load.phases)) for p in load.phases]
        for phase,p,q,pv in powers:
            totals[load.bus][phase]=[a+b for a,b in zip(totals[load.bus][phase],(p,q,pv))]
    for child in reversed(parent):
        for phase in (1,2,3):totals[parent[child]][phase]=[a+b for a,b in zip(totals[parent[child]][phase],totals[child][phase])]
    result={}
    for line in feeder.lines:
        child=line.bus2 if parent.get(line.bus2)==line.bus1 else line.bus1
        result[line.id]=(max(max(math.hypot(p,q),math.hypot(p-pv,q)) for p,q,pv in (totals[child][phase] for phase in line.phases))/(feeder.voltage_kv/math.sqrt(3)), 'terminal' if not children[child] else 'backbone')
    # Open ties carry zero now; reserve full-feeder endpoint capacity, without claiming N-1.
    amps=max((x[0] for x in result.values()),default=0)
    result.update({line.id:(amps,'backbone') for line in feeder.tie_lines})
    return result


def assign_equipment(feeder,spec,operating_feeders=None):
    if not uses_reference(spec):return
    catalog=reference_catalog();settings=spec.equipment_design
    requirements=required_currents(feeder)
    for variant in operating_feeders or []:
        for line_id,(amps,role) in required_currents(variant).items():
            requirements[line_id]=(max(requirements[line_id][0],amps),role)
    from .joint_equipment import conditioning_evidence,longest_path_km
    joint=settings.conditioning=='length_voltage'
    cohorts=data('equipment_joint.json')['cohorts'] if joint else {}
    path_km=longest_path_km(feeder) if joint else 0
    rng=random.Random(f'equipment:{feeder.seed}')
    selected=[]
    for line in sorted(feeder.lines+feeder.tie_lines,key=lambda e:e.id):
        amps,role=requirements[line.id]
        candidates={key:c for key,c in catalog.items() if c.get('selectable',True) and c['source_feeder'] in settings.reference_feeders
            and c['construction']==line.construction and c['nphases']==len(line.phases)
            and c['normamps']*settings.loading_margin>=amps}
        if not candidates:raise ValueError(f'No sourced equipment for {line.id}: {line.construction}, {len(line.phases)} phases, {amps:.3f} A; change source pool or design capacity')
        observed={k:c for k,c in candidates.items() if 'projection_from' not in c}
        if observed:candidates=observed
        minimum=min(c['normamps'] for c in candidates.values())
        candidates={k:c for k,c in candidates.items() if c['normamps']<=minimum*settings.capacity_band}
        conditional={k:c for k,c in candidates.items() if c['role_counts'].get(role,0)>0}
        fallback=not bool(conditional)
        if conditional:candidates=conditional
        keys=sorted(candidates)
        weights=[candidates[k]['observation_count'] if fallback else candidates[k]['role_counts'][role] for k in keys]
        evidence={k:conditioning_evidence(k,candidates[k],role,line,amps,feeder,settings,cohorts,path_km) for k in keys} if joint else {}
        if settings.source_weighting=='equal_feeder':
            source_totals=Counter()
            for k,w in zip(keys,weights):source_totals[candidates[k]['source_feeder']]+=w
            weights=[w/source_totals[candidates[k]['source_feeder']] for k,w in zip(keys,weights)]
        if joint:weights=[w*evidence[k]['joint_weight'] for k,w in zip(keys,weights)]
        key=rng.choices(keys,weights=weights,k=1)[0];line.conductor=key;c=candidates[key]
        selected.append({'line_id':line.id,'equipment_id':key,'source_feeder':c['source_feeder'],'source_linecode':c['source_linecode'],
            'source_sha256':c['source_sha256'],'estimated_current_a':amps,'role':role,'role_fallback':fallback,
            'phase_mapping':c.get('phase_mapping','observed phase count'),'projection_from':c.get('projection_from'),
            'candidate_count':len(keys),'selection_probability':weights[keys.index(key)]/sum(weights),
            'normamps':c['normamps'],'source_frequency_hz':c['source_frequency_hz'],'target_frequency_hz':feeder.frequency_hz,
            'source_operating_voltage_kv':c['observed_voltage_kv'],'source_voltage_class_kv':c.get('source_voltage_class_kv'),
            'source_weighting':settings.source_weighting,'target_voltage_kv':feeder.voltage_kv,**evidence.get(key,{})})
    feeder.design_evidence['equipment_selection']={'profile_id':'epri_joint_equipment_v2','settings':settings.model_dump(mode='json'),
        'verified_reference_analysis':describe_equipment_profiles()['groups'],
        'selection_reason_status':'Agent/user rationale proposes criteria; verified_reference_analysis is authoritative for numeric ranges',
        'selection_seed':feeder.seed,'algorithm':'capacity-constrained conditional whole-tuple bootstrap','lines':selected,
        'conditioning':settings.conditioning,'operating_condition_count':len(operating_feeders or []),
        'joint_profile_id':'epri_equipment_length_role_v1' if joint else None,
        'physical_prior':'Length/role likelihood times soft impedance-current path-drop score; not AC voltage prediction. Bandwidth, smoothing and voltage budget are research assumptions.',
        'scope':'Joint observed source parameters, model ratings retained; frequency and voltage transfer explicit; no independent R/X/C/rating noise.'}
    feeder.design_evidence['initial_conductor_sizing']=selected
    feeder.assumptions.append('Equipment selected as whole observed EPRI circuit tuples; source model ratings retained. X scaled by 50/60, R/C unchanged; voltage transfer is a research mapping, not insulation certification.')


def equipment_contract_matches(feeder,spec):
    catalog=data('conductors.json')
    expected=uses_reference(spec)
    for line in feeder.lines+feeder.tie_lines:
        c=catalog.get(line.conductor)
        if c is None:return False
        empirical='source_feeder' in c
        if empirical!=expected or not c.get('selectable',True):return False
        if empirical and (c['construction']!=line.construction or c['nphases']!=len(line.phases) or c['source_feeder'] not in spec.equipment_design.reference_feeders):return False
    return True


def next_upgrade(line,allowed_feeders=None):
    catalog=data('conductors.json');current=catalog[line.conductor]
    if line.conductor in LEGACY:
        i=LEGACY.index(line.conductor)
        return LEGACY[i+1] if i+1<len(LEGACY) else None
    choices=[(c['normamps'],c['r1'],key) for key,c in catalog.items() if 'source_feeder' in c and c.get('selectable',True)
        and c['source_feeder'] in (allowed_feeders or [current['source_feeder']])
        and c['construction']==current['construction'] and c['nphases']==current['nphases']
        and c['normamps']>current['normamps'] and c['r1']<=current['r1'] and c['x1']<=current['x1']]
    return min(choices)[2] if choices else None
