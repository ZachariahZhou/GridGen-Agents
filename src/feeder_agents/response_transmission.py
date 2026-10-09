"""DC perturbation predictions rank candidates; full AC probes decide acceptance."""
import math

import numpy as np


def rank_parallel_actions(model, probes, assessment, actions, *, lookahead=1):
    """Screen every legal parallel circuit using a rank-one nodal update.

    A branch susceptance increment changes the reduced nodal matrix by
    delta_b * a * a.T. The Sherman-Morrison update predicts redistributed
    transfer flows, including the competing paths that carry little current
    initially. Transformer taps remain fixed. R, charging and PV controls are
    omitted here and retained by the authoritative AC candidate evaluation.
    """
    if not actions: return []
    case = model['case']; bus = case['bus']; branch = case['branch']
    index = {str(int(row[0])): i for i, row in enumerate(bus)}
    slack = next(i for i, row in enumerate(bus) if int(row[1]) == 3)
    columns = [i for i in range(len(bus)) if i != slack]
    incidence = np.zeros((len(branch), len(bus)))
    for i, row in enumerate(branch):
        incidence[i, index[str(int(row[0]))]] = 1
        incidence[i, index[str(int(row[1]))]] = -1
    reduced = incidence[:, columns]
    taps = np.where(branch[:, 8] == 0, 1., branch[:, 8])
    b = np.where(branch[:, 10] > 0, 1/branch[:, 3]/taps, 0.)
    nodal = reduced.T @ (b[:, None]*reduced)
    try:
        influence = np.linalg.solve(nodal, reduced.T)
        states = {}
        for p in probes:
            injection = np.zeros(len(bus))
            for group, sign in ((p['injection_buses'], 1), (p['withdrawal_buses'], -1)):
                for name in group: injection[index[name]] += sign/len(group)
            theta = np.linalg.solve(nodal, injection[columns])
            flows = b*(reduced @ theta)
            values = np.abs(flows[p['monitor_branches']])
            aggregate = float(values.max() if p['aggregation'] == 'max_abs' else values.mean())
            states[p['id']] = (p, theta, aggregate)
    except np.linalg.LinAlgError:
        return sorted(actions, key=lambda a: -a['proxy_effect'])
    # A short prospective path avoids committing a locally attractive corridor
    # that removes the opportunity for a stronger two-step improvement. These
    # are DC predictions only; each committed step still needs fresh AC checks.
    path_influence = reduced @ influence if lookahead > 1 else None
    ranked = []
    for action in actions:
        i = action['index']; e = model['metadata']['branch_evidence'][i]
        delta_b = b[i]/e['circuits']; ratios = {}
        denominator = 1+delta_b*(reduced[i] @ influence[:, i])
        for name, (p, theta, original) in states.items():
            updated = theta-influence[:, i]*(delta_b*(reduced[i] @ theta))/denominator
            flows = b*(reduced @ updated)
            flows[i] += delta_b*(reduced[i] @ updated)
            values = np.abs(flows[p['monitor_branches']])
            aggregate = float(values.max() if p['aggregation'] == 'max_abs' else values.mean())
            ratios[name] = aggregate/original if original > 1e-12 else 1.
        deficit = 0.
        for target in assessment['targets']:
            t = target['target']; observed = target['observed']
            if observed is None: deficit += 1e12; continue
            prediction = observed*ratios[t['probe_id']]
            deficit += max((t['lower'] or 0)-prediction,
                prediction-(t['upper'] if t['upper'] is not None else math.inf), 0)/max(t['lower'] or 0, t['upper'] or 0, 1e-9)
        item = dict(action, dc_predicted_deficit=deficit, dc_response_ratios=ratios)
        if lookahead > 1:
            following = [a for a in actions if a['index'] != i or action['value'] < 4]
            indices = np.asarray([a['index'] for a in following], dtype=int)
            if len(indices):
                increments = np.asarray([b[j]/model['metadata']['branch_evidence'][j]['circuits'] for j in indices])
                updated_influence = path_influence[:, indices] - np.outer(
                    path_influence[:, i], path_influence[i, indices])*delta_b/denominator
                denominators = 1+increments*updated_influence[indices, np.arange(len(indices))]
                future_ratios = {}
                for name, (p, theta, aggregate) in states.items():
                    z = reduced @ theta
                    first = z-path_influence[:, i]*delta_b*z[i]/denominator
                    second = first[:, None]-updated_influence*(increments*first[indices]/denominators)[None, :]
                    flows = b[:, None]*second
                    flows[i, :] += delta_b*second[i, :]
                    flows[indices, np.arange(len(indices))] += increments*second[indices, np.arange(len(indices))]
                    values = np.abs(flows[p['monitor_branches'], :])
                    response = values.max(axis=0) if p['aggregation'] == 'max_abs' else values.mean(axis=0)
                    future_ratios[name] = response/aggregate if aggregate > 1e-12 else np.ones(len(indices))
                future = np.zeros(len(indices)); non_worsening = np.ones(len(indices), dtype=bool)
                for target in assessment['targets']:
                    t = target['target']; observed = target['observed']
                    if observed is None:
                        future += 1e12; continue
                    prediction = observed*future_ratios[t['probe_id']]
                    d = np.maximum(np.maximum((t['lower'] or 0)-prediction,
                        prediction-(t['upper'] if t['upper'] is not None else math.inf)), 0)/max(t['lower'] or 0, t['upper'] or 0, 1e-9)
                    future += d
                    non_worsening &= d <= target['deficit']+1e-9
                future[~non_worsening] = math.inf
                best = int(np.argmin(future))
                item['dc_lookahead_deficit'] = min(deficit, float(future[best]))
                if future[best] < deficit:
                    next_action = following[best]
                    item['dc_lookahead_action'] = dict(kind='parallel_line', index=next_action['index'],
                        value=next_action['value']+int(next_action['index'] == i))
        ranked.append(item)
    return sorted(ranked, key=lambda a: (a.get('dc_lookahead_deficit', a['dc_predicted_deficit']),
                                        a['dc_predicted_deficit'], a['index']))
