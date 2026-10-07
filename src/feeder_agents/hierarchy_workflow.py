"""Versioned artifacts for MV/LV generation; old single-voltage checks are not reused."""
import fcntl
import hashlib
import html
import json
import multiprocessing
from pathlib import Path
import re
import zipfile

from .artifacts import atomic_json, digest, file_digest
from .hierarchy import HierarchicalSpec, generate_hierarchy, export_hierarchy, evaluate_hierarchy


def read_hierarchy_result(root):
    """Check saved artifacts and recompute acceptance, without another power flow."""
    from .hierarchy import HierarchicalFeeder
    root=Path(root).resolve()
    result=json.loads((root/'result.json').read_text())
    if digest({k:v for k,v in result.items() if k!='result_hash'})!=result.get('result_hash'):
        raise ValueError('Hierarchy result checksum changed')
    for name,expected in result['artifacts'].items():
        path=(root/name).resolve()
        if not path.is_relative_to(root) or not path.is_file() or file_digest(path)!=expected:
            raise ValueError('Hierarchy artifact missing or changed: '+name)
    from .hierarchy import historical_hierarchy_spec
    spec=historical_hierarchy_spec(json.loads((root/'manifest.json').read_text())['spec'])
    for sample in result['samples']:
        if not re.fullmatch(r'sample_[0-9]{5}',sample['sample_id']):raise ValueError('Invalid saved sample ID')
        if sample.get('error'):continue
        folder=root/sample['sample_id']
        feeder=HierarchicalFeeder.model_validate(json.loads((folder/'feeder.json').read_text()))
        simulation=json.loads((folder/'simulation.json').read_text())
        checked=evaluate_hierarchy(feeder,spec,simulation)
        if 'feedback' in sample:
            from .hierarchy_feedback import verify_saved_feedback
            verify_saved_feedback(folder/'feedback',feeder,spec)
        if checked['accepted']!=sample['accepted']:raise ValueError('Saved acceptance differs from current verification')
    if len(result['samples'])!=spec.count or result['accepted']!=sum(s['accepted'] for s in result['samples']):
        raise ValueError('Inconsistent hierarchy result counts')
    return result


def visualization(feeder,result,checks):
    from .visual_theme import fit_coordinates,svg_page
    xy=fit_coordinates({b.id:(b.x_km,b.y_km) for b in feeder.buses})
    buses={b.id:b for b in feeder.buses};svg=[]
    for edge in [*feeder.lines,*feeder.transformers]:
        x,y=xy[edge.bus1];xx,yy=xy[edge.bus2]
        if hasattr(edge,'kva'):
            measured=result.get('transformer_metrics',{}).get(edge.id,{})
            color='#ba8545';width=7
            label=f'{edge.id}: {edge.primary_kv}/{edge.secondary_kv}kV, {edge.kva}kVA, delta/wye lag; loading={measured.get("max_phase_loading_ratio")}'
            # Co-located windings: transformer symbol stays visible at map scale.
            svg.append(f'<rect x="{x-5}" y="{y-5}" width="10" height="10" fill="{color}" tabindex="0"><title>{html.escape(label)}</title></rect>')
        else:
            current=result.get('line_current_a',{}).get(edge.id)
            equipment=feeder.equipment_catalog[edge.conductor]
            rating=equipment['normamps']
            color='#b64b4b' if current is not None and current>rating else '#356a93' if buses[edge.bus1].voltage_kv>1 else '#318579'
            width=3 if buses[edge.bus1].voltage_kv>1 else 1.5
            label=f'{edge.id}: region={buses[edge.bus1].region_id}, {buses[edge.bus1].voltage_kv}kV, phases={edge.phases}, {edge.length_km:.5f}km, I={current}, rating={rating}A, product={edge.conductor}, installation={equipment.get("installation",edge.construction)}, R-temperature={equipment.get("resistance_temperature_c","source")}C'
        dash='6 3' if getattr(edge,'construction',None)=='cable' else 'none'
        svg.append(f'<line stroke-dasharray="{dash}" x1="{x}" y1="{y}" x2="{xx}" y2="{yy}" stroke="{color}" stroke-width="{width}" tabindex="0"><title>{html.escape(label)}</title></line>')
    for edge in feeder.tie_lines:
        x,y=xy[edge.bus1];xx,yy=xy[edge.bus2];mx,my=(x+xx)/2,(y+yy)/2
        label=f'{edge.id}: normally-open MV tie; {edge.length_km:.5f} km; I={result.get("line_current_a",{}).get(edge.id)} A; terminal status={result.get("line_open_status",{}).get(edge.id)}'
        svg.append(f'<g><title>{html.escape(label)}</title><line x1="{x}" y1="{y}" x2="{xx}" y2="{yy}" stroke="#bd7628" stroke-width="2" stroke-dasharray="8 4 2 4"/>'
                   f'<circle cx="{mx}" cy="{my}" r="5" fill="white" stroke="#bd7628" stroke-width="1.5"/>'
                   f'<text x="{mx+8}" y="{my-7}" font-size="10" fill="#925719">NO</text></g>')
    for b in feeder.buses:
        x,y=xy[b.id];label=f'{b.id}: region={b.region_id}, {b.role}, {b.voltage_kv}kV, phases={b.phases}, V={result.get("bus_phase_voltage_pu",{}).get(b.id)}, VUF={result.get("bus_vuf_percent",{}).get(b.id)}'
        color='#84719a' if b.role=='customer' else '#536a81'
        svg.append(f'<circle cx="{x}" cy="{y}" r="3" fill="{color}" tabindex="0"><title>{html.escape(label)}</title></circle>')
    for b in feeder.buses:
        if b.role in ('source','lv_bus'):
            x,y=xy[b.id]
            svg.append(f'<text x="{x+9}" y="{y-9}" font-family="Arial, sans-serif" font-size="11" fill="#45566b">{html.escape(b.id)}</text>')
    body=''.join(svg)
    state='Passed' if checks['accepted'] else 'Failed'
    analysis=feeder.design_evidence.get('lv_equipment',{})
    topology=feeder.design_evidence.get('resolved_mv_topology',{}).get('family','legacy balanced_tree')
    return svg_page('MV-LV distribution feeder','MV-transformer-LV-customer network · Synthetic spatial coordinates',body,
        [('MV','#356a93'),('Transformer (square)','#ba8545'),('LV','#318579'),('Customers','#84719a'),('Normally-open tie (NO)','#bd7628'),('Line limit violation','#b64b4b')],
        [('Buses',len(feeder.buses)),('Transformers',len(feeder.transformers)),('Customers',len(feeder.loads)),('Acceptance',state)],
        notes='MV topology: '+topology+'; normally-open ties: '+str(len(feeder.tie_lines))+
        '. Energized connectivity is radial. Solid lines: overhead; dashed lines: cable; orange dash-dot lines: open ties (NO). '
        'Synthetic coordinates use equal scale; crossings do not imply connections. Equivalent grounding, without an explicit neutral. '
        'LV installation: '+str(analysis.get('installation',''))+'. Installation rationale and equipment provenance are stored in design_evidence.json.')


def _solve(master,connection):
    from .simulation import simulate
    try:connection.send({'result':simulate(master)})
    except Exception as exc:connection.send({'error':f'{type(exc).__name__}: {exc}'})
    finally:connection.close()


def solve_isolated(master,timeout=45):
    context=multiprocessing.get_context('spawn')
    receive,send=context.Pipe(duplex=False)
    process=context.Process(target=_solve,args=(master,send));process.start();send.close()
    try:
        if not receive.poll(timeout):raise TimeoutError('Hierarchy solve exceeded timeout')
        response=receive.recv()
        if 'error' in response:raise ValueError(response['error'])
        return response['result']
    finally:
        receive.close();process.join(timeout=1)
        if process.is_alive():process.terminate();process.join(timeout=1)
        if process.is_alive():process.kill();process.join(timeout=1)


def run_hierarchy(spec,workspace,run_id,*,agent_feedback=False,feedback_model=None,feedback_rounds=3,request="",local_topology=False,use_memory=True,protect_customer_allocation=None):
    from .rules import data
    if protect_customer_allocation is None:
        protect_customer_allocation='customer_allocation' in (spec.model_fields_set if isinstance(spec,HierarchicalSpec) else spec)
    if local_topology:agent_feedback=True
    if not 0<=feedback_rounds<=8:raise ValueError('Feedback rounds must be between 0 and 8')
    spec=HierarchicalSpec.model_validate(spec.model_dump() if isinstance(spec,HierarchicalSpec) else spec)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',run_id):raise ValueError('Invalid run ID')
    root=Path(workspace).resolve()/'hierarchical_experiments'/run_id
    root.mkdir(parents=True,exist_ok=True)
    # Fingerprint both model code and the electrical catalogues used in construction.
    code_root=Path(__file__).parent
    fingerprint=digest({p.name:file_digest(p) for p in sorted(code_root.glob('*.py'))})
    manifest=dict(schema_version=1,spec=spec.model_dump(),code_hash=fingerprint,
                  equipment_hash=digest(data('hierarchy_equipment.json')),
                  lv_equipment_hash=digest(data('lv_conductors.json')),
                  lv_underground_hash=digest(data('lv_underground.json')),
                  joint_equipment_hash=digest(data('equipment_joint.json')),
                  mv_equipment_hash=digest(data('conductors.json')))
    if agent_feedback:
        manifest['feedback']=dict(enabled=True,max_rounds=feedback_rounds,request=request,local_topology=local_topology,protect_customer_allocation=protect_customer_allocation,model=getattr(feedback_model,'model_name',None))
        manifest['feedback']['use_memory']=use_memory
    with (root/'.lock').open('w') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (root/'manifest.json').exists():
            if json.loads((root/'manifest.json').read_text())!=manifest:raise ValueError('Hierarchy configuration/code changed; use a new run ID')
            if (root/'result.json').exists():
                return read_hierarchy_result(root)
        atomic_json(root/'manifest.json',manifest)
        samples=[]
        for i in range(spec.count):
            folder=root/f'sample_{i:05d}';folder.mkdir(exist_ok=True)
            seed=int.from_bytes(hashlib.sha256(f'{spec.seed}:{i}'.encode()).digest()[:4],'big')
            try:
                feeder=generate_hierarchy(spec,seed)
                atomic_json(folder/'feeder.json',feeder.model_dump())
                atomic_json(folder/'equipment_catalog.json',feeder.equipment_catalog)
                master=export_hierarchy(feeder,folder/'opendss')
                result=solve_isolated(master)
                checks=evaluate_hierarchy(feeder,spec,result)
                feedback={}
                if agent_feedback:
                    from .hierarchy_feedback import run_feedback
                    from .verified_memory import VerifiedMemory
                    revised=run_feedback(feeder,spec,result,folder/'feedback',model=feedback_model,max_rounds=feedback_rounds,request=request,local_topology=local_topology,protect_customer_allocation=protect_customer_allocation,experience_store=VerifiedMemory(Path(workspace)/'feedback_experiences.sqlite') if use_memory else None)
                    feeder=revised['feeder'];result=revised['simulation'];checks=revised['checks']
                    feedback={k:v for k,v in revised['report'].items() if k not in ('trace','model_calls')}
                    atomic_json(folder/'feeder.json',feeder.model_dump())
                    atomic_json(folder/'equipment_catalog.json',feeder.equipment_catalog)
                    export_hierarchy(feeder,folder/'opendss')
                atomic_json(folder/'simulation.json',result);atomic_json(folder/'validation.json',checks)
                # Structural reporting must never reclassify completed electrical checks.
                diagnostic_status={}
                try:
                    from .graph_structure import graph_structure_report
                    atomic_json(folder/'structure_diagnostic.json',graph_structure_report(feeder))
                except Exception as diagnostic_exc:
                    diagnostic_status={'structure_diagnostic_error':f'{type(diagnostic_exc).__name__}: {diagnostic_exc}'}
                    try:
                        atomic_json(folder/'structure_diagnostic_error.json',diagnostic_status)
                    except OSError:
                        pass  # The error is still retained in the sample result below.
                (folder/'visualization.html').write_text(visualization(feeder,result,checks))
                samples.append(dict(sample_id=folder.name,seed=seed,**checks,**diagnostic_status,**({'feedback':feedback} if agent_feedback else {})))
            except Exception as exc:
                error=dict(sample_id=folder.name,seed=seed,accepted=False,error=f'{type(exc).__name__}: {exc}')
                from .lv_equipment import EquipmentSelectionError
                if isinstance(exc,EquipmentSelectionError):error['construction_failure']=exc.diagnostic
                atomic_json(folder/'error.json',error);samples.append(error)
        accepted=sum(s['accepted'] for s in samples)
        from .lv_equipment import resolve_installation
        lv_profile,lv_installation=resolve_installation(spec)
        report=f'MV/LV research feeder: {spec.voltage_kv}/{spec.lv_voltage_kv} kV; {spec.transformer_count} transformers; {spec.users} single-phase customers; {spec.n_buses} total buses. Attempted {spec.count} cases; accepted {accepted}.\n\nLV catalog: {lv_profile}; installation: {lv_installation}. Product data and impedance-equivalent conversions are recorded separately. Transformer parameters are selected jointly with transfer assumptions recorded. Equivalent grounding excludes neutral displacement, time series and protection validation.'
        from .hierarchy_topology_design import resolve_mv_topology,resolve_lv_topology
        report+='\n\nMV topology: '+resolve_mv_topology(spec).family+'; LV topology: '+resolve_lv_topology(spec)+'. Ring configurations retain normally-open ties. Energized connectivity is radial with one equivalent source; multi-source operation and N-1 security are not guaranteed.'
        report+='\n\nEach structure_diagnostic.json records graph structure and modeling simplifications. Electrical acceptance does not establish statistical similarity to the population of real feeders.'
        from .installation_planning import decision_summary
        report+='\n\n'+decision_summary(spec)
        if spec.structure_targets is not None:
            report+='\n\nExplicit structure targets: '+json.dumps(spec.structure_targets.model_dump(exclude_none=True),ensure_ascii=False)+'. Depth counts energized MV hops from the source; transformer-distance statistics exclude the source. Unmet targets prevent acceptance. Absence of a permitted local action does not prove global infeasibility.'
        if agent_feedback:
            report+='\n\nFeedback loop: the agent selects equipment or permitted local topology repairs from measured evidence. The executor checks invariants and recalculates, with at most '+str(feedback_rounds)+' rounds per case.\n'+ '\n'.join(s['sample_id']+': '+s.get('feedback',{}).get('stop_reason',s.get('error','Incomplete')) for s in samples)
        (root/'report.md').write_text(report)
        rows=''.join(f'<li><a href="{s["sample_id"]}/visualization.html">{s["sample_id"]}</a>: '+('Passed' if s['accepted'] else 'Failed')+'</li>' for s in samples)
        (root/'index.html').write_text('<meta charset="utf-8"><h1>MV/LV research cases</h1><p>'+html.escape(report)+'</p><ul>'+rows+'</ul><a href="dataset.zip">Download OpenDSS models and validation evidence</a>')
        with zipfile.ZipFile(root/'dataset.zip','w',zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob('*')):
                if path.is_file() and path.name not in ('.lock','dataset.zip','result.json'):archive.write(path,path.relative_to(root))
        artifacts={str(p.relative_to(root)):file_digest(p) for p in root.rglob('*') if p.is_file() and p.name not in ('.lock','result.json')}
        output=dict(experiment_id=run_id,directory=str(root),attempted=spec.count,accepted=accepted,
                    failed_or_unaccepted=spec.count-accepted,samples=samples,verified_report=report,artifacts=artifacts)
        output['result_hash']=digest(output)
        atomic_json(root/'result.json',output)
        return output
