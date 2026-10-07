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
            return DesignIntent(summary='分层馈线',task='hierarchical',assignments=[
                {'field':'hierarchy.lv_installation_decision','origin':'inferred','reason':'缺少走廊信息，采用明确的科研假设',
                 'value':{'selected':'aerial_bundle','reason':'未指定敷设，选择架空基线研究案例，不从城乡标签推断',
                    'factors':[{'criterion':'research_objective','observation':'架空基线科研假设','origin':'assumption','evidence':''}],
                    'alternatives':[{'installation':i,'reason':'作为科研候选比较，未知实际走廊'} for i in ('aerial_bundle','buried_direct','buried_duct')],
                    'unknowns':['未给走廊及既有设施信息']}},
                {'field':'hierarchy.n_buses','value':37,'origin':'inferred','reason':'可被输入框覆盖的推断'},
                {'field':'hierarchy.transformer_count','value':2,'origin':'user','evidence':'2台配变','reason':'明确台数'},
                {'field':'hierarchy.total_kw','value':48,'origin':'user','evidence':'48kW','reason':'明确负荷'}])
    out=design_from_request('中低压分层馈线，2台配变，48kW。',tmp_path,'nl',execute=False,node_count=21,model=Model())
    assert out['plan_type']=='hierarchical'
    assert out['plan']['spec']['users']==12
    assert out['plan']['spec']['n_buses']==21
