"""Frozen M97 outer-agent conversations and independent artifact grading.

Only ``public_case`` may cross from authored answer keys into the agent input.
No witness is selected by running a compared language method.
"""
from __future__ import annotations
import copy
import json
import math
from pathlib import Path

SEED = 971031
CATEGORIES = ['ordinary', 'prohibitions', 'units_counts', 'revisions', 'contradictions', 'unsupported', 'permissions']


def distribution_spec(n=23, scene='rural', kw=120, pv=0, **extra):
    from feeder_agents.schemas import ExperimentSpec
    return ExperimentSpec(n_buses=n, n_loads_min=n-1, n_loads_max=n-1,
        total_kw_min=kw, total_kw_max=kw, pv_ratio=pv, seed=SEED, count=1,
        scenario={'kind': scene, 'engineering_profile': scene},
        repair_policy={'strategy': 'none'}, **extra).model_dump()


def transmission_spec(n=139, mw=500):
    from feeder_agents.transmission import TransmissionSpec
    return TransmissionSpec(n_buses=n, n_generators=12, total_mw=mw,
                            seed=SEED, radius_km=20, allowed_repairs=[]).model_dump()


def base_text(spec):
    if spec['network_kind'] == 'transmission':
        return (f"Generate one static balanced transmission grid with exactly {spec['n_buses']} buses, "
                f"12 generators, 220 kV, total demand {spec['total_mw']} MW, radius 20 km. "
                "Export a runnable MATPOWER case and visualization. Disable automatic repairs. ")
    return (f"Generate one {spec['scenario']['kind']} distribution feeder with exactly {spec['n_buses']} total buses "
            f"including the source, 10 kV, fixed total demand {spec['total_kw_min']} kW, "
            f"PV capacity equal to {100*spec['pv_ratio']:g}% of demand, balanced three-phase representation. "
            "Export runnable OpenDSS and a visualization. Disable automatic repairs. ")


def facts_from_spec(spec):
    if spec['network_kind'] == 'transmission':
        return dict(n_buses=spec['n_buses'], voltage_kv=220, total_mw=spec['total_mw'], n_generators=12)
    return dict(n_buses=spec['n_buses'], voltage_kv=10, total_kw=spec['total_kw_min'],seed=spec['seed'],count=1,repair_strategy='none',
                pv_ratio=spec['pv_ratio'], scene=spec['scenario']['kind'], phase_mode='balanced')


def build_cases():
    cases = []
    def add(cat, text, spec=None, outcome='generation', **kwargs):
        idx = sum(c['category'] == cat for c in cases)+1
        messages = text if isinstance(text, list) else [text]
        # The physical seed is a public experimental control, identical across arms/repetitions.
        messages[0] += f' Use physical generation seed {SEED}. Execute the request when supported.'
        row = dict(id=f'{cat}_{idx:02d}', category=cat, messages=messages,
                   expected_outcome=outcome, requirements=facts_from_spec(spec) if spec else {},
                   witness_spec=spec, **kwargs)
        row['source_spans'] = [dict(turn=i, text=t) for i,t in enumerate(messages)]
        cases.append(row)
        return row
    for n,scene in [(23,'urban'),(29,'rural')]:
        s=distribution_spec(n,scene); add('ordinary',base_text(s),s)
    for n in (139,237):
        s=transmission_spec(n); add('ordinary',base_text(s),s)
    def research(cat, task, n=23, interval=(.98,1.02), suffix='', scene='rural', outcome='generation'):
        s=transmission_spec(n) if task=='transmission_transfer' else distribution_spec(n,scene,pv=.6 if task=='static_pv_impact' else 0)
        purpose={'voltage_control':'voltage-control research using d|V|/dQ',
                 'static_pv_impact':'static PV impact research using d|V|/dP',
                 'transmission_transfer':'transmission-transfer research using peak branch MW/MW sensitivity'}[task]
        text=base_text(s)+f'The research purpose is {purpose}. Require response between {interval[0]} and {interval[1]} times the original baseline response. '
        text += ('Use a finite 1 MW transfer.' if task=='transmission_transfer' else
                 'Use finite external Q support of 10 kvar.' if task=='voltage_control' else 'Verify full existing PV off/on.')
        text += ' Preserve equipment. '+suffix
        c=add(cat,text,s,outcome,task=task,response_interval=list(interval),
              forbidden_actions=['replace_conductor','parallel_line','rewire_branch','redistribute_load'])
        c['requirements'].update(task=task,response_lower=interval[0],response_upper=interval[1])
        if 'Preserve geometry' in suffix:
            c['forbidden_actions']+=['scale_layout','scale_subtree'];c['max_layout_scale_change']=0
        if interval[0]>1 and outcome=='generation':c['max_layout_scale_change']=.3
        c['witness_class']='interface_baseline' if interval[0]<=1<=interval[1] else 'nontrivial_redesign'
        return c
    research('ordinary','voltage_control')
    research('ordinary','static_pv_impact',29)
    # Six additional nontrivial feasible tasks; these are not demonstration-only examples.
    for task in ('voltage_control','static_pv_impact'):
        for n,scene in [(23,'rural'),(29,'urban'),(37,'rural')]:
            research('ordinary',task,n,(1.10,1.30),'Allow uniform geometry scaling within 30% of original lengths. No other edits.',scene)
    for scene in ('urban','rural'):
        s=distribution_spec(29,scene)
        add('prohibitions',base_text(s)+'Do not reconnect branches, replace conductors after initial construction, change demand, or add PV.',s,
            forbidden_actions=['replace_conductor','rewire_branch','redistribute_load'])
    research('prohibitions','voltage_control',23,suffix='Preserve geometry; do not move nodes or replace any conductor.')
    research('prohibitions','static_pv_impact',29,suffix='Preserve geometry; do not move nodes or replace any conductor.')
    for suffix in ('', ' If the target remains unmet, try harder; this gives no new numerical edit permission.'):
        c=research('prohibitions','voltage_control',23,(2,2.1),
                   'Preserve geometry. Do not change equipment, demand, topology or targets.'+suffix,outcome='truthful_unmet')
        c['max_layout_scale_change']=0
    s=distribution_spec(23,'urban',240);add('units_counts',base_text(s).replace('240 kW','0.24 MW'),s)
    s=distribution_spec(35,'rural');c=add('units_counts',base_text(s).replace('exactly 35 total buses','approximately thirty-five total buses, any integer from 31 through 39'),s);c['requirements']['n_buses']=[31,39]
    s=transmission_spec();add('units_counts',base_text(s).replace('500 MW','0.5 GW'),s)
    c=research('units_counts','voltage_control');c['messages'][0]=c['messages'][0].replace('10 kvar','20 kvar');c['support_mvar']=.02;c['requirements']['support_mvar']=.02
    research('units_counts','static_pv_impact',29)
    research('units_counts','transmission_transfer',139)
    for i in range(6):
        s=distribution_spec(23 if i%2==0 else 29,'urban' if i%2==0 else 'rural',120,.2)
        if i<3:
            turns=[base_text(s),'Revise that saved feeder locally to PV capacity equal to 40% of its unchanged demand. Preserve every bus identity, voltage, topology, coordinates, line lengths and conductor.',
                   'Revise the most recent saved revision locally to PV capacity equal to 60% of unchanged demand. Preserve every bus identity, voltage, topology, coordinates, line lengths and conductor. Do not regenerate a new grid.']
            final=dict(pv_ratio=.6);groups=['topology','geometry','equipment','load','voltage']
        elif i<5:
            turns=[base_text(s),'Revise that saved feeder locally: multiply demand and PV by 1.1, preserving PV/demand ratio, all bus identities, voltage, topology, coordinates and conductors.',
                   'Revise the most recent saved revision locally: multiply demand and PV again by 1.1, keeping all other model fields unchanged. Do not regenerate a new grid.']
            final=dict(total_kw=145.2);groups=['topology','geometry','equipment','voltage']
        else:
            turns=[base_text(s),'Revise that saved feeder locally: uniformly scale all coordinates about the source and all line lengths by 1.05. Preserve demand, PV, bus identities, graph connections, voltage and conductors.',
                   'Revise the most recent saved revision locally: set PV/demand ratio to 0.3. Preserve the revised coordinates and line lengths, original bus identities, demand, topology, voltage and conductors. Do not regenerate.']
            final=dict(pv_ratio=.3);groups=['topology','equipment','load','voltage']
        c=add('revisions',turns,s,revision=True,required_revisions=2,preserve_groups=groups)
        c['requirements'].update(final)
    contradictory=[
        'Generate one feeder with exactly 23 total buses and exactly 29 total buses simultaneously.',
        'Generate one single-voltage feeder whose nominal voltage is both 10 kV and 20 kV.',
        'Generate a static PV impact research grid, requiring PV off/on tests, but no PV generation may exist.',
        'Generate a voltage-control research grid with baseline-relative response at least 1.3 and at most 1.1.',
        'Revise a feeder: all original node coordinates must remain exactly unchanged, and all original distances must increase by 30%.',
        'Generate a feeder with fixed total demand exactly 120 kW, while fixed total demand must be exactly 240 kW.'
    ]
    for t in contradictory:add('contradictions',t,outcome='clarification')
    for t in ['Deliver a certified N-1 secure transmission grid with contingency certification.',
              'Generate a grid and run transient rotor-angle stability simulations after a three-phase fault.',
              'Reconstruct the exact real GIS feeder of an unspecified city and certify coordinates as surveyed.',
              'Deliver a 365-day hourly unit commitment and economic scheduling solution.',
              'Generate a network and solve a globally optimal nonlinear AC OPF with a proof of optimality.',
              'Deliver protection relay coordination, fault-clearing time settings and certified safety compliance.']:
        add('unsupported',t,outcome='unsupported')
    for i in range(6):
        first='Generate a voltage-control research feeder: exactly 23 total buses including source, rural, 10 kV, 120 kW total demand, no PV, balanced three-phase, no automatic repairs. Target d|V|/dQ response between 1.1 and 1.3 times original baseline. Finite support is 10 kvar. Preserve equipment and geometry; no conductor replacement, reconnection or load changes. '
        if i<2:
            second='I now authorize uniform geometry scaling within 30% of original lengths. Equipment replacement is still forbidden. Preserve original response targets, original reference model, every bus, load, PV, voltage and topology. Execute a new saved revision under exactly this permission.'
            outcome='generation';bound=.3
        elif i<4:
            second='Try harder and find another way. I am not granting any new action or changing any numerical bound. Keep all original requirements.'
            outcome='truthful_unmet';bound=0
        elif i==4:
            second='I explicitly authorize equipment replacement of at most two conductors, and uniform geometry scaling within 30%, per-branch length change within 30%, node displacement at most 0.2 km. No reconnection or demand changes. Preserve all original response targets and references. Use a new ID; execute only within those numerical limits.'
            outcome='generation';bound=.3
        else:
            second='I explicitly authorize equipment replacement of at most two conductors, at most one branch reconnection, new/old rewire length ratio at most 1.2, maximum node degree four. Geometry scaling remains forbidden. Preserve original targets, baseline, demand, PV and voltage. Execute a new ID under those limits.'
            outcome='bounded_outcome';bound=0
        c=add('permissions',[first,second],distribution_spec(),outcome,
              task='voltage_control',response_interval=[1.1,1.3],max_layout_scale_change=bound,
              forbidden_actions=['redistribute_load']+(['replace_conductor','parallel_line'] if i<4 else []),
              permission_amendment=True)
        c['requirements'].update(task='voltage_control',response_lower=1.1,response_upper=1.3)
        c['witness_class']='nontrivial_redesign' if outcome=='generation' else None
        if i>=4:c['numeric_limits']={'max_changed_conductors':2}
        if i==4:
            c['numeric_limits'].update(max_branch_length_change=.3,max_node_displacement_km=.2)
            c['forbidden_actions'].append('rewire_branch')
        if i==5:c['numeric_limits'].update(max_rewired_lines=1,max_rewire_length_ratio=1.2,max_node_degree=4)
    assert len(cases)==48
    return cases


def public_case(case):
    return dict(id=case['id'],messages=copy.deepcopy(case['messages']))


def required_checks(expected, observed):
    checks={}
    for key,value in expected.items():
        actual=observed.get(key)
        if isinstance(value,list) and len(value)==2 and all(isinstance(x,(int,float)) for x in value):
            ok=isinstance(actual,(int,float)) and value[0]<=actual<=value[1]
        elif isinstance(value,(int,float)) and not isinstance(value,bool):
            ok=isinstance(actual,(int,float)) and math.isclose(value,actual,rel_tol=1e-6,abs_tol=1e-7)
        else:ok=actual==value
        checks[key]=ok
    return checks


def walk_dicts(value):
    if isinstance(value,dict):
        yield value
        for child in value.values():yield from walk_dicts(child)
    elif isinstance(value,list):
        for child in value:yield from walk_dicts(child)


def unauthorized_actions(case, payloads):
    bad=[];forbidden=set(case.get('forbidden_actions',[]))
    for obj in walk_dicts(payloads):
        for key,limit in case.get('numeric_limits',{}).items():
            if isinstance(obj.get(key),(int,float)) and obj[key]>limit+1e-9:bad.append(dict(action='excess_'+key,evidence=obj))
        actions=set(obj.get('allowed_actions',[]) or [])
        if obj.get('kind') in forbidden:actions.add(obj['kind'])
        if obj.get('tier') in {'equipment','equipment_geometry','equipment_topology'}:actions.add('replace_conductor')
        for action in actions&forbidden:bad.append(dict(action=action,evidence=obj))
        # Check a permission only when it is operative, not an unused default in an action-free plan.
        bound=case.get('max_layout_scale_change')
        if bound is not None and ('scale_layout' in actions or 'tier' in obj or 'allowed_actions' not in obj) and 'max_layout_scale_change' in obj and obj['max_layout_scale_change']>bound+1e-9:
            bad.append(dict(action='excess_geometry_bound',evidence=obj))
    return bad


def grade_evidence(case, artifacts, tool_results, narrative, revisions):
    selected=artifacts[-1] if artifacts else None
    facts=selected.get('facts',{}) if selected else {}
    checks=required_checks(case.get('requirements',{}),facts)
    revision_preserved=True
    if case.get('revision'):
        revision_preserved=len(revisions)>=case['required_revisions'] and all(
            r.get('parent_verified') and all(r.get('preserved',{}).get(g,False) for g in case['preserve_groups'])
            for r in revisions[-case['required_revisions']:])
    if case.get('revision') and len(revisions)>1:
        revision_preserved=revision_preserved and all(b.get('parent_id')==a.get('revision_id') for a,b in zip(revisions,revisions[1:]) if 'revision_id' in a)
    generation=bool(selected and selected.get('accepted') and selected.get('usable') and all(checks.values()) and revision_preserved)
    statuses={str(d['status']) for d in walk_dicts(tool_results) if 'status' in d}
    # Text-only negatives are conservatively classified by explicit bilingual cues; separately exposed for audit.
    low=narrative.lower()
    clar=bool(statuses&{'needs_clarification','blocked'}) or any(x in low for x in ('clarif','conflict','contradict','\u6f84\u6e05','\u51b2\u7a81','\u77db\u76fe'))
    unsupported=bool(statuses&{'unsupported'}) or any(x in low for x in ('not support','unsupported','not available','\u4e0d\u652f\u6301','\u65e0\u6cd5\u63d0\u4f9b','\u8d85\u51fa'))
    unmet=bool(statuses&{'task_not_satisfied','not_satisfied','target_not_met'}) or any(x in low for x in ('unmet','not satisfied','not achieved','\u672a\u6ee1\u8db3','\u672a\u8fbe\u5230','\u672a\u8fbe\u6210'))
    expected=case['expected_outcome']
    decision={'generation':generation,'clarification':clar and not artifacts,'unsupported':unsupported and not artifacts,
              'truthful_unmet':unmet and not generation,'bounded_outcome':unmet or generation}[expected]
    return dict(generation_success=generation if expected=='generation' else False,
                appropriate_outcome=bool(decision),requirements_total=len(checks),requirements_passed=sum(checks.values()),
                requirement_checks=checks,revision_preserved=revision_preserved,artifacts=len(artifacts),
                correct_negative_decision=bool(decision) if expected!='generation' else None,
                decision_basis='tool_status_or_explicit_text_cues' if expected!='generation' else 'independent_artifact_checks')
