import hashlib
import html
import json
import os
import tempfile
from pathlib import Path

from .schemas import Feeder
from .rules import data


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.write-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(payload)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def render_html(feeder: Feeder, result: dict, accepted: bool) -> str:
    """Portable SVG with hover, click, wheel zoom and drag; no CDN dependencies."""
    from .visual_theme import fit_coordinates,svg_page
    points=fit_coordinates({b.id:(b.x_km,b.y_km) for b in feeder.buses})
    catalogue = data('conductors.json')
    svg = []
    trunk = feeder.design_evidence.get('trunk_buses',[])
    trunk_edges = {frozenset((a,b)) for a,b in zip(trunk,trunk[1:])}
    membership = {bus:village['id'] for village in feeder.design_evidence.get('villages',[]) for bus in village['buses']}
    for line in feeder.lines:
        x1, y1 = points[line.bus1]
        x2, y2 = points[line.bus2]
        current = result.get('line_current_a', {}).get(line.id)
        ratio = current / catalogue[line.conductor]['normamps'] if current is not None else None
        is_trunk = frozenset((line.bus1,line.bus2)) in trunk_edges
        color = '#b64b4b' if ratio is not None and ratio > 1 else ('#356a93' if is_trunk else '#64748b')
        label = html.escape(f'{line.id} | {line.bus1} → {line.bus2} | '
                            f'{line.length_km:.3f} km | {line.conductor} | phases={line.phases} | {line.construction} | I={result.get("line_phase_current_a",{}).get(line.id)} | loading={ratio} | '+('Trunk' if is_trunk else 'Lateral'))
        svg.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" '
                   f'stroke-width="3" tabindex="0"><title>{label}</title></line>')
    for line in feeder.tie_lines:
        x1, y1 = points[line.bus1]
        x2, y2 = points[line.bus2]
        label = html.escape(f'{line.id} | {line.bus1} → {line.bus2} | '
                            f'{line.length_km:.3f} km | {line.conductor} | Normally-open tie')
        svg.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#8a719c" '
                   f'stroke-width="3" stroke-dasharray="8 6" tabindex="0"><title>{label}</title></line>')
    loads = {x.bus: x for x in feeder.loads}
    band=feeder.design_evidence.get('rule_system',{}).get('voltage_profile',{}).get('research_voltage',[.93,1.07])
    for bus in feeder.buses:
        x, y = points[bus.id]
        values = result.get('bus_voltage_pu', {}).get(bus.id, [])
        voltage = min(values) if values else None
        color = '#64748b' if voltage is None else ('#b64b4b' if voltage < band[0] or voltage > band[1] else '#318579')
        load = loads.get(bus.id)
        role='Source' if bus.id==feeder.source_bus else 'Load bus' if load else 'Zero-load junction'
        label = f'{bus.id} | {role} | phases={bus.phases} | voltage={voltage} pu | phase voltages={result.get("bus_phase_voltage_pu",{}).get(bus.id)} | VUF={result.get("bus_vuf_percent",{}).get(bus.id)} %'
        if bus.id in membership:
            label += f' | {membership[bus.id]}'
        if load:
            label += f' | load={load.kw:.2f} kW | PV={load.pv_kw:.2f} kW | {load.category} | phase powers={[p.model_dump() for p in load.phase_powers]}'
        svg.append(f'<circle cx="{x}" cy="{y}" r="{7 if bus.id == feeder.source_bus else 4}" '
                   f'fill="{color}" tabindex="0"><title>{html.escape(label)}</title></circle>')
    state = 'Accepted' if accepted else 'Not accepted'
    body = '\n'.join(svg)
    return svg_page('Distribution feeder',f'{feeder.voltage_kv:g} kV · {feeder.phase_mode} · Synthetic spatial coordinates',body,
        [('Within voltage limits','#318579'),('Limit violation','#b64b4b'),('Unmeasured / lateral','#64748b'),('Trunk','#356a93'),('Normally-open tie (dashed)','#8a719c')],
        [('Buses',len(feeder.buses)),('Lines',len(feeder.lines)),('Acceptance',state)],
        notes='Synthetic coordinates shown at equal scale, not GIS data. Voltage colors use the case-specific research limits. Equipment provenance is included in the model archive. SVG exports retain the full view.')
