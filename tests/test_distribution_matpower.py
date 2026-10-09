import copy
import json
from pathlib import Path

import numpy as np
import pytest

from feeder_agents.adaptive_planning import AdaptiveProposal, _compile
from feeder_agents.design import design_from_request
from feeder_agents.generation import generate_feeder
from feeder_agents.matpower import parse_matpower
from feeder_agents.schemas import ExperimentPlan, ExperimentSpec
from feeder_agents.simulation import export_dss, simulate
from feeder_agents.structured_planning import StructuredPlanner
from test_adaptive_planning import Model
from planning_fixtures import equivalent_proposal as proposal


def test_direct_typed_response_preserves_default_origin_through_planner(tmp_path):
    typed=AdaptiveProposal.model_validate(proposal())
    response=StructuredPlanner(Model([typed]),AdaptiveProposal,tmp_path).invoke([('human','\u751f\u621069\u8282\u70b9\u79d1\u7814\u914d\u7f51')])
    brief=_compile(response,'\u751f\u621069\u8282\u70b9\u79d1\u7814\u914d\u7f51')
    assert brief['plan']['parameter_origins']['n_loads_min']=='default'
    assert brief['plan']['parameter_origins']['total_kw_min']=='inferred'


def dual_proposal():
    value=proposal()
    value['single_voltage_spec']['scenario']['tie_count']=2
    value['single_voltage_spec']['pv_ratio']=.2
    value['ledger']['requirements'].append(dict(id='formats',segment_ids=[1],evidence='\u8f93\u51faOpenDSS\u548cMATPOWER',
        meaning='\u53cc\u683c\u5f0f\u4ea4\u4ed8',priority='hard',disposition='supported',target_field='deliverables',
        expected_value=['opendss','matpower'],operator='contains'))
    return value


def test_balanced_dual_export_through_natural_language_entry_and_tamper(tmp_path):
    value=dual_proposal();request='\u751f\u621069\u8282\u70b9\u79d1\u7814\u914d\u7f51\uff1b\u8f93\u51faOpenDSS\u548cMATPOWER'
    result=design_from_request(request,tmp_path,'dual',model=Model([value]),planning_mode='adaptive',planning_memory_mode='off')
    assert result['status']=='completed',result
    assert result['requirement_acceptance']['jointly_accepted']==1
    folder=Path(result['outcome']['directory'])/'sample_00000'
    case=parse_matpower(folder/'matpower/case_generated.m')
    assert len(case['bus'])==69
    assert sum(row[2]>0 for row in case['bus'])==48
    assert sum(row[10]==0 for row in case['branch'])==2
    assert sum(row[1]==2 for row in case['bus'])==0  # photovoltaic is not a voltage-controlled PV bus
    assert sum(row[1] for row in case['gen'][1:])==pytest.approx(.1)
    check=json.loads((folder/'matpower/validation.json').read_text())
    assert check['verified'] and check['converged']
    assert check['max_bus_voltage_error_pu']<1e-5
    metadata=json.loads((folder/'matpower/metadata.json').read_text())
    assert metadata['source_boundary']['kind']=='fixed_dss_terminal_voltage'
    assert metadata['source_boundary']['preserves_thevenin_impedance'] is False
    from feeder_agents.requirement_contract import delivered_formats
    from feeder_agents.schemas import Feeder
    feeder=Feeder.model_validate_json((folder/'feeder.json').read_text())
    brief=_compile(AdaptiveProposal.model_validate(value),request)
    path=folder/'matpower/case_generated.m';text=path.read_text();path.write_text(text.replace('mpc.baseMVA = 1;', 'mpc.baseMVA = 2;'))
    assert delivered_formats(folder,brief,feeder)==['opendss']


def test_unbalanced_dual_format_cannot_be_silently_approximated():
    value=dual_proposal();value['single_voltage_spec']['phase_design']={'mode':'unbalanced'}
    with pytest.raises(ValueError,match='balanced|\u4e09\u76f8|MATPOWER'):
        _compile(AdaptiveProposal.model_validate(value),'\u751f\u621069\u8282\u70b9\u79d1\u7814\u914d\u7f51\uff1b\u8f93\u51faOpenDSS\u548cMATPOWER')


def test_export_rejects_asymmetric_catalogue_matrix_even_with_balanced_label(tmp_path):
    import feeder_agents.distribution_matpower as module
    spec=ExperimentSpec(n_buses=5,total_kw_min=100,total_kw_max=100)
    feeder=generate_feeder(spec,123)
    from feeder_agents.rules import data
    catalogue=data('conductors.json')
    asymmetric=next((name for name,c in catalogue.items() if c.get('nphases')==3 and 'source_feeder' in c
        and max(c['rmatrix'][::4])-min(c['rmatrix'][::4])>1e-6),None)
    assert asymmetric is not None
    feeder.lines[0].conductor=asymmetric;feeder.lines[0].construction=catalogue[asymmetric]['construction']
    with pytest.raises(ValueError,match='symmetric|asymmetric'):
        module.build_case(feeder,spec,dict(converged=True))


@pytest.mark.parametrize('construction',['illustrative','cable'])
def test_line_units_charging_and_cross_solver_power_flow(tmp_path,construction):
    from feeder_agents.distribution_matpower import build_case,export_distribution_matpower
    from feeder_agents.rules import data
    spec=ExperimentSpec(n_buses=8,total_kw_min=200,total_kw_max=200,pv_ratio=.3)
    feeder=generate_feeder(spec,123)
    for line in feeder.lines:line.construction=construction
    sim=simulate(export_dss(feeder,tmp_path/'opendss'))
    case,meta=build_case(feeder,spec,sim)
    first=feeder.lines[0];c=data('conductors.json')[first.conductor]
    assert case['branch'][0,2]==pytest.approx(c['r1']*first.length_km/100)
    assert case['branch'][0,3]==pytest.approx(c['x1']*(.3 if construction=='cable' else 1)*first.length_km/100)
    expected_b=2*np.pi*50*200e-9*first.length_km*100 if construction=='cable' else 0
    assert case['branch'][0,4]==pytest.approx(expected_b)
    check=export_distribution_matpower(feeder,spec,sim,tmp_path/'matpower')
    assert check['verified'],check
    assert check['max_bus_voltage_error_pu']<1e-5
    assert check['loss_error_kw']<.01


@pytest.mark.parametrize('package',['pypower','scipy','numpy'])
def test_dual_export_resume_rejects_changed_solver_version(tmp_path,monkeypatch,package):
    import feeder_agents.workflow as workflow
    from feeder_agents.planning import build_plan
    plan=build_plan('\u5e73\u8861\u79d1\u7814case\u53cc\u683c\u5f0f',dict(n_buses=5,n_loads_min=4,n_loads_max=4,
        total_kw_min=100,total_kw_max=100,equipment_design={'mode':'legacy'}))
    plan=ExperimentPlan.model_validate({**plan.model_dump(),'export_formats':['opendss','matpower']})
    result=workflow.run_experiment(plan.spec,tmp_path,'solver_version',plan=plan)
    assert result['accepted']==1
    check=tmp_path/'experiments/solver_version/sample_00000/matpower/validation.json'
    assert json.loads(check.read_text())['verified']
    real_version=workflow.importlib.metadata.version
    monkeypatch.setattr(workflow.importlib.metadata,'version',
        lambda name: real_version(name)+'-changed' if name==package else real_version(name))
    with pytest.raises(ValueError,match='configuration/rules/software differ'):
        workflow.run_experiment(plan.spec,tmp_path,'solver_version',plan=plan)
