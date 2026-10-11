"""Exploratory counter-moves for met ports; original acceptance targets stay fixed."""
import copy
import math

from .response_distribution import conductor_options,layout_options,rank_distribution_actions
from .response_flexibility import distribution_actions
from .response_candidates import direction_key


def conditional_compensation_actions(model, probes, assessment, primary, counters, *,
                                    original=None, spec=None, search=None, audit=None):
    """Size a geometric counter-move conditional on the complete first move.

    For fixed topology, mean aggregation and a fixed first move, the calibrated
    shared-path response is affine in the second scale factor. Its deficit and
    centering objectives are piecewise linear: their breakpoints suffice for
    a one-dimensional fit. This is a proposal only, with no AC evaluations.
    The caller must screen the complete original-reference contract and then
    measure every target with AC. No target or permission is changed here.
    """
    if model['kind'] != 'distribution' or len(assessment['targets']) < 2:
        return []
    targeted = {row['target']['probe_id'] for row in assessment['targets']}
    if any(p.get('aggregation', 'mean_abs') != 'mean_abs' for p in probes if p['id'] in targeted):
        # A max-of-affine proxy can switch the active monitored port. Keep the
        # ordinary joint proposals rather than fit an invalid affine secant.
        return []
    # Existing counter steps identify a direction, not the required amplitude.
    directions = {}
    for action in counters:
        if action['kind'] in {'scale_layout', 'scale_subtree'}:
            directions.setdefault((action['kind'], action.get('child_bus'), action.get('probe_id')), action)
    ranked_primary = rank_distribution_actions(model, probes, assessment, primary)
    pairs = []
    for first in ranked_primary:
        if 'proxy_response_estimates' not in first:
            continue
        for second in directions.values():
            if first.get('probe_id') == second.get('probe_id') or abs(second['factor']-1) < 1e-10:
                continue
            pairs.append(dict(kind='compound', actions=[first, second],
                diagnosis='Condition the geometric compensation amplitude on the first move and all original response targets; contract screening and AC verification are mandatory.'))
    output = []; primary_models = {}; intervals = {}
    for pair in rank_distribution_actions(model, probes, assessment, pairs):
        first, second = pair['actions']
        if 'proxy_response_estimates' not in pair:
            continue
        intercept = first['proxy_response_estimates']
        estimates = pair['proxy_response_estimates']
        rows = assessment['targets']
        if any(intercept.get(r['target']['probe_id']) is None or
               estimates.get(r['target']['probe_id']) is None for r in rows):
            continue
        slopes = {key: (estimates[key]-value)/(second['factor']-1)
                  for key, value in intercept.items()}
        factors = {1., second['factor']}
        for row in rows:
            key = row['target']['probe_id']
            if abs(slopes[key]) < 1e-12:
                continue
            lower, upper = row['target']['lower'], row['target']['upper']
            goals = [lower, upper]
            if lower is not None and upper is not None:
                goals.append((lower+upper)/2)
            factors.update(1+(goal-intercept[key])/slopes[key] for goal in goals if goal is not None)

        def priority(factor):
            deficit, centering = 0., 0.
            for row in rows:
                key = row['target']['probe_id']
                value = intercept[key]+slopes[key]*(factor-1)
                lower, upper = row['target']['lower'], row['target']['upper']
                scale = max(lower or 0, upper or 0, 1e-9)
                delta = max((lower or 0)-value, value-(upper if upper is not None else math.inf), 0)/scale
                # Ignore only floating-point cancellation in this cheap ranking.
                deficit += 0. if delta < 1e-12 else delta
                if lower is not None and upper is not None:
                    centering += abs(value-(lower+upper)/2)/scale
            return deficit, centering, abs(math.log(factor))

        factors = {f for f in factors if math.isfinite(f) and f > 1e-9}
        unconstrained = min(factors, key=priority)
        interval = None
        if original is not None:
            from .response_design import apply_response_action
            from .response_geometry import compensation_scale_interval
            from .artifacts import digest
            key=digest(first)
            if key not in primary_models:
                primary_models[key]=apply_response_action(model,spec,first)
            direction=(key,second['kind'],second.get('child_bus'))
            if direction not in intervals:
                intervals[direction]=compensation_scale_interval(primary_models[key],original,spec,search,second)
            interval=intervals[direction]
            if interval['empty']:
                if audit is not None:audit.append(dict(event='compensation_fit',status='empty_interval',
                    first_action=first.get('action_id'),second_kind=second['kind'],
                    second_child=second.get('child_bus'),interval=interval))
                continue
            lo,hi=interval['lower'],interval['upper']
            factors={min(hi,max(lo,f)) for f in factors}|{lo,hi}
            factors={f for f in factors if math.isfinite(f) and f>1e-9}
        factor = min(factors, key=priority)
        from .response_geometry import action_direction_diagnostic
        delta={r['target']['probe_id']:intercept[r['target']['probe_id']]-r['observed'] for r in rows}
        fit=dict(unconstrained_factor=unconstrained,selected_factor=factor,interval=interval,
            boundary_selected=bool(interval and any(math.isclose(factor,interval[k],rel_tol=1e-9,abs_tol=1e-12)
                for k in ('lower','upper'))),predicted_deficit=priority(factor)[0],
            selectivity=action_direction_diagnostic(delta,slopes,first.get('probe_id'),second.get('probe_id')))
        if audit is not None:audit.append(dict(event='compensation_fit',status='identity' if abs(factor-1)<1e-9 else 'proposed',
            first_action=first.get('action_id'),second_kind=second['kind'],second_child=second.get('child_bus'),**fit))
        if abs(factor-1) < 1e-9:
            continue
        adjusted = dict(second, factor=factor)
        if second['kind'] == 'scale_layout':
            adjusted['original_scale'] = second['original_scale']/second['factor']*factor
        output.append(dict(kind='compound', actions=[first, adjusted],
            conditional_compensation=True, compensation_fit=fit, diagnosis=pair['diagnosis']))
    return output


def compensation_actions(model,original,spec,probes,assessment,search):
    if model['kind']!='distribution':return []
    output=[]
    for row in assessment['targets']:
        observed=row['observed']
        if not row['passed'] or observed is None or observed<=1e-12:continue
        for direction in (-1,1):
            for step in (.02,.1):
                proposal=copy.deepcopy(row);proposal['passed']=False
                bounds=sorted([observed*(1+direction*step),observed*(1+direction*step*1.25)])
                proposal['target'].update(lower=bounds[0],upper=bounds[1])
                # These artificial offsets are proposal instructions only.
                # They never enter the run's plan, scoring or exported targets.
                # Inactive rows retain the other ports for interference-aware
                # local preselection. Only this synthetic offset proposes moves.
                context=[proposal if r is row else dict(r,passed=True) for r in assessment['targets']]
                exploratory=dict(assessment,targets=context)
                candidates=[]
                if 'replace_conductor' in search.allowed_actions:candidates+=conductor_options(model,spec,probes,exploratory)
                if 'scale_layout' in search.allowed_actions:candidates+=layout_options(model,original,spec,exploratory,search)
                if set(search.allowed_actions)&{'scale_subtree','rewire_branch','redistribute_load'}:
                    candidates+=distribution_actions(model,original,spec,probes,exploratory,search)
                per_kind={};directions=set()
                for action in candidates:
                    key=direction_key(action)
                    if key in directions:continue
                    directions.add(key)
                    kind=action['kind'];per_kind[kind]=per_kind.get(kind,0)+1
                    if per_kind[kind]>2:continue
                    output.append(dict(action,compensation=True,
                        diagnosis='Exploratory counter-move for a currently met port; may appear only in a joint preview. Original target is unchanged. '+action['diagnosis']))
    return output
