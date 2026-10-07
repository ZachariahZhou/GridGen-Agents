"""Render conclusions from executed checks, never infer compliance from a label."""
import json


RULE_LABELS = {
    'cn.frequency': '名义频率条款', 'cn.power_factor': '用户功率因数条款',
    'cn.voltage': '用户端电压条款', 'research.voltage': '研究电压范围',
    'research.unbalance':'研究电压不平衡率','research.ampacity': '所选设备载流量', 'model.radial_connected': '连通辐射结构',
    'model.demand': '实验规格与几何一致性', 'model.solver': '潮流与功率平衡', 'design.voltage_scope':'电压规则包一致性', 'design.geometry':'空间长度一致性', 'design.equipment':'研究设备参数完整性'}
STATUS = {'pass': '通过', 'fail': '未通过', 'not_applicable': '不适用（不代表通过）'}


def render_report(summary):
    lines = [f"实验 `{summary['experiment_id']}`：接受 {summary['accepted']} / 尝试 {summary['attempted']}；"
             f"未接受 {summary['failed_or_unaccepted']}。",
             '模型：单电压快照，相别模式见各样本；PV为恒功率等效注入。']
    if summary.get('directory'):
        lines.append(f"产物目录：`{summary['directory']}`；入口 `index.html`，数据集 `dataset.zip`，检查证据 `sample_*/validation.json`。")
    samples = summary['samples'][:10]
    for sample in samples:
        lines.append(f"\n样本 `{sample['sample_id']}`：")
        if sample.get('error'):
            lines.append(f"生成/计算失败：{sample['error']}")
            continue
        lines.append(f"{sample.get('voltage_kv', 10):g} kV；负荷点 {sample['load_count']}，总负荷 {sample['total_kw']:.3f} kW；"
                     f"潮流{'收敛' if sample['solver_converged'] else '未收敛'}。")
        lines.append(f"相别模式：{('三相不平衡' if sample.get('phase_mode')=='unbalanced' else '三相平衡')}；最大VUF：{sample.get('max_vuf_percent')}%；ABC覆盖节点：{sample.get('vuf_eligible_bus_count')}；缺相节点不计算VUF。")
        if sample.get('matpower_export') is not None:
            exported=sample['matpower_export']
            if exported.get('verified'):
                lines.append(f"MATPOWER平衡等值导出已重载并通过PYPOWER AC对照；最大节点电压差 {exported['max_bus_voltage_error_pu']:.3g} pu。文件：matpower/case_generated.m。固定馈线入口电压，未保留上游电源内部阻抗；边界假设见metadata.json。")
            else:
                lines.append('MATPOWER导出尚未通过独立验证：'+str(exported.get('reason','数值对照未通过'))+'；详见matpower/validation.json。')
        if sample.get('equipment_sources'):
            lines.append(f"设备采用真实馈线派生目录：{sample['equipment_sources']}，实际型号数{sample['equipment_type_count']}；完整参数、来源和频率转换见equipment_catalog.json，非独立随机拼接参数。")
        if sample.get('design_method') == 'reference_conditioned_spatial_v1':
            lines.append(f"总节点{sample['bus_count']}，负荷点{sample['load_count']}，零负荷连接节点{sample['junction_count']}；位置/权重采用参考节点深度与分支条件采样。参考依据和回退记录见design_evidence.json。")
        if sample.get('design_method') == 'rural_villages_v1':
            lines.append(f"结构：主干连接{sample['village_count']}个合成村落；村落负荷占80%。"
                '初始导线按下游负荷/PV端点估算电流选择；布局和容量裕度为研究假设，设计依据见design_evidence.json。')
        if sample.get('design_method') == 'empirical_tree_v1':
            lines.append('使用EPRI实际馈线派生模型的长度/分支/相对负荷参考分布，结合显式先验生成；'
                '属于部分边际校准，非中国城乡代表性认证。分布截断、混合权重及设备初选依据见design_evidence.json。')
        if sample.get('min_voltage_pu') is not None:
            lines.append(f"电压 {sample['min_voltage_pu']:.6f}–{sample['max_voltage_pu']:.6f} pu；总线长 {sample['total_line_km']:.3f} km。")
        lines.append(f"修复策略 {sample.get('repair_strategy', 'unknown')}；试验 {sample['repairs']} 次，"
                     f"保留 {sample.get('repair_commits',0)} 次，回滚 {sample.get('repair_rollbacks',0)} 次。")
        if sample.get('repair_stop_reason'):
            lines.append(f"修复停止原因：`{sample['repair_stop_reason']}`。")
        for rule, status in sample.get('rule_status', {}).items():
            label = ('文档候选约束 '+rule) if rule.startswith('doc.') else RULE_LABELS.get(rule,rule)
            lines.append(f"- {label}：{STATUS[status]}。")
    if len(summary['samples']) > 10:
        lines.append('这里只展示前10个样本，完整结果见summary.json。')
    lines.append('\n通过仅指已执行且适用的检查；不适用条款未被认证。设备与部分几何仍为研究先验，不代表全参数校准或完整设计合规。')
    return '\n\n'.join(lines)


def verified_answer(messages):
    """Prefer current-turn tool reports; do not replay an earlier turn's results."""
    latest_user = max((i for i,m in enumerate(messages) if m.type == 'human'), default=-1)
    reports = {}
    for message in messages[latest_user+1:]:
        if message.type != 'tool' or getattr(message,'name',None) not in {'generate_cases','execute_plan','read_experiment_result','execute_paired_study','design_from_language'}:
            continue
        try:
            payload = json.loads(message.content)
        except (TypeError, ValueError):
            continue
        if isinstance(payload,dict) and isinstance(payload.get('verified_report'),str):
            key = payload.get('experiment_id', payload.get('directory','result'))
            reports[key] = payload['verified_report']
    return '\n\n---\n\n'.join(reports.values()) if reports else messages[-1].content
