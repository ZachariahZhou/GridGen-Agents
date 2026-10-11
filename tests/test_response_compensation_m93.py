"""Bounded compensation uses original geometry and explains action directions."""
import copy
import math
from types import SimpleNamespace as NS

import pytest


def radial_fixture(bound=.3, displacement=10.):
    lines=[NS(id=k,bus1=u,bus2=v,length_km=1.,conductor='research_small')
           for k,u,v in [('trunk','s','j'),('left','j','a'),('right','j','b')]]
    buses=[NS(id=k,x_km=x,y_km=y) for k,x,y in [('s',0.,0.),('j',1.,0.),('a',2.,0.),('b',1.,1.)]]
    model=dict(kind='distribution',feeder=NS(source_bus='s',lines=lines,tie_lines=[],buses=buses,design_evidence={}))
    spec=NS(segment_km_min=.01,segment_km_max=10.)
    search=NS(allowed_actions=['scale_layout','scale_subtree'],max_layout_scale_change=bound,
              max_branch_length_change=bound,max_node_displacement_km=displacement)
    return model,spec,search


def scaled(model,factor):
    result=copy.deepcopy(model)
    for b in result['feeder'].buses:b.x_km*=factor;b.y_km*=factor
    for e in result['feeder'].lines:e.length_km*=factor
    return result


def test_interval_uses_original_lengths_after_first_action():
    from feeder_agents.response_geometry import compensation_scale_interval
    original,spec,search=radial_fixture()
    after=scaled(original,1.2)
    interval=compensation_scale_interval(after,original,spec,search,
        dict(kind='scale_subtree',child_bus='b',factor=.9))
    assert interval['lower']==pytest.approx(.7/1.2)
    assert interval['upper']==pytest.approx(1.3/1.2)


def test_interval_intersects_cumulative_node_displacement():
    from feeder_agents.response_geometry import compensation_scale_interval
    original,spec,search=radial_fixture(bound=.75,displacement=.15)
    interval=compensation_scale_interval(original,original,spec,search,
        dict(kind='scale_subtree',child_bus='b',factor=.9))
    assert (interval['lower'],interval['upper'])==pytest.approx((.85,1.15))
    # A primary global edit already moved the source-side branches too far;
    # a local B edit cannot repair their displacement.
    assert compensation_scale_interval(scaled(original,1.2),original,spec,search,
        dict(kind='scale_subtree',child_bus='b',factor=.9))['empty']


def test_empty_interval_is_strict_json_safe_before_any_upper_bound_is_computed():
    import json
    from feeder_agents.response_geometry import compensation_scale_interval
    original,spec,search=radial_fixture(bound=.1)
    interval=compensation_scale_interval(scaled(original,1.2),original,spec,search,
        dict(kind='scale_subtree',child_bus='b'))
    assert interval['empty'] and interval['reason']=='unchanged_line:trunk'
    json.dumps(interval,allow_nan=False)


@pytest.mark.parametrize('child',['a','b'])
def test_interval_respects_the_separate_budget_for_previously_rewired_edges(child):
    from feeder_agents.response_geometry import compensation_scale_interval
    original,spec,search=radial_fixture(bound=.1)
    search.allowed_actions.append('rewire_branch');search.max_rewire_length_ratio=2.
    current=copy.deepcopy(original)
    edge=current['feeder'].lines[-1];edge.bus1='s';edge.length_km=math.sqrt(2.)
    interval=compensation_scale_interval(current,original,spec,search,
        dict(kind='scale_subtree',child_bus=child,factor=1.01))
    assert not interval['empty']
    assert interval['lower'] < 1.01 < interval['upper']
    assert interval['upper']==pytest.approx(math.sqrt(2.) if child=='b' else 1.1)


def test_bound_endpoint_rescues_an_inadmissible_compensation_optimum():
    from feeder_agents.response_compensation import conditional_compensation_actions
    model,spec,search=radial_fixture(bound=.3)
    # Parent is already at +20%; primary adds another 5% on every line.
    current=scaled(model,1.2)
    probes=[dict(id=p,kind='voltage_q',injection_buses=[p],monitor_buses=[p]) for p in ('a','b')]
    assessment=dict(targets=[
        dict(target=dict(probe_id='a',lower=1.2,upper=1.4),observed=1.,deficit=.2/1.4,passed=False),
        dict(target=dict(probe_id='b',lower=.7,upper=.72),observed=1.,deficit=.28/.72,passed=False)])
    first=dict(kind='scale_layout',factor=1.05,original_scale=1.26,probe_id='a')
    counter=dict(kind='scale_subtree',child_bus='b',factor=.9,probe_id='b')
    actions=conditional_compensation_actions(current,probes,assessment,[first],[counter],
        original=model,spec=spec,search=search)
    assert actions
    # The unconstrained ideal puts B's edge below its original 70% floor.
    assert actions[0]['actions'][1]['factor']==pytest.approx(.7/1.26)
    evidence=actions[0]['compensation_fit']
    assert evidence['unconstrained_factor'] < evidence['interval']['lower']
    assert evidence['boundary_selected']


def test_direction_diagnostics_distinguish_cancellation_from_selectivity():
    from feeder_agents.response_geometry import action_direction_diagnostic
    collinear=action_direction_diagnostic({'a':1.,'b':2.},{'a':2.,'b':4.},'a','b')
    selective=action_direction_diagnostic({'a':1.,'b':2.},{'a':0.,'b':4.},'a','b')
    assert collinear['direction_separation']==pytest.approx(0.)
    assert collinear['remaining_primary_increment']==pytest.approx(0.)
    assert selective['direction_separation']>0
    assert selective['remaining_primary_increment']==pytest.approx(1.)
    no_control=action_direction_diagnostic({'a':1.,'b':2.},{'a':4.,'b':0.},'a','b')
    assert no_control['remaining_primary_increment'] is None
    assert no_control['classification']=='no_protected_port_effect'


def test_compensation_policy_is_explicit_and_validated():
    from feeder_agents.response_schema import ResponseSearch
    assert ResponseSearch().compensation_mode=='bounded'
    for mode in ('fixed','conditional','bounded'):
        assert ResponseSearch(compensation_mode=mode).compensation_mode==mode
    with pytest.raises(ValueError):ResponseSearch(compensation_mode='ignore_permissions')


@pytest.mark.parametrize('ties,previews',[(0,8),(1,64)])
def test_random_geometry_baseline_ignores_targets_and_preserves_contract(ties,previews,monkeypatch):
    from feeder_agents.response_baselines import random_geometry_proposer
    from feeder_agents.response_measurement import create_model
    from feeder_agents.response_schema import ResponseDesignPlan
    from feeder_agents.response_design import apply_response_action,research_contract_preserved
    plan=ResponseDesignPlan.model_validate(dict(research_question='Independent geometry baseline.',
        base_spec=dict(network_kind='distribution',n_buses=13,seed=1559,
            scenario=dict(kind='urban',layout='spatial_mst',tie_count=ties)),
        probes=[dict(id='a',kind='voltage_q')],
        targets=[dict(probe_id='a',reference='baseline_ratio',lower=1.1,upper=1.2)],
        search=dict(allowed_actions=['scale_layout','scale_subtree'],max_layout_scale_change=.2,
            max_branch_length_change=.2,max_node_displacement_km=.1,max_joint_actions=2,candidates_per_round=previews)))
    original=create_model(plan.base_spec)
    import feeder_agents.response_design as runner
    failed=[]
    def counted(*args,**kwargs):
        passed=research_contract_preserved(*args,**kwargs)
        if not passed:failed.append(False)
        return passed
    monkeypatch.setattr(runner,'research_contract_preserved',counted)
    audit=[]
    left=random_geometry_proposer(87)(original,plan.base_spec,[],{'targets':['first']},plan.search,original,audit)
    assert len(failed)==sum(r.get('reason')=='random_intermediate_contract_rejected' for r in audit)
    if ties:assert failed
    right=random_geometry_proposer(87)(original,plan.base_spec,[],{'targets':['different']},plan.search,original)
    assert left==right and len(left)==plan.search.candidates_per_round
    for action in left:
        assert research_contract_preserved(original,apply_response_action(original,plan.base_spec,action),plan)


def test_layout_only_permissions_do_not_require_a_displacement_limit():
    from feeder_agents.response_geometry import compensation_scale_interval
    original,spec,search=radial_fixture(bound=.2)
    search.allowed_actions=['scale_layout'];search.max_branch_length_change=None;search.max_node_displacement_km=None
    interval=compensation_scale_interval(scaled(original,1.1),original,spec,search,dict(kind='scale_layout'))
    assert (interval['lower'],interval['upper'])==pytest.approx((.8/1.1,1.2/1.1))
