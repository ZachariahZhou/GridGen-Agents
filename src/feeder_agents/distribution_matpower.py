"""Balanced single-voltage research-case export with explicit source reduction.

The original feeder bus count is preserved. Its source-terminal voltage from
OpenDSS becomes a fixed MATPOWER slack; the upstream Thevenin impedance is not
retained. Agreement is checked at this boundary condition, not for arbitrary
changes to the original upstream network. Unbalanced models are never folded.
"""
import cmath
import copy
import math
from pathlib import Path

import numpy as np

from .artifacts import atomic_json, digest
from .matpower import parse_matpower
from .rules import data


def _sequence(line, frequency):
    record=data('conductors.json')[line.conductor]
    if 'source_feeder' in record:
        if record['nphases']!=3 or record['construction']!=line.construction:
            raise ValueError('MATPOWER requires compatible symmetric three-phase equipment')
        sequence=[]
        for name in ('r','x','c'):
            matrix=np.asarray(record[name+'matrix'],dtype=float).reshape(3,3)
            diagonal=np.diag(matrix);off=matrix[~np.eye(3,dtype=bool)]
            if not np.allclose(diagonal,diagonal[0],rtol=0,atol=1e-7) or not np.allclose(off,off[0],rtol=0,atol=1e-7):
                raise ValueError('Asymmetric equipment has no lossless symmetric positive-sequence export: '+line.id)
            value=float(np.mean(diagonal)-np.mean(off))
            if name=='x':value*=frequency/record['source_frequency_hz']
            sequence.append(value)
    else:
        profile=data('construction_profiles.json')[line.construction]
        sequence=[record['r1']*profile['r1_factor'],record['x1']*profile['x1_factor'],profile['c1_nf_per_km']]
    if any(not math.isfinite(v) or v<0 for v in sequence) or sum(sequence[:2])<=0:
        raise ValueError('Invalid positive-sequence line parameters')
    return *sequence,record['normamps']


def build_case(feeder,spec,simulation):
    """Build standard version-2 matrices and a reversible object-ID mapping."""
    import networkx as nx
    if feeder.phase_mode!='balanced' or spec.phase_design.mode!='balanced':
        raise ValueError('MATPOWER adapter supports balanced distribution only; no automatic three-phase reduction')
    if any(set(obj.phases)!={1,2,3} for obj in [*feeder.buses,*feeder.lines,*feeder.tie_lines,*feeder.loads]):
        raise ValueError('MATPOWER adapter requires three-phase symmetric objects')
    ids=[b.id for b in feeder.buses];mapping={name:i+1 for i,name in enumerate(ids)}
    graph=nx.Graph();graph.add_nodes_from(ids);graph.add_edges_from((e.bus1,e.bus2) for e in feeder.lines)
    if len(mapping)!=len(ids) or set(graph)!=set(ids) or not nx.is_tree(graph) or len(feeder.lines)!=len(ids)-1:
        raise ValueError('Invalid radial feeder for MATPOWER export')
    if feeder.source_bus not in mapping or any(l.bus not in mapping for l in feeder.loads):
        raise ValueError('Unknown source/load bus')
    if any(e.bus1 not in mapping or e.bus2 not in mapping for e in feeder.tie_lines):
        raise ValueError('Unknown tie endpoint')
    electrical=[_sequence(e,feeder.frequency_hz) for e in feeder.lines+feeder.tie_lines]
    if not simulation.get('converged'):
        raise ValueError('A converged DSS source-terminal solution is required')
    source=feeder.source_bus.lower()
    phasors=simulation['bus_phase_voltage_complex'][source]
    values=[complex(*phasors[str(i)]) for i in (1,2,3)]
    rotation=complex(-.5,math.sqrt(3)/2)
    positive=(values[0]+rotation*values[1]+rotation**2*values[2])/3
    negative=(values[0]+rotation**2*values[1]+rotation*values[2])/3
    zero=sum(values)/3
    if abs(positive)==0 or max(abs(negative),abs(zero))/abs(positive)>1e-6:
        raise ValueError('DSS source-terminal solution is not balanced')
    vm=abs(positive)/(feeder.voltage_kv*1000/math.sqrt(3));va=math.degrees(cmath.phase(positive))
    if not math.isfinite(vm+va) or vm<=0:raise ValueError('Invalid source-terminal voltage')
    base=1.0;zbase=feeder.voltage_kv**2/base
    from .voltage import voltage_profile
    vmin,vmax=voltage_profile(feeder.voltage_kv)['research_voltage']
    bus=np.zeros((len(ids),13))
    for i,name in enumerate(ids):
        bus[i]=[i+1,3 if name==feeder.source_bus else 1,0,0,0,0,1,vm if name==feeder.source_bus else 1,va if name==feeder.source_bus else 0,feeder.voltage_kv,1,vmax,vmin]
    # The slack represents an external grid, not a thermal unit or calibrated
    # generator capability curve. Bounds are explicit PF placeholders, not OPF data.
    bound=max(10.,2*sum(math.hypot(l.kw,l.kvar)+l.pv_kw for l in feeder.loads)/1000)
    slack=np.zeros(21);slack[:10]=[mapping[feeder.source_bus],0,0,bound,-bound,vm,base,1,bound,-bound]
    generators=[slack];generator_map=[dict(row=0,kind='upstream_grid_equivalent',bus=feeder.source_bus)]
    for load in feeder.loads:
        bus[mapping[load.bus]-1,2]+=load.kw/1000
        bus[mapping[load.bus]-1,3]+=load.kvar/1000
        if load.pv_kw>0:
            row=np.zeros(21);row[:10]=[mapping[load.bus],load.pv_kw/1000,0,0,0,1,base,1,load.pv_kw/1000,load.pv_kw/1000]
            generator_map.append(dict(row=len(generators),kind='solar_pv_fixed_pq',load_id=load.id,bus=load.bus))
            generators.append(row)
    branches=[];branch_map=[]
    for i,(line,(r,x,c,amps)) in enumerate(zip(feeder.lines+feeder.tie_lines,electrical)):
        active=i<len(feeder.lines)
        rating=math.sqrt(3)*feeder.voltage_kv*amps/1000 if amps>0 else 0
        branches.append([mapping[line.bus1],mapping[line.bus2],r*line.length_km/zbase,x*line.length_km/zbase,
            2*math.pi*feeder.frequency_hz*c*1e-9*line.length_km*zbase,rating,0,0,0,0,int(active),-360,360])
        branch_map.append(dict(row=i,line_id=line.id,conductor=line.conductor,length_km=line.length_km,
            r1_ohm_km=r,x1_ohm_km=x,c1_nf_km=c,normamps=amps if amps>0 else None,status=int(active),
            rating_basis='nominal-voltage MVA derived from catalog ampacity; not a voltage-invariant current limit'))
    case=dict(version='2',baseMVA=base,bus=bus,gen=np.asarray(generators),branch=np.asarray(branches))
    metadata=dict(version='m69',feeder_hash=digest(feeder.model_dump()),representation='balanced_positive_sequence',
        source_boundary=dict(kind='fixed_dss_terminal_voltage',bus=feeder.source_bus,voltage_pu=vm,angle_degrees=va,
            preserves_thevenin_impedance=False,scope='Same feeder bus count. Base-condition equivalence only; source remains fixed if downstream demand is subsequently changed.'),
        buses=mapping,branches=branch_map,generators=generator_map,
        limits='Steady-state PF case; no cost curves, dispatch model, fault equivalent or OPF feasibility certification. Zero RATE_B/C means unspecified.',
        references=['https://matpower.org/documentation/ref-manual/legacy/functions/caseformat.html','https://dss-extensions.org/dss-format/Vsource.html'])
    return case,metadata


def _solve_and_compare(case,feeder,simulation):
    from pypower.api import runpf,ppoption
    solved,converged=runpf(copy.deepcopy(case),ppoption(VERBOSE=0,OUT_ALL=0,PF_ALG=1,PF_TOL=1e-10,PF_MAX_IT=50,ENFORCE_Q_LIMS=0))
    finite=all(np.isfinite(solved[k]).all() for k in ('bus','gen','branch'))
    if not converged or not finite:return dict(verified=False,converged=bool(converged),finite=bool(finite),reason='AC power flow failed')
    dss_voltage=np.array([sum(simulation['bus_voltage_pu'][b.id.lower()])/3 for b in feeder.buses])
    error=float(np.max(np.abs(solved['bus'][:,7]-dss_voltage)))
    loss=float(np.sum(solved['branch'][:,13]+solved['branch'][:,15])*1000)
    loss_error=abs(loss-simulation['loss_kw'])
    balance=abs(float(np.sum(solved['gen'][:,1])-np.sum(solved['bus'][:,2]))*1000-loss)
    return dict(verified=bool(error<=1e-5 and loss_error<=.01 and balance<=1e-5),converged=True,finite=True,
        solver='PYPOWER AC Newton-Raphson (MATPOWER-compatible); MATLAB/Octave not executed',
        max_bus_voltage_error_pu=error,loss_kw=loss,dss_loss_kw=simulation['loss_kw'],loss_error_kw=loss_error,
        power_balance_error_kw=balance,min_voltage_pu=float(solved['bus'][:,7].min()),
        tolerances=dict(max_bus_voltage_error_pu=1e-5,loss_error_kw=.01,power_balance_error_kw=1e-5),
        scope='Conversion verification at the documented fixed source-terminal voltage; not new operational acceptance.')


def verify_distribution_matpower(folder,feeder,spec,simulation):
    """Reload the deliverable, bind every table to the feeder, then solve it."""
    import json
    folder=Path(folder)
    expected,metadata=build_case(feeder,spec,simulation)
    parsed=parse_matpower(folder/'case_generated.m')
    case=dict(version='2',baseMVA=parsed['base_mva'],**{k:np.asarray(parsed[k]) for k in ('bus','gen','branch')})
    parity=case['baseMVA']==expected['baseMVA'] and all(case[k].shape==expected[k].shape and np.allclose(case[k],expected[k],rtol=1e-11,atol=1e-12) for k in ('bus','gen','branch'))
    stored=json.loads((folder/'metadata.json').read_text())
    if not parity or digest(stored)!=digest(metadata):return dict(verified=False,reason='Export tables or provenance differ from the delivered feeder')
    return _solve_and_compare(case,feeder,simulation)


def export_distribution_matpower(feeder,spec,simulation,folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    case,metadata=build_case(feeder,spec,simulation)
    lines=['function mpc = case_generated','% Balanced distribution research case. See metadata.json for source-boundary assumptions.',"mpc.version = '2';",'mpc.baseMVA = 1;']
    for name in ('bus','gen','branch'):
        lines.append('mpc.'+name+' = [')
        lines.extend(' '.join(format(float(v),'.15g') for v in row)+';' for row in case[name])
        lines.append('];')
    (folder/'case_generated.m').write_text('\n'.join(lines)+'\n')
    atomic_json(folder/'metadata.json',metadata)
    result=verify_distribution_matpower(folder,feeder,spec,simulation)
    atomic_json(folder/'validation.json',result)
    return result
