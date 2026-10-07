from test_hierarchy_feedback import ChoiceModel
from feeder_agents.hierarchy import HierarchicalSpec,generate_hierarchy,export_hierarchy
from feeder_agents.simulation import simulate


def test_verified_memory_replay_idempotence_and_tamper(tmp_path,monkeypatch):
    import feeder_agents.hierarchy_feedback as loop
    from feeder_agents.verified_memory import VerifiedMemory
    monkeypatch.setattr(loop,'solve_isolated',simulate)
    spec=HierarchicalSpec(users=12,transformer_count=2,total_kw=48)
    f=generate_hierarchy(spec,42);f.transformers[0].kva=5.
    sim=simulate(export_hierarchy(f,tmp_path/'initial'))
    memory=VerifiedMemory(tmp_path/'memory.sqlite')
    out=loop.run_feedback(f,spec,sim,tmp_path/'feedback',model=ChoiceModel(),max_rounds=4,experience_store=memory)
    hints=memory.retrieve(spec)
    assert hints and hints[0]['improved']>0
    memory.ingest(tmp_path/'feedback',spec,out['feeder'])
    assert memory.retrieve(spec)==hints
    assert not memory.retrieve(spec.model_copy(update={'total_kw':49.}))
    (tmp_path/'feedback/candidate_0000/simulation.json').write_text('{}')
    assert not memory.retrieve(spec)


def test_agent_receives_advisory_memory(tmp_path,monkeypatch):
    import json
    import feeder_agents.hierarchy_feedback as loop
    monkeypatch.setattr(loop,'solve_isolated',simulate)
    class Store:
        def retrieve(self,spec):return [dict(kind='upgrade_transformer',trials=2,improved=2)]
        def ingest(self,*args):return 0
    class Model(ChoiceModel):
        def invoke(self,messages):
            payload=json.loads(messages[-1][1])
            assert payload['verified_experience'][0]['improved']==2
            assert all(a['kind']!='reconnect_service' for a in payload['options'].values())
            return super().invoke(messages)
    spec=HierarchicalSpec(users=12,transformer_count=2,total_kw=48)
    f=generate_hierarchy(spec,42);f.transformers[0].kva=5.
    sim=simulate(export_hierarchy(f,tmp_path/'initial'))
    model=Model()
    loop.run_feedback(f,spec,sim,tmp_path/'feedback',model=model,experience_store=Store())
    assert model.calls>0
