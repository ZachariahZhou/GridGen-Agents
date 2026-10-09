"""Compare fixed-catalogue diagnosis with an explicitly expanded design space."""
import argparse
import csv
import json
from pathlib import Path

from feeder_agents.artifacts import atomic_json, file_digest
from feeder_agents.response_design import read_response_result


def summarize(previous, catalogue, expanded, output):
    previous, catalogue, expanded, output = map(Path,(previous,catalogue,expanded,output))
    output.mkdir(parents=True,exist_ok=True)
    previous_rows={r['case_id']:r for r in json.loads((previous/'results.json').read_text())['rows']}
    expanded_rows={r['case_id']:r for r in json.loads((expanded/'results.json').read_text())['rows']}
    rows=[]; provenance=[]
    for r in json.loads((catalogue/'results.json').read_text())['rows']:
        case=r['case_id'];e=expanded_rows[case];old=previous_rows[case]
        original=previous/'response_designs'/case
        fixed=catalogue/'response_designs'/case;extension=expanded/'response_designs'/case
        a,b=read_response_result(fixed),read_response_result(extension)
        hashes=[file_digest(p/'baseline/model/feeder.json') for p in (original,fixed,extension)]
        if len(set(hashes))!=1:raise ValueError('Baseline model changed: '+case)
        m=[json.loads((p/'manifest.json').read_text()) for p in (original,fixed,extension)]
        if not all(v['plan']['targets']==m[0]['plan']['targets'] for v in m):raise ValueError('Targets changed')
        row=dict(case_id=case,seed=r['seed'],previous_ratio=old['response_ratio'],
            catalogue_ratio=r['response_ratio'],expanded_ratio=e['response_ratio'],
            catalogue_steps=r['accepted_steps'],expanded_steps=e['accepted_steps'],
            catalogue_target_met=r['target_met'],expanded_target_met=e['target_met'],
            layout_scale=e['layout_scale'],layout_permission=e['layout_permission'],
            expanded_base_accepted=e['base_accepted'],expanded_verification_passed=e['verification_passed'],
            contract_preserved=e['protected_contract_preserved'],baseline_identical=True,
            original_baseline_sha256=hashes[0],diagnosis_code=r['diagnosis_code'])
        rows.append(row)
        provenance.append(dict(case_id=case,source_hashes=[v['source_hash'] for v in m],
            plan_hashes=[file_digest(p/'manifest.json') for p in (original,fixed,extension)],
            fixed_result_hash=a['result_hash'],expanded_result_hash=b['result_hash'],diagnosis=a['diagnosis']))
    atomic_json(output/'response_recovery.json',dict(
        protocol='Only previously unmet urban tasks. Catalogue-only permissions unchanged; expanded experiment explicitly allows bounded uniform spatial scaling. Original baselines byte-identical and targets unchanged.',
        interpretation='Different action permissions; do not report expanded success as recovery under the original fixed-geometry contract or as full-suite success.',rows=rows,provenance=provenance))
    with (output/'response_recovery.csv').open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'axes.spines.top':False,'axes.spines.right':False})
    fig,ax=plt.subplots(figsize=(9,4.8));x=np.arange(len(rows));width=.23
    series=[('previous_ratio','Single-line search (4 rounds)','#7c91ad'),
            ('catalogue_ratio','Coordinated catalogue search','#d2a456'),
            ('expanded_ratio','Explicit layout permission (+/-15%)','#277c73')]
    for i,(key,label,color) in enumerate(series):
        values=[r[key] for r in rows];xs=x+(i-1)*width
        ax.bar(xs,np.array(values)-1,bottom=1,width=width*.92,color=color,label=label,zorder=3)
        for xx,value in zip(xs,values):ax.text(xx,value+.0014,f'{value:.4f}',ha='center',va='bottom',fontsize=9)
    ax.axhline(1.05,color='#277c73',ls='--',lw=1.2)
    ax.axhspan(1.05,1.125,color='#277c73',alpha=.07)
    ax.text(.99,.97,'Requested interval: [1.05, 1.25]\nUpper part of interval omitted',transform=ax.transAxes,ha='right',va='top',fontsize=9,color='#346a61')
    ax.set(xticks=x,xticklabels=[f'Urban 25-bus / seed {r["seed"]}' for r in rows],
        ylim=(1,1.125),ylabel='Terminal dV/dP / original baseline',title='Diagnosing limited catalogue headroom')
    ax.grid(axis='y',alpha=.18,zorder=0);ax.legend(loc='upper left',fontsize=9,frameon=False)
    fig.text(.5,.025,'Same original grids and response targets; the green series permits coordinated coordinate/length changes.',ha='center',fontsize=9)
    fig.tight_layout(rect=(0,.055,1,1))
    fig.savefig(output/'response_recovery.png',dpi=240);fig.savefig(output/'response_recovery.pdf');plt.close(fig)
    return rows


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for name in ('previous','catalogue','expanded','output'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args();summarize(args.previous,args.catalogue,args.expanded,args.output)
