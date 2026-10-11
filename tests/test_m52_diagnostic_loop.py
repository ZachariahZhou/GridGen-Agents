"""Failure evidence must control changes, never grant acceptance authority."""
import copy
import json

import pytest

from feeder_agents.adaptive_planning import AdaptiveProposal, _compile, adaptive_plan


def proposal(users=12):
    return dict(status='ready', family='hierarchical', distribution_spec=dict(users=users),
                ledger=dict(network_kind='distribution', model_family='hierarchical', summary='\u7528\u6237\u8981\u6c42', requirements=[
                    dict(id='users', segment_ids=[0], evidence='12\u4e2a\u7528\u6237', meaning='\u7528\u6237\u6570',
                         priority='hard', disposition='supported', target_field='hierarchy.users', expected_value=12)]))


def decision_from_context(messages):
    context = json.loads(messages[-1][1])
    action = context['allowed_actions'][0]
    return dict(action_id=action['id'], evidence_ids=[f['id'] for f in context['facts']],
                reason='\u68c0\u67e5\u7ed9\u51fa\u4e86\u5177\u4f53\u5931\u8d25\u5b57\u6bb5\u6216\u7f3a\u5931\u539f\u6587\uff0c\u6309\u9650\u5b9a\u52a8\u4f5c\u4fee\u6b63\u3002',
                expected_change='\u7ea0\u6b63\u8bc1\u636e\u6240\u6307\u7684\u9519\u8bef\u5e76\u91cd\u65b0\u9a8c\u6536\u539f\u59cb\u9700\u6c42\u3002')


class Model:
    model_name = 'offline-diagnostic-test'

    def __init__(self, responses, diagnosis=None):
        self.responses = iter(responses)
        self.calls = []
        self.diagnosis = diagnosis

    def with_structured_output(self, schema, **kwargs):
        parent = self

        class Bound:
            def invoke(self, messages):
                parent.calls.append(schema.__name__)
                if schema.__name__ == 'FailureDiagnosis':
                    if isinstance(parent.diagnosis, Exception):
                        raise parent.diagnosis
                    result = parent.diagnosis or decision_from_context(messages)
                elif schema.__name__ == 'SemanticReview':
                    result = dict(approved=True, issues=[])
                else:
                    result = next(parent.responses)
                return copy.deepcopy(result)

        return Bound()


def analyses(root):
    return [json.loads(p.read_text()) for p in sorted((root/'failure_analysis').glob('attempt_*.json'))]


def test_missing_clause_has_machine_evidence():
    from feeder_agents.planning_diagnostics import PlanningValidationError
    with pytest.raises(PlanningValidationError) as exc:
        _compile(AdaptiveProposal.model_validate(proposal()), '12\u4e2a\u7528\u6237\uff1b\u65e0\u5149\u4f0f')
    assert exc.value.code == 'missing_segments'
    assert exc.value.facts[0]['source_quote'] == '\u65e0\u5149\u4f0f'
    assert exc.value.facts[0]['segment_id'] == 1


def test_mismatch_diagnosis_records_adjustment_and_revalidation(tmp_path):
    model = Model([proposal(15), proposal()])
    result = adaptive_plan('12\u4e2a\u7528\u6237', tmp_path, model)
    assert result['status'] == 'ready'
    record = analyses(tmp_path)[0]
    assert record['decision']['action_id'] == 'align_spec'
    fact = record['facts'][0]
    assert fact['expected'] == 12 and fact['observed'] == 15 and fact['operator'] == 'eq'
    assert record['verification']['status'] == 'passed'
    assert 'distribution_spec.users' in [x['path'] for x in record['adjustment']['changes']]
    assert model.calls.count('FailureDiagnosis') == 1


def test_missing_negative_requirement_is_added_without_other_changes(tmp_path):
    fixed = proposal()
    fixed['distribution_spec']['pv_ratio'] = 0
    fixed['ledger']['requirements'].append(dict(id='pv', segment_ids=[1], evidence='\u65e0\u5149\u4f0f', meaning='\u7981\u6b62\u5149\u4f0f',
        priority='hard', disposition='supported', target_field='hierarchy.pv_ratio', expected_value=0))
    result = adaptive_plan('12\u4e2a\u7528\u6237\uff1b\u65e0\u5149\u4f0f', tmp_path, Model([proposal(), fixed]))
    assert result['status'] == 'ready' and result['plan']['spec']['pv_ratio'] == 0
    record = analyses(tmp_path)[0]
    assert record['decision']['action_id'] == 'complete_ledger'
    assert record['verification']['status'] == 'passed'
    assert any(e['stage'] == 'semantic_review' for e in result['adaptive_trace']['events'])


def test_scoped_repair_rejects_unrelated_load_change(tmp_path):
    fixed = proposal()
    fixed['distribution_spec']['total_kw'] = 12
    result = adaptive_plan('12\u4e2a\u7528\u6237', tmp_path, Model([proposal(15), fixed]))
    assert result['status'] == 'planning_failed'
    assert result['adaptive_trace']['stop_reason'] == 'adjustment_scope_violation'
    assert analyses(tmp_path)[0]['verification']['status'] == 'failed'


@pytest.mark.parametrize('override', [dict(evidence_ids=['invented']), dict(action_id='override_validator')])
def test_invented_diagnostic_authority_is_rejected(tmp_path, override):
    decision = dict(action_id='align_spec', evidence_ids=['F1'], reason='\u6709\u4f9d\u636e', expected_change='\u8c03\u6574\u6570\u91cf')
    decision.update(override)
    model = Model([proposal(15)], diagnosis=decision)
    result = adaptive_plan('12\u4e2a\u7528\u6237', tmp_path, model)
    assert result['status'] == 'planning_failed'
    assert result['adaptive_trace']['stop_reason'] == 'invalid_diagnosis'
    assert model.calls.count('AdaptiveProposal') == 1


def test_opaque_validator_failure_stops_without_model_guess(monkeypatch, tmp_path):
    def broken(*args):
        raise ValueError('validator internal mismatch')
    monkeypatch.setattr('feeder_agents.adaptive_planning._compile', broken)
    model = Model([proposal()])
    result = adaptive_plan('12\u4e2a\u7528\u6237', tmp_path, model)
    assert result['status'] == 'planning_failed'
    assert result['adaptive_trace']['stop_reason'] == 'unclassified_validation_failure'
    assert model.calls == ['AdaptiveProposal']


def test_diagnostic_transport_failure_stops_and_records(tmp_path):
    model = Model([proposal(15)], diagnosis=TimeoutError('diagnosis timeout'))
    result = adaptive_plan('12\u4e2a\u7528\u6237', tmp_path, model)
    assert result['status'] == 'planning_failed'
    assert result['adaptive_trace']['stop_reason'] == 'diagnosis_error'
    assert analyses(tmp_path)[0]['diagnostic_error']['type'] == 'TimeoutError'


def test_schema_extra_field_repaired_with_auditable_location(tmp_path):
    broken = proposal()
    broken['distribution_spec']['deliverables'] = ['opendss']
    result = adaptive_plan('12\u4e2a\u7528\u6237', tmp_path, Model([broken, proposal()]))
    assert result['status'] == 'ready'
    record = analyses(tmp_path)[0]
    assert record['decision']['action_id'] == 'repair_representation'
    assert record['facts'][0]['location'] == ['distribution_spec', 'deliverables']
    assert record['verification']['status'] == 'passed'


def test_repeated_failure_does_not_start_another_diagnosis(tmp_path):
    model = Model([proposal(15), proposal(15)])
    result = adaptive_plan('12\u4e2a\u7528\u6237', tmp_path, model)
    assert result['status'] == 'planning_failed'
    assert result['adaptive_trace']['stop_reason'] == 'repeated_failure'
    assert model.calls == ['AdaptiveProposal', 'FailureDiagnosis', 'AdaptiveProposal']
    assert analyses(tmp_path)[0]['verification']['status'] == 'failed'


def test_valid_first_proposal_has_no_diagnostic_overhead(tmp_path):
    model = Model([proposal()])
    assert adaptive_plan('12\u4e2a\u7528\u6237', tmp_path, model)['status'] == 'ready'
    assert model.calls == ['AdaptiveProposal'] and not analyses(tmp_path)


@pytest.mark.parametrize('schema_failure', [False, True])
def test_failed_intermediate_candidate_cannot_reset_adjustment_boundary(tmp_path, schema_failure):
    middle = proposal(14)
    middle['distribution_spec']['total_kw'] = 12
    if schema_failure:
        middle['distribution_spec']['deliverables'] = ['opendss']
    final = proposal()
    final['distribution_spec']['total_kw'] = 12
    result = adaptive_plan('12\u4e2a\u7528\u6237', tmp_path, Model([proposal(15), middle, final]))
    assert result['status'] == 'planning_failed'
    assert result['adaptive_trace']['stop_reason'] == 'adjustment_scope_violation'


@pytest.mark.parametrize('representation', ['dict', 'string', 'alias', 'root_error'])
def test_schema_repair_cannot_change_unspecified_load_or_threshold(tmp_path, representation):
    broken = proposal()
    if representation == 'root_error':
        broken['distribution_spec']['n_buses'] = 26
    else:
        broken['distribution_spec']['deliverables'] = ['opendss']
    if representation == 'string':
        broken['distribution_spec'] = json.dumps(broken['distribution_spec'])
    if representation == 'alias':
        broken['hierarchical_spec'] = broken.pop('distribution_spec')
    changed = proposal()
    changed['distribution_spec'].update(total_kw=12, lv_voltage_min_pu=.6)
    result = adaptive_plan('12\u4e2a\u7528\u6237', tmp_path, Model([broken, changed]))
    assert result['status'] == 'planning_failed'


def test_transmission_topology_repair_allows_schema_derived_edges(tmp_path):
    p = dict(status='ready', family='transmission', transmission_spec=dict(topology='meshed'),
             ledger=dict(network_kind='transmission',model_family='transmission',summary='\u8f93\u7535\u73af\u7f51',requirements=[
                 dict(id='topology',segment_ids=[0],evidence='\u8f93\u7535\u73af\u7f51',meaning='\u73af\u72b6',priority='hard',
                      disposition='supported',target_field='transmission.topology',expected_value='ring')]))
    fixed=copy.deepcopy(p)
    fixed['transmission_spec']['topology']='ring'
    result=adaptive_plan('\u751f\u6210\u8f93\u7535\u73af\u7f51',tmp_path,Model([p,fixed]))
    assert result['status']=='ready' and result['plan']['spec']['extra_edges']==0


def test_internal_compile_typeerror_is_not_reported_as_review_transport(monkeypatch, tmp_path):
    def broken(*args):raise TypeError('compile bug')
    monkeypatch.setattr('feeder_agents.adaptive_planning._compile',broken)
    with pytest.raises(TypeError):adaptive_plan('12\u4e2a\u7528\u6237',tmp_path,Model([proposal()]))
    trace=json.loads((tmp_path/'adaptive_trace.json').read_text())
    assert trace['stop_reason']=='compile_error'
    assert trace['events'][-1]['error_type']=='TypeError'


def test_schema_diagnosis_distinguishes_spec_fields_from_delivery_contract():
    from feeder_agents.planning_diagnostics import failure_bundle
    from feeder_agents.structured_planning import StructuredPlanningError
    from pydantic import ValidationError
    p=proposal()
    p['distribution_spec']['deliverables']=['opendss']
    with pytest.raises(ValidationError) as caught:AdaptiveProposal.model_validate(p)
    bundle=failure_bundle(StructuredPlanningError(str(caught.value),caught.value),'schema','12\u4e2a\u7528\u6237',p)
    contract=bundle['capability_evidence']['deliverables']
    assert contract['ledger_target_field']=='deliverables'
    assert contract['valid_in_spec'] is False and 'opendss' in contract['formats']
    action=bundle['allowed_actions'][0]
    assert action['preserve_requirement_bindings'] is True
