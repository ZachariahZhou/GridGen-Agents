"""Plot measured response-task controllability from the paired experiment CSV/JSON."""
import argparse
import json
from pathlib import Path


def plot(source,output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    rows=json.loads(Path(source).read_text())['rows'];output=Path(output);output.mkdir(parents=True,exist_ok=True)
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'axes.spines.top':False,'axes.spines.right':False})
    fig,axes=plt.subplots(1,3,figsize=(12.4,4.5));colors={'restricted':'#c47955','expanded':'#287c78'}
    labels={'restricted':'Restricted design space','expanded':'Expanded design space'}
    tasks=['large_change','selective','opposed'];titles=['Larger response change','Increase A, preserve B','Increase A, decrease B']
    for ax,task,title in zip(axes,tasks,titles):
        group=[r for r in rows if r['task']==task];seeds=sorted({r['seed'] for r in group})
        if task=='large_change':
            ax.axhspan(1.3,1.5,color=colors['expanded'],alpha=.09)
            ax.axhline(1.,ls='--',color='#919ba7',lw=.8)
            for policy in colors:
                selected=[r for r in group if r['policy']==policy]
                ax.plot([r['seed'] for r in selected],[r['ratios']['a'] for r in selected],
                    'o-' if policy=='expanded' else 's--',color=colors[policy],lw=1.4,label=labels[policy],ms=6)
            ax.set(xlabel='Seed',ylabel='Response A / original baseline',xticks=seeds,ylim=(.98,1.55))
        else:
            low,high=(.99,1.01) if task=='selective' else (.65,.85)
            ax.add_patch(Rectangle((low,1.15),high-low,.2,facecolor=colors['expanded'],alpha=.1,edgecolor=colors['expanded']))
            for row in group:
                color=colors[row['policy']];x,y=row['ratios']['b'],row['ratios']['a']
                ax.scatter(x,y,c=color,s=45,marker='o' if row['policy']=='expanded' else 's',zorder=3)
                offset=(5,3)
                if task=='selective' and row['policy']=='expanded':
                    offset=[(-27,-12),(17,0),(5,13)][seeds.index(row['seed'])%3]
                ax.annotate(str(row['seed']),(x,y),xytext=offset,textcoords='offset points',fontsize=8,color=color,
                    arrowprops=dict(arrowstyle='-',color=color,lw=.5) if offset!=(5,3) else None)
            ax.set(xlabel='Response B / original baseline',ylabel='Response A / original baseline',
                   xlim=(.965,1.035) if task=='selective' else (.59,1.05),ylim=(.98,1.42))
        passed=sum(r['target_met'] for r in group if r['policy']=='expanded')
        count=sum(r['policy']=='expanded' for r in group)
        ax.set_title(f'{title}\nExpanded: {passed}/{count} verified',fontsize=11)
        ax.grid(alpha=.16);ax.set_axisbelow(True)
    handles,labels_=axes[0].get_legend_handles_labels();fig.legend(handles,labels_,loc='upper center',ncol=2,frameon=False,bbox_to_anchor=(.5,1.02))
    fig.text(.5,.015,'Shaded regions: requested response intervals. Paired cases share original grids, targets and AC-preview budgets.',ha='center',fontsize=9)
    fig.tight_layout(rect=(0,.045,1,.95))
    fig.savefig(output/'response_flexibility.png',dpi=240,bbox_inches='tight');fig.savefig(output/'response_flexibility.pdf',bbox_inches='tight');plt.close(fig)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--input',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();plot(args.input,args.output)
