import pytest
from feeder_agents.hierarchy import HierarchicalSpec, generate_hierarchy, export_hierarchy, evaluate_hierarchy
from feeder_agents.hierarchy_actions import apply_action, contract_preserved
from feeder_agents.simulation import simulate


def test_public_products_are_selected_by_role_and_actual_demand(tmp_path):
    from feeder_agents.lv_equipment import catalogue, next_lv_upgrade
    assert sum(p['construction']=='overhead' for p in catalogue().values()) == 11
    spec = HierarchicalSpec(lv_equipment_profile='nexans_abc',users=24,total_kw=96)
    feeder = generate_hierarchy(spec,42)
    rows=feeder.design_evidence['lv_equipment']['lines']
    assert len(rows)==30
    for line in feeder.lines:
        if line.id not in rows: continue
        code=feeder.equipment_catalog[line.conductor]
        assert code['cores']==(2 if len(line.phases)==1 else 4)
        assert line.construction=='overhead'
        assert code['normamps']*spec.loading_margin>=rows[line.id]['design_current_a']
        assert rows[line.id]['estimated_drop_pu']<=spec.lv_drop_budget_pu/2
        assert len(code['source_sha256'])==64
    result=simulate(export_hierarchy(feeder,tmp_path))
    assert evaluate_hierarchy(feeder,spec,result)['accepted']
    line=next(e for e in feeder.lines if e.id.startswith('service'))
    replacement=next_lv_upgrade(feeder,line)
    action=dict(kind='upgrade_lv_line',component=line.id,replacement=replacement)
    with pytest.raises(ValueError):apply_action(feeder,action,[])
    changed=apply_action(feeder,action,['upgrade_lv_line'])
    assert contract_preserved(feeder,changed)
    assert changed.equipment_catalog[replacement]['normamps']>feeder.equipment_catalog[line.conductor]['normamps']


def test_lv_capacity_exhaustion_is_not_silently_accepted():
    with pytest.raises(ValueError,match='LV catalogue exhausted'):
        generate_hierarchy(HierarchicalSpec(transformer_count=1,users=2,lv_branches=2,total_kw=400),42)


def test_longer_service_requires_larger_section_and_legacy_remains_available():
    a=generate_hierarchy(HierarchicalSpec(lv_equipment_profile='nexans_abc',users=24,total_kw=96),42)
    b=generate_hierarchy(HierarchicalSpec(lv_equipment_profile='nexans_abc',users=24,total_kw=96,service_km_min=.1,service_km_max=.1),42)
    def sizes(f):return sum(f.equipment_catalog[e.conductor]['section_mm2'] for e in f.lines if e.id.startswith('service'))
    assert sizes(b)>sizes(a)
    old=generate_hierarchy(HierarchicalSpec(lv_equipment_profile='legacy_epri'),42)
    assert any(e.conductor.startswith('lv_ckt7') for e in old.lines)


def test_measured_lv_violation_proposes_only_authorized_lv_upgrade():
    from feeder_agents.hierarchy_actions import propose_actions
    from feeder_agents.hierarchy_inverse import HierarchySearch
    feeder=generate_hierarchy(HierarchicalSpec(),42)
    issue=dict(deficit=.2,witness='service_1',condition='evening',metric='max_line_loading_ratio',operator='le',threshold=.8,actual=1.,phase=1)
    search=HierarchySearch(allowed_actions=['upgrade_lv_line'])
    actions=propose_actions(feeder,dict(constraints=[issue]),search)
    assert actions and all(a['kind']=='upgrade_lv_line' or a['kind']=='joint_local' for a in actions)
    changed=apply_action(feeder,actions[0],search.allowed_actions)
    assert changed.design_evidence['lv_equipment']['lines']['service_1']['product']!=feeder.design_evidence['lv_equipment']['lines']['service_1']['product']
