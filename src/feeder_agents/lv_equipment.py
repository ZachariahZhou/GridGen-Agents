"""Traceable LV product selection; grounded phase equivalent, not a four-wire solver."""
import cmath
import math
import networkx as nx
from .rules import data


def catalogue():
    return {**data('lv_conductors.json'),**data('lv_underground.json')}


def resolve_installation(spec,transformer_id=None):
    if spec.lv_regions:
        from .installation_planning import region_for_transformer
        if transformer_id is None:
            choices={r.decision.selected for r in spec.lv_regions}
            return 'regional',next(iter(choices)) if len(choices)==1 else 'mixed'
        region=region_for_transformer(spec,transformer_id)
        if region is None:raise ValueError('LV transformer has no design region')
        return resolve_installation(spec.model_copy(update={'lv_regions':[],'lv_installation_decision':region.decision}))
    profile=spec.lv_equipment_profile;installation=spec.lv_installation
    if profile=='legacy_epri':
        if spec.lv_installation_decision is not None:raise ValueError('legacy_epri is incompatible with product installation analysis')
        if installation!='auto':raise ValueError('legacy_epri is incompatible with a product installation requirement')
        return profile,'legacy_equivalent'
    decision=spec.lv_installation_decision
    if decision is not None:
        if installation!='auto' and installation!=decision.selected:
            raise ValueError('Agent installation decision conflicts with explicit installation')
        installation=decision.selected
    if installation=='auto':
        # Offline CLI fallback has no semantic request to analyse. It must not
        # pretend to be an Agent decision or derive construction from scene.
        installation='buried_duct' if profile=='nexans_underground' else 'aerial_bundle'
    resolved='nexans_abc' if installation=='aerial_bundle' else 'nexans_underground'
    if profile not in ('auto',resolved):raise ValueError('LV profile and installation are incompatible')
    return resolved,installation


def resistance(product):
    return product['rac_ohm_km'] if 'rac_ohm_km' in product else product['rac80_ohm_km']


def published_rating(product,installation):
    ratings=product.get('ratings_a',{'aerial_bundle':product.get('normamps')})
    if installation not in ratings or ratings[installation] is None:
        raise ValueError('Product is incompatible with requested installation')
    return ratings[installation]


def model_code(product, phases, derating=1., installation=None):
    """Published per-conductor R/X -> declared grounded diagonal / 2R2X loop.

    CEMPEX 2C+E has two working conductors; PE carries no normal load and is
    not mistaken for neutral. Unknown primitive mutual terms stay unmodelled.
    """
    installation=installation or product.get('default_installation','aerial_bundle')
    n=len(phases)
    if (product['cores'],n) not in ((2,1),(4,3)) or len(set(phases))!=n or any(p not in (1,2,3) for p in phases):
        raise ValueError('Product working cores do not match requested phases')
    if not math.isfinite(derating) or not 0<derating<=1:raise ValueError('Invalid ampacity derating')
    rating=published_rating(product,installation)
    c=dict(product);factor=2 if n==1 else 1
    c.update(nphases=n,projection_phases=list(phases),ampacity_derating=derating,
             installation=installation,resistance_temperature_c=product.get('resistance_temperature_c',80),
             published_normamps=rating,normamps=rating*derating,
             rmatrix=[factor*resistance(c) if i==j else 0. for i in range(n) for j in range(n)],
             xmatrix=[factor*c['x1_50hz_ohm_km'] if i==j else 0. for i in range(n) for j in range(n)],
             cmatrix=[0.]*(n*n),
             model_conversion='Diagonal grounded per-phase equivalent; two-working-core service uses 2R/2X loop; PE excluded from normal current; no mutual/neutral displacement; short-line shunt C neglected.')
    return c


def code_id(product,phases,installation=None):
    suffix='_'+(installation or product['default_installation']) if 'default_installation' in product else ''
    return product['id']+suffix+'_p'+''.join(map(str,phases))


class EquipmentSelectionError(ValueError):
    """A construction diagnostic; no model or feasible design is implied."""
    def __init__(self, diagnostic):
        self.diagnostic=diagnostic
        super().__init__(f'LV catalogue exhausted at {diagnostic["component"]}: '
            f'{diagnostic["required_current_a"]:.3f} A required, '
            f'{diagnostic["maximum_supported_current_a"]:.3f} A supported by compatible products; '
            'redesign only unspecified structural parameters, preserving powers and engineering limits')


def assign_lv_equipment(feeder,spec):
    if spec.lv_equipment_profile=='legacy_epri':return
    overall_profile,overall_installation=resolve_installation(spec)
    products=catalogue();buses={b.id:b for b in feeder.buses}
    graph=nx.DiGraph((e.bus1,e.bus2,{'length_km':e.length_km}) for e in feeder.lines if buses[e.bus1].voltage_kv<1)
    prefix={};suffix={}
    order=list(nx.topological_sort(graph))
    for node in order:prefix[node]=max((prefix[p]+graph[p][node]['length_km'] for p in graph.predecessors(node)),default=0.)
    for node in reversed(order):suffix[node]=max((suffix[c]+graph[node][c]['length_km'] for c in graph.successors(node)),default=0.)
    evidence={};vln=spec.lv_voltage_kv*1000/math.sqrt(3)
    for edge in feeder.lines:
        if buses[edge.bus1].voltage_kv>=1:continue
        profile,installation=resolve_installation(spec,buses[edge.bus1].transformer_id)
        downstream=nx.descendants(graph,edge.bus2)|{edge.bus2}
        loads=[l for l in feeder.loads if l.bus in downstream]
        # Envelope: load-only and simultaneous installed PV with zero load.
        # Also screen neutral fundamental current in the four-core bundle.
        currents=[]
        for pv_only in (False,True):
            phase=[]
            for p in (1,2,3):
                s=sum((complex(l.pv_kw,0) if pv_only else complex(l.kw,l.kvar)) for l in loads if l.phases==[p])
                phase.append(complex(s).conjugate()*1000/vln*cmath.exp(-2j*math.pi*(p-1)/3))
            currents.extend(abs(i) for i in phase)
            currents.append(abs(sum(phase)))
        demand=max(currents,default=0.)
        cores=2 if len(edge.phases)==1 else 4
        # The old star has two edges per customer path. A chain can have more:
        # reserve a share of the full head-to-customer drop budget for each edge.
        path_edges=len(nx.ancestors(graph,edge.bus2))+max(nx.single_source_shortest_path_length(graph,edge.bus2).values(),default=0)
        longest_path_km=prefix[edge.bus2]+suffix[edge.bus2]
        allocated_drop=(spec.lv_drop_budget_pu*edge.length_km/longest_path_km if spec.customer_connection in ('distributed_taps','mixed_taps') else spec.lv_drop_budget_pu/max(1,path_edges))
        # For any root-to-user path, every denominator is at least that path's
        # length, so the sum of allocated edge drops cannot exceed the budget.
        choices=[];capacities=[]
        for p in products.values():
            if p['cores']!=cores or p['construction']!=('overhead' if installation=='aerial_bundle' else 'cable'):continue
            c=model_code(p,edge.phases,spec.lv_ampacity_derating,installation)
            drop=demand*edge.length_km*math.hypot(c['rmatrix'][0],c['xmatrix'][0])/vln
            supported=min(c['normamps']*spec.loading_margin,
                allocated_drop*vln/max(edge.length_km*math.hypot(c['rmatrix'][0],c['xmatrix'][0]),1e-12))
            capacities.append(supported)
            if c['normamps']*spec.loading_margin>=demand and drop<=allocated_drop:
                choices.append((p['section_mm2'],p,c,drop))
        if not choices:
            raise EquipmentSelectionError(dict(code='lv_catalogue_exhausted',component=edge.id,
                connection='service' if cores==2 else 'lv_branch',installation=installation,
                required_current_a=demand,maximum_supported_current_a=max(capacities,default=0.),
                compatible_products=len(capacities),length_km=edge.length_km,downstream_users=len(loads),
                suggested_parameters=['users'] if cores==2 else ['lv_branches','transformer_count'],
                caveat='Suggestions are not permissions. Original explicit counts, load/PV, catalogue and sizing bounds remain binding.'))
        _,p,c,drop=min(choices,key=lambda row:row[0])
        edge.conductor=code_id(p,edge.phases,installation);edge.construction=p['construction']
        feeder.equipment_catalog[edge.conductor]=c
        evidence[edge.id]=dict(product=p['id'],installation=installation,region_id=buses[edge.bus1].region_id,design_current_a=demand,estimated_drop_pu=drop,
            selection='smallest section satisfying phase/neutral fundamental current envelope and path-length allocated drop budget',
            allocated_drop_budget_pu=allocated_drop,longest_customer_path_edges=path_edges,longest_customer_path_km=longest_path_km,
            drop_allocation='length_proportional' if spec.customer_connection in ('distributed_taps','mixed_taps') else 'edge_count',
            ampacity_derating=spec.lv_ampacity_derating)
    used={e.conductor for e in feeder.lines+feeder.tie_lines}
    feeder.equipment_catalog={k:v for k,v in feeder.equipment_catalog.items() if k in used}
    feeder.assumptions=[a for a in feeder.assumptions if 'LV line uses EPRI' not in a and 'capacitance' not in a.lower()]
    feeder.assumptions.extend([f'LV equipment profile={overall_profile}; installation={overall_installation}; sourced products with declared handbook mapping.',
        'LV matrices are grounded diagonal equivalents of published R/X; two-core services use 2R/2X loop. No explicit neutral displacement or harmonic heating.',
        'Ampacity uses the selected installation column and recorded environmental conditions; user derating is a multiplier, not a thermal recalculation.'])
    from .installation_planning import decision_record
    feeder.design_evidence['lv_equipment']=dict(**decision_record(spec),profile=overall_profile,requested_profile=spec.lv_equipment_profile,installation=overall_installation,lines=evidence,
        drop_budget_pu=spec.lv_drop_budget_pu,method='load/PV envelope, fundamental neutral screening, depth-aware path voltage-drop budget; final OpenDSS acceptance required')

    if spec.lv_regions:
        feeder.design_evidence['lv_equipment']['regions']=[dict(
            **r.model_dump(),users=sum(buses[l.bus].region_id==r.id for l in feeder.loads),
            total_kw=sum(l.kw for l in feeder.loads if buses[l.bus].region_id==r.id),
            line_ids=[e.id for e in feeder.lines if buses[e.bus1].voltage_kv<1 and buses[e.bus1].region_id==r.id]) for r in spec.lv_regions]


def next_lv_upgrade(feeder,line):
    old=feeder.equipment_catalog[line.conductor]
    if 'published_normamps' not in old:return None
    installation=old.get('installation','aerial_bundle')
    candidates=[]
    for p in catalogue().values():
        if (p['cores'],p['family'],p['construction'])!=(old['cores'],old['family'],old['construction']):continue
        if (published_rating(p,installation)>old['published_normamps'] and p['section_mm2']>old['section_mm2']
            and resistance(p)<=resistance(old)
            and math.hypot(resistance(p),p['x1_50hz_ohm_km'])<=math.hypot(resistance(old),old['x1_50hz_ohm_km'])):
            candidates.append(p)
    return code_id(min(candidates,key=lambda p:p['section_mm2']),line.phases,installation) if candidates else None
