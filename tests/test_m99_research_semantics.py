"""Public research compilation keeps candidate faults separate from source ambiguity."""
import copy
import json

import pytest

from feeder_agents.research_language import design_research_from_request


SOURCE = ('Generate exactly 13 total buses including the source for voltage-control research. '
          'Require response between 0.99 and 1.01 times the original baseline.')


def candidate(port='b1', *, explicit_interval=True):
    plan = dict(task='voltage_control', research_question='Preserve the requested research model',
                base_spec=dict(network_kind='distribution', n_buses=13, seed=42,
                    voltage_kv=10, total_kw_min=120, total_kw_max=120, pv_ratio=0,
                    repair_policy=dict(strategy='none')),
                injection_buses=[port], search=dict(allowed_actions=[], max_rounds=0))
    if explicit_interval:plan.update(response_lower=.99, response_upper=1.01)
    return dict(status='ready', plan=plan, issues=[])


class Replies:
    def __init__(self, *candidates):
        self.candidates = candidates
        self.calls = 0

    def with_structured_output(self, *args, **kwargs):return self

    def invoke(self, messages):
        value = self.candidates[min(self.calls, len(self.candidates) - 1)]
        self.calls += 1
        return copy.deepcopy(value)


def records(root):
    return [json.loads(path.read_text()) for path in sorted(root.glob('model_calls/*/attempt_*.json'))]


@pytest.fixture
def no_ac(monkeypatch):
    from feeder_agents import simulation, transmission
    def forbidden(*args, **kwargs):raise AssertionError('Compiler attempted an AC solve')
    monkeypatch.setattr(simulation, 'simulate', forbidden)
    monkeypatch.setattr(transmission, 'dependencies', forbidden)


def test_invented_port_is_corrected_before_ready_cache_without_ac(tmp_path, no_ac):
    model = Replies(candidate('b999'), candidate())
    result = design_research_from_request(SOURCE, tmp_path, 'corrected', execute=False, model=model)
    assert result['status'] == 'draft'
    assert model.calls == 2
    assert result['plan']['injection_buses'] == ['b1']
    assert result['solver_accounting']['total_ac_calls'] == 0
    root = tmp_path / 'designs/corrected'
    saved = json.loads((root / 'brief.json').read_text())
    assert saved['status'] == 'ready' and saved['plan']['injection_buses'] == ['b1']
    attempts = records(root)
    assert [r['status'] for r in attempts] == ['invalid', 'valid']
    assert attempts[0]['structured_response']['plan']['injection_buses'] == ['b999']


def test_repeated_bad_ports_leave_no_ready_cache_and_same_id_can_recover(tmp_path, no_ac):
    bad = Replies(candidate('b999'))
    result = design_research_from_request(SOURCE, tmp_path, 'retry', execute=False, model=bad)
    assert result['status'] == 'planning_failed'
    assert result['failure_origin'] == 'compiler'
    assert bad.calls == 3
    root = tmp_path / 'designs/retry'
    assert not (root / 'brief.json').exists()
    good = Replies(candidate())
    result = design_research_from_request(SOURCE, tmp_path, 'retry', execute=False, model=good)
    assert result['status'] == 'draft' and good.calls == 1
    assert sum(r['status'] == 'invalid' for r in records(root)) == 3
    assert sum(r['status'] == 'valid' for r in records(root)) == 1
    assert not (tmp_path / 'research_tasks').exists()


def test_explicit_unknown_source_port_still_needs_clarification(tmp_path, no_ac):
    request = SOURCE + ' Use injection bus b999.'
    model = Replies(candidate('b999'))
    result = design_research_from_request(request, tmp_path, 'source_port', execute=False, model=model)
    assert result['status'] == 'needs_clarification'
    assert model.calls == 1
    assert result['plan'] is None
    saved = json.loads((tmp_path / 'designs/source_port/brief.json').read_text())
    assert saved['status'] == 'needs_clarification' and saved['plan'] is None
    assert 'b999' in ' '.join(result['issues'])


@pytest.mark.parametrize('mention', ['Do not use injection bus b999.',
                                   'A previous example used injection bus b999.'])
def test_rejected_or_historical_port_does_not_establish_user_ambiguity(tmp_path, no_ac, mention):
    model = Replies(candidate('b999'))
    result = design_research_from_request(SOURCE + ' ' + mention, tmp_path, 'invented', execute=False, model=model)
    assert result['status'] == 'planning_failed' and result['failure_origin'] == 'compiler'
    assert model.calls == 3
    assert not (tmp_path / 'designs/invented/brief.json').exists()


@pytest.mark.parametrize('level', ['high sensitivity', 'low sensitivity', '\u9ad8\u7075\u654f\u5ea6', '\u4f4e\u654f\u611f\u5ea6',
                                 'high sensitivity without PV'])
def test_undefined_response_level_clarifies_before_calling_model(tmp_path, no_ac, level):
    request = 'Generate exactly 13 total buses for voltage-control research with ' + level + '.'
    model = Replies(candidate(explicit_interval=False))
    result = design_research_from_request(request, tmp_path, 'undefined', execute=False, model=model)
    assert result['status'] == 'needs_clarification'
    assert model.calls == 0
    assert result['plan'] is None
    assert not (tmp_path / 'research_tasks').exists()


@pytest.mark.parametrize('context', ['Do not require high sensitivity.',
                                   'A previous example requested low sensitivity.'])
def test_inactive_qualitative_level_does_not_block_a_defined_request(tmp_path, no_ac, context):
    request = 'Generate exactly 13 total buses for voltage-control research. ' + context
    model = Replies(candidate(explicit_interval=False))
    result = design_research_from_request(request, tmp_path, 'inactive', execute=False, model=model)
    assert result['status'] == 'draft'
    assert model.calls == 1


def test_numerically_defined_qualitative_level_preserves_its_interval(tmp_path, no_ac):
    model = Replies(candidate())
    result = design_research_from_request(SOURCE + ' This numerical interval defines high sensitivity.',
        tmp_path, 'defined', execute=False, model=model)
    assert result['status'] == 'draft' and model.calls == 1
    assert (result['plan']['response_lower'], result['plan']['response_upper']) == (.99, 1.01)


def test_transmission_port_validation_and_correction_do_not_solve_ac(tmp_path, no_ac):
    good = dict(status='ready', issues=[], plan=dict(task='transmission_transfer',
        research_question='Transfer response research', response_lower=.99, response_upper=1.01,
        base_spec=dict(network_kind='transmission', n_buses=13, n_generators=3,
                       total_mw=120, seed=42, allowed_repairs=[]),
        injection_buses=['2'], withdrawal_buses=['3'], search=dict(allowed_actions=[], max_rounds=0)))
    bad = copy.deepcopy(good);bad['plan']['injection_buses'] = ['999']
    request = ('Generate exactly 13 total buses for transmission-transfer research. '
               'Require response between 0.99 and 1.01 times the original baseline.')
    model = Replies(bad, good)
    result = design_research_from_request(request, tmp_path, 'transmission', execute=False, model=model)
    assert result['status'] == 'draft' and model.calls == 2
    assert result['solver_accounting']['total_ac_calls'] == 0


@pytest.mark.parametrize('field,value', [('injection_buses', ['999']),
                                       ('injection_buses', ['3']),
                                       ('injection_buses', ['2', '2']),
                                       ('monitor_branches', [-1]),
                                       ('monitor_branches', [999]),
                                       ('phases', [])])
def test_bad_transmission_ports_do_not_run_default_dispatch_initialization(tmp_path, no_ac, field, value):
    bad = dict(status='ready', issues=[], plan=dict(task='transmission_transfer',
        research_question='Transfer response research', response_lower=.99, response_upper=1.01,
        base_spec=dict(network_kind='transmission', n_buses=13, n_generators=3, total_mw=120, seed=42),
        injection_buses=['2'], withdrawal_buses=['3'], search=dict(allowed_actions=[], max_rounds=0)))
    bad['plan'][field] = value
    request = ('Generate exactly 13 total buses for transmission-transfer research. '
               'Require response between 0.99 and 1.01 times the original baseline.')
    model = Replies(bad)
    result = design_research_from_request(request, tmp_path, 'invalid_transmission', execute=False, model=model)
    assert result['status'] == 'planning_failed' and model.calls == 3
    assert not (tmp_path / 'designs/invalid_transmission/brief.json').exists()


def test_only_current_validated_brief_identity_is_reused(tmp_path, no_ac):
    from feeder_agents.artifacts import digest
    model = Replies(candidate())
    first = design_research_from_request(SOURCE, tmp_path, 'cache', execute=False, model=model)
    assert first['status'] == 'draft'
    unused = Replies(candidate('b999'))
    cached = design_research_from_request(SOURCE, tmp_path, 'cache', execute=False, model=unused)
    assert cached['status'] == 'draft' and unused.calls == 0
    path = tmp_path / 'designs/cache/brief.json'
    saved = json.loads(path.read_text())
    assert saved['identity']['compiler_version'] == 'research_task_v1.5'
    saved.pop('brief_hash')
    saved['identity']['compiler_version'] = 'research_task_v1.4'
    saved['brief_hash'] = digest(saved)
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match='use a new ID'):
        design_research_from_request(SOURCE, tmp_path, 'cache', execute=False, model=unused)
    assert unused.calls == 0
