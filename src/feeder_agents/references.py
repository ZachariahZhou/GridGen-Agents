"""Source-preserving distribution reference cases, separate from synthetic geometry."""
from copy import deepcopy
import fcntl
import html
import json
import math
import multiprocessing
from pathlib import Path
import re
import zipfile

from .artifacts import atomic_json, digest, file_digest
from .rules import data


def reference_library():
    return data('matpower_references.json')


def list_references(voltage_kv=None, min_buses=1, max_buses=10000, evidence_class='any'):
    rows=[]
    for entry in reference_library()['cases']:
        stats=entry['statistics']
        levels=sorted({b[9] for b in entry['case']['bus']})
        n=len(entry['case']['bus'])
        if voltage_kv is not None and not any(math.isclose(voltage_kv,v,abs_tol=1e-6) for v in levels):
            continue
        if not min_buses<=n<=max_buses or evidence_class not in ('any',entry['provenance']['evidence_class']):
            continue
        rows.append({'case_id':entry['case_id'],'voltage_kv':levels,'bus_count':n,
            'provenance':{k:entry['provenance'][k] for k in ('evidence_class','origin_note','source_url','region')},
            'statistics':{k:v for k,v in stats.items() if k not in {'branches','positive_load_weights','offspring_counts','joint_features'}}})
    return rows


def get_reference(case_id):
    if not re.fullmatch(r'[A-Za-z0-9_]{1,64}',case_id):
        raise ValueError('Invalid reference case ID')
    for entry in reference_library()['cases']:
        if entry['case_id']==case_id:
            return entry
    raise ValueError(f'Unknown packaged reference case: {case_id}')


def scale_reference_load(case, load_scale):
    if not isinstance(load_scale,(int,float)) or not math.isfinite(load_scale) or not .05<=load_scale<=5:
        raise ValueError('load_scale must be finite and within 0.05–5')
    result=deepcopy(case)
    for bus in result['bus']:
        bus[2]*=load_scale
        bus[3]*=load_scale
    return result


def validate_reference_result(case,result):
    """Electrical consistency and case-supplied bounds; never certify unknown ratings."""
    active=[(i,b) for i,b in enumerate(case['branch'],1) if b[10]>0]
    voltages=result.get('bus_voltage_pu',{})
    currents=result.get('line_current_a',{})
    complete=all(len(voltages.get(f'b{int(b[0])}',[]))==3 for b in case['bus'])
    complete=complete and all(f'l{i}' in currents for i,_ in active)
    nums=[result.get(k) for k in ('source_kw','load_kw','generation_kw','loss_kw')]
    nums += [v for values in voltages.values() for v in values]+list(currents.values())
    finite=all(isinstance(v,(int,float)) and math.isfinite(v) for v in nums)
    valid=bool(result.get('converged')) and complete and finite
    expected=sum(b[2] for b in case['bus'])*1000
    tolerance=max(.1,abs(expected)*1e-4)
    balance=load_error=None
    if valid:
        balance=abs(result['source_kw']-result['load_kw']+result['generation_kw']-result['loss_kw'])
        load_error=abs(result['load_kw']-expected)
        valid=(balance<=tolerance and load_error<=tolerance and abs(result['generation_kw'])<=tolerance
               and all(v>0 for values in voltages.values() for v in values))
    voltage_violations=[]
    if valid:
        for bus in case['bus']:
            for phase,v in enumerate(voltages[f'b{int(bus[0])}'],1):
                if v<bus[12]-1e-6 or v>bus[11]+1e-6:
                    voltage_violations.append(dict(bus=int(bus[0]),phase=phase,voltage_pu=v,min_pu=bus[12],max_pu=bus[11]))
    missing_ratings=[];missing_flows=[];rating_violations=[]
    for i,b in active:
        if b[5]<=0:
            missing_ratings.append(i)
        else:
            powers=result.get('line_terminal_mva',{}).get(f'l{i}',[])
            if len(powers)!=2 or not all(isinstance(p,(int,float)) and math.isfinite(p) for p in powers):
                missing_flows.append(i)
            elif max(powers)>b[5]+1e-8:
                rating_violations.append(dict(branch=i,actual_mva=max(powers),rate_a_mva=b[5]))
    rating_status=('fail' if rating_violations else 'insufficient_data' if missing_ratings or missing_flows or not valid else 'pass')
    return dict(valid=bool(valid),voltage_status=('pass' if not voltage_violations else 'fail') if valid else 'insufficient_data',
        voltage_violations=voltage_violations, rating_status=rating_status,
        missing_rate_a_branches=missing_ratings,missing_flow_branches=missing_flows,rating_violations=rating_violations,
        power_balance_error_kw=balance,delivered_load_error_kw=load_error,
        complete_operational_verification=bool(valid and not voltage_violations and rating_status=='pass'),
        boundary='Only source-case voltage bounds and known branch MVA limits; no protection, geography or regulatory certification.')


def write_matpower_variant(case, path):
    """Write a directly loadable MATPOWER v2 power-flow case in canonical units."""
    lines=["function mpc = reference_variant", "% Derived research case; see source.m and manifest.json for provenance.",
           "mpc.version = '2';", f"mpc.baseMVA = {case['base_mva']:.16g};"]
    for table in ('bus','gen','branch'):
        lines.append(f'mpc.{table} = [')
        lines.extend('  '+' '.join(f'{v:.16g}' for v in row)+';' for row in case[table])
        lines.append('];')
    Path(path).write_text('\n'.join(lines)+'\n',encoding='utf-8')


def _solve_reference(case,directory):
    from .matpower import export_reference_dss
    from .simulation import simulate
    master=export_reference_dss(case,Path(directory))
    return simulate(master)


def _reference_process(case,directory,connection):
    try:
        connection.send({'result':_solve_reference(case,directory)})
    except Exception as exc:
        connection.send({'error':f'{type(exc).__name__}: {exc}'})
    finally:
        connection.close()


def solve_reference_isolated(case,directory,timeout=45):
    context=multiprocessing.get_context('spawn')
    receive,send=context.Pipe(duplex=False)
    process=context.Process(target=_reference_process,args=(case,directory,send))
    process.start();send.close()
    try:
        if not receive.poll(timeout):
            raise TimeoutError('Reference DSS calculation exceeded timeout')
        response=receive.recv()
        if 'error' in response:
            raise ValueError(response['error'])
        return response['result']
    finally:
        receive.close()
        process.join(timeout=2)
        if process.is_alive():
            process.terminate();process.join(timeout=2)
        if process.is_alive():
            process.kill();process.join(timeout=2)


def reference_visualization(case,result):
    import networkx as nx
    graph=nx.Graph()
    graph.add_nodes_from(int(b[0]) for b in case['bus'])
    graph.add_edges_from((int(b[0]),int(b[1])) for b in case['branch'] if b[10]>0)
    points=nx.spring_layout(graph,seed=42)
    svg=[]
    xy={n:(450+400*float(p[0]),350+300*float(p[1])) for n,p in points.items()}
    for i,b in enumerate(case['branch'],1):
        a,c=xy[int(b[0])],xy[int(b[1])]
        title=html.escape(f'branch {i}: {int(b[0])}–{int(b[1])}; r={b[2]:.6g}pu x={b[3]:.6g}pu; '+
            (f'RATE_A={b[5]}MVA' if b[5]>0 else 'RATE_A unspecified'))
        svg.append(f'<line x1="{a[0]}" y1="{a[1]}" x2="{c[0]}" y2="{c[1]}" stroke="{ "#6a859e" if b[10]>0 else "#8a719c"}" tabindex="0" stroke-dasharray="{ "none" if b[10]>0 else "5 5"}"><title>{title}</title></line>')
    for b in case['bus']:
        x,y=xy[int(b[0])];v=result.get('bus_voltage_pu',{}).get(f'b{int(b[0])}',[])
        color='#ba8545' if b[1]==3 else '#318579' if b[2]>0 else '#536a81'
        label=html.escape(f'bus {int(b[0])}; {b[9]}kV; P={b[2]*1000:.5g}kW Q={b[3]*1000:.5g}kvar; V={v}')
        svg.append(f'<circle cx="{x}" cy="{y}" r="5" fill="{color}" tabindex="0"><title>{label}</title></circle>')
    from .visual_theme import svg_page
    return svg_page(case['name']+' · Reference network','Topology schematic | Non-geographic coordinates', ''.join(svg),
        [('Source','#ba8545'),('Load','#318579'),('Junction','#536a81'),('Open branch (dashed)','#8a719c')],
        [('Buses',len(case['bus'])),('Branches',len(case['branch']))],notes='Spring-layout positions do not represent geography or line lengths. Hover or click to inspect parameters.',viewbox='0 0 900 700')


def run_reference_case(case_id,workspace,run_id,load_scale=1.0):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',run_id):
        raise ValueError('Invalid reference run ID')
    from .matpower import analyze_case
    from .workflow import _runtime_fingerprint
    entry=get_reference(case_id)
    case=scale_reference_load(entry['case'],load_scale)
    stats=analyze_case(case)
    if not stats['export_supported']:
        raise ValueError(f"Reference export unsupported: {stats['export_blockers']}")
    root=Path(workspace).resolve()/'reference_cases'/run_id
    root.mkdir(parents=True,exist_ok=True)
    manifest=dict(case_id=case_id,load_scale=load_scale,case_hash=digest(case),
                  provenance=entry['provenance'],runtime=_runtime_fingerprint())
    with (root/'.lock').open('w') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (root/'manifest.json').exists():
            if json.loads((root/'manifest.json').read_text())!=manifest:
                raise ValueError('Reference run ID already exists with a different configuration/version')
            if (root/'result.json').exists():
                old=json.loads((root/'result.json').read_text())
                if all((root/f).is_file() and file_digest(root/f)==h for f,h in old['artifacts'].items()):
                    return old
                raise ValueError('Reference artifacts changed; choose a new run ID')
        atomic_json(root/'manifest.json',manifest)
        atomic_json(root/'normalized_case.json',case)
        write_matpower_variant(case,root/'reference_variant.m')
        atomic_json(root/'reference_statistics.json',stats)
        (root/'source.m').write_text(entry['source_text'],encoding='utf-8')
        (root/'SOURCE_LICENSE.txt').write_text(reference_library()['license_text'],encoding='utf-8')
        # Spawn avoids native DSS calls in the UI thread and isolates each case.
        result=solve_reference_isolated(case,str(root/'opendss'))
        checks=validate_reference_result(case,result)
        atomic_json(root/'simulation.json',result)
        atomic_json(root/'validation.json',checks)
        (root/'visualization.html').write_text(reference_visualization(case,result),encoding='utf-8')
        report=(f"MATPOWER reference {case_id}: load multiplier {load_scale:g}; {len(case['bus'])} buses; "
            f"electrical consistency {'passed' if checks['valid'] else 'failed'}; original voltage bounds: {checks['voltage_status']}; "
            f"branch MVA ratings: {checks['rating_status']}. Source category: {entry['provenance']['evidence_class']}. "
            'Zero-load junctions, original voltages and impedances are retained. Coordinates show topology, not physical distances. Missing ratings cannot establish the absence of overloads.')
        (root/'report.md').write_text(report,encoding='utf-8')
        (root/'index.html').write_text('<!doctype html><meta charset="utf-8"><h1>MATPOWER reference feeder</h1><p>'+html.escape(report)+
            '</p><a href="visualization.html">Network schematic</a> · <a href="dataset.zip">Model archive</a> · <a href="validation.json">Validation evidence</a>',encoding='utf-8')
        with zipfile.ZipFile(root/'dataset.zip','w',zipfile.ZIP_DEFLATED) as archive:
            for p in sorted(root.rglob('*')):
                if p.is_file() and p.name not in {'.lock','dataset.zip','result.json'}:
                    archive.write(p,p.relative_to(root))
        output=dict(case_id=case_id,run_id=run_id,directory=str(root),load_scale=load_scale,
                    validation=checks,statistics=stats,verified_report=report,
                    artifacts={str(p.relative_to(root)):file_digest(p) for p in sorted(root.rglob('*'))
                        if p.is_file() and p.name not in {'.lock','result.json'}})
        atomic_json(root/'result.json',output)
        return output


def extract_joint_features(case):
    """Per-node/branch relationships; no inferred geometry or fitted population law."""
    import networkx as nx
    from .matpower import analyze_case
    summary=analyze_case(case)
    if not summary['radial'] or len(summary['reference_buses'])!=1:
        return {'status':'unsupported_topology','nodes':[],'branches':[]}
    source=summary['reference_buses'][0]
    bus={int(b[0]):b for b in case['bus']}
    graph=nx.Graph()
    graph.add_nodes_from(bus)
    edge_data={}
    for branch in summary['branches']:
        if branch['active']:
            a,b=branch['from_bus'],branch['to_bus']
            graph.add_edge(a,b)
            edge_data[frozenset((a,b))]=branch
    tree=nx.bfs_tree(graph,source)
    downstream={i:row[2]*1000 for i,row in bus.items()}
    for node in reversed(list(nx.topological_sort(tree))):
        for parent in tree.predecessors(node):
            downstream[parent]+=downstream[node]
    depth=nx.single_source_shortest_path_length(tree,source)
    return {'status':'extracted_not_fitted','nodes':[
        dict(bus_id=i,role='source' if i==source else 'load' if row[2]!=0 or row[3]!=0 else 'junction',
             depth_edges=depth[i],children=tree.out_degree(i),p_kw=row[2]*1000,q_kvar=row[3]*1000,
             downstream_kw=downstream[i]) for i,row in bus.items()],
        'branches':[dict(parent=a,child=b,child_depth_edges=depth[b],downstream_kw=downstream[b],
            r_ohm=edge_data[frozenset((a,b))]['r_ohm'],x_ohm=edge_data[frozenset((a,b))]['x_ohm'],
            rate_a_mva=edge_data[frozenset((a,b))]['rate_a_mva']) for a,b in tree.edges]}
