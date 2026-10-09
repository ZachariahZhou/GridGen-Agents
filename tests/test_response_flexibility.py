"""Research freedoms stay bounded against the original physical model."""
import copy
import math

import networkx as nx
import pytest


def payload(actions=None, **bounds):
    return dict(research_question='Spatially selective response design',
        base_spec=dict(network_kind='distribution',n_buses=13,total_kw_min=360,total_kw_max=360,
            pv_ratio=.3,seed=42,scenario=dict(kind='urban',engineering_profile='urban',layout='spatial_mst'),
            phase_design=dict(mode='unbalanced',single_phase_laterals=0,two_phase_laterals=0),
            repair_policy=dict(strategy='none')),
        probes=[dict(id='p',kind='voltage_p',step_mw=.01)],
        targets=[dict(probe_id='p',reference='baseline_ratio',lower=1.3,upper=1.5)],
        search=dict(allowed_actions=actions or [],**bounds))


def setup_plan(p):
    from feeder_agents.response_schema import ResponseDesignPlan
    from feeder_agents.response_measurement import create_model
    plan=ResponseDesignPlan.model_validate(p)
    return plan,create_model(plan.base_spec)


def test_expanded_bounds_require_separate_permissions():
    from feeder_agents.response_schema import ResponseDesignPlan
    p=payload(['scale_layout'],max_layout_scale_change=.75,max_rounds=24,candidates_per_round=32,max_joint_actions=2)
    assert ResponseDesignPlan.model_validate(p).search.max_layout_scale_change==.75
    for action in ('scale_subtree','rewire_branch','redistribute_load'):
        with pytest.raises(ValueError):ResponseDesignPlan.model_validate(payload([action]))
    p=payload(['redistribute_load'],max_load_redistribution_fraction=.2,max_load_bus_change=.5)
    p['base_spec']['scenario']['load_shape']='uniform'
    with pytest.raises(ValueError,match='heterogeneous'):ResponseDesignPlan.model_validate(p)


def test_subtree_scale_has_local_effect_and_original_reference_budget():
    from feeder_agents.response_design import apply_response_action,research_contract_preserved
    plan,original=setup_plan(payload(['scale_subtree'],max_branch_length_change=.5,max_node_displacement_km=1))
    f=original['feeder'];graph=nx.Graph((e.bus1,e.bus2) for e in f.lines);tree=nx.bfs_tree(graph,f.source_bus)
    child=next(b for b in tree if b!=f.source_bus and 0<len(nx.descendants(tree,b))<len(f.buses)-2)
    action=dict(kind='scale_subtree',child_bus=child,factor=1.2)
    trial=apply_response_action(original,plan.base_spec,action)
    assert research_contract_preserved(original,trial,plan)
    changed=nx.descendants(tree,child)|{child}
    assert all(a==b for a,b in zip(f.buses,trial['feeder'].buses) if a.id not in changed)
    excessive=apply_response_action(trial,plan.base_spec,dict(action,factor=1.4))
    assert not research_contract_preserved(original,excessive,plan)


def test_load_transfer_conserves_total_pq_and_each_pv_and_phase_ratios(tmp_path):
    from feeder_agents.response_design import apply_response_action,research_contract_preserved
    from feeder_agents.response_measurement import resolve_probes,measure
    plan,original=setup_plan(payload(['redistribute_load'],max_load_redistribution_fraction=.2,max_load_bus_change=.5))
    a,b=original['feeder'].loads[:2];amount=.2*min(a.kw,b.kw)
    action=dict(kind='redistribute_load',donor=a.id,receiver=b.id,kw=amount)
    trial=apply_response_action(original,plan.base_spec,action)
    assert research_contract_preserved(original,trial,plan)
    for field in ('kw','kvar','pv_kw'):
        assert sum(getattr(l,field) for l in trial['feeder'].loads)==pytest.approx(sum(getattr(l,field) for l in original['feeder'].loads))
    assert [l.pv_kw for l in original['feeder'].loads]==[l.pv_kw for l in trial['feeder'].loads]
    assert measure(trial,plan.base_spec,resolve_probes(original,plan.probes),tmp_path)['base_accepted']
    trial['feeder'].loads[0].phase_powers[0].kw+=.1
    assert not research_contract_preserved(original,trial,plan)


def test_rewire_candidates_preserve_tree_distance_and_phase_contract(tmp_path):
    from feeder_agents.response_design import apply_response_action,research_contract_preserved,propose_response_actions,score_response
    from feeder_agents.response_measurement import measure,resolve_probes
    p=payload(['rewire_branch'],max_rewired_lines=2,max_rewire_length_ratio=2,max_node_degree=4)
    p['targets'][0].update(lower=.5,upper=.95)
    plan,original=setup_plan(p);probes=resolve_probes(original,plan.probes)
    measured=measure(original,plan.base_spec,probes,tmp_path/'base');scored=score_response(measured,plan.targets,measured)
    actions=propose_response_actions(original,plan.base_spec,probes,scored,plan.search,original)
    assert actions
    accepted=[]
    for i,action in enumerate(actions):
        trial=apply_response_action(original,plan.base_spec,action)
        assert research_contract_preserved(original,trial,plan)
        checked=measure(trial,plan.base_spec,probes,tmp_path/str(i))
        accepted.append(checked['base_accepted'])
    assert any(accepted)
    bad=copy.deepcopy(original);bad['feeder'].lines[0].bus2=bad['feeder'].source_bus
    assert not research_contract_preserved(original,bad,plan)


def test_compound_move_and_budget_do_not_relax_original_contract():
    from feeder_agents.response_design import apply_response_action,research_contract_preserved
    plan,original=setup_plan(payload(['scale_layout','redistribute_load'],max_layout_scale_change=.5,
        max_load_redistribution_fraction=.2,max_load_bus_change=.5,max_joint_actions=2))
    a,b=original['feeder'].loads[:2]
    action=dict(kind='compound',actions=[dict(kind='scale_layout',factor=1.1,original_scale=1.1),
        dict(kind='redistribute_load',donor=a.id,receiver=b.id,kw=.1*min(a.kw,b.kw))])
    trial=apply_response_action(original,plan.base_spec,action)
    assert research_contract_preserved(original,trial,plan)
    trial['feeder'].loads[0].pv_kw+=1
    assert not research_contract_preserved(original,trial,plan)


def test_language_local_permissions_are_checked_against_user_numbers(tmp_path):
    from feeder_agents.response_language import design_response_from_request
    class Model:
        def with_structured_output(self,*args,**kwargs):return self
        def invoke(self,messages):return dict(status='ready',plan=payload(['scale_subtree'],
            max_branch_length_change=.6,max_node_displacement_km=.5),issues=[])
    base='\u751f\u621013\u8282\u70b9\u57ce\u5e02\u9988\u7ebf\uff0c\u63a7\u5236\u7535\u538b\u7075\u654f\u5ea6\u3002'
    denied=design_response_from_request(base,tmp_path,'denied',execute=False,model=Model())
    assert denied['status']=='needs_clarification'
    granted=design_response_from_request(base+'\u5141\u8bb8\u5b50\u6811\u7f29\u653e\u4e0d\u8d85\u8fc760%\uff0c\u8282\u70b9\u4f4d\u79fb\u4e0d\u8d85\u8fc70.5km\u3002',tmp_path,'granted',execute=False,model=Model())
    assert granted['status']=='draft'
    excessive=design_response_from_request(base+'\u5141\u8bb8\u5b50\u6811\u7f29\u653e\u4e0d\u8d85\u8fc730%\uff0c\u8282\u70b9\u4f4d\u79fb\u4e0d\u8d85\u8fc70.5km\u3002',tmp_path,'excessive',execute=False,model=Model())
    assert excessive['status']=='needs_clarification'


@pytest.mark.parametrize('action,bounds,text',[
    ('scale_subtree',dict(max_branch_length_change=.6,max_node_displacement_km=.5),'\u4e0d\u5141\u8bb8\u5b50\u6811\u7f29\u653e\u4e0d\u8d85\u8fc760%\uff0c\u8282\u70b9\u4f4d\u79fb\u4e0d\u8d85\u8fc70.5km\u3002'),
    ('rewire_branch',dict(max_rewired_lines=2,max_rewire_length_ratio=2,max_node_degree=4),'\u4e0d\u5141\u8bb8\u91cd\u63a5\u6700\u591a2\u6761\u652f\u8def\uff0c\u65b0\u7ebf\u957f\u4e0d\u8d85\u8fc7\u539f\u6765\u76842\u500d\uff0c\u6700\u5927\u5ea6\u65704\u3002'),
    ('redistribute_load',dict(max_load_redistribution_fraction=.2,max_load_bus_change=.5),'\u4e0d\u5141\u8bb8\u8d1f\u8377\u91cd\u5206\u914d\u8d85\u8fc7\u603b\u8d1f\u837720%\uff0c\u5355\u8282\u70b9\u8d1f\u8377\u53d8\u5316\u4e0d\u8d85\u8fc750%\u3002')])
def test_negated_permissions_never_grant_actions(action,bounds,text):
    from feeder_agents.response_language import _local_permission_issues
    from feeder_agents.response_schema import ResponseSearch
    assert _local_permission_issues(text,ResponseSearch(allowed_actions=[action],**bounds))


def test_load_budget_is_cumulative_and_fixed_coordinates_reject_local_scaling():
    from feeder_agents.response_design import apply_response_action,research_contract_preserved
    from feeder_agents.response_schema import ResponseDesignPlan
    p=payload(['redistribute_load'],max_load_redistribution_fraction=.001,max_load_bus_change=.9)
    plan,original=setup_plan(p);a,b=original['feeder'].loads[:2]
    move=dict(kind='redistribute_load',donor=a.id,receiver=b.id,kw=.00075*original['feeder'].total_kw)
    first=apply_response_action(original,plan.base_spec,move)
    assert research_contract_preserved(original,first,plan)
    second=apply_response_action(first,plan.base_spec,move)
    assert not research_contract_preserved(original,second,plan)
    p=payload(['scale_subtree'],max_branch_length_change=.5,max_node_displacement_km=.5)
    p['base_spec']['scenario']['positions_km']=[[i*.05,0] for i in range(13)]
    with pytest.raises(ValueError,match='coordinates'):ResponseDesignPlan.model_validate(p)


def test_small_displacement_budget_generates_small_valid_moves(tmp_path):
    from feeder_agents.response_design import propose_response_actions,score_response
    from feeder_agents.response_measurement import measure,resolve_probes
    plan,original=setup_plan(payload(['scale_subtree'],max_branch_length_change=.9,max_node_displacement_km=.001))
    probes=resolve_probes(original,plan.probes);measured=measure(original,plan.base_spec,probes,tmp_path)
    options=propose_response_actions(original,plan.base_spec,probes,score_response(measured,plan.targets,measured),plan.search,original)
    assert options


def test_met_port_gets_compensation_only_inside_joint_previews(tmp_path):
    from feeder_agents.response_design import propose_response_actions,score_response
    from feeder_agents.response_measurement import measure,resolve_probes
    p=payload(['replace_conductor','scale_layout'],max_layout_scale_change=.75,max_joint_actions=2,candidates_per_round=64)
    p['probes']=[dict(id='a',kind='voltage_p',injection_buses=['b1']),dict(id='b',kind='voltage_p',injection_buses=['b9'])]
    p['targets']=[dict(probe_id='a',reference='baseline_ratio',lower=1.3,upper=1.5),dict(probe_id='b',reference='baseline_ratio',lower=.99,upper=1.01)]
    plan,original=setup_plan(p);probes=resolve_probes(original,plan.probes);m=measure(original,plan.base_spec,probes,tmp_path)
    options=propose_response_actions(original,plan.base_spec,probes,score_response(m,plan.targets,m),plan.search,original)
    assert any(a['kind']=='compound' and any(v.get('compensation') for v in a['actions']) for a in options)
    assert not any(a.get('compensation') for a in options)


def test_subtree_contract_rejects_arbitrary_coordinate_warp():
    from feeder_agents.response_design import research_contract_preserved
    plan,original=setup_plan(payload(['scale_subtree'],max_branch_length_change=.5,max_node_displacement_km=.5))
    trial=copy.deepcopy(original);trial['feeder'].buses[-1].x_km+=.001
    positions={b.id:(b.x_km,b.y_km) for b in trial['feeder'].buses}
    for line in trial['feeder'].lines:line.length_km=math.dist(positions[line.bus1],positions[line.bus2])
    assert not research_contract_preserved(original,trial,plan)


@pytest.mark.parametrize('text',['Do not allow spatial scaling up to 15%.','Never allow uniform layout scaling up to 15%.'])
def test_english_denial_does_not_grant_uniform_scaling(text):
    from feeder_agents.response_language import _layout_permission
    assert _layout_permission(text) is None


def test_subtree_scaling_can_rotate_a_normally_open_tie(tmp_path):
    from feeder_agents.response_design import apply_response_action,research_contract_preserved
    from feeder_agents.response_measurement import measure,resolve_probes
    p=payload(['scale_subtree'],max_branch_length_change=.5,max_node_displacement_km=.5)
    p['base_spec']['scenario']['tie_count']=1
    plan,original=setup_plan(p)
    trial=apply_response_action(original,plan.base_spec,dict(kind='scale_subtree',child_bus='b4',factor=1.01))
    assert original['feeder'].tie_lines
    assert research_contract_preserved(original,trial,plan)
    assert measure(trial,plan.base_spec,resolve_probes(original,plan.probes),tmp_path)['base_accepted']


@pytest.mark.parametrize('denial',["Don't allow",'Don’t allow'])
@pytest.mark.parametrize('action,bounds,text',[
    ('scale_subtree',dict(max_branch_length_change=.6,max_node_displacement_km=.5),'subtree scaling, even within 60%; node displacement limit 0.5 km.'),
    ('rewire_branch',dict(max_rewired_lines=2,max_rewire_length_ratio=2,max_node_degree=4),'reconnecting 2 branches; new line length at most 2 times original; maximum degree 4.'),
    ('redistribute_load',dict(max_load_redistribution_fraction=.2,max_load_bus_change=.5),'load redistribution, even within 20%; per-bus load change limited to 50%.')])
def test_contracted_denial_never_grants_local_actions(denial,action,bounds,text):
    from feeder_agents.response_language import _local_permission_issues
    from feeder_agents.response_schema import ResponseSearch
    assert _local_permission_issues(denial+' '+text,ResponseSearch(allowed_actions=[action],**bounds))
