"""Recorded transmission compiler errors get schema- and source-bound correction."""
import copy
import json
from pathlib import Path

import pytest


FIXTURE=json.loads((Path(__file__).parent/'fixtures/m98_transmission_research_failures.json').read_text())


class ReplayModel:
    def __init__(self,candidates):
        self.candidates=candidates
        self.calls=0

    def with_structured_output(self,*args,**kwargs):
        return self

    def invoke(self,messages):
        candidate=self.candidates[min(self.calls,len(self.candidates)-1)]
        self.calls+=1
        return copy.deepcopy(candidate)


def corrected_candidate():
    candidate=copy.deepcopy(FIXTURE['attempts']['initial']['candidate'])
    candidate['plan']['base_spec']['allowed_repairs']=[]
    candidate['plan']['assumptions']=['Automatic repairs are explicitly disabled by the user.']
    return candidate


@pytest.mark.parametrize('second',['misplaced_repairs','misplaced_issues','dropped_bounds'])
def test_recorded_semantic_then_schema_failure_can_be_corrected_without_outer_restart(tmp_path,second):
    from feeder_agents.research_language import design_research_from_request
    model=ReplayModel([FIXTURE['attempts']['initial']['candidate'],
                       FIXTURE['attempts'][second]['candidate'],corrected_candidate()])
    result=design_research_from_request(FIXTURE['source_request'],tmp_path,'corrected',execute=False,model=model)
    assert result['status']=='draft'
    assert model.calls==3
    assert result['plan']['base_spec']['allowed_repairs']==[]
    assert result['plan']['base_spec']['n_buses']==139
    assert result['plan']['base_spec']['seed']==971031
    assert result['plan']['response_lower']==.98 and result['plan']['response_upper']==1.02
    assert result['compiled_plan']['context']['transfer_mw']==1
    calls=sorted((tmp_path/'designs/corrected/model_calls').glob('*/attempt_*.json'))
    assert [json.loads(p.read_text())['status'] for p in calls]==['invalid','invalid','valid']


def test_source_correction_contract_preserves_explicit_bounds_and_disables_repairs_at_actual_schema_path():
    from feeder_agents.research_source import research_source_contract
    contract=research_source_contract(FIXTURE['source_request'],'transmission')
    required={item['path']:item['value'] for item in contract}
    assert required['plan.base_spec.allowed_repairs']==[]
    assert required['plan.response_reference']=='baseline_ratio'
    assert required['plan.response_lower']==.98
    assert required['plan.response_upper']==1.02
    for item in contract:
        assert item['source'] and item['source'] in FIXTURE['source_request']


def test_source_correction_contract_does_not_invent_bounds_for_default_protocol():
    from feeder_agents.research_source import research_source_contract
    contract=research_source_contract('Study transmission transfer using the default task protocol.','transmission')
    assert not any(item['path'].startswith('plan.response_') for item in contract)


def test_repeated_invalid_recorded_candidate_is_bounded_and_never_executed(tmp_path):
    from feeder_agents.research_language import design_research_from_request
    model=ReplayModel([FIXTURE['attempts']['dropped_bounds']['candidate']])
    result=design_research_from_request(FIXTURE['source_request'],tmp_path,'blocked',model=model)
    assert result['status']=='planning_failed' and result['failure_origin']=='compiler'
    assert model.calls==3
    assert not (tmp_path/'research_tasks').exists()
