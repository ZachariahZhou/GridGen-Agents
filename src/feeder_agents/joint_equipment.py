"""Empirical length/equipment conditioning and a transparent physical soft prior."""
import math
import networkx as nx


def length_likelihood(key,role,length,cohorts,bandwidth):
    group=cohorts.get(key,{})
    observations=group.get(role,[])
    fallback=not bool(observations)
    if fallback:observations=[v for values in group.values() for v in values]
    kernel=(sum(math.exp(-.5*(math.log(length/v)/bandwidth)**2) for v in observations)/len(observations)
            if observations else 1.)
    return {'likelihood':.1+.9*kernel,'observations':len(observations),'fallback':fallback,
            'within_observed_range':min(observations)<=length<=max(observations) if observations else None}


def longest_path_km(feeder):
    graph=nx.Graph()
    graph.add_weighted_edges_from((e.bus1,e.bus2,e.length_km) for e in feeder.lines)
    return max(nx.single_source_dijkstra_path_length(graph,feeder.source_bus).values())


def conditioning_evidence(key,equipment,role,line,amps,feeder,settings,cohorts,path_km):
    empirical=length_likelihood(equipment.get('projection_from',key),role,line.length_km,cohorts,settings.length_bandwidth)
    n=equipment['nphases'];r=equipment['rmatrix'];x=equipment['xmatrix']
    ratio=feeder.frequency_hz/equipment['source_frequency_hz']
    # Triangle-inequality impedance bound with endpoint current proxy. It is
    # intentionally a sizing surrogate, not a solved voltage or AC certificate.
    impedance=max(sum(math.hypot(r[i*n+j],x[i*n+j]*ratio) for j in range(n)) for i in range(n))
    drop=amps*line.length_km*impedance/(feeder.voltage_kv*1000/math.sqrt(3))
    budget=settings.voltage_drop_budget_pu*line.length_km/max(path_km,line.length_km)
    physical=math.exp(-min(50.,(drop/budget)**2))
    return {'length_likelihood':empirical['likelihood'],'length_observations':empirical['observations'],
        'length_fallback':empirical['fallback'],'length_in_observed_range':empirical['within_observed_range'],
        'estimated_drop_pu':drop,'allocated_drop_budget_pu':budget,'voltage_weight':physical,
        'joint_weight':empirical['likelihood']*physical,'length_km':line.length_km}
