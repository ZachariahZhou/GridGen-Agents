"""Audit all frozen response trials and export network-paired tables/figures."""
import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path

import numpy as np
from feeder_agents.artifacts import atomic_json,digest,file_digest
from feeder_agents.benchmark.response_summary import validate_rows,network_scores,paired_effect,group_summary
from feeder_agents.benchmark.response_trials import verify_frozen_suite,trial_plan
from feeder_agents.response_schema import ResponseDesignPlan
from feeder_agents.response_design import read_response_result,research_contract_preserved,score_response
from feeder_agents.response_search import physical_fingerprint
from feeder_agents.schemas import Feeder


def read(path):return json.loads(Path(path).read_text())


def audit_record(row,job,source_hash,reference=None):
    """Check invocation and actual work even when no final model was produced."""
    folder=Path(row['directory']).parents[1]
    request=read(folder/'trial.json');meter=row['actual_solver_accounting']
    expected=trial_plan(job['public_task'],job['arm'],job['budget'])
    failed=row['outcome'] in ('execution_error','worker_error')
    return dict(job_id=row['job_id'],row_checksum=read(folder/'row_checksum.json')['sha256']==digest(row),
        row_copy=read(folder/'row.json')==row,
        invocation=request==dict(public_task=job['public_task'],arm=job['arm'],random_seed=job['random_seed'],
            budget=job['budget'],plan=expected,source_hash=source_hash),
        identity=all(row[k]==v for k,v in dict(job_id=job['job_id'],network_id=job['public_task']['network_id'],
            task_id=job['public_task']['task_id'],layer=job['public_task']['layer'],space=job['public_task']['space'],
            arm=job['arm'],random_seed=job['random_seed'],budget=job['budget']).items()),
        hashes=row['task_hash']==digest(job['public_task']) and row['plan_hash']==digest(expected),
        accounting=all(isinstance(v,int) and v>=0 for v in [*meter['by_phase'].values(),*meter['by_backend'].values()])
            and sum(meter['by_phase'].values())==sum(meter['by_backend'].values())==meter['total_ac_calls']==row['total_ac_calls']
            and meter['by_phase'].get('search',0)==row['search_ac_calls'],
        error_is_failure=not failed or (row['success'] is False and bool(row.get('error'))),
        construction_reference=reference is None or (row.get('reference_matches') is not False and
            all(k not in row or row[k]==reference[k] for k in ('baseline_hash','baseline_probes_hash'))))


def audit_trial(row,job,source_hash,reference=None):
    root=Path(row['directory']);result=read_response_result(root)
    manifest=read(root/'manifest.json');plan=ResponseDesignPlan.model_validate(manifest['plan'])
    initial=dict(kind='distribution',feeder=Feeder.model_validate_json((root/'baseline/model/feeder.json').read_text()))
    final=dict(kind='distribution',feeder=Feeder.model_validate_json((root/'selected/feeder.json').read_text()))
    full=score_response(result['selected'],plan.targets,result['baseline'])
    half=score_response(result['verification'],plan.targets,result['baseline'])
    checks=read(root/'verification/checks.json')
    preserved=research_contract_preserved(initial,final,plan)
    accepted=bool(full['target_met'] and half['target_met'] and all(checks['step_consistency'].values()) and preserved)
    expected=trial_plan(job['public_task'],job['arm'],job['budget'])
    return dict(job_id=row['job_id'],recorded_result_hash=row['result_hash']==result['result_hash'],
        plan_hash=row['plan_hash']==digest(expected),declared_plan=plan.model_dump(mode='json')==expected,
        declared_task=row['task_hash']==digest(job['public_task']),source=manifest['source_hash']==source_hash,
        physical_baseline=row['baseline_hash']==physical_fingerprint(initial),
        measured_baseline=row['baseline_probes_hash']==digest(result['baseline']['probes']),
        resolved_ports=row['probes_hash']==digest(read(root/'resolved_probes.json')),
        construction_reference=reference is None or all(row[k]==reference[k] for k in ('baseline_hash','baseline_probes_hash')),
        acceptance=row['success']==accepted,contract=row['contract_preserved']==preserved,
        response_values=row['response_values']==[t['observed'] for t in full['targets']],
        search_count=row['search_ac_calls']==result['solver_accounting']['by_phase'].get('search',0),
        total_count=row['total_ac_calls']==result['solver_accounting']['total_ac_calls'])


def export_csv(path,rows):
    fields=list(dict.fromkeys(k for row in rows for k in row))
    with path.open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
        writer.writerows({k:json.dumps(v,sort_keys=True) if isinstance(v,(dict,list)) else v for k,v in row.items()} for row in rows)


def summarize(root):
    root=Path(root);suite=verify_frozen_suite(root);rows=read(root/'results.json')['rows']
    validate_rows(suite['jobs'],rows)
    by_id={j['job_id']:j for j in suite['jobs']}
    audits=[];exceptions=[];records=[]
    for row in rows:
        job=by_id[row['job_id']];reference=suite.get('reference_bindings',{}).get(row['network_id'])
        records.append(audit_record(row,job,suite['source_hash'],reference))
        if row['outcome'] in ('execution_error','worker_error'):
            exceptions.append(dict(job_id=row['job_id'],error=row.get('error'),accounting_recorded=True));continue
        audits.append(audit_trial(row,job,suite['source_hash'],reference))
    paired=[]
    by_task=defaultdict(list)
    for row in rows:by_task[row['task_id']].append(row)
    for task,values in by_task.items():
        available=[r for r in values if all(k in r for k in ('baseline_hash','probes_hash','baseline_probes_hash'))]
        paired.append(dict(task_id=task,
            missing_evidence_only_on_execution_error=all(r in available or r['outcome']=='execution_error' for r in values),
            same_initial_physics=len({r['baseline_hash'] for r in available})<=1,
            same_ports=len({r['probes_hash'] for r in available})<=1,
            same_baseline_response=len({r['baseline_probes_hash'] for r in available})<=1))
    snapshots=all(file_digest(root/'source/feeder_agents'/name)==sha for name,sha in suite['source_files'].items())
    all_passed=snapshots and all(all(v for k,v in a.items() if k!='job_id') for a in [*audits,*records])
    all_passed=all_passed and all(all(v for k,v in a.items() if k!='task_id') for a in paired)
    audit=dict(all_passed=all_passed,source_snapshot=snapshots,trials=audits,invocations=records,paired=paired,exceptions=exceptions,
        note='Auditable execution failures count as unsuccessful trials. Missing or corrupt evidence blocks inference.')
    atomic_json(root/'evidence_audit.json',audit)
    # Raw tables retain even incomplete/error outcomes; formal inference requires audit.
    export_csv(root/'results.csv',rows)
    if not all_passed:raise ValueError('Evidence audit failed; preserve the full ledger and inspect affected comparisons')
    scores=network_scores(rows);export_csv(root/'network_scores.csv',scores)
    groups=[]
    for space in ('G','GE'):
        for layer in ('research','witness'):
            for budget in suite['budgets']:
                for arm in suite['arms']:
                    subset=[r for r in rows if (r['space'],r['layer'],r['budget'],r['arm'])==(space,layer,budget,arm)]
                    if not subset:continue
                    groups.append(group_summary(rows,space=space,layer=layer,budget=budget,arm=arm))
    effects=[paired_effect(rows,'full',other,space=space,layer=layer,budget=budget)
        for space in ('G','GE') for layer in ('research','witness') for budget in suite['budgets'] for other in ('fixed','single','random')]
    primary=suite.get('primary_estimand',{})
    primary_effect=next((e for e in effects if all(e.get(k)==v for k,v in primary.items() if k!='unit')),None) if primary else None
    summary=dict(source_hash=suite['source_hash'],trials=len(rows),networks=len({r['network_id'] for r in rows}),
        primary_estimand=primary,primary_effect=primary_effect,execution_errors=len(exceptions),
        groups=groups,paired_effects=effects,search_ac_calls=sum(r['search_ac_calls'] for r in rows),
        total_ac_calls=sum(r['total_ac_calls'] for r in rows),
        scope='Paired network-cluster estimates; random seeds averaged, never best-of-seed. G/GE witness pools differ because GE includes additional device/mixed witnesses; compare methods within each space and separately inspect common-witness rows.')
    atomic_json(root/'summary.json',summary);export_csv(root/'group_summary.csv',groups);export_csv(root/'paired_effects.csv',effects)
    common=[r for r in rows if r['layer']=='witness' and r['task_id'].endswith('_witness_'+r['space'])]
    extra=[r for r in rows if r['layer']=='witness' and r not in common]
    export_csv(root/'common_witness_network_scores.csv',network_scores(common))
    export_csv(root/'extra_GE_witness_network_scores.csv',network_scores(extra))
    figures(root,rows,groups,effects)
    return summary


def figures(root,rows,groups,effects):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'pdf.fonttype':42,
        'axes.spines.top':False,'axes.spines.right':False})
    colors=dict(full='#237f79',fixed='#c08b53',single='#647c9a',random='#ac667b')
    fig,axes=plt.subplots(2,2,figsize=(10,7),layout='constrained',sharey=True)
    for i,layer in enumerate(('research','witness')):
        for j,space in enumerate(('G','GE')):
            ax=axes[i,j]
            for arm,color in colors.items():
                values=sorted([g for g in groups if (g['space'],g['layer'],g['arm'])==(space,layer,arm)],key=lambda g:g['budget'])
                ax.plot([g['budget'] for g in values],[100*g['network_mean_success'] for g in values],'o-',label=arm,color=color)
            ax.set(title=f'{space}: {layer} tasks',xlabel='Search AC request cap',ylabel='Mean verified success per network (%)',ylim=(-3,103))
            ax.grid(alpha=.18);ax.legend(frameon=False,fontsize=8)
    fig.savefig(root/'m96_success_budget.png',dpi=220);fig.savefig(root/'m96_success_budget.pdf');plt.close(fig)
    chosen=[e for e in effects if e['space']=='GE' and e['budget']==160]
    fig,ax=plt.subplots(figsize=(9,4.2),layout='constrained')
    for i,e in enumerate(chosen):
        ax.plot(np.array(e['ci95'])*100,[i,i],color=colors[e['right']],lw=2)
        ax.scatter(100*e['mean_success_difference'],i,color=colors[e['right']],s=45)
    ax.axvline(0,color='#98a7b9',ls=':');ax.set_yticks(range(len(chosen)),[f"{e['layer']}: full minus {e['right']}" for e in chosen])
    ax.set(xlabel='Paired success difference (percentage points), 95% network bootstrap',title='Geometry + equipment, 160 search AC requests')
    ax.grid(axis='x',alpha=.18)
    fig.savefig(root/'m96_paired_effects.png',dpi=220);fig.savefig(root/'m96_paired_effects.pdf');plt.close(fig)
    fig,axes=plt.subplots(1,2,figsize=(11,4.2),layout='constrained')
    for idx,space in enumerate(('G','GE')):
        for pos,(arm,color) in enumerate(colors.items()):
            values=[r['deficit'] for r in rows if (r['space'],r['arm'],r['budget'],r['layer'])==(space,arm,160,'research') and r.get('deficit') is not None]
            axes[idx].boxplot(values,positions=[pos],widths=.55,showfliers=True,patch_artist=True,boxprops=dict(facecolor=color,alpha=.55),medianprops=dict(color='#25313c'))
        axes[idx].set(xticks=range(4),xticklabels=list(colors),ylabel='Total target deficit (recorded values)',title=f'{space}: independent research intervals')
        axes[idx].grid(axis='y',alpha=.18)
    fig.savefig(root/'m96_deficits.png',dpi=220);fig.savefig(root/'m96_deficits.pdf');plt.close(fig)
    fig,axes=plt.subplots(2,2,figsize=(10,7),layout='constrained')
    panels=[('Total AC requests per trial',lambda r:r['total_ac_calls']),
        ('Changed conductor assignments',lambda r:r.get('changes',{}).get('changed_conductors')),
        ('Maximum node displacement (km)',lambda r:r.get('changes',{}).get('maximum_node_displacement_km')),
        ('Maximum absolute line-length ratio change',lambda r:max(abs(r['changes']['line_length_ratio_min']-1),abs(r['changes']['line_length_ratio_max']-1)) if 'changes' in r else None)]
    for ax,(label,measure) in zip(axes.flat,panels):
        labels=[]
        for pos,(arm,color) in enumerate(colors.items()):
            selected=[r for r in rows if (r['space'],r['layer'],r['budget'],r['arm'])==('GE','research',160,arm)]
            values=[measure(r) for r in selected if measure(r) is not None]
            ax.boxplot(values,positions=[pos],widths=.55,showfliers=True,patch_artist=True,boxprops=dict(facecolor=color,alpha=.55),medianprops=dict(color='#25313c'))
            labels.append(f'{arm}\n{len(values)}/{len(selected)}')
        ax.set(xticks=range(4),xticklabels=labels,ylabel=label);ax.grid(axis='y',alpha=.18)
    fig.suptitle('Geometry + equipment: work and modification at the 160-call cap\nObserved / attempted trials; failures remain in the ledger',fontsize=11)
    fig.savefig(root/'m96_work_modifications.png',dpi=220);fig.savefig(root/'m96_work_modifications.pdf');plt.close(fig)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--input',required=True)
    args=parser.parse_args();result=summarize(Path(args.input))
    print('Audited',result['trials'],'trials on',result['networks'],'networks; search AC',result['search_ac_calls'])
