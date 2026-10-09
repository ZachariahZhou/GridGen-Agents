"""Offline test fixtures extracted from the original research regression suite."""
import copy
import json


def equivalent_proposal():
    return dict(status='ready', family='single_voltage', single_voltage_spec=dict(
        n_buses=69, total_kw_min=500, total_kw_max=500,
        scenario=dict(kind='rural', load_placement='reference_conditioned', reference_case_id='case69')),
        ledger=dict(network_kind='distribution', model_family='single_voltage', summary='69\u6bcd\u7ebf\u79d1\u7814\u7b97\u4f8b',
            requirements=[dict(id='buses', segment_ids=[0], evidence='\u751f\u621069\u8282\u70b9\u79d1\u7814\u914d\u7f51', meaning='\u603b\u7535\u6c14\u6bcd\u7ebf\u6570',
                priority='hard', disposition='supported', target_field='n_buses', expected_value=69)]))


def hierarchy_proposal(users=12):
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
