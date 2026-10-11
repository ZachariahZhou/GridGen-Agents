"""Independent, frozen downstream reuse study. No generator/evaluator imports."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import shutil
import statistics
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')


def write_csv(path, rows):
    rows = list(rows)
    keys = list(dict.fromkeys(key for row in rows for key in row))
    with Path(path).open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def voltage_metrics(values):
    values = list(values)
    if not values or not all(math.isfinite(v) for v in values):
        raise ValueError('Missing or nonfinite phase voltages')
    return {
        'phase_count': len(values), 'voltage_min_pu': min(values),
        'voltage_max_pu': max(values),
        'voltage_mae_pu': statistics.mean(abs(v - 1) for v in values),
        'voltage_rmse_pu': math.sqrt(statistics.mean((v - 1) ** 2 for v in values)),
        'voltage_violation_count': sum(v < .95 or v > 1.05 for v in values),
    }


def allocate_q(total_mvar, count):
    if count <= 0:
        raise ValueError('Actuator requires at least one phase port')
    return [total_mvar / count] * count


def next_q(q_mvar, mean_voltage_pu, gain, limit):
    return max(-limit, min(limit, q_mvar + gain * (1 - mean_voltage_pu)))


def iteration_behavior(commands):
    tail = commands[-9:]
    increments = [b - a for a, b in zip(tail, tail[1:])]
    significant = [x for x in increments if abs(x) > 1e-6]
    flips = sum(a * b < 0 for a, b in zip(significant, significant[1:]))
    span = max(tail) - min(tail) if tail else 0
    return {'oscillatory_tail': flips >= 6 and span > 1e-4,
            'tail_q_span_mvar': span, 'tail_q_sign_reversals': flips}


def _measure(engine, case):
    voltages = {}
    local = []
    port_set = {(str(bus).lower(), phase) for bus, phase in case['ports']}
    for bus in engine.Circuit.AllBusNames():
        engine.Circuit.SetActiveBus(bus)
        for phase, magnitude in zip(engine.Bus.Nodes(), engine.Bus.puVmagAngle()[::2]):
            if phase in (1, 2, 3):
                if bus.lower() != case['source_bus'].lower():
                    voltages[f'{bus}.{phase}'] = magnitude
                if (bus.lower(), phase) in port_set:
                    local.append(magnitude)
    if len(local) != len(port_set):
        raise ValueError('Frozen controller port is absent from delivered circuit')
    thermal = {}
    for line in engine.Lines:
        rated = engine.CktElement.NormalAmps()
        if rated <= 0:
            raise ValueError('Nonpositive thermal rating')
        thermal[line.Name()] = max(engine.CktElement.CurrentsMagAng()[::2]) / rated
    actual_q = 0.0
    for generator in engine.Generators:
        if generator.Name().startswith('m97q_'):
            actual_q -= sum(engine.CktElement.Powers()[1:2 * engine.CktElement.NumConductors():2]) / 1000
    delivered_kw = delivered_kvar = 0.0
    for load in engine.Loads:
        power = engine.CktElement.Powers()[:2 * engine.CktElement.NumConductors()]
        delivered_kw += sum(power[::2])
        delivered_kvar += sum(power[1::2])
    metrics = voltage_metrics(voltages.values())
    metrics.update(
        ac_converged=bool(engine.Solution.Converged()),
        local_voltage_mean_pu=statistics.mean(local),
        local_voltage_worst_error_pu=max(abs(v - 1) for v in local),
        local_voltage_mean_error_pu=abs(1 - statistics.mean(local)),
        actual_q_mvar=actual_q, abs_actual_q_mvar=abs(actual_q),
        thermal_max_ratio=max(thermal.values(), default=0),
        thermal_violation_count=sum(v > 1 for v in thermal.values()),
        loss_kw=engine.Circuit.Losses()[0] / 1000,
        source_kw=-engine.Circuit.TotalPower()[0],
        source_kvar=-engine.Circuit.TotalPower()[1],
        delivered_load_kw=delivered_kw, delivered_load_kvar=delivered_kvar,
        ac_internal_iterations=engine.Solution.Iterations(),
    )
    return metrics, voltages, thermal


def _initialize(case, scenario, output):
    from opendssdirect import dss
    engine = dss.NewContext()
    engine.Basic.AllowChangeDir(False)
    engine.Basic.DataPath(str(Path(output).resolve()))
    engine.Text.Command(f'Compile "{case["master_path"]}"')
    base_converged = bool(engine.Solution.Converged())
    entries = [(load.Name(), load.kW(), load.kvar()) for load in engine.Loads]
    totals = {}
    for name, kw, kvar in entries:
        stem = name.rsplit('_p', 1)[0]
        group = totals.setdefault(stem, [0.0, 0.0])
        group[0] += kw
        group[1] += kvar
    commands = []
    for name, kw, kvar in entries:
        if scenario['phase_weights'] is not None:
            stem, phase = name.rsplit('_p', 1)
            weight = scenario['phase_weights'][int(phase) - 1]
            kw, kvar = (value * weight for value in totals[stem])
        commands.append(f'Edit Load.{name} kw={kw * scenario["load_scale"]:.15g} kvar={kvar * scenario["load_scale"]:.15g}')
    kv = case['voltage_kv'] / math.sqrt(3)
    for index, (bus, phase) in enumerate(case['ports']):
        commands.append(f'New Generator.m97q_{index} bus1={bus}.{phase}.0 phases=1 conn=wye kv={kv:.15g} kw=0 kvar=0 model=1 vminpu=0.01 vmaxpu=2')
    for command in commands:
        engine.Text.Command(command)
    engine.Solution.Solve()
    return engine, commands, base_converged


def _set_q(engine, total, count):
    commands = []
    for index, value in enumerate(allocate_q(total, count)):
        command = f'Edit Generator.m97q_{index} kw=0 kvar={value * 1000:.15g}'
        engine.Text.Command(command)
        commands.append(command)
    return commands


def run_case(case, scenario, gain, output, protocol):
    """Evaluate a copied delivered circuit; write summaries and final wrapper only."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    for path, expected in case['source_model_hashes'].items():
        if sha256(path) != expected:
            raise ValueError(f'Input hash mismatch before execution: {path}')
    engine, setup, baseline_ok = _initialize(case, scenario, output)
    metrics, voltages, thermal = _measure(engine, case)
    q = 0.0
    history = [{**metrics, 'update': 0, 'command_q_mvar': q}]
    all_converged = baseline_ok and metrics['ac_converged']
    controller_converged = False
    consecutive = 0
    q_commands = []
    for update in range(1, protocol['max_updates'] + 1):
        if gain == 0 or not all_converged:
            break
        previous_v = metrics['local_voltage_mean_pu']
        proposed = next_q(q, previous_v, gain, protocol['q_limit_mvar'])
        q_commands = _set_q(engine, proposed, len(case['ports']))
        engine.Solution.Solve()
        metrics, voltages, thermal = _measure(engine, case)
        all_converged = all_converged and metrics['ac_converged']
        small = (abs(proposed - q) <= protocol['q_tolerance_mvar'] and
                 abs(metrics['local_voltage_mean_pu'] - previous_v) <= protocol['voltage_tolerance_pu'])
        consecutive = consecutive + 1 if small else 0
        q = proposed
        history.append({**metrics, 'update': update, 'command_q_mvar': q})
        if consecutive >= 2 and all_converged:
            controller_converged = True
            break
    unchanged = all(sha256(path) == expected for path, expected in case['source_model_hashes'].items())
    result = {key: case[key] for key in ('model_id', 'network_id', 'variant', 'n_buses', 'scene')}
    result.update(metrics)
    result.update(
        scenario=scenario['id'], holdout=scenario['id'] != 'reference', gain=gain,
        command_q_mvar=q, ac_converged=all_converged,
        controller_converged=controller_converged,
        target_reached=metrics['local_voltage_mean_error_pu'] <= protocol['target_tolerance_pu'],
        saturated=abs(q) >= protocol['q_limit_mvar'] - 1e-6,
        saturation_fraction=statistics.mean(abs(row['command_q_mvar']) >= protocol['q_limit_mvar'] - 1e-6 for row in history),
        updates=len(history) - 1, ac_calls=len(history) + 1,
        input_unchanged=unchanged, execution_error='',
        q_readback_error_mvar=abs(q - metrics['actual_q_mvar']),
        **iteration_behavior([row['command_q_mvar'] for row in history]),
    )
    result['status'] = ('ac_failure' if not all_converged else 'uncontrolled' if gain == 0 else
                        'converged' if controller_converged else 'oscillatory' if result['oscillatory_tail'] else 'iteration_cap')
    # Recompiling an immutable input and replaying the finite perturbation is callable.
    (output / 'Final.dss').write_text('\n'.join([f'Compile "{case["master_path"]}"', *setup, *q_commands, 'Solve', '']))
    write_csv(output / 'iterations.csv', history)
    write_json(output / 'final_phase_voltages.json', voltages)
    write_json(output / 'final_thermal_ratios.json', thermal)
    write_json(output / 'result.json', result)
    return result


def _protected(model):
    return {
        'fixed': {key: model[key] for key in ('phase_mode', 'voltage_kv', 'frequency_hz', 'source_bus', 'total_kw')},
        'buses': [{key: bus[key] for key in ('id', 'phases')} for bus in model['buses']],
        'loads': model['loads'],
        'connectivity': [{key: line[key] for key in ('id', 'bus1', 'bus2', 'phases')}
                         for line in model['lines'] + model['tie_lines']],
    }


def task_variant(task_id):
    return ('equipment_mixed_witness' if '_equipment_' in task_id or '_mixed_' in task_id
            else 'geometry_witness')


def freeze_study(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    if (output / 'manifest.json').exists():
        raise FileExistsError('Frozen manifest already exists; use run or a new study directory')
    output.mkdir(parents=True, exist_ok=True)
    rows = json.loads((root / 'experiments/m96_formal_response_v1_verified/results.json').read_text())['rows']
    selected = sorted([row for row in rows if row['arm'] == 'full' and row['space'] == 'GE'
                       and row['layer'] == 'witness' and row['budget'] == 160], key=lambda x: x['task_id'])
    tasks = {task['task_id']: task for task in json.loads((root / 'experiments/m96_formal_tasks_v1/public_tasks.json').read_text())['tasks']}
    networks = {row['network_id']: row for row in json.loads((root / 'experiments/m96_formal_tasks_v1/networks.json').read_text())['networks']}
    if len(selected) != 32 or len({row['network_id'] for row in selected}) != 16:
        raise ValueError('Expected the complete 32-variant / 16-network cohort')
    models, ledger, source_hashes = [], [], {}
    for row in selected:
        directory = Path(row['directory'])
        task = tasks[row['task_id']]
        probes_path = directory / 'resolved_probes.json'
        probes = json.loads(probes_path.read_text())
        ports = next(p['injection_ports'] for p in probes if p['id'] == 'a')
        network_id = row['network_id']
        variant = task_variant(row['task_id'])
        base = directory / 'baseline/model'
        original = json.loads((base / 'feeder.json').read_text())
        delivered = json.loads((directory / 'selected/feeder.json').read_text())
        protected = _protected(original) == _protected(delivered)
        baseline_source = [line for line in (base / 'opendss/Master.dss').read_text().splitlines() if line.lower().startswith('new circuit.')]
        delivered_source = [line for line in (directory / 'selected/opendss/Master.dss').read_text().splitlines() if line.lower().startswith('new circuit.')]
        protected = protected and baseline_source == delivered_source
        if not protected:
            raise ValueError(f'Protected-field mismatch: {row["task_id"]}')
        ledger.append({**{key: row[key] for key in ('network_id', 'task_id', 'job_id', 'success', 'feasible', 'contract_preserved', 'response_values', 'changes')},
                       'requested_targets': task['plan']['targets'], 'variant': variant,
                       'protected_fields_match': protected, 'model_provenance': 'actual full-method selected delivery, not private witness'})
        for label, source in [('ordinary', base), (variant, directory / 'selected')]:
            model_id = f'{network_id}_{label}'
            if any(model['model_id'] == model_id for model in models):
                continue
            dest = output / 'inputs' / model_id
            dest.mkdir(parents=True)
            paths = [source / 'feeder.json', *(source / 'opendss').glob('*.dss'), source / 'opendss/BusCoords.csv']
            hashes = {}
            for path in paths:
                if not path.is_file():
                    raise FileNotFoundError(path)
                hashes[str(path)] = sha256(path)
                target = dest / path.name
                shutil.copy2(path, target)
                hashes[str(target)] = sha256(target)
            model = json.loads((dest / 'feeder.json').read_text())
            if any(load['pv_kw'] != 0 for load in model['loads']):
                raise ValueError('Unexpected PV in frozen no-PV cohort')
            models.append({'model_id': model_id, 'network_id': network_id, 'variant': label,
                           'master_path': str(dest / 'Master.dss'), 'ports': ports,
                           'voltage_kv': model['voltage_kv'], 'source_bus': model['source_bus'],
                           'n_buses': len(model['buses']), 'scene': networks[network_id]['scene'],
                           'source_model_hashes': hashes, 'total_kw': model['total_kw']})
        source_hashes[str(probes_path)] = sha256(probes_path)
    if len(models) != 48:
        raise ValueError(f'Expected 48 distinct matched models, found {len(models)}')
    source_paths = ['src/feeder_agents/benchmark/m97_downstream.py', 'scripts/run_m97_downstream.py',
                    'tests/test_m97_downstream.py', 'docs/research/m97-downstream-protocol.md']
    for relative in source_paths:
        path = root / relative
        source_hashes[relative] = sha256(path)
        destination = output / 'source' / path.name
        destination.parent.mkdir(exist_ok=True)
        shutil.copy2(path, destination)
    for relative in ('experiments/m96_formal_response_v1_verified/results.json',
                     'experiments/m96_formal_tasks_v1/public_tasks.json',
                     'experiments/m96_formal_tasks_v1/networks.json'):
        source_hashes[relative] = sha256(root / relative)
    protocol = {'q_limit_mvar': .3, 'gains': [0, 5, 25, 100], 'max_updates': 80,
                'q_tolerance_mvar': 1e-6, 'voltage_tolerance_pu': 1e-6,
                'target_tolerance_pu': 1e-4, 'setpoint_pu': 1.0,
                'scenarios': [{'id': name, 'load_scale': scale, 'phase_weights': weights}
                              for name, scale, weights in [('reference', 1, None), ('load085', .85, None),
                                ('load115', 1.15, None), ('load130', 1.3, None),
                                ('phase_reverse', 1.15, [.2, .3, .5]),
                                ('phase_concentrated', 1.15, [.7, .2, .1])]]}
    manifest = {'study': 'm97_downstream_v1', 'frozen_at': datetime.now(timezone.utc).isoformat(),
                'origin': 'M96 known-feasible witness-task selected deliveries; reused base networks',
                'expected_networks': 16, 'requested_variants': 32, 'available_variants': sum(row['success'] for row in selected),
                'expected_models': 48, 'expected_runs': 1152, 'protocol': protocol,
                'models': models, 'source_hashes': source_hashes, 'source_scope': source_paths,
                'source_snapshot_hash': sha256(output / 'source/m97_downstream.py')}
    write_json(output / 'generation_ledger.json', ledger)
    write_json(output / 'manifest.json', manifest)
    write_json(output / 'manifest_checksum.json', {'sha256': sha256(output / 'manifest.json')})
    return manifest


def _job(job):
    case, scenario, gain, output, protocol = job
    try:
        return run_case(case, scenario, gain, output, protocol)
    except Exception:
        result = {key: case[key] for key in ('model_id', 'network_id', 'variant', 'n_buses', 'scene')}
        result.update(scenario=scenario['id'], holdout=scenario['id'] != 'reference', gain=gain,
                      execution_error=traceback.format_exc(), status='execution_failure',
                      ac_converged=False, controller_converged=False)
        write_json(Path(output) / 'result.json', result)
        return result


def sensitivity_crosscheck(case, output):
    """Independent finite-Q cross-check at reference; never used for tuning."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    engine, _, baseline_ok = _initialize(case, {'load_scale': 1, 'phase_weights': None}, output)
    before, _, _ = _measure(engine, case)
    _set_q(engine, .005, len(case['ports']))
    engine.Solution.Solve()
    after, _, _ = _measure(engine, case)
    return {'model_id': case['model_id'], 'network_id': case['network_id'], 'variant': case['variant'],
            'sensitivity_pu_per_mvar': (after['local_voltage_mean_pu'] - before['local_voltage_mean_pu']) / .005,
            'actual_delta_q_mvar': after['actual_q_mvar'] - before['actual_q_mvar'],
            'ac_converged': baseline_ok and before['ac_converged'] and after['ac_converged'], 'ac_calls': 3}


def execute_study(output, workers=4):
    output = Path(output).resolve()
    manifest = json.loads((output / 'manifest.json').read_text())
    if sha256(output / 'manifest.json') != json.loads((output / 'manifest_checksum.json').read_text())['sha256']:
        raise ValueError('Frozen manifest changed')
    if sha256(__file__) != manifest['source_snapshot_hash']:
        raise ValueError('Execution source does not match frozen study')
    if not 1 <= workers <= 6:
        raise ValueError('Study permits one to six worker processes')
    protocol = manifest['protocol']
    jobs = [(case, scenario, gain, output / 'runs' / case['model_id'] / f'{scenario["id"]}_g{gain}', protocol)
            for case in manifest['models'] for scenario in protocol['scenarios'] for gain in protocol['gains']]
    write_json(output / 'execution.json', {'started_at': datetime.now(timezone.utc).isoformat(),
               'workers': workers, 'source_hash': sha256(__file__), 'manifest_hash': sha256(output / 'manifest.json')})
    results = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_job, job) for job in jobs]
        for future in as_completed(futures):
            results.append(future.result())
            if len(results) % 96 == 0:
                print(f'Completed {len(results)}/{len(jobs)} downstream runs', flush=True)
                write_json(output / 'progress.json', {'completed': len(results), 'expected': len(jobs)})
    results.sort(key=lambda row: (row['model_id'], row['scenario'], row['gain']))
    write_csv(output / 'runs.csv', results)
    write_json(output / 'runs.json', results)
    crosschecks = []
    for case in manifest['models']:
        try:
            crosschecks.append(sensitivity_crosscheck(case, output / 'sensitivity' / case['model_id']))
        except Exception:
            crosschecks.append({'model_id': case['model_id'], 'network_id': case['network_id'],
                               'variant': case['variant'], 'execution_error': traceback.format_exc()})
    write_csv(output / 'independent_sensitivity.csv', crosschecks)
    write_json(output / 'independent_sensitivity.json', crosschecks)
    all_hashes = {path: digest for case in manifest['models'] for path, digest in case['source_model_hashes'].items()}
    changed = [path for path, digest in all_hashes.items() if sha256(path) != digest]
    write_json(output / 'input_integrity.json', {'checked_files': len(all_hashes), 'changed': changed,
                                               'unchanged': not changed})
    summarize_study(output)
    write_json(output / 'complete.json', {'completed_at': datetime.now(timezone.utc).isoformat(),
              'expected_runs': len(jobs), 'completed_runs': len(results),
              'execution_errors': sum(bool(r.get('execution_error')) for r in results),
              'input_unchanged': not changed, 'source_hash': sha256(__file__)})
    return results


def summarize_study(output):
    import numpy as np
    output = Path(output)
    rows = json.loads((output / 'runs.json').read_text())
    metrics = ['voltage_mae_pu', 'voltage_rmse_pu', 'voltage_min_pu', 'voltage_max_pu',
               'voltage_violation_count', 'local_voltage_mean_error_pu', 'local_voltage_worst_error_pu',
               'abs_actual_q_mvar', 'thermal_max_ratio', 'thermal_violation_count', 'loss_kw', 'updates',
               'controller_converged', 'target_reached', 'saturated', 'oscillatory_tail']
    means = []
    for holdout in (False, True):
        for network in sorted({r['network_id'] for r in rows}):
            for variant in ('ordinary', 'geometry_witness', 'equipment_mixed_witness'):
                for gain in (0, 5, 25, 100):
                    group = [r for r in rows if r['network_id'] == network and r['variant'] == variant
                             and r['gain'] == gain and r['holdout'] == holdout]
                    valid = [r for r in group if not r.get('execution_error') and r['ac_converged']]
                    mean = {'network_id': network, 'variant': variant, 'gain': gain, 'holdout': holdout,
                            'runs': len(group), 'valid_runs': len(valid)}
                    mean.update({metric: statistics.mean(r[metric] for r in valid) if valid else None for metric in metrics})
                    means.append(mean)
    write_csv(output / 'network_summary.csv', means)
    index = {(r['network_id'], r['variant'], r['gain'], r['holdout']): r for r in means}
    paired = []
    for row in means:
        if row['variant'] == 'ordinary':
            continue
        base = index[(row['network_id'], 'ordinary', row['gain'], row['holdout'])]
        paired.append({**{key: row[key] for key in ('network_id', 'variant', 'gain', 'holdout', 'runs', 'valid_runs')},
                       **{f'delta_{metric}': row[metric] - base[metric] if row[metric] is not None and base[metric] is not None else None
                          for metric in metrics}})
    write_csv(output / 'network_paired.csv', paired)
    effects = []
    rng = np.random.default_rng(970031)
    for holdout in (False, True):
        for variant in ('geometry_witness', 'equipment_mixed_witness'):
            for gain in (0, 5, 25, 100):
                group = [r for r in paired if r['variant'] == variant and r['gain'] == gain and r['holdout'] == holdout]
                for metric in metrics:
                    values = np.array([r[f'delta_{metric}'] for r in group if r[f'delta_{metric}'] is not None])
                    if not len(values):
                        continue
                    boots = rng.choice(values, size=(5000, len(values)), replace=True).mean(axis=1)
                    effects.append({'variant': variant, 'gain': gain, 'holdout': holdout, 'metric': metric,
                                    'networks': len(values), 'mean_difference': float(values.mean()),
                                    'ci_low': float(np.quantile(boots, .025)), 'ci_high': float(np.quantile(boots, .975))})
    write_csv(output / 'paired_effects.csv', effects)
    from collections import Counter
    summary = {'runs': len(rows), 'networks': len({r['network_id'] for r in rows}),
               'status_counts': dict(Counter(r['status'] for r in rows)),
               'ac_converged': sum(r['ac_converged'] for r in rows),
               'input_unchanged': all(r.get('input_unchanged', False) for r in rows),
               'execution_errors': sum(bool(r.get('execution_error')) for r in rows),
               'max_q_readback_error_mvar': max(r.get('q_readback_error_mvar', 0) for r in rows),
               'ac_calls_factorial': sum(r.get('ac_calls', 0) for r in rows),
               'groups': []}
    for holdout in (False, True):
        for variant in ('ordinary', 'geometry_witness', 'equipment_mixed_witness'):
            for gain in (0, 5, 25, 100):
                group = [r for r in rows if r['variant'] == variant and r['gain'] == gain and r['holdout'] == holdout]
                valid = [r for r in group if not r.get('execution_error') and r['ac_converged']]
                summary['groups'].append({'variant': variant, 'gain': gain, 'holdout': holdout, 'runs': len(group),
                   'ac_converged': len(valid), 'status_counts': dict(Counter(r['status'] for r in group)),
                   **{metric: statistics.mean(r[metric] for r in valid) if valid else None for metric in metrics}})
    write_json(output / 'summary.json', summary)
    _plot(output, rows, means, effects)
    return summary


def _plot(output, rows, means, effects):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    colors = {'ordinary': '#354f6a', 'geometry_witness': '#be713a', 'equipment_mixed_witness': '#47998e'}
    labels = {'ordinary': 'Ordinary', 'geometry_witness': 'Geometry-task delivery', 'equipment_mixed_witness': 'Equipment/mixed-task delivery'}
    plt.rcParams.update({'font.size': 10, 'axes.spines.top': False, 'axes.spines.right': False,
                         'pdf.fonttype': 42, 'font.family': 'DejaVu Sans'})
    fig, axes = plt.subplots(2, 2, figsize=(11, 8))
    for ax, metric, ylabel in zip(axes.flat,
            ['local_voltage_mean_error_pu', 'abs_actual_q_mvar', 'controller_converged', 'thermal_max_ratio'],
            ['Local mean voltage error (pu)', 'Absolute actuator Q (Mvar)', 'Projected fixed-point convergence fraction', 'Maximum line loading ratio']):
        for variant, offset in zip(colors, [-.18, 0, .18]):
            for i, gain in enumerate((0, 5, 25, 100)):
                group = [r[metric] for r in means if r['variant'] == variant and r['gain'] == gain and r['holdout'] and r[metric] is not None]
                if metric == 'controller_converged' and gain == 0:
                    continue
                ax.scatter([i + offset] * len(group), group, s=16, color=colors[variant], alpha=.32)
                if group:
                    ax.scatter(i + offset, np.mean(group), color=colors[variant], marker='_', s=220, linewidth=2.5,
                               label=labels[variant] if i == (1 if metric == 'controller_converged' else 0) else None)
        ax.set_xticks(range(4), ['Off', '5', '25', '100'])
        ax.set_xlabel('Common feedback gain (Mvar / pu)')
        ax.set_ylabel(ylabel)
        ax.grid(axis='y', alpha=.2)
    axes[0, 0].legend(fontsize=8)
    fig.suptitle('Independent static control on 16 reused M96 networks\nEach point averages five held-out conditions within one network', fontsize=12)
    fig.tight_layout()
    for ext in ('png', 'pdf'):
        fig.savefig(output / f'm97_downstream_control.{ext}', dpi=220, bbox_inches='tight')
    plt.close(fig)
    fig, axes = plt.subplots(1, 3, figsize=(13, 4))
    for ax, metric, ylabel in zip(axes,
        ['local_voltage_mean_error_pu', 'abs_actual_q_mvar', 'voltage_mae_pu'],
        ['Difference in local voltage error (pu)', 'Difference in absolute Q (Mvar)', 'Difference in all-phase voltage MAE (pu)']):
        for variant, offset in [('geometry_witness', -.07), ('equipment_mixed_witness', .07)]:
            for i, gain in enumerate((0, 5, 25, 100)):
                effect = next(r for r in effects if r['variant'] == variant and r['gain'] == gain and r['holdout'] and r['metric'] == metric)
                value = effect['mean_difference']
                ax.errorbar(i + offset, value, yerr=[[value - effect['ci_low']], [effect['ci_high'] - value]],
                            color=colors[variant], fmt='o', capsize=3, label=labels[variant] if i == 0 else None)
        ax.axhline(0, color='#777777', linewidth=.8)
        ax.set_xticks(range(4), ['Off', '5', '25', '100'])
        ax.set_xlabel('Common feedback gain (Mvar / pu)')
        ax.set_ylabel(ylabel)
        ax.ticklabel_format(axis='y', style='sci', scilimits=(-3, 3))
    axes[0].legend(fontsize=8)
    fig.suptitle('Delivered variant minus ordinary case: five held-out conditions\nMeans and descriptive 95% network-bootstrap intervals; 16 independent networks', fontsize=11)
    fig.tight_layout()
    for ext in ('png', 'pdf'):
        fig.savefig(output / f'm97_downstream_paired.{ext}', dpi=220, bbox_inches='tight')
    plt.close(fig)
