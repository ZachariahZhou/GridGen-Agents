"""Deterministic answer-key scoring of actual generated model fields, not LLM prose."""
import math


def observed_requirements(feeder):
    buses={b.id:b for b in feeder.buses}
    installation_by_region={}
    names={r['id']:r['name'] for r in feeder.design_evidence.get('lv_equipment',{}).get('regions',[])}
    installations=set()
    for line in feeder.lines:
        b=buses[line.bus1]
        if b.voltage_kv>=1:continue
        code=feeder.equipment_catalog[line.conductor]
        installation=code.get('installation','legacy_equivalent')
        installations.add(installation)
        if b.region_id is not None:
            installation_by_region.setdefault(names.get(b.region_id,b.region_id),set()).add(installation)
    total=sum(l.kw for l in feeder.loads)
    return dict(n_buses=len(feeder.buses),mv_buses=sum(b.voltage_kv>1 for b in feeder.buses),users=len(feeder.loads),transformers=len(feeder.transformers),
        lv_branches=sorted({sum(b.role=='lv_branch' and b.transformer_id==t.id for b in feeder.buses) for t in feeder.transformers}),
        total_kw=total,pv_ratio=sum(l.pv_kw for l in feeder.loads)/total if total else None,
        mv_voltage_kv=sorted({b.voltage_kv for b in feeder.buses if b.voltage_kv>=1}),
        lv_voltage_kv=sorted({b.voltage_kv for b in feeder.buses if b.voltage_kv<1}),
        lv_installations=sorted(installations),region_installation={k:sorted(v) for k,v in installation_by_region.items()})


def score_requirements(case,feeder):
    observed=observed_requirements(feeder) if feeder is not None else {}
    checks=[]
    for r in case.requirements:
        actual=observed.get(r.field)
        if r.field=='region_installation':actual=(actual or {}).get(r.region_name)
        met=False
        if actual is not None:
            if r.field in ('lv_installations','region_installation'):
                expected=set(r.value if isinstance(r.value,list) else [r.value])
                met=bool(actual) and (set(actual)==expected if r.operator=='eq' else
                    expected.isdisjoint(actual) if r.operator=='excludes' else expected<=set(actual))
            else:
                values=actual if isinstance(actual,list) else [actual]
                met=bool(values) and all(isinstance(v,(int,float)) and math.isfinite(v) and (
                    abs(v-r.value)<=r.tolerance if r.operator=='eq' else
                    v>=r.value-r.tolerance if r.operator=='ge' else v<=r.value+r.tolerance) for v in values)
        checks.append(dict(**r.model_dump(),observed=actual,satisfied=met))
    return dict(checks=checks,satisfied=sum(c['satisfied'] for c in checks),total=len(checks),
        all_satisfied=bool(checks) and all(c['satisfied'] for c in checks))
