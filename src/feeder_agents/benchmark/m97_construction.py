"""Frozen M97 ordinary construction and independent saved-export audit.

This is a deterministic structured-input benchmark, never an LLM ablation.
The independent audit reads delivery artifacts and shares numerical backends
and catalogue semantics with production; it is not an independent solver.
"""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
import copy
import csv
import hashlib
import importlib.metadata
import json
import math
import multiprocessing
import os
from pathlib import Path
import shutil
import time
import traceback

import networkx as nx
import numpy as np

from feeder_agents.artifacts import atomic_json, digest, file_digest
from feeder_agents.solver_accounting import SolverMeter, solver_phase, record_ac_solve


def frozen_tasks():
    """Designed coverage, three independently drawn seeds per profile."""
    from feeder_agents.schemas import ExperimentSpec
    from feeder_agents.hierarchy import HierarchicalSpec
    from feeder_agents.transmission import TransmissionSpec
    tasks = []
    profiles = [
        ('urban', 23, 6, 'balanced', 'long_trunk'),
        ('rural', 37, 10, 'unbalanced', 'comb'),
        ('urban', 59, 20, 'unbalanced', 'multi_branch'),
        ('rural', 79, 6, 'balanced', 'balanced_tree'),
        ('urban', 37, 10, 'balanced', 'irregular_tree'),
        ('rural', 59, 20, 'unbalanced', 'long_trunk'),
        ('urban', 79, 6, 'unbalanced', 'comb'),
        ('rural', 23, 10, 'balanced', 'multi_branch'),
        ('urban', 59, 20, 'balanced', 'balanced_tree'),
        ('rural', 79, 6, 'unbalanced', 'irregular_tree'),
        ('urban', 23, 10, 'balanced', 'open_ring'),
        ('urban', 37, 20, 'unbalanced', 'open_ring'),
        ('urban', 59, 6, 'balanced', 'spatial_mst'),
        ('rural', 37, 10, 'unbalanced', 'spatial_mst'),
        ('rural', 79, 20, 'balanced', 'rural_villages'),
    ]
    for i, (scene, n, kv, phase, topology) in enumerate(profiles):
        scenario = dict(kind=scene, engineering_profile=scene, layout=topology)
        if topology not in ('spatial_mst', 'rural_villages'):
            scenario.update(layout='structured_radial', topology={'family': topology},
                            tie_count=int(topology == 'open_ring'))
        for replicate in range(3):
            seed = 970000 + 101*i + 17*replicate
            total = float(n*(8 if scene == 'urban' else 5))
            spec = ExperimentSpec(n_buses=n, voltage_kv=kv, total_kw_min=total,
                total_kw_max=total, pv_ratio=(0., .3, .6)[i % 3], seed=seed,
                segment_km_min=.025 if scene == 'urban' else .05,
                segment_km_max=.1 if scene == 'urban' else .2,
                scenario=scenario, phase_design={'mode': phase},
                repair_policy={'strategy': 'fixed', 'allow_rewire': False}, max_repairs=2)
            tasks.append(dict(id=f'd{i:02d}_r{replicate}', profile=f'd{i:02d}',
                              domain='distribution', replicate=replicate, spec=spec.model_dump()))
    hierarchy_profiles = [
        dict(scene='urban', voltage_kv=6, lv_voltage_kv=.4, mv_buses=7,
             transformer_count=3, users=21, total_kw=63, mv_topology={'family': 'open_ring'},
             lv_topology='branch_star', customer_connection='service_star',
             lv_installation='buried_duct', lv_equipment_profile='nexans_underground'),
        dict(scene='rural', voltage_kv=10, lv_voltage_kv=.38, mv_buses=9,
             transformer_count=4, users=30, total_kw=75, mv_topology={'family': 'comb'},
             lv_topology='radial_chain', customer_connection='distributed_taps',
             lv_installation='aerial_bundle', lv_equipment_profile='nexans_abc'),
        dict(scene='urban', voltage_kv=20, lv_voltage_kv=.4, mv_buses=13,
             transformer_count=5, users=39, total_kw=117, mv_topology={'family': 'branched_network'},
             lv_topology='mixed_radial', customer_connection='mixed_taps',
             lv_installation='buried_direct', lv_equipment_profile='nexans_underground'),
    ]
    for i, profile in enumerate(hierarchy_profiles):
        for replicate in range(3):
            spec = HierarchicalSpec(**profile, pv_ratio=(.2, .4, .6)[i],
                lv_branches=2, phase_weights=[.5, .3, .2], seed=975000+101*i+17*replicate)
            tasks.append(dict(id=f'h{i:02d}_r{replicate}', profile=f'h{i:02d}',
                              domain='hierarchy', replicate=replicate, spec=spec.model_dump()))
    transmission_profiles = [
        (57, 110, 'regional', 'dispersed', 0),
        (57, 220, 'ring_chords', 'concentrated', 0),
        (139, 220, 'corridor', 'dispersed', 7),
        (139, 500, 'regional', 'concentrated', 0),
        (237, 220, 'regional', 'dispersed', 12),
        (237, 500, 'corridor', 'concentrated', 0),
    ]
    for i, (n, kv, family, loads, peripheral) in enumerate(transmission_profiles):
        ng = max(6, n//8)
        for replicate in range(3):
            spec = TransmissionSpec(n_buses=n, n_generators=ng, voltage_kv=kv,
                total_mw=float(n*8), radius_km=(20 if kv == 110 else 40 if kv == 220 else 65),
                mesh_family=family, mesh_style=('local', 'mixed', 'long_distance')[i % 3],
                load_pattern=loads, radial_bus_count=peripheral,
                generator_types=[('thermal', 'hydro', 'wind', 'solar')[j % 4] for j in range(ng)],
                allow_topology_changes=False, seed=978000+101*i+17*replicate,
                allowed_repairs=['shunt_step', 'voltage_setpoint', 'parallel_line',
                                 'upgrade_transformer', 'transformer_tap', 'redispatch'])
            tasks.append(dict(id=f't{i:02d}_r{replicate}', profile=f't{i:02d}',
                              domain='transmission', replicate=replicate, spec=spec.model_dump()))
    return tasks


def graph_descriptors(nodes, edges, ties=(), source=None):
    active = nx.Graph(); active.add_nodes_from(nodes); active.add_edges_from(edges)
    physical = active.copy(); physical.add_edges_from(ties)
    connected = bool(nodes) and nx.is_connected(active)
    degrees = list(dict(active.degree()).values())
    return dict(nodes=len(active), energized_edges=active.number_of_edges(),
        physical_edges=physical.number_of_edges(), open_ties=len(ties),
        energized_components=nx.number_connected_components(active),
        energized_cycle_rank=active.number_of_edges()-len(active)+nx.number_connected_components(active),
        physical_cycle_rank=physical.number_of_edges()-len(physical)+nx.number_connected_components(physical),
        degree_mean=float(np.mean(degrees)) if degrees else None,
        degree_max=max(degrees, default=0), terminal_fraction=sum(x == 1 for x in degrees)/max(1,len(degrees)),
        source_max_depth_hops=max(nx.single_source_shortest_path_length(active,source).values()) if source in active else None,
        diameter_hops=nx.diameter(active) if connected else None,
        mean_shortest_path_hops=nx.average_shortest_path_length(active) if connected else None)


def audit_model(model, spec, domain):
    """Independently recompute hard scalar/graph requirements from saved JSON."""
    buses = model['buses']; loads = model['loads']; lines = model['lines']
    ties = model.get('tie_lines', []); transformers = model.get('transformers', [])
    ids = [b['id'] for b in buses]; positions = {b['id']: (b['x_km'], b['y_km']) for b in buses}
    edges = [(e['bus1'], e['bus2']) for e in lines+transformers]
    graph = graph_descriptors(ids, edges, [(e['bus1'],e['bus2']) for e in ties], model['source_bus'])
    total = sum(l['kw'] for l in loads); pv = sum(l['pv_kw'] for l in loads)
    expected_total = spec['total_kw_min'] if domain == 'distribution' else spec['total_kw']
    expected_loads = spec['n_loads_min'] if domain == 'distribution' else spec['users']
    checks = dict(bus_count=len(buses) == spec['n_buses'] and len(set(ids)) == len(ids),
        load_count=len(loads) == expected_loads,
        load_total=math.isclose(total, expected_total, rel_tol=0, abs_tol=1e-7),
        pv_total=math.isclose(pv, expected_total*spec['pv_ratio'], rel_tol=0, abs_tol=1e-7),
        power_factor=all(math.isclose(l['kvar'], l['kw']*math.tan(math.acos(spec['power_factor'])), abs_tol=1e-7) for l in loads),
        energized_tree=graph['energized_components'] == 1 and graph['energized_cycle_rank'] == 0 and len(edges) == len(buses)-1,
        geometry=all(e['bus1'] in positions and e['bus2'] in positions and math.isclose(e['length_km'], math.dist(positions[e['bus1']],positions[e['bus2']]), rel_tol=1e-8, abs_tol=1e-9) for e in lines+ties),
        load_locations=len({l['bus'] for l in loads}) == len(loads) and all(l['bus'] in ids and l['bus'] != model['source_bus'] for l in loads),
        phase_continuity=all(set(e['phases']) <= set(next(b['phases'] for b in buses if b['id']==e['bus1'])) and set(e['phases']) <= set(next(b['phases'] for b in buses if b['id']==e['bus2'])) for e in lines+ties))
    if domain == 'distribution':
        checks.update(voltage=model['voltage_kv'] == spec['voltage_kv'],
            phase_mode=model['phase_mode'] == spec['phase_design']['mode'],
            tie_count=len(ties) == spec['scenario']['tie_count'],
            line_lengths=all(spec['segment_km_min']-1e-9 <= e['length_km'] <= spec['segment_km_max']+1e-9 for e in lines))
        scene = spec['scenario']['engineering_profile']
        expected_construction = {'urban': 'cable', 'rural': 'overhead', 'generic': 'illustrative'}[scene]
        checks['construction'] = all(e['construction'] == expected_construction for e in lines+ties)
    else:
        lookup = {b['id']: b for b in buses}
        checks.update(transformer_count=len(transformers) == spec['transformer_count'],
            mv_count=sum(b['voltage_kv'] > 1 for b in buses) == spec['mv_buses'],
            voltage_levels={b['voltage_kv'] for b in buses} == {spec['voltage_kv'], spec['lv_voltage_kv']},
            transformer_boundaries=all(lookup[t['bus1']]['voltage_kv'] == t['primary_kv'] == spec['voltage_kv'] and lookup[t['bus2']]['voltage_kv'] == t['secondary_kv'] == spec['lv_voltage_kv'] for t in transformers),
            single_phase_customers=all(len(l['phases']) == 1 for l in loads))
    lengths = [e['length_km'] for e in lines+ties]
    descriptors = dict(graph, total_kw=total, pv_kw=pv, load_count=len(loads),
        transformer_count=len(transformers), total_line_km=sum(lengths),
        min_line_km=min(lengths,default=0), max_line_km=max(lengths,default=0),
        equipment_types=len({e['conductor'] for e in lines+ties}),
        single_phase_bus_count=sum(len(b['phases']) == 1 for b in buses),
        two_phase_bus_count=sum(len(b['phases']) == 2 for b in buses))
    return dict(checks=checks, descriptors=descriptors)


def audit_dss_export(master, model, spec, domain):
    """Fresh DSS context plus explicit object/location checks on exported files."""
    from opendssdirect import dss
    from feeder_agents.simulation import simulate
    from feeder_agents.schemas import ExperimentSpec, Feeder
    from feeder_agents.evaluation import evaluate, verdict
    from feeder_agents.hierarchy import HierarchicalSpec, HierarchicalFeeder, evaluate_hierarchy
    static = audit_model(model, spec, domain)
    simulation = simulate(master)
    if domain == 'distribution':
        rules = evaluate(Feeder(**model), ExperimentSpec(**spec), simulation)
        physical = verdict(rules, 'normal')
        rule_checks = {r['rule_id']: r['status'] != 'fail' for r in rules}
    else:
        physical = evaluate_hierarchy(HierarchicalFeeder(**model), HierarchicalSpec(**spec), simulation)
        rule_checks = physical['checks']
    engine = dss.NewContext(); engine.Basic.AllowChangeDir(False)
    record_ac_solve('opendss')
    engine.Text.Command(f'Compile "{Path(master).resolve()}"')
    actual_loads = {}; actual_pv = {}; actual_lines = {}
    for load in engine.Loads:
        actual_loads[load.Name()] = dict(bus=engine.CktElement.BusNames()[0].split('.')[0],
            phases=engine.CktElement.NumPhases(), kw=load.kW(), kvar=load.kvar())
    for gen in engine.Generators:
        actual_pv[gen.Name()] = dict(bus=engine.CktElement.BusNames()[0].split('.')[0], kw=gen.kW())
    for line in engine.Lines:
        actual_lines[line.Name()] = dict(buses=[b.split('.')[0] for b in engine.CktElement.BusNames()],
            phases=engine.CktElement.NumPhases(), length=line.Length(), rating=engine.CktElement.NormalAmps(),
            opened=engine.CktElement.IsOpen(1,0) and engine.CktElement.IsOpen(2,0))
    expected_loads = {}; expected_pv = {}
    for load in model['loads']:
        split = domain == 'distribution' and model['phase_mode'] == 'unbalanced'
        parts = [(f"{load['id']}_p{p['phase']}", 1, p['kw'], p['kvar'], p['pv_kw']) for p in load['phase_powers']] if split else [(load['id'],len(load['phases']),load['kw'],load['kvar'],load['pv_kw'])]
        for name, phases, kw, kvar, pv in parts:
            expected_loads[name.lower()] = dict(bus=load['bus'].lower(),phases=phases,kw=kw,kvar=kvar)
            if pv > 0: expected_pv['pv_'+name.lower()] = dict(bus=load['bus'].lower(),kw=pv)
    checks = dict(static['checks'])
    checks['export_bus_ids'] = set(engine.Circuit.AllBusNames()) == {b['id'].lower() for b in model['buses']}
    checks['export_load_names'] = set(actual_loads) == set(expected_loads)
    checks['export_load_locations'] = checks['export_load_names'] and all(actual_loads[n]['bus']==v['bus'] and actual_loads[n]['phases']==v['phases'] for n,v in expected_loads.items())
    checks['export_load_power'] = checks['export_load_names'] and all(math.isclose(actual_loads[n][k], v[k], rel_tol=1e-9, abs_tol=1e-7) for n,v in expected_loads.items() for k in ('kw','kvar'))
    checks['export_pv_locations_power'] = set(actual_pv)==set(expected_pv) and all(actual_pv[n]['bus']==v['bus'] and math.isclose(actual_pv[n]['kw'],v['kw'],rel_tol=1e-9,abs_tol=1e-7) for n,v in expected_pv.items())
    expected_lines = model['lines'] + model.get('tie_lines', [])
    tie_ids = {e['id'].lower() for e in model.get('tie_lines',[])}
    checks['export_lines'] = set(actual_lines)=={e['id'].lower() for e in expected_lines} and all(actual_lines[e['id'].lower()]['buses']==[e['bus1'].lower(),e['bus2'].lower()] and actual_lines[e['id'].lower()]['phases']==len(e['phases']) and math.isclose(actual_lines[e['id'].lower()]['length'],e['length_km'],rel_tol=1e-9) and actual_lines[e['id'].lower()]['opened']==(e['id'].lower() in tie_ids) for e in expected_lines)
    expected_transformers = {t['id'].lower() for t in model.get('transformers',[])}
    checks['export_transformer_count'] = set(engine.Transformers.AllNames())-{'none'} == expected_transformers
    for bus in model['buses']:
        engine.Circuit.SetActiveBus(bus['id'])
        checks.setdefault('export_bus_phases', True)
        checks['export_bus_phases'] &= set(engine.Bus.Nodes())- {0} == set(bus['phases'])
    checks['fresh_rules'] = bool(physical['accepted'])
    currents = simulation.get('line_current_a',{})
    loading = max((currents.get(name,0)/v['rating'] for name,v in actual_lines.items() if v['rating']>0),default=0.)
    descriptors = dict(static['descriptors'], min_voltage_pu=simulation.get('min_voltage_pu'),
        max_voltage_pu=simulation.get('max_voltage_pu'), max_loading_ratio=loading,
        max_vuf_percent=simulation.get('max_vuf_percent'), losses_kw=simulation.get('loss_kw'))
    return dict(export_pass=all(checks.values()), physical_pass=bool(physical['accepted']),
        checks=checks, rule_checks=rule_checks, descriptors=descriptors, simulation=simulation,
        evidence='Saved OpenDSS reload, direct engine object inspection, fresh measurements and shared rule semantics.')


def audit_transmission_export(path, json_case, metadata, spec):
    from feeder_agents.matpower import parse_matpower
    from feeder_agents.transmission import TransmissionSpec, validate_case
    parsed = parse_matpower(Path(path))
    case = dict(version='2',baseMVA=parsed['base_mva'],
                **{k:np.asarray(parsed[k],dtype=float) for k in ('bus','gen','branch')})
    checked, solved = validate_case(case, TransmissionSpec(**spec), metadata)
    bus, gen, branch = case['bus'], case['gen'], case['branch']
    graph = graph_descriptors(bus[:,0].astype(int).tolist(),
        [(int(r[0]),int(r[1])) for r in branch if r[10]>0], source=int(bus[bus[:,1]==3][0,0]))
    kv_counts = Counter(bus[:,9]); expected_kv = Counter({spec['voltage_kv']:spec['n_buses']})
    if spec.get('voltage_layers'): expected_kv=Counter({v['kv']:v['buses'] for v in spec['voltage_layers']})
    checks = dict(export_matches_json=all(np.asarray(json_case[k]).shape==case[k].shape and np.allclose(json_case[k],case[k],rtol=1e-12,atol=1e-12) for k in ('bus','gen','branch')) and math.isclose(float(json_case['baseMVA']),float(case['baseMVA'])),
        bus_count=len(bus)==spec['n_buses'] and len(set(bus[:,0]))==len(bus),
        generator_count=len(gen)==spec['n_generators'], voltage_levels=kv_counts==expected_kv,
        demand=math.isclose(float(bus[:,2].sum()),spec['total_mw'],abs_tol=1e-7),
        power_factor=np.allclose(bus[:,3],bus[:,2]*math.tan(math.acos(spec['power_factor'])),rtol=1e-9,atol=1e-7),
        generator_types=metadata['generator_types']==spec['generator_types'],
        connected=graph['energized_components']==1,
        exact_branch_count=len(branch)==spec['n_buses']+spec['extra_edges'],
        fresh_rules=checked['accepted'])
    checks = {k:bool(v) for k,v in checks.items()}
    lengths = [e['length_km'] for e in metadata['branch_evidence'] if e['kind']=='line']
    descriptors = dict(graph, total_mw=float(bus[:,2].sum()), generator_count=len(gen),
        total_line_km=sum(lengths), min_line_km=min(lengths), max_line_km=max(lengths),
        equipment_types=len({e.get('profile_id') for e in metadata['branch_evidence']}),
        **checked['metrics'])
    return dict(export_pass=all(checks.values()), physical_pass=checked['accepted'],
        checks=checks, rule_checks=checked['checks'], descriptors=descriptors,
        evidence='Independent MATPOWER text parser, JSON/matrix comparison, exact spec checks and fresh PYPOWER AC validation.')


def summarize_rows(rows, expected_count):
    count = lambda field: sum(bool(r.get(field)) for r in rows)
    return dict(expected=expected_count,completed=len(rows),missing=expected_count-len(rows),
        constructed=count('constructed'),constructor_failures=len(rows)-count('constructed'),
        initial_accepted=count('initial_accepted'), final_accepted=count('final_accepted'),
        initial_export_pass=count('initial_export_pass'),final_export_pass=count('final_export_pass'),
        initial_delivered=sum(bool(r.get('initial_accepted') and r.get('initial_export_pass')) for r in rows),
        final_delivered=sum(bool(r.get('final_accepted') and r.get('final_export_pass') and r.get('requirements_preserved',True)) for r in rows),
        repair_recoveries=sum(not r.get('initial_accepted') and bool(r.get('final_accepted')) for r in rows))


def metered_distribution_worker(payload):
    """Capture actual SolverMeter events inside the production child process."""
    from feeder_agents.workflow import _run_sample
    started = time.monotonic()
    with SolverMeter() as meter, solver_phase('construction_and_repair'):
        try:
            return _run_sample(payload)
        finally:
            atomic_json(Path(payload[2])/'solver_meter.json',dict(meter.snapshot(),events=meter.events,
                seconds=time.monotonic()-started,pid=os.getpid()))


def _load(path):
    return json.loads(Path(path).read_text())


def _protected_distribution(model):
    result = copy.deepcopy(model)
    result.pop('design_evidence',None); result.pop('assumptions',None)
    for edge in result['lines'] + result.get('tie_lines',[]): edge.pop('conductor',None)
    return result


def run_task(task, root):
    root=Path(root); folder=root/'cases'/task['id']; folder.mkdir(parents=True,exist_ok=True)
    result_path=folder/'record.json'
    if result_path.exists(): return _load(result_path)
    atomic_json(folder/'task.json',task)
    started=time.monotonic(); domain=task['domain']; spec=task['spec']
    row=dict(id=task['id'],profile=task['profile'],domain=domain,seed=spec['seed'],
        sample_seed=int.from_bytes(hashlib.sha256(f"{spec['seed']}:0".encode()).digest()[:4],'big'),
        constructed=False,initial_accepted=False,final_accepted=False,
        initial_export_pass=False,final_export_pass=False,requirements_preserved=False,
        repair_attempts=0,repair_commits=0)
    meter=SolverMeter()
    with meter:
        try:
            from feeder_agents.paper.adapters import FixedRepair
            if domain=='distribution':
                import feeder_agents.workflow as workflow
                from feeder_agents.schemas import ExperimentSpec
                original=workflow._run_sample
                workflow._run_sample=metered_distribution_worker
                try:
                    with solver_phase('construction_and_repair'):
                        outcome=workflow.run_experiment(ExperimentSpec(**spec),folder,'construction',workers=1,
                            execution_limits={'sample_timeout_seconds':180})
                finally: workflow._run_sample=original
                sample=Path(outcome['directory'])/'sample_00000'; sample_row=outcome['samples'][0]
                row['construction_directory']=str(sample)
                if not (sample/'feeder.json').exists(): raise RuntimeError(sample_row.get('error','No exported model'))
                initial=_load(sample/'baseline'/'feeder.json'); final=_load(sample/'feeder.json')
                row.update(constructed=True,initial_accepted=_load(sample/'baseline'/'validation.json')['accepted'],
                    final_accepted=sample_row['accepted'],repair_attempts=sample_row.get('repairs',0),
                    repair_commits=sample_row.get('repair_commits',0),
                    requirements_preserved=_protected_distribution(initial)==_protected_distribution(final))
                with solver_phase('independent_export_audit'):
                    for arm,model,path in [('initial',initial,sample/'baseline'/'opendss'/'Master.dss'),('final',final,sample/'opendss'/'Master.dss')]:
                        audit=audit_dss_export(path,model,spec,domain)
                        atomic_json(folder/(arm+'_audit.json'),audit)
                        row[arm+'_export_pass']=audit['export_pass']; row[arm+'_descriptors']=audit['descriptors']
                        row[arm+'_failed_checks']=[k for k,v in audit['checks'].items() if not v]
                        row[arm+'_failed_rules']=[k for k,v in audit['rule_checks'].items() if not v]
                row['child_meter']=_load(sample/'solver_meter.json')
            elif domain=='hierarchy':
                import feeder_agents.hierarchy_workflow as workflow
                import feeder_agents.hierarchy_feedback as feedback
                from feeder_agents.simulation import simulate
                from feeder_agents.hierarchy import HierarchicalSpec, HierarchicalFeeder
                # The task already runs in an isolated worker. Avoid nested DSS
                # workers so the same real meter includes every repair solve.
                old_workflow,old_feedback=workflow.solve_isolated,feedback.solve_isolated
                workflow.solve_isolated=simulate; feedback.solve_isolated=simulate
                try:
                    with solver_phase('construction_and_repair'):
                        outcome=workflow.run_hierarchy(spec,folder,'construction',agent_feedback=True,
                            feedback_model=FixedRepair(),feedback_rounds=4,use_memory=False,
                            local_topology=False,protect_customer_allocation=True)
                finally: workflow.solve_isolated=old_workflow; feedback.solve_isolated=old_feedback
                sample=Path(outcome['directory'])/'sample_00000'; sample_row=outcome['samples'][0]
                row['construction_directory']=str(sample)
                if sample_row.get('error'): raise RuntimeError(sample_row['error'])
                baseline=sample/'feedback'/'candidate_0000'
                initial=_load(baseline/'feeder.json'); final=_load(sample/'feeder.json')
                from feeder_agents.hierarchy import export_hierarchy
                # Export the saved initial JSON once for a separately reloadable arm.
                initial_master=export_hierarchy(HierarchicalFeeder(**initial),folder/'initial_export')
                report=_load(sample/'feedback'/'result.json')
                row.update(constructed=True,initial_accepted=_load(baseline/'assessment.json')['checks']['accepted'],
                    final_accepted=sample_row['accepted'],repair_attempts=report['rounds'],
                    repair_commits=report['accepted_steps'],requirements_preserved=feedback.feedback_contract(
                        HierarchicalFeeder(**initial),HierarchicalFeeder(**final),HierarchicalSpec(**spec),False,
                        protect_customer_allocation=True))
                with solver_phase('independent_export_audit'):
                    for arm,model,path in [('initial',initial,initial_master),('final',final,sample/'opendss'/'Master.dss')]:
                        audit=audit_dss_export(path,model,spec,domain)
                        atomic_json(folder/(arm+'_audit.json'),audit)
                        row[arm+'_export_pass']=audit['export_pass'];row[arm+'_descriptors']=audit['descriptors']
                        row[arm+'_failed_checks']=[k for k,v in audit['checks'].items() if not v]
                        row[arm+'_failed_rules']=[k for k,v in audit['rule_checks'].items() if not v]
            else:
                from feeder_agents.transmission import run_transmission, export_case, TransmissionSpec
                from feeder_agents.transmission_feedback import preserved
                with solver_phase('construction_and_repair'):
                    outcome=run_transmission(spec,folder,'construction',agent_feedback=True,
                        feedback_model=FixedRepair(),feedback_rounds=4,use_memory=False)
                sample=Path(outcome['directory'])/'sample_00000';sample_row=outcome['samples'][0]
                row['construction_directory']=str(sample)
                if sample_row.get('error'): raise RuntimeError(sample_row['error'])
                baseline=sample/'feedback'/'candidate_0000'
                def arrays(payload):
                    return {k:np.asarray(v,dtype=float) if k in ('bus','gen','branch') else v for k,v in payload.items()}
                initial=arrays(_load(baseline/'case.json'));final=arrays(_load(sample/'case.json'))
                original=_load(baseline/'metadata.json');metadata=_load(sample/'metadata.json')
                export_case(initial,folder/'initial_export.m')
                report=_load(sample/'feedback'/'result.json')
                row.update(constructed=True,initial_accepted=_load(baseline/'assessment.json')['checked']['accepted'],
                    final_accepted=sample_row['accepted'],repair_attempts=len(report['trace']),
                    repair_commits=report['accepted_steps'],requirements_preserved=preserved(initial,original,final,metadata,TransmissionSpec(**spec)))
                with solver_phase('independent_export_audit'):
                    for arm,case,meta,path in [('initial',initial,original,folder/'initial_export.m'),('final',final,metadata,sample/'case_generated.m')]:
                        audit=audit_transmission_export(path,case,meta,spec)
                        atomic_json(folder/(arm+'_audit.json'),audit)
                        row[arm+'_export_pass']=audit['export_pass'];row[arm+'_descriptors']=audit['descriptors']
                        row[arm+'_failed_checks']=[k for k,v in audit['checks'].items() if not v]
                        row[arm+'_failed_rules']=[k for k,v in audit['rule_checks'].items() if not v]
        except Exception as exc:
            row['error']=f'{type(exc).__name__}: {exc}'
            (folder/'exception.txt').write_text(traceback.format_exc())
    row['seconds']=time.monotonic()-started
    row['solver_meter']=dict(meter.snapshot(),events=meter.events)
    row['actual_ac_calls']=meter.snapshot()['total_ac_calls']+row.get('child_meter',{}).get('total_ac_calls',0)
    atomic_json(result_path,row)
    return row


def freeze(root):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    protocol_path=root/'protocol.json'
    if protocol_path.exists(): return _load(protocol_path)
    package=Path(__file__).resolve().parents[1]
    tasks=frozen_tasks()
    inventory={}
    for path in sorted(package.rglob('*')):
        relative=path.relative_to(package)
        if not path.is_file() or '__pycache__' in relative.parts: continue
        if path.suffix not in ('.py','.json','.csv','.yaml','.yml','.txt','.md'): continue
        if 'benchmark' in relative.parts and path.name not in ('__init__.py','m97_construction.py'): continue
        destination=root/'source'/'feeder_agents'/relative
        destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,destination)
        if 'benchmark' not in relative.parts: inventory[str(relative)]=file_digest(path)
    protocol=dict(version='m97_construction_v1',created_utc=datetime.now(timezone.utc).isoformat(),
        expected_count=72,profile_count=24,tasks=tasks,production_inventory=inventory,
        production_source_hash=digest(inventory),evaluator_hash=file_digest(Path(__file__)),
        versions={n:importlib.metadata.version(n) for n in ('numpy','scipy','networkx','pydantic','pypower','OpenDSSDirect.py')},
        initial_arm='The exact saved pre-feedback model from the same construction run.',
        repair_policy={'distribution_rounds':2,'hierarchy_rounds':4,'transmission_rounds':4,
            'selector':'FixedRepair deterministic violation priority; no live model calls',
            'topology_changes':False,'requirement_relaxation':False,'memory':False},
        acceptance='Exact request preservation AND fresh saved-export audit AND all original physical/rule checks; no revised threshold.',
        feasibility='Inputs are frozen supported structured requests, not a pre-certified feasible response cohort. Every constructor failure remains in the 72-task denominator.',
        graph_convention='Simple undirected bus graph; active lines plus explicit transformers are energized edges; normally-open ties count only in physical graph; parallel circuits represented by one corridor; degree includes source; depth is source hop eccentricity.',
        independence='Independent artifact reload and evaluator, with shared OpenDSS/PYPOWER backends and production rule/catalogue semantics; no independent-solver or realism calibration claim.',
        work_accounting='Actual SolverMeter requested AC calls including failures, generator initialization and feedback replay. Distribution child events are added explicitly. Hierarchy task process replaces nested isolation with identical simulate API so events remain visible.',
        stopping='One fixed draw per seed, no reseeding, no loop until acceptance; 180-second supervised sample bound for distribution.')
    protocol['protocol_hash']=digest(protocol)
    atomic_json(protocol_path,protocol)
    return protocol


def run_all(root, workers=2):
    root=Path(root).resolve();protocol=_load(root/'protocol.json')
    expected=root/'source'/'feeder_agents'/'benchmark'/'m97_construction.py'
    if Path(__file__).resolve()!=expected.resolve():
        raise RuntimeError('Run with PYTHONPATH='+str(root/'source')+' to use the frozen snapshot')
    if file_digest(Path(__file__))!=protocol['evaluator_hash']:raise ValueError('Frozen evaluator changed')
    source=Path(__file__).resolve().parents[1]
    if any(file_digest(source/name)!=sha for name,sha in protocol['production_inventory'].items()):
        raise ValueError('Frozen production source changed')
    if not 1<=workers<=2:raise ValueError('Use one or two task workers (distribution may add one supervised worker each)')
    records=[]
    with ProcessPoolExecutor(max_workers=workers,mp_context=multiprocessing.get_context('spawn')) as pool:
        futures={pool.submit(run_task,task,str(root)):task for task in protocol['tasks']}
        for future in as_completed(futures):
            task=futures[future]
            try:row=future.result()
            except Exception as exc:
                row=dict(id=task['id'],profile=task['profile'],domain=task['domain'],constructed=False,
                    error=f'Worker failure: {type(exc).__name__}: {exc}')
                atomic_json(root/'cases'/task['id']/'record.json',row)
            records.append(row)
            with (root/'ledger.jsonl').open('a') as stream:stream.write(json.dumps(row,allow_nan=False)+'\n')
            print(f"{len(records)}/72 {task['id']} constructed={row.get('constructed')} initial={row.get('initial_accepted')} final={row.get('final_accepted')} export={row.get('final_export_pass')} seconds={row.get('seconds',0):.2f}",flush=True)
    return write_report(root)


def write_report(root):
    root=Path(root);protocol=_load(root/'protocol.json')
    rows=[_load(root/'cases'/t['id']/'record.json') for t in protocol['tasks'] if (root/'cases'/t['id']/'record.json').exists()]
    summary=summarize_rows(rows,protocol['expected_count'])
    summary['by_domain']={domain:summarize_rows([r for r in rows if r['domain']==domain],sum(t['domain']==domain for t in protocol['tasks'])) for domain in ('distribution','hierarchy','transmission')}
    summary['actual_ac_calls']=sum(r.get('actual_ac_calls',0) for r in rows)
    summary['summed_case_seconds']=sum(r.get('seconds',0) for r in rows)
    summary['protocol_hash']=protocol['protocol_hash']
    summary['production_source_hash']=protocol['production_source_hash']
    atomic_json(root/'summary.json',summary)
    failures=[r for r in rows if not (r.get('final_accepted') and r.get('final_export_pass') and r.get('requirements_preserved'))]
    atomic_json(root/'failure_ledger.json',failures)
    flattened=[]
    for r in rows:
        for arm in ('initial','final'):
            flat={k:v for k,v in r.items() if not isinstance(v,(dict,list))}
            flat.update(arm=arm,accepted=r.get(arm+'_accepted',False),export_pass=r.get(arm+'_export_pass',False),**r.get(arm+'_descriptors',{}))
            flattened.append(flat)
    with (root/'raw_metrics.csv').open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=sorted({k for row in flattened for k in row}));writer.writeheader();writer.writerows(flattened)
    lines=['# M97 ordinary construction and export audit','',
        f"Frozen cohort: {summary['expected']} cases in 24 profiles, three independent seeds per profile. Completed: {summary['completed']}. No LLM calls.",'',
        '| Domain | Cases | Constructed | Initial physical acceptance | Final physical acceptance | Final verified delivery |',
        '|---|---:|---:|---:|---:|---:|']
    for domain,s in summary['by_domain'].items():lines.append(f"| {domain} | {s['expected']} | {s['constructed']} | {s['initial_accepted']} | {s['final_accepted']} | {s['final_delivered']} |")
    lines.extend(['',f"Across the full denominator, initial delivery passed {summary['initial_delivered']}/{summary['expected']} and final delivery passed {summary['final_delivered']}/{summary['expected']}. Fixed repair recovered {summary['repair_recoveries']} initially unaccepted cases. Constructor failures: {summary['constructor_failures']}; unfinished: {summary['missing']}.",'',
        f"Actual requested AC calls: {summary['actual_ac_calls']}. Summed per-case wall time, including construction, repair, native exports and independent auditing: {summary['summed_case_seconds']:.2f} seconds. Worker parallelism means this sum is not elapsed batch time.",'',
        'Initial and repaired arms use the exact saved initial model. Ordinary distribution permits two conductor-upgrade rounds; hierarchical and transmission workflows use at most four deterministic violation-priority decisions. Original demand, representation, counts, topology and thresholds remain protected. This comparison measures bounded deterministic repair, not an LLM effect.','',
        'No source-request feasibility guarantee was assumed. Syntax validation is not a physical feasibility certificate. All fixed seed attempts, construction failures, solver failures, unmet physical rules and failed exports remain visible. The audit uses independent saved-export loading and direct object inspection, with shared numerical engines and rule semantics.','',
        'Graph convention: '+protocol['graph_convention'],'',
        '## Observed descriptor ranges','', '| Domain | Descriptor | Minimum | Maximum |','|---|---|---:|---:|'])
    for domain in summary['by_domain']:
        for metric in ('nodes','energized_edges','physical_cycle_rank','degree_max','diameter_hops','terminal_fraction','total_line_km','min_voltage_pu','max_voltage_pu','max_loading_ratio','max_branch_loading'):
            vals=[r['final_descriptors'][metric] for r in rows if r['domain']==domain and isinstance(r.get('final_descriptors',{}).get(metric),(int,float))]
            if vals:lines.append(f'| {domain} | {metric} | {min(vals):.6g} | {max(vals):.6g} |')
    lines.extend(['','These ranges describe the generated sample only. They do not establish statistical calibration to real urban, rural or transmission populations. Snapshot feasibility does not certify dynamics, time series, N−1 security or protection. Hierarchical LV uses a grounded equivalent without an explicit neutral.','',
        '## Failure ledger',''])
    if not failures:lines.append('No final failures in this frozen cohort.')
    for r in failures:lines.append(f"- `{r['id']}`: {r.get('error') or ', '.join(r.get('final_failed_checks',[])+r.get('final_failed_rules',[])) or 'requirements-preservation failure'}")
    lines.extend(['','## Reproducibility','',f"Production source hash: `{protocol['production_source_hash']}`. Protocol hash: `{protocol['protocol_hash']}`.",
        'Frozen specs, source snapshot, software versions, source inventory and acceptance definitions are in `protocol.json`. Per-case artifacts, meter events, audit measurements and repair histories are under `cases/`. `raw_metrics.csv` contains both arms; `failure_ledger.json` preserves every final failure.'])
    (root/'report.md').write_text('\n'.join(lines)+'\n')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(1,2,figsize=(9,3.5),layout='constrained')
    labels=['Distribution\n(n=45)','MV/LV hierarchy\n(n=9)','Transmission\n(n=18)']
    x=np.arange(3);domains=list(summary['by_domain'])
    for offset,arm,color in [(-.18,'initial','#6b8cae'),(.18,'final','#238b78')]:
        values=[100*summary['by_domain'][d][arm+'_delivered']/summary['by_domain'][d]['expected'] for d in domains]
        bars=axes[0].bar(x+offset,values,width=.34,label=arm.title(),color=color)
        axes[0].bar_label(bars,labels=[f"{summary['by_domain'][d][arm+'_delivered']}/{summary['by_domain'][d]['expected']}" for d in domains],fontsize=8,padding=3)
    axes[0].set(xticks=x,xticklabels=labels,ylim=(0,114),ylabel='Verified delivery (%)',title='Same baseline, bounded fixed repair');axes[0].legend(frameon=False,loc='lower right')
    for i,d in enumerate(domains):
        subset=[r for r in rows if r['domain']==d]
        axes[1].scatter([i]*len(subset),[r.get('actual_ac_calls',0) for r in subset],alpha=.6,s=20,color=['#476f9a','#b18744','#238b78'][i])
    axes[1].set(xticks=x,xticklabels=labels,ylabel='Actual requested AC calls per case',title='Construction, repair and export audit')
    for ax in axes:ax.spines[['top','right']].set_visible(False);ax.grid(axis='y',alpha=.2);ax.set_axisbelow(True)
    fig.savefig(root/'construction_summary.pdf');fig.savefig(root/'construction_summary.png',dpi=180);plt.close(fig)
    return summary
