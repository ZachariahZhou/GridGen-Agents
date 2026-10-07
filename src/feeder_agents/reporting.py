"""Render conclusions from executed checks, never infer compliance from a label."""
import json


RULE_LABELS = {
    'cn.frequency': 'Nominal frequency clause', 'cn.power_factor': 'Customer power-factor clause',
    'cn.voltage': 'Customer voltage clause', 'research.voltage': 'Research voltage limits',
    'research.unbalance':'Research voltage unbalance','research.ampacity': 'Equipment ampacity', 'model.radial_connected': 'Connected radial topology',
    'model.demand': 'Specification and geometry consistency', 'model.solver': 'Power flow and power balance', 'design.voltage_scope':'Voltage rule-set consistency', 'design.geometry':'Spatial length consistency', 'design.equipment':'Equipment parameter completeness'}
STATUS = {'pass': 'Passed', 'fail': 'Failed', 'not_applicable': 'Not applicable (not a pass)'}


def render_report(summary):
    lines = [f"Experiment `{summary['experiment_id']}`: accepted {summary['accepted']} / attempted {summary['attempted']}; "
             f"not accepted {summary['failed_or_unaccepted']}.",
             'Model: single-voltage network; see each sample for its phase mode. PV uses constant-power equivalent injections.']
    if summary.get('directory'):
        lines.append(f"Artifacts: `{summary['directory']}`; index: `index.html`; archive: `dataset.zip`; checks: `sample_*/validation.json`.")
    samples = summary['samples'][:10]
    for sample in samples:
        lines.append(f"\nSample `{sample['sample_id']}`:")
        if sample.get('error'):
            lines.append(f"Generation / calculation failed: {sample['error']}")
            continue
        lines.append(f"{sample.get('voltage_kv', 10):g} kV; load points: {sample['load_count']}; total demand: {sample['total_kw']:.3f} kW; "
                     f"power flow {'converged' if sample['solver_converged'] else 'did not converge'}.")
        lines.append(f"Phase mode: {('unbalanced three-phase' if sample.get('phase_mode')=='unbalanced' else 'balanced three-phase')}; maximum VUF: {sample.get('max_vuf_percent')}%; eligible ABC buses: {sample.get('vuf_eligible_bus_count')}. VUF excludes buses with missing phases.")
        if sample.get('matpower_export') is not None:
            exported=sample['matpower_export']
            if exported.get('verified'):
                lines.append(f"The balanced MATPOWER equivalent was reloaded and checked against PYPOWER AC results; maximum bus-voltage difference: {exported['max_bus_voltage_error_pu']:.3g} pu. File: matpower/case_generated.m. The feeder inlet voltage is fixed and upstream source impedance is excluded; see metadata.json for boundary assumptions.")
            else:
                lines.append('MATPOWER export has not passed independent verification: '+str(exported.get('reason','Numerical comparison failed'))+'; see matpower/validation.json.')
        if sample.get('equipment_sources'):
            lines.append(f"Equipment uses feeder-derived catalogs: {sample['equipment_sources']}; selected types: {sample['equipment_type_count']}. Parameters are selected jointly. See equipment_catalog.json for provenance and frequency conversion.")
        if sample.get('design_method') == 'reference_conditioned_spatial_v1':
            lines.append(f"Buses: {sample['bus_count']}; load points: {sample['load_count']}; zero-load junctions: {sample['junction_count']}. Placement and weights are conditioned on reference depth and branching. Evidence and fallback records: design_evidence.json.")
        if sample.get('design_method') == 'rural_villages_v1':
            lines.append(f"A trunk connects {sample['village_count']} synthetic villages containing 80% of demand. "
                'Initial conductors use downstream load/PV current estimates; layout and capacity margins are research assumptions. See design_evidence.json.')
        if sample.get('design_method') == 'empirical_tree_v1':
            lines.append('Length, branching and relative-load marginals derived from EPRI feeders are combined with explicit priors. '
                'This is partial marginal calibration, not validation of population representativeness. Truncation, mixture weights and equipment-selection evidence are in design_evidence.json.')
        if sample.get('min_voltage_pu') is not None:
            lines.append(f"Voltage: {sample['min_voltage_pu']:.6f}–{sample['max_voltage_pu']:.6f} pu; total line length: {sample['total_line_km']:.3f} km.")
        lines.append(f"Repair policy: {sample.get('repair_strategy', 'unknown')}; attempts: {sample['repairs']}; "
                     f"committed: {sample.get('repair_commits',0)}; rolled back: {sample.get('repair_rollbacks',0)}.")
        if sample.get('repair_stop_reason'):
            lines.append(f"Repair stop reason: `{sample['repair_stop_reason']}`.")
        for rule, status in sample.get('rule_status', {}).items():
            label = ('Document candidate constraint '+rule) if rule.startswith('doc.') else RULE_LABELS.get(rule,rule)
            lines.append(f"- {label}: {STATUS[status]}.")
    if len(summary['samples']) > 10:
        lines.append('Showing the first 10 samples; full results are in summary.json.')
    lines.append('\nPassing refers to executed, applicable checks. Inapplicable clauses are not certified. Equipment and some geometry retain research priors; results do not establish full parameter calibration or complete planning compliance.')
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
