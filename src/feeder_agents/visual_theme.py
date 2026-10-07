"""Shared, offline academic figure presentation for network SVGs."""
import html


def fit_coordinates(points,width=900,height=600,padding=55):
    xs=[p[0] for p in points.values()];ys=[p[1] for p in points.values()]
    scale=min((width-2*padding)/max(max(xs)-min(xs),.001),(height-2*padding)/max(max(ys)-min(ys),.001))
    cx=(max(xs)+min(xs))/2;cy=(max(ys)+min(ys))/2
    return {key:(width/2+(x-cx)*scale,height/2-(y-cy)*scale) for key,(x,y) in points.items()}


def svg_page(title,subtitle,body,legend,metrics=(),notes='',viewbox='0 0 900 600'):
    esc=html.escape
    keys=''.join(f'<span><i style="background:{esc(color)}"></i>{esc(label)}</span>' for label,color in legend)
    stats=''.join(f'<div><small>{esc(str(label))}</small><strong>{esc(str(value))}</strong></div>' for label,value in metrics)
    return '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'''+esc(title)+'''</title>
<style>
*{box-sizing:border-box}body{margin:0;padding:22px 26px;background:#f5f7fa;color:#253449;font:14px/1.6 "Segoe UI","Noto Sans CJK SC",Arial,sans-serif}.sheet{max-width:1280px;margin:auto;background:white;border:1px solid #dce3eb;border-radius:8px;padding:24px 28px}.eyebrow{font-size:10px;letter-spacing:2px;color:#718096;font-weight:600}h1{font-size:23px;font-weight:600;margin:5px 0 4px;letter-spacing:.2px}.subtitle{color:#66758a;margin:0 0 16px}.stats{display:flex;flex-wrap:wrap;border-top:1px solid #e4e9ef;border-bottom:1px solid #e4e9ef;margin-bottom:15px}.stats>div{padding:10px 26px 10px 0;min-width:115px}.stats small{display:block;font-size:11px;color:#718096}.stats strong{font:600 18px/1.7 Georgia,"Times New Roman",serif;font-variant-numeric:tabular-nums}.legend{display:flex;flex-wrap:wrap;gap:7px 20px;font-size:12px;color:#526174}.legend span{display:inline-flex;align-items:center;gap:7px}.legend i{display:inline-block;width:15px;height:3px}.tools{display:flex;gap:8px;justify-content:flex-end;margin:10px 0}button{background:white;border:1px solid #cdd6e0;border-radius:4px;padding:5px 12px;color:#34465c;cursor:pointer;font:inherit;font-size:12px}button:hover,button:focus-visible{background:#edf3f8;border-color:#315b7d}svg{width:100%;height:440px;display:block;touch-action:none;background:white;border:1px solid #eef1f5}#detail{border-top:1px solid #e4e9ef;padding-top:12px;margin-top:12px;min-height:45px;color:#45566b;overflow-wrap:anywhere;font-size:12px}.notes{color:#758295;font-size:11px;margin:9px 0 0}svg [tabindex]:focus{outline:none;stroke:#202e40;stroke-width:3} @media(max-width:600px){body{padding:8px}.sheet{padding:14px}h1{font-size:19px}svg{height:350px}}@media print{body{padding:0;background:white}.sheet{border:0}.tools,#detail{display:none}svg{height:auto}}
</style></head><body><main class="sheet"><div class="eyebrow">POWER SYSTEMS · RESEARCH CASE</div><h1>'''+esc(title)+'''</h1><p class="subtitle">'''+esc(subtitle)+'''</p><section class="stats">'''+stats+'''</section><div class="legend">'''+keys+'''</div><div class="tools"><button id="reset">复位</button><button id="export">导出 SVG</button></div><svg xmlns="http://www.w3.org/2000/svg" id="canvas" viewBox="'''+viewbox+'''" role="img" aria-label="'''+esc(title)+'''"><title>'''+esc(title)+'''</title><desc>'''+esc(subtitle)+'''</desc><rect width="100%" height="100%" fill="white"/><g id="drawing">'''+body+'''</g></svg><div id="detail" aria-live="polite">滚轮缩放 · 拖动平移 · 点击或使用 Tab 选择设备查看参数</div><p class="notes">'''+esc(notes)+'''</p></main><script>
const svg=document.querySelector('#canvas'),g=document.querySelector('#drawing');let scale=1,tx=0,ty=0,drag=null;
function paint(){g.setAttribute('transform',`translate(${tx} ${ty}) scale(${scale})`)}
function point(e){const p=svg.createSVGPoint();p.x=e.clientX;p.y=e.clientY;return p.matrixTransform(svg.getScreenCTM().inverse())}
svg.addEventListener('wheel',e=>{e.preventDefault();const p=point(e),next=Math.max(.25,Math.min(16,scale*Math.exp(-e.deltaY*.001)));tx=p.x-(p.x-tx)*next/scale;ty=p.y-(p.y-ty)*next/scale;scale=next;paint()},{passive:false});
svg.addEventListener('pointerdown',e=>{drag=point(e);svg.setPointerCapture(e.pointerId)});svg.addEventListener('pointermove',e=>{if(drag){const p=point(e);tx+=p.x-drag.x;ty+=p.y-drag.y;drag=p;paint()}});svg.addEventListener('pointerup',()=>drag=null);svg.addEventListener('pointercancel',()=>drag=null);
function details(e){const t=e.target.querySelector('title');if(t)document.querySelector('#detail').textContent=t.textContent}g.addEventListener('click',details);g.addEventListener('focusin',details);
document.querySelector('#reset').onclick=()=>{scale=1;tx=ty=0;paint()};
document.querySelector('#export').onclick=()=>{const copy=svg.cloneNode(true);copy.querySelector('#drawing').removeAttribute('transform');copy.setAttribute('width',svg.viewBox.baseVal.width);copy.setAttribute('height',svg.viewBox.baseVal.height);const vb=svg.viewBox.baseVal;copy.querySelector('rect').setAttribute('width',vb.width);copy.querySelector('rect').setAttribute('height',vb.height+85);copy.setAttribute('viewBox',`${vb.x} ${vb.y} ${vb.width} ${vb.height+85}`);copy.setAttribute('height',vb.height+85);const ns='http://www.w3.org/2000/svg';function text(t,x,y,size){const n=document.createElementNS(ns,'text');n.setAttribute('x',x);n.setAttribute('y',y);n.setAttribute('fill','#34465c');n.setAttribute('font-family','Arial, sans-serif');n.setAttribute('font-size',size);n.textContent=t;copy.appendChild(n)}text(document.querySelector('h1').textContent,20,vb.height+20,15);text(document.querySelector('.subtitle').textContent,20,vb.height+42,11);let lx=20;document.querySelectorAll('.legend span').forEach(item=>{const mark=document.createElementNS(ns,'rect');mark.setAttribute('x',lx);mark.setAttribute('y',vb.height+60);mark.setAttribute('width',14);mark.setAttribute('height',3);mark.setAttribute('fill',item.querySelector('i').style.background);copy.appendChild(mark);text(item.textContent,lx+20,vb.height+65,10);lx+=Math.max(115,item.textContent.length*11+30)});const url=URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(copy)],{type:'image/svg+xml;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download='network_figure.svg';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
</script></body></html>'''


def render_saved_view(path,layout_mode='geographic',scope='full',transformer_id=None):
    """Render saved numerical evidence with current styling; never mutate artifacts."""
    import json
    from pathlib import Path
    path=Path(path);root=path.parent
    def read(name):return json.loads((root/name).read_text())
    if layout_mode not in ('geographic','topology'):raise ValueError('Unknown display layout')
    if scope not in ('mv','full','lv'):raise ValueError('Unknown network display scope')
    if layout_mode=='topology' or scope!='full':
        from .schematic import network_graph,network_view,schematic_html
        checks=read('validation.json') if (root/'validation.json').exists() else {}
        if (root/'case.json').exists() and layout_mode=='topology':
            return schematic_html(network_graph(read('case.json'),read('metadata.json') if (root/'metadata.json').exists() else {}),accepted=checks.get('accepted'))
        if (root/'feeder.json').exists():
            graph=network_view(network_graph(read('feeder.json')),scope,transformer_id)
            points={n:(a['x_km'],a['y_km']) for n,a in graph.nodes(data=True)} if layout_mode=='geographic' else None
            return schematic_html(graph,positions=points,accepted=checks.get('accepted'),geographic=layout_mode=='geographic')
    if (root/'case.json').exists() and (root/'metadata.json').exists():
        import numpy as np
        from .transmission import visualization
        case=read('case.json')
        for key in ('bus','gen','branch'):case[key]=np.asarray(case[key])
        return visualization(case,read('metadata.json'),read('validation.json'))
    if (root/'feeder.json').exists() and (root/'simulation.json').exists():
        f=read('feeder.json');sim=read('simulation.json')
        if 'transformers' in f:
            from .hierarchy import HierarchicalFeeder
            from .hierarchy_workflow import visualization
            checks=read('validation.json') if (root/'validation.json').exists() else {}
            if 'accepted' not in checks:return path.read_text(encoding='utf-8')
            return visualization(HierarchicalFeeder.model_validate(f),sim,checks)
        # Single-voltage artifact schemas differ across frozen milestones.
        from .schemas import Feeder
        from .artifacts import render_html
        from pydantic import ValidationError
        try:feeder=Feeder.model_validate(f)
        except ValidationError:return path.read_text(encoding='utf-8')
        outcome=read('outcome.json') if (root/'outcome.json').exists() else None
        if outcome is not None:return render_html(feeder,sim,outcome['accepted'])
    if (root/'normalized_case.json').exists() and (root/'simulation.json').exists():
        from .references import reference_visualization
        return reference_visualization(read('normalized_case.json'),read('simulation.json'))
    return path.read_text(encoding='utf-8')
