"""MV/transformer/LV/customer research models, with explicit voltage boundaries."""
from collections import Counter
import math
from pathlib import Path
import random
from typing import Literal

import networkx as nx
from pydantic import Field, model_validator

from .schemas import StrictModel, Bus, Feeder, Line, Load, PhasePower, ExperimentSpec
from .rules import data
from .structure_targets import StructureTargets,validate_target_scope,structural_constraints
from .installation_planning import InstallationDecision, InstallationRegion
from .hierarchy_topology_design import MVTopologySettings, resolve_mv_topology, resolve_lv_topology


class HierarchicalSpec(StrictModel):
    scene: Literal['urban','rural']='urban'
    voltage_kv: Literal[6,10,20]=10
    lv_voltage_kv: Literal[.38,.4]=.4
    transformer_count: int=Field(default=3,ge=1,le=32)
    mv_buses: int | None=Field(default=None,ge=2,le=1001,
        description='MV buses including source, independent of transformer count. Omitted with fixed total retains T+1; unconstrained corridor defaults use 2T+1.')
    customer_allocation: Literal['varied','balanced']='varied'
    customer_connection: Literal['distributed_taps','service_star','mixed_taps']=Field(default='distributed_taps',description='Distributed taps: each customer bus is a three-phase public-line connection point with one single-phase load; successive points follow a lateral. No extra buses or meter-side daisy chaining. service_star uses dedicated single-phase terminal services from the branch junction. mixed_taps combines public ABC points with single-phase terminal services without adding buses. service_km bounds apply to consecutive connection-point spacing in distributed mode.')
    mv_topology_policy: Literal['legacy_v1','branched_v2','branched_v3','branched_v4']='branched_v4'
    mv_topology: MVTopologySettings=Field(default_factory=MVTopologySettings,
        description='MV structural family. Auto uses a scene/size prior; explicit choices override scene. Rings have normally-open ties and radial operation, one equivalent source.')
    structure_targets: StructureTargets | None=Field(default=None,description='Only explicit numeric MV graph goals; unmet goals require bounded feedback and solver verification.')
    lv_topology: Literal['auto','branch_star','radial_chain','mixed_radial']='auto'
    lv_branches: int=Field(default=2,ge=1,le=6)
    users: int | None=Field(default=None,ge=1,le=1000)
    n_buses: int | None=Field(default=None,ge=5,le=2001)
    total_kw: float=Field(default=96,gt=0,le=10000)
    power_factor: float=Field(default=.95,ge=.8,le=1)
    pv_ratio: float=Field(default=.3,ge=0,le=3)
    phase_weights: list[float]=Field(default_factory=lambda:[.5,.3,.2],min_length=3,max_length=3,
        description='Fractions of single-phase customer counts; actual phase kW fractions are reported, not forced exact.')
    mv_segment_km_min: float=Field(default=.05,ge=.005,le=5)
    mv_segment_km_max: float=Field(default=.15,ge=.005,le=5)
    lv_segment_km_min: float=Field(default=.01,ge=.001,le=.3)
    lv_segment_km_max: float=Field(default=.03,ge=.001,le=.3)
    service_km_min: float=Field(default=.003,ge=.001,le=.1)
    service_km_max: float=Field(default=.012,ge=.001,le=.1)
    lv_equipment_profile: Literal['auto','nexans_abc','nexans_underground','legacy_epri']='auto'
    lv_installation: Literal['auto','aerial_bundle','buried_direct','buried_duct']='auto'
    lv_installation_decision: InstallationDecision | None=Field(default=None,description='Agent scenario analysis: compare aerial/direct burial/duct; quoted facts separated from assumptions. Explicit installation wins; conflicting decisions rejected.')
    lv_regions: list[InstallationRegion]=Field(default_factory=list,max_length=32,description='Optional named transformer supply regions with separate Agent installation decisions; must cover each transformer exactly once. Does not specify geographic polygons or region load allocations.')
    lv_ampacity_derating: float=Field(default=1,gt=0,le=1,description='Multiplier on the selected product/installation rating and its documented thermal conditions; not a thermal model.')
    lv_drop_budget_pu: float=Field(default=.05,gt=0,le=.2,description='LV head-to-customer estimated drop budget, divided by the longest path through each edge; final power flow governs acceptance.')
    loading_margin: float=Field(default=.8,gt=0,le=1)
    mv_voltage_min_pu: float=Field(default=.93,gt=.5,lt=1)
    mv_voltage_max_pu: float=Field(default=1.07,gt=1,lt=1.5)
    lv_voltage_min_pu: float=Field(default=.9,gt=.5,lt=1)
    lv_voltage_max_pu: float=Field(default=1.1,gt=1,lt=1.5)
    max_vuf_percent: float=Field(default=2,gt=0,le=20)
    count: int=Field(default=1,ge=1,le=100)
    seed: int=Field(default=42,ge=0,le=2**32-1)

    @model_validator(mode='before')
    @classmethod
    def scene_defaults(cls,value):
        if isinstance(value,dict) and value.get('scene')=='rural':
            value=dict(value)
            for key,default in dict(mv_segment_km_min=.1,mv_segment_km_max=.3,lv_segment_km_min=.02,
                                    lv_segment_km_max=.05,service_km_min=.005,service_km_max=.015).items():
                value.setdefault(key,default)
        return value

    @model_validator(mode='after')
    def counts(self):
        from .lv_equipment import resolve_installation
        from .installation_planning import validate_regions
        validate_regions(self)
        resolve_installation(self)
        if self.mv_buses is None:
            detailed=self.n_buses is None and self.mv_topology.family not in ('balanced_tree','irregular_tree')
            self.mv_buses=1+self.transformer_count*(2 if detailed else 1)
        if self.mv_buses<self.transformer_count+1:
            raise ValueError('mv_buses must include source and at least one MV tap per transformer')
        infrastructure=self.mv_buses+self.transformer_count*(1+self.lv_branches)
        if self.users is None:
            self.users=self.n_buses-infrastructure if self.n_buses is not None else max(24,self.transformer_count*self.lv_branches)
        if self.users<self.transformer_count*self.lv_branches:
            raise ValueError('Need at least one user per LV branch; increase users/n_buses or reduce transformers/branches')
        expected=infrastructure+self.users
        if self.n_buses is not None and self.n_buses!=expected:
            raise ValueError(f'Conflicting counts: total buses must equal mv_buses+transformers*(1+lv_branches)+users={expected}')
        if expected>2001 or self.users>1000:raise ValueError('Hierarchy exceeds node/user limits')
        self.n_buses=expected
        if any(not math.isfinite(w) or w<=0 for w in self.phase_weights) or not math.isclose(sum(self.phase_weights),1,abs_tol=1e-9):
            raise ValueError('phase_weights must be three positive fractions summing to1')
        for name in ('mv_segment','lv_segment','service'):
            if getattr(self,name+'_km_min')>getattr(self,name+'_km_max'):raise ValueError('Inverted length interval')
        validate_target_scope(self)
        resolve_mv_topology(self)
        from .hierarchy_structure import transformer_tap_indices
        transformer_tap_indices(self,self.seed)
        return self


class VoltageBus(Bus):
    voltage_kv: float=Field(gt=0)
    role: Literal['source','mv_tap','mv_junction','lv_bus','lv_branch','customer']
    transformer_id: str | None=None
    region_id: str | None=None


class DistributionTransformer(StrictModel):
    id: str
    bus1: str
    bus2: str
    primary_kv: float=Field(gt=1)
    secondary_kv: float=Field(gt=0,lt=1)
    kva: float=Field(gt=0)
    xhl_percent: float=Field(gt=0)
    loadloss_percent: float=Field(gt=0)
    noloadloss_percent: float=Field(ge=0)
    imag_percent: float=Field(ge=0)
    connection: Literal['delta_grounded_wye_lag']='delta_grounded_wye_lag'
    source: dict


class HierarchicalFeeder(Feeder):
    buses: list[VoltageBus]
    transformers: list[DistributionTransformer]
    equipment_catalog: dict


class HierarchicalPlan(StrictModel):
    research_question: str
    spec: HierarchicalSpec
    assumptions: list[str]=Field(default_factory=list)


def historical_hierarchy_spec(payload):
    """Read persisted pre-topology records without applying today's scene defaults.

    Only use at artifact-reading boundaries. New requests retain auto selection.
    """
    payload=dict(payload)
    payload.setdefault('mv_topology_policy','legacy_v1')
    legacy='mv_buses' not in payload and 'customer_allocation' not in payload
    payload.setdefault('mv_topology',{'family':'balanced_tree'})
    payload.setdefault('lv_topology','branch_star')
    if legacy and payload['lv_topology']=='auto':
        payload['lv_topology']='radial_chain' if payload.get('scene')=='rural' else 'branch_star'
    payload.setdefault('mv_buses',payload.get('transformer_count',3)+1)
    payload.setdefault('customer_allocation','balanced')
    payload.setdefault('customer_connection','service_star')
    return HierarchicalSpec.model_validate(payload)


def hierarchy_input_defaults():
    """Prompt defaults preserve omitted counts instead of advertising a solved example."""
    defaults=HierarchicalSpec().model_dump()
    defaults.update(users=None,n_buses=None,mv_buses=None)
    return defaults


def hierarchy_capabilities():
    from .lv_equipment import catalogue
    c=data('hierarchy_equipment.json')
    return dict(local_topology_feedback='Opt-in reconnect_service: terminal customer reattachment to a nearer LV branch (service_star) or terminal public connection point (distributed_taps) in the same transformer/region, within service length bounds; preserve positions, phases, load, equipment and radiality. Explicit topology freeze overrides opt-in. No general rewiring or cross-transformer transfer.',
                product='Research feeder network model, not time-series or snapshot data generation',
                deliverables=['network topology','spatial layout','phase assignments','equipment parameters','customer load configuration','visualization','OpenDSS model'],
                excluded_deliverables=['load/PV time series','8760-hour profiles'],
                validation_role='Internal steady-state electrical checks; optional user-specified conditions do not imply time-series generation',
                voltage_kv=[6,10,20],lv_voltage_kv=[.38,.4],frequency_hz=50,
                mv_topologies=['branched_network','long_trunk','comb','multi_branch','balanced_tree','irregular_tree','open_ring','ring_laterals','multi_open_ring'],
                topology_policy='New auto uses branched_network: unequal synthetic corridors with nested branches, source degree one and ordinary active degree <=4 under branched_v4 (sparse feasible degree-4 junctions), <=3 under v2/v3. terminal_count is exact, bounded by transformers and floor(mv_buses/2). Urban terminal density ceiling .22 versus rural .17. V4 jointly reserves ceil(T/3) inline transformer taps, capped at T-1, when terminal_count is omitted; explicit terminal_count or positive tie requirements override this soft reserve. These are design priors, not fitted real-feeder rates. Urban auto adds 1–3 local normally-open ties only with >=17 MV buses and >=3 terminals; rural default zero. Explicit local_tie_count=0–8 overrides either scene. New branched_v4 uses v3 variable corridor spacing within given length bounds and selects spatially feasible local ties without an eight-edge target or universal hop upper bound; distinct endpoints and exact requested counts remain mandatory. V4 spreads nonterminal taps by graph distance and makes constrained local MV construction edits with a soft sparse degree-4 target, recording unmet targets. These edits happen before equipment sizing, not through the LLM feedback loop. Historical branched_v2/v3 keep their former behavior. Pure ring families are explicit options, never the new default. Historical auto models use legacy_v1 policy. mv_buses includes source, independent of transformer_count; each operating MV terminal has a transformer. Multi-open-ring ring_count 2–4. All models have one equivalent source and radial energized topology; no independent supplies or N-1 certification. Unconstrained corridor defaults: 2T+1 MV buses; fixed-total requests and explicit balanced/irregular trees retain T+1 unless specified.',
                lv_topologies=['branch_star','radial_chain','mixed_radial'],lv_topology_policy='Auto: urban mixed_radial (station-wise chain/star), rural radial_chain; explicit choice overrides. lv_branches is the exact number of LV distribution junctions per transformer. customer_allocation=varied uses seeded heterogeneous users per station and branch; balanced preserves even allocation. These are research priors, not calibrated populations.',
                customer_connections=['distributed_taps','service_star','mixed_taps'],customer_connection_policy='Default distributed_taps: one single-phase load at each three-phase public LV connection point, connected successively along branch laterals. Exact buses/users retained; no added service-drop nodes. Explicit service_star preserves terminal services. Historical files retain service_star.',
                transformer_ratings_kva=sorted({t['kva'] for t in c['transformers']}),
                lv_line=c['lv_line'],lv_products=catalogue(),default_lv_profile='auto',lv_selection_policy='Agent compares construction alternatives from request evidence; no urban/rural installation mapping. Offline unanalysed auto uses a labelled aerial baseline.',lv_installations=['aerial_bundle','buried_direct','buried_duct'],lv_regional_scope='lv_regions assigns complete transformer supply areas to separate installation decisions; no arbitrary polygons, region load quotas or within-transformer mixed construction',transfer=c['transfer'],
                structural_feedback=dict(target_fields=['structure_targets.max_mv_depth','structure_targets.max_tap_distance_hops'],scope='Explicit upper bounds on MV source depth and nearest transformer hop distance for non-source MV buses, only branched_v4/branched_network. Ordinary Agent loop may reattach spatially feasible MV subtrees, preserving coordinates, leaves, taps, source exit, ties, phases and demands. Topology freeze forbids this action. Every selected edit requires new power flow and non-regressing constraint deficits; bounded search may stop unmet.'),
                boundary='Grounded-equivalent LV, no explicit neutral displacement; single-phase customer services. Separate hierarchical_inverse path supports guarded local transformer/MV/LV upgrades and same-phase PV relocation; no arbitrary feedback revision.',
                inverse_task='hierarchical_inverse',inverse_proposal_policies=['violation_order','diverse'],post_search_verification='Optional named conditions evaluated only after candidate selection; separate from search targets; no robustness guarantee over an interval.',inverse_actions=['upgrade_transformer','upgrade_mv_line','upgrade_lv_line','relocate_pv'],
                node_formula='mv_buses+transformer_count*(1+lv_branches)+users')


def generate_hierarchy(spec: HierarchicalSpec,seed: int):
    from .generation import generate_feeder
    from .equipment import assign_equipment
    rng=random.Random(seed)
    source=data('hierarchy_equipment.json')
    tcount=spec.transformer_count
    mv_spec=ExperimentSpec(count=1,n_buses=max(2,tcount)+1,total_kw_min=spec.total_kw,total_kw_max=spec.total_kw,
        power_factor=spec.power_factor,pv_ratio=spec.pv_ratio,voltage_kv=spec.voltage_kv,
        segment_km_min=spec.mv_segment_km_min,segment_km_max=spec.mv_segment_km_max,
        scenario={'kind':spec.scene,'engineering_profile':spec.scene,'layout':'structured_radial','topology':{'family':'balanced_tree'}},
        phase_design={'mode':'unbalanced','single_phase_laterals':0,'two_phase_laterals':0},
        max_repairs=0,repair_policy={'strategy':'none'})
    mv=generate_feeder(mv_spec,seed)
    if tcount==1:
        keep={mv.source_bus,mv.loads[0].bus}
        mv.buses=[b for b in mv.buses if b.id in keep]
        mv.lines=[e for e in mv.lines if e.bus1 in keep and e.bus2 in keep]
        mv.loads=mv.loads[:1]
    from .hierarchy_topology_design import design_mv_topology
    design_mv_topology(mv,spec)
    from .hierarchy_structure import transformer_tap_indices,customer_groups,lv_chain_for_transformer
    taps=[f'b{i}' for i in transformer_tap_indices(spec,seed)]
    for load,bus in zip(mv.loads,taps):load.bus=bus
    group_counts=customer_groups(spec,seed)
    from .scene_profiles import apply_scene_profile
    mv=apply_scene_profile(mv,mv_spec)
    buses=[VoltageBus(**b.model_dump(),voltage_kv=spec.voltage_kv,role='source' if b.id==mv.source_bus else 'mv_tap' if b.id in taps else 'mv_junction') for b in mv.buses]
    busmap={b.id:b for b in buses}
    user_weights=[rng.lognormvariate(0,.3) for _ in range(spec.users)]
    weights_sum=sum(user_weights)
    user_kw=[spec.total_kw*w/weights_sum for w in user_weights]
    user_kw[-1]+=spec.total_kw-sum(user_kw)
    # Stratified phase allocation ensures diversity; actual ratios are reported.
    phase_counts=[int(spec.users*w) for w in spec.phase_weights]
    for i in sorted(range(3),key=lambda p:spec.users*spec.phase_weights[p]-phase_counts[p],reverse=True)[:spec.users-sum(phase_counts)]:phase_counts[i]+=1
    user_phases=[p+1 for p,n in enumerate(phase_counts) for _ in range(n)]
    rng.shuffle(user_phases)
    loads=[];lv_lines=[];transformers=[];catalog={};index=0
    phase_totals=Counter()
    def lv_code(phases):
        code='lv_ckt7_'+''.join(map(str,phases))
        if code not in catalog:
            c=dict(source['lv_line'])
            for q in ('r','x','c'):
                m=c[q+'matrix'];c[q+'matrix']=[m[(a-1)*3+b-1] for a in phases for b in phases]
            c['nphases']=len(phases);c['projection_phases']=phases
            catalog[code]=c
        return code
    for t,aggregate in enumerate(mv.loads):
        txid=f'tx_{t+1}';tap=busmap[aggregate.bus];headid=f'lv_{t+1}'
        # Drawing offset only; transformer has no physical line length.
        head=VoltageBus(id=headid,x_km=tap.x_km+.001,y_km=tap.y_km+.001,
                         voltage_kv=spec.lv_voltage_kv,role='lv_bus',transformer_id=txid)
        buses.append(head)
        group_loads=[]
        chain=lv_chain_for_transformer(spec,seed,t)
        chain_angle=random.Random(f'lv-chain:{seed}:{t}').uniform(-math.pi,math.pi)
        previous=head
        for branch in range(spec.lv_branches):
            angle=(chain_angle if chain else 2*math.pi*branch/spec.lv_branches)+rng.uniform(-.25,.25)
            length=rng.uniform(spec.lv_segment_km_min,spec.lv_segment_km_max)
            bid=f'lv_{t+1}_branch_{branch+1}'
            parent=previous if chain else head
            junction=VoltageBus(id=bid,x_km=parent.x_km+length*math.cos(angle),y_km=parent.y_km+length*math.sin(angle),
                voltage_kv=spec.lv_voltage_kv,role='lv_branch',transformer_id=txid)
            buses.append(junction)
            lv_lines.append(Line(id=f'lv_main_{t+1}_{branch+1}',bus1=parent.id,bus2=bid,length_km=length,conductor=lv_code([1,2,3])))
            previous=junction
            service_parent=junction;public_points=[]
            count=group_counts[t][branch];mixed=spec.customer_connection=='mixed_taps'
            fraction=random.Random(f'mixed-taps:{seed}:{t}:{branch}').uniform(.5,.72)
            public_count=max(1,min(count-1,round(count*fraction))) if mixed and count>1 else 0
            for local_index in range(count):
                phase=user_phases[index];kw=user_kw[index];index+=1
                uid=f'user_{index}';distance=rng.uniform(spec.service_km_min,spec.service_km_max)
                theta=rng.uniform(0,2*math.pi)
                distributed=spec.customer_connection=='distributed_taps' or (mixed and local_index<public_count)
                parent=service_parent if distributed else (public_points[(local_index-public_count)%len(public_points)] if public_points else junction)
                route_angle=angle+(math.pi/2 if branch%2==0 else -math.pi/2)+(theta-math.pi)*.05 if distributed else (angle+(local_index%2)*math.pi+(theta-math.pi)*.1 if mixed else theta)
                line_phases=[1,2,3] if distributed else [phase]
                customer=VoltageBus(id=uid,x_km=parent.x_km+distance*math.cos(route_angle),y_km=parent.y_km+distance*math.sin(route_angle),
                    phases=line_phases,voltage_kv=spec.lv_voltage_kv,role='customer',transformer_id=txid)
                buses.append(customer)
                lv_lines.append(Line(id=f'service_{index}',bus1=parent.id,bus2=uid,phases=line_phases,length_km=distance,conductor=lv_code(line_phases)))
                if distributed:service_parent=customer;public_points.append(customer)
                kvar=kw*math.tan(math.acos(spec.power_factor));pv=kw*spec.pv_ratio
                load=Load(id=f'load_{index}',bus=uid,kw=kw,kvar=kvar,pv_kw=pv,contract_kva=kw/spec.power_factor,
                    phases=[phase],category='residential',phase_powers=[PhasePower(phase=phase,kw=kw,kvar=kvar,pv_kw=pv)])
                loads.append(load);group_loads.append(load);phase_totals[phase]+=kw
        p=[sum(l.kw for l in group_loads if l.phases==[phase]) for phase in (1,2,3)]
        required=3*max(max(v/spec.power_factor,v*spec.pv_ratio) for v in p)/spec.loading_margin
        choices=[c for c in source['transformers'] if c['kva']>=required]
        if not choices:raise ValueError(f'No sourced transformer can carry worst-phase demand at {txid}: required {required:g}kVA')
        rating=min(c['kva'] for c in choices)
        choices=[c for c in choices if c['kva']==rating]
        chosen=rng.choices(choices,weights=[c['observations'] for c in choices],k=1)[0]
        transformers.append(DistributionTransformer(id=txid,bus1=tap.id,bus2=headid,primary_kv=spec.voltage_kv,
            secondary_kv=spec.lv_voltage_kv,kva=chosen['kva'],xhl_percent=chosen['xhl_percent']*50/60,
            loadloss_percent=chosen['loadloss_percent'],noloadloss_percent=chosen['noloadloss_percent'],
            imag_percent=chosen['imag_percent'],source={**chosen,'required_kva_worst_phase':required,'transfer':source['transfer']}))
        aggregate.kw=sum(p);aggregate.kvar=aggregate.kw*math.tan(math.acos(spec.power_factor))
        aggregate.pv_kw=aggregate.kw*spec.pv_ratio;aggregate.contract_kva=aggregate.kw/spec.power_factor
        aggregate.phase_powers=[PhasePower(phase=phase,kw=p[phase-1],kvar=p[phase-1]*math.tan(math.acos(spec.power_factor)),pv_kw=p[phase-1]*spec.pv_ratio) for phase in (1,2,3)]
    # Re-size MV lines after allocating actual customer phases and station powers.
    assign_equipment(mv,mv_spec)
    mv_catalog=data('conductors.json')
    catalog.update({line.conductor:mv_catalog[line.conductor] for line in mv.lines+mv.tie_lines})
    feeder=HierarchicalFeeder(seed=seed,voltage_kv=spec.voltage_kv,frequency_hz=50,phase_mode='unbalanced',
        source_bus=mv.source_bus,total_kw=spec.total_kw,buses=buses,lines=mv.lines+lv_lines,tie_lines=mv.tie_lines,loads=loads,
        transformers=transformers,equipment_catalog=catalog,
        assumptions=[source['transfer'],'LV line uses EPRI source generic secondary equivalent, not a calibrated LV product catalogue.',
            source['lv_line'].get('capacitance_policy','Source LV capacitance retained.'),
            'Synthetic spatial hierarchy; grounded return equivalent; no neutral displacement, time series or protection.',
            'Normally-open MV ties are physical links; energized topology remains radial. No independent multi-source or restoration guarantee.',
            'Delta/grounded-wye lag target connection is explicit; it is not claimed as the source transformer vector group.',
            'Voltage and VUF limits are configurable research acceptance bounds.'],
        design_evidence={'hierarchical':True,'spec':spec.model_dump(exclude={'structure_targets'} if spec.structure_targets is None else set()),'actual_load_phase_kw':{str(k):v for k,v in phase_totals.items()},
                         'node_count_formula':'MV+T*(1+B)+U','mv_initial_design':mv.design_evidence,
                         'structure_resolution':{'mv_buses':spec.mv_buses,'transformer_taps':taps,
                            'customer_groups':group_counts,'allocation':spec.customer_allocation,
                            'basis':'Seeded synthetic structural variation; not a fitted utility population'},
                         'topology_policy_version':spec.mv_topology_policy,'resolved_mv_topology':resolve_mv_topology(spec).model_dump(),
                         'resolved_lv_topology':resolve_lv_topology(spec),
                         'customer_connection':{'mode':spec.customer_connection,'bus_semantics':'Public ABC connection points and single-phase terminal services; load phases remain fixed; no additional buses' if spec.customer_connection=='mixed_taps' else 'Public LV connection point with a single-phase customer injection; three-phase through conductor; no separate meter drop' if spec.customer_connection=='distributed_taps' else 'Single-phase terminal with dedicated service from LV junction'}})

    from .lv_equipment import assign_lv_equipment
    if spec.lv_regions:
        from .installation_planning import region_for_transformer
        taps={t.bus1:t.id for t in feeder.transformers}
        for bus in feeder.buses:
            region=region_for_transformer(spec,bus.transformer_id or taps.get(bus.id))
            if region:bus.region_id=region.id
    assign_lv_equipment(feeder,spec)
    return feeder


def export_hierarchy(feeder: HierarchicalFeeder,directory: Path):
    from .simulation import export_dss
    directory=Path(directory)
    mv_ids={b.id for b in feeder.buses if b.voltage_kv>1}
    mv=Feeder(seed=feeder.seed,voltage_kv=feeder.voltage_kv,frequency_hz=50,source_bus=feeder.source_bus,
        total_kw=feeder.total_kw,phase_mode='unbalanced',buses=[Bus(**{k:v for k,v in b.model_dump().items() if k in Bus.model_fields}) for b in feeder.buses if b.id in mv_ids],
        lines=[e for e in feeder.lines if e.bus1 in mv_ids],tie_lines=feeder.tie_lines,loads=[],assumptions=[])
    master=export_dss(mv,directory)
    codes=[];lines=[];used=set()
    for line in feeder.lines:
        if line.bus1 in mv_ids:continue
        c=feeder.equipment_catalog[line.conductor];n=len(line.phases)
        if line.conductor not in used:
            matrices=[]
            for q in ('r','x','c'):
                m=c[q+'matrix'];factor=50/c['source_frequency_hz'] if q=='x' else 1
                rows=[' '.join(f'{m[i*n+j]*factor:.12g}' for j in range(i+1)) for i in range(n)]
                matrices.append(f'{q}matrix=['+ ' | '.join(rows)+']')
            codes.append(f'! {c["source_definition"]} sha256={c["source_sha256"]}; grounded-equivalent model (conversion in equipment_catalog)')
            codes.append(f'New LineCode.{line.conductor} nphases={n} units=km basefreq=50 normamps={c["normamps"]} rg=0 xg=0 '+' '.join(matrices))
            used.add(line.conductor)
        phases='.'.join(map(str,line.phases))
        lines.append(f'New Line.{line.id} bus1={line.bus1}.{phases} bus2={line.bus2}.{phases} phases={n} linecode={line.conductor} length={line.length_km:.12g} units=km normamps={c["normamps"]}')
    for filename,contents in [('LineCodes.dss',codes),('Lines.dss',lines)]:
        with (directory/filename).open('a') as stream:stream.write('\n'.join(contents)+'\n')
    tx=[]
    for t in feeder.transformers:
        tx.append(f'! source={t.source["source_path"]}:{t.source["source_line"]} sha256={t.source["source_sha256"]}')
        tx.append(f'New Transformer.{t.id} phases=3 windings=2 buses=[{t.bus1}.1.2.3 {t.bus2}.1.2.3.0] conns=[delta wye] '
            f'kvs=[{t.primary_kv} {t.secondary_kv}] kvas=[{t.kva} {t.kva}] xhl={t.xhl_percent:.12g} '
            f'%loadloss={t.loadloss_percent:.12g} %noloadloss={t.noloadloss_percent:.12g} %imag={t.imag_percent:.12g} leadlag=lag '
            'wdg=2 rneut=0 xneut=0')
    (directory/'Transformers.dss').write_text('\n'.join(tx)+'\n')
    busmap={b.id:b for b in feeder.buses};loads=[];pv=[]
    for l in feeder.loads:
        kv=busmap[l.bus].voltage_kv/math.sqrt(3);phase=l.phases[0]
        loads.append(f'New Load.{l.id} phases=1 bus1={l.bus}.{phase}.0 conn=wye kv={kv:.12g} kw={l.kw:.12g} kvar={l.kvar:.12g} model=1 vminpu=.01 vmaxpu=2')
        if l.pv_kw:pv.append(f'New Generator.pv_{l.id} phases=1 bus1={l.bus}.{phase}.0 conn=wye kv={kv:.12g} kw={l.pv_kw:.12g} kvar=0 model=1 vminpu=.01 vmaxpu=2')
    (directory/'Loads.dss').write_text('\n'.join(loads)+'\n');(directory/'Generation.dss').write_text('\n'.join(pv)+'\n')
    (directory/'BusCoords.csv').write_text('\n'.join(f'{b.id}, {b.x_km:.12g}, {b.y_km:.12g}' for b in feeder.buses)+'\n')
    levels=sorted({b.voltage_kv for b in feeder.buses})
    content=master.read_text().replace('Redirect Loads.dss','Redirect Transformers.dss\nRedirect Loads.dss')
    content=content.replace(f'Set VoltageBases=[{feeder.voltage_kv}]','Set VoltageBases=['+' '.join(map(str,levels))+']')
    master.write_text(content)
    return master


def evaluate_hierarchy(f: HierarchicalFeeder,spec: HierarchicalSpec,result: dict):
    buses={b.id:b for b in f.buses};graph=nx.MultiGraph();graph.add_nodes_from(buses)
    graph.add_edges_from((e.bus1,e.bus2) for e in f.lines)
    graph.add_edges_from((t.bus1,t.bus2) for t in f.transformers)
    checks={}
    checks['radial_connected']=bool(buses) and nx.is_tree(graph) and set(graph)==set(buses) and len(buses)==len(f.buses)
    all_lines=f.lines+f.tie_lines
    checks['identifiers']=len({e.id.casefold() for e in all_lines})==len(all_lines) and len({t.id for t in f.transformers})==len(f.transformers) and len({l.id for l in f.loads})==len(f.loads)
    checks['voltage_boundaries']=all(e.bus1 in buses and e.bus2 in buses and buses[e.bus1].voltage_kv==buses[e.bus2].voltage_kv for e in all_lines) and all(
        t.bus1 in buses and t.bus2 in buses and buses[t.bus1].voltage_kv==t.primary_kv==spec.voltage_kv and buses[t.bus2].voltage_kv==t.secondary_kv==spec.lv_voltage_kv for t in f.transformers)
    checks['phase_continuity']=all(e.bus1 in buses and e.bus2 in buses and set(e.phases)<=set(buses[e.bus1].phases) and set(e.phases)<=set(buses[e.bus2].phases) for e in all_lines)
    checks['user_connections']=len({l.bus for l in f.loads})==len(f.loads) and all(
        l.bus in buses and buses[l.bus].role=='customer' and set(l.phases)<=set(buses[l.bus].phases) and len(l.phases)==1 and buses[l.bus].voltage_kv==spec.lv_voltage_kv for l in f.loads)
    from .customer_connections import connection_matches
    checks['customer_connection']=connection_matches(f,spec.customer_connection)
    checks['tier_roles']=Counter(b.role for b in f.buses)==Counter({'source':1,'mv_tap':spec.transformer_count,
        'mv_junction':spec.mv_buses-spec.transformer_count-1,'lv_bus':spec.transformer_count,
        'lv_branch':spec.transformer_count*spec.lv_branches,'customer':spec.users})
    taps={t.bus1 for t in f.transformers}
    checks['transformer_taps']=len(taps)==spec.transformer_count and taps=={b.id for b in f.buses if b.role=='mv_tap'}
    geometry=True
    for e in all_lines:
        if e.bus1 not in buses or e.bus2 not in buses:
            geometry=False;continue
        a,b=buses[e.bus1],buses[e.bus2]
        kind='mv_segment' if a.voltage_kv>1 else 'service' if b.role=='customer' else 'lv_segment'
        geometry=geometry and math.isclose(e.length_km,math.hypot(a.x_km-b.x_km,a.y_km-b.y_km),rel_tol=1e-8,abs_tol=1e-10)
        geometry=geometry and getattr(spec,kind+'_km_min')-1e-10<=e.length_km<=getattr(spec,kind+'_km_max')+1e-10
    checks['geometry']=geometry
    # Historical artifacts predate these contracts; do not relabel their topology.
    if f.design_evidence.get('topology_policy_version') or 'mv_topology' in spec.model_fields_set or f.tie_lines:
        from .hierarchy_topology_design import topology_contract_matches,lv_topology_matches
        checks['mv_topology']=topology_contract_matches(f,spec,result)
        checks['lv_topology']=lv_topology_matches(f,spec)
    if spec.lv_regions:
        from .installation_planning import region_contract_valid
        checks['regional_design']=region_contract_valid(f,spec)
    volt=result.get('bus_phase_voltage_pu',{});curr=result.get('line_current_a',{});tx=result.get('transformer_metrics',{})
    def finite(x):return isinstance(x,(int,float)) and math.isfinite(x)
    complete=all(set(volt.get(b.id,{}))==set(map(str,b.phases)) and all(finite(v) and v>0 for v in volt.get(b.id,{}).values()) for b in f.buses)
    complete=complete and all(finite(curr.get(e.id)) for e in all_lines)
    complete=complete and all(t.id in tx and finite(tx[t.id].get('max_phase_loading_ratio'))
        and tx[t.id]['max_phase_loading_ratio']>=0 and len(tx[t.id].get('terminal_kva',[]))==2
        and all(finite(v) and v>=0 for v in tx[t.id]['terminal_kva']) for t in f.transformers)
    complete=complete and all(finite(result.get(k)) for k in ('source_kw','load_kw','generation_kw','loss_kw'))
    checks['measurements_complete']=complete
    checks['converged']=result.get('converged') is True
    checks['voltage_bases']=all(finite(result.get('bus_base_kv_ln',{}).get(b.id)) and math.isclose(result['bus_base_kv_ln'][b.id],b.voltage_kv/math.sqrt(3),rel_tol=1e-6) for b in f.buses)
    tolerance=max(.01,spec.total_kw*1e-5)
    checks['demand']=len(f.loads)==spec.users and len(f.buses)==spec.n_buses and len(f.transformers)==spec.transformer_count and math.isclose(sum(l.kw for l in f.loads),spec.total_kw,abs_tol=1e-7) and math.isclose(sum(l.pv_kw for l in f.loads),spec.total_kw*spec.pv_ratio,abs_tol=1e-7)
    checks['demand']=checks['demand'] and all(math.isclose(l.kw/math.hypot(l.kw,l.kvar),spec.power_factor,rel_tol=1e-9) for l in f.loads)
    checks['delivered_power']=complete and abs(result['load_kw']-spec.total_kw)<tolerance and abs(result['generation_kw']-spec.total_kw*spec.pv_ratio)<tolerance
    # Aggregate agreement cannot detect swapped customer powers. Verify each
    # exported constant-PQ load/PV against the selected model as well.
    from .injection_contract import element_injections_match
    injection_checks=element_injections_match(f,result)
    checks['delivered_element_power']=all(injection_checks.values())
    expected_counts=[int(spec.users*w) for w in spec.phase_weights]
    for i in sorted(range(3),key=lambda p:spec.users*spec.phase_weights[p]-expected_counts[p],reverse=True)[:spec.users-sum(expected_counts)]:expected_counts[i]+=1
    actual_counts=Counter(l.phases[0] for l in f.loads if len(l.phases)==1)
    checks['phase_allocation']=actual_counts==Counter({p+1:n for p,n in enumerate(expected_counts)})
    checks['phase_allocation']=checks['phase_allocation'] and all(len(l.phase_powers)==1 and len(l.phases)==1
        and l.phase_powers[0].phase==l.phases[0] and all(math.isclose(getattr(l.phase_powers[0],k),getattr(l,k),rel_tol=1e-9,abs_tol=1e-8)
            for k in ('kw','kvar','pv_kw')) for l in f.loads)
    checks['power_balance']=complete and abs(result['source_kw']-result['load_kw']+result['generation_kw']-result['loss_kw'])<tolerance
    voltage_metrics={}
    for level in ('mv','lv'):
        subset=[b for b in f.buses if (b.voltage_kv>1)==(level=='mv')]
        values=[v for b in subset for v in volt.get(b.id,{}).values()]
        layer_complete=bool(subset) and all(set(volt.get(b.id,{}))==set(map(str,b.phases)) for b in subset)
        valid_values=layer_complete and bool(values) and all(finite(v) and v>0 for v in values)
        voltage_metrics[level+'_min_voltage_pu']=min(values) if valid_values else None
        voltage_metrics[level+'_max_voltage_pu']=max(values) if valid_values else None
        lo=getattr(spec,level+'_voltage_min_pu');hi=getattr(spec,level+'_voltage_max_pu')
        checks[level+'_voltage']=complete and all(lo<=v<=hi for b in subset for v in volt[b.id].values())
    checks['line_capacity']=complete and all(curr[e.id]<=f.equipment_catalog[e.conductor]['normamps']*(1+1e-8) for e in all_lines)
    checks['transformer_capacity']=complete and all(tx[t.id]['max_phase_loading_ratio']<=1+1e-8 and max(tx[t.id]['terminal_kva'])<=t.kva*(1+1e-8) for t in f.transformers)
    eligible=[b for b in f.buses if len(b.phases)==3]
    checks['unbalance']=all(finite(result.get('bus_vuf_percent',{}).get(b.id)) and result['bus_vuf_percent'][b.id]<=spec.max_vuf_percent for b in eligible)
    if spec.structure_targets is not None:
        checks['structure_targets']=all(c['deficit']==0 for c in structural_constraints(f,spec))
    return dict(accepted=all(checks.values()),checks=checks,**voltage_metrics,
        element_injection_checks=injection_checks,
        transformer_max_loading=max((x['max_phase_loading_ratio'] for x in tx.values()),default=None),
        min_voltage_pu=result.get('min_voltage_pu'),max_vuf_percent=result.get('max_vuf_percent'),
        boundary='Snapshot research acceptance with grounded-equivalent LV; not neutral/earthing/protection certification')
