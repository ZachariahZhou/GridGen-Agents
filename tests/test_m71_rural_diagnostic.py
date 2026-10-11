"""Known rural layout errors receive narrow, ledger-preserving repair authority."""
import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from feeder_agents.adaptive_planning import AdaptiveProposal, _compile
from feeder_agents.planning_diagnostics import check_adjustment, failure_bundle, PlanningValidationError
from feeder_agents.source_review import check_source_numbers
from feeder_agents.structured_planning import StructuredPlanningError

FIXTURES=Path(__file__).parent/'fixtures/m71_rural_diagnostic'
SOURCE='\u751f\u621010kV\u519c\u6751\u5355\u7535\u538b\u5e73\u8861\u9988\u7ebf\uff0c35\u4e2a\u6bcd\u7ebf\uff0c\u4e3b\u5e72\u8fde\u63a53\u4e2a\u6751\u843d\uff0c\u603b\u8d1f\u8377500kW\uff0c\u5149\u4f0f\u4e3a20%\uff0c\u8f93\u51faOpenDSS\u548cMATPOWER\u3002'


def candidate():
    fields=[('voltage_kv',10),('n_buses',35),('scenario.kind','rural'),('scenario.village_count',3),
            ('total_kw_min',500),('total_kw_max',500),('pv_ratio',.2),('phase_design.mode','balanced')]
    requirements=[dict(id=f'req_{i}',segment_ids=[0],evidence=SOURCE[:-1],meaning=field,
                      priority='hard',disposition='supported',target_field=field,expected_value=value,operator='eq')
                  for i,(field,value) in enumerate(fields)]
    requirements.append(dict(id='exports',segment_ids=[0],evidence=SOURCE[:-1],meaning='Requested formats',
        priority='hard',disposition='supported',target_field='deliverables',expected_value=['opendss','matpower'],operator='contains'))
    return dict(status='ready',family='single_voltage',single_voltage_spec=dict(n_buses=35,total_kw_min=500,total_kw_max=500,
        voltage_kv=10,pv_ratio=.2,seed=1709,scenario=dict(kind='rural',layout='structured_radial',
        topology=dict(family='multi_branch',branch_count=3),village_count=3),phase_design=dict(mode='balanced')),
        ledger=dict(network_kind='distribution',model_family='single_voltage',summary='Public source contract',requirements=requirements))


def diagnosed(proposal,request=SOURCE):
    with pytest.raises(ValidationError) as caught:AdaptiveProposal.model_validate(proposal)
    error=StructuredPlanningError(str(caught.value),caught.value)
    return failure_bundle(error,'schema',request,proposal)


def repair_action(bundle):
    actions=[a for a in bundle['allowed_actions'] if a['id']=='repair_representation']
    assert len(actions)==1,bundle['allowed_actions']
    return actions[0]


def repaired(proposal):
    value=copy.deepcopy(proposal)
    value['single_voltage_spec']['scenario'].update(layout='rural_villages',topology=None)
    return value


@pytest.mark.parametrize('name',['r035','r073'])
def test_saved_public_failure_gets_exact_coupled_layout_scope_without_mutation(name):
    saved=json.loads((FIXTURES/f'{name}.json').read_text());before=copy.deepcopy(saved['candidate'])
    bundle=diagnosed(saved['candidate'],saved['request']);action=repair_action(bundle)
    assert set(action['paths'])=={'single_voltage_spec.scenario.layout','single_voltage_spec.scenario.topology'}
    assert action['preserve_requirement_bindings'] is True
    assert saved['candidate']==before


def test_valid_village_binding_can_reach_compile_and_source_review():
    before=candidate();action=repair_action(diagnosed(before));after=repaired(before)
    check_adjustment(before,after,action)
    assert after['ledger']==before['ledger']
    brief=_compile(AdaptiveProposal.model_validate(after),SOURCE)
    check_source_numbers(SOURCE,brief)
    assert brief['status']=='ready'
    assert brief['plan']['spec']['n_buses']==35
    assert set(brief['plan']['export_formats'])=={'opendss','matpower'}


def test_existing_rural_layout_only_authorizes_clearing_incompatible_topology():
    before=candidate();before['single_voltage_spec']['scenario']['layout']='rural_villages'
    action=repair_action(diagnosed(before))
    assert action['paths']==['single_voltage_spec.scenario.topology']
    check_adjustment(before,repaired(before),action)


@pytest.mark.parametrize('name',['r035','r073'])
def test_saved_wrong_branch_binding_normalizes_at_compile_without_rewriting_candidate(name):
    saved=json.loads((FIXTURES/f'{name}.json').read_text());before=saved['candidate']
    after=repaired(before);action=repair_action(diagnosed(before,saved['request']))
    check_adjustment(before,after,action)
    assert after['ledger']==before['ledger']
    brief=_compile(AdaptiveProposal.model_validate(after),saved['request'])
    check_source_numbers(saved['request'],brief)
    assert brief['status']=='ready'
    spec=brief['plan']['spec']
    assert spec['scenario']['village_count'] in (3,4)
    assert spec['scenario']['layout']=='rural_villages'
    assert spec['n_buses']==(35 if name=='r035' else 75)
    assert spec['total_kw_min']==spec['total_kw_max']==(500 if name=='r035' else 1000)
    assert spec['pv_ratio']==.2 and spec['phase_design']['mode']==('balanced' if name=='r035' else 'unbalanced')
    assert set(brief['plan']['export_formats'])==({'opendss','matpower'} if name=='r035' else {'opendss'})
    changes=[c for c in brief['requirement_normalizations'] if c['kind']=='village_count_mapping']
    assert len(changes)==1
    assert changes[0]['before']['target_field']=='scenario.topology.branch_count'
    assert changes[0]['after']['target_field']=='scenario.village_count'
    assert changes[0]['after']['evidence'] in saved['request']
    assert changes[0]['after']['expected_value']==spec['scenario']['village_count']
    assert before['ledger']['requirements']==saved['candidate']['ledger']['requirements']


@pytest.mark.parametrize('path,value',[
    ('n_buses',36),('total_kw_min',490),('total_kw_max',510),('pv_ratio',0),('seed',701),('count',2),
    ('voltage_kv',20),('scenario.village_count',4),('scenario.tie_count',1),('phase_design.mode','unbalanced'),
    ('phase_design.max_vuf_percent',3),('equipment_design.loading_margin',1),
])
def test_scoped_rural_representation_cannot_change_other_design_or_acceptance(path,value):
    before=candidate();action=repair_action(diagnosed(before));after=repaired(before)
    node=after['single_voltage_spec'];*parents,leaf=path.split('.')
    for parent in parents:node=node.setdefault(parent,{})
    node[leaf]=value
    with pytest.raises(ValueError):check_adjustment(before,after,action)


@pytest.mark.parametrize('change',['value','priority','target','remove','exports'])
def test_rural_representation_cannot_rewrite_or_remove_hard_requirements(change):
    before=candidate();action=repair_action(diagnosed(before));after=repaired(before)
    requirements=after['ledger']['requirements']
    if change=='value':requirements[3]['expected_value']=4
    elif change=='priority':requirements[3]['priority']='preference'
    elif change=='target':requirements[3]['target_field']='scenario.topology.branch_count'
    elif change=='remove':requirements.pop(3)
    else:requirements[-1]['expected_value']=['opendss']
    with pytest.raises(ValueError,match='valid requirement binding'):check_adjustment(before,after,action)


@pytest.mark.parametrize('conflict',['other_range','urban','coordinates','unsupported_voltage'])
def test_probe_grants_no_layout_authority_when_other_constraints_prevent_validation(conflict):
    before=candidate();spec=before['single_voltage_spec']
    if conflict=='other_range':spec['total_kw_min']=600
    elif conflict=='urban':spec['scenario']['kind']='urban'
    elif conflict=='coordinates':spec['scenario']['positions_km']=[(i*.1,0) for i in range(35)]
    else:spec['voltage_kv']=.4
    assert [a['id'] for a in diagnosed(before)['allowed_actions']]==['stop']


def test_fixed_source_layout_cannot_be_silently_overridden_at_compile():
    before=candidate()
    before['ledger']['requirements'].append(dict(id='fixed_layout',segment_ids=[0],evidence=SOURCE[:-1],meaning='Fixed layout',
        priority='hard',disposition='supported',target_field='scenario.layout',expected_value='structured_radial',operator='eq'))
    after=repaired(before);check_adjustment(before,after,repair_action(diagnosed(before)))
    with pytest.raises(PlanningValidationError,match='scenario.layout'):
        _compile(AdaptiveProposal.model_validate(after),SOURCE)


def test_existing_structured_layout_repair_scope_does_not_expand():
    before=candidate();scene=before['single_voltage_spec']['scenario'];scene.pop('village_count');scene['layout']='spatial_mst'
    assert repair_action(diagnosed(before))['paths']==['single_voltage_spec.scenario.layout']


@pytest.mark.parametrize('name',['r035','r073'])
def test_saved_initial_proposal_recovers_with_offered_patch_and_normalization(name,tmp_path):
    from feeder_agents.adaptive_planning import adaptive_plan
    from feeder_agents.artifacts import digest
    from test_m52_diagnostic_loop import decision_from_context
    saved=json.loads((FIXTURES/f'{name}_trajectory.json').read_text())
    first=copy.deepcopy(saved['candidates'][0])
    class OfferedPatchModel:
        model_name='offline-offered-patch-selector'
        calls=[]
        def with_structured_output(self,schema,**kwargs):
            parent=self
            class Bound:
                def invoke(self,messages):
                    parent.calls.append(schema.__name__)
                    if schema.__name__=='FailureDiagnosis':return decision_from_context(messages)
                    if schema.__name__=='SemanticReview':return dict(approved=True,issues=[])
                    if parent.calls.count('AdaptiveProposal')==1:return copy.deepcopy(first)
                    context=json.loads(messages[-1][1]);action=context['allowed_adjustment']
                    return dict(status='ready',family=context['previous_response']['family'],
                        base_candidate_hash=context['base_candidate_hash'],edits=action['validated_repairs'][0]['edits'])
            return Bound()
    model=OfferedPatchModel();result=adaptive_plan(saved['request'],tmp_path,model)
    assert result['status']=='ready'
    spec=result['plan']['spec']
    assert spec['scenario']['layout']=='rural_villages'
    assert spec['scenario']['village_count']==(3 if name=='r035' else 4)
    assert spec['pv_ratio']==.2 and spec['voltage_kv']==10
    assert spec['n_buses']==first['single_voltage_spec']['n_buses']
    assert spec['total_kw_min']==first['single_voltage_spec']['total_kw_min']
    check_source_numbers(saved['request'],result)
    assert result['requirement_acceptance']['all_satisfied']
    records=[json.loads(p.read_text()) for p in sorted((tmp_path/'failure_analysis').glob('*.json'))]
    assert all(r['adjustment']['protocol']=='scoped_patch' for r in records)
    assert model.calls.count('AdaptiveProposal')<=3


@pytest.mark.parametrize('name',['r035','r073'])
def test_public_source_only_mapping_is_normalized_with_exact_quote(name):
    saved=json.loads((FIXTURES/f'{name}.json').read_text());before=repaired(saved['candidate'])
    original=copy.deepcopy(before)
    wrong=[r for r in before['ledger']['requirements'] if r['target_field']=='scenario.topology.branch_count']
    assert len(wrong)==1 and wrong[0]['evidence'] in saved['request']
    assert '\u6751\u843d' in wrong[0]['evidence'] and '\u652f\u7ebf' not in wrong[0]['evidence']
    brief=_compile(AdaptiveProposal.model_validate(before),saved['request'])
    check_source_numbers(saved['request'],brief)
    assert before==original
    assert brief['status']=='ready'
    changed=next(r for r in brief['requirement_ledger']['requirements'] if r['id']==wrong[0]['id'])
    assert changed['target_field']=='scenario.village_count'
    assert changed['expected_value']==wrong[0]['expected_value']
    assert changed['evidence']==wrong[0]['evidence']
    assert changed['segment_ids']==wrong[0]['segment_ids']
    assert brief['requirement_acceptance']['all_satisfied']
