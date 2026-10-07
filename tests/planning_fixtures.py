"""Offline test fixtures extracted from the original research regression suite."""
import copy
import json


def equivalent_proposal():
    return dict(status='ready', family='single_voltage', single_voltage_spec=dict(
        n_buses=69, total_kw_min=500, total_kw_max=500,
        scenario=dict(kind='rural', load_placement='reference_conditioned', reference_case_id='case69')),
        ledger=dict(network_kind='distribution', model_family='single_voltage', summary='69母线科研算例',
            requirements=[dict(id='buses', segment_ids=[0], evidence='生成69节点科研配网', meaning='总电气母线数',
                priority='hard', disposition='supported', target_field='n_buses', expected_value=69)]))


def hierarchy_proposal(users=12):
    return dict(status='ready', family='hierarchical', distribution_spec=dict(users=users),
                ledger=dict(network_kind='distribution', model_family='hierarchical', summary='用户要求', requirements=[
                    dict(id='users', segment_ids=[0], evidence='12个用户', meaning='用户数',
                         priority='hard', disposition='supported', target_field='hierarchy.users', expected_value=12)]))


def decision_from_context(messages):
    context = json.loads(messages[-1][1])
    action = context['allowed_actions'][0]
    return dict(action_id=action['id'], evidence_ids=[f['id'] for f in context['facts']],
                reason='检查给出了具体失败字段或缺失原文，按限定动作修正。',
                expected_change='纠正证据所指的错误并重新验收原始需求。')


class DiagnosticModel:
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
