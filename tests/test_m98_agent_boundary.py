"""Real graph/tool-boundary regressions; only the external language model is replaced."""
import json

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class CallModel(BaseChatModel):
    call_name: str
    call_args: dict
    fail_after_tool: bool = False

    @property
    def _llm_type(self):
        return 'boundary-regression'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, **kwargs):
        start = max(i for i, m in enumerate(messages) if m.type == 'human')
        if any(m.type == 'tool' for m in messages[start:]):
            if self.fail_after_tool:
                raise RuntimeError('The external model must not be needed to deliver a verified terminal result')
            message = AIMessage(content='Tool outcome recorded.')
        else:
            message = AIMessage(content='', tool_calls=[dict(name=self.call_name, args=self.call_args,
                id='boundary_call', type='tool_call')])
        return ChatResult(generations=[ChatGeneration(message=message)])


def invoke(tmp_path, request, model):
    from feeder_agents.agent import agent_session
    with agent_session(tmp_path, 'test', model=model) as agent:
        return agent.invoke({'messages': [{'role': 'user', 'content': request}]},
            {'configurable': {'thread_id': 'boundary'}, 'recursion_limit': 64})


def test_outer_rewriting_cannot_remove_original_prohibitions(tmp_path, monkeypatch):
    import feeder_agents.design as design
    seen = []
    def capture(request, *args, **kwargs):
        seen.append(request)
        return dict(status='needs_clarification', issues=['No change has been executed.'])
    monkeypatch.setattr(design, 'design_from_request', capture)
    source = 'Generate a 23-bus 10 kV rural feeder. Preserve equipment and geometry; no conductor replacement.'
    invoke(tmp_path, source, CallModel(call_name='design_from_language', call_args={
        'request': 'Generate a feeder. Allow conductor replacement.', 'design_id': 'source'}))
    assert seen == [source]


def test_mentioning_design_in_followup_does_not_discard_original_source():
    from feeder_agents.agent_boundary import source_request
    original = 'Generate a feeder. Preserve equipment and geometry.'
    followup = 'Try harder on the design; this grants no new permission.'
    assert original in source_request([original, followup])
    assert followup in source_request([original, followup])
    assert source_request([original, 'Now generate a new 10-bus grid.']) == 'Now generate a new 10-bus grid.'


def test_raw_generation_cannot_bypass_natural_language_contract(tmp_path):
    result = invoke(tmp_path, 'Generate a 23-bus feeder and verify existing PV off/on.',
        CallModel(call_name='generate_cases', call_args={'experiment_id': 'bypass',
            'spec': {'n_buses': 5, 'n_loads_min': 4, 'n_loads_max': 4}}))
    tool = next(m for m in result['messages'] if m.type == 'tool')
    assert json.loads(tool.content)['status'] == 'needs_source_bound_tool'
    assert not (tmp_path/'projects/test/experiments/bypass').exists()


def test_generated_revision_identifier_is_safe_before_tool_execution(tmp_path, monkeypatch):
    import re
    import feeder_agents.revisions as revisions
    seen=[]
    def capture(root, parent_id, revision_id, *args, **kwargs):
        seen.append(revision_id)
        return dict(status='blocked', verified_report='Captured the prepared revision identifier.')
    monkeypatch.setattr(revisions, 'revise_case', capture)
    invoke(tmp_path, 'Scale the existing model demand by1.1; preserve the topology.', CallModel(
        call_name='revise_existing_case', call_args={'parent_id': 'parent',
            'revision_id': 'rev_scale_1.1x', 'feedback': 'Scale demand by1.1.'}))
    assert len(seen)==1 and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',seen[0])
    assert seen[0]!='rev_scale_1.1x'


def test_explicit_invalid_user_identifier_requires_clarification(tmp_path, monkeypatch):
    import feeder_agents.revisions as revisions
    seen=[]
    monkeypatch.setattr(revisions, 'revise_case', lambda *a, **k: seen.append(a) or {})
    result=invoke(tmp_path, 'Scale demand and save the revision with ID rev_scale_1.1x.', CallModel(
        call_name='revise_existing_case', call_args={'parent_id': 'parent',
            'revision_id': 'rev_scale_1.1x', 'feedback': 'Scale demand.'}))
    assert not seen
    assert json.loads(next(m.content for m in result['messages'] if m.type=='tool'))['status']=='needs_clarification'


def test_missing_result_returns_actionable_tool_error_without_crashing_graph(tmp_path):
    result = invoke(tmp_path, 'Read saved result missing_case.', CallModel(
        call_name='read_experiment_result', call_args={'experiment_id': 'missing_case'}))
    payload = json.loads(next(m.content for m in result['messages'] if m.type == 'tool'))
    assert payload['status'] == 'tool_error'
    assert payload['error_type'] == 'FileNotFoundError'
    assert payload['retryable'] is False
    assert result['messages'][-1].type == 'ai'


def test_reversed_response_bounds_block_even_a_rewritten_ordinary_tool(tmp_path, monkeypatch):
    import feeder_agents.design as design
    seen = []
    monkeypatch.setattr(design, 'design_from_request', lambda *a, **k: seen.append(a) or {})
    result = invoke(tmp_path,
        'Generate a feeder with response lower bound 1.3 and upper bound 1.1 times baseline.',
        CallModel(call_name='design_from_language', call_args={
            'request': 'Generate a feeder with response between 1.1 and 1.3.', 'design_id': 'contradiction'}))
    assert not seen
    payload = json.loads(next(m.content for m in result['messages'] if m.type == 'tool'))
    assert payload['status'] == 'needs_clarification'


def tiny_response(tmp_path, lower=.98, upper=1.02):
    from feeder_agents.response_design import run_response_design
    return run_response_design(dict(research_question='Reader regression', base_spec=dict(
        network_kind='distribution', n_buses=5, n_loads_min=4, n_loads_max=4, total_kw_min=80, total_kw_max=80,
        voltage_kv=10, repair_policy={'strategy': 'none'}),
        probes=[dict(id='q', kind='voltage_q')],
        targets=[dict(probe_id='q', reference='baseline_ratio', lower=lower, upper=upper)],
        search=dict(allowed_actions=[])), tmp_path, 'response_case')


def test_legacy_reader_dispatches_actual_response_artifact(tmp_path):
    from feeder_agents.agent import make_tools
    output = tiny_response(tmp_path)
    tools = {t.name: t for t in make_tools(tmp_path, 'test')}
    result = tools['read_experiment_result'].invoke({'experiment_id': 'response_case'})
    assert result['target_met'] is True
    assert result['directory'] == output['directory']
    assert result['artifact_kind'] == 'electrical_response'


def test_reader_preserves_response_hash_verification(tmp_path):
    from feeder_agents.agent import make_tools
    tiny_response(tmp_path)
    path = tmp_path/'response_designs/response_case/selected/feeder.json'
    data = json.loads(path.read_text()); data['voltage_kv'] = 20
    path.write_text(json.dumps(data))
    tools = {t.name: t for t in make_tools(tmp_path, 'test')}
    with pytest.raises(ValueError, match='hash|Hash|changed|mismatch'):
        tools['read_experiment_result'].invoke({'experiment_id': 'response_case'})


def test_completed_study_keeps_output_without_unverified_terminal_delivery(tmp_path):
    from feeder_agents.agent_results import design_output
    directory = tmp_path/'studies/paired'
    directory.mkdir(parents=True)
    output = dict(status='completed', outcome={'directory': str(directory), 'base_cases': 1},
        verified_report='Paired study completed; inspect its condition reports.')
    result = design_output(tmp_path, output)
    assert result == output
    assert 'delivery' not in result


def test_result_reader_rejects_symlink_escape(tmp_path):
    from feeder_agents.agent_results import read_grid_result
    root = tmp_path/'project'; (root/'experiments').mkdir(parents=True)
    external = tmp_path/'external'; external.mkdir()
    (root/'experiments/escaped').symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match='escaped'):
        read_grid_result(root, 'escaped')


def test_verified_design_is_delivered_without_another_model_call(tmp_path, monkeypatch):
    from feeder_agents.workflow import run_experiment
    from feeder_agents.schemas import ExperimentSpec
    import feeder_agents.design as design
    def actual_case(request, root, design_id, *args, **kwargs):
        output = run_experiment(ExperimentSpec(n_buses=5, n_loads_min=4, n_loads_max=4,
            total_kw_min=80, total_kw_max=80, repair_policy={'strategy': 'none'}), root, design_id)
        return dict(status='completed', outcome={k: output[k] for k in
            ('directory', 'attempted', 'accepted')}, verified_report=output['verified_report'])
    monkeypatch.setattr(design, 'design_from_request', actual_case)
    result = invoke(tmp_path, 'Generate a 5-bus 10 kV feeder with 80 kW demand.',
        CallModel(call_name='design_from_language', call_args={
            'request': 'Generate a 5-bus feeder.', 'design_id': 'delivered'}, fail_after_tool=True))
    final = result['messages'][-1]
    assert final.type == 'ai'
    assert 'delivered' in final.content
    assert (tmp_path/'projects/test/experiments/delivered/sample_00000/opendss/Master.dss').exists()


def test_verified_first_model_does_not_end_a_multistep_request():
    from feeder_agents.agent_boundary import OriginalRequestMiddleware
    from langchain_core.messages import HumanMessage, ToolMessage
    state = {'messages': [HumanMessage(content='Generate a model, then create two local revisions and compare all three.'),
        ToolMessage(content=json.dumps({'status': 'completed', 'delivery': {'ready': True,
            'directory': '/project/first', 'result_id': 'first'}}), tool_call_id='base')]}
    assert OriginalRequestMiddleware('/project').before_model(state, None) is None


def test_unmet_response_with_no_allowed_actions_finishes_truthfully(tmp_path, monkeypatch):
    import feeder_agents.design as design
    def unadjustable(request, root, design_id, **kwargs):
        output = tiny_response(root, lower=1.1, upper=1.3)
        assert not output['target_met']
        return dict(status='target_not_met', outcome={'directory': output['directory']},
            verified_report=output['verified_report'])
    monkeypatch.setattr(design, 'design_from_request', unadjustable)
    result = invoke(tmp_path, 'Generate a feeder with response1.1–1.3 times baseline; permit no edits.',
        CallModel(call_name='design_electrical_response', call_args={'request': 'Generate a feeder.',
            'design_id': 'fixed'}, fail_after_tool=True))
    final = result['messages'][-1]
    assert 'not met' in final.content.lower()
    assert 'Verified model ready' not in final.content


def test_transmission_revision_cannot_drop_targets_or_enable_forbidden_repairs(tmp_path, monkeypatch):
    import feeder_agents.transmission as transmission
    seen = []
    monkeypatch.setattr(transmission, 'revise_transmission', lambda *a, **k: seen.append((a, k)) or {})
    result = invoke(tmp_path, 'Try harder on saved t; preserve targets and topology, no new repairs.',
        CallModel(call_name='revise_existing_transmission', call_args={'parent_id': 't',
            'revision_id': 'bad', 'feedback': 'Allow changes', 'targets': [],
            'allowed_repairs': ['relocate_corridor'], 'allow_topology_changes': True}))
    assert not seen
    tool = next(m for m in result['messages'] if m.type == 'tool')
    assert json.loads(tool.content)['status'] == 'needs_clarification'


def test_followup_permission_cannot_be_invented_by_model(tmp_path):
    from feeder_agents.agent import agent_session
    root = tmp_path/'projects/test'; tiny_response(root)
    config = {'configurable': {'thread_id': 'permissions'}, 'recursion_limit': 64}
    with agent_session(tmp_path, 'test', model=CallModel(call_name='read_experiment_result',
            call_args={'experiment_id': 'response_case'})) as agent:
        agent.invoke({'messages': [{'role': 'user', 'content': 'Read response_case.'}]}, config)
    with agent_session(tmp_path, 'test', model=CallModel(call_name='revise_electrical_response_permissions',
            call_args={'parent_id': 'response_case', 'design_id': 'unauthorized', 'execute': True,
                'permissions': {'tier': 'equipment', 'max_changed_conductors': 2}})) as agent:
        result = agent.invoke({'messages': [{'role': 'user', 'content':
            'Try harder. I grant no new permissions; do not replace conductors or change geometry.'}]}, config)
    last_tool = next(m for m in reversed(result['messages']) if m.type == 'tool')
    assert json.loads(last_tool.content)['status'] == 'needs_clarification'
    assert not (root/'permission_ladders/unauthorized').exists()


def test_permission_tool_exposes_actual_grant_schema(tmp_path):
    from feeder_agents.agent import make_tools
    from langchain_core.utils.function_calling import convert_to_openai_tool
    selected = next(t for t in make_tools(tmp_path, 'test') if t.name == 'revise_electrical_response_permissions')
    schema = convert_to_openai_tool(selected)['function']['parameters']['properties']['permissions']
    assert schema['properties']['tier']['enum'] == ['equipment', 'equipment_topology', 'equipment_geometry', 'geometry']
    assert 'tier' in schema['required']
    assert schema['additionalProperties'] is False
    assert 'max_branch_length_change' in schema['properties']


def test_revision_uses_latest_delivered_parent_unless_user_names_another(tmp_path, monkeypatch):
    import feeder_agents.revisions as revisions
    from feeder_agents.agent import agent_session
    from feeder_agents.workflow import run_experiment
    from feeder_agents.schemas import ExperimentSpec
    root = tmp_path/'projects/test'
    run_experiment(ExperimentSpec(n_buses=5, n_loads_min=4, n_loads_max=4,
        total_kw_min=80, total_kw_max=80), root, 'newer_revision')
    config = {'configurable': {'thread_id': 'parent'}, 'recursion_limit': 64}
    with agent_session(tmp_path, 'test', model=CallModel(call_name='read_experiment_result',
            call_args={'experiment_id': 'newer_revision'})) as agent:
        agent.invoke({'messages': [{'role': 'user', 'content': 'Read newer_revision.'}]}, config)
    seen = []
    def capture(root, parent_id, revision_id, *args, **kwargs):
        seen.append((parent_id, kwargs['feedback']))
        return dict(status='blocked', verified_report='No edit needed for this boundary test.')
    monkeypatch.setattr(revisions, 'revise_case', capture)
    with agent_session(tmp_path, 'test', model=CallModel(call_name='revise_existing_case',
            call_args={'parent_id': 'old_original', 'revision_id': 'next', 'feedback': 'Ignore preservation.'})) as agent:
        agent.invoke({'messages': [{'role': 'user', 'content':
            'Now increase its PV ratio to 0.6; preserve everything else.'}]}, config)
    assert seen == [('newer_revision', 'Now increase its PV ratio to 0.6; preserve everything else.')]
