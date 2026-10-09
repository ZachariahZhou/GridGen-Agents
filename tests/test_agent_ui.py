import json

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from feeder_agents.agent import agent_session


class ScriptedModel(BaseChatModel):
    """Only the external model is replaced; tools, graph and solver run for real."""
    @property
    def _llm_type(self):
        return 'scripted-test-model'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if any(getattr(m, 'type', '') == 'tool' for m in messages):
            message = AIMessage(content='\u5df2\u751f\u6210\uff0c\u7ed3\u679c\u4ee5\u5de5\u5177\u8fd4\u56de\u7684\u9a8c\u8bc1\u62a5\u544a\u4e3a\u51c6\u3002')
        else:
            message = AIMessage(content='', tool_calls=[{
                'name': 'generate_cases', 'id': 'call_test', 'type': 'tool_call',
                'args': {'experiment_id': 'agent_test', 'spec': {
                    'count': 1, 'n_loads_min': 4, 'n_loads_max': 4,
                    'total_kw_min': 200, 'total_kw_max': 200}}
            }])
        return ChatResult(generations=[ChatGeneration(message=message)])


def test_langchain_agent_runs_real_tool_and_retains_thread(tmp_path):
    with agent_session(tmp_path, 'project_a', model=ScriptedModel()) as agent:
        result = agent.invoke({'messages': [{'role': 'user', 'content': 'Generate cases'}]},
                              {'configurable': {'thread_id': 'thread1'}})
        tool_messages = [m for m in result['messages'] if m.type == 'tool']
        assert tool_messages
        payload = json.loads(tool_messages[0].content)
        assert payload['accepted'] == 1
    assert (tmp_path / 'projects/project_a/experiments/agent_test/sample_00000/opendss/Master.dss').exists()
    with agent_session(tmp_path, 'project_a', model=ScriptedModel()) as agent:
        state = agent.get_state({'configurable': {'thread_id': 'thread1'}})
        assert any(m.type == 'tool' for m in state.values['messages'])
    with agent_session(tmp_path, 'project_b', model=ScriptedModel()) as agent:
        assert not agent.get_state({'configurable': {'thread_id': 'thread1'}}).values


def test_ui_loads_without_api_key():
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / 'app.py').run(timeout=20)
    assert not app.exception
    assert any('GridGen-Agents' in item.value for item in app.title)


def test_natural_design_error_is_recorded_with_support_id(tmp_path, monkeypatch):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    import feeder_agents.design as design
    monkeypatch.chdir(tmp_path)
    def broken(*args, **kwargs):
        raise TypeError("unexpected keyword argument 'planning_mode'")
    monkeypatch.setattr(design, 'design_from_request', broken)
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / 'app.py').run(timeout=20)
    next(w for w in app.text_area if w.label == 'Design request').set_value('\u751f\u6210\u4e00\u676110kv\u7684\u519c\u6751\u914d\u7535\u7f51\uff0c30\u591a\u4e2a\u8282\u70b9\uff0c\u805a\u96c6\u62103\u4e2a\u6751\u843d')
    next(b for b in app.button if b.label == 'Generate from request').click().run(timeout=20)
    assert not app.exception
    records = list((tmp_path / 'workspace/ui_errors').glob('*.json'))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record['error_type'] == 'TypeError'
    assert 'planning_mode' in record['traceback']
    assert any(record['error_id'] in x.value for x in app.error)


def test_ui_form_generates_real_case_without_model_credentials(tmp_path, monkeypatch):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    entrypoint = Path(__file__).resolve().parents[1] / 'app.py'
    monkeypatch.chdir(tmp_path)
    from feeder_agents.workflow import run_experiment
    from feeder_agents.schemas import ExperimentSpec
    run_experiment(ExperimentSpec(n_loads_min=4,n_loads_max=4,total_kw_min=200,total_kw_max=200),tmp_path/'workspace','aa_old')
    app = AppTest.from_file(entrypoint).run(timeout=20)
    for widget in app.number_input:
        if widget.label == 'Generation attempts':
            widget.set_value(1)
        elif widget.label in {'Minimum load points', 'Maximum load points'}:
            widget.set_value(4)
    for widget in app.text_input:
        if widget.label == 'Experiment ID (resume with identical settings)':
            widget.set_value('ui_case')
    next(button for button in app.button if button.label == 'Generate and validate').click().run(timeout=45)
    assert not app.exception
    assert app.success
    summary = json.loads((tmp_path / 'workspace/experiments/ui_case/summary.json').read_text())
    assert summary['attempted'] == summary['accepted'] == 1
    assert 'ui_case' in str(next(w for w in app.selectbox if w.label=='Select experiment').value)


class MultiStepModel(ScriptedModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        steps = sum(m.type == 'tool' for m in messages)
        message = AIMessage(content='done') if steps == 6 else AIMessage(content='', tool_calls=[{
            'name': 'read_project_memory', 'id': f'call_{steps}', 'type': 'tool_call', 'args': {}}])
        return ChatResult(generations=[ChatGeneration(message=message)])


def test_planning_length_conversation_fits_graph_budget(tmp_path):
    with agent_session(tmp_path, model=MultiStepModel()) as agent:
        result = agent.invoke({'messages': [{'role': 'user', 'content': 'plan then execute'}]},
                              {'configurable': {'thread_id': 'long'}, 'recursion_limit': 64})
        assert result['messages'][-1].content == 'done'


def test_new_design_selects_its_own_view_and_allows_sample_switch(tmp_path, monkeypatch):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    import feeder_agents.design as design
    entrypoint=Path(__file__).resolve().parents[1]/'app.py'
    monkeypatch.chdir(tmp_path)

    from feeder_agents.artifacts import digest
    from feeder_agents.schemas import ExperimentSpec
    from feeder_agents.workflow import run_experiment

    def saved(root, name):
        folder=root/'designs'/name
        folder.mkdir(parents=True)
        outcome=run_experiment(ExperimentSpec(count=2,n_loads_min=4,n_loads_max=4,
            total_kw_min=200,total_kw_max=200,seed=17 if name=='z_old' else 29),root,name)
        brief={'plan_type':'feeder','status':'ready'}
        (folder/'brief.json').write_text(json.dumps({**brief,'brief_hash':digest(brief)}))
        result={'status':'completed','verified_report':outcome['verified_report'],'outcome':outcome}
        (folder/'result.json').write_text(json.dumps(result))
        return result

    root=tmp_path/'workspace/projects/default'
    saved(root,'z_old')
    monkeypatch.setattr(design,'design_from_request',lambda request,project_root,design_id,**kwargs:saved(project_root,design_id))
    app=AppTest.from_file(entrypoint).run(timeout=20)
    next(w for w in app.text_input if w.label=='Design ID').set_value('a_new')
    next(w for w in app.text_area if w.label=='Design request').set_value('\u751f\u6210\u65b0\u6848\u4f8b')
    next(b for b in app.button if b.label=='Generate from request').click().run(timeout=20)
    assert not app.exception
    selected=next(w for w in app.selectbox if w.label=='Select design')
    assert 'a_new' in str(selected.value)
    sample=next(w for w in app.selectbox if w.label=='Select design sample')
    sample.set_value('sample_00001').run(timeout=20)
    assert not app.exception
    assert any('a_new' in c.value and 'sample_00001' in c.value for c in app.caption)
    assert app.get('iframe')
    new_view=app.get('iframe')[0].proto.srcdoc
    selected=next(w for w in app.selectbox if w.label=='Select design')
    selected.set_value(str(root/'designs/z_old/result.json')).run(timeout=20)
    assert not app.exception
    assert app.get('iframe')
    assert any('z_old' in c.value and 'sample_00000' in c.value for c in app.caption)
    assert app.get('iframe')[0].proto.srcdoc!=new_view
