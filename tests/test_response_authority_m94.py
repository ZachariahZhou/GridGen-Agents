"""Authority evidence describes editable effects without certifying feasibility."""
import copy

import pytest


def test_signed_decomposition_preserves_mean_absolute_response():
    from feeder_agents.response_authority import voltage_components
    result=voltage_components({'a.1':2.,'a.2':-2.},{'a.1':1.5,'a.2':-1.5},'mean_abs')
    assert result['available']
    assert result['aggregate_response']==pytest.approx(2.)
    assert result['source_contribution']==pytest.approx(1.5)
    assert result['network_contribution']==pytest.approx(.5)
    assert result['network_fraction']==pytest.approx(.25)
    assert result['source_contribution']+result['network_contribution']==pytest.approx(result['aggregate_response'])


@pytest.mark.parametrize('response,source,aggregation',[
    ({'a':1.},{},'mean_abs'),({'a':0.},{'a':0.},'mean_abs'),
    ({'a':1.},{'a':float('nan')},'mean_abs'),({'a':1.},{'a':.5},'max_abs')])
def test_missing_or_unsupported_components_are_unavailable(response,source,aggregation):
    from feeder_agents.response_authority import voltage_components
    assert not voltage_components(response,source,aggregation)['available']


def authority_plan(actions):
    from feeder_agents.response_schema import ResponseDesignPlan
    return ResponseDesignPlan.model_validate(dict(research_question='Estimate available geometric influence.',
        base_spec=dict(network_kind='distribution',n_buses=9),
        probes=[dict(id='a',kind='voltage_q')],
        targets=[dict(probe_id='a',reference='baseline_ratio',lower=1.1,upper=1.2)],
        search=dict(allowed_actions=actions,max_layout_scale_change=.2)))


def test_authority_flags_source_dominance_and_respects_permissions():
    from feeder_agents.response_authority import estimate_adjustment_authority
    plan=authority_plan(['scale_layout'])
    baseline=dict(probes=dict(a=dict(valid=True,value=.002,signed_response={'a.1':.002},
        source_signed_response={'a.1':.0016})))
    before=copy.deepcopy(baseline)
    report=estimate_adjustment_authority(plan,baseline)
    row=report['probes'][0]
    assert row['network_fraction']==pytest.approx(.2)
    assert row['line_scaling_scenario']['target_range']==pytest.approx([.96,1.04])
    assert row['geometry_only_target_outside_linear_scenario']
    assert not report['infeasibility_proven'] and baseline==before
    guidance={r['action']:r for r in report['design_guidance']}
    assert guidance['replace_conductor']['requires_new_authorization']
    allowed=estimate_adjustment_authority(authority_plan(['scale_layout','replace_conductor']),baseline)
    assert not allowed['probes'][0]['geometry_only_target_outside_linear_scenario']
    assert not next(r for r in allowed['design_guidance'] if r['action']=='replace_conductor')['requires_new_authorization']


def test_authority_measurement_uses_existing_snapshots(tmp_path):
    from feeder_agents.response_measurement import create_model,resolve_probes,measure
    from feeder_agents.solver_accounting import SolverMeter
    from feeder_agents.response_authority import estimate_adjustment_authority
    plan=authority_plan(['scale_layout']);model=create_model(plan.base_spec)
    probes=resolve_probes(model,plan.probes)
    with SolverMeter() as meter:
        measured=measure(model,plan.base_spec,probes,tmp_path)
    assert meter.snapshot()['total_ac_calls']==3
    report=estimate_adjustment_authority(plan,measured)
    assert report['probes'][0]['available']
    assert set(measured['probes']['a']['source_signed_response'])==set(measured['probes']['a']['signed_response'])
