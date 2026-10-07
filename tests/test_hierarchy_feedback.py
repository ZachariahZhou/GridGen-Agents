from feeder_agents.hierarchy import HierarchicalSpec,generate_hierarchy

class ChoiceModel:
    def __init__(self,decision='repair',action_id='a0'):self.calls=0;self.decision=decision;self.action_id=action_id
    def with_structured_output(self,*args,**kwargs):return self
    def invoke(self,messages):
        self.calls+=1
        return dict(decision=self.decision,action_id=self.action_id if self.decision=='repair' else None,diagnosis='Measured equipment capacity violation',reason='Select a sourced upgrade')


def test_passed_feeder_never_calls_agent(tmp_path,monkeypatch):
    from feeder_agents.hierarchy_feedback import run_feedback
    from feeder_agents.simulation import export_dss
    from feeder_agents.hierarchy import export_hierarchy,evaluate_hierarchy
    from feeder_agents.simulation import simulate
    spec=HierarchicalSpec(users=12,transformer_count=2,total_kw=48)
    f=generate_hierarchy(spec,42);sim=simulate(export_hierarchy(f,tmp_path/'initial'))
    model=ChoiceModel()
    out=run_feedback(f,spec,sim,tmp_path/'feedback',model=model)
    assert out['checks']['accepted'] and model.calls==0


def test_agent_selects_repair_and_next_evaluation_accepts(tmp_path,monkeypatch):
    import feeder_agents.hierarchy_feedback as module
    from feeder_agents.hierarchy import export_hierarchy
    from feeder_agents.simulation import simulate
    spec=HierarchicalSpec(users=12,transformer_count=2,total_kw=48)
    f=generate_hierarchy(spec,42)
    f.transformers[0].kva=5
    sim=simulate(export_hierarchy(f,tmp_path/'initial'))
    monkeypatch.setattr(module,'solve_isolated',simulate)
    model=ChoiceModel()
    out=module.run_feedback(f,spec,sim,tmp_path/'feedback',model=model,max_rounds=4)
    assert model.calls>0 and out['checks']['accepted']
    assert out['report']['accepted_steps']>0
    assert out['report']['contract_preserved']
    assert out['feeder'].loads==f.loads


def test_invented_action_is_rejected_without_network_change(tmp_path,monkeypatch):
    import feeder_agents.hierarchy_feedback as module
    from feeder_agents.hierarchy import export_hierarchy
    from feeder_agents.simulation import simulate
    spec=HierarchicalSpec(users=12,transformer_count=2,total_kw=48)
    f=generate_hierarchy(spec,42);f.transformers[0].kva=5
    sim=simulate(export_hierarchy(f,tmp_path/'initial'))
    out=module.run_feedback(f,spec,sim,tmp_path/'feedback',model=ChoiceModel(action_id='invented'))
    assert not out['checks']['accepted']
    assert out['report']['stop_reason']=='agent_error'
    assert out['feeder']==f


def test_workflow_publishes_repaired_model_and_replays_saved_actions(tmp_path,monkeypatch):
    import feeder_agents.hierarchy_workflow as workflow
    import feeder_agents.hierarchy_feedback as feedback
    from feeder_agents.simulation import simulate
    spec=HierarchicalSpec(users=12,transformer_count=2,total_kw=48)
    f=generate_hierarchy(spec,42);f.transformers[0].kva=5
    monkeypatch.setattr(workflow,'generate_hierarchy',lambda *args:f)
    monkeypatch.setattr(workflow,'solve_isolated',simulate)
    monkeypatch.setattr(feedback,'solve_isolated',simulate)
    result=workflow.run_hierarchy(spec,tmp_path,'loop',agent_feedback=True,feedback_model=ChoiceModel())
    assert result['accepted']==1
    assert result['samples'][0]['feedback']['accepted_steps']>0
    assert workflow.read_hierarchy_result(result['directory'])['accepted']==1


def test_stop_and_budget_preserve_unaccepted_network(tmp_path,monkeypatch):
    import feeder_agents.hierarchy_feedback as module
    from feeder_agents.hierarchy import export_hierarchy
    from feeder_agents.simulation import simulate
    spec=HierarchicalSpec(users=12,transformer_count=2,total_kw=48)
    f=generate_hierarchy(spec,42);f.transformers[0].kva=5
    sim=simulate(export_hierarchy(f,tmp_path/'initial'))
    for name,model,rounds,expected in [('stop',ChoiceModel('clarify'),3,'needs_clarification'),('budget',ChoiceModel(),0,'round_limit')]:
        out=module.run_feedback(f,spec,sim,tmp_path/name,model=model,max_rounds=rounds)
        assert out['report']['stop_reason']==expected and out['feeder']==f
        assert not out['checks']['accepted']


def test_rejected_candidates_do_not_replace_current_model(tmp_path,monkeypatch):
    import feeder_agents.hierarchy_feedback as module
    from feeder_agents.hierarchy import export_hierarchy
    from feeder_agents.simulation import simulate
    spec=HierarchicalSpec(users=12,transformer_count=2,total_kw=48)
    f=generate_hierarchy(spec,42)
    for tx in f.transformers:tx.kva=5
    sim=simulate(export_hierarchy(f,tmp_path/'initial'))
    monkeypatch.setattr(module,'solve_isolated',simulate)
    monkeypatch.setattr(module,'acceptable_step',lambda before,after:False)
    model=ChoiceModel()
    out=module.run_feedback(f,spec,sim,tmp_path/'feedback',model=model,max_rounds=2)
    assert model.calls==2 and out['report']['stop_reason']=='round_limit'
    assert out['feeder']==f and out['report']['accepted_steps']==0
    assert len({e['candidate'] for e in out['report']['trace']})==2
