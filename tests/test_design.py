import pytest

from feeder_agents.design import compile_intent
from feeder_agents.schemas import DesignIntent


def test_compile_preserves_explicit_values_and_documents_inferences(tmp_path):
    request='设计农村馈线，总负荷2MW，12个负荷点。'
    intent=DesignIntent(summary='农村研究馈线',assignments=[
        {'field':'scenario.kind','value':'rural','origin':'user','evidence':'农村','reason':'场景'},
        {'field':'total_kw_min','value':2000,'origin':'user','evidence':'总负荷2MW','reason':'MW转kW'},
        {'field':'total_kw_max','value':2000,'origin':'user','evidence':'总负荷2MW','reason':'固定负荷'},
        {'field':'n_loads_min','value':12,'origin':'user','evidence':'12个负荷点','reason':'固定数量'},
        {'field':'n_loads_max','value':12,'origin':'user','evidence':'12个负荷点','reason':'固定数量'},
        {'field':'repair_policy.strategy','value':'heuristic','origin':'inferred','reason':'采用离线有界修复'}])
    result=compile_intent(request,intent,tmp_path)
    assert result['status']=='ready'
    assert result['plan']['spec']['total_kw_min']==2000
    assert result['plan']['parameter_origins']['repair_policy.strategy']=='inferred'
    assert result['plan']['spec']['scenario']['kind']=='rural'


def test_unsupported_and_missing_evidence_cannot_be_executed(tmp_path):
    intent=DesignIntent(summary='不平衡馈线',unsupported=['三相不平衡尚未支持'])
    assert compile_intent('设计不平衡馈线',intent,tmp_path)['status']=='unsupported'
    intent=DesignIntent(summary='测试',assignments=[{'field':'pv_ratio','value':.5,
        'origin':'user','evidence':'光伏50%','reason':'用户要求'}])
    with pytest.raises(ValueError,match='evidence'):
        compile_intent('设计一条馈线',intent,tmp_path)


def test_node_count_is_optional_but_explicit_ui_count_is_exact(tmp_path):
    from feeder_agents.design import design_from_request
    class Model:
        model_name='scripted'
        def with_structured_output(self,*args,**kwargs): return self
        def invoke(self,*args,**kwargs): return DesignIntent(summary='农村研究馈线')
    automatic=design_from_request('设计一条馈线',tmp_path,'auto',execute=False,model=Model())
    assert automatic['status']=='draft'
    assert automatic['plan']['spec']['n_loads_min']==20
    specified=design_from_request('设计一条馈线',tmp_path,'explicit',execute=False,model=Model(),node_count=13)
    assert specified['plan']['spec']['n_loads_min']==specified['plan']['spec']['n_loads_max']==12


def test_invalid_default_evidence_gets_one_bounded_correction(tmp_path):
    from feeder_agents.design import design_from_request
    class Model:
        calls=0
        def with_structured_output(self,*args,**kwargs): return self
        def invoke(self,messages):
            self.calls+=1
            if self.calls==1:
                return DesignIntent(summary='研究馈线',assignments=[{'field':'frequency_hz','value':50,
                    'origin':'user','evidence':'50Hz','reason':'默认'}])
            assert 'User evidence not found' in messages[-1][1]
            return DesignIntent(summary='研究馈线')
    model=Model()
    result=design_from_request('生成一条馈线',tmp_path,'corrected',execute=False,model=model)
    assert model.calls==2
    assert result['plan']['parameter_origins']['frequency_hz']=='default'
    assert len(result['interpretation_corrections'])==1


def test_conflicting_explicit_node_counts_do_not_generate(tmp_path):
    from feeder_agents.design import design_from_request
    class Model:
        def with_structured_output(self,*args,**kwargs): return self
        def invoke(self,*args,**kwargs):
            return DesignIntent(summary='研究馈线',assignments=[{'field':'n_loads_min','value':8,
                'origin':'user','evidence':'8个负荷点','reason':'用户要求'}])
    with pytest.raises(ValueError,match='节点数量冲突'):
        design_from_request('8个负荷点',tmp_path,'conflict',model=Model(),node_count=13)
    assert not (tmp_path/'experiments'/'conflict').exists()


def test_schema_parse_error_is_corrected_and_raw_response_is_saved(tmp_path):
    import json
    from langchain_core.messages import AIMessage
    from feeder_agents.design import interpret_request
    class Model:
        calls=0
        def with_structured_output(self,*args,**kwargs):
            assert kwargs['include_raw'] is True
            return self
        def invoke(self,messages):
            self.calls+=1
            if self.calls==1:
                return dict(raw=AIMessage(content='',tool_calls=[dict(name='DesignIntent',args={'summary':'bad','assignments':[{'reason':'x'*601}]},id='call_1')]),parsed=None,parsing_error=ValueError('reason exceeds 600 characters'))
            assert 'reason exceeds 600' in messages[-1][1]
            return dict(raw=AIMessage(content='repaired'),parsed=DesignIntent(summary='研究馈线'),parsing_error=None)
    model=Model();result=interpret_request('生成科研馈线',tmp_path,model=model)
    assert result['status']=='ready' and model.calls==2
    files=list((tmp_path/'model_calls').rglob('*.json'))
    assert len(files)==2
    records=[json.loads(p.read_text()) for p in sorted(files)]
    assert records[0]['raw']['tool_calls'][0]['args']['assignments'][0]['reason']=='x'*601
    assert records[0]['status']=='invalid' and records[1]['status']=='valid'
    assert len(result['interpretation_traces'])==2


def test_structural_correction_is_bounded_and_transport_is_not_retried(tmp_path):
    from feeder_agents.design import interpret_request
    class Model:
        calls=0
        error=False
        def with_structured_output(self,*args,**kwargs):return self
        def invoke(self,messages):
            self.calls+=1
            if self.error:raise TimeoutError('unavailable')
            return dict(raw={'content':'bad'},parsed=None,parsing_error=ValueError('invalid schema'))
    model=Model()
    with pytest.raises(ValueError,match='仍未通过'):interpret_request('科研馈线',tmp_path/'invalid',model=model)
    assert model.calls==2
    model=Model();model.error=True
    with pytest.raises(TimeoutError):interpret_request('科研馈线',tmp_path/'timeout',model=model)
    assert model.calls==1
    assert len(list((tmp_path/'timeout/model_calls').rglob('*.json')))==1


def test_hierarchical_scope_excludes_other_task_fields(tmp_path):
    from feeder_agents.design import interpret_request
    class Model:
        def with_structured_output(self,schema,**kwargs):
            props=schema.model_json_schema()
            assert props['properties']['task']['const']=='hierarchical'
            assignment=next(v for k,v in props['$defs'].items() if k=='HierarchyAssignment')
            fields=assignment['properties']['field']['enum']
            assert 'hierarchy.users' in fields and 'phase_design' not in fields
            return self
        def invoke(self,messages):
            assert 'taxonomy_style_references' not in messages[1][1]
            return dict(summary='不支持',task='hierarchical',unsupported=['显式中性线'])
    assert interpret_request('显式中性线',tmp_path,model=Model(),task_scope='hierarchical')['status']=='unsupported'
