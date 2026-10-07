"""Small, fully schema-validated repair candidates for known coupled fields."""
import copy


def representation_repairs(raw):
    from .schemas import ExperimentSpec
    from .hierarchy import HierarchicalSpec
    from .requirement_contract import canonical, compare
    from .planning_patches import _assign
    family=raw.get('family')
    prefix={'single_voltage':'single_voltage_spec','hierarchical':'distribution_spec'}.get(family)
    if prefix is None or not isinstance(raw.get(prefix),dict):return []
    spec=raw[prefix]
    ledger=raw.get('ledger')
    if not isinstance(ledger,dict):return []
    requirements=ledger.get('requirements',[])
    if not isinstance(requirements,list):return []
    requirements=[r for r in requirements if isinstance(r,dict) and r.get('priority')=='hard']
    bound={canonical(r.get('target_field') or '') for r in requirements}
    schema=ExperimentSpec if family=='single_voltage' else HierarchicalSpec
    options=[]

    def offer(updates, name):
        edits=[]
        for path,value in updates.items():
            node=raw
            for part in path.split('.'):
                node=node.get(part) if isinstance(node,dict) else None
            if node!=value:edits.append(dict(path=path,operation='set',value=value))
        if not edits:return
        candidate=copy.deepcopy(raw)
        for edit in edits:_assign(candidate,edit)
        try:validated=schema.model_validate(candidate[prefix])
        except (ValueError,TypeError):return
        # An arithmetic repair cannot discard a user-bound quantity. Other
        # constraints still go through the ordinary complete source/ledger audit.
        for r in requirements:
            field=canonical(r.get('target_field') or '')
            attr={'n_buses':'n_buses','users':'users','transformers':'transformer_count','mv_buses':'mv_buses',
                  'lv_branches':'lv_branches','n_loads_min':'n_loads_min','n_loads_max':'n_loads_max'}.get(field)
            if attr and hasattr(validated,attr) and not compare(getattr(validated,attr),r.get('expected_value'),r.get('operator','eq')):
                return
        options.append(dict(id=name,edits=edits,schema_validated=True,
                            note='Only representation/count consistency proved; original requirements and physical validation still required.'))

    if family=='single_voltage':
        scenario=spec.get('scenario') or {}
        if not isinstance(scenario,dict):return []
        changes={}
        if scenario.get('village_count') is not None:
            changes.update({prefix+'.scenario.layout':'rural_villages',prefix+'.scenario.topology':None})
        elif scenario.get('topology') and scenario.get('layout')!='structured_radial':
            changes[prefix+'.scenario.layout']='structured_radial'
        offer(changes,'compatible_layout')
        n=spec.get('n_buses')
        if type(n) is int and not bound & {'n_loads','n_loads_min','n_loads_max'}:
            if scenario.get('load_placement','all_nodes')=='all_nodes':
                for field in ('n_loads_min','n_loads_max'):
                    if field in spec and spec[field]!=n-1:changes[prefix+'.'+field]=n-1
            offer(changes,'compatible_layout_and_load_counts')
    else:
        def value(key):
            return spec.get(key,schema.model_fields[key].get_default(call_default_factory=True))
        t=value('transformer_count');b=value('lv_branches');mv=value('mv_buses');n=value('n_buses');users=value('users')
        if mv is None:mv=t+1 if n is not None else 2*t+1
        if not all(type(v) is int for v in (t,b,mv)):return []
        infrastructure=mv+t*(1+b)
        if type(n) is int and 'n_buses' in bound and 'users' not in bound:
            offer({prefix+'.users':n-infrastructure},'derive_users_from_fixed_total')
        if type(users) is int and 'n_buses' not in bound:
            offer({prefix+'.n_buses':infrastructure+users},'derive_total_from_components')
        # Keep the existing total budget and move only unbound MV/user slots.
        # Every LV branch needs a user; total equality remains independently checked.
        if type(users) is int and users < t*b and not bound & {'users','mv_buses'}:
            total=n if type(n) is int else infrastructure+users
            offer({prefix+'.users':t*b, prefix+'.mv_buses':total-t*(1+b)-t*b},
                  'reallocate_free_bus_budget')
    # Identical paths/values are one candidate, not extra search diversity.
    unique=[]
    for option in options:
        if not any(o['edits']==option['edits'] for o in unique):unique.append(option)
    return unique


def village_alignment(raw, facts):
    """Prove the coupled representation change for a diagnosed exact village count."""
    from .schemas import ExperimentSpec
    from .requirement_contract import canonical
    if raw.get('family')!='single_voltage' or not isinstance(raw.get('single_voltage_spec'),dict):return None
    relevant=[f for f in facts if canonical(f.get('field') or '')=='scenario.village_count'
              and f.get('operator','eq')=='eq' and type(f.get('expected')) is int]
    if len(relevant)!=len(facts) or len({f['expected'] for f in relevant})!=1:return None
    number=relevant[0]['expected']
    spec=copy.deepcopy(raw['single_voltage_spec']);scene=spec.get('scenario')
    if not isinstance(scene,dict):return None
    updates={'village_count':number,'layout':'rural_villages','topology':None}
    edits=[dict(path='single_voltage_spec.scenario.'+key,operation='set',value=value)
           for key,value in updates.items() if scene.get(key)!=value]
    scene.update(updates)
    try:ExperimentSpec.model_validate(spec)
    except (ValueError,TypeError):return None
    return dict(id='align_village_representation',edits=edits,schema_validated=True,
                note='Village count comes from the diagnosed source/ledger requirement. Revalidate all original constraints.') if edits else None


def alignment_choice(raw, facts, action):
    """Prove one scalar choice per field against its complete numeric bounds."""
    import math
    from .planning_diagnostics import _spec_path
    from .planning_patches import apply_scoped_patch
    from .artifacts import digest
    grouped={}
    for fact in facts:
        path=_spec_path(fact.get('field'),raw.get('family'))
        if path not in action.get('paths',[]):return None
        grouped.setdefault(path,[]).append((fact.get('operator','eq'),fact.get('expected')))
    for requirement in (raw.get('ledger') or {}).get('requirements',[]):
        path=_spec_path(requirement.get('target_field'),raw.get('family'))
        if path in grouped and requirement.get('priority')=='hard':
            grouped[path].append((requirement.get('operator','eq'),requirement.get('expected_value')))
    edits=[]
    for path,bounds in grouped.items():
        if any(op not in ('eq','ge','le') or not isinstance(value,(str,int,float,bool)) for op,value in bounds):return None
        if all(type(value) in (int,float) and math.isfinite(value) for _,value in bounds):
            lower=max((value for op,value in bounds if op in ('ge','eq')),default=-math.inf)
            upper=min((value for op,value in bounds if op in ('le','eq')),default=math.inf)
            if lower>upper:return None
            prefix,field=path.split('.',1)
            value=raw.get(prefix,{}).get(field)
            if value is None and prefix=='single_voltage_spec' and field=='n_buses':
                loads=raw[prefix].get('n_loads_min')
                if type(loads) is int:value=loads+1
            if type(value) not in (int,float) or not math.isfinite(value):
                value=lower if math.isfinite(lower) else upper
            value=max(lower,min(upper,value))
        else:
            if any(op!='eq' for op,_ in bounds) or any(value!=bounds[0][1] for _,value in bounds):return None
            value=bounds[0][1]
        edits.append(dict(path=path,operation='set',value=value))
    if not edits:return None
    try:apply_scoped_patch(raw,edits,action,digest(raw))
    except (ValueError,TypeError):return None
    return dict(id='align_observed_scalar',edits=edits,schema_validated=True,
                note='Derived from recorded requirement mismatch and all hard field bounds; still requires original-source review and delivery verification.')
