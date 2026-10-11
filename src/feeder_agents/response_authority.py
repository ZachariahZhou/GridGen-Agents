"""Measured response components and permission-aware, non-certifying guidance."""
import math


def voltage_components(response, source_response, aggregation):
    """Project source/net signed derivatives onto the measured absolute response.

    The decomposition sums to mean_abs even for mixed signs. It is descriptive:
    the finite-source response may itself change with the operating point.
    """
    if aggregation!='mean_abs':return dict(available=False,reason='Non-mean aggregation is not decomposed')
    if not response or set(response)!=set(source_response):
        return dict(available=False,reason='Matching signed source measurements are unavailable')
    if not all(isinstance(v,(int,float)) and math.isfinite(v) for v in list(response.values())+list(source_response.values())):
        return dict(available=False,reason='Nonfinite component measurement')
    aggregate=sum(abs(v) for v in response.values())/len(response)
    if aggregate<=1e-12:return dict(available=False,reason='Near-zero aggregate response')
    projected=sum((1 if v>0 else -1 if v<0 else 0)*source_response[k] for k,v in response.items())/len(response)
    network=aggregate-projected
    return dict(available=True,aggregate_response=aggregate,source_contribution=projected,
        network_contribution=network,network_fraction=network/aggregate,
        source_fraction=projected/aggregate,
        scope='Signed source-referenced derivative decomposition; not independent component control or an AC bound')


def estimate_adjustment_authority(plan, baseline):
    """Explain the initial design without changing its ports or permissions."""
    allowed=set(plan.search.allowed_actions)
    result=dict(infeasibility_proven=False,reference='Frozen original model and measured source response',
        probes=[],design_guidance=[],automatic_changes=False,
        scope='Linear line-scaling scenarios ignore coupling and displacement limits; guide design and diagnosis, never certify reachability')
    if plan.base_spec.network_kind!='distribution':
        result['unavailable_reason']='Source/line voltage decomposition applies only to supported distribution probes'
        return result
    by_id={p.id:p for p in plan.probes}
    geometry=max(plan.search.max_layout_scale_change or 0,plan.search.max_branch_length_change or 0)
    for target in plan.targets:
        p=by_id[target.probe_id];measured=baseline['probes'].get(p.id,{})
        parts=(voltage_components(measured.get('signed_response',{}),measured.get('source_signed_response',{}),p.aggregation)
               if measured.get('valid') else dict(available=False,reason='Response measurement invalid'))
        row=dict(probe_id=p.id,**parts,geometry_only_target_outside_linear_scenario=False)
        if parts['available'] and geometry and measured.get('value',0)>1e-12:
            value=measured['value'];network=parts['network_contribution']
            denominator=value if target.reference=='baseline_ratio' else 1.
            interval=sorted([(value-geometry*network)/denominator,(value+geometry*network)/denominator])
            row['line_scaling_scenario']=dict(fraction=geometry,target_range=interval,
                reference=target.reference,certified_bound=False)
            row['geometry_only_target_outside_linear_scenario']=bool(allowed<={'scale_layout','scale_subtree'} and
                ((target.lower is not None and target.lower>interval[1]+1e-8) or
                 (target.upper is not None and target.upper<interval[0]-1e-8)))
        result['probes'].append(row)
    for action,advice in (
        ('replace_conductor','During permitted equipment selection, compare compatible whole-tuple conductors on influential paths; changing line length alone may have limited authority.'),
        ('rewire_branch','During permitted topology design, consider distinct source paths and local branches that influence the primary port without the protected port.')):
        permitted=action in allowed
        result['design_guidance'].append(dict(action=action,within_current_permissions=permitted,
            requires_new_authorization=not permitted,advice=advice,
            application='Use the existing bounded action and full contract checks' if permitted else
                'Prepare a revised authorized plan before any change; the current search cannot apply this suggestion'))
    result['reference_policy']='Initial-design suggestions may inform an authorized future construction; never replace a frozen grid, seed, probe or ratio denominator to pass the current task.'
    return result
