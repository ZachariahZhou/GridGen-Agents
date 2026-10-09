"""Reproducible pilot; record every attempted case, including unmet targets."""
import argparse
import copy
import csv
import json
from pathlib import Path
import time

import yaml

from feeder_agents.artifacts import atomic_json
from feeder_agents.response_design import run_response_design


def run(output, failed_from=None, layout_scale_change=None):
    repo=Path(__file__).resolve().parents[1]
    rural=yaml.safe_load((repo/'examples/response_rural_37.yaml').read_text())
    transmission=yaml.safe_load((repo/'examples/response_transmission_139.yaml').read_text())
    plans=[]
    for scene in ('rural','urban'):
        for seed in (41,42,43):
            for direction,lo,hi in [('increase',1.05,1.25),('decrease',.75,.95)]:
                p=copy.deepcopy(rural);p['base_spec']['seed']=seed
                if scene=='urban':
                    p['base_spec'].update(n_buses=25,total_kw_min=360,total_kw_max=360,
                        scenario=dict(kind='urban',engineering_profile='urban',layout='spatial_mst'))
                p['targets'][0].update(lower=lo,upper=hi)
                p['research_question']=f'{scene} {direction}: condition terminal voltage sensitivity while preserving topology, demand and phases.'
                plans.append((f'{scene}_{p["base_spec"]["n_buses"]}_{seed}_{direction}',p))
    for buses in (37,139):
        for seed in (41,42,43):
            p=copy.deepcopy(transmission);p['base_spec'].update(n_buses=buses,seed=seed)
            if buses==37:
                p['base_spec'].update(n_generators=6,total_mw=480)
                p['probes'][0].update(injection_buses=['10'],withdrawal_buses=['30'],step_mw=.1)
            plans.append((f'transmission_{buses}_{seed}_decrease',p))
    if failed_from is not None:
        source=Path(failed_from).resolve()
        failed={r['case_id'] for r in json.loads((source/'results.json').read_text())['rows'] if not r['target_met']}
        plans=[(p['id'],p['plan']) for p in json.loads((source/'suite.json').read_text())['plans'] if p['id'] in failed]
    if layout_scale_change is not None:
        if failed_from is None or not 0 < layout_scale_change <= .25:
            raise ValueError('Explicit layout extension requires --retry-failed-from and a scale bound in (0, 0.25]')
        from feeder_agents.response_schema import ResponseDesignPlan
        for _, plan in plans:
            plan['search']['allowed_actions']=list(dict.fromkeys(plan['search']['allowed_actions']+['scale_layout']))
            plan['search']['max_layout_scale_change']=layout_scale_change
            plan.setdefault('assumptions',[]).append(f'Expanded experiment: uniform spatial scaling within {layout_scale_change:.0%} of original; original response target retained. Different action permissions from the conductor-only experiment.')
            ResponseDesignPlan.model_validate(plan)
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    atomic_json(output/'suite.json',dict(purpose='Pilot response controllability and research-contract preservation',
        protocol='Three independent seeds; original base specification fixed within each run; central AC differences; separate half-step verification.',
        interpretation='Pilot only; no population realism or universal success claim.',
        retry_failed_from=str(Path(failed_from).resolve()) if failed_from else None,
        permission_extension=dict(action='scale_layout',max_layout_scale_change=layout_scale_change) if layout_scale_change is not None else None,
        plans=[dict(id=i,plan=p) for i,p in plans]))
    rows=[]
    for name,plan in plans:
        start=time.perf_counter();kind=plan['base_spec']['network_kind'];probe=plan['probes'][0]['id']
        row=dict(case_id=name,domain=kind,n_buses=plan['base_spec']['n_buses'],seed=plan['base_spec']['seed'],
            lower=plan['targets'][0]['lower'],upper=plan['targets'][0]['upper'])
        try:
            r=run_response_design(plan,output,name)
            before=r['baseline']['probes'][probe]['value'];after=r['selected']['probes'][probe]['value']
            row.update(target_met=r['target_met'],base_accepted=r['selected']['base_accepted'],
                verification_passed=r['verification_passed'],protected_contract_preserved=r['protected_contract_preserved'],
                initial_response=before,final_response=after,response_ratio=after/before if before else None,
                unit=r['selected']['probes'][probe]['unit'],accepted_steps=r['accepted_steps'],
                candidates=r['evaluated_candidates'],stop_reason=r['stop_reason'],directory=r['directory'])
            row['diagnosis_code']=r['diagnosis']['code']
            row['layout_permission']=plan['search'].get('max_layout_scale_change')
            if kind=='distribution':
                selected=json.loads((Path(r['directory'])/'selected/feeder.json').read_text())
                row['layout_scale']=selected['design_evidence'].get('response_layout_scaling',{}).get('original_scale',1.)
        except Exception as exc:
            row.update(target_met=False,error=f'{type(exc).__name__}: {exc}')
        row['seconds']=round(time.perf_counter()-start,3);rows.append(row)
        atomic_json(output/'results.json',dict(rows=rows))
        print(name,row.get('target_met'),row.get('response_ratio'),row.get('stop_reason',row.get('error')),flush=True)
    fields=list(dict.fromkeys(key for row in rows for key in row))
    with (output/'results.csv').open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader();writer.writerows(rows)
    summarize(output,rows)
    if failed_from:
        report=output/'report.md'
        report.write_text('This file records only retests of previously unmet tasks. Earlier passing cases were not rerun; the combined records are not a new complete independent comparison.\n\n'+report.read_text(),encoding='utf-8')
    if layout_scale_change is not None:
        report=output/'report.md'
        report.write_text(f'This batch explicitly expands edit permissions: uniform geometry scaling within +/-{layout_scale_change:.0%} of the original scale. Buses, topology, demand, phases and original response targets remain fixed; coordinates and lengths scale together. This changes the design space and must not be reported as complete recovery under the original conductor-only contract.\n\n'+report.read_text(),encoding='utf-8')
    return rows


def summarize(output,rows):
    if not rows:
        (output/'report.md').write_text('No unmet tasks remain for this retest.',encoding='utf-8')
        return
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'axes.spines.top':False,'axes.spines.right':False})
    groups=[('Distribution: increase',[r for r in rows if r['domain']=='distribution' and r['lower']>1]),
            ('Distribution: decrease',[r for r in rows if r['domain']=='distribution' and r['upper']<1]),
            ('Transmission: decrease',[r for r in rows if r['domain']=='transmission'])]
    groups=[group for group in groups if group[1]]
    fig,axes=plt.subplots(1,len(groups),figsize=(4.4*len(groups),4.5),constrained_layout=True)
    if len(groups)==1:axes=[axes]
    for ax,(title,group) in zip(axes,groups):
        if group:ax.axhspan(group[0]['lower'],group[0]['upper'],color='#cfe5dd',alpha=.6,label='Requested interval')
        ax.axhline(1,color='#697586',ls='--',lw=1,label='Original baseline')
        for i,row in enumerate(group):
            value=row.get('response_ratio')
            if value is None:continue
            color='#267d74' if row['target_met'] else '#bd574c'
            ax.plot([i,i],[1,value],color=color,lw=2)
            ax.scatter(i,value,c=color,s=45,zorder=3,marker='o' if row['target_met'] else 'x')
        ax.set(title=title,ylabel='Response / original baseline',xticks=range(len(group)),
            xticklabels=[r['case_id'].replace('_increase','').replace('_decrease','').replace('transmission','TX') for r in group])
        ax.tick_params(axis='x',rotation=55);ax.grid(axis='y',alpha=.2)
    axes[0].legend(loc='best',fontsize=8)
    fig.savefig(output/'response_controllability.png',dpi=220);fig.savefig(output/'response_controllability.pdf');plt.close(fig)
    success=sum(r['target_met'] for r in rows);feasible=sum(r.get('base_accepted',False) for r in rows)
    lines=['# Electrical-response-conditioned synthesis: experiment record','',
        '## Motivation','Test whether the existing generator can change targeted electrical responses through legal equipment edits while preserving buses, topology, demand and phases.','',
        '## Procedure','Urban 25-bus and rural 37-bus unbalanced feeders target 5%-25% increases or decreases in terminal active-power voltage sensitivity. The 37/139-bus transmission models target 2%-50% reductions in peak branch transfer response. Every group uses seeds 41, 42 and 43; every attempt is included.',
        'Each round runs full base electrical checks and central-difference probes before accepting improvements that worsen no individual target. Independent verification uses half the original perturbation step and does not guide search. This checks local numerical stability, not generalization across operating ranges.',
        'This batch uses deterministic candidate selection without LLM calls. Language compilation and LLM candidate selection require separate checks.','',
        '## Results',f'Targets and independent verification passed: {success}/{len(rows)}; selected-model base electrical checks passed: {feasible}/{len(rows)}.',
        '|Case|Response ratio|Target met|Accepted edits|Stop reason|','|---|---:|---|---:|---|']
    for row in rows:
        ratio=f'{row["response_ratio"]:.6f}' if row.get('response_ratio') is not None else 'N/A'
        lines.append(f'|{row["case_id"]}|{ratio}|{row["target_met"]}|{row.get("accepted_steps",0)}|{row.get("stop_reason",row.get("error"))}|')
    lines.extend(['','## Data and interpretation','results.csv supports tables and figures; suite.json preserves every input. Run directories retain raw power flows, probe ports, candidate histories, catalogue selections, independent verification and final OpenDSS/MATPOWER models.',
        'This is a functional pilot. Target intervals are research choices, not realism classes calibrated from actual grids; three seeds do not establish a population success rate. Unmet targets mean that the available catalogue and bounded search found no suitable solution, not that one is physically impossible.',
        '![Response controllability](response_controllability.png)'])
    (output/'report.md').write_text('\n\n'.join(lines),encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--retry-failed-from',type=Path)
    parser.add_argument('--layout-scale-change',type=float,help='Explicit alternate experiment permission, relative to original geometry; requires failed-case replay.')
    args=parser.parse_args();run(args.output,args.retry_failed_from,args.layout_scale_change)
