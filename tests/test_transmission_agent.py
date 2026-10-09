import numpy as np
from feeder_agents.transmission import TransmissionSpec,generate_case,validate_case


def test_voltage_layers_have_real_transformers():
    spec=TransmissionSpec(n_buses=12,n_generators=4,total_mw=180,voltage_layers=[{'kv':220,'buses':6},{'kv':110,'buses':6}])
    case,meta=generate_case(spec,42)
    assert set(case['bus'][:,9])=={110,220}
    assert len([e for e in meta['branch_evidence'] if e['kind']=='transformer'])==2
    checked,_=validate_case(case,spec,meta)
    assert checked['accepted'],checked


def test_feedback_repairs_compensation_without_relaxing_limits(tmp_path):
    from feeder_agents.transmission_feedback import run_feedback
    class Model:
        def with_structured_output(self,*args,**kwargs):return self
        def invoke(self,messages):
            import json
            options=json.loads(messages[-1][1])['options']
            selected=next(k for k,v in options.items() if v['kind']=='shunt_step')
            return dict(decision='repair',action_id=selected,diagnosis='Reactive limit',reason='Adjust physical compensation')
    spec=TransmissionSpec(n_buses=9,n_generators=3,voltage_kv=750,total_mw=90,shunt_compensation=0.)
    case,meta=generate_case(spec,42)
    result=run_feedback(case,meta,spec,tmp_path,model=Model(),max_rounds=6)
    assert result['report']['accepted_steps']>0
    assert result['assessment']['checked']['accepted']
    np.testing.assert_array_equal(case['bus'][:,2:4],result['case']['bus'][:,2:4])


class SelectShunt:
    def with_structured_output(self,*args,**kwargs):return self
    def invoke(self,messages):
        import json
        options=json.loads(messages[-1][1])['options']
        key=next(k for k,v in options.items() if v['kind']=='shunt_step')
        return dict(decision='repair',action_id=key,diagnosis='Reactive absorption exceeds limit',reason='Install bounded reactor compensation')


def test_memory_replay_deduplicates_and_rejects_tamper(tmp_path):
    from feeder_agents.transmission_feedback import run_feedback
    from feeder_agents.transmission_memory import TransmissionMemory
    spec=TransmissionSpec(n_buses=12,voltage_kv=750,total_mw=90,shunt_compensation=.4)
    case,meta=generate_case(spec,42);memory=TransmissionMemory(tmp_path/'memory.sqlite')
    out=run_feedback(case,meta,spec,tmp_path/'loop',model=SelectShunt(),max_rounds=6,memory=memory)
    assert out['assessment']['checked']['accepted']
    first=memory.retrieve(spec);assert first
    memory.ingest(tmp_path/'loop',spec);assert memory.retrieve(spec)==first
    assert not memory.retrieve(spec.model_copy(update={'total_mw':91}))
    (tmp_path/'loop/candidate_0000/case.json').write_text('{}')
    assert not memory.retrieve(spec)


def test_local_corridor_requires_permission_and_preserves_demands():
    from feeder_agents.transmission_feedback import assessment,propose,apply,preserved
    spec=TransmissionSpec(n_buses=12,total_mw=180,allow_topology_changes=True,mesh_style='mixed')
    case,meta=generate_case(spec,42);a,solved=assessment(case,meta,spec)
    action=next(o for o in propose(case,meta,spec,a,solved) if o['kind']=='relocate_corridor')
    c,m=apply(case,meta,spec,action)
    assert preserved(case,meta,c,m,spec)
    assert validate_case(c,spec,m)[0]['checks']['topology']
    import pytest
    with pytest.raises(ValueError):apply(case,meta,spec.model_copy(update={'allow_topology_changes':False}),action)


def test_illegal_model_action_cannot_change_case(tmp_path):
    from feeder_agents.transmission_feedback import run_feedback,fingerprint
    class Bad(SelectShunt):
        def invoke(self,messages):return dict(decision='repair',action_id='not_offered',diagnosis='bad',reason='bad')
    spec=TransmissionSpec(voltage_kv=750,total_mw=90,shunt_compensation=0.)
    c,m=generate_case(spec,42);out=run_feedback(c,m,spec,tmp_path,model=Bad())
    assert out['report']['stop_reason']=='agent_error'
    assert fingerprint(c,m)==fingerprint(out['case'],out['metadata'])


def test_versioned_revision_preserves_source_and_supports_targets(tmp_path):
    from feeder_agents.transmission import run_transmission,revise_transmission,read_transmission_result
    import json
    spec=TransmissionSpec(n_buses=9,total_mw=180)
    original=run_transmission(spec,tmp_path,'parent');before=original['result_hash']
    revised=revise_transmission(tmp_path,'parent','revision',targets=[dict(metric='max_branch_loading',operator='le',threshold=1.)],allowed_repairs=[])
    assert revised['accepted']==1
    assert read_transmission_result(original['directory'])['result_hash']==before
    assert json.loads((tmp_path/'transmission_experiments/revision/manifest.json').read_text())['parent']['parent_result_hash']==before


def test_conditions_share_equipment_and_report_unmet_limits():
    spec=TransmissionSpec(n_buses=9,total_mw=180,validation_conditions=[dict(name='peak',load_scale=2.)])
    case,meta=generate_case(spec,42);checked,_=validate_case(case,spec,meta)
    assert not checked['accepted'] and not checked['checks']['condition:peak']
    assert checked['conditions'][0]['metrics']['demand_mw']==360.
    assert not checked['conditions'][0]['checks']['generator_p_limits']


def test_homogeneous_generator_type_shorthand():
    spec=TransmissionSpec(n_generators=4,generator_types=['thermal'])
    assert spec.generator_types==['thermal']*4


def test_document_rule_is_enforced_with_evidence():
    rule=dict(rule_id='doc.'+'1'*20,document_id='2'*64,source_title='research rule',chunk_index=0,voltage_levels_kv=[220],metric='max_line_km',operator='le',threshold=.01,unit='km',quote='\u6bcf\u6bb5\u7ebf\u8def\u7684\u957f\u5ea6\u4e0d\u5f97\u8d85\u8fc70.01km',locator='section 1')
    spec=TransmissionSpec(n_buses=9,total_mw=180,document_rules=[rule]);case,meta=generate_case(spec,42)
    checked,_=validate_case(case,spec,meta)
    assert not checked['accepted'] and not checked['document_rules'][0]['passed']
    assert checked['document_rules'][0]['quote']==rule['quote']


def test_joint_repair_rechecks_all_internal_conditions(tmp_path):
    from feeder_agents.transmission_feedback import run_feedback
    spec=TransmissionSpec(n_buses=12,voltage_kv=750,total_mw=90,shunt_compensation=.4,allowed_repairs=['shunt_step'],validation_conditions=[dict(name='load_110',load_scale=1.1)])
    case,meta=generate_case(spec,42)
    out=run_feedback(case,meta,spec,tmp_path,model=SelectShunt(),max_rounds=6)
    assert out['assessment']['checked']['accepted']
    assert out['assessment']['checked']['conditions'][0]['accepted']
