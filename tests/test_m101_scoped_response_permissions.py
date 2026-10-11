"""Initial-construction repair controls must not erase explicit response scope."""
import copy

import pytest


REQUESTS = [
    '\u521d\u59cb\u6784\u5efa\u5b8c\u6210\u540e\u7981\u7528\u81ea\u52a8\u4fee\u590d\u3002\u7528\u4e8e\u7535\u538b\u63a7\u5236\u7814\u7a76\uff0c\u8981\u6c42\u54cd\u5e94\u4e3a\u539f\u59cb\u57fa\u51c6\u76841.10\u52301.30\u500d\u3002'
    '\u4ec5\u5141\u8bb8\u7edf\u4e00\u51e0\u4f55\u7f29\u653e\uff0c\u6240\u6709\u7ebf\u957f\u76f8\u5bf9\u539f\u59cb\u6a21\u578b\u53d8\u5316\u4e0d\u8d85\u8fc730%\u3002\u4fdd\u6301\u8bbe\u5907\u3001\u8d1f\u8377\u3001\u5149\u4f0f\u3001\u7535\u538b\u3001\u6bcd\u7ebf\u53ca\u62d3\u6251\uff0c\u7981\u6b62\u5176\u4ed6\u4fee\u6539\u3002',
    'Disable automatic repairs after initial construction. For voltage-control research require '
    'response between 1.10 and 1.30 times the original baseline. Only uniform geometry scaling '
    'within 30% of original lengths is authorized; preserve equipment, demand, PV capacity, voltage, '
    'buses and topology. No other edits.',
]


def candidate(retain_permission):
    return dict(status='ready', issues=[], plan=dict(
        task='voltage_control', research_question='Preserve construction controls and response permissions.',
        base_spec=dict(network_kind='distribution', n_buses=7, total_kw_min=12, total_kw_max=12,
                       pv_ratio=0, seed=101051, repair_policy={'strategy': 'none'}),
        response_lower=1.1, response_upper=1.3, preserve_equipment=True,
        preserve_geometry=not retain_permission,
        search=dict(allowed_actions=['scale_layout'], max_layout_scale_change=.3)
        if retain_permission else None))


@pytest.mark.parametrize('wording', REQUESTS)
def test_explicit_bounded_geometry_permission_survives_initial_repair_prohibition(wording):
    from feeder_agents.response_language import _layout_permission
    from feeder_agents.research_source import research_source_contract
    assert _layout_permission(wording) == pytest.approx(.3)
    obligations = {item['path']: item['value'] for item in research_source_contract(wording, 'distribution')}
    assert obligations['plan.base_spec.repair_policy.strategy'] == 'none'
    assert obligations['plan.preserve_geometry'] is False
    assert obligations['plan.search.allowed_actions'] == ['scale_layout']
    assert obligations['plan.search.max_layout_scale_change'] == pytest.approx(.3)


@pytest.mark.parametrize('wording', REQUESTS)
def test_compiler_rejects_lost_permission_then_accepts_corrected_scope(tmp_path, wording):
    from feeder_agents.research_language import design_research_from_request
    class ReplayModel:
        def __init__(self):
            self.calls = 0
        def with_structured_output(self, *args, **kwargs):
            return self
        def invoke(self, messages):
            self.calls += 1
            return copy.deepcopy(candidate(self.calls > 1))
    model = ReplayModel()
    result = design_research_from_request(wording, tmp_path, 'scoped', execute=False, model=model)
    assert model.calls == 2
    assert result['status'] == 'draft'
    assert result['plan']['base_spec']['repair_policy']['strategy'] == 'none'
    assert result['compiled_plan']['response_plan']['search']['allowed_actions'] == ['scale_layout']
    assert result['compiled_plan']['response_plan']['search']['max_layout_scale_change'] == pytest.approx(.3)
    assert result['plan']['response_lower'] == 1.1
    assert result['plan']['response_upper'] == 1.3


@pytest.mark.parametrize('wording', [
    'Disable automatic repairs after initial construction. No modification action is permitted. '
    'Preserve all equipment, geometry, topology, demand and PV.',
    '\u521d\u59cb\u6784\u5efa\u5b8c\u6210\u540e\u7981\u7528\u81ea\u52a8\u4fee\u590d\u3002\u7981\u6b62\u4efb\u4f55\u4fee\u6539\u3002\u4fdd\u6301\u8bbe\u5907\u3001\u51e0\u4f55\u3001\u62d3\u6251\u3001\u8d1f\u8377\u548c\u5149\u4f0f\u3002',
    '\u4e0d\u5141\u8bb8\u7edf\u4e00\u51e0\u4f55\u7f29\u653e\uff0c\u6240\u6709\u7ebf\u957f\u53d8\u5316\u4e0d\u8d85\u8fc730%\u3002',
    'Uniform geometry scaling within 30% is not authorized.',
])
def test_absolute_prohibition_does_not_grant_geometry_permission(wording):
    from feeder_agents.response_language import _layout_permission
    from feeder_agents.research_source import research_source_contract
    assert _layout_permission(wording) is None
    assert not any(row['path'] == 'plan.search.allowed_actions' and row['value']
                   for row in research_source_contract(wording, 'distribution'))


def test_retaining_permission_does_not_require_performing_an_edit():
    from feeder_agents.research_language import _source_issues
    from feeder_agents.research_schema import ResearchTaskPlan
    payload = candidate(True)['plan']
    payload['search']['max_rounds'] = 0
    payload['response_lower'], payload['response_upper'] = .98, 1.02
    request = REQUESTS[1].replace('1.10 and 1.30', '0.98 and 1.02')
    assert not _source_issues(request, ResearchTaskPlan.model_validate(payload), None)


def test_baseline_multiplier_overrides_probe_units_in_transmission_target():
    from feeder_agents.research_source import response_constraints, research_source_contract
    wording = ('Require peak-branch MW/MW sensitivity between 0.98 and 1.02 times '
               'the original baseline response.')
    assert response_constraints(wording) == [('baseline_ratio', 'ge', .98), ('baseline_ratio', 'le', 1.02)]
    obligations = {item['path']: item['value'] for item in research_source_contract(wording, 'transmission')}
    assert obligations['plan.response_reference'] == 'baseline_ratio'
    assert obligations['plan.response_lower'] == .98
    assert obligations['plan.response_upper'] == 1.02
    assert response_constraints('Require MW/MW sensitivity between 0.98 and 1.02.') == [
        ('absolute', 'ge', .98), ('absolute', 'le', 1.02)]


def test_dimensional_probe_does_not_silently_make_baseline_target_absolute():
    from feeder_agents.research_language import _source_issues
    from feeder_agents.research_schema import ResearchTaskPlan
    payload = dict(task='transmission_transfer', research_question='Retain the original response reference.',
        base_spec=dict(network_kind='transmission', n_buses=17, n_generators=3, total_mw=120,
                       voltage_kv=110, allowed_repairs=[]),
        response_reference='absolute', response_lower=.98, response_upper=1.02,
        preserve_geometry=True, preserve_equipment=True, search={'allowed_actions': []})
    wording = ('Study transmission transfer. Disable automatic repairs after initial construction. '
               'Require peak-branch MW/MW sensitivity between 0.98 and 1.02 times '
               'the original baseline response.')
    issues = _source_issues(wording, ResearchTaskPlan.model_validate(payload), None)
    assert any('response interval differs' in issue for issue in issues)
    payload['response_reference'] = 'baseline_ratio'
    assert not _source_issues(wording, ResearchTaskPlan.model_validate(payload), None)
