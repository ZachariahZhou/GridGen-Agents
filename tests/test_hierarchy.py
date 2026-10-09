import math
import pytest

from feeder_agents.hierarchy import HierarchicalSpec, generate_hierarchy, export_hierarchy, evaluate_hierarchy
from feeder_agents.simulation import simulate


def test_node_contract_and_power_not_duplicated():
    spec=HierarchicalSpec(transformer_count=2,users=12,total_kw=48)
    f=generate_hierarchy(spec,42)
    assert len(f.buses)==spec.mv_buses+2*(1+spec.lv_branches)+12
    assert sum(x.kw for x in f.loads)==pytest.approx(48)
    assert len(f.transformers)==2
    buses={b.id:b for b in f.buses}
    assert all(buses[l.bus].voltage_kv==.4 for l in f.loads)
    assert all(buses[e.bus1].voltage_kv==buses[e.bus2].voltage_kv for e in f.lines)
    assert HierarchicalSpec(transformer_count=2,n_buses=21).users==12
    with pytest.raises(ValueError):HierarchicalSpec(transformer_count=2,users=12,n_buses=22)


def test_dss_multivoltage_and_transformer_overload(tmp_path):
    spec=HierarchicalSpec(transformer_count=2,users=12,total_kw=48)
    f=generate_hierarchy(spec,42)
    result=simulate(export_hierarchy(f,tmp_path/'base'))
    checks=evaluate_hierarchy(f,spec,result)
    assert checks['accepted'],checks
    assert all(math.isclose(result['bus_base_kv_ln'][b.id],b.voltage_kv/math.sqrt(3),rel_tol=1e-6) for b in f.buses)
    assert len(result['transformer_metrics'])==2
    overloaded=f.model_copy(deep=True)
    overloaded.transformers[0].kva=1
    bad=simulate(export_hierarchy(overloaded,tmp_path/'overloaded'))
    assert not evaluate_hierarchy(overloaded,spec,bad)['checks']['transformer_capacity']


def test_wrong_voltage_and_missing_measurements_fail(tmp_path):
    spec=HierarchicalSpec(transformer_count=1,users=6,total_kw=12)
    f=generate_hierarchy(spec,1)
    result=simulate(export_hierarchy(f,tmp_path))
    assert evaluate_hierarchy(f,spec,result)['accepted']
    result['bus_base_kv_ln'][f.loads[0].bus]=10/math.sqrt(3)
    assert not evaluate_hierarchy(f,spec,result)['checks']['voltage_bases']
    result['transformer_metrics']={}
    assert not evaluate_hierarchy(f,spec,result)['checks']['measurements_complete']


def test_nonfinite_transformer_terminal_measurement_is_not_accepted(tmp_path):
    spec=HierarchicalSpec(transformer_count=1,users=6,total_kw=12)
    f=generate_hierarchy(spec,1)
    result=simulate(export_hierarchy(f,tmp_path))
    result['transformer_metrics'][f.transformers[0].id]['terminal_kva'][1]=float('nan')
    checked=evaluate_hierarchy(f,spec,result)
    assert not checked['checks']['measurements_complete']
    assert not checked['accepted']


def test_natural_language_plan_keeps_total_node_semantics(tmp_path):
    from feeder_agents.design import design_from_request
    from feeder_agents.schemas import DesignIntent
    class Model:
        model_name='scripted'
        def with_structured_output(self,*args,**kwargs):return self
        def invoke(self,messages):
            return DesignIntent(summary='\u5206\u5c42\u9988\u7ebf',task='hierarchical',assignments=[
                {'field':'hierarchy.lv_installation_decision','origin':'inferred','reason':'\u7f3a\u5c11\u8d70\u5eca\u4fe1\u606f\uff0c\u91c7\u7528\u660e\u786e\u7684\u79d1\u7814\u5047\u8bbe',
                 'value':{'selected':'aerial_bundle','reason':'\u672a\u6307\u5b9a\u6577\u8bbe\uff0c\u9009\u62e9\u67b6\u7a7a\u57fa\u7ebf\u7814\u7a76\u6848\u4f8b\uff0c\u4e0d\u4ece\u57ce\u4e61\u6807\u7b7e\u63a8\u65ad',
                    'factors':[{'criterion':'research_objective','observation':'\u67b6\u7a7a\u57fa\u7ebf\u79d1\u7814\u5047\u8bbe','origin':'assumption','evidence':''}],
                    'alternatives':[{'installation':i,'reason':'\u4f5c\u4e3a\u79d1\u7814\u5019\u9009\u6bd4\u8f83\uff0c\u672a\u77e5\u5b9e\u9645\u8d70\u5eca'} for i in ('aerial_bundle','buried_direct','buried_duct')],
                    'unknowns':['\u672a\u7ed9\u8d70\u5eca\u53ca\u65e2\u6709\u8bbe\u65bd\u4fe1\u606f']}},
                {'field':'hierarchy.n_buses','value':37,'origin':'inferred','reason':'\u53ef\u88ab\u8f93\u5165\u6846\u8986\u76d6\u7684\u63a8\u65ad'},
                {'field':'hierarchy.transformer_count','value':2,'origin':'user','evidence':'2\u53f0\u914d\u53d8','reason':'\u660e\u786e\u53f0\u6570'},
                {'field':'hierarchy.total_kw','value':48,'origin':'user','evidence':'48kW','reason':'\u660e\u786e\u8d1f\u8377'}])
    out=design_from_request('\u4e2d\u4f4e\u538b\u5206\u5c42\u9988\u7ebf\uff0c2\u53f0\u914d\u53d8\uff0c48kW\u3002',tmp_path,'nl',execute=False,node_count=21,model=Model())
    assert out['plan_type']=='hierarchical'
    assert out['plan']['spec']['users']==12
    assert out['plan']['spec']['n_buses']==21
