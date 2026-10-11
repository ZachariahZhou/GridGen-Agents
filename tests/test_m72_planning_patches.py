import copy
import json

import pytest

from feeder_agents.artifacts import digest
from feeder_agents.adaptive_planning import adaptive_plan
from test_m52_diagnostic_loop import Model, proposal, analyses


def patch(base, edits):
    return dict(status='ready', family=base['family'], base_candidate_hash=digest(base), edits=edits)


def normalized_base(users=15):
    from feeder_agents.adaptive_planning import AdaptiveProposal, _compile
    p=AdaptiveProposal.model_validate(proposal(users))
    try:_compile(p,'12\u4e2a\u7528\u6237')
    except ValueError:pass
    return p.model_dump()


def test_patch_only_changes_user_count_and_recomputes_total(tmp_path):
    base=normalized_base()
    edit=dict(path='distribution_spec.users', value=12)
    result=adaptive_plan('12\u4e2a\u7528\u6237',tmp_path,Model([proposal(15),patch(base,[edit])]))
    assert result['status']=='ready'
    assert result['plan']['spec']['users']==12
    assert result['plan']['spec']['n_buses']==base['distribution_spec']['n_buses']-3
    assert result['plan']['spec']['total_kw']==base['distribution_spec']['total_kw']
    record=analyses(tmp_path)[0]
    assert record['verification']['status']=='passed'
    assert record['adjustment']['protocol']=='scoped_patch'


def test_stale_patch_does_not_mutate_base(tmp_path):
    base=normalized_base();bad=patch(base,[dict(path='distribution_spec.users',value=12)])
    bad['base_candidate_hash']='0'*64
    fixed=patch(base,[dict(path='distribution_spec.users',value=12)])
    result=adaptive_plan('12\u4e2a\u7528\u6237',tmp_path,Model([proposal(15),bad,fixed]))
    assert result['status']=='ready'
    assert any(e['stage']=='patch_rejected' for e in result['adaptive_trace']['events'])
    assert result['plan']['spec']['total_kw']==96


def test_illegal_patch_is_rejected_then_legal_patch_uses_original_base(tmp_path):
    base=normalized_base()
    bad=patch(base,[dict(path='distribution_spec.users',value=12),dict(path='distribution_spec.total_kw',value=12)])
    good=patch(base,[dict(path='distribution_spec.users',value=12)])
    result=adaptive_plan('12\u4e2a\u7528\u6237',tmp_path,Model([proposal(15),bad,good]))
    assert result['status']=='ready' and result['plan']['spec']['total_kw']==96
    rejected=[e for e in result['adaptive_trace']['events'] if e['stage']=='patch_rejected']
    assert len(rejected)==1 and 'total_kw' in rejected[0]['error']


def test_patch_cannot_remove_hard_ledger(tmp_path):
    base=normalized_base()
    bad=patch(base,[dict(path='ledger.requirements',operation='remove')])
    result=adaptive_plan('12\u4e2a\u7528\u6237',tmp_path,Model([proposal(15),bad,bad]))
    assert result['status']=='planning_failed'
    assert result['adaptive_trace']['stop_reason']=='patch_attempt_limit'


def test_initial_patch_has_no_authorized_base(tmp_path):
    result=adaptive_plan('12\u4e2a\u7528\u6237',tmp_path,Model([patch(normalized_base(),[dict(path='distribution_spec.users',value=12)])]))
    assert result['status']=='planning_failed'
    assert result['adaptive_trace']['stop_reason']=='unbound_patch'


def test_patch_cannot_reset_explicit_bus_requirement(tmp_path):
    base=normalized_base();base['ledger']['requirements'].append(dict(id='fixed_n',segment_ids=[1],
        evidence='\u603b\u6bcd\u7ebf31\u4e2a',meaning='\u56fa\u5b9a\u603b\u6570',priority='hard',disposition='supported',target_field='hierarchy.n_buses',expected_value=31,operator='eq',reason=''))
    from feeder_agents.planning_patches import apply_scoped_patch
    with pytest.raises(ValueError):
        apply_scoped_patch(base,[dict(path='distribution_spec.users',value=12)],
            dict(id='align_spec',paths=['distribution_spec.users']),digest(base))


def test_patch_only_response_rejects_mixed_full_proposal():
    from feeder_agents.adaptive_planning import AdaptiveProposal
    value=proposal();value.update(edits=[dict(path='distribution_spec.users',value=12)],base_candidate_hash='a'*64)
    with pytest.raises(ValueError,match='patch|Patch'):
        AdaptiveProposal.model_validate(value)


def test_overlapping_and_duplicate_patch_paths_rejected():
    from feeder_agents.planning_patches import apply_scoped_patch
    base=normalized_base();action=dict(id='repair_representation',paths=['distribution_spec'])
    with pytest.raises(ValueError,match='overlap|duplicate'):
        apply_scoped_patch(base,[dict(path='distribution_spec.users',value=12),dict(path='distribution_spec.users',value=15)],action,digest(base))


def test_schema_valid_patch_failing_contract_rolls_back_before_next_patch(tmp_path):
    base=normalized_base()
    wrong=patch(base,[dict(path='distribution_spec.users',value=13)])
    correct=patch(base,[dict(path='distribution_spec.users',value=12)])
    result=adaptive_plan('12\u4e2a\u7528\u6237',tmp_path,Model([proposal(15),wrong,correct]))
    assert result['status']=='ready' and result['plan']['spec']['users']==12
    assert analyses(tmp_path)[0]['candidate']['distribution_spec']['users']==15
    assert analyses(tmp_path)[0]['rejected_patches'][0]['base_unchanged'] is True


def test_free_mv_count_patch_recomputes_derived_total():
    from feeder_agents.planning_patches import apply_scoped_patch
    from feeder_agents.adaptive_planning import AdaptiveProposal
    base=AdaptiveProposal.model_validate(proposal()).model_dump()
    after=apply_scoped_patch(base,[dict(path='distribution_spec.mv_buses',value=9)],
        dict(id='align_spec',paths=['distribution_spec.mv_buses']),digest(base))
    parsed=AdaptiveProposal.model_validate(after)
    assert parsed.distribution_spec.mv_buses==9
    assert parsed.distribution_spec.n_buses==30
    assert parsed.distribution_spec.users==12


def test_combined_root_schema_and_malformed_ledger_is_a_bounded_diagnosis():
    from test_m71_rural_diagnostic import candidate, SOURCE
    from feeder_agents.planning_diagnostics import failure_bundle
    from feeder_agents.structured_planning import StructuredPlanningError
    from feeder_agents.adaptive_planning import AdaptiveProposal
    from pydantic import ValidationError
    broken=candidate();broken['ledger']='{invalid json'
    with pytest.raises(ValidationError) as exc:AdaptiveProposal.model_validate(broken)
    bundle=failure_bundle(StructuredPlanningError(str(exc.value),exc.value),'schema',SOURCE,broken)
    assert bundle['code']=='schema_error'
    assert bundle['allowed_actions'][-1]['id']=='stop'
