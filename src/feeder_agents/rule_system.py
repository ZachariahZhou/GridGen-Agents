"""Design inventory and executable guards shared by the Agent and workflow."""
import math
from .rules import data
from .voltage import voltage_profile, available_voltage_profiles

HANDLERS = {
    'S01': ['design.voltage_scope'], 'S04': ['model.demand'], 'L02': ['model.demand'], 'G01': ['design.geometry'],
    'G03': ['model.demand', 'design.geometry'], 'G04': ['design.geometry'],
    'T01': ['model.radial_connected'], 'L01': ['model.demand'],
    'E01': ['design.equipment'], 'O01': ['model.solver'],
    'O02': ['cn.voltage', 'research.voltage'], 'O03': ['research.ampacity'],
    'O04': ['cn.power_factor'],
}


def describe_rule_system(spec=None):
    from .schemas import ExperimentSpec
    spec = spec or ExperimentSpec()
    profile = voltage_profile(spec.voltage_kv)
    requirements = data('design_requirements.json')
    for item in requirements:
        item['check_ids'] = HANDLERS.get(item['id'], [])
        item['enforcement'] = 'partial_executable' if item['check_ids'] else 'inventory_only'
        item['selected_voltage_kv'] = spec.voltage_kv
        if (item['id'] == 'O02' and spec.voltage_kv not in (6, 10, 20)) or (item['id'] == 'O04' and spec.voltage_kv < 1):
            item['implementation_note'] += '; sourced customer-clause handler unavailable at selected voltage'
        if item['id'] in {'C01', 'C02'} and profile['tier'] != 'mv':
            item['coverage'] = 'unsupported'
            item['implementation_note'] = 'No packaged reference distribution for this voltage tier'
    return {'version': 1, 'voltage_profile': profile,
            'network_policy': {'network_kind': 'distribution', 'source_model': 'upstream_grid_equivalent',
                'thermal_generation': 'Possible in distribution (e.g. CHP), but explicit thermal units are unsupported by this generator',
                'transmission': 'Unsupported; not inferred solely from voltage or generator fuel',
                'supported_injections': ['fixed_unity_pf_pv']}, 'requirements': requirements,
            'available_profiles': available_voltage_profiles(),
            'notice': 'Inventory coverage is not a pass verdict. Unsupported capabilities are never certified.'}


def evaluate_design_rules(feeder, spec):
    profile = voltage_profile(spec.voltage_kv)
    positions = {b.id: (b.x_km, b.y_km) for b in feeder.buses}
    geometry = (all(math.isfinite(x) for p in positions.values() for x in p) and
                all(e.bus1 in positions and e.bus2 in positions and
                    math.isclose(e.length_km, math.dist(positions[e.bus1], positions[e.bus2]),
                                 rel_tol=1e-9, abs_tol=1e-10) for e in feeder.lines))
    catalogue = data('conductors.json')
    bad = []
    for line in feeder.lines:
        c = catalogue.get(line.conductor)
        if c is None or any(not isinstance(c.get(k), (float, int)) or not math.isfinite(c[k])
                            or c[k] <= 0 for k in ('r1', 'x1', 'r0', 'x0', 'normamps')):
            bad.append(line.id)
    values = [
        ('design.voltage_scope', feeder.voltage_kv == spec.voltage_kv and feeder.frequency_hz == spec.frequency_hz,
         {'profile_id': profile['id'], 'requested_kv': spec.voltage_kv, 'actual_kv': feeder.voltage_kv}),
        ('design.geometry', geometry, {'basis': 'Synthetic straight-line km; not road routing or tower spans'}),
        ('design.equipment', not bad, {'invalid_lines': bad, 'basis': 'Observed EPRI equipment tuples; source and frequency/voltage mapping recorded' if any(l.conductor.startswith('epri_') for l in feeder.lines) else profile['equipment_basis']}),
    ]
    return [dict(rule_id=rid, rule_version='1', category='validity', kind='project_assumption',
                 source_id='project-v1', locator='feeder-design-rule-system',
                 status='pass' if ok else 'fail', evidence=evidence) for rid, ok, evidence in values]
