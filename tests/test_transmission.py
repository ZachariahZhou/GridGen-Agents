import json
import pytest


def test_transmission_has_mesh_generators_and_real_ac_validation(tmp_path):
    from feeder_agents.transmission import TransmissionSpec,generate_case,validate_case,run_transmission
    import networkx as nx
    s=TransmissionSpec(n_buses=9,total_mw=180,n_generators=3,seed=41)
    case,metadata=generate_case(s,41)
    graph=nx.Graph();graph.add_edges_from((int(r[0]),int(r[1])) for r in case['branch'])
    assert nx.is_connected(graph) and len(graph.edges)>=s.n_buses
    assert sum(case['bus'][:,2])==pytest.approx(180)
    assert sum(case['bus'][:,1]==3)==1 and sum(case['bus'][:,1]==2)==2
    checked,solved=validate_case(case,s)
    assert checked['accepted'],checked
    result=run_transmission(s,tmp_path,'transmission')
    assert result['accepted']==1
    assert (tmp_path/'transmission_experiments/transmission/sample_00000/case_generated.m').is_file()


def test_rating_and_generator_limits_cannot_be_hidden_by_convergence():
    from feeder_agents.transmission import TransmissionSpec,generate_case,validate_case
    s=TransmissionSpec(n_buses=9,total_mw=180,n_generators=3)
    case,_=generate_case(s,41);case['branch'][:,5]=.001
    checked,_=validate_case(case,s)
    assert checked['converged'] and not checked['accepted'] and not checked['checks']['branch_limits']


def test_transmission_schema_does_not_accept_distribution_voltage():
    from feeder_agents.transmission import TransmissionSpec
    with pytest.raises(ValueError):TransmissionSpec(voltage_kv=10)


def test_physical_units_and_voltage_dependent_ratings():
    import math
    from feeder_agents.transmission import TransmissionSpec,generate_case,parameter_checks
    ratings=[]
    for kv in (110,220,330,500,750):
        spec=TransmissionSpec(voltage_kv=kv,n_buses=9,total_mw=180)
        case,metadata=generate_case(spec,41)
        assert all(parameter_checks(case,spec,metadata).values())
        e=metadata['branch_evidence'][0];row=case['branch'][0]
        zbase=kv**2/case['baseMVA']
        assert row[2]==pytest.approx(e['r_ohm_km']*e['length_km']/zbase/e['circuits'])
        assert row[5]==pytest.approx(math.sqrt(3)*kv*e['max_i_ka']*e['circuits'])
        ratings.append(row[5])
        case['branch'][0,4]*=2
        assert not parameter_checks(case,spec,metadata)['line_unit_consistency']
    assert ratings==sorted(ratings)
