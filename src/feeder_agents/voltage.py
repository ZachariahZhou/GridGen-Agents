"""Voltage-scoped research profiles, not certified equipment/design standards."""
from copy import deepcopy

# Bounds below are sampling priors, never statutory supply radii or span limits.
PROFILES = {
    .38: dict(tier='lv', label='Balanced three-phase LV', defaults=dict(total_kw_min=20, total_kw_max=80,
        segment_km_min=.005, segment_km_max=.03), research_voltage=[.93, 1.07]),
    6: dict(tier='mv', label='6 kV MV', defaults=dict(total_kw_min=500, total_kw_max=1500,
        segment_km_min=.03, segment_km_max=.2), research_voltage=[.93, 1.07]),
    10: dict(tier='mv', label='10 kV MV', defaults={}, research_voltage=[.93, 1.07]),
    20: dict(tier='mv', label='20 kV MV', defaults=dict(total_kw_min=1500, total_kw_max=4000,
        segment_km_min=.05, segment_km_max=.4), research_voltage=[.93, 1.07]),
    35: dict(tier='hv', label='35 kV HV distribution research equivalent', defaults=dict(total_kw_min=3000, total_kw_max=10000,
        segment_km_min=.1, segment_km_max=1), research_voltage=[.95, 1.05]),
}


def voltage_profile(voltage_kv=10):
    if voltage_kv not in PROFILES:
        raise ValueError(f'Unsupported voltage {voltage_kv} kV; supported single-voltage research cases: {list(PROFILES)}. '
                         '66/110 kV and transformer-coupled voltage levels are planning-only.')
    p = deepcopy(PROFILES[voltage_kv])
    p.update(id=f'cn_research_{voltage_kv:g}kv_v1', version=1, voltage_kv=voltage_kv,
             origin='project_assumption', supported=True,
             equipment_basis='Illustrative sequence impedances/current ratings; not certified insulation, thermal or fault ratings.',
             missing_capabilities=['transformers', 'switching', 'protection', 'unbalance', 'neutral', 'time_series'])
    return p


def available_voltage_profiles():
    return [voltage_profile(v) for v in PROFILES] + [
        dict(voltage_kv=v, tier='hv', supported=False, status='planning_only') for v in (66, 110)]
