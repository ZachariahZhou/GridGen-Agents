"""Equivalent research-case planning using the existing single-voltage engine."""


def equivalent_plan(spec, ledger, assumptions, supplied):
    """Track actual source bindings; a planner-supplied default is not a user value."""
    from .planning import build_plan
    from .requirement_contract import canonical

    plan=build_plan(ledger.summary,spec.model_dump())
    hard={}
    for item in ledger.requirements:
        if item.priority!='hard' or item.disposition!='supported':continue
        key=canonical(item.target_field or '')
        keys={'phase_model':['phase_design.mode'], 'scene':['scenario.kind'],
              'tie_count':['scenario.tie_count'], 'total_kw':['total_kw_min','total_kw_max'],
              'n_loads':['n_loads_min','n_loads_max']}.get(key,[key])
        for key in keys:hard.setdefault(key,[]).append(item.model_dump())

    formats=['opendss']
    for item in ledger.requirements:
        if canonical(item.target_field or '')=='deliverables' and item.disposition=='supported' and item.operator in ('eq','contains'):
            requested=item.expected_value if isinstance(item.expected_value,list) else [item.expected_value]
            if 'matpower' in requested and 'matpower' not in formats:formats.append('matpower')
    plan=type(plan).model_validate({**plan.model_dump(),'export_formats':formats})
    records={}
    def walk(values,explicit,prefix=''):
        for name,value in values.items():
            field=prefix+name
            bindings=[item for key,items in hard.items() if field==key or field.startswith(key+'.') for item in items]
            fixed=any(item['operator']=='eq' for item in bindings)
            origin='user' if fixed else 'inferred' if name in explicit else 'default'
            records[field]=dict(value=value,origin=origin,mutable=not fixed,requirements=bindings,
                scope='replanning; execution repairs remain bounded by repair_policy and validation')
            plan.parameter_origins[field]=origin
            if isinstance(value,dict):walk(value,explicit.get(name,{}) or {},field+'.')
    walk(spec.model_dump(),supplied)
    plan.assumptions.extend(assumptions)
    contract=dict(version='m68',representation='equivalent_bus_branch',node_count_semantics='electrical_buses_including_source',
        load_count_semantics='positive_load_sites_with_explicit_injection_semantics',customer_count=None,
        parameters=records,user_requirements=ledger.model_dump(),
        scope='Single-voltage research model; no explicit customer or transformer expansion. Synthetic coordinates remain part of the existing generator.')
    evidence=[dict(field=k,value=r['value'],origin=r['origin'],evidence='; '.join(i['evidence'] for i in r['requirements']),
        reason='Explicit user constraint' if r['requirements'] else 'Inferred or research default; replanning may adjust it while preserving the user contract')
        for k,r in records.items() if r['origin']!='default' and not isinstance(r['value'],dict)]
    return plan,contract,evidence


def single_voltage_observations(network, validation=None):
    """Observe electrical objects, never infer customer count from aggregate load sites."""
    import math
    import networkx as nx
    graph=nx.Graph()
    graph.add_nodes_from(b.id for b in network.buses)
    graph.add_edges_from((e.bus1,e.bus2) for e in network.lines)
    p=sum(l.kw for l in network.loads);q=sum(l.kvar for l in network.loads)
    phase_model=None
    if network.phase_mode=='balanced' and all(set(b.phases)=={1,2,3} for b in network.buses):
        phase_model='balanced'
    elif network.phase_mode=='unbalanced' and network.loads and all(
        {v.phase for v in load.phase_powers}==set(load.phases) and
        math.isclose(sum(v.kw for v in load.phase_powers),load.kw,abs_tol=1e-8)
        for load in network.loads):
        phase_model='unbalanced'
    reference={}
    placement=network.design_evidence.get('load_placement',{})
    if placement.get('method')=='reference_conditioned_empirical':
        from .load_placement import reference_occupancy
        try:
            source=reference_occupancy(placement['case_id'])
            targets=placement['target_nodes']
            selected={r['bus_id']:r['normalized_kw'] for r in targets if r.get('selected')}
            actual={load.bus:load.kw for load in network.loads}
            valid=(placement['reference_hash']==source['reference_hash'] and placement['reference_nodes']==source['nodes']
                and {r['bus_id'] for r in targets}==set(graph)-{network.source_bus}
                and len(targets)==len(graph)-1 and selected.keys()==actual.keys()
                and all(math.isclose(value,actual[bus],rel_tol=1e-9,abs_tol=1e-8) for bus,value in selected.items()))
            if valid:reference={'scenario.reference_case_id':source['case_id'],'scenario.load_placement':'reference_conditioned'}
        except (ValueError,KeyError,TypeError):pass
    elif {l.bus for l in network.loads}==set(graph)-{network.source_bus}:
        reference['scenario.load_placement']='all_nodes'
    semantics=network.design_evidence.get('load_semantics')
    if semantics=='aggregated' and (not network.loads or any(load.category!='aggregated' for load in network.loads)):semantics=None
    return dict(**reference,load_semantics=semantics,network_kind='distribution',model_family='single_voltage',phase_model=phase_model,
        n_buses=len(network.buses),n_loads=len(network.loads),n_loads_min=len(network.loads),n_loads_max=len(network.loads),
        zero_load_buses=len({b.id for b in network.buses}-{l.bus for l in network.loads}),
        voltage_kv=network.voltage_kv,frequency_hz=network.frequency_hz,total_kw=p,total_kw_min=p,total_kw_max=p,
        power_factor=p/math.hypot(p,q) if p or q else None,pv_ratio=sum(l.pv_kw for l in network.loads)/p if p else None,
        tie_count=len(network.tie_lines),source_count=int(network.source_bus in graph),
        operating_topology='radial' if len(graph) and nx.is_tree(graph) and len(network.lines)==len(graph)-1 else None,
        physical_cycle_rank=len(network.lines)+len(network.tie_lines)-len(network.buses)+1)


def single_voltage_design_observations(network, spec):
    """Bind design controls to a matching manifest and observable network facts.

    Layout/mode are execution provenance. Topology and explicit load-shape
    controls additionally need their existing structural/physical validators.
    This is not a geographic or population-realism certificate.
    """
    from .topologies import topology_matches
    from .load_shapes import load_shape_matches
    from .settlements import hierarchy_matches
    from .scene_profiles import scene_contract_matches
    out={'mode':spec.mode,'scenario.layout':spec.scenario.layout}
    lengths=[line.length_km for line in network.lines+network.tie_lines]
    if lengths:out.update(segment_km_min=min(lengths),segment_km_max=max(lengths))
    if scene_contract_matches(network,spec):
        out['scenario.engineering_profile']=spec.scenario.engineering_profile
    if spec.scenario.layout=='structured_radial' and topology_matches(network,spec):
        cfg=spec.scenario.topology
        out['scenario.topology']=cfg.model_dump()
        out.update({'scenario.topology.'+k:v for k,v in cfg.model_dump().items() if v is not None})
    if load_shape_matches(network,spec):
        out['scenario.load_shape']=spec.scenario.load_shape
        out['scenario.load_concentration']=spec.scenario.load_concentration
    if spec.scenario.layout=='rural_villages' and hierarchy_matches(network,spec):
        out['scenario.village_count']=len(network.design_evidence.get('villages',[]))
    return out
