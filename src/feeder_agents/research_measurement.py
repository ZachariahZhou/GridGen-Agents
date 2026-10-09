"""Finite downstream tests on copies; never re-label a derivative as task utility."""
import copy
import math
from pathlib import Path

import numpy as np

from .artifacts import atomic_json
from .response_measurement import _distribution_solve, _transmission_solve


def _distribution_checks(feeder,spec,result):
    from .rules import load_rules,data
    rules=load_rules(spec);limits=rules['research.voltage']['parameters'];catalogue=data('conductors.json')
    values=[v for phases in result['bus_phase_voltage_pu'].values() for v in phases.values()]
    loading=max(result['line_current_a'][line.id]/catalogue[line.conductor]['normamps'] for line in feeder.lines)
    balance=abs(result['source_kw']-(result['load_kw']-result['generation_kw']+result['loss_kw']))
    checks=dict(converged=bool(result['converged']),finite=all(math.isfinite(v) for v in values+[loading,balance]),
        voltage=min(values)>=limits['min_pu']-1e-7 and max(values)<=limits['max_pu']+1e-7,
        line_ratings=loading<=rules['research.ampacity']['parameters']['max_loading_ratio']+1e-7,
        active_power_balance=balance<=max(.01,feeder.total_kw*1e-5))
    if feeder.phase_mode=='unbalanced':
        vuf=result.get('bus_vuf_percent',{})
        checks['phase_unbalance']=all(isinstance(vuf.get(b.id),(int,float)) and math.isfinite(vuf[b.id])
            and vuf[b.id]<=spec.phase_design.max_vuf_percent+1e-9 for b in feeder.buses if b.phases==[1,2,3])
    # Reuse operational rule checks, including phase unbalance and document rules.
    # Demand and element contracts describe the unperturbed grid and do not apply
    # to an explicitly declared external Q intervention or PV-off contrast.
    from .evaluation import evaluate
    for row in evaluate(feeder,spec,result):
        if row['category']=='operational' and row['status']!='not_applicable':
            checks[row['rule_id']]=row['status']=='pass'
    return checks,dict(min_voltage_pu=min(values),max_voltage_pu=max(values),max_line_loading=loading,
        source_kw=result['source_kw'],loss_kw=result['loss_kw'])


def _distribution_task(model,spec,context,folder):
    feeder=model['feeder'];probe=context['probe']
    base=_distribution_solve(feeder,folder/'reference')
    if context['task']=='voltage_control':
        altered=_distribution_solve(feeder,folder/'q_support',probe,context['support_mvar'])
        expected_q=sum(l.kvar for l in feeder.loads)-1000*context['support_mvar']
        intervention_ok=math.isclose(altered['load_kvar'],expected_q,abs_tol=max(.01,abs(expected_q)*1e-5))
        extra=dict(support_mvar=context['support_mvar'],actual_load_kvar=altered['load_kvar'],expected_load_kvar=expected_q)
        before,after=base,altered
    else:
        from .phases import scale_phase_load
        off=copy.deepcopy(feeder)
        for load in off.loads:scale_phase_load(load,1.,0.)
        altered=_distribution_solve(off,folder/'pv_off')
        injection=sum(l.pv_kw for l in feeder.loads)
        intervention_ok=abs(base['generation_kw']-injection)<=max(.01,injection*1e-5) and abs(altered['generation_kw'])<.01
        extra=dict(pv_on_kw=base['generation_kw'],pv_off_kw=altered['generation_kw'],
            load_kw_difference=base['load_kw']-altered['load_kw'],
            source_kw_difference=base['source_kw']-altered['source_kw'],loss_kw_difference=base['loss_kw']-altered['loss_kw'])
        before,after=altered,base
    changes={f'{bus}.{phase}':after['bus_phase_voltage_pu'][bus][str(phase)]-before['bus_phase_voltage_pu'][bus][str(phase)]
        for bus,phase in probe['monitor_ports']}
    checks,metrics=_distribution_checks(feeder,spec,altered)
    checks.update(intervention_delivered=intervention_ok,nonzero_voltage_effect=bool(changes) and max(map(abs,changes.values()))>1e-10)
    metrics.update(extra,max_voltage_change_pu=max(map(abs,changes.values())),mean_signed_voltage_change_pu=sum(changes.values())/len(changes))
    return dict(accepted=all(checks.values()),checks=checks,metrics=metrics,signed_voltage_change_pu=changes,
        protocol=context,scope='Finite static experiment on a copy; external Q test ports are not an installed controller. PV on/off is not hosting-capacity or time-series analysis.')


def _transmission_task(model,spec,context,folder):
    probe=context['probe'];amount=context['transfer_mw']
    before=_transmission_solve(model,folder/'reference',probe,0.)
    after=_transmission_solve(model,folder/'transfer',probe,amount)
    bus,gen,branch=after['bus'],after['gen'],after['branch'];tol=1e-5
    loading=np.maximum(np.hypot(branch[:,13],branch[:,14]),np.hypot(branch[:,15],branch[:,16]))/branch[:,5]
    losses=float(np.sum(branch[:,13]+branch[:,15]));net=float(np.sum(bus[:,2]-before['bus'][:,2]))
    checks=dict(balanced_transfer=abs(net)<tol,reactive_demand_fixed=bool(np.allclose(bus[:,3],before['bus'][:,3],rtol=0,atol=1e-9)),
        voltage=bool(np.all(bus[:,7]>=spec.voltage_min_pu-tol) and np.all(bus[:,7]<=spec.voltage_max_pu+tol)),
        branch_ratings=bool(np.all(branch[:,5]>0) and np.all(loading<=1+tol)),
        generator_p_limits=bool(np.all(gen[:,1]>=gen[:,9]-tol) and np.all(gen[:,1]<=gen[:,8]+tol)),
        generator_q_limits=bool(np.all(gen[:,2]>=gen[:,4]-tol) and np.all(gen[:,2]<=gen[:,3]+tol)),
        power_balance=bool(abs(gen[:,1].sum()-bus[:,2].sum()-losses)<tol and losses>=-tol))
    changes={str(i):float(branch[i,13]-before['branch'][i,13]) for i in probe['monitor_branches']}
    checks['nonzero_branch_effect']=max(map(abs,changes.values()))>1e-10
    metrics=dict(transfer_mw=amount,net_transfer_injection_mw=net,max_branch_loading=float(loading.max()),
        min_voltage_pu=float(bus[:,7].min()),max_voltage_pu=float(bus[:,7].max()),
        max_voltage_change_pu=float(np.max(np.abs(bus[:,7]-before['bus'][:,7]))),
        max_branch_flow_change_mw=max(map(abs,changes.values())),losses_mw=losses,
        critical_branch_index=int(np.argmax(loading)),remaining_rating_fraction=float(1-loading.max()))
    return dict(accepted=all(checks.values()),checks=checks,metrics=metrics,signed_branch_change_mw=changes,
        protocol=context,scope='Balanced P-only transfer at fixed Q; slack covers incremental losses. No OPF, maximum transfer capacity or N-1 certification.')


def evaluate_research_task(model,spec,context,folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    try:
        result=(_transmission_task if context['task']=='transmission_transfer' else _distribution_task)(model,spec,context,folder)
    except (ValueError,KeyError,RuntimeError,ZeroDivisionError) as exc:
        result=dict(accepted=False,checks={'complete_finite_experiment':False},metrics={},error=f'{type(exc).__name__}: {exc}',protocol=context)
    atomic_json(folder/'task_validation.json',result)
    return result
