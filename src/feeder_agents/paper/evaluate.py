"""Score physical artifacts against private keys, never model explanations."""
from ..requirement_contract import model_observations,compare


def observations(domain,network,metadata=None,validation=None):
    return model_observations(domain,network,metadata,validation)


def score(task,observed):
    checks=[]
    for rule in task.requirements:
        actual=observed.get(rule.field);passed=compare(actual,rule.value,rule.op,rule.tolerance)
        checks.append(dict(**rule.model_dump(),observed=actual,passed=bool(passed)))
    return dict(all_satisfied=bool(checks) and all(c['passed'] for c in checks),checks=checks)


def aggregate(rows):
    groups={}
    for r in rows:groups.setdefault((r['domain'],r['track'],r['method']),[]).append(r)
    result=[]
    for (domain,track,method),items in sorted(groups.items()):
        generated=[r for r in items if r['expected']=='generate'];boundary=[r for r in items if r['expected']!='generate']
        rate=lambda seq,key:sum(bool(r.get(key)) for r in seq)/len(seq) if seq else None
        semantic={}
        for r in generated:semantic.setdefault(r['group'],[]).append(r['joint_success'])
        result.append(dict(domain=domain,track=track,method=method,generation_trials=len(generated),boundary_trials=len(boundary),
            joint_success_rate=rate(generated,'joint_success'),requirement_all_rate=rate(generated,'requirements_met'),
            benchmark_success_rate=rate(generated,'benchmark_success') if any('benchmark_success' in r for r in generated) else None,
            electrical_valid_rate=rate(generated,'electrical_valid'),export_reload_rate=rate(generated,'export_reload'),
            semantic_group_macro_success=sum(sum(v)/len(v) for v in semantic.values())/len(semantic) if semantic else None,
            boundary_status_match_rate=rate(boundary,'status_match'),false_generation_rate=rate(boundary,'generated'),
            boundary_semantic_correctness='pending human review' if boundary else None,failures=sum(r['status']=='error' for r in items),
            execution_errors=sum(r['status']=='error' for r in items),
            planning_failures=sum(r['status'] in ('planning_failed','task_mismatch') for r in items),
            generated_unaccepted=sum(bool(r.get('generated')) and not r.get('joint_success',False) for r in generated),
            unsuccessful_generation=sum(not r.get('joint_success',False) for r in generated)))
    return result
