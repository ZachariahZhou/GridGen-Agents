"""Source contracts reproduced from the frozen M97 outer-agent failures."""
import copy
import json
from pathlib import Path

import pytest


def task_plan(task='voltage_control', **changes):
    base = dict(network_kind='distribution', n_buses=13, seed=971031,
        total_kw_min=120, total_kw_max=120, pv_ratio=.6 if task == 'static_pv_impact' else 0,
        scenario=dict(kind='rural', engineering_profile='rural'),
        phase_design=dict(mode='balanced'), repair_policy=dict(strategy='none'))
    if task == 'transmission_transfer':
        base = dict(network_kind='transmission', n_buses=17, n_generators=3, total_mw=120,
                    voltage_kv=110, seed=971031, allowed_repairs=[])
    return dict(task=task, research_question='Preserve the original research experiment.',
        base_spec=base, response_lower=.98, response_upper=1.02,
        preserve_geometry=True, preserve_equipment=True, **changes)


def response_plan():
    return dict(research_question='Response study', base_spec=task_plan()['base_spec'],
        probes=[dict(id='q', kind='voltage_q')],
        targets=[dict(probe_id='q', reference='baseline_ratio', lower=1.1, upper=1.3)],
        search=dict(allowed_actions=[]))


class FixedModel:
    def __init__(self, plan):
        self.plan = plan

    def with_structured_output(self, *args, **kwargs):
        return self

    def invoke(self, messages):
        return dict(status='ready', plan=copy.deepcopy(self.plan), issues=[])


@pytest.mark.parametrize('wording', [
    'Allow uniform geometry scaling within 30% of original lengths.',
    'I now authorize uniform geometry scaling within 30% of original lengths.',
])
def test_explicit_english_geometry_grant_is_recognized(wording):
    from feeder_agents.response_language import _layout_permission
    assert _layout_permission(wording) == pytest.approx(.3)


@pytest.mark.parametrize('wording', [
    'Do not allow uniform geometry scaling within 30%.',
    'Try harder; this gives no new numerical edit permission.',
])
def test_no_permission_text_cannot_create_geometry_grant(wording):
    from feeder_agents.response_language import _layout_permission
    assert _layout_permission(wording) is None


def test_reversed_source_response_bounds_block_before_grid_creation(tmp_path):
    from feeder_agents.response_language import design_response_from_request
    result = design_response_from_request(
        'Generate a feeder with baseline-relative electrical response lower bound 1.3 and upper bound 1.1.',
        tmp_path, 'reversed', model=FixedModel(response_plan()))
    assert result['status'] == 'needs_clarification'
    assert not (tmp_path / 'response_designs').exists()


def test_response_between_original_baseline_bounds_are_preserved(tmp_path):
    from feeder_agents.response_language import design_response_from_request
    result = design_response_from_request(
        'Generate a feeder with electrical response between 1.2 and 1.25 times the original baseline response.',
        tmp_path, 'widened', execute=False, model=FixedModel(response_plan()))
    assert result['status'] == 'planning_failed' and result['failure_origin'] == 'compiler'


@pytest.mark.parametrize('task,text,field,value', [
    ('voltage_control', 'Use physical generation seed 971031.', 'seed', 42),
    ('voltage_control', 'Generate two rural distribution feeders.', 'count', 1),
    ('voltage_control', 'Disable automatic repairs.', 'repair_policy', dict(strategy='fixed')),
    ('static_pv_impact', 'PV capacity equal to 60% of demand.', 'pv_ratio', .1),
    ('voltage_control', 'balanced three-phase representation.', 'phase_design', dict(mode='unbalanced')),
    ('transmission_transfer', 'Disable automatic repairs.', 'allowed_repairs', ['parallel_line']),
])
def test_explicit_construction_requirements_survive_research_compilation(task, text, field, value):
    from feeder_agents.research_language import _source_issues
    from feeder_agents.research_schema import ResearchTaskPlan
    payload = task_plan(task)
    payload.pop('response_lower'); payload.pop('response_upper')
    payload['base_spec'][field] = value
    assert _source_issues(text, ResearchTaskPlan.model_validate(payload), None)


@pytest.mark.parametrize('task,text,field,value', [
    ('voltage_control', 'Use finite external Q support of 20 kvar.', 'support_mvar', .01),
    ('voltage_control', 'Finite support is 20 kvar.', 'support_mvar', .01),
    ('transmission_transfer', 'Use a finite 2 MW transfer.', 'transfer_mw', 1.),
])
def test_finite_interventions_cannot_fall_back_to_defaults(task, text, field, value):
    from feeder_agents.research_language import _source_issues
    from feeder_agents.research_schema import ResearchTaskPlan
    payload = task_plan(task, **{field: value})
    payload.pop('response_lower'); payload.pop('response_upper')
    assert _source_issues(text, ResearchTaskPlan.model_validate(payload), None)


def test_pv_request_through_response_entry_runs_actual_off_on_experiment(tmp_path):
    from feeder_agents.response_language import design_response_from_request
    request = ('Generate one rural distribution feeder with exactly 13 total buses, 10 kV, '
        'fixed total demand 120 kW, PV capacity equal to 60% of demand, balanced three-phase representation. '
        'Disable automatic repairs. The research purpose is static PV impact research using d|V|/dP. '
        'Require response between 0.98 and 1.02 times the original baseline response. '
        'Verify full existing PV off/on. Preserve equipment and geometry. Use physical generation seed 971031.')
    result = design_response_from_request(request, tmp_path, 'pv', model=FixedModel(task_plan('static_pv_impact')))
    assert result['plan_type'] == 'research_task'
    assert result['status'] == 'completed'
    evidence = json.loads((tmp_path/'research_tasks/pv/suitability.json').read_text())
    assert evidence['task_validation']['accepted']
    assert evidence['task_validation']['metrics']['pv_on_kw'] == pytest.approx(72)
    assert evidence['task_validation']['metrics']['pv_off_kw'] == 0


def test_geometry_only_amendment_keeps_equipment_frozen_and_targets_intact():
    from feeder_agents.response_permissions import prepare_permission_revision
    original = response_plan()
    revision = prepare_permission_revision(original,
        dict(tier='geometry', max_changed_conductors=0, max_layout_scale_change=.3))
    assert revision['plan']['search']['allowed_actions'] == ['scale_layout']
    assert revision['plan']['search']['max_changed_conductors'] is None
    assert revision['plan']['targets'] == original['targets']
    assert revision['plan']['base_spec']['seed'] == 971031


def test_geometry_only_source_rejects_invented_equipment_and_local_geometry():
    from feeder_agents.response_permissions import permission_source_issues
    request = ('I now authorize uniform geometry scaling within 30% of original lengths. '
        'Equipment replacement is still forbidden. Preserve original response targets.')
    assert not permission_source_issues(request, response_plan(),
        dict(tier='geometry', max_changed_conductors=0, max_layout_scale_change=.3))
    assert permission_source_issues(request, response_plan(),
        dict(tier='equipment_geometry', max_changed_conductors=1,
             max_layout_scale_change=.3, max_branch_length_change=.3, max_node_displacement_km=.1))


@pytest.mark.parametrize('request_text', [
    'Try harder; this gives no new numerical edit permission.',
    'Do not allow replacing up to 4 conductors.',
    'No equipment permission is granted; try harder.',
    'I cannot authorize replacing up to 4 conductors.',
    "I can't allow replacing up to 4 conductors.",
])
def test_permission_revision_requires_actual_user_grant(request_text):
    from feeder_agents.response_permissions import permission_source_issues
    assert permission_source_issues(request_text, response_plan(),
        dict(tier='equipment', max_changed_conductors=4))


@pytest.mark.parametrize('text,expected', [
    ('Generate a static PV impact grid.', {'static_pv_impact'}),
    ('Design a response study. Verify full existing PV off/on.', {'static_pv_impact'}),
    ('Design electrical response with finite external Q support of 20 kvar.', {'voltage_control'}),
    ('Generate a feeder; do not study static PV impact research.', set()),
])
def test_task_detection_retains_the_requested_finite_experiment(text, expected):
    from feeder_agents.research_language import research_task_names
    assert research_task_names(text) == expected


@pytest.mark.parametrize('text,conflict', [
    ('Baseline-relative response at least 1.3 and at most 1.1.', True),
    ('Response ratio lower bound 1.3; response ratio upper bound 1.1.', True),
    ('Response ratio between 1.3 and 1.1.', True),
    ('Response ratio between 1.1 and 1.3.', False),
    ('Response ratio at least 1.1.', False),
])
def test_response_contradiction_preflight_is_independent_of_candidate(text, conflict):
    from feeder_agents.research_source import response_interval_issues
    assert bool(response_interval_issues(text)) is conflict


@pytest.mark.parametrize('task,text,field,value', [
    ('voltage_control', 'Use finite external Q support of 20 kvar.', 'support_mvar', .02),
    ('voltage_control', 'Finite support is 20 kvar.', 'support_mvar', .02),
    ('transmission_transfer', 'Use a finite 2 MW transfer.', 'transfer_mw', 2.),
])
def test_matching_finite_intervention_units_are_accepted(task, text, field, value):
    from feeder_agents.research_language import _source_issues
    from feeder_agents.research_schema import ResearchTaskPlan
    payload = task_plan(task, **{field:value})
    assert not _source_issues(text+' Require response between 0.98 and 1.02 times the original baseline.',
                             ResearchTaskPlan.model_validate(payload), None)


def test_explicit_conductor_grant_is_accepted_but_limit_cannot_be_widened():
    from feeder_agents.response_permissions import permission_source_issues
    text = 'I now allow replacing up to 4 conductors. Preserve geometry.'
    assert not permission_source_issues(text,response_plan(),dict(tier='equipment',max_changed_conductors=4))
    assert permission_source_issues(text,response_plan(),dict(tier='equipment',max_changed_conductors=5))


@pytest.mark.parametrize('text', [
    'Do not authorize uniform geometry scaling within 30%.',
    'I do not authorize uniform geometry scaling within 30%.',
    'Never allow uniform geometry scaling within 30%.',
    'I cannot authorize uniform geometry scaling within 30%.',
    "I can't allow uniform geometry scaling within 30%.",
])
def test_negative_geometry_authorization_cannot_be_extracted_as_positive(text):
    from feeder_agents.response_language import _layout_permission
    assert _layout_permission(text) is None


@pytest.mark.parametrize('text,field,value', [
    ('Fixed total demand 120 kW.', 'total_kw_min', 100),
    ('Fixed total demand 120 kW.', 'total_kw_max', 140),
    ('120 kW total demand.', 'total_kw_max', 140),
    ('No PV.', 'pv_ratio', .6),
    ('Use 10 kV.', 'voltage_kv', 20),
])
def test_response_compiler_preserves_explicit_physical_conditions(text, field, value):
    from feeder_agents.response_language import response_source_issues
    from feeder_agents.response_schema import ResponseDesignPlan
    payload = response_plan(); payload['base_spec'][field] = value
    assert response_source_issues(text, ResponseDesignPlan.model_validate(payload))


@pytest.mark.parametrize('task', ['voltage_control','static_pv_impact'])
def test_permission_ladder_preserves_original_finite_task_and_baseline(tmp_path,task):
    from feeder_agents.research_tasks import compile_research_task
    from feeder_agents.response_permission_workflow import run_permission_ladder
    compiled=compile_research_task(task_plan(task,search=dict(max_rounds=0)))
    context=compiled['context']; original=copy.deepcopy(compiled['response_plan'])
    result=run_permission_ladder(original,
        [dict(tier='geometry',max_layout_scale_change=.3)],tmp_path,'task_ladder',task_context=context)
    assert result['target_met']
    validation=result['result']['selected']['task_validation']
    assert validation['accepted'] and validation['protocol']==context
    if task=='voltage_control':
        assert validation['metrics']['support_mvar']==pytest.approx(.01)
    else:
        assert validation['metrics']['pv_on_kw']==pytest.approx(72)
        assert validation['metrics']['pv_off_kw']==0
    assert result['authorized_stages'][0]['plan']['targets']==original['targets']
    manifest=json.loads((tmp_path/'permission_ladders/task_ladder/manifest.json').read_text())
    assert manifest['task_context']==context
    from feeder_agents.schemas import Feeder
    from feeder_agents.artifacts import digest
    baseline=json.loads((Path(result['result']['directory'])/'baseline/model/feeder.json').read_text())
    assert digest(Feeder.model_validate(baseline).model_dump())==context['original_model_hash']


def test_permission_ladder_cannot_accept_failed_original_finite_intervention(tmp_path):
    from feeder_agents.research_tasks import compile_research_task
    from feeder_agents.response_permission_workflow import run_permission_ladder
    compiled=compile_research_task(task_plan(support_mvar=100,search=dict(max_rounds=0)))
    result=run_permission_ladder(compiled['response_plan'],
        [dict(tier='geometry',max_layout_scale_change=.2),dict(tier='geometry',max_layout_scale_change=.3)],
        tmp_path,'failed_task',task_context=compiled['context'])
    assert not result['target_met']
    assert len(result['stages'])==2
    baseline_models=[]
    for record in result['stages']:
        stage=Path(record['directory'])
        saved=json.loads((stage/'result.json').read_text())
        assert not saved['selected']['task_validation']['accepted']
        assert saved['selected']['task_validation']['protocol']['support_mvar']==100
        assert record['baseline_verified']
        baseline_models.append(json.loads((stage/'baseline/model/feeder.json').read_text()))
    assert baseline_models[0]==baseline_models[1]


def test_permission_ladder_rejects_changed_original_task_baseline(tmp_path):
    from feeder_agents.research_tasks import compile_research_task
    from feeder_agents.response_permission_workflow import run_permission_ladder
    compiled=compile_research_task(task_plan(search=dict(max_rounds=0)))
    compiled['response_plan']['base_spec']['seed']+=1
    with pytest.raises(ValueError,match='reference'):
        run_permission_ladder(compiled['response_plan'],
            [dict(tier='geometry',max_layout_scale_change=.3)],tmp_path,'wrong_baseline',task_context=compiled['context'])
    assert not list((tmp_path/'response_designs').glob('*/selected/feeder.json'))


RECORDED_EQUIPMENT_GEOMETRY_GRANT=(
    'I explicitly authorize equipment replacement of at most two conductors, and uniform geometry '
    'scaling within 30%, per-branch length change within 30%, node displacement at most 0.2 km. '
    'No reconnection or demand changes. Preserve all original response targets and references. '
    'Use a new ID; execute only within those numerical limits.')


def test_recorded_equipment_geometry_grant_accepts_word_count_and_per_branch_bound():
    from feeder_agents.response_permissions import permission_source_issues
    grant=dict(tier='equipment_geometry',max_changed_conductors=2,max_layout_scale_change=.3,
               max_branch_length_change=.3,max_node_displacement_km=.2)
    assert not permission_source_issues(RECORDED_EQUIPMENT_GEOMETRY_GRANT,response_plan(),grant)


@pytest.mark.parametrize('field,value',[
    ('max_changed_conductors',3),('max_layout_scale_change',.4),
    ('max_branch_length_change',.4),('max_node_displacement_km',.3),
])
def test_recorded_combined_grant_cannot_exceed_any_original_limit(field,value):
    from feeder_agents.response_permissions import permission_source_issues
    grant=dict(tier='equipment_geometry',max_changed_conductors=2,max_layout_scale_change=.3,
               max_branch_length_change=.3,max_node_displacement_km=.2)
    grant[field]=value
    assert permission_source_issues(RECORDED_EQUIPMENT_GEOMETRY_GRANT,response_plan(),grant)


def test_negated_combined_grant_does_not_authorize_geometry_or_equipment():
    from feeder_agents.response_permissions import permission_source_issues
    grant=dict(tier='equipment_geometry',max_changed_conductors=2,max_layout_scale_change=.3,
               max_branch_length_change=.3,max_node_displacement_km=.2)
    source=RECORDED_EQUIPMENT_GEOMETRY_GRANT.replace('I explicitly authorize','I do not authorize')
    assert permission_source_issues(source,response_plan(),grant)


def test_conductor_word_limit_applies_to_initial_response_search():
    from feeder_agents.response_language import _local_permission_issues
    from feeder_agents.response_schema import ResponseSearch
    assert not _local_permission_issues('Allow replacing at most two conductors.',
        ResponseSearch(allowed_actions=['replace_conductor'],max_changed_conductors=2))
    assert _local_permission_issues('Allow replacing at most two conductors.',
        ResponseSearch(allowed_actions=['replace_conductor'],max_changed_conductors=3))
