"""Semantic observations shared by planning, delivered-model audit and paper scoring.

Capabilities describe model representations, not a guarantee of numerical imbalance.
Only explicitly observed quantities can satisfy a delivered-model requirement.
"""
import math
from collections import Counter

ALIASES={
    'capability.phase_model':'phase_model','phase_design.mode':'phase_model',
    'hierarchy.voltage_kv':'mv_voltage_kv','hierarchy.transformer_count':'transformers',
    'transformer_count':'transformers','hierarchy.lv_installation':'installations',
    'transmission.transformer_count':'transformers',
    'lv_installation':'installations','lv_installations':'installations',
    'scenario.tie_count':'tie_count',
    'scenario.kind':'scene',
    'hierarchy.lv_voltage_kv':'lv_voltage_kv',
    'semantic_capabilities.transmission.deliverables':'deliverables',
    'semantic_capabilities.hierarchical.deliverables':'deliverables',
    'capability.deliverables':'deliverables',
}
CAPABILITIES={
    'hierarchical':{'network_kind':'distribution','phase_model':'unbalanced','frequency_hz':50,
        'deliverables':['opendss'],
        'implementation':'explicit ABC MV/LV branches, three-phase transformers and single-phase customers; no separate phase_design switch',
        'unsupported':['neutral displacement','time-series generation','protection certification']},
    'single_voltage':{'network_kind':'distribution','phase_model':'phase_design.mode; unbalanced only at 6/10/20kV','deliverables':['opendss','matpower'],
        'format_scope':'MATPOWER is opt-in, balanced only with symmetric equipment; fixes source-terminal voltage and verifies AC agreement. No implicit unbalanced reduction.',
        'implementation':'Equivalent single-voltage buses with aggregate loads; optional phase-resolved injections, zero-load junctions and normally-open ties'},
    'transmission':{'network_kind':'transmission','phase_model':'balanced',
        'deliverables':['matpower'],
        'implementation':'balanced positive-sequence AC; voltage_layers create explicit inter-layer transformers'},
}
PERFORMANCE_BOUNDS={
    **{metric+'_'+bound:(metric+'_'+bound,op) for metric in ('total_kw','n_loads','segment_km') for bound,op in (('min','ge'),('max','le'))},
    **{prefix+'structure_targets.'+metric:('structure_targets.'+metric,'le')
       for prefix in ('','hierarchy.') for metric in ('max_mv_depth','max_tap_distance_hops')},
    **{f'{prefix}{level}_voltage_{bound}_pu':(f'{level}_{bound}_voltage_pu',op)
       for prefix in ('','hierarchy.') for level in ('mv','lv') for bound,op in (('min','ge'),('max','le'))},
    'transmission.voltage_min_pu':('min_voltage_pu','ge'),
    'transmission.voltage_max_pu':('max_voltage_pu','le'),
}


def canonical(field):
    if field in ALIASES:return ALIASES[field]
    # Capability labels are sometimes copied with the context namespace.
    # Only two observable properties accept these known namespace variants;
    # their values must still pass the actual family/model/file checks.
    namespace,_,leaf=field.rpartition('.')
    if namespace in ('capability','hierarchical','distribution',
                     'semantic_capabilities.hierarchical','semantic_capabilities.single_voltage','semantic_capabilities.transmission') and leaf in ('phase_model','deliverables','frequency_hz'):
        return leaf
    return field.split('.',1)[1] if field.startswith(('hierarchy.','transmission.')) else field


def compare(actual,expected,op='eq',tolerance=1e-6):
    if actual is None or expected is None:return False
    if isinstance(actual,bool) or isinstance(expected,bool):return op=='eq' and type(actual)==type(expected) and actual==expected
    if isinstance(actual,(int,float)) and isinstance(expected,(int,float)):
        if not math.isfinite(actual) or not math.isfinite(expected):return False
        return abs(actual-expected)<=tolerance if op=='eq' else abs(actual-expected)>tolerance if op=='excludes' else actual>=expected-tolerance if op=='ge' else actual<=expected+tolerance if op=='le' else False
    if isinstance(actual,list):
        values=expected if isinstance(expected,list) else [expected]
        if not actual:return False
        if all(isinstance(v,str) for v in actual+values):
            a,b=set(actual),set(values)
            return a==b if op=='eq' else b<=a if op=='contains' else not a&b if op=='excludes' else False
        return op=='eq' and len(actual)==len(values) and all(compare(a,b,tolerance=tolerance) for a,b in zip(actual,values))
    if isinstance(actual,dict) and isinstance(expected,dict):
        return op=='eq' and actual.keys()==expected.keys() and all(compare(actual[k],expected[k],tolerance=tolerance) for k in actual)
    return op=='eq' and actual==expected


def _compare_voltage_layers(actual,expected,op):
    """Voltage/count mappings have set semantics, unlike ordered model arrays."""
    for layers in (actual,expected):
        if not isinstance(layers,list) or not layers:return False
        seen=[]
        for layer in layers:
            if not isinstance(layer,dict) or set(layer)!={'kv','buses'}:return False
            kv,n=layer['kv'],layer['buses']
            if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<=0 for v in (kv,n)):
                return False
            if int(n)!=n or any(compare(kv,previous) for previous in seen):return False
            seen.append(kv)
    matches=[any(compare(a,b) for a in actual) for b in expected]
    if op=='eq':return len(actual)==len(expected) and all(matches)
    if op=='contains':return all(matches)
    if op=='excludes':return not any(matches)
    return False


def _indexed_voltage_layers(actual,order=None):
    """Index selects a nominal voltage; counts always come from actual layers.

    A delivery's declared order identifies which voltage an index means. Missing
    layers stay missing instead of moving another layer into the vacant index.
    """
    if not _compare_voltage_layers(actual,actual,'eq'):return {}
    if order is None:order=actual
    if not _compare_voltage_layers(order,order,'eq'):return {}
    observed={}
    for index,selector in enumerate(order):
        matches=[layer for layer in actual if compare(layer['kv'],selector['kv'])]
        if len(matches)==1:
            observed.update({f'voltage_layers[{index}].{key}':matches[0][key] for key in ('kv','buses')})
    return observed


def plan_observations(brief):
    family=brief['plan_type'];plan=brief['plan'];spec=plan.get('spec',plan.get('base_spec',{}))
    observed={'model_family':'hierarchical' if family in ('hierarchical','hierarchical_inverse') else 'transmission' if family=='transmission' else 'single_voltage'}
    def flatten(value,prefix=''):
        for key,item in value.items():
            name=prefix+key;observed[name]=item
            if isinstance(item,dict):flatten(item,name+'.')
    flatten(spec)
    if family=='transmission':
        observed['mesh_family']=('ring_chords' if spec.get('topology')=='ring' else 'regional') if spec.get('mesh_family','auto')=='auto' else spec['mesh_family']
    if family in ('hierarchical','hierarchical_inverse'):
        from .hierarchy import historical_hierarchy_spec
        from .lv_equipment import resolve_installation
        s=historical_hierarchy_spec(spec)
        observed.update(network_kind='distribution',phase_model='unbalanced',frequency_hz=50,mv_voltage_kv=s.voltage_kv,
            transformers=s.transformer_count,lv_branches=[s.lv_branches],
            installations=sorted({r.decision.selected for r in s.lv_regions}) if s.lv_regions else [resolve_installation(s)[1]])
        from .hierarchy_topology_design import resolve_mv_topology,resolve_lv_topology,resolved_blueprint
        topology=resolve_mv_topology(s)
        active_edges,_,_,_=resolved_blueprint(s,s.seed)
        degrees=Counter(node for edge in active_edges for node in edge)
        observed['mv_terminal_count']=sum(node!=0 and degree==1 for node,degree in degrees.items())
        observed.update({'mv_topology.'+k:v for k,v in topology.model_dump().items() if v is not None})
        cycles=(topology.local_tie_count if topology.family=='branched_network' else
                topology.ring_count if topology.family=='multi_open_ring' else int(topology.family in ('open_ring','ring_laterals')))
        observed.update(mv_tie_count=cycles,mv_physical_cycle_rank=cycles,mv_operating_topology='radial',mv_source_count=1)
        observed['lv_topology']=resolve_lv_topology(s)
    elif family=='transmission':
        observed.update(network_kind='transmission',phase_model='balanced',
            transformers=2*max(0,len(spec.get('voltage_layers') or [])-1),
            voltage_levels=[str(v['kv']) for v in spec.get('voltage_layers') or []] or [str(spec.get('voltage_kv'))])
        if not spec.get('voltage_layers') and {'voltage_kv','n_buses'}<=set(spec):
            observed['voltage_layers']=[dict(kv=spec['voltage_kv'],buses=spec['n_buses'])]
        observed.update(_indexed_voltage_layers(observed.get('voltage_layers')))
    else:
        observed.update(network_kind='distribution',phase_model=spec.get('phase_design',{}).get('mode','balanced'))
        if 'scenario' in spec:observed['scene']=spec['scenario'].get('kind')
        observed.update(operating_topology='radial',source_count=1,tie_count=spec.get('scenario',{}).get('tie_count',0))
        for metric in ('total_kw','n_loads'):
            if spec.get(metric+'_min')==spec.get(metric+'_max'):observed[metric]=spec.get(metric+'_min')
    observed['deliverables']=CAPABILITIES.get(observed['model_family'],{}).get('deliverables',[])
    if family=='feeder':observed['deliverables']=plan.get('export_formats',['opendss'])
    return observed


def normalize_deliverable_requirement(item):
    """Normalize only explicit supported format names, never arbitrary outputs."""
    import re
    if item.target_field not in (None, 'deliverables', 'output_format', 'export_format'):
        return
    if not re.search('\u8f93\u51fa|\u4ea4\u4ed8|\u5bfc\u51fa|\u63d0\u4f9b|export|deliver|output', item.evidence, re.I):
        return
    formats = re.findall(r'(?<![A-Za-z])(?:OpenDSS|MATPOWER)(?![A-Za-z])', item.evidence, re.I)
    if not formats:
        return
    directive = re.split('\u8f93\u51fa|\u4ea4\u4ed8|\u5bfc\u51fa|\u63d0\u4f9b|export|deliver|output', item.evidence, flags=re.I)[-1]
    remainder = re.sub('OpenDSS|MATPOWER|\u53ef\u76f4\u63a5\u8c03\u7528|\u53ef\u8fd0\u884c|\u53ef\u8c03\u7528|\u6a21\u578b|\u6587\u4ef6|\u683c\u5f0f|\u4ee5\u53ca|\u5e76\u4e14|\u548c|\u4e0e|\u53ca|\u7684|model|files?|format|and|runnable', '', directive, flags=re.I)
    if re.sub(r'[\s，,。；;、/。:：+&()（）-]', '', remainder):
        return  # A compound/unknown output requirement needs explicit interpretation.
    values = item.expected_value if isinstance(item.expected_value,list) else [item.expected_value]
    aliases = {'opendss','opendss model','opendss_model','opendss\u6a21\u578b','matpower','matpower model','matpower_model','matpower\u6a21\u578b'}
    if any(v is not None and (not isinstance(v,str) or v.lower() not in aliases) for v in values):
        return  # Do not erase additional unsupported deliverables.
    if item.operator not in ('eq','contains'):
        return
    item.target_field='deliverables'
    item.expected_value=sorted({v.lower() for v in formats})
    item.operator='contains'


def delivered_formats(folder, brief, network):
    """Use actual export reloads, not capability claims, as delivery evidence."""
    import numpy as np
    try:
        if brief['plan_type']=='hierarchical':
            from .simulation import simulate
            from .hierarchy import historical_hierarchy_spec,evaluate_hierarchy
            master=folder/'opendss'/'Master.dss'
            if not master.is_file():return []
            simulation=simulate(master)
            spec=historical_hierarchy_spec(brief['plan']['spec'])
            return ['opendss'] if evaluate_hierarchy(network,spec,simulation)['accepted'] else []
        if brief['plan_type']=='feeder':
            from tempfile import TemporaryDirectory
            from pathlib import Path
            from .simulation import simulate,export_dss
            from .schemas import ExperimentSpec
            from .evaluation import evaluate,verdict
            master=folder/'opendss'/'Master.dss'
            if not master.is_file():return []
            # Exact deterministic export parity binds the runnable file to this model,
            # including ties and phase powers. A different runnable case cannot pass.
            with TemporaryDirectory() as tmp:
                export_dss(network,Path(tmp))
                for expected in Path(tmp).glob('*.dss'):
                    delivered=folder/'opendss'/expected.name
                    if not delivered.is_file() or delivered.read_bytes()!=expected.read_bytes():return []
            spec=ExperimentSpec.model_validate(brief['plan']['spec'])
            simulation=simulate(master)
            formats=['opendss'] if verdict(evaluate(network,spec,simulation),spec.mode)['accepted'] else []
            if 'matpower' in brief['plan'].get('export_formats',[]) and 'opendss' in formats:
                from .distribution_matpower import verify_distribution_matpower
                try:
                    if verify_distribution_matpower(folder/'matpower',network,spec,simulation).get('verified'):formats.append('matpower')
                except (OSError,ValueError,KeyError,ImportError):pass
            return formats
        if brief['plan_type']=='transmission':
            from .matpower import parse_matpower
            parsed=parse_matpower(folder/'case_generated.m')
            equal=all(np.asarray(parsed[k]).shape==network[k].shape and np.allclose(parsed[k],network[k],rtol=1e-8,atol=1e-8) for k in ('bus','gen','branch'))
            return ['matpower'] if equal and compare(parsed['base_mva'],network['baseMVA']) else []
    except Exception:
        return []  # Missing, corrupt or non-runnable export never satisfies a contract.
    return []


def transmission_topology(network):
    """Observe each nominal-voltage layer; inter-layer transformers are separate.

    A multilayer ring means a ring within each layer, not a single global cycle.
    Disconnected or non-meshed structures are never certified by metadata labels.
    """
    import networkx as nx
    bus=network['bus'];branch=network['branch']
    graph=nx.Graph();graph.add_nodes_from(int(b[0]) for b in bus)
    graph.add_edges_from((int(e[0]),int(e[1])) for e in branch if e[10]>0)
    if not graph or not nx.is_connected(graph) or nx.number_of_selfloops(graph):return None
    layers=[graph.subgraph(int(b[0]) for b in bus if b[9]==kv) for kv in set(bus[:,9])]
    if any(len(g)<3 or not nx.is_connected(g) or g.number_of_edges()<len(g) for g in layers):return None
    return 'ring' if all(all(d==2 for _,d in g.degree()) for g in layers) else 'meshed'


def model_observations(domain,network,metadata=None,validation=None,*,voltage_layer_order=None):
    if domain=='single_voltage':
        from .research_case import single_voltage_observations
        return single_voltage_observations(network,validation)
    if domain=='distribution':
        from .benchmark.evaluate import observed_requirements
        out=observed_requirements(network)
        for key in ('mv_voltage_kv','lv_voltage_kv'):out[key]=out[key][0] if len(out[key])==1 else out[key]
        out['installations']=out.pop('lv_installations')
        # Representation evidence, not a claim that every operating point has nonzero VUF.
        abc=any(set(b.phases)=={1,2,3} for b in network.buses if b.voltage_kv>=1)
        phase_resolved=bool(network.loads) and all(len(l.phases)==1 and len(l.phase_powers)==1 and l.phase_powers[0].phase==l.phases[0] for l in network.loads)
        if abc and network.transformers and phase_resolved:out['phase_model']='unbalanced'
        out.update(network_kind='distribution',model_family='hierarchical',frequency_hz=network.frequency_hz)
        if network.design_evidence.get('topology_policy_version'):
            from .hierarchy_topology_design import topology_observations
            out.update(topology_observations(network))
        # This is configuration provenance, not independent geographic realism evidence.
        out['scene']=network.design_evidence.get('spec',{}).get('scene')
        out['lv_installation_decision']=network.design_evidence.get('spec',{}).get('lv_installation_decision')
        p=sum(l.kw for l in network.loads);q=sum(l.kvar for l in network.loads)
        out['power_factor']=p/math.hypot(p,q) if p or q else None
        out.update({k:v for k,v in (validation or {}).items() if k in ('min_voltage_pu','max_vuf_percent',
            'mv_min_voltage_pu','mv_max_voltage_pu','lv_min_voltage_pu','lv_max_voltage_pu')})
        return out
    bus=network['bus'];gen=network['gen'];metadata=metadata or {}
    from .transmission_topology import observed_family
    # Generator contract: bus 1 belongs to the first configured voltage layer.
    # A layer's nominal voltage is distinct from a uniform whole-network value.
    primary=bus[bus[:,0]==1]
    # BUS_I encodes the generator's layer order, independently of array row order.
    layers=[dict(kv=float(k),buses=n) for k,n in Counter(row[9] for row in sorted(bus,key=lambda row:row[0])).items()]
    import networkx as nx
    graph=nx.Graph();graph.add_nodes_from(int(b[0]) for b in bus)
    graph.add_edges_from((int(e[0]),int(e[1])) for e in network['branch'] if e[10]>0)
    connectivity=('bridgeless' if not list(nx.bridges(graph)) else 'connected') if nx.is_connected(graph) else None
    return dict(network_kind='transmission',model_family='transmission',phase_model='balanced',n_buses=len(bus),n_generators=len(gen),
        total_mw=float(sum(bus[:,2])),voltage_kv=float(bus[0,9]) if len(set(bus[:,9]))==1 else None,
        primary_voltage_kv=float(primary[0,9]) if len(primary)==1 else None,
        voltage_levels=sorted({f'{v:g}' for v in bus[:,9]}),topology=transmission_topology(network),
        voltage_layers=sorted(layers,key=lambda layer:layer['kv'],reverse=True),
        generator_types=sorted(set(metadata.get('generator_types',[]))),mesh_family=observed_family(network,metadata),
        connectivity=connectivity,radial_bus_count=sum(d==1 for _,d in graph.degree()),
        transformers=sum(e['kind']=='transformer' for e in metadata.get('branch_evidence',[])),
        **(validation or {}).get('metrics',{}),**_indexed_voltage_layers(layers,voltage_layer_order))



def _explained_installation_decision(value):
    """A choice with a written rationale, independent of the chosen method."""
    return (isinstance(value,dict) and value.get('selected') in
            ('aerial_bundle','buried_direct','buried_duct') and
            isinstance(value.get('reason'),str) and bool(value['reason'].strip()))

def audit_ledger(ledger,observed,stage='model'):
    checks=[]
    for item in ledger.requirements:
        if item.priority!='hard':continue
        key=canonical(item.target_field or '')
        op=item.operator
        if stage=='model' and item.target_field in PERFORMANCE_BOUNDS:
            key,default_op=PERFORMANCE_BOUNDS[item.target_field]
            if op=='eq':op=default_op
        if stage=='model' and item.target_field=='transmission.voltage_kv':key='primary_voltage_kv'
        actual=observed.get(key)
        expected=item.expected_value
        if key=='lv_installation_decision' and expected is True:
            status='passed' if _explained_installation_decision(actual) else 'failed'
        elif not key or item.expected_value is None:
            status='unverified'
        elif actual is None:status='unverified'
        elif key=='connectivity' and expected=='connected' and op=='eq':status='passed' if actual in ('connected','bridgeless') else 'failed'
        elif key=='voltage_layers':status='passed' if _compare_voltage_layers(actual,expected,op) else 'failed'
        else:status='passed' if compare(actual,expected,op) else 'failed'
        checks.append(dict(id=item.id,field=item.target_field,semantic_field=key,expected=item.expected_value,
            observed=actual,operator=op,status=status,evidence=item.evidence,
            evidence_kind='reference_provenance' if key=='scenario.reference_case_id' else 'configuration' if key in ('scene','load_semantics','mode','scenario.layout','scenario.engineering_profile','mesh_family') else 'replayed_execution_policy' if key in ('allowed_repairs','allow_topology_changes') and stage=='model' else stage))
    return dict(stage=stage,checks=checks,all_satisfied=bool(checks) and all(c['status']=='passed' for c in checks),
        failed=sum(c['status']=='failed' for c in checks),unverified=sum(c['status']=='unverified' for c in checks))


def transmission_execution_observations(folder,brief,network):
    """Permissions are execution facts, not electrical attributes of a graph.

    Require a matching saved contract, valid action replay, and equality of the
    replayed winner to the actual delivered case. A policy label alone is not
    evidence that an unchanged-topology requirement was honored.
    """
    import json
    import numpy as np
    from .artifacts import digest
    from .transmission_feedback import verify_saved_feedback,load_candidate
    root=folder/'feedback'
    contract=json.loads((root/'contract.json').read_text())
    if digest(contract['spec'])!=digest(brief['plan']['spec']):return {}
    report=verify_saved_feedback(root)
    selected,_=load_candidate(root,report['selected_candidate'])
    if not all(selected[k].shape==network[k].shape and np.allclose(selected[k],network[k],rtol=1e-10,atol=1e-10) for k in ('bus','gen','branch')):return {}
    policy=contract['spec'];original,_=load_candidate(root,'candidate_0000')
    old_edges={tuple(sorted(map(int,e[:2]))) for e in original['branch'] if e[10]>0}
    new_edges={tuple(sorted(map(int,e[:2]))) for e in network['branch'] if e[10]>0}
    if not policy['allow_topology_changes'] and old_edges!=new_edges:return {}
    return dict(allowed_repairs=policy['allowed_repairs'],allow_topology_changes=policy['allow_topology_changes'])


def _transmission_batch_observations(brief,output):
    """Bind count/seed to the verified transmission run, including revisions."""
    import hashlib
    import json
    import re
    import numpy as np
    from pathlib import Path
    from .transmission import historical_transmission_spec,read_transmission_result
    observed=dict(count=0,_batch_integrity=False)
    try:
        root=Path(output['directory']);rows=output.get('samples',[])
        saved=read_transmission_result(root)
        manifest=json.loads((root/'manifest.json').read_text())
        spec=historical_transmission_spec(manifest['spec'])
        planned=historical_transmission_spec(brief['plan']['spec'])
        if spec.model_dump()!=planned.model_dump():return observed
        models=set()
        for row in rows:
            name=row.get('sample_id','')
            if not isinstance(name,str) or not re.fullmatch(r'sample_[0-9]{5}',name):continue
            try:
                case=json.loads((root/name/'case.json').read_text())
                arrays=[np.asarray(case[k],dtype=float) for k in ('bus','gen','branch')]
                if all(a.ndim==2 and a.shape[0]>0 and a.shape[1]>=width and np.isfinite(a).all()
                       for a,width in zip(arrays,(13,21,13))):models.add(name)
            except (OSError,ValueError,KeyError,TypeError):pass
        observed['count']=len(models)
        expected_ids={f'sample_{i:05d}' for i in range(spec.count)}
        if any(len(items)!=spec.count or {r['sample_id'] for r in items}!=expected_ids
               for items in (rows,saved['samples'])):return observed
        if output.get('attempted')!=spec.count or saved.get('attempted')!=spec.count:return observed
        saved_rows={r['sample_id']:r for r in saved['samples']}
        revision=manifest.get('parent') is not None
        if revision and spec.count!=1:return observed
        for row in rows:
            index=int(row['sample_id'].split('_')[1])
            expected=None if revision else int.from_bytes(hashlib.sha256(f'{spec.seed}:{index}'.encode()).digest()[:4],'big')
            recorded=saved_rows[row['sample_id']]
            if row.get('seed')!=expected or recorded.get('seed')!=expected:return observed
            if row.get('accepted')!=recorded.get('accepted'):return observed
            if row.get('accepted') and row['sample_id'] not in models:return observed
        observed['_batch_integrity']=True
        # A revision retains the parent's model; its seed is not a new draw.
        if not revision and len(models)==spec.count:observed['seed']=spec.seed
    except (OSError,ValueError,KeyError,TypeError):pass
    return observed


def batch_execution_observations(brief,output):
    """Batch size from readable artifacts; root seed from verified derivation.

    A root RNG seed is provenance, not a per-feeder graph property. The plan alone
    cannot certify it. Missing/duplicate models or changed derived seeds fail shut.
    """
    if brief['plan_type']=='transmission':return _transmission_batch_observations(brief,output)
    if brief['plan_type'] not in ('hierarchical','feeder'):return {}
    import hashlib
    import json
    from pathlib import Path
    from .hierarchy import HierarchicalFeeder,historical_hierarchy_spec
    from .schemas import Feeder,ExperimentSpec
    model_type=Feeder if brief['plan_type']=='feeder' else HierarchicalFeeder
    spec_type=ExperimentSpec.model_validate if brief['plan_type']=='feeder' else historical_hierarchy_spec
    root=Path(output['directory']);models={};rows=output.get('samples',[])
    for row in rows:
        name=row.get('sample_id','')
        import re
        if not re.fullmatch(r'sample_[0-9]{5}',name):continue
        try:models[name]=model_type.model_validate(json.loads((root/name/'feeder.json').read_text()))
        except (OSError,ValueError,TypeError):continue
    observed=dict(count=len(models),_batch_integrity=False)
    try:
        spec=spec_type(json.loads((root/'manifest.json').read_text())['spec'])
        planned=spec_type(brief['plan']['spec'])
        if spec.model_dump()!=planned.model_dump():return observed
        if len(rows)!=spec.count or output.get('attempted')!=spec.count:return observed
        if {row['sample_id'] for row in rows}!={f'sample_{i:05d}' for i in range(spec.count)}:return observed
        for row in rows:
            index=int(row['sample_id'].split('_')[1])
            expected=int.from_bytes(hashlib.sha256(f'{spec.seed}:{index}'.encode()).digest()[:4],'big')
            if row.get('seed')!=expected:return observed
            model=models.get(row['sample_id'])
            if model is not None and model.seed!=expected:return observed
            if row.get('accepted') and model is None:return observed
        observed['_batch_integrity']=True
        if len(models)==spec.count:observed['seed']=spec.seed
    except (OSError,ValueError,TypeError,KeyError):pass
    return observed


def audit_delivery(ledger,brief,output):
    """Read final artifacts after feedback. Never replace actual values with the plan."""
    import json
    import numpy as np
    from pathlib import Path
    root=Path(output['directory']);reports=[]
    batch_observed=batch_execution_observations(brief,output)
    batch_integrity=batch_observed.pop('_batch_integrity',None)
    for row in output.get('samples',[]):
        folder=root/row['sample_id'];observed={};electrical=False;model_accepted=False
        try:
            validation=json.loads((folder/'validation.json').read_text())
            model_accepted=bool(row.get('accepted')) and bool(validation.get('accepted'))
            electrical=model_accepted
            if brief['plan_type']=='hierarchical' and 'structure_targets' in validation.get('checks',{}):
                electrical=all(v for k,v in validation['checks'].items() if k!='structure_targets')
            if brief['plan_type']=='hierarchical':
                from .hierarchy import HierarchicalFeeder,historical_hierarchy_spec,evaluate_hierarchy
                network=HierarchicalFeeder.model_validate(json.loads((folder/'feeder.json').read_text()))
                spec=historical_hierarchy_spec(brief['plan']['spec'])
                simulation=json.loads((folder/'simulation.json').read_text())
                checked=evaluate_hierarchy(network,spec,simulation)
                model_accepted=model_accepted and checked['accepted']
                electrical=all(v for k,v in checked['checks'].items() if k!='structure_targets')
                observed=model_observations('distribution',network,validation=checked)
            elif brief['plan_type']=='feeder':
                from .schemas import Feeder,ExperimentSpec
                from .evaluation import evaluate,verdict
                from .artifacts import digest
                network=Feeder.model_validate(json.loads((folder/'feeder.json').read_text()))
                spec=ExperimentSpec.model_validate(brief['plan']['spec'])
                simulation=json.loads((folder/'simulation.json').read_text())
                checked=verdict(evaluate(network,spec,simulation),spec.mode)
                model_accepted=model_accepted and checked['accepted'] and row.get('model_hash')==digest(network.model_dump())
                electrical=checked['operational_pass']
                observed=model_observations('single_voltage',network,validation=validation)
                # Scene is configuration provenance only, and the batch manifest
                # must match this plan; it does not certify geographic realism.
                manifest=json.loads((root/'manifest.json').read_text())
                if manifest.get('spec')==brief['plan']['spec']:
                    from .research_case import single_voltage_design_observations
                    observed.update(single_voltage_design_observations(network,spec))
                    observed['scene']=spec.scenario.kind
            elif brief['plan_type']=='transmission':
                network=json.loads((folder/'case.json').read_text())
                for k in ('bus','gen','branch'):network[k]=np.asarray(network[k])
                observed=model_observations('transmission',network,json.loads((folder/'metadata.json').read_text()),validation,
                    voltage_layer_order=brief['plan']['spec'].get('voltage_layers'))
                if any(canonical(r.target_field or '') in ('allowed_repairs','allow_topology_changes') and r.priority=='hard' for r in ledger.requirements):
                    observed.update(transmission_execution_observations(folder,brief,network))
            if any(canonical(r.target_field or '')=='deliverables' and r.priority=='hard' for r in ledger.requirements):
                observed['deliverables']=delivered_formats(folder,brief,network)
        except (OSError,ValueError,KeyError,TypeError):
            model_accepted=False  # Missing evidence cannot retain a previously loaded accepted flag.
        if brief['plan_type'] in ('feeder','hierarchical','transmission') and batch_integrity is not True:model_accepted=False
        observed.update(batch_observed)
        audit=audit_ledger(ledger,observed)
        for item in audit['checks']:
            if item['semantic_field'] in ('count','seed') and item['observed'] is not None:
                item['evidence_kind']='batch_execution'
        reports.append(dict(sample_id=row['sample_id'],electrical_accepted=electrical,
            model_accepted=model_accepted,joint_accepted=model_accepted and audit['all_satisfied'],**audit))
    return dict(samples=reports,attempted=output.get('attempted',len(reports)),batch_integrity=batch_integrity,
        jointly_accepted=sum(r['joint_accepted'] for r in reports),
        all_satisfied=bool(reports) and len(reports)==output.get('attempted',len(reports)) and all(r['joint_accepted'] for r in reports),
        scope='Explicit measurable requirements; unverified semantic requirements require review. Scene is configuration provenance, not geographic validation.')
