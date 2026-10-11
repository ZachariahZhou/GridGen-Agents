"""Analytic geometric proposal bounds and local, non-certifying diagnostics."""
import math

import networkx as nx


def compensation_scale_interval(after_primary, original, spec, search, second):
    """Bound a second positive scale against ORIGINAL energized-line geometry.

    Every energized edge is either unchanged or multiplied by s. Node motion
    is affine in s, so displacement balls yield quadratic intervals. Ties and
    non-geometric conditions still require the full contract screen. An empty
    interval excludes this action pair only; it does not prove task infeasibility.
    """
    feeder=after_primary['feeder']; reference=original['feeder']
    result=dict(lower=1e-9,upper=math.inf,empty=False,limiting_lower='positive_scale',
        limiting_upper=None,scope='Original-reference energized lengths, segment limits and node displacement; full contract screening remains mandatory')
    def empty(reason):
        # An early contradiction may precede every computed upper bound. An
        # absent bound on a rejected pair is JSON null, not nonstandard Infinity.
        return dict(result,empty=True,reason=reason,
            upper=result['upper'] if math.isfinite(result['upper']) else None)
    kind=second['kind'];allowed=set(search.allowed_actions)
    if kind not in {'scale_layout','scale_subtree'} or kind not in allowed:
        return empty('unauthorized_geometry_action')
    positions={b.id:(b.x_km,b.y_km) for b in feeder.buses}
    originals={b.id:(b.x_km,b.y_km) for b in reference.buses}
    if kind=='scale_layout':
        moving=set(positions);anchor=positions[feeder.source_bus]
    else:
        tree=nx.bfs_tree(nx.Graph((e.bus1,e.bus2) for e in feeder.lines),feeder.source_bus)
        child=second['child_bus']
        if child==feeder.source_bus or child not in tree:
            return empty('invalid_subtree')
        moving=nx.descendants(tree,child)|{child}
        anchor=positions[next(tree.predecessors(child))]
    bound=(max(search.max_branch_length_change or 0,search.max_layout_scale_change or 0)
           if 'scale_subtree' in allowed else search.max_layout_scale_change)
    reference_edges={e.id:e for e in reference.lines}

    def intersect(lower,upper,label):
        if lower>result['lower']:result.update(lower=lower,limiting_lower=label)
        if upper<result['upper']:result.update(upper=upper,limiting_upper=label)
        if result['lower']>result['upper']:
            result.update(empty=True,reason=label)

    for edge in feeder.lines:
        old=reference_edges[edge.id]
        rewired={old.bus1,old.bus2}!={edge.bus1,edge.bus2}
        if rewired and 'rewire_branch' not in allowed:
            return empty('unauthorized_rewired_edge:'+edge.id)
        lower_ratio=1/search.max_rewire_length_ratio if rewired else 1-bound
        upper_ratio=search.max_rewire_length_ratio if rewired else 1+bound
        lower=max(lower_ratio*old.length_km,spec.segment_km_min)
        upper=min(upper_ratio*old.length_km,spec.segment_km_max)
        changed=kind=='scale_layout' or edge.bus1 in moving or edge.bus2 in moving
        if changed:
            intersect(lower/edge.length_km,upper/edge.length_km,'line:'+edge.id)
        elif not lower-1e-10<=edge.length_km<=upper+1e-10:
            return empty('unchanged_line:'+edge.id)
    if 'scale_subtree' in allowed:
        radius=search.max_node_displacement_km
        for bus,position in positions.items():
            d=[position[i]-originals[bus][i] for i in (0,1)]
            v=[position[i]-anchor[i] if bus in moving else 0. for i in (0,1)]
            vv=sum(x*x for x in v); dd=sum(x*x for x in d); dv=sum(x*y for x,y in zip(d,v))
            if vv<=1e-20:
                if dd>radius**2+1e-12:return empty('unchanged_node:'+bus)
                continue
            disc=dv*dv-vv*(dd-radius**2)
            if disc < -1e-12*max(dv*dv,vv*(dd+radius**2),1e-20):
                return empty('node_displacement:'+bus)
            root=math.sqrt(max(0.,disc))
            intersect(1+(-dv-root)/vv,1+(-dv+root)/vv,'node:'+bus)
    return result


def action_direction_diagnostic(primary_delta, secondary_slope, focus, protected):
    """Two-port proxy secants; neither AC controllability nor infeasibility proof.

    The first column is a finite primary change, the second a conditional
    secondary slope. Separation is invariant to column rescaling, but changes
    if response coordinates/units change; compare matched target protocols.
    """
    if focus not in primary_delta or protected not in primary_delta or focus==protected:
        return dict(classification='unavailable',scope='Local sequential proxy secants only')
    a=primary_delta[focus];c=primary_delta[protected]
    b=secondary_slope[focus];d=secondary_slope[protected]
    norm=math.hypot(a,c)*math.hypot(b,d)
    separation=min(1.,abs(a*d-b*c)/norm) if norm>1e-20 else 0.
    controllable=abs(d)>1e-12
    return dict(primary_probe=focus,protected_probe=protected,
        primary_delta={focus:a,protected:c},secondary_slope={focus:b,protected:d},
        direction_separation=separation,
        linear_cancellation_factor=1-c/d if controllable else None,
        remaining_primary_increment=a-b*c/d if controllable else None,
        classification=('no_protected_port_effect' if not controllable else
                        'near_collinear' if separation<1e-3 else 'distinct_directions'),
        scope='Local sequential proxy secants, conditional on this primary move; not an AC controllability or infeasibility certificate')
