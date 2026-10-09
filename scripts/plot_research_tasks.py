"""Plot recorded task targets separately from measured finite-intervention effects."""
import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import numpy as np


def plot(source,output):
    rows=json.loads(Path(source).read_text())['rows']
    if any('selected_task_metrics' not in row for row in rows):
        raise ValueError('Incomplete attempts must be reported before plotting paired measurements.')
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'axes.titlesize':12,
        'axes.labelsize':10,'axes.spines.top':False,'axes.spines.right':False,
        'pdf.fonttype':42,'savefig.dpi':230})
    fig,axes=plt.subplots(2,3,figsize=(12,6.3),layout='constrained')
    tasks=[('voltage_control','Voltage-control test grid','#2365A1',1000,'Max. voltage change (mpu)'),
        ('static_pv_impact','Static PV-impact test grid','#D27728',1000,'Max. voltage change (mpu)'),
        ('transmission_transfer','Transmission-transfer test grid','#208675',1,'Max. branch-flow change (MW)')]
    for col,(task,title,color,factor,label) in enumerate(tasks):
        subset=sorted((r for r in rows if r['task']==task),key=lambda r:r['seed'])
        x=np.arange(len(subset))
        if not subset:raise ValueError(f'No records for {task}')
        bounds={(r['response_lower'],r['response_upper']) for r in subset}
        if len(bounds)!=1:raise ValueError('Use a separate panel for each target protocol.')
        low,high=bounds.pop();top=axes[0,col];bottom=axes[1,col]
        top.axhspan(low,high,color=color,alpha=.1)
        top.axhline(1,color='#A1A8B1',ls='--',lw=.8)
        for k,row in enumerate(subset):
            top.plot([k,k],[1,row['response_ratio']],color=color,lw=1.8,zorder=2)
            top.scatter(k,1,s=45,facecolor='white',edgecolor='#788491',zorder=3)
            top.scatter(k,row['response_ratio'],s=42,color=color,zorder=3)
            top.annotate(f"{row['response_ratio']:.3f}",(k,row['response_ratio']),xytext=(0,9),
                textcoords='offset points',ha='center',fontsize=9,color=color)
        count=sum(bool(r['target_met'] and r['verification_passed'] and r['task_experiment_passed']) for r in subset)
        top.set_title(f'{title}\n{subset[0]["n_buses"]} buses | {count}/{len(subset)} complete tasks',loc='left',pad=12)
        top.text(.03,.04,f'Target band: [{low:.2f}, {high:.2f}]',transform=top.transAxes,fontsize=9,color=color)
        top.set_ylim(.43,1.43);top.set_ylabel('Response / original response')
        metric='max_branch_flow_change_mw' if task=='transmission_transfer' else 'max_voltage_change_pu'
        before=[r['original_task_metrics'][metric]*factor for r in subset]
        after=[r['selected_task_metrics'][metric]*factor for r in subset]
        bottom.bar(x-.17,before,.3,color='#D2D9E0',edgecolor='#8E99A4',lw=.5)
        bottom.bar(x+.17,after,.3,color=color)
        bottom.set_ylabel(label);bottom.set_ylim(0,max(before+after)*1.28)
        if task=='voltage_control':protocol=f"External Q support: {subset[0]['selected_task_metrics']['support_mvar']*1000:g} kvar"
        elif task=='static_pv_impact':protocol=f"PV off / on: {subset[0]['selected_task_metrics']['pv_on_kw']:.0f} kW"
        else:protocol=f"Balanced transfer: {subset[0]['selected_task_metrics']['transfer_mw']:g} MW; fixed Q"
        bottom.set_title(protocol,loc='left',fontsize=10,pad=9)
        for ax in (top,bottom):
            ax.set_xticks(x,[str(r['seed']) for r in subset]);ax.set_xlabel('Seed')
            ax.set_xlim(-.55,len(subset)-.45);ax.grid(axis='y',color='#E9EDF1',lw=.6);ax.set_axisbelow(True)
    fig.legend(handles=[Line2D([],[],marker='o',ls='',markerfacecolor='white',markeredgecolor='#788491',label='Original grid'),
        Line2D([],[],marker='o',ls='',color='#2365A1',label='Selected grid (task color)')],
        loc='outside lower center',ncol=2,frameon=False)
    fig.suptitle('Task-conditioned grid synthesis: response targets and finite AC experiments',fontsize=14,y=1.03)
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    for ext in ('png','pdf'):fig.savefig(output.with_suffix('.'+ext),bbox_inches='tight',facecolor='white')
    plt.close(fig)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--results',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();plot(args.results,args.output)
