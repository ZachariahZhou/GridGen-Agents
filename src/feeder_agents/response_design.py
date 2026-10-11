"""Bounded research-response synthesis with immutable goals and verified edits."""
import copy
import csv
import fcntl
import importlib.metadata
import json
import math
import random
import re
import zipfile
from itertools import zip_longest, combinations
from pathlib import Path

import networkx as nx
from pydantic import Field

from .artifacts import atomic_json, digest, file_digest
from .response_measurement import create_model, resolve_probes, measure
from .response_schema import ResponseDesignPlan
from .schemas import StrictModel
from .response_distribution import conductor_options, layout_options, catalogue_diagnosis
from .solver_accounting import SolverMeter, solver_phase, ACSearchBudgetExceeded
from .response_search import (physical_fingerprint, exploration_eligible,
                              response_priority, retain_archive, learn_proxy_gains)


def protected_fingerprint(model):
    if model['kind'] == 'distribution':
        value = model['feeder'].model_dump()
        value.pop('design_evidence'); value.pop('assumptions')
        for line in value['lines'] + value['tie_lines']: line.pop('conductor')
        return digest(value)
    from .transmission import case_payload
    case = case_payload(model['case'])
    # BS is derived from charging and the frozen compensation fraction by the
    # existing parallel-circuit transformation; it is not independently tuned.
    for bus in case['bus']: bus[5] = 0
    for branch in case['branch']:
        for col in (2, 3, 4, 5, 6, 7): branch[col] = 0
    meta = copy.deepcopy(model['metadata'])
    for branch in meta['branch_evidence']: branch.pop('circuits', None)
    return digest(dict(case=case, metadata=meta))


def research_contract_preserved(original, trial, plan):
    if trial['kind'] != original['kind']: return False
    if original['kind'] == 'distribution':
        from .response_contract import distribution_contract
        return distribution_contract(original, trial, plan)
    return protected_fingerprint(original) == protected_fingerprint(trial)


def score_response(measurement, targets, reference):
    scored = dict(measurement); rows = []; deficits = []
    for target in targets:
        value = measurement['probes'][target.probe_id]['value']
        denominator = reference['probes'][target.probe_id]['value'] if target.reference == 'baseline_ratio' else 1
        if denominator is None or denominator <= 1e-12:
            raise ValueError('Baseline-relative target has zero or invalid reference response')
        observed = value/denominator if value is not None else None
        scale = max(target.lower or 0, target.upper or 0, 1e-9)
        deficit = (max((target.lower or 0)-observed, observed-(target.upper if target.upper is not None else math.inf), 0)/scale
                   if observed is not None else 1e12)
        if deficit < 1e-8: deficit = 0.
        deficits.append(deficit)
        rows.append(dict(target=target.model_dump(), observed=observed, deficit=deficit, passed=deficit == 0))
    scored.update(targets=rows, deficits=deficits,
                  target_met=bool(measurement['base_accepted'] and measurement['valid'] and not any(deficits)))
    return scored


def accept_candidate(before, after, preserved):
    """No material deficit regression; final target thresholds stay unchanged.

    Central AC differences exhibit small numerical cross-port variation.
    The 1e-6 normalized search tolerance does not relax score_response or the
    independently verified delivery target, including already-met targets.
    """
    return bool(preserved and after['base_accepted'] and after['valid']
        and len(before['deficits']) == len(after['deficits'])
        and all(a <= b+1e-6 for a, b in zip(after['deficits'], before['deficits']))
        and (sum(after['deficits']) < sum(before['deficits'])-1e-9 or not before['base_accepted']))


def _distribution_options(model, spec, probes, assessment):
    return conductor_options(model, spec, probes, assessment)


def propose_response_actions(model, spec, probes, assessment, search, original=None, audit=None, *, remaining_rounds=None, compensation_audit=None, seen_physical=None, candidate_filter=None):
    if model['kind'] == 'distribution':
        actions = _distribution_options(model, spec, probes, assessment) if 'replace_conductor' in search.allowed_actions else []
        if not assessment['base_accepted'] and 'replace_conductor' in search.allowed_actions:
            from .repairs import candidates
            for item in candidates(model['feeder'], spec, assessment['base_simulation'], assessment['base_validation']['checks']):
                if item.action == 'upgrade_conductor':
                    actions.insert(0, dict(kind='replace_conductor', line_id=item.line_id,
                        conductor=item.conductor, proxy_effect=item.priority,
                        diagnosis='Existing voltage/ampacity diagnosis: '+item.rationale))
        if 'scale_layout' in search.allowed_actions:
            if original is None: raise ValueError('Layout proposals require the original baseline')
            # Reserve candidates for the alternate authorized control, instead
            # of letting many similar conductor moves consume the whole preview.
            layout = layout_options(model, original, spec, assessment, search)
            actions = [x for pair in zip_longest(layout, actions) for x in pair if x is not None]
        if set(search.allowed_actions)&{'scale_subtree','rewire_branch','redistribute_load'}:
            if original is None:raise ValueError('Local proposals require the original baseline')
            from .response_flexibility import distribution_actions
            local=distribution_actions(model,original,spec,probes,assessment,search)
            # Interleave families and target ports; one abundant family must
            # not suppress all other design freedoms before AC verification.
            groups={}
            for action in actions+local:
                groups.setdefault((action['kind'],action.get('probe_id')),[]).append(action)
            actions=[a for row in zip_longest(*groups.values()) for a in row if a is not None]
    else:
        actions = []
        if 'parallel_line' in search.allowed_actions:
            impact = {}
            for p in assessment['probes'].values():
                for index, value in p.get('signed_response', {}).items():
                    impact[int(index)] = max(impact.get(int(index), 0), abs(value))
            for i, e in enumerate(model['metadata']['branch_evidence']):
                if e['kind'] == 'line' and e['circuits'] < 4:
                    actions.append(dict(kind='parallel_line', index=i, value=e['circuits']+1,
                        proxy_effect=impact.get(i, 0), diagnosis='Add a complete parallel circuit; retain the fixed charging compensation fraction; verify AC transfer response'))
            from .response_transmission import rank_parallel_actions
            actions = rank_parallel_actions(model, probes, assessment, actions,
                lookahead=1 if search.selector == 'greedy' else min(2, search.max_rounds if remaining_rounds is None else remaining_rounds))
    if model['kind'] == 'distribution':
        from .response_distribution import rank_distribution_actions
        actions = rank_distribution_actions(model, probes, assessment, actions, preserve_diversity=True)
    unique = {}
    for action in actions:
        key = digest({k: v for k, v in action.items() if k not in ('diagnosis', 'proxy_effect', 'dc_predicted_deficit', 'dc_response_ratios', 'dc_lookahead_deficit', 'dc_lookahead_action', 'proxy_predicted_deficits', 'proxy_response_estimates', 'proxy_raw_response_estimates')})[:16]
        unique.setdefault(key, dict(action, action_id=key))
    elementary=list(unique.values())
    if search.max_joint_actions>1:
        # A coordinated preview can improve coupled goals when isolated moves
        # would disturb a currently satisfied target. Acceptance is unchanged.
        joint=[]
        for a,b in combinations(elementary[:min(12,len(elementary))],2):
            if a['kind']==b['kind'] and any(a.get(k) is not None and a.get(k)==b.get(k) for k in ('line_id','child_bus','index')):continue
            action=dict(kind='compound',actions=[a,b],diagnosis='Joint preview of two permitted moves; validate every target and the combined original-reference budget')
            joint.append(dict(action,action_id=digest(action)[:16]))
        from .response_compensation import compensation_actions, conditional_compensation_actions
        compensation=[]
        counter_moves=compensation_actions(model,original,spec,probes,assessment,search)
        for a in elementary[:12]:
            for b in counter_moves:
                if a.get('probe_id')==b.get('probe_id'):continue
                action=dict(kind='compound',actions=[a,b],diagnosis='Joint response adjustment with a compensating move at an already-met port; evaluate only the original targets')
                compensation.append(dict(action,action_id=digest(action)[:16]))
        mode=search.compensation_mode
        fitted = (conditional_compensation_actions(model, probes, assessment, elementary[:12], counter_moves,
            original=original if mode=='bounded' else None,spec=spec,search=search,audit=compensation_audit)
            if mode!='fixed' else [])
        fitted = [dict(a, action_id=digest(a)[:16]) for a in fitted]
        actions=[a for row in zip_longest(elementary,joint,compensation,fitted) for a in row if a is not None]
    else:actions=elementary
    if candidate_filter is not None:
        actions=[action for action in actions if candidate_filter(action)]
    if model['kind']=='distribution' and original is not None:
        actions = rank_distribution_actions(model, probes, assessment, actions)
        # Cheap structural/permission screening precedes the preview budget.
        from types import SimpleNamespace
        contract=SimpleNamespace(search=search,base_spec=spec)
        from .response_candidates import diverse_directions
        actions=diverse_directions(actions)
        screened=[];fingerprints=set(seen_physical or ())
        fingerprints.add(physical_fingerprint(model))
        for action in actions:
            try:
                trial=apply_response_action(model,spec,action)
                preserved=research_contract_preserved(original,trial,contract)
                identity=physical_fingerprint(trial) if preserved else None
                if preserved and identity in fingerprints:
                    if audit is not None:audit.append(dict(action=action,eligible_for_ac=False,
                        reason='duplicate_before_preview',physical_hash=identity))
                    continue
                if audit is not None:audit.append(dict(action=action,eligible_for_ac=preserved,reason='Original-reference contract satisfied' if preserved else 'Edit budget or structural contract failed'))
                if preserved:
                    fingerprints.add(identity);screened.append(action)
            except (ValueError,KeyError,nx.NetworkXException) as exc:
                if audit is not None:audit.append(dict(action=action,eligible_for_ac=False,reason=type(exc).__name__+': '+str(exc)))
                continue
            if len(screened)>=search.candidates_per_round:break
        return screened
    return actions[:search.candidates_per_round]


def apply_response_action(model, spec, action):
    if action['kind']=='compound':
        if not 1<len(action['actions'])<=2 or any(a['kind']=='compound' for a in action['actions']):raise ValueError('Invalid compound action')
        trial=model
        for elementary in action['actions']:trial=apply_response_action(trial,spec,elementary)
        return trial
    if action['kind'] in {'scale_subtree','rewire_branch','redistribute_load'}:
        from .response_flexibility import apply_distribution_action
        return apply_distribution_action(model,spec,action)
    trial = copy.deepcopy(model)
    if action['kind'] == 'replace_conductor':
        replacements = action.get('replacements') or [action]
        for replacement in replacements:
            line = next(e for e in trial['feeder'].lines if e.id == replacement['line_id'])
            line.conductor = replacement['conductor']
    elif action['kind'] == 'scale_layout':
        feeder = trial['feeder']; factor = action['factor']
        source = next(b for b in feeder.buses if b.id == feeder.source_bus)
        x, y = source.x_km, source.y_km
        for bus in feeder.buses:
            bus.x_km = x+(bus.x_km-x)*factor
            bus.y_km = y+(bus.y_km-y)*factor
        for line in feeder.lines+feeder.tie_lines: line.length_km *= factor
        feeder.design_evidence['response_layout_scaling'] = dict(original_scale=action['original_scale'],
            basis='Explicit research permission; uniform scale of coordinates and lengths about fixed source')
    else:
        from .transmission_feedback import apply
        trial['case'], trial['metadata'] = apply(model['case'], model['metadata'], spec, action)
    return trial


class ResponseDecision(StrictModel):
    action_id: str = Field(description='One verified eligible action_id, or stop')
    reason: str = Field(min_length=1, max_length=1600)


def _choose(options, plan, root, model):
    if plan.search.selector == 'uniform':
        # A reproducible selector within the shared proposer, not a uniform
        # sampler over the entire legal model space.
        rng = random.Random(f'{plan.base_spec.seed}:{Path(root).name}')
        return rng.choice(options), dict(selector='uniform',
            policy='Uniform selection among verified eligible retained alternatives; shared physical proposer')
    if plan.search.selector in {'heuristic', 'greedy'}:
        def priority(option):
            actual = sum(option['assessment']['deficits'])
            future = option['action'].get('dc_lookahead_deficit', actual) if plan.search.selector == 'heuristic' else actual
            return (not option['assessment']['target_met'], future, actual,
                    len(option['action'].get('actions', [option['action']])), option['action']['action_id'])
        return min(options, key=priority), dict(selector=plan.search.selector,
            policy='AC-verified completion first; physical lookahead for heuristic, measured deficit for greedy')
    from .structured_planning import StructuredPlanner
    if model is None:
        from .agent import configured_model
        model = configured_model(timeout=30, max_retries=0, max_tokens=2000, disable_thinking=True)
    planner = StructuredPlanner(model, ResponseDecision, root)
    evidence = [dict(action=o['action'], targets=o['assessment']['targets']) for o in options]
    authority=options[0]['assessment'].get('adjustment_authority') if options else None
    decision = planner.invoke([dict(role='system', content='Select one electrically verified eligible candidate for the next search expansion. Archive candidates may temporarily trade response deficits within a fixed allowance; hard constraints remain mandatory. Never invent actions or alter requirements. Return stop if no justified exploration remains.'),
        dict(role='user', content=json.dumps(dict(research_question=plan.research_question, candidates=evidence,
            original_adjustment_authority=authority), ensure_ascii=False))])
    planner.valid()
    if decision.action_id == 'stop': return None, dict(selector='agent', **decision.model_dump())
    chosen = next((o for o in options if o['action']['action_id'] == decision.action_id), None)
    if chosen is None: raise ValueError('Agent selected an unverified response action')
    return chosen, dict(selector='agent', **decision.model_dump())


def _export(model, spec, folder, assessment, accepted=None):
    folder.mkdir(parents=True, exist_ok=True)
    accepted = assessment['target_met'] if accepted is None else accepted
    if model['kind'] == 'distribution':
        from .simulation import export_dss, simulate
        from .artifacts import render_html
        feeder = model['feeder']
        feeder.design_evidence['final_equipment_assignments'] = [dict(line_id=e.id, equipment_id=e.conductor) for e in feeder.lines+feeder.tie_lines]
        atomic_json(folder/'feeder.json', feeder.model_dump())
        result = simulate(export_dss(feeder, folder/'opendss'))
        atomic_json(folder/'simulation.json', result)
        (folder/'visualization.html').write_text(render_html(feeder, result, accepted), encoding='utf-8')
    else:
        from .transmission import case_payload, export_case, visualization
        atomic_json(folder/'case.json', case_payload(model['case']))
        atomic_json(folder/'metadata.json', model['metadata'])
        export_case(model['case'], folder/'case_generated.m')
        (folder/'visualization.html').write_text(visualization(model['case'], model['metadata'], dict(assessment['base_validation'], accepted=accepted)), encoding='utf-8')
    atomic_json(folder/'response.json', assessment)
    atomic_json(folder/'validation.json', assessment['base_validation'])


def _manifest(plan,task_context=None):
    package = Path(__file__).parent
    result=dict(schema_version='response_v1', plan=plan.model_dump(),
        source_hash=digest({str(p.relative_to(package)): file_digest(p) for p in sorted(package.rglob('*')) if p.suffix in ('.py', '.json')}),
        versions={name: importlib.metadata.version(name) for name in ('numpy', 'opendssdirect.py', 'pydantic')})
    if task_context is not None:result['task_context']=task_context
    return result


def read_response_result(root):
    """Read only hash-verified saved evidence, without generating or solving."""
    root = Path(root).resolve()
    saved = json.loads((root/'result.json').read_text()); checksum = saved.pop('result_hash')
    if digest(saved) != checksum: raise ValueError('Response result changed')
    if Path(saved['directory']).resolve() != root: raise ValueError('Response directory changed')
    for name, sha in saved['artifacts'].items():
        path = (root/name).resolve()
        if not path.is_relative_to(root) or not path.is_file() or file_digest(path) != sha:
            raise ValueError('Response artifacts changed')
    return dict(saved, result_hash=checksum)


def run_response_design(plan, workspace, design_id, *, model=None,task_context=None):
    plan = ResponseDesignPlan.model_validate(plan.model_dump() if isinstance(plan, ResponseDesignPlan) else plan)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', design_id): raise ValueError('Invalid response design ID')
    root = Path(workspace).resolve()/'response_designs'/design_id
    root.mkdir(parents=True, exist_ok=True)
    with (root/'.lock').open('w') as lock, SolverMeter(plan.search.max_search_ac_calls) as meter:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest = _manifest(plan,task_context)
        if (root/'manifest.json').exists():
            if json.loads((root/'manifest.json').read_text()) != manifest: raise ValueError('Response configuration/code changed; use a new ID')
            if not (root/'result.json').exists(): raise ValueError('Interrupted response run; use a new ID')
            return read_response_result(root)
        atomic_json(root/'manifest.json', manifest)
        with solver_phase('construction'):
            current_model = create_model(plan.base_spec)
        original_model = copy.deepcopy(current_model)
        if task_context is not None:
            from .research_tasks import _model_hash
            if _model_hash(original_model)!=task_context['original_model_hash']:
                raise ValueError('Research task reference model changed')
        probes = resolve_probes(current_model, plan.probes)
        def checked_measure(candidate,folder,step_factor=1):
            measured=measure(candidate,plan.base_spec,probes,folder,step_factor=step_factor)
            if task_context is not None:
                from .research_measurement import evaluate_research_task
                measured['electrical_base_accepted']=measured['base_accepted']
                measured['task_validation']=evaluate_research_task(candidate,plan.base_spec,task_context,folder/'task')
                measured['base_accepted']=measured['base_accepted'] and measured['task_validation']['accepted']
            return measured
        atomic_json(root/'resolved_probes.json', probes)
        with solver_phase('baseline'):
            initial = checked_measure(current_model,root/'baseline')
        current = score_response(initial, plan.targets, initial)
        from .response_authority import estimate_adjustment_authority
        authority=estimate_adjustment_authority(plan,initial)
        current['adjustment_authority']=authority
        atomic_json(root/'adjustment_authority.json',authority)
        with solver_phase('export'):
            _export(copy.deepcopy(current_model), plan.base_spec, root/'baseline/model', current)
        trace = []; rows = [dict(candidate='baseline', round=0, eligible=True, committed=True,
            parent=None, base_accepted=current['base_accepted'], target_met=current['target_met'], deficit=sum(current['deficits']))]
        selected = 'baseline'; stop = 'targets_met' if current['target_met'] else 'search_exhausted'
        strategy = plan.search.strategy
        if strategy == 'auto': strategy = 'archive' if len(plan.targets) > 1 else 'monotone'
        active = dict(candidate='baseline', model=current_model, assessment=current, parent=None)
        states = {'baseline': active}; pending = []
        seen = {physical_fingerprint(current_model)}
        for round_index in range(1, plan.search.max_rounds+1):
            if current['target_met'] or not current['valid']: break
            proposal_audit=[]; compensation_audit=[]
            parent_model, parent_assessment = active['model'], active['assessment']
            options = propose_response_actions(parent_model, plan.base_spec, probes, parent_assessment, plan.search, original_model,proposal_audit,
                remaining_rounds=plan.search.max_rounds-round_index+1,compensation_audit=compensation_audit,seen_physical=seen)
            atomic_json(root/f'proposals/round_{round_index:02d}.json',dict(parent=active['candidate'], screened=proposal_audit,
                compensation_fits=compensation_audit, proposed_previews=len(options)))
            eligible = []; budget_exhausted = False
            for action in options:
                candidate = f'round_{round_index:02d}_{action["action_id"]}'
                trial_model = apply_response_action(parent_model, plan.base_spec, action)
                preserved = research_contract_preserved(original_model, trial_model, plan)
                identity = physical_fingerprint(trial_model)
                if not preserved or identity in seen:
                    trace.append(dict(event='candidate_skipped', candidate=candidate, parent=active['candidate'],
                        reason='duplicate_physical_model' if identity in seen else 'original_contract_failed'))
                    continue
                seen.add(identity)
                try:
                    with solver_phase('search'):
                        trial = score_response(checked_measure(trial_model,root/'trials'/candidate), plan.targets, initial)
                        trial['adjustment_authority']=authority
                except ACSearchBudgetExceeded:
                    trace.append(dict(event='incomplete_candidate', candidate=candidate,
                        parent=active['candidate'], reason='search_ac_budget_exhausted'))
                    budget_exhausted = True
                    break
                if plan.search.calibrate_proxies:
                    trial['proxy_gains'] = learn_proxy_gains(parent_assessment, trial, action)
                monotone = accept_candidate(parent_assessment, trial, preserved)
                keep = (exploration_eligible(trial, current, preserved=preserved,
                            allowance=plan.search.exploration_deficit_allowance)
                        if strategy == 'archive' else monotone)
                record = dict(candidate=candidate, round=round_index, action=action, eligible=keep,
                    parent=active['candidate'], physical_hash=identity, nonregressing_improvement=monotone,
                    proxy_gains=trial.get('proxy_gains', {}),
                    committed=False, contract_preserved=preserved, base_accepted=trial['base_accepted'],
                    target_met=trial['target_met'], deficit=sum(trial['deficits']), targets=trial['targets'],
                    reason=('Verified bounded exploration' if strategy == 'archive' else 'Verified non-worsening improvement')
                        if keep else 'Rejected: hard constraint, invalid response, or search acceptance policy')
                rows.append({k: v for k, v in record.items() if k in rows[0]})
                trace.append(record)
                if keep:
                    state = dict(candidate=candidate, action=dict(action, action_id=candidate),
                        model=trial_model, assessment=trial, record=record, parent=active['candidate'])
                    states[candidate] = state
                    eligible.append(state)
            if strategy == 'archive':
                # The delivered incumbent only improves. Temporary tradeoffs
                # live in separate feasible search states, never in the target.
                for state in eligible:
                    if response_priority(state['assessment']) < response_priority(current):
                        current_model, current, selected = state['model'], state['assessment'], state['candidate']
                pending = [s for s in pending+eligible if exploration_eligible(s['assessment'], current,
                    preserved=True, allowance=plan.search.exploration_deficit_allowance)]
                pending = retain_archive(pending, plan.search.archive_width)
                eligible = pending
            if budget_exhausted:
                # Fully measured monotone candidates before the interruption
                # can still improve the incumbent; incomplete ones cannot.
                if strategy == 'monotone' and eligible:
                    best = min(eligible, key=lambda s: response_priority(s['assessment']))
                    current_model, current, selected = best['model'], best['assessment'], best['candidate']
                stop = 'targets_met' if current['target_met'] else 'search_ac_budget_exhausted'
                break
            if current['target_met']:
                stop = 'targets_met'; break
            if not eligible:
                stop = 'no_verified_exploration_candidate' if strategy == 'archive' else 'no_verified_improving_action'
                break
            try:
                chosen, decision = _choose(eligible, plan, root/f'decisions/round_{round_index:02d}', model)
            except Exception as exc:
                trace.append(dict(event='selector_failed', error_type=type(exc).__name__))
                stop = 'agent_selection_failed'; break
            trace.append(dict(event='selection', round=round_index, **decision))
            if chosen is None: stop = 'agent_requested_stop'; break
            active = chosen
            trace[-1]['next_parent'] = chosen['candidate']
            if strategy == 'archive':
                pending = [s for s in pending if s['candidate'] != chosen['candidate']]
            else:
                current_model, current, selected = chosen['model'], chosen['assessment'], chosen['candidate']
            if current['target_met']: stop = 'targets_met'
        selected_path = []
        cursor = selected
        while cursor != 'baseline':
            selected_path.append(cursor)
            cursor = states[cursor]['parent']
        selected_path.reverse()
        for record in trace:
            if 'committed' in record: record['committed'] = record['candidate'] in selected_path
        for row in rows: row['committed'] = row['candidate'] == 'baseline' or row['candidate'] in selected_path
        # This independent step is evaluated once after selection, never used
        # to choose an alternative candidate or steer subsequent proposals.
        with solver_phase('verification'):
            heldout = score_response(checked_measure(current_model,root/'verification',
                                      step_factor=plan.verification_step_factor), plan.targets, initial)
        consistency = {}
        for p in probes:
            a, b = current['probes'][p['id']], heldout['probes'][p['id']]
            consistency[p['id']] = bool(a['valid'] and b['valid'] and all(
                abs(v-b['signed_response'][key]) <= 1e-7+plan.verification_relative_tolerance*max(abs(v), abs(b['signed_response'][key]))
                for key, v in a.get('signed_response', {}).items()))
        verified = heldout['target_met'] and all(consistency.values())
        preserved = research_contract_preserved(original_model, current_model, plan)
        success = bool(current['target_met'] and verified and preserved)
        if current['target_met'] and not verified: stop = 'independent_verification_failed'
        diagnosis = dict(code=stop, infeasibility_proven=False, requires_explicit_permission=False,
            summary='All requested targets and independent checks passed.' if success else
                'The bounded search did not meet the requested targets; this is not a physical infeasibility proof.')
        diagnosis['adjustment_authority']=authority
        diagnosis['search_coverage']=dict(
            selected_near_collinear_compensations=sum(bool(t.get('committed') and
                t.get('action',{}).get('compensation_fit',{}).get('selectivity',{}).get('classification')=='near_collinear') for t in trace),
            deduplication_policy='Original, visited and within-round physical duplicates are removed before the distribution preview quota is filled')
        from .response_contract import change_summary
        diagnosis['design_space']=dict(permissions=plan.search.model_dump(),actual_changes=change_summary(original_model,current_model),
            reference='All edit limits and response ratios refer to the original model; AC and each response target remain authoritative')
        if not success and current_model['kind'] == 'distribution':
            diagnosis['catalogue_evidence'] = catalogue_diagnosis(current_model, plan.base_spec, probes, current)
            if (current['valid'] and current['base_accepted'] and not current['target_met']
                and plan.search.allowed_actions == ['replace_conductor']
                and not _distribution_options(current_model, plan.base_spec, probes, current)):
                diagnosis.update(code='catalogue_direction_exhausted', requires_explicit_permission=True,
                    summary='No direction-improving conductor replacement remains on the probed paths in the permitted catalogue. More rounds of the same moves cannot help. Changing geometry, equipment scope or targets requires explicit permission; no AC infeasibility is proven.',
                    suggested_action='For generated spatial_mst geometry, explicitly permit scale_layout with a numerical max_layout_scale_change; otherwise revise the design space explicitly.')
        atomic_json(root/'diagnosis.json', diagnosis)
        with solver_phase('export'):
            _export(current_model, plan.base_spec, root/'selected', current, accepted=success)
        accounting = meter.snapshot()
        atomic_json(root/'solver_accounting.json', dict(accounting, events=meter.events))
        atomic_json(root/'selected/acceptance.json', dict(accepted=success,
            base_accepted=current['base_accepted'], selected_target_met=current['target_met'],
            verification_passed=verified, protected_contract_preserved=preserved))
        atomic_json(root/'history.json', trace)
        atomic_json(root/'verification/checks.json', dict(targets_met=heldout['target_met'], step_consistency=consistency))
        with (root/'candidates.csv').open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
        report = (f'Electrical response design: {"accepted" if success else "target not met"}. '
            f'Base electrical checks: {current["base_accepted"]}; selected target checks: {current["target_met"]}; '
            f'independent step verification: {verified}; protected contract: {preserved}. '
            f'Selected candidate: {selected}. Stop: {stop}.\n\n{diagnosis["summary"]}\n\n'
            'Static finite-difference AC responses under the recorded reference injections, balancing and controls. '
            'These are reusable grid models; probes are internal measurements, not a time-series product. '
            'Unmet targets indicate bounded-search failure, not mathematical infeasibility. '
            'Matching selected responses does not certify general realism, dynamics or N-1 security.')
        (root/'report.md').write_text(report, encoding='utf-8')
        with zipfile.ZipFile(root/'dataset.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in sorted((root/'selected').rglob('*')):
                if path.is_file(): archive.write(path, path.relative_to(root))
            for path in sorted((root/'proposals').glob('*.json')):archive.write(path,path.relative_to(root))
            for name in ('manifest.json', 'resolved_probes.json', 'history.json', 'candidates.csv', 'report.md', 'diagnosis.json', 'adjustment_authority.json', 'solver_accounting.json', 'verification/checks.json', 'verification/response.json'):
                archive.write(root/name, name)
        output = dict(status='completed' if success else 'target_not_met', target_met=success,
            selected_target_met=current['target_met'], verification_passed=verified,
            protected_contract_preserved=preserved, stop_reason=stop, directory=str(root),
            winner=selected, accepted_steps=sum(t.get('committed', False) for t in trace),
            search_strategy=strategy, selected_path=selected_path, solver_accounting=accounting,
            evaluated_candidates=len(rows), baseline=initial, selected=current, verification=heldout,
            verified_report=report, diagnosis=diagnosis,
            artifacts={str(p.relative_to(root)): file_digest(p) for p in sorted(root.rglob('*')) if p.is_file() and p.name not in ('.lock', 'result.json')})
        output['result_hash'] = digest(output); atomic_json(root/'result.json', output)
        return output
