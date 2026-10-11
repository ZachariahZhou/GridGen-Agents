"""A bad generated plan must not be presented as contradictory user intent."""
import copy
import json

import pytest


class CandidateReplay:
    def __init__(self, *plans):
        self.plans = plans
        self.calls = 0

    def with_structured_output(self, *args, **kwargs):
        return self

    def invoke(self, messages):
        candidate = self.plans[min(self.calls, len(self.plans) - 1)]
        self.calls += 1
        return copy.deepcopy(candidate)


def candidate(family, buses=13):
    base = dict(network_kind='distribution', n_buses=buses, voltage_kv=10,
                total_kw_min=120, total_kw_max=120, pv_ratio=0, seed=42,
                repair_policy=dict(strategy='none'))
    plan = dict(research_question='Generate the requested research model', base_spec=base,
                search=dict(allowed_actions=[], max_rounds=0))
    if family == 'research':
        plan.update(task='voltage_control', response_lower=.99, response_upper=1.01)
    else:
        plan.update(probes=[dict(id='q', kind='voltage_q', injection_buses=['b1'])],
                    targets=[dict(probe_id='q', reference='baseline_ratio', lower=.99, upper=1.01)])
    return dict(status='ready', plan=plan, issues=[])


def source(family):
    task = ' for voltage-control research' if family == 'research' else ''
    return ('Generate exactly 13 total buses including the source' + task +
            '. Require response between 0.99 and 1.01 times the original baseline.')


def entry(family):
    from feeder_agents.research_language import design_research_from_request
    from feeder_agents.response_language import design_response_from_request
    return design_research_from_request if family == 'research' else design_response_from_request


@pytest.mark.parametrize('family,attempts', [('research', 3), ('response', 2)])
def test_repeated_plan_count_violation_is_compiler_failure_not_input_conflict(tmp_path, family, attempts):
    model = CandidateReplay(candidate(family, buses=17))
    result = entry(family)(source(family), tmp_path, 'failure', execute=False, model=model)
    assert result['status'] == 'planning_failed'
    assert result['failure_origin'] == 'compiler'
    assert model.calls == attempts
    assert not (tmp_path / 'research_tasks').exists()
    assert not (tmp_path / 'response_designs').exists()
    assert not (tmp_path / 'designs/failure/brief.json').exists()
    records = list((tmp_path / 'designs/failure/model_calls').glob('*/attempt_*.json'))
    assert len(records) == attempts
    assert all(json.loads(p.read_text())['status'] == 'invalid' for p in records)


def test_response_source_violation_gets_bounded_correction_and_keeps_request(tmp_path):
    model = CandidateReplay(candidate('response', buses=17), candidate('response'))
    result = entry('response')(source('response'), tmp_path, 'corrected', execute=False, model=model)
    assert result['status'] == 'draft'
    assert result['request'] == source('response')
    assert result['plan']['base_spec']['n_buses'] == 13
    assert result['plan']['targets'][0]['lower'] == .99
    assert result['plan']['targets'][0]['upper'] == 1.01
    records = sorted((tmp_path / 'designs/corrected/model_calls').glob('*/attempt_*.json'))
    assert [json.loads(p.read_text())['status'] for p in records] == ['invalid', 'valid']


@pytest.mark.parametrize('family', ['research', 'response'])
def test_actual_source_contradiction_still_clarifies_without_model_call(tmp_path, family):
    model = CandidateReplay(candidate(family))
    request = source(family).replace('0.99 and 1.01', '1.3 and 1.1')
    result = entry(family)(request, tmp_path, 'conflict', model=model)
    assert result['status'] == 'needs_clarification'
    assert model.calls == 0


@pytest.mark.parametrize('family', ['research', 'response'])
def test_conflicting_exact_counts_are_user_conflicts_before_compilation(tmp_path, family):
    model = CandidateReplay(candidate(family))
    request = source(family) + ' The same model must contain exactly 17 total buses.'
    result = entry(family)(request, tmp_path, 'count_conflict', model=model)
    assert result['status'] == 'needs_clarification'
    assert model.calls == 0


@pytest.mark.parametrize('text,expected', [
    ('Generate exactly 29 total buses including the source.', {29}),
    ('Create a 37-bus rural feeder.', {37}),
    ('The total bus count is 139.', {139}),
    ('The total bus count must be 139.', {139}),
    ('Use at least 3 generators with exactly 139 total buses.', {139}),
    ('Create a rural village network with exactly 29 buses.', {29}),
    ('Generate a minimum of 13 total buses and a maximum of 17 total buses.', set()),
    ('Generate >= 13 total buses and <= 17 total buses.', set()),
    ('Generate from 31 through 39 total buses.', set()),
    ('Generate between 31 and 39 total buses.', set()),
    ('Generate approximately 35 buses.', set()),
    ('Generate at least 31 buses.', set()),
    ('Generate 31-39 buses.', set()),
    ('The previous model had 29 buses; now generate exactly 37 buses.', {37}),
    ('Do not require exactly 29 buses.', set()),
    ('Use 13 buses per village.', set()),
    ('The MV layer has 13 buses.', set()),
    ('The MV layer has 13 total buses.', set()),
    ('Each village contains exactly 13 total buses.', set()),
    ('Generate exactly 29 total buses; require exactly 37 total buses.', {29, 37}),
])
def test_english_total_bus_scope_does_not_convert_ranges_or_local_counts_to_exact(text, expected):
    from feeder_agents.source_review import exact_bus_counts
    assert exact_bus_counts(text) == expected
