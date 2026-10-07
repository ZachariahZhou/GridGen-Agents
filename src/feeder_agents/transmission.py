"""Synthetic balanced transmission research cases, separate from distribution."""
import copy
import fcntl
import hashlib
import html
import importlib.metadata
import json
import math
from pathlib import Path
import re
from typing import Literal
import zipfile
import networkx as nx
import numpy as np
from pydantic import Field,model_validator
from .schemas import StrictModel,DocumentRule
from .artifacts import atomic_json,digest,file_digest


class VoltageLayer(StrictModel):
    kv: Literal[110,220,330,500,750]
    buses: int=Field(ge=3,le=300)


class TransmissionTarget(StrictModel):
    metric: Literal['min_voltage_pu','max_voltage_pu','max_branch_loading','losses_mw']
    operator: Literal['ge','le']
    threshold: float=Field(ge=0)


class TransmissionCondition(StrictModel):
    name: str=Field(pattern=r'^[A-Za-z0-9_-]{1,40}$')
    load_scale: float=Field(default=1.,gt=0,le=3)
    targets: list[TransmissionTarget]=Field(default_factory=list,max_length=12)


class TransmissionSpec(StrictModel):
    network_kind: Literal['transmission']='transmission'
    voltage_layers: list[VoltageLayer] | None=Field(default=None,description='Explicit nominal-voltage layers. The generator automatically installs two explicit two-winding transformers between adjacent layers with catalogue-sized MVA, R/X and taps; no transformer_count or user-entered transformer parameters are needed.')
    allow_topology_changes: bool=False
    connectivity: Literal['connected','bridgeless']='connected'
    radial_bus_count: int | None=Field(default=None,ge=0,le=297,description='Optional exact count of terminal peripheral buses across voltage layers; omitted means unconstrained, generation starts with zero. Not compatible with ring or bridgeless requests.')
    mesh_style: Literal['local','mixed','long_distance']='local'
    mesh_family: Literal['auto','regional','corridor','ring_chords']=Field(default='auto',description='Auto meshed uses a spatial regional tree augmented into closed corridors; corridor uses an elongated footprint; ring_chords preserves the historical ring backbone. Explicit ring topology uses ring_chords.')
    load_pattern: Literal['dispersed','concentrated']='dispersed'
    generator_buses: list[int] | None=None
    generator_weights: list[float] | None=None
    targets: list[TransmissionTarget]=Field(default_factory=list,max_length=12)
    document_rules: list[DocumentRule]=Field(default_factory=list,max_length=32)
    validation_conditions: list[TransmissionCondition]=Field(default_factory=list,max_length=6)
    allowed_repairs: list[Literal['shunt_step','voltage_setpoint','parallel_line','upgrade_transformer','transformer_tap','redispatch','relocate_corridor']]=Field(default_factory=lambda:['shunt_step','voltage_setpoint','parallel_line','upgrade_transformer','transformer_tap','redispatch','relocate_corridor'])
    n_buses: int=Field(default=14,ge=4,le=300)
    n_generators: int=Field(default=3,ge=1,le=100)
    voltage_kv: Literal[110,220,330,500,750]=220
    total_mw: float=Field(default=300,gt=0,le=50000)
    power_factor: float=Field(default=.97,ge=.85,le=1)
    topology: Literal['ring','meshed']='meshed'
    extra_edges: int | None=Field(default=None,ge=0,le=400)
    shunt_compensation: float=Field(default=.7,ge=0,le=1,description='Initial fixed-reactor charging compensation fraction; shunt_step may revise it only if allowed_repairs includes that action.')
    radius_km: float=Field(default=40,ge=1,le=500)
    generator_types: list[Literal['generic','thermal','hydro','wind','solar']] | None=None
    voltage_min_pu: float=Field(default=.95,gt=.5,lt=1)
    voltage_max_pu: float=Field(default=1.05,gt=1,lt=1.5)
    count: int=Field(default=1,ge=1,le=100)
    seed: int=Field(default=42,ge=0,le=2**32-1)

    @model_validator(mode='before')
    @classmethod
    def derive_primary_voltage(cls,value):
        # The first explicit voltage layer defines the primary voltage. Share
        # this representation rule across direct, one-shot and Agent callers.
        # An explicitly conflicting value remains an error in scope().
        if isinstance(value,dict) and value.get('voltage_layers') and 'voltage_kv' not in value:
            first=value['voltage_layers'][0]
            kv=first.get('kv') if isinstance(first,dict) else getattr(first,'kv',None)
            if kv is not None:value=dict(value,voltage_kv=kv)
        return value

    @model_validator(mode='after')
    def scope(self):
        if len({c.name for c in self.validation_conditions})!=len(self.validation_conditions):raise ValueError('Condition names must be unique')
        if len({r.rule_id for r in self.document_rules})!=len(self.document_rules):raise ValueError('Duplicate document rule IDs')
        if len(set(self.allowed_repairs))!=len(self.allowed_repairs):raise ValueError('Repair permissions must be unique')
        bounds={}
        for target in self.targets:
            if not math.isfinite(target.threshold):raise ValueError('Targets must be finite')
            d=bounds.setdefault(target.metric,{'ge':float('-inf'),'le':float('inf')})
            d[target.operator]=(max if target.operator=='ge' else min)(d[target.operator],target.threshold)
            if d['ge']>d['le']:raise ValueError('Conflicting bounds for '+target.metric)
            if target.metric in ('min_voltage_pu','max_voltage_pu') and ((target.operator=='ge' and target.threshold>self.voltage_max_pu) or (target.operator=='le' and target.threshold<self.voltage_min_pu)):
                raise ValueError('Target conflicts with protected voltage limits')
            if target.metric=='max_branch_loading' and target.operator=='ge' and target.threshold>1:raise ValueError('Target conflicts with protected branch capacity')
        if self.n_generators>=self.n_buses:raise ValueError('At least one PQ bus is required')
        if self.topology=='ring' and self.extra_edges not in (None,0):raise ValueError('Ring cannot have extra mesh edges')
        if self.topology=='ring' and self.mesh_family not in ('auto','ring_chords'):raise ValueError('Ring topology conflicts with a non-ring mesh_family')
        groups=[v.buses for v in self.voltage_layers] if self.voltage_layers else [self.n_buses]
        if self.voltage_layers:
            if sum(groups)!=self.n_buses:raise ValueError('Voltage-layer bus counts must sum to n_buses')
            if len({v.kv for v in self.voltage_layers})!=len(groups):raise ValueError('Voltage layers must have distinct nominal voltages')
            if self.voltage_layers[0].kv!=self.voltage_kv:raise ValueError('First layer must match voltage_kv')
        voltages={v.kv for v in self.voltage_layers} if self.voltage_layers else {self.voltage_kv}
        for rule in self.document_rules:
            if not set(rule.voltage_levels_kv)<=voltages or rule.scenario!='any' or rule.mode=='stress' or rule.load_semantics=='high_voltage_users':raise ValueError('Document rule applicability does not match transmission model')
        if self.generator_buses is not None and (len(self.generator_buses)!=self.n_generators or len(set(self.generator_buses))!=self.n_generators or any(b<1 or b>self.n_buses for b in self.generator_buses)):
            raise ValueError('generator_buses must be unique valid bus IDs with n_generators entries')
        if self.generator_weights is not None and (len(self.generator_weights)!=self.n_generators or any(not math.isfinite(w) or w<=0 for w in self.generator_weights)):
            raise ValueError('generator_weights requires n_generators positive finite weights')
        from .transmission_topology import radial_allocations
        if self.radial_bus_count and (self.connectivity=='bridgeless' or self.topology=='ring' or self.mesh_family=='ring_chords'):
            raise ValueError('radial peripheral buses conflict with bridgeless or ring construction')
        peripheral=radial_allocations(self)
        maximum=sum((n-r)*(n-r-1)//2-(n-r) for n,r in zip(groups,peripheral))
        if self.extra_edges is None:self.extra_edges=0 if self.topology=='ring' else min(maximum,max(1,self.n_buses//3))
        if self.extra_edges>maximum:raise ValueError('Too many edges for a simple network')
        if self.topology=='meshed' and self.extra_edges==0 and maximum>0:raise ValueError('Meshed requires at least one chord')
        if self.generator_types is None:self.generator_types=['generic']*self.n_generators
        if len(self.generator_types)==1:self.generator_types=self.generator_types*self.n_generators
        if len(self.generator_types)!=self.n_generators:raise ValueError('One static type label per generator is required')
        # Composed operating conditions must satisfy the same protected bounds
        # before generation starts. The helper clears nested conditions.
        for condition in self.validation_conditions:
            try:_condition_spec(self,condition)
            except ValueError as exc:raise ValueError(f'Invalid validation condition {condition.name!r}: {exc}') from exc
        return self


def historical_transmission_spec(payload):
    values=dict(payload);values.setdefault('connectivity','bridgeless')
    return TransmissionSpec.model_validate(values)


def dependencies():
    try:
        from pypower.api import runpf,ppoption
        from pypower.case9 import case9
    except ImportError as exc:raise ValueError('Install transmission extras: pip install -e ".[transmission]"') from exc
    return runpf,ppoption,case9


def generate_case(spec,seed):
    from .transmission_model import generate
    return generate(spec,seed)


def parameter_checks(case,spec,metadata):
    from .transmission_model import parameter_checks as check
    return check(case,spec,metadata)


def validate_case(case,spec,metadata=None,*,legacy_topology=False):
    runpf,ppoption,_=dependencies()
    solved,converged=runpf(copy.deepcopy(case),ppoption(VERBOSE=0,OUT_ALL=0,PF_ALG=1,PF_TOL=1e-9,PF_MAX_IT=30,ENFORCE_Q_LIMS=0))
    bus=solved['bus'];gen=solved['gen'];branch=solved['branch'];tol=1e-5
    finite=all(np.isfinite(x).all() for x in (bus,gen,branch))
    graph=nx.Graph();graph.add_nodes_from(bus[:,0].astype(int));graph.add_edges_from((int(r[0]),int(r[1])) for r in branch if r[10]>0)
    losses=float(np.sum(branch[:,13]+branch[:,15])) if branch.shape[1]>=17 else float('nan')
    loading=np.maximum(np.hypot(branch[:,13],branch[:,14]),np.hypot(branch[:,15],branch[:,16]))/np.maximum(branch[:,5],1e-12)
    # Bind requested PF to each actual bus injection, preserving signs and
    # requiring QD=0 at zero PD without dividing by the bus demand.
    expected_q=bus[:,2]*math.tan(math.acos(spec.power_factor))
    from .transmission_topology import graph_contract
    checks=dict(converged=bool(converged),finite=bool(finite),connected=nx.is_connected(graph),
        topology=graph_contract(graph,spec,legacy_global=legacy_topology),
        demand=bool(abs(bus[:,2].sum()-spec.total_mw)<tol and np.allclose(bus[:,3],expected_q,rtol=1e-9,atol=tol)),
        voltage=bool(np.all(bus[:,7]>=spec.voltage_min_pu-tol) and np.all(bus[:,7]<=spec.voltage_max_pu+tol)),
        generator_p_limits=bool(np.all(gen[:,1]>=gen[:,9]-tol) and np.all(gen[:,1]<=gen[:,8]+tol)),
        generator_q_limits=bool(np.all(gen[:,2]>=gen[:,4]-tol) and np.all(gen[:,2]<=gen[:,3]+tol)),
        branch_limits=bool(np.all(branch[:,5]>0) and np.all(loading<=1+tol)),
        balance=bool(abs(gen[:,1].sum()-bus[:,2].sum()-losses)<tol and losses>=-tol))
    if metadata is not None:checks.update(parameter_checks(case,spec,metadata))
    metrics=dict(min_voltage_pu=float(bus[:,7].min()),max_voltage_pu=float(bus[:,7].max()),max_branch_loading=float(loading.max()),
        generation_mw=float(gen[:,1].sum()),demand_mw=float(bus[:,2].sum()),losses_mw=losses)
    # Invalid numerical results are recorded explicitly, not JSON NaN.
    metrics={k:v if math.isfinite(v) else None for k,v in metrics.items()}
    for i,target in enumerate(spec.targets):
        value=metrics[target.metric]
        checks[f'target_{i}']=value is not None and (value>=target.threshold-tol if target.operator=='ge' else value<=target.threshold+tol)
    document_observations=document_rule_observations(solved,spec,metadata)
    for row in document_observations:
        key='rule:'+row['rule_id']
        checks[key]=checks.get(key,True) and row['passed']
    conditions=[]
    for condition in spec.validation_conditions:
        trial,condition_spec=condition_case(case,spec,condition)
        validation,_=validate_case(trial,condition_spec,metadata,legacy_topology=legacy_topology)
        checks['condition:'+condition.name]=validation['accepted']
        conditions.append(dict(name=condition.name,load_scale=condition.load_scale,**validation))
    return dict(accepted=all(checks.values()),converged=bool(converged),checks=checks,metrics=metrics,conditions=conditions,document_rules=document_observations,
        boundary='Steady-state AC check with explicit post-solve P/Q/rating checks; no OPF, security or dynamic certification.'),solved


def document_rule_observations(case,spec,metadata):
    observations=[]
    for rule in spec.document_rules:
        buses=[r for r in case['bus'] if r[9] in rule.voltage_levels_kv]
        indices=[i for i,e in enumerate(metadata['branch_evidence']) if e['kind']=='line' and e['voltage_kv'] in rule.voltage_levels_kv] if metadata else []
        value=None
        if rule.metric=='min_voltage_pu' and buses:value=min(r[7] for r in buses)
        elif rule.metric=='max_voltage_pu' and buses:value=max(r[7] for r in buses)
        elif rule.metric=='max_loading_ratio' and indices and case['branch'].shape[1]>=17:
            value=max(max(np.hypot(case['branch'][i,13],case['branch'][i,14]),np.hypot(case['branch'][i,15],case['branch'][i,16]))/case['branch'][i,5] for i in indices)
        elif rule.metric=='max_line_km' and indices:value=max(metadata['branch_evidence'][i]['length_km'] for i in indices)
        elif rule.metric=='total_line_km' and indices:value=sum(metadata['branch_evidence'][i]['length_km'] for i in indices)
        value=float(value) if value is not None and np.isfinite(value) else None
        passed=value is not None and (value>=rule.threshold-1e-7 if rule.operator=='ge' else value<=rule.threshold+1e-7)
        observations.append(dict(rule_id=rule.rule_id,metric=rule.metric,operator=rule.operator,threshold=rule.threshold,value=value,passed=passed,quote=rule.quote,locator=rule.locator))
    return observations


def _condition_spec(spec,condition):
    values=spec.model_dump();values.update(total_mw=spec.total_mw*condition.load_scale,targets=[t.model_dump() for t in condition.targets],validation_conditions=[])
    return TransmissionSpec(**values)


def condition_case(case,spec,condition):
    condition_spec=_condition_spec(spec,condition)
    trial=copy.deepcopy(case);trial['bus'][:,2:4]*=condition.load_scale;trial['gen'][:,1]*=condition.load_scale
    return trial,condition_spec


def case_payload(case):
    return {k:v.tolist() if hasattr(v,'tolist') else v for k,v in case.items() if k in ('version','baseMVA','bus','gen','branch')}


def export_case(case,path):
    lines=['function mpc = case_generated','% Synthetic transmission research case; see metadata.json for assumptions.',"mpc.version = '2';",f"mpc.baseMVA = {case['baseMVA']};"]
    for name in ('bus','gen','branch'):
        lines.append(f'mpc.{name} = [')
        lines.extend(' '.join(format(float(x),'.15g') for x in row)+';' for row in case[name])
        lines.append('];')
    Path(path).write_text('\n'.join(lines)+'\n')


def visualization(case,metadata,checks):
    from .visual_theme import svg_page,fit_coordinates
    points=fit_coordinates({int(k):v for k,v in metadata['positions_km'].items()});body=[]
    for index,row in enumerate(case['branch'],1):
        a,b=int(row[0]),int(row[1]);x,y=points[a];xx,yy=points[b]
        label=html.escape(f'Branch {index} | {a}–{b} | R={row[2]:.5g} pu | X={row[3]:.5g} pu | rating={row[5]:.1f} MVA')
        is_transformer=metadata['branch_evidence'][index-1].get('kind')=='transformer'
        color='#ba8545' if is_transformer else '#8295a7'
        dash='5 3' if is_transformer else 'none'
        body.append(f'<line stroke-dasharray="{dash}" x1="{x}" y1="{y}" x2="{xx}" y2="{yy}" stroke="{color}" stroke-width="1.8" tabindex="0"><title>{label}</title></line>')
    for row in case['bus']:
        n=int(row[0]);kind=int(row[1]);x,y=points[n]
        color={1:'#536a81',2:'#318579',3:'#ba8545'}[kind]
        label=html.escape(f'Bus {n} | '+{1:'PQ',2:'PV',3:'Slack'}[kind]+f' | {row[9]:g} kV | P={row[2]:.2f} MW | Q={row[3]:.2f} Mvar')
        if kind==3:shape=f'<rect x="{x-6}" y="{y-6}" width="12" height="12"'
        elif kind==2:shape=f'<path d="M {x} {y-7} L {x+7} {y+6} L {x-7} {y+6} Z"'
        else:shape=f'<circle cx="{x}" cy="{y}" r="4.5"'
        body.append(shape+f' fill="{color}" stroke="white" stroke-width="1.2" tabindex="0"><title>{label}</title>'+('</rect>' if kind==3 else '</path>' if kind==2 else '</circle>'))
        if len(points)<=40:body.append(f'<text x="{x+9}" y="{y-7}" font-family="Arial, sans-serif" font-size="11" fill="#45566b">{n}</text>')
    metrics=checks.get('metrics',{})
    voltage=metrics.get('min_voltage_pu');voltage_text=f'{voltage:.4f} pu' if voltage is not None else '—'
    voltage_label='/'.join(f'{v:g}' for v in sorted(set(case['bus'][:,9]),reverse=True))
    return svg_page('Transmission network',f"{voltage_label} kV · Balanced positive-sequence model · Synthetic spatial coordinates",''.join(body),
        [('Slack bus (square)','#ba8545'),('PV bus (triangle)','#318579'),('PQ bus (circle)','#536a81'),('Lines','#8295a7'),('Transformer (dashed)','#ba8545')],
        [('Buses',len(points)),('Generators',len(case['gen'])),('Minimum voltage',voltage_text),('Acceptance','Passed' if checks['accepted'] else 'Failed')],
        notes='Symbols indicate bus type, not voltage level. Synthetic coordinates are shown at equal scale, not as GIS data. Metrics come from AC power-flow validation.')


def read_transmission_result(root):
    root=Path(root).resolve();out=json.loads((root/'result.json').read_text())
    if digest({k:v for k,v in out.items() if k!='result_hash'})!=out['result_hash']:raise ValueError('Transmission result hash changed')
    for name,value in out['artifacts'].items():
        p=(root/name).resolve()
        if not p.is_relative_to(root) or not p.is_file() or file_digest(p)!=value:raise ValueError('Transmission artifact changed')
    return out


def run_transmission(spec,workspace,run_id,*,agent_feedback=False,feedback_model=None,feedback_rounds=4,request='',_initial=None,use_memory=True):
    spec=TransmissionSpec.model_validate(spec.model_dump() if isinstance(spec,TransmissionSpec) else spec)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',run_id):raise ValueError('Invalid experiment ID')
    root=Path(workspace).resolve()/'transmission_experiments'/run_id;root.mkdir(parents=True,exist_ok=True)
    if not 0<=feedback_rounds<=12:raise ValueError('Feedback rounds must be 0–12')
    if _initial is not None and spec.count!=1:raise ValueError('Revision requires one sample')
    dependencies()
    from .rules import data
    manifest=dict(spec=spec.model_dump(),parent=_initial[2] if _initial is not None else None,source_hash=digest({p.name:file_digest(p) for p in sorted(Path(__file__).parent.glob('transmission*.py'))}),feedback=dict(enabled=agent_feedback,rounds=feedback_rounds,request=request,model=getattr(feedback_model,'model_name',None)),parameter_catalogue_hash=digest(data('transmission_lines.json')),
        line_family_catalogue_hash=digest(data('transmission_line_families.json')),
        versions={n:importlib.metadata.version(n) for n in ['pypower','scipy','numpy']})
    manifest['feedback']['use_memory']=use_memory
    with (root/'.lock').open('w') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (root/'manifest.json').exists():
            if json.loads((root/'manifest.json').read_text())!=manifest:raise ValueError('Transmission configuration/code changed; use a new ID')
            if (root/'result.json').exists():return read_transmission_result(root)
        atomic_json(root/'manifest.json',manifest);rows=[]
        for i in range(spec.count):
            path=root/f'sample_{i:05d}';path.mkdir(exist_ok=True)
            seed=int.from_bytes(hashlib.sha256(f'{spec.seed}:{i}'.encode()).digest()[:4],'big')
            try:
                case,metadata=generate_case(spec,seed) if _initial is None else (copy.deepcopy(_initial[0]),copy.deepcopy(_initial[1]))
                if _initial is not None:seed=None
                feedback=None
                if agent_feedback:
                    from .transmission_feedback import run_feedback
                    from .transmission_memory import TransmissionMemory
                    revised=run_feedback(case,metadata,spec,path/'feedback',model=feedback_model,max_rounds=feedback_rounds,request=request,memory=TransmissionMemory(Path(workspace)/'transmission_experiences.sqlite') if use_memory else None)
                    case=revised['case'];metadata=revised['metadata'];feedback=revised['report']
                atomic_json(path/'case.json',case_payload(case));atomic_json(path/'metadata.json',metadata)
                export_case(case,path/'case_generated.m')
                (path/'case_generated.py').write_text('import json\nfrom pathlib import Path\nimport numpy as np\n\ndef case_generated():\n    case=json.loads(Path(__file__).with_name("case.json").read_text())\n    for key in ("bus","gen","branch"): case[key]=np.array(case[key],dtype=float)\n    return case\n')
                checked,solved=validate_case(case,spec,metadata);atomic_json(path/'validation.json',checked)
                if checked['converged'] and checked['checks']['finite']:atomic_json(path/'solution.json',case_payload(solved))
                (path/'visualization.html').write_text(visualization(case,metadata,checked))
                rows.append(dict(sample_id=path.name,seed=seed,**checked,feedback=feedback))
            except Exception as exc:
                row=dict(sample_id=path.name,seed=seed,accepted=False,error=f'{type(exc).__name__}: {exc}');rows.append(row);atomic_json(path/'error.json',row)
        accepted=sum(r['accepted'] for r in rows)
        voltage_label='/'.join(str(v.kv) for v in spec.voltage_layers) if spec.voltage_layers else str(spec.voltage_kv)
        report=f'Transmission research model: {voltage_label} kV; {spec.n_buses} buses; {spec.n_generators} static generators; {spec.total_mw} MW demand; {spec.topology}. Generated {spec.count} cases; {accepted} passed AC power flow and constraint checks.\n\nMATPOWER v2 models include per-case parameter provenance and assumptions. Voltage-dependent engineering parameters are converted using length and base quantities and then checked. The objective is plausible research cases, not exact replication of real-network distributions. Time series, dynamics, OPF and N-1 validation are not included.'
        report+=f'\n\nTarget constraints: {len(spec.targets)}; internal validation conditions: {len(spec.validation_conditions)}; applicable document rules: {len(spec.document_rules)}; agent feedback '+('enabled' if agent_feedback else 'disabled')+'.'
        (root/'report.md').write_text(report)
        links=''.join(f'<li><a href="{r["sample_id"]}/visualization.html">{r["sample_id"]}</a>: {r["accepted"]} · <a href="{r["sample_id"]}/case_generated.m">MATPOWER</a></li>' for r in rows if 'error' not in r)
        (root/'index.html').write_text('<meta charset="utf-8"><h1>Transmission research models</h1><p>'+html.escape(report)+'</p><ul>'+links+'</ul>')
        with zipfile.ZipFile(root/'dataset.zip','w',zipfile.ZIP_DEFLATED) as z:
            for p in sorted(root.rglob('*')):
                if p.is_file() and p.name not in ('.lock','dataset.zip','result.json'):z.write(p,p.relative_to(root))
        out=dict(experiment_id=run_id,directory=str(root),attempted=spec.count,accepted=accepted,failed_or_unaccepted=spec.count-accepted,
            samples=rows,verified_report=report,artifacts={str(p.relative_to(root)):file_digest(p) for p in root.rglob('*') if p.is_file() and p.name not in ('.lock','result.json')})
        out['result_hash']=digest(out);atomic_json(root/'result.json',out)
        return out


def transmission_capabilities():
    return dict(connectivity=['connected','bridgeless'],radial_periphery='Optional exact radial_bus_count; same-voltage core attachment; no AC N-1 guarantee',schema=TransmissionSpec.model_json_schema(),network='balanced 50Hz single/multi-voltage AC',
        topology=['regional spatial mesh (default)','elongated corridor mesh','explicit historical ring/chords','local/mixed/long-distance additional corridors','explicit inter-layer transformers'],
        feedback=['AC violation witnesses','LLM tool selection','bounded device/dispatch/tap/shunt repair','opt-in same-voltage local corridor relocation','rollback','fresh AC replay','verified experience'],
        equipment='Joint voltage-compatible overhead-line R/X/C/rating families selected using provisional DC transfer and length; published 110/220 kV types, declared 330/500/750 kV engineering priors; AC checks remain authoritative',
        document_rules='Explicitly applicable project rules: voltage, line loading, maximum/total corridor length; scope mismatches rejected',
        inverse='Static metric targets and optional internal load-scale validation conditions on shared equipment; bounded joint repair, no optimality claim',
        revisions='Preserve saved network, loads and locations; revise targets and permitted repair tools in a new run',
        excluded=['time-series data generation','dynamic models','OPF','N-1 certification','unbalanced transmission'])


def revise_transmission(workspace,parent_id,revision_id,request='',sample_index=0,targets=None,allowed_repairs=None,allow_topology_changes=False,model=None,max_rounds=6):
    if not all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',v) for v in (parent_id,revision_id)) or parent_id==revision_id:raise ValueError('Use a distinct valid revision ID')
    root=Path(workspace).resolve()/'transmission_experiments'/parent_id
    parent=read_transmission_result(root)
    if not 0<=sample_index<parent['attempted']:raise ValueError('Invalid sample index')
    spec=historical_transmission_spec(json.loads((root/'manifest.json').read_text())['spec'])
    values=spec.model_dump();values['count']=1;values['allow_topology_changes']=allow_topology_changes
    if targets is not None:values['targets']=targets
    if allowed_repairs is not None:values['allowed_repairs']=allowed_repairs
    spec=TransmissionSpec(**values);folder=root/f'sample_{sample_index:05d}'
    case=json.loads((folder/'case.json').read_text())
    for key in ('bus','gen','branch'):case[key]=np.asarray(case[key],dtype=float)
    metadata=json.loads((folder/'metadata.json').read_text())
    lineage=dict(parent_id=parent_id,parent_result_hash=parent['result_hash'],sample_index=sample_index,request=request)
    return run_transmission(spec,workspace,revision_id,agent_feedback=True,feedback_model=model,feedback_rounds=max_rounds,request=request,_initial=(case,metadata,lineage))
