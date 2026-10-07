"""Paired factorial experiments on explicitly frozen physical networks."""
import csv
import fcntl
import html
import itertools
import json
import re
import statistics
import zipfile
from pathlib import Path

from .artifacts import atomic_json, digest, file_digest
from .generation import generate_feeder
from .schemas import ExperimentSpec, Feeder, StudyPlan
from .workflow import run_experiment, _runtime_fingerprint
from .rules import data, load_rules


def build_study_plan(research_question, base_parameters, pv_ratios, load_scales):
    parameters=dict(base_parameters)
    parameters.setdefault('repair_policy',{'strategy':'none'})
    parameters.setdefault('max_repairs',0)
    return StudyPlan(research_question=research_question,base_spec=ExperimentSpec.model_validate(parameters),
                     pv_ratios=pv_ratios,load_scales=load_scales)


def network_fingerprint(feeder):
    return digest({'buses':[b.model_dump() for b in feeder.buses], 'lines':[e.model_dump() for e in feeder.lines],
        'tie_lines':[e.model_dump() for e in feeder.tie_lines],
        'voltage_kv':feeder.voltage_kv,'frequency_hz':feeder.frequency_hz,'source_bus':feeder.source_bus,
        'phase_mode':feeder.phase_mode,'connections':[(l.id,l.bus,l.contract_kva,l.phases,l.category) for l in feeder.loads]})


def make_variant(base, pv_ratio, load_scale):
    variant=base.model_copy(deep=True)
    variant.total_kw=base.total_kw*load_scale
    for load,original in zip(variant.loads,base.loads):
        load.kw=original.kw*load_scale
        load.kvar=original.kvar*load_scale
        weights=base.design_evidence.get('inverse_pv_allocation',{}).get('weights')
        load.pv_kw=(base.total_kw*weights[load.id] if weights else original.kw)*pv_ratio
        for phase,original_phase in zip(load.phase_powers,original.phase_powers):
            phase.kw=original_phase.kw*load_scale;phase.kvar=original_phase.kvar*load_scale
        if load.phase_powers:
            from .phase_operations import set_phase_pv
            weights=base.design_evidence['phase_design']['pv_phase_weights']
            set_phase_pv(load,weights)
    variant.assumptions += [f'Paired case: load multiplier={load_scale}; PV capacity/unscaled baseline kW={pv_ratio}.',
                            'Topology, conductors, positions, load allocation and contract capacities frozen; no repair.']
    return variant


def run_study(plan, workspace, study_id, workers=1):
    plan=StudyPlan.model_validate(plan.model_dump())
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',study_id):
        raise ValueError('Invalid study_id')
    root=Path(workspace).resolve()/'studies'/study_id
    root.mkdir(parents=True,exist_ok=True)
    with (root/'.lock').open('w') as lock:
        try:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('Study is already running') from exc
        manifest={'plan':plan.model_dump(),'runtime':_runtime_fingerprint(),
                  'rules':load_rules(),'conductors':data('conductors.json')}
        manifest['configuration_hash']=digest(manifest)
        path=root/'manifest.json'
        if path.exists() and json.loads(path.read_text())!=manifest:
            raise ValueError('Study configuration/software changed; use a new study_id')
        atomic_json(path,manifest)
        atomic_json(root/'plan.json',plan.model_dump())
        atomic_json(root/'progress.json',{'status':'running','completed_combinations':0,
                    'total_combinations':len(plan.pv_ratios)*len(plan.load_scales)})
        try:
            return _execute_study(plan,root,workers,manifest['configuration_hash'])
        except Exception as exc:
            progress=json.loads((root/'progress.json').read_text())
            atomic_json(root/'progress.json',{**progress,'status':'failed','error_type':type(exc).__name__})
            raise


def _execute_study(plan,root,workers,configuration_hash):
    baseline=run_experiment(plan.base_spec,root,'base',workers)
    base_models=[]
    baseline_metrics=[]
    for sample in baseline['samples']:
        folder=root/'experiments/base'/sample['sample_id']
        if not sample.get('model_hash'):
            raise ValueError(f"Baseline {sample['sample_id']} has no model; cannot form paired cases")
        feeder=Feeder.model_validate_json((folder/'feeder.json').read_text())
        base_models.append(feeder)
        baseline_metrics.append(json.loads((folder/'simulation.json').read_text()))
    rows=[]; combinations=[]
    for index,(pv,scale) in enumerate(itertools.product(plan.pv_ratios,plan.load_scales)):
        spec=plan.base_spec.model_dump()
        spec.update(total_kw_min=plan.base_spec.total_kw_min*scale,
                    total_kw_max=plan.base_spec.total_kw_max*scale,pv_ratio=pv/scale)
        variants=[make_variant(base,pv,scale) for base in base_models]
        experiment_id=f'variant_{index:03d}'
        outcome=run_experiment(ExperimentSpec.model_validate(spec),root,experiment_id,workers,initial_feeders=variants)
        group_rows=[]
        for pair, sample in enumerate(outcome['samples']):
            folder=root/'experiments'/experiment_id/sample['sample_id']
            base=base_models[pair]
            metric={}; consistent=False
            if sample.get('model_hash'):
                final=Feeder.model_validate_json((folder/'feeder.json').read_text())
                metric=json.loads((folder/'simulation.json').read_text())
                consistent=network_fingerprint(final)==network_fingerprint(base) and final.loads==variants[pair].loads
                if not consistent:
                    raise ValueError('Paired invariant violation: network or requested loads changed')
            base_sim=baseline_metrics[pair]
            delta=(metric['min_voltage_pu']-base_sim['min_voltage_pu']
                   if metric.get('min_voltage_pu') is not None and base_sim.get('min_voltage_pu') is not None else None)
            row={'base_case_id':f"base/{baseline['samples'][pair]['sample_id']}",
                 'baseline_model_hash':digest(base.model_dump()),'network_hash':network_fingerprint(base),
                 'experiment_id':experiment_id,'sample_id':sample['sample_id'], 'seed':base.seed,
                 'pv_capacity_ratio':pv,'load_scale':scale,'effective_pv_ratio':pv/scale,
                 'paired_verified':consistent,'valid':sample['valid'],'accepted':sample['accepted'],
                 'operational_pass':sample['operational_pass'],'solver_converged':sample['solver_converged'],
                 'min_voltage_pu':metric.get('min_voltage_pu'),'max_voltage_pu':metric.get('max_voltage_pu'),
                 'delta_min_voltage_from_base':delta,'loss_kw':metric.get('loss_kw'),
                 'load_kw':metric.get('load_kw'),'generation_kw':metric.get('generation_kw'),
                 'source_kw':metric.get('source_kw'),'error':sample.get('error'),
                 'relative_path':f'experiments/{experiment_id}/{sample["sample_id"]}'}
            rows.append(row);group_rows.append(row)
        voltages=[r['min_voltage_pu'] for r in group_rows if r['valid'] and r['min_voltage_pu'] is not None]
        combinations.append({'experiment_id':experiment_id,'pv_capacity_ratio':pv,'load_scale':scale,
            'attempted':len(group_rows),'valid':sum(r['valid'] for r in group_rows),
            'operational_pass':sum(r['operational_pass'] for r in group_rows),
            'mean_min_voltage_pu':statistics.mean(voltages) if voltages else None})
        atomic_json(root/'progress.json',{'status':'running','completed_combinations':index+1,
                    'total_combinations':len(plan.pv_ratios)*len(plan.load_scales)})
    report=(f"受控研究 `{root.name}`：{len(base_models)}条基准网络，{len(combinations)}个参数组合，{len(rows)}个变体。\n\n"
            f"配对一致性通过 {sum(r['paired_verified'] for r in rows)}/{len(rows)}；运行检查通过 {sum(r['operational_pass'] for r in rows)}/{len(rows)}。\n\n"
            '节点、线路、设备与合同容量固定；修复禁用。PV轴以未缩放基准峰值负荷为分母，改变负荷不改变PV绝对容量。'
            '电压差以各自基准案例为参考，不代表统计显著性；压力模式接受不等于运行合格。\n\n'
            f'目录：`{root}`；`comparison.csv`为逐案例指标，`index.html`为对比入口，`dataset.zip`包含模型和证据。')
    result={'study_id':root.name,'configuration_hash':configuration_hash,'directory':str(root),
            'base_cases':len(base_models),'variant_count':len(rows),'combinations':combinations,
            'samples':rows,'verified_report':report}
    atomic_json(root/'summary.json',result)
    with (root/'comparison.csv').open('w',newline='',encoding='utf-8-sig') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    (root/'report.md').write_text(report,encoding='utf-8')
    _render_index(root,result)
    with zipfile.ZipFile(root/'dataset.zip.tmp','w',zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(root.rglob('*')):
            if file.is_file() and file.name not in {'.lock','dataset.zip','dataset.zip.tmp','progress.json'}:
                archive.write(file,file.relative_to(root))
    (root/'dataset.zip.tmp').replace(root/'dataset.zip')
    atomic_json(root/'progress.json',{'status':'completed','completed_combinations':len(combinations),
                'total_combinations':len(combinations)})
    return result


def _render_index(root,result):
    rows=[]
    for sample in result['samples']:
        rows.append(f'<tr data-pair="{html.escape(sample["base_case_id"])}"><td>{html.escape(sample["base_case_id"])}</td>'
            f'<td>{sample["pv_capacity_ratio"]}</td><td>{sample["load_scale"]}</td>'
            f'<td>{sample["min_voltage_pu"]}</td><td>{sample["loss_kw"]}</td><td>{sample["operational_pass"]}</td>'
            f'<td>{sample["paired_verified"]}</td><td><a href="{sample["relative_path"]}/visualization.html">查看网络</a></td></tr>')
    options=''.join(f'<option>{html.escape(name)}</option>' for name in sorted({r['base_case_id'] for r in result['samples']}))
    (root/'index.html').write_text('<!doctype html><meta charset="utf-8"><title>受控馈线研究</title>'
        '<style>body{font:15px system-ui;max-width:1200px;margin:35px auto}td,th{padding:10px;border-bottom:1px solid #ddd}select{padding:8px}</style>'
        f'<h1>受控参数研究 · {html.escape(root.name)}</h1><p>{result["base_cases"]}条基准网络，{result["variant_count"]}个变体；修复禁用。</p>'
        '<p>PV容量比例以未缩放的基准负荷为分母。运行越限保留，不等于模型无效；配对检查确认网络和负荷变换保持一致。</p>'
        '<p><a href="comparison.csv">下载CSV</a> · <a href="dataset.zip">下载全部模型</a> · <a href="report.md">研究说明</a></p>'
        f'<label>筛选同一基础网络：<select id="pair"><option value="">全部</option>{options}</select></label>'
        '<table><thead><tr><th>基准案例</th><th>PV容量比例</th><th>负荷倍数</th><th>最低电压pu</th><th>损耗kW</th><th>运行合格</th><th>配对一致</th><th>模型</th></tr></thead><tbody>'
        +''.join(rows)+'</tbody></table><script>document.querySelector("#pair").onchange=e=>document.querySelectorAll("tbody tr").forEach(r=>r.hidden=!!e.target.value&&r.dataset.pair!==e.target.value);</script>',encoding='utf-8')
