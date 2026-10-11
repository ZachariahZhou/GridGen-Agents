"""Small matched AC pilot; preserve every outcome, including bounded failures."""
import argparse
import copy
import csv
import json
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import yaml

from feeder_agents.artifacts import atomic_json, digest
from feeder_agents.response_design import run_response_design


ARMS = {'greedy': ('monotone', 'greedy'),
        'guided': ('monotone', 'heuristic'),
        'archive': ('archive', 'heuristic')}


def cases(repo, joint_actions=1):
    template = yaml.safe_load((repo/'examples/response_selective_25.yaml').read_text())
    plans = []
    for seed in (1311, 1317, 1323):
        for task in ('selective_q', 'opposed_p'):
            p = copy.deepcopy(template)
            p['base_spec']['seed'] = seed
            if task == 'opposed_p':
                p['base_spec'].update(n_buses=37, total_kw_min=280, total_kw_max=280)
                p['base_spec']['scenario'].update(kind='rural', engineering_profile='rural')
                p['targets'][1].update(lower=.65, upper=.85)
            for probe in p['probes']:
                probe['kind'] = 'voltage_q' if task == 'selective_q' else 'voltage_p'
            p['search'].update(max_rounds=6, candidates_per_round=8,
                max_joint_actions=joint_actions, max_search_ac_calls=160, archive_width=4,
                exploration_deficit_allowance=.15)
            p['research_question'] = f'Fixed-permission {task} response-search pilot, seed {seed}.'
            plans.append((f'{task}_{seed}', p))
    for buses, seed in ((139, 1311), (237, 1317)):
        p = dict(research_question='Reduce peak transfer response with bounded complete parallel circuits.',
            base_spec=dict(network_kind='transmission', n_buses=buses, n_generators=12 if buses==139 else 20,
                voltage_kv=220, total_mw=1200 if buses==139 else 2000, seed=seed),
            probes=[dict(id='transfer', kind='transfer_p', injection_buses=['10'],
                withdrawal_buses=['30'], step_mw=.5, aggregation='max_abs')],
            targets=[dict(probe_id='transfer', reference='baseline_ratio', lower=.5, upper=.98)],
            search=dict(allowed_actions=['parallel_line'], max_rounds=6,
                candidates_per_round=8, max_joint_actions=1, max_search_ac_calls=96,
                archive_width=4, exploration_deficit_allowance=.15))
        plans.append((f'transfer_{buses}_{seed}', p))
    return plans


def execute(job):
    name, arm, plan, output = job
    start = time.perf_counter()
    row = dict(case_id=name, arm=arm, n_buses=plan['base_spec']['n_buses'],
               seed=plan['base_spec']['seed'], search_limit=plan['search']['max_search_ac_calls'])
    try:
        result = run_response_design(plan, output, f'{name}_{arm}')
        root = Path(result['directory'])
        rows = json.loads((root/'history.json').read_text())
        resolved = json.loads((root/'resolved_probes.json').read_text())
        baseline_model = json.loads((root/('baseline/model/feeder.json' if plan['base_spec']['network_kind']=='distribution'
                                        else 'baseline/model/case.json')).read_text())
        row.update(success=result['target_met'], feasible=result['selected']['base_accepted'],
            contract_preserved=result['protected_contract_preserved'],
            verification_passed=result['verification_passed'],
            search_ac_calls=result['solver_accounting']['by_phase'].get('search', 0),
            total_ac_calls=result['solver_accounting']['total_ac_calls'],
            deficit=sum(result['selected']['deficits']),
            accepted_steps=result['accepted_steps'], candidates=result['evaluated_candidates'],
            duplicate_skips=sum(r.get('reason')=='duplicate_physical_model' for r in rows),
            retained_tradeoffs=sum(r.get('eligible',False) and not r.get('nonregressing_improvement',True) for r in rows),
            selected_tradeoffs=sum(r.get('committed',False) and not r.get('nonregressing_improvement',True) for r in rows),
            response_values=[r['observed'] for r in result['selected']['targets']],
            baseline_hash=digest(baseline_model), probes_hash=digest(resolved),
            stop_reason=result['stop_reason'], result_hash=result['result_hash'], directory=str(root))
    except Exception as exc:
        row.update(success=False, error=f'{type(exc).__name__}: {exc}')
    row['seconds'] = round(time.perf_counter()-start, 3)
    return row


def summarize(output, rows):
    suite = json.loads((output/'suite.json').read_text())
    joint_actions = suite['jobs'][0]['plan']['search']['max_joint_actions']
    summary = {}
    for arm in ARMS:
        group = [r for r in rows if r['arm'] == arm]
        summary[arm] = dict(tasks=len(group), successes=sum(r['success'] for r in group),
            feasible=sum(r.get('feasible',False) for r in group),
            search_ac_calls=sum(r.get('search_ac_calls',0) for r in group),
            total_ac_calls=sum(r.get('total_ac_calls',0) for r in group),
            selected_tradeoffs=sum(r.get('selected_tradeoffs',0) for r in group))
    matches = []
    for case_id in sorted({r['case_id'] for r in rows}):
        group = [r for r in rows if r['case_id'] == case_id]
        matches.append(dict(case_id=case_id, all_arms_complete=len(group)==len(ARMS),
            identical_baseline=len({r.get('baseline_hash') for r in group})==1 and all(r.get('baseline_hash') for r in group),
            identical_probes=len({r.get('probes_hash') for r in group})==1 and all(r.get('probes_hash') for r in group),
            search_limits_respected=all(r.get('search_ac_calls', float('inf'))<=r['search_limit'] for r in group)))
    atomic_json(output/'summary.json', dict(arms=summary, matched_checks=matches,
        scope='Development pilot on fixed authored requests; not a held-out generalization estimate or comparison with independent random legal search.'))
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with (output/'results.csv').open('w',newline='') as stream:
        writer = csv.DictWriter(stream,fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'axes.spines.top':False,'axes.spines.right':False})
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6), layout='constrained')
    labels = ['Greedy', 'Guided monotone', 'Bounded archive']
    colors = ['#8b98ab', '#4383a8', '#268579']
    for index, (arm, label, color) in enumerate(zip(ARMS,labels,colors)):
        s = summary[arm]
        axes[0].bar(index, s['successes'], color=color, width=.6)
        axes[0].text(index, s['successes']+.1, f"{s['successes']}/{s['tasks']}", ha='center')
        axes[1].bar(index, s['search_ac_calls'], color=color, width=.6)
    axes[0].set(ylabel='Fully accepted tasks',ylim=(0,max(s['tasks'] for s in summary.values())+1),title='Response and final verification')
    axes[1].set(ylabel='Requested AC evaluations',title='Actual search work')
    for ax in axes:
        ax.set_xticks(range(3),labels); ax.grid(axis='y',alpha=.18); ax.set_axisbelow(True)
    fig.savefig(output/'m91_search.png',dpi=200); fig.savefig(output/'m91_search.pdf'); plt.close(fig)
    lines = ['# M91 metered response-search pilot','',
        '## Purpose','Test bounded alternative-path exploration while preserving identical physical design permissions and reporting actual requested AC evaluations.','',
        '## Protocol',f'Eight fixed tasks: three 25-bus selective Q-response cases, three 37-bus opposing P-response cases, and 139-/237-bus transfer cases. Each task uses all three search arms, identical initial models, ports, target intervals and action bounds. Each distribution task has a 160-call search limit; each transmission task has a 96-call limit. All arms allow six expansions and eight previews per expansion. Distribution proposals contain at most {joint_actions} elementary moves; transmission proposals contain one. The archive retains at most four alternatives and allows a temporary sum-deficit increase of 0.15.','',
        'These are structured numerical experiments. They do not invoke an LLM or measure language understanding. All attempted cases are included. One counted call is a requested AC snapshot evaluation; internal Newton iterations and OpenDSS voltage-base initialization are excluded. Construction, baseline, export and final verification are separately counted and are outside the search limit.','',
        '| Arm | Accepted | Feasible final models | Search AC calls | Total AC calls | Selected tradeoff steps |',
        '|---|---:|---:|---:|---:|---:|']
    for arm, s in summary.items():
        lines.append(f"| {arm} | {s['successes']}/{s['tasks']} | {s['feasible']}/{s['tasks']} | {s['search_ac_calls']} | {s['total_ac_calls']} | {s['selected_tradeoffs']} |")
    lines.extend(['','## Interpretation',
        'The greedy arm disables the transfer lookahead and selects measured deficit improvements. The guided arm retains the M90 monotone acceptance and physical ranking. The archive arm shares that physical proposer but retains bounded feasible tradeoffs and alternative paths. These comparisons do not isolate proposal quality against an independent random legal-design sampler.',
        'Equal upper budgets do not imply identical realized work. Report both the per-task success and actual call counts; never infer universal superiority from this small development pilot. Search failures are not infeasibility proofs. The final half-step test is numerical consistency evidence, not independent-solver or downstream-controller validation.','',
        '![Matched pilot](m91_search.png)','',
        '## Complete per-task outcomes','',
        '| Case | Arm | Accepted | Deficit | Search calls | Stop reason |','|---|---|---:|---:|---:|---|'])
    for row in rows:
        lines.append(f"| {row['case_id']} | {row['arm']} | {row['success']} | {row.get('deficit', float('nan')):.6g} | {row.get('search_ac_calls','')} | {row.get('stop_reason',row.get('error',''))} |")
    (output/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='experiments/m91_response_search')
    parser.add_argument('--workers',type=int,default=2)
    parser.add_argument('--joint-actions',type=int,choices=[1,2],default=1)
    args=parser.parse_args()
    output=Path(args.output).resolve(); output.mkdir(parents=True,exist_ok=True)
    repo=Path(__file__).resolve().parents[1]
    jobs=[]
    for name, template in cases(repo, args.joint_actions):
        for arm,(strategy,selector) in ARMS.items():
            plan=copy.deepcopy(template);plan['search'].update(strategy=strategy,selector=selector)
            jobs.append((name,arm,plan,str(output)))
    suite=dict(protocol='m91_matched_pilot_v1',jobs=[dict(case_id=n,arm=a,plan=p) for n,a,p,_ in jobs])
    if (output/'suite.json').exists() and json.loads((output/'suite.json').read_text()) != suite:
        raise ValueError('Existing suite differs; use a new output directory')
    atomic_json(output/'suite.json',suite)
    rows=[]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for future in as_completed([pool.submit(execute,job) for job in jobs]):
            row=future.result();rows.append(row)
            rows.sort(key=lambda r:(r['case_id'],r['arm']))
            atomic_json(output/'results.json',dict(rows=rows))
            print(row['case_id'],row['arm'],row['success'],row.get('search_ac_calls'),
                  row.get('stop_reason',row.get('error')),flush=True)
    summarize(output,rows)


if __name__ == '__main__':
    main()
