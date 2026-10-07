"""Read-only, non-geographical power-network drawings.

Layout positions are display coordinates only. Model coordinates, branch lengths,
electrical values, device identifiers and switch states are never rewritten.
"""
from collections import defaultdict
from functools import lru_cache
import html
import json
import math

import networkx as nx
import numpy as np


def network_graph(model, metadata=None):
    """Adapt feeder or MATPOWER records without collapsing parallel devices."""
    value = model.model_dump() if hasattr(model, 'model_dump') else model
    graph = nx.MultiGraph()
    if 'buses' in value:
        graph.graph['domain'] = 'distribution'
        graph.graph['hierarchical'] = bool(value.get('transformers'))
        loads = {load['bus']: load for load in value.get('loads', [])}
        for bus in value['buses']:
            name = bus['id']; load = loads.get(name, {})
            role = bus.get('role', 'customer' if load else 'junction')
            if name == value['source_bus']:
                role = 'source'
            graph.add_node(name, role=role, voltage_kv=bus.get('voltage_kv',value['voltage_kv']),
                generator=False, load_kw=load.get('kw',0), pv_kw=load.get('pv_kw',0), phases=bus.get('phases',[]),
                transformer_id=bus.get('transformer_id'),x_km=bus.get('x_km'),y_km=bus.get('y_km'))
        groups = [('line',value['lines']),('tie',value.get('tie_lines',[])),('transformer',value.get('transformers',[]))]
        for kind, devices in groups:
            for device in devices:
                a,b=device['bus1'],device['bus2']
                if a not in graph or b not in graph:
                    raise ValueError('Display edge references an unknown bus')
                graph.add_edge(a,b,key=f'{kind}:{device["id"]}',kind=kind,id=device['id'],
                    active=kind!='tie',length_km=device.get('length_km'),rating=device.get('kva'),
                    phases=device.get('phases'),conductor=device.get('conductor'))
    else:
        graph.graph['domain'] = 'transmission'
        metadata=metadata or {}; generators={int(g[0]) for g in value['gen'] if g[7]>0}
        for bus in value['bus']:
            name=int(bus[0]);role='source' if int(bus[1])==3 else 'generator' if name in generators else 'bus'
            graph.add_node(name,role=role,voltage_kv=float(bus[9]),generator=name in generators,
                           load_mw=float(bus[2]),pv_kw=0)
        evidence=metadata.get('branch_evidence',[])
        for i,edge in enumerate(value['branch']):
            a,b=int(edge[0]),int(edge[1]);record=evidence[i] if i<len(evidence) else {}
            if a not in graph or b not in graph:
                raise ValueError('Display edge references an unknown bus')
            kind=record.get('kind','transformer' if graph.nodes[a]['voltage_kv']!=graph.nodes[b]['voltage_kv'] else 'line')
            graph.add_edge(a,b,key=f'branch:{i}',id=f'branch_{i+1}',kind=kind,active=bool(edge[10]),
                           length_km=record.get('length_km'),rating=float(edge[5]),circuits=record.get('circuits',1))
    if not graph:
        raise ValueError('Cannot draw an empty network')
    return graph


def network_view(graph, scope='mv', transformer_id=None):
    """Read-only display projection; no electrical equivalent or export reduction."""
    if scope not in ('mv','full','lv'):
        raise ValueError('Unknown network display scope')
    if not graph.graph.get('hierarchical'):
        scope='full'
    transformers={a['id']:(u,v) for u,v,a in graph.edges(data=True) if a['kind']=='transformer'}
    if scope=='mv':
        result=graph.subgraph(n for n,a in graph.nodes(data=True) if a['voltage_kv']>1).copy()
        for identifier,(a,b) in transformers.items():
            primary=a if a in result else b
            customers=[attrs for _,attrs in graph.nodes(data=True)
                       if attrs.get('transformer_id')==identifier and attrs['role']=='customer']
            attributes=result.nodes[primary]
            for key,value in [('aggregated_users',len(customers)),
                              ('aggregated_kw',sum(c.get('load_kw',0) for c in customers)),
                              ('aggregated_pv_kw',sum(c.get('pv_kw',0) for c in customers))]:
                attributes[key]=attributes.get(key,0)+value
    elif scope=='lv':
        if transformer_id not in transformers:
            raise ValueError('Choose an existing transformer for the LV view')
        nodes={n for n,a in graph.nodes(data=True) if a.get('transformer_id')==transformer_id}
        nodes.update(transformers[transformer_id])
        result=graph.subgraph(nodes).copy()
    else:
        result=graph.copy()
    result.graph.update(scope=scope,transformer_id=transformer_id if scope=='lv' else None,
        full_node_count=len(graph),full_edge_count=graph.number_of_edges())
    return result


def _layout_score(graph, positions):
    """Prefer fewer crossings, then fewer nearly coincident nodes."""
    edges=list(graph.edges()); crossings=0
    def orient(a,b,c):return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    for i,(a,b) in enumerate(edges):
        for c,d in edges[i+1:]:
            if len({a,b,c,d})<4:continue
            pa,pb,pc,pd=(positions[n] for n in (a,b,c,d))
            if orient(pa,pb,pc)*orient(pa,pb,pd)<-1e-12 and orient(pc,pd,pa)*orient(pc,pd,pb)<-1e-12:
                crossings+=1
    values=list(positions.values())
    close=sum(math.dist(a,b)<.045 for i,a in enumerate(values) for b in values[i+1:])
    return crossings,close


@lru_cache(maxsize=64)
def _cached_layout(nodes, edges, seed):
    graph=nx.Graph();graph.add_nodes_from(nodes);graph.add_weighted_edges_from(edges,weight='display_length')
    for a,b,data in graph.edges(data=True):data['attraction']=1/data['display_length']
    if len(nodes)==1:return ((nodes[0],(0.,0.)),)
    # Synthetic drawing distances reserve space for the MV backbone. They are
    # unrelated to line length, impedance or the original geographic coordinates.
    try:
        spring=nx.spring_layout(graph,seed=seed,iterations=120,weight='attraction')
    except ImportError:
        # NetworkX's large-graph spring path needs optional SciPy. Keep the
        # core distribution viewer usable with a NumPy-only layout instead.
        spring=nx.circular_layout(graph)
    candidates=[spring]
    if len(nodes)<=250 and nx.is_connected(graph):
        try:
            candidates.append(nx.kamada_kawai_layout(graph,pos=spring,weight='display_length'))
        except ImportError:
            # Kamada–Kawai also needs SciPy; the spring candidate remains valid.
            pass
    best=min(candidates,key=lambda p:_layout_score(graph,p)) if len(nodes)<=250 else spring
    return tuple((n,(float(best[n][0]),float(best[n][1]))) for n in nodes)


def schematic_layout(graph, seed=42):
    nodes=tuple(sorted(graph,key=lambda n:(type(n).__name__,str(n))))
    order={n:i for i,n in enumerate(nodes)}
    pairs={tuple(sorted((a,b),key=order.get)) for a,b in graph.edges()}
    edges=tuple((a,b,3. if graph.graph.get('domain')=='distribution' and all(graph.nodes[n]['voltage_kv']>=1 for n in (a,b)) else 1.)
                for a,b in sorted(pairs,key=lambda e:(order[e[0]],order[e[1]])))
    return dict(_cached_layout(nodes,edges,seed))


def node_style(attributes):
    role=attributes['role'];kv=attributes['voltage_kv']
    if role=='source':return 's','#24364b',48
    if role=='generator':return '^','#b37638',38
    if role=='mv_tap':return 'D','#395d85',29
    if role=='mv_junction':return 'o','#8596a8',15
    if role=='lv_bus':return 'D','#9b6080',23
    if role=='customer':return 'o','#58a896',12
    if role=='lv_branch':return 'o','#278272',22
    color='#395d85' if kv<100 else '#3b95a0' if kv<220 else '#395d85' if kv<500 else '#8574aa'
    return 'o',color,25


def edge_style(attributes):
    if attributes['kind']=='tie' or not attributes['active']:return '#c58a31','dashed',1.0
    if attributes['kind']=='transformer':return '#9b6080','dashed',1.3
    return '#8596a8','solid',.9


def display_edges(graph):
    """Keep parallel edges visible as separate symmetric arcs."""
    groups=defaultdict(list)
    for a,b,key,attributes in graph.edges(keys=True,data=True):
        groups[frozenset((a,b))].append((a,b,key,attributes))
    for members in groups.values():
        for i,(a,b,key,attributes) in enumerate(members):
            yield a,b,key,attributes,(i-(len(members)-1)/2)*.22


def schematic_html(graph, *, positions=None, accepted=None, geographic=False):
    from .visual_theme import fit_coordinates,svg_page
    positions=schematic_layout(graph) if positions is None else positions
    xy=fit_coordinates(positions);body=[]
    for a,b,key,attributes,curve in display_edges(graph):
        x,y=xy[a];xx,yy=xy[b];color,dash,width=edge_style(attributes)
        label=html.escape(f'{attributes["id"]} | {a} – {b} | '+json.dumps(attributes,ensure_ascii=False))
        cx,cy=(x+xx)/2-curve*(yy-y),(y+yy)/2+curve*(xx-x)
        body.append(f'<path d="M {x} {y} Q {cx} {cy} {xx} {yy}" fill="none" stroke="{color}" stroke-width="{width*1.5}" stroke-dasharray="{"6 4" if dash=="dashed" else "none"}" tabindex="0"><title>{label}</title></path>')
        if attributes['kind']=='tie':
            mx,my=(x+xx)/2,(y+yy)/2
            body.append(f'<circle cx="{mx}" cy="{my}" r="4" fill="white" stroke="{color}"/><text x="{mx+6}" y="{my-5}" font-size="9" fill="{color}">NO</text>')
    for node,attributes in graph.nodes(data=True):
        x,y=xy[node];marker,color,size=node_style(attributes);r=math.sqrt(size)*.8
        label=html.escape(str(node)+' | '+json.dumps(attributes,ensure_ascii=False))
        border='#c58a31' if attributes.get('pv_kw',0)>0 else 'white'
        if marker=='s':shape=f'<rect x="{x-r}" y="{y-r}" width="{2*r}" height="{2*r}"';end='rect'
        elif marker=='D':shape=f'<path d="M {x} {y-r} L {x+r} {y} L {x} {y+r} L {x-r} {y} Z"';end='path'
        elif marker=='^':shape=f'<path d="M {x} {y-r} L {x+r} {y+r} L {x-r} {y+r} Z"';end='path'
        else:shape=f'<circle cx="{x}" cy="{y}" r="{r}"';end='circle'
        body.append(shape+f' fill="{color}" stroke="{border}" stroke-width="1" tabindex="0"><title>{label}</title></{end}>')
    kv='/'.join(f'{v:g}' for v in sorted({n['voltage_kv'] for _,n in graph.nodes(data=True)},reverse=True))
    scope=graph.graph.get('scope','full')
    title='中压馈线骨架' if scope=='mv' else f'低压台区 {graph.graph["transformer_id"]}' if scope=='lv' else '电力网络拓扑示意图'
    layout='合成空间坐标' if geographic else '非地理布局'
    detail='低压用户按配变汇总，点击接点查看用户数和功率' if scope=='mv' else '含该配变及其用户' if scope=='lv' else '全部母线可见'
    return svg_page(title,f'{kv} kV · {layout} · {detail}',''.join(body),
        [('电源 / 平衡母线（方形）','#24364b'),('MV配变接点（菱形）/ 母线','#395d85'),('用户','#58a896'),('配变低压侧','#9b6080'),('常开联络 NO / PV用户边框','#c58a31')],
        [('显示母线',len(graph)),('全模型母线',graph.graph.get('full_node_count',len(graph))),
         ('显示支路',graph.number_of_edges()),('常开联络',sum(e['kind']=='tie' for *_,e in graph.edges(data=True))),('全模型验收','通过' if accepted is True else '未通过' if accepted is False else '见验证记录')],
        notes=('沿用模型合成空间坐标。' if geographic else 'NetworkX自动拓扑布局（弹簧/Kamada–Kawai，可选依赖缺失时回退），显示距离无物理单位。')+
        '紫色虚线为变压器，橙色虚线为常开/断开支路。交叉不代表连接。视图筛选不生成等值电气模型；导出仍保留全网络。PV为节点属性。')


def draw_schematic(ax, graph, positions=None):
    """Matplotlib drawing shared by the exportable representative gallery."""
    from matplotlib.patches import FancyArrowPatch
    positions=schematic_layout(graph) if positions is None else positions
    for a,b,key,attributes,curve in display_edges(graph):
        x,y=positions[a];xx,yy=positions[b];color,dash,width=edge_style(attributes)
        if curve:
            ax.add_patch(FancyArrowPatch((x,y),(xx,yy),arrowstyle='-',connectionstyle=f'arc3,rad={curve}',
                                        color=color,lw=width,linestyle=dash,zorder=1))
        else:ax.plot([x,xx],[y,yy],color=color,lw=width,ls='--' if dash=='dashed' else '-',zorder=1)
        if attributes['kind']=='tie':ax.plot((x+xx)/2,(y+yy)/2,'o',ms=3.7,mfc='white',mec=color,mew=.9,zorder=2)
    # Group scatter calls to keep larger exported figures reasonably compact.
    groups=defaultdict(list)
    for node,attributes in graph.nodes(data=True):
        groups[(*node_style(attributes),attributes.get('pv_kw',0)>0)].append(node)
    for (marker,color,size,pv),nodes in groups.items():
        ax.scatter([positions[n][0] for n in nodes],[positions[n][1] for n in nodes],s=size,marker=marker,
                   c=color,edgecolors='#c58a31' if pv else 'white',linewidths=.55,zorder=3)
    values=np.asarray(list(positions.values()));low,high=values.min(axis=0),values.max(axis=0)
    center=(low+high)/2;radius=max(float((high-low).max())*.61,.1)
    ax.set_xlim(center[0]-radius,center[0]+radius);ax.set_ylim(center[1]-radius,center[1]+radius)
    ax.set_aspect('equal',adjustable='box');ax.axis('off')
    return positions
