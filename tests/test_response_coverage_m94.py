"""Response proposals retain protected ports and unique physical directions."""
import copy

import pytest


def test_counter_moves_preserve_all_ports_and_select_distinct_directions(monkeypatch):
    import feeder_agents.response_compensation as module
    from types import SimpleNamespace
    observed=[]
    def local(model,original,spec,probes,assessment,search):
        observed.append(copy.deepcopy(assessment))
        return [dict(kind='scale_subtree',child_bus=child,factor=f,probe_id='b',diagnosis='Test')
                for child,f in [('shared',.95),('shared',.96),('private',.97)]]
    monkeypatch.setattr(module,'distribution_actions',local)
    assessment=dict(targets=[
        dict(target=dict(probe_id='a',lower=1.1,upper=1.2),observed=1.,passed=False),
        dict(target=dict(probe_id='b',lower=.99,upper=1.01),observed=1.,passed=True)])
    before=copy.deepcopy(assessment)
    options=module.compensation_actions(dict(kind='distribution'),None,None,[],assessment,
        SimpleNamespace(allowed_actions=['scale_subtree']))
    assert assessment==before
    assert all([r['target']['probe_id'] for r in item['targets']]==['a','b'] for item in observed)
    assert all(item['targets'][0]['passed'] for item in observed)
    assert {'shared','private'}<={a['child_bus'] for a in options}


def test_direction_diversity_precedes_amplitude_variants():
    from feeder_agents.response_candidates import diverse_directions
    options=[dict(kind='scale_subtree',child_bus=child,factor=f,probe_id='a')
        for child,f in [('b1',1.1),('b1',1.2),('b1',1.3),('b2',1.1),('b3',1.1)]]
    result=diverse_directions(options)
    assert [a['child_bus'] for a in result[:3]]==['b1','b2','b3']
    assert len(result)==len(options)


def test_different_probe_labels_do_not_make_the_same_branch_a_new_direction():
    from feeder_agents.response_candidates import diverse_directions
    options=[dict(kind='scale_subtree',child_bus=child,factor=f,probe_id=probe)
        for child,f,probe in [('shared',1.1,'a'),('shared',1.2,'b'),('private',1.1,'a')]]
    assert [a['child_bus'] for a in diverse_directions(options)[:2]]==['shared','private']


def test_preview_quota_is_filled_after_physical_and_visited_deduplication(monkeypatch):
    from feeder_agents.response_measurement import create_model
    from feeder_agents.response_schema import ResponseDesignPlan
    from feeder_agents.response_search import physical_fingerprint
    import feeder_agents.response_design as module
    plan=ResponseDesignPlan.model_validate(dict(research_question='Unique candidate coverage.',
        base_spec=dict(network_kind='distribution',n_buses=9,seed=19),
        probes=[dict(id='a',kind='voltage_q')],targets=[dict(probe_id='a',lower=1.)],
        search=dict(allowed_actions=['scale_layout'],max_layout_scale_change=.5,candidates_per_round=2)))
    original=create_model(plan.base_spec)
    actions=[dict(kind='scale_layout',factor=f,original_scale=f,probe_id=f'p{i}')
        for i,f in enumerate([1.1,1.1,1.2,1.3,1.4])]
    monkeypatch.setattr(module,'layout_options',lambda *args:actions)
    import feeder_agents.response_distribution as distribution
    monkeypatch.setattr(distribution,'rank_distribution_actions',lambda m,p,a,actions,**kw:actions)
    visited={physical_fingerprint(module.apply_response_action(original,plan.base_spec,actions[0]))}
    before=visited.copy();audit=[]
    options=module.propose_response_actions(original,plan.base_spec,[],dict(base_accepted=True),
        plan.search,original,audit,seen_physical=visited)
    assert visited==before
    assert [a['factor'] for a in options]==[1.2,1.3]
    assert sum(a.get('reason')=='duplicate_before_preview' for a in audit)==2
