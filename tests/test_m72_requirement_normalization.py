import pytest

from feeder_agents.requirement_contract import audit_ledger
from feeder_agents.requirement_normalization import normalize_ledger
from feeder_agents.requirements import RequirementLedger


def ledger(evidence, field, value, operator='eq', family='transmission', **extra):
    return RequirementLedger(network_kind='transmission' if family == 'transmission' else 'distribution',
        model_family=family, summary='test', requirements=[dict(id='r', segment_ids=[0], evidence=evidence,
        meaning='source requirement', priority='hard', disposition='supported', target_field=field,
        expected_value=value, operator=operator), *extra.get('requirements', [])])


@pytest.mark.parametrize('words,lower,upper', [('\u4e94\u5341\u591a\u4e2a\u6bcd\u7ebf',51,59), ('\u4e00\u767e\u591a\u4e2a\u8282\u70b9',101,199),
    ('\u4e09\u5341\u591a\u4e2a\u6bcd\u7ebf',31,39), ('200\u591a\u4e2a\u8282\u70b9',201,299)])
def test_count_band_adds_missing_bounds_without_erasing_existing_constraint(words, lower, upper):
    request='\u751f\u6210'+words+'。'
    record=ledger(words,'transmission.n_buses',lower+4,'ge')
    normalize_ledger(record,'transmission',request)
    bounds={(r.operator,r.expected_value) for r in record.requirements if r.priority=='hard'}
    assert ('ge',lower+4) in bounds
    assert ('le',upper) in bounds
    assert all(r.evidence in request for r in record.requirements)


def test_typed_bus_range_adds_both_bounds():
    record=ledger('35\u523039\u4e2a\u6bcd\u7ebf','transmission.n_buses',None)
    normalize_ledger(record,'transmission','\u751f\u621035\u523039\u4e2a\u6bcd\u7ebf\u3002')
    assert {('ge',35),('le',39)} <= {(r.operator,r.expected_value) for r in record.requirements}


def test_village_count_does_not_accept_branch_count_mapping():
    record=ledger('\u4e3b\u5e72\u8fde\u63a53\u4e2a\u6751\u843d','scenario.topology.branch_count',3,family='single_voltage')
    normalize_ledger(record,'single_voltage','\u4e3b\u5e72\u8fde\u63a53\u4e2a\u6751\u843d\u3002')
    assert record.requirements[0].target_field=='scenario.village_count'


@pytest.mark.parametrize('value,operator', [('selected','contains'), ({'selected':'buried_duct','reason':'invented'},'eq'), (None,'eq')])
def test_installation_explanation_observes_decision_without_locking_choice(value,operator):
    evidence='\u6577\u8bbe\u65b9\u5f0f\u548c\u8bbe\u5907\u5408\u7406\u9009\u62e9\u5e76\u89e3\u91ca'
    record=ledger(evidence,'hierarchy.lv_installation_decision',value,operator,family='hierarchical')
    normalize_ledger(record,'hierarchical',evidence+'。')
    assert record.requirements[0].expected_value is True
    assert record.requirements[0].operator=='eq'
    assert audit_ledger(record,{'lv_installation_decision':{'selected':'aerial_bundle','reason':'fits terrain'}},'plan')['all_satisfied']
    assert not audit_ledger(record,{'lv_installation_decision':{'selected':'aerial_bundle','reason':''}},'plan')['all_satisfied']
    assert not audit_ledger(record,{'lv_installation_decision':None},'plan')['all_satisfied']


@pytest.mark.parametrize('source', ['\u539f\u6765\u4e94\u5341\u591a\u4e2a\u6bcd\u7ebf\uff0c\u73b0\u5728\u6539\u4e3a70\u6bcd\u7ebf\u3002', '\u4e0d\u8981\u4e94\u5341\u591a\u4e2a\u6bcd\u7ebf\u3002',
    '\u201c\u4e94\u5341\u591a\u4e2a\u6bcd\u7ebf\u201d\u53ea\u662f\u5f15\u7528\u3002', '\u81f3\u5c11\u4e94\u5341\u591a\u4e2a\u6bcd\u7ebf\u3002'])
def test_noncurrent_or_ambiguous_band_is_not_compiled(source):
    record=ledger('\u4e94\u5341\u591a\u4e2a\u6bcd\u7ebf','n_buses',55,'ge')
    normalize_ledger(record,'transmission',source)
    assert len(record.requirements)==1


def test_conflicting_existing_hard_count_is_preserved_for_review():
    record=ledger('\u4e94\u5341\u591a\u4e2a\u6bcd\u7ebf','n_buses',60,'ge')
    normalize_ledger(record,'transmission','\u751f\u6210\u4e94\u5341\u591a\u4e2a\u6bcd\u7ebf\u3002')
    assert len(record.requirements)==1
    assert record.requirements[0].expected_value==60


def test_fixed_installation_is_not_reinterpreted_as_open_choice():
    evidence='\u5fc5\u987b\u5730\u4e0b\u6392\u7ba1\u6577\u8bbe\u5e76\u89e3\u91ca'
    record=ledger(evidence,'hierarchy.lv_installation_decision',{'selected':'buried_duct'},family='hierarchical')
    normalize_ledger(record,'hierarchical',evidence)
    assert record.requirements[0].expected_value=={'selected':'buried_duct'}


def test_compound_request_preserves_independent_fixed_installation():
    from feeder_agents.requirements import check_ledger_plan
    from feeder_agents.hierarchy import HierarchicalSpec
    evidence='\u6577\u8bbe\u65b9\u5f0f\u548c\u8bbe\u5907\u5408\u7406\u9009\u62e9\u5e76\u89e3\u91ca'
    fixed=dict(id='fixed',segment_ids=[1],evidence='\u5fc5\u987b\u5730\u4e0b\u6392\u7ba1\u6577\u8bbe',meaning='fixed construction',
        priority='hard',disposition='supported',target_field='hierarchy.lv_installation',expected_value='buried_duct')
    record=ledger(evidence,'hierarchy.lv_installation_decision',None,family='hierarchical',requirements=[fixed])
    request=evidence+'\u3002\u5fc5\u987b\u5730\u4e0b\u6392\u7ba1\u6577\u8bbe\u3002'
    normalize_ledger(record,'hierarchical',request)
    assert record.requirements[1].expected_value=='buried_duct'
    decision=dict(selected='buried_duct',reason='Grounded site analysis',
        factors=[dict(criterion='installation_requirement',observation='Buried duct is required',
                      origin='user',evidence='\u5fc5\u987b\u5730\u4e0b\u6392\u7ba1\u6577\u8bbe')],
        alternatives=[dict(installation=method,reason='Site comparison') for method in
                      ('aerial_bundle','buried_direct','buried_duct')])
    spec=HierarchicalSpec(lv_installation='buried_duct',lv_installation_decision=decision)
    brief=dict(status='ready',plan_type='hierarchical',plan={'spec':spec.model_dump()})
    check_ledger_plan(record,brief)
    assert brief['requirement_acceptance']['all_satisfied']


def test_negated_village_phrase_keeps_original_mapping_for_review():
    record=ledger('3\u4e2a\u6751\u843d','scenario.topology.branch_count',3,family='single_voltage')
    normalize_ledger(record,'single_voltage','\u4e0d\u89813\u4e2a\u6751\u843d\u3002')
    assert record.requirements[0].target_field=='scenario.topology.branch_count'


def test_compound_conflicting_bus_counts_stay_for_semantic_review():
    record=ledger('\u4e94\u5341\u591a\u4e2a\u6bcd\u7ebf','n_buses',55,'ge')
    normalize_ledger(record,'transmission','\u751f\u6210\u4e94\u5341\u591a\u4e2a\u6bcd\u7ebf\uff0c\u540c\u65f6\u8981\u6c4270\u4e2a\u6bcd\u7ebf\u3002')
    assert len(record.requirements)==1


@pytest.mark.parametrize('source', [
    '\u4e0d\u80fd\u91c7\u7528\u4e94\u5341\u591a\u4e2a\u6bcd\u7ebf\u3002',
    '\u201c\u4e94\u5341\u591a\u4e2a\u6bcd\u7ebf\u201d\u662f\u65e7\u65b9\u6848\u3002',
    '\u751f\u6210\u4e94\u5341\u591a\u4e2a\u6bcd\u7ebf\u3002\u73b0\u6539\u4e3a70\u4e2a\u6bcd\u7ebf\u3002',
    '\u751f\u6210\u4e94\u5341\u591a\u4e2a\u6bcd\u7ebf\u3002\u540c\u65f6\u5fc5\u987b70\u4e2a\u6bcd\u7ebf\u3002',
])
def test_noncurrent_band_in_full_request_remains_for_review(source):
    record=ledger('\u4e94\u5341\u591a\u4e2a\u6bcd\u7ebf','n_buses',55,'ge')
    normalize_ledger(record,'transmission',source)
    assert len(record.requirements)==1


def test_explicit_installation_in_same_clause_is_not_erased():
    evidence='\u6577\u8bbe\u65b9\u5f0f\u5408\u7406\u9009\u62e9\u5e76\u89e3\u91ca'
    record=ledger(evidence,'hierarchy.lv_installation_decision',
                  {'selected':'buried_duct','reason':'\u5fc5\u987b\u5730\u4e0b\u6392\u7ba1\u6577\u8bbe'},family='hierarchical')
    normalize_ledger(record,'hierarchical',evidence+'\uff0c\u5fc5\u987b\u5730\u4e0b\u6392\u7ba1\u6577\u8bbe\u3002')
    assert record.requirements[0].expected_value['selected']=='buried_duct'


def test_duplicate_long_identifier_gets_unique_bounded_suffix():
    identifier='n'*40
    record=ledger('\u4e94\u5341\u591a\u4e2a\u6bcd\u7ebf','n_buses',55,'ge')
    record.requirements[0].id=identifier
    normalize_ledger(record,'transmission','\u751f\u6210\u4e94\u5341\u591a\u4e2a\u6bcd\u7ebf\u3002')
    assert len({r.id for r in record.requirements})==len(record.requirements)
    assert len(record.requirements)==2


@pytest.mark.parametrize('source,want', [
    ('\u751f\u6210\u5168\u7f5157\u4e2a\u6bcd\u7ebf\u3002',{57}), ('\u751f\u6210123\u4e2a\u8282\u70b9\u3002',{123}),
    ('\u751f\u6210\u4e94\u5341\u4e03\u4e2a\u6bcd\u7ebf\u3002',{57}), ('\u4e2d\u538b57\u4e2a\u6bcd\u7ebf\uff0c\u5168\u7f51100\u4e2a\u6bcd\u7ebf\u3002',{100}),
    ('\u6bcf\u53f0\u914d\u53d857\u4e2a\u4f4e\u538b\u8282\u70b9\u3002',set()), ('\u751f\u621057\u523059\u4e2a\u6bcd\u7ebf\u3002',set()),
    ('\u81f3\u5c1157\u4e2a\u6bcd\u7ebf\u3002',set()), ('\u65e7\u65b9\u684857\u4e2a\u6bcd\u7ebf\uff0c\u6539\u4e3a61\u4e2a\u6bcd\u7ebf\u3002',{61}),
    ('\u751f\u621057\u4e2a\u6bcd\u7ebf\uff0c\u540c\u65f6\u5fc5\u987b61\u4e2a\u6bcd\u7ebf\u3002',{57,61}),
])
def test_exact_bus_counts_only_returns_current_total_counts(source,want):
    from feeder_agents.source_review import exact_bus_counts
    assert exact_bus_counts(source)==want


def test_exact_source_bus_count_rejects_ledger_or_plan_widening():
    from feeder_agents.source_review import check_source_numbers
    from feeder_agents.planning import build_plan
    from feeder_agents.planning_diagnostics import PlanningValidationError
    plan=build_plan('test',{'n_buses':58})
    brief=dict(plan_type='feeder',plan=plan.model_dump(),requirement_ledger=dict(requirements=[
        dict(id='count',target_field='n_buses',expected_value=57,operator='ge',priority='hard')]))
    with pytest.raises(PlanningValidationError):check_source_numbers('\u751f\u621057\u4e2a\u6bcd\u7ebf\u3002',brief)
    brief['plan']['spec']['n_buses']=57
    with pytest.raises(PlanningValidationError):check_source_numbers('\u751f\u621057\u4e2a\u6bcd\u7ebf\u3002',brief)


@pytest.mark.parametrize('evidence,count', [('57\u4e2a\u6bcd\u7ebf',57),('123\u4e2a\u8282\u70b9',123),('\u4e94\u5341\u4e03\u4e2a\u6bcd\u7ebf',57)])
def test_exact_bus_count_adds_equality_without_widening_existing_hard_bound(evidence,count):
    record=ledger(evidence,'n_buses',count,'ge')
    normalize_ledger(record,'transmission','\u751f\u6210'+evidence+'。')
    assert ('eq',count) in {(r.operator,r.expected_value) for r in record.requirements}
    assert ('ge',count) in {(r.operator,r.expected_value) for r in record.requirements}


def test_exact_count_conflict_keeps_original_hard_entry():
    record=ledger('57\u4e2a\u6bcd\u7ebf','n_buses',60,'ge')
    normalize_ledger(record,'transmission','\u751f\u621057\u4e2a\u6bcd\u7ebf\u3002')
    assert [(r.operator,r.expected_value) for r in record.requirements]==[('ge',60)]


@pytest.mark.parametrize('source', [
    '\u751f\u621057\u4e2a\u6bcd\u7ebf\u4ee5\u4e0a\u3002','\u751f\u621057\u4e2a\u6bcd\u7ebf\u4ee5\u4e0b\u3002','\u9700\u898157\u4e2a\u6bcd\u7ebf\u5de6\u53f3\u3002',
    '\u4e0d\u5141\u8bb857\u4e2a\u6bcd\u7ebf\u3002','\u751f\u621057\u4e2a\u6bcd\u7ebf\u621661\u4e2a\u6bcd\u7ebf\u5747\u53ef\u3002',
])
def test_qualified_or_negated_exact_counts_are_not_equalities(source):
    from feeder_agents.source_review import exact_bus_counts
    assert exact_bus_counts(source)==set()


def test_quoted_old_exact_count_is_not_current():
    from feeder_agents.source_review import exact_bus_counts
    assert exact_bus_counts('\u201c57\u4e2a\u6bcd\u7ebf\u201d\u662f\u65e7\u65b9\u6848\uff0c\u751f\u621061\u4e2a\u6bcd\u7ebf\u3002')=={61}


@pytest.mark.parametrize('source,want', [('\u8282\u70b9\u6570\u4e3a57\u3002',57),('\u603b\u6bcd\u7ebf\u6570=123\u3002',123),('\u603b\u8282\u70b9\u657057\u3002',57)])
def test_exact_count_supports_count_noun_before_number(source,want):
    from feeder_agents.source_review import exact_bus_counts
    assert exact_bus_counts(source)=={want}


def test_decimal_bus_count_is_not_integer_exact_source():
    from feeder_agents.source_review import exact_bus_counts
    assert exact_bus_counts('\u751f\u621057.5\u4e2a\u6bcd\u7ebf\u3002')==set()


def test_extra_decision_criterion_in_same_clause_is_not_dropped():
    evidence='\u6577\u8bbe\u65b9\u5f0f\u5408\u7406\u9009\u62e9\u5e76\u89e3\u91ca'
    record=ledger(evidence,'hierarchy.lv_installation_decision',{'factors':['environment']},family='hierarchical')
    normalize_ledger(record,'hierarchical',evidence+'\uff0c\u5fc5\u987b\u6bd4\u8f83\u73af\u5883\u5f71\u54cd\u3002')
    assert record.requirements[0].expected_value=={'factors':['environment']}


def test_existing_structured_range_is_compiled_only_when_source_confirms():
    record=ledger('\u56db\u5341\u591a\u4e2a\u6bcd\u7ebf','n_buses',{'ge':41,'le':49},'eq')
    normalize_ledger(record,'transmission','\u751f\u6210\u56db\u5341\u591a\u4e2a\u6bcd\u7ebf\u3002')
    assert {('ge',41),('le',49)} <= {(r.operator,r.expected_value) for r in record.requirements if isinstance(r.expected_value,int)}


@pytest.mark.parametrize('source', [
    '\u5168\u7f5157\u4e2a\u6bcd\u7ebf\u3002\u4e2d\u538b\u6539\u4e3a9\u4e2a\u6bcd\u7ebf\u3002',
    '\u5168\u7f5157\u4e2a\u6bcd\u7ebf\uff0c\u4e2d\u538b\u6539\u4e3a9\u4e2a\u6bcd\u7ebf\u3002',
])
def test_regional_revision_keeps_current_total_count(source):
    from feeder_agents.source_review import exact_bus_counts
    assert exact_bus_counts(source)=={57}


def test_noun_first_range_is_not_exact_count():
    from feeder_agents.source_review import exact_bus_counts
    assert exact_bus_counts('\u8282\u70b9\u6570\u4e3a57\u523059\u3002')==set()


def test_ascii_quoted_count_is_not_instruction():
    from feeder_agents.source_review import exact_bus_counts
    assert exact_bus_counts('\u53c2\u8003\u6807\u9898\u5199\u7740"57\u4e2a\u6bcd\u7ebf"\u3002')==set()


def test_not_allowed_count_band_is_not_compiled():
    record=ledger('\u4e94\u5341\u591a\u4e2a\u6bcd\u7ebf','n_buses',55,'ge')
    normalize_ledger(record,'transmission','\u4e0d\u5141\u8bb8\u4e94\u5341\u591a\u4e2a\u6bcd\u7ebf\u3002')
    assert len(record.requirements)==1


@pytest.mark.parametrize('source', [
    '\u4e0d\u8981\u4e94\u5341\u591a\u4e2a\u6bcd\u7ebf\u3002\u751f\u621070\u4e2a\u6bcd\u7ebf\u3002',
    '\u4e0d\u80fd\u91c7\u7528\u4e94\u5341\u591a\u4e2a\u6bcd\u7ebf\u3002\u751f\u621070\u4e2a\u6bcd\u7ebf\u3002',
    '\u201c\u4e94\u5341\u591a\u4e2a\u6bcd\u7ebf\u201d\u53ea\u662f\u5f15\u7528\u3002\u751f\u621070\u4e2a\u6bcd\u7ebf\u3002',
    '\u751f\u6210\u4e94\u5341\u591a\u4e2a\u6bcd\u7ebf\u3002\u73b0\u6539\u4e3a70\u4e2a\u6bcd\u7ebf\u3002',
])
def test_source_review_ignores_rejected_or_superseded_band(source):
    from feeder_agents.source_review import check_source_numbers
    from feeder_agents.planning import build_plan
    from feeder_agents.planning_diagnostics import PlanningValidationError
    def candidate(count):
        return dict(plan_type='feeder',plan=build_plan('test',{'n_buses':count}).model_dump(),
            requirement_ledger=dict(requirements=[dict(id='current',target_field='n_buses',
                expected_value=70,operator='eq',priority='hard')]))
    check_source_numbers(source,candidate(70))
    with pytest.raises(PlanningValidationError):
        check_source_numbers(source,candidate(71))


@pytest.mark.parametrize('case,seed,expected', [('H047_w1','1709',(41,49)),('T123_w1','2713',(101,199))])
def test_saved_replay_structured_count_range_compiles_to_source_bounds(case,seed,expected):
    import json
    from pathlib import Path
    from feeder_agents.requirements import RequirementLedger
    root=Path(__file__).resolve().parent/'fixtures/m72_planning_replay'
    saved=json.loads((root/case/seed/'failure_analysis'/'attempt_01.json').read_text())
    record=RequirementLedger.model_validate(saved['candidate']['ledger'])
    normalize_ledger(record,record.model_family,saved['request'])
    bounds={(r.operator,r.expected_value) for r in record.requirements if isinstance(r.expected_value,int)
            and r.target_field in ('hierarchy.n_buses','transmission.n_buses')}
    assert {('ge',expected[0]),('le',expected[1])}<=bounds
    assert all(r.evidence in saved['request'] for r in record.requirements)


@pytest.mark.parametrize('case,seed,villages', [('R035_w0','2713',3),('R058_w0','1709',3),('R073_w1','3911',4)])
def test_saved_replay_village_quote_does_not_freeze_topology_family(case,seed,villages):
    import json
    from pathlib import Path
    from feeder_agents.requirements import RequirementLedger
    root=Path(__file__).resolve().parent/'fixtures/m72_planning_replay'
    saved=json.loads((root/case/seed/'failure_analysis'/'attempt_01.json').read_text())
    record=RequirementLedger.model_validate(saved['candidate']['ledger'])
    normalize_ledger(record,record.model_family,saved['request'])
    assert not any(r.target_field in ('scenario.topology.family','scenario.topology.branch_count')
                   and '\u6751\u843d' in r.evidence for r in record.requirements)
    assert any(r.target_field=='scenario.village_count' and r.expected_value==villages
               for r in record.requirements)
    if case=='R058_w0':
        assert any(r.target_field=='operating_topology' and r.expected_value=='radial'
                   for r in record.requirements)
    else:
        assert any(r.target_field=='scenario.tie_count' and r.expected_value==0
                   for r in record.requirements)
    assert all(r.evidence in saved['request'] for r in record.requirements)


@pytest.mark.parametrize('value', [{'ge':42,'le':49},[41,50]])
def test_mismatched_structured_band_stays_for_review(value):
    record=ledger('\u56db\u5341\u591a\u4e2a\u6bcd\u7ebf','n_buses',value,'ge')
    normalize_ledger(record,'transmission','\u751f\u6210\u56db\u5341\u591a\u4e2a\u6bcd\u7ebf\u3002')
    assert len(record.requirements)==1 and record.requirements[0].expected_value==value


def test_explicit_family_and_extra_branch_count_remain_hard():
    extras=[dict(id='family',segment_ids=[0],evidence='\u91c7\u7528\u591a\u5206\u652f\u62d3\u6251',meaning='fixed topology',
        priority='hard',disposition='supported',target_field='scenario.topology.family',
        expected_value='multi_branch'),
        dict(id='branches',segment_ids=[0],evidence='\u53e6\u5916\u8bbe\u7f6e2\u6761\u652f\u7ebf',meaning='extra branches',
        priority='hard',disposition='supported',target_field='scenario.topology.branch_count',
        expected_value=2)]
    record=ledger('\u4e3b\u5e72\u8fde\u63a53\u4e2a\u6751\u843d','scenario.topology.family','multi_branch',family='single_voltage',requirements=extras)
    normalize_ledger(record,'single_voltage','\u4e3b\u5e72\u8fde\u63a53\u4e2a\u6751\u843d\uff1b\u91c7\u7528\u591a\u5206\u652f\u62d3\u6251\uff1b\u53e6\u5916\u8bbe\u7f6e2\u6761\u652f\u7ebf\u3002')
    assert record.requirements[0].target_field=='scenario.village_count'
    assert record.requirements[1].target_field=='scenario.topology.family'
    assert record.requirements[2].target_field=='scenario.topology.branch_count'


def test_explicit_decision_criterion_in_next_sentence_is_not_dropped():
    evidence='\u6577\u8bbe\u65b9\u5f0f\u5408\u7406\u9009\u62e9\u5e76\u89e3\u91ca'
    record=ledger(evidence,'hierarchy.lv_installation_decision',{'factors':['environment']},family='hierarchical')
    normalize_ledger(record,'hierarchical',evidence+'\u3002\u5fc5\u987b\u6bd4\u8f83\u73af\u5883\u5f71\u54cd\u3002')
    assert record.requirements[0].expected_value=={'factors':['environment']}


def test_structured_band_keeps_stronger_independent_hard_bound():
    tighter=dict(id='chosen',segment_ids=[0],evidence='\u9009\u62e945\u4e2a\u6bcd\u7ebf',meaning='fixed chosen count',
        priority='hard',disposition='supported',target_field='n_buses',expected_value=45,operator='eq')
    record=ledger('\u56db\u5341\u591a\u4e2a\u6bcd\u7ebf','n_buses',{'ge':41,'le':49},'ge',requirements=[tighter])
    normalize_ledger(record,'transmission','\u751f\u6210\u56db\u5341\u591a\u4e2a\u6bcd\u7ebf\uff1b\u9009\u62e945\u4e2a\u6bcd\u7ebf\u3002')
    assert {('ge',41),('le',49),('eq',45)} <= {(r.operator,r.expected_value) for r in record.requirements
        if isinstance(r.expected_value,int)}


@pytest.mark.parametrize('evidence,value', [
    ('\u751f\u6210\u4e94\u5341\u591a\u4e2a\u6bcd\u7ebf\uff0c\u540c\u65f6\u8981\u6c4270\u4e2a\u6bcd\u7ebf',55),
    ('\u751f\u621035\u523039\u4e2a\u6bcd\u7ebf\uff0c\u540c\u65f6\u8981\u6c4270\u4e2a\u6bcd\u7ebf',35),
])
def test_compound_quote_with_conflicting_bus_count_is_not_normalized(evidence,value):
    record=ledger(evidence,'n_buses',value,'ge')
    normalize_ledger(record,'transmission',evidence+'。')
    assert len(record.requirements)==1
