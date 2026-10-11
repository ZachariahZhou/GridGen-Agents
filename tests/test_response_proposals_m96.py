"""Fair proposal grammar and ablation checks on a development-only model."""
import copy
import pytest


@pytest.fixture
def fixture_model():
    from feeder_agents.response_schema import ResponseDesignPlan
    from feeder_agents.response_measurement import create_model
    from test_response_permissions_m95 import plan_payload
    p=plan_payload();p['search'].update(allowed_actions=['scale_layout','scale_subtree','replace_conductor'],
        max_changed_conductors=4,max_joint_actions=2,max_layout_scale_change=.5,max_branch_length_change=.5)
    plan=ResponseDesignPlan.model_validate(p)
    return plan,create_model(plan.base_spec)


def test_atomic_edit_semantics_and_nonmutation():
    from feeder_agents.benchmark.response_proposals import atomic_edit_count,filter_single_edits
    one=dict(kind='replace_conductor',line_id='l1',conductor='a')
    batch=dict(kind='replace_conductor',replacements=[one,dict(one,conductor='b')])
    geom=dict(kind='scale_layout',factor=1.1)
    joint=dict(kind='compound',actions=[batch,geom])
    actions=[one,batch,geom,joint];before=copy.deepcopy(actions)
    assert [atomic_edit_count(a) for a in actions]==[1,2,1,3]
    assert filter_single_edits(actions)==[one,geom]
    assert actions==before


def test_single_filter_precedes_preview_quota(fixture_model,monkeypatch):
    import feeder_agents.response_design as design
    import feeder_agents.response_distribution as dist
    from feeder_agents.rules import data
    from feeder_agents.benchmark.response_proposals import atomic_edit_count
    plan,model=fixture_model
    moves=[]
    for line in model['feeder'].lines[:3]:
        name=next(n for n in dist.compatible_conductors(line,plan.base_spec,data('conductors.json')) if n!=line.conductor)
        moves.append(dict(kind='replace_conductor',line_id=line.id,conductor=name,proxy_effect=1,diagnosis='fixture'))
    batch=dict(kind='replace_conductor',replacements=moves[:2],proxy_effect=2,diagnosis='fixture batch')
    monkeypatch.setattr(design,'_distribution_options',lambda *args:[batch]+moves)
    monkeypatch.setattr(dist,'rank_distribution_actions',lambda m,p,a,actions,**kw:actions)
    search=plan.search.model_copy(update=dict(allowed_actions=['replace_conductor'],max_joint_actions=1,candidates_per_round=2))
    assessment={'base_accepted':True}
    baseline=design.propose_response_actions(model,plan.base_spec,[],assessment,search,model)
    explicit=design.propose_response_actions(model,plan.base_spec,[],assessment,search,model,candidate_filter=None)
    filtered=design.propose_response_actions(model,plan.base_spec,[],assessment,search,model,
        candidate_filter=lambda a:atomic_edit_count(a)==1)
    assert baseline==explicit
    assert len(filtered)==2 and all(atomic_edit_count(a)==1 for a in filtered)
    assert any(atomic_edit_count(a)>1 for a in baseline)


def test_random_sampler_ignores_targets_and_preserves_contract(fixture_model):
    from feeder_agents.response_baselines import random_legal_proposer
    from feeder_agents.response_design import apply_response_action,research_contract_preserved
    from feeder_agents.response_search import physical_fingerprint
    plan,model=fixture_model
    a=random_legal_proposer(912)(model,plan.base_spec,[],{'targets':['secret A']},plan.search,model)
    b=random_legal_proposer(912)(model,plan.base_spec,[],{'targets':['different B']},plan.search,model)
    assert a==b and len(a)==plan.search.candidates_per_round
    hashes=[]
    for action in a:
        changed=apply_response_action(model,plan.base_spec,action)
        assert research_contract_preserved(model,changed,plan)
        hashes.append(physical_fingerprint(changed))
    assert len(hashes)==len(set(hashes))


@pytest.mark.parametrize('kind',['scale_layout','scale_subtree','replace_conductor'])
def test_random_primitive_draw_supports_each_family(fixture_model,kind):
    import random
    from feeder_agents.response_baselines import draw_legal_primitive
    from feeder_agents.response_design import apply_response_action,research_contract_preserved
    plan,model=fixture_model
    action=draw_legal_primitive(model,model,plan.base_spec,plan.search,kind,random.Random(8))
    assert action['kind']==kind
    assert research_contract_preserved(model,apply_response_action(model,plan.base_spec,action),plan)


def test_random_catalog_batch_support_is_not_single_line_only(fixture_model):
    from feeder_agents.response_baselines import draw_legal_primitive
    class FixedDraw:
        def choice(self,items):return max(items) if all(isinstance(x,int) for x in items) else items[0]
        def sample(self,items,k):return list(items[:k])
    plan,model=fixture_model
    action=draw_legal_primitive(model,model,plan.base_spec,plan.search,'replace_conductor',FixedDraw())
    assert len(action['replacements'])==4


def test_adapter_restores_proposer_after_error():
    import feeder_agents.response_design as runner
    from feeder_agents.benchmark.response_proposals import proposal_adapter
    original=runner.propose_response_actions
    with pytest.raises(RuntimeError):
        with proposal_adapter('random',3):
            assert runner.propose_response_actions is not original
            raise RuntimeError('injected')
    assert runner.propose_response_actions is original
