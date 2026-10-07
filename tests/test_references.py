from copy import deepcopy
import pytest
from feeder_agents.references import scale_reference_load, validate_reference_result


def small_case():
    return dict(name='tiny',base_mva=10,bus=[
        [1,3,0,0,0,0,1,1,0,12.66,1,1.1,.9],
        [2,1,.1,.04,0,0,1,1,0,12.66,1,1.1,.9]],
        branch=[[1,2,.01,.02,0,0,0,0,0,0,1,-360,360]],
        gen=[[1,0,0,10,-10,1,10,1,10,0]])


def test_scale_keeps_network_and_zero_load_buses():
    base=small_case(); old=deepcopy(base)
    candidate=scale_reference_load(base,1.5)
    assert base==old
    assert candidate['bus'][1][2:4]==pytest.approx([.15,.06])
    assert candidate['bus'][0][2:4]==[0,0]
    assert candidate['branch']==base['branch']
    with pytest.raises(ValueError): scale_reference_load(base,float('nan'))


def test_unknown_ratings_not_pass_and_missing_voltage_invalid():
    case=small_case()
    result=dict(converged=True,bus_voltage_pu={'b1':[1]*3,'b2':[.99]*3},
                line_current_a={'l1':5},line_terminal_mva={'l1':[.11,.108]},
                source_kw=101,load_kw=100,generation_kw=0,loss_kw=1)
    checks=validate_reference_result(case,result)
    assert checks['valid']
    assert checks['rating_status']=='insufficient_data'
    assert not checks['complete_operational_verification']
    del result['bus_voltage_pu']['b2']
    assert not validate_reference_result(case,result)['valid']


def test_packaged_reference_library_and_variant_roundtrip(tmp_path):
    from feeder_agents.references import list_references,get_reference,write_matpower_variant
    from feeder_agents.matpower import parse_matpower
    rows=list_references()
    assert len(rows)==18
    assert sum(r['statistics']['export_supported'] for r in rows)==16
    assert len(list_references(evidence_class='actual_derived'))==3
    case=scale_reference_load(get_reference('case69')['case'],.8)
    path=tmp_path/'reference_variant.m'
    write_matpower_variant(case,path)
    parsed=parse_matpower(path)
    assert len(parsed['bus'])==69
    assert sum(b[2] for b in parsed['bus'])==pytest.approx(sum(b[2] for b in case['bus']))
    assert parsed['branch'][0]==pytest.approx(case['branch'][0])
