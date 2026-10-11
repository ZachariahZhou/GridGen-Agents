"""Complete ledgers and network-cluster comparisons for response experiments."""
from collections import defaultdict
import numpy as np


def validate_rows(jobs,rows):
    expected={j['job_id'] for j in jobs};actual=[r['job_id'] for r in rows]
    if len(actual)!=len(set(actual)) or set(actual)!=expected:
        raise ValueError('Duplicate, unexpected or missing trial rows')
    for row in rows:
        if any(not isinstance(row.get(k),int) or row[k]<0 for k in ('search_ac_calls','total_ac_calls')):
            raise ValueError('Missing actual solver accounting for '+row['job_id'])
        if row['search_ac_calls']>row['budget'] or row['total_ac_calls']<row['search_ac_calls']:
            raise ValueError('Invalid trial work totals')


def network_scores(rows):
    groups=defaultdict(list)
    for r in rows:groups[(r['network_id'],r['arm'],r['space'],r['layer'],r['budget'])].append(r)
    scores=[]
    for key,group in sorted(groups.items()):
        tasks=defaultdict(list)
        for row in group:tasks[row['task_id']].append(row)
        rate=float(np.mean([np.mean([r['success'] for r in values]) for values in tasks.values()]))
        scores.append(dict(zip(('network_id','arm','space','layer','budget'),key),success_rate=rate,
            tasks=len(tasks),trials=len(group),search_ac_calls_sum=sum(r['search_ac_calls'] for r in group),
            total_ac_calls_sum=sum(r['total_ac_calls'] for r in group)))
    return scores


def group_summary(rows,*,space,layer,budget,arm):
    subset=[r for r in rows if (r['space'],r['layer'],r['budget'],r['arm'])==(space,layer,budget,arm)]
    points=[r['success_rate'] for r in network_scores(subset)]
    deficits=[r['deficit'] for r in subset if r.get('deficit') is not None]
    return dict(space=space,layer=layer,budget=budget,arm=arm,trials=len(subset),networks=len(points),
        accepted_trials=sum(r['success'] for r in subset),network_mean_success=float(np.mean(points)) if points else None,
        mean_deficit_observed=float(np.mean(deficits)) if deficits else None,deficit_observations=len(deficits),
        deficit_unavailable=len(subset)-len(deficits),execution_errors=sum(r['outcome'] in ('execution_error','worker_error') for r in subset),
        search_ac_calls=sum(r['search_ac_calls'] for r in subset),total_ac_calls=sum(r['total_ac_calls'] for r in subset))


def paired_effect(rows,left,right,*,space,layer,budget):
    selected=[r for r in rows if r['space']==space and r['layer']==layer and r['budget']==budget and r['arm'] in (left,right)]
    scores=network_scores(selected)
    a={r['network_id']:r['success_rate'] for r in scores if r['arm']==left}
    b={r['network_id']:r['success_rate'] for r in scores if r['arm']==right}
    if set(a)!=set(b) or not a:raise ValueError('Incomplete paired network groups')
    differences=np.array([a[k]-b[k] for k in sorted(a)])
    rng=np.random.default_rng(96261103)
    draws=rng.choice(differences,size=(10000,len(differences)),replace=True).mean(axis=1)
    interval=np.quantile(draws,[.025,.975]).tolist()
    return dict(left=left,right=right,space=space,layer=layer,budget=budget,networks=len(a),
        mean_success_difference=float(differences.mean()),ci95=interval,bootstrap_networks=len(a),
        bootstrap_draws=10000,bootstrap_seed=96261103,network_differences=dict(zip(sorted(a),differences.tolist())))
