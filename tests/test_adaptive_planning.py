import copy
import json
import pytest
from collections import deque

from feeder_agents.adaptive_planning import adaptive_plan


class Model:
    model_name = 'offline'

    def __init__(self, responses):
        self.responses = deque(responses)
        self.calls = 0
        self.schemas = []

    def with_structured_output(self, schema, **kwargs):
        parent = self

        class Bound:
            def invoke(self, messages):
                parent.calls += 1
                parent.schemas.append(schema.__name__)
                if schema.__name__ == 'FailureDiagnosis':
                    from planning_fixtures import decision_from_context
                    return decision_from_context(messages)
                if schema.__name__ == 'SemanticReview' and (not parent.responses or 'approved' not in parent.responses[0]):
                    return dict(approved=True, issues=[])
                return parent.invoke(messages)

        return Bound()

    def invoke(self, messages):
        result = self.responses.popleft()
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)


def proposal(users=12):
    return dict(status='ready', family='hierarchical', distribution_spec=dict(users=users),
                ledger=dict(network_kind='distribution', model_family='hierarchical', summary='用户要求',
                            requirements=[dict(id='r1', segment_ids=[0], evidence='12个用户', meaning='用户数',
                                               priority='hard', disposition='supported', target_field='hierarchy.users', expected_value=12)]))


def test_clear_request_one_call_and_ledger_preserved(tmp_path):
    model = Model([proposal()])
    result = adaptive_plan('生成12个用户', tmp_path, model)
    assert result['status'] == 'ready'
    assert result['plan']['spec']['users'] == 12
    assert result['requirement_ledger']['requirements'][0]['expected_value'] == 12
    assert model.calls == 1
    assert result['adaptive_trace']['route'] == 'direct'


def test_village_count_is_not_transformer_count():
    from feeder_agents.adaptive_planning import AdaptiveProposal, _compile
    value = proposal()
    item = value['ledger']['requirements'][0]
    item.update(evidence='聚集成3个村落', meaning='空间分群', target_field='hierarchy.transformer_count', expected_value=3)
    with pytest.raises(ValueError, match='spatial-layout'):
        _compile(AdaptiveProposal.model_validate(value), '聚集成3个村落')


def test_village_specialized_route_keeps_original_request(tmp_path):
    text = '生成一条10kv的农村配电网，30多个节点，聚集成3个村落'
    model = Model([dict(status='specialized', family='specialized', issues=['Use rural_villages spatial generator'])])
    result = adaptive_plan(text, tmp_path, model)
    assert result['status'] == 'specialized'
    assert result['request'] == text
    assert model.calls == 1


def test_mismatch_upgrades_with_bounded_correction(tmp_path):
    model = Model([proposal(15), proposal()])
    result = adaptive_plan('生成12个用户', tmp_path, model)
    assert result['status'] == 'ready'
    assert model.calls == 4  # two proposals, diagnosis, independent revalidation
    assert result['adaptive_trace']['route'] == 'focused'
    assert 'Hard requirement changed' in str(result['adaptive_trace'])


def test_repeated_failure_stops_without_relaxing_requirement(tmp_path):
    model = Model([proposal(15)] * 3)
    result = adaptive_plan('生成12个用户', tmp_path, model)
    assert result['status'] == 'planning_failed'
    assert model.calls == 3  # repeated evidence stops before a second diagnosis
    assert result['adaptive_trace']['stop_reason'] == 'repeated_failure'


def test_transport_failure_not_retried_as_design_problem(tmp_path):
    model = Model([TimeoutError('offline')])
    with pytest.raises(TimeoutError):
        adaptive_plan('生成12个用户', tmp_path, model)
    assert model.calls == 1
    trace = json.loads((tmp_path/'adaptive_trace.json').read_text())
    assert trace['stop_reason'] == 'transport_error'


def test_explicit_scope_guard_needs_no_model(tmp_path):
    model = Model([])
    result = adaptive_plan('生成8760小时时序数据', tmp_path, model)
    assert result['status'] == 'unsupported'
    assert model.calls == 0


def test_uncertainty_gets_focused_review(tmp_path):
    uncertain = proposal()
    uncertain['uncertainties'] = ['敷设条件尚待结合目录选择']
    model = Model([uncertain, proposal()])
    result = adaptive_plan('生成12个用户', tmp_path, model)
    assert result['status'] == 'ready'
    assert model.calls == 4


def test_omitted_clause_not_silently_accepted(tmp_path):
    model = Model([proposal(), proposal()])
    result = adaptive_plan('生成12个用户；无光伏', tmp_path, model)
    assert result['status'] == 'planning_failed'


def test_frontdoor_adaptive_draft_and_cache_mode(tmp_path):
    from feeder_agents.design import design_from_request
    model = Model([proposal()])
    result = design_from_request('生成12个用户', tmp_path, 'new', execute=False, model=model, planning_mode='adaptive')
    assert result['status'] == 'draft'
    assert result['adaptive_trace']['route'] == 'direct'
    result = design_from_request('生成12个用户', tmp_path, 'new', execute=False, model=model, planning_mode='adaptive')
    assert model.calls == 1
    with pytest.raises(ValueError, match='new design_id'):
        design_from_request('生成12个用户', tmp_path, 'new', execute=False, model=model)


def test_correction_cannot_erase_previous_hard_requirement(tmp_path):
    downgraded = proposal(15)
    downgraded['ledger']['requirements'][0]['priority'] = 'preference'
    model = Model([proposal(15), downgraded, downgraded])
    result = adaptive_plan('生成12个用户', tmp_path, model)
    assert result['status'] == 'planning_failed'


def test_provider_json_string_objects_are_decoded_without_extra_analysis(tmp_path):
    response = proposal()
    response['ledger'] = json.dumps(response['ledger'])
    response['distribution_spec'] = json.dumps(response['distribution_spec'])
    model = Model([response, response])
    result = adaptive_plan('生成12个用户', tmp_path, model)
    assert result['status'] == 'ready'
    assert model.calls == 1


def test_only_explicit_default_boilerplate_can_be_completed_without_llm(tmp_path):
    model = Model([proposal()])
    result = adaptive_plan('生成12个用户。其他默认。', tmp_path, model)
    assert result['status'] == 'ready'
    assert model.calls == 1
    assert result['requirement_ledger']['requirements'][-1]['priority'] == 'preference'


def test_hierarchical_spec_alias_is_representation_only(tmp_path):
    response = proposal()
    response['hierarchical_spec'] = response.pop('distribution_spec')
    model = Model([response, response])
    assert adaptive_plan('生成12个用户', tmp_path, model)['status'] == 'ready'


def test_bare_hierarchy_voltage_gets_model_stage_semantics(tmp_path):
    response = proposal()
    r = response['ledger']['requirements'][0]
    r.update(evidence='10kV', target_field='voltage_kv', expected_value=10)
    model = Model([response])
    result = adaptive_plan('生成10kV馈线', tmp_path, model)
    assert result['requirement_ledger']['requirements'][0]['target_field'] == 'hierarchy.voltage_kv'


def test_transmission_base_voltage_derived_only_when_omitted():
    from feeder_agents.adaptive_planning import AdaptiveProposal
    response = dict(status='ready', family='transmission', transmission_spec=dict(
        n_buses=10, voltage_layers=[dict(kv=110, buses=6), dict(kv=220, buses=4)]))
    assert AdaptiveProposal.model_validate(response).transmission_spec.voltage_kv == 110
    response['transmission_spec']['voltage_kv'] = 500
    with pytest.raises(ValueError):
        AdaptiveProposal.model_validate(response)


def test_transmission_layer_transformers_are_observable_in_plan():
    from feeder_agents.requirement_contract import plan_observations
    from feeder_agents.transmission import TransmissionSpec
    spec = TransmissionSpec(voltage_kv=110, n_buses=10, voltage_layers=[dict(kv=110, buses=6), dict(kv=220, buses=4)])
    obs = plan_observations(dict(plan_type='transmission', plan=dict(spec=spec.model_dump())))
    assert obs['transformers'] == 2


def test_voltage_layer_requirement_does_not_depend_on_array_order():
    from feeder_agents.requirements import RequirementLedger
    from feeder_agents.requirement_contract import audit_ledger
    layers = [dict(kv=110, buses=6), dict(kv=220, buses=4)]
    ledger = RequirementLedger(network_kind='transmission', model_family='transmission', summary='层级', requirements=[dict(
        id='layers', segment_ids=[0], evidence='双电压', meaning='层级', priority='hard', disposition='supported',
        target_field='transmission.voltage_layers', expected_value=layers)])
    assert audit_ledger(ledger, dict(voltage_layers=list(reversed(layers))))['all_satisfied']


def test_explicit_simultaneous_conflict_needs_no_model(tmp_path):
    model = Model([])
    result = adaptive_plan('同一模型同一工况总负荷必须同时严格等于100MW和160MW。', tmp_path, model)
    assert result['status'] == 'needs_clarification'
    assert model.calls == 0


def test_interlayer_transformer_prose_maps_to_physical_existence(tmp_path):
    request = '层间显式变压器'
    response = dict(status='ready', family='transmission', transmission_spec=dict(n_buses=10,
        voltage_layers=[dict(kv=110,buses=6),dict(kv=220,buses=4)]), ledger=dict(
        network_kind='transmission', model_family='transmission', summary=request, requirements=[dict(
            id='tx',segment_ids=[0],evidence=request,meaning=request,priority='hard',disposition='supported',
            target_field='voltage_layers',expected_value='auto_generated')]))
    model = Model([response, response])
    result = adaptive_plan(request, tmp_path, model)
    assert result['status'] == 'ready'
    assert result['requirement_ledger']['requirements'][0]['expected_value'] == 1
