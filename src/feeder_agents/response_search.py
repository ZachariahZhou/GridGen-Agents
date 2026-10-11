"""Physical-state identity and bounded, diverse response-search alternatives."""
import math
from collections import Counter

from .artifacts import digest


def learn_proxy_gains(before, after, action):
    """Calibrate incremental path-response changes from an actual AC preview.

    A fixed source contributes to the measured sensitivity but is not scaled
    by a line edit. A multiplicative whole-response proxy can consequently
    overshoot its prediction. Fit a positive bounded secant gain, retaining
    prior information if a move has negligible predicted effect. This is a
    local proposal heuristic, never a response certificate.
    """
    gains = dict(before.get('proxy_gains', {}))
    predicted = action.get('proxy_raw_response_estimates', {})
    counts = Counter(r['target']['probe_id'] for r in before['targets'])
    for old, new in zip(before['targets'], after['targets']):
        key = old['target']['probe_id']
        if counts[key] != 1 or key not in predicted or any(v is None for v in (old['observed'], new['observed'], predicted[key])):
            continue
        delta = predicted[key]-old['observed']
        if abs(delta) <= 1e-5*max(abs(old['observed']), 1e-9):
            continue
        gain = (new['observed']-old['observed'])/delta
        if math.isfinite(gain) and .02 <= gain <= 5.:
            gains[key] = gain
    return gains


def physical_fingerprint(model):
    if model['kind'] == 'distribution':
        payload = model['feeder'].model_dump()
        payload.pop('design_evidence', None)
        payload.pop('assumptions', None)
    else:
        from .transmission import case_payload
        payload = case_payload(model['case'])
    def canonical(value):
        if isinstance(value, float):
            # Distribution exports use 12 significant digits. Transmission
            # retains 15: only arithmetic-order noise is coalesced there.
            digits = 12 if model['kind'] == 'distribution' else 15
            return float(format(value, f'.{digits}g')) if value else 0.
        if isinstance(value, dict):
            return {key: canonical(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [canonical(item) for item in value]
        return value
    return digest(dict(kind=model['kind'], model=canonical(payload)))


def response_priority(assessment):
    return (not (assessment['base_accepted'] and assessment['valid']),
            sum(assessment['deficits']), max(assessment['deficits'], default=0.))


def exploration_eligible(assessment, incumbent, *, preserved, allowance):
    deficits = assessment['deficits']
    return bool(preserved and assessment['base_accepted'] and assessment['valid']
                and len(deficits) == len(incumbent['deficits'])
                and all(math.isfinite(d) and d >= 0 for d in deficits)
                and sum(deficits) <= sum(incumbent['deficits'])+allowance+1e-9)


def retain_archive(states, width):
    """Keep the best aggregate candidate and complementary per-target specialists.

    A dominated physical state can still have different continuations, so this
    is bounded diverse retention, not a proof-preserving Pareto elimination.
    """
    ranked = sorted(states, key=lambda s: (response_priority(s['assessment']), s['candidate']))
    if len(ranked) <= width:
        return ranked
    kept = [ranked[0]]
    for port in range(len(ranked[0]['assessment']['deficits'])):
        if len(kept) == width:
            break
        specialist = min(ranked, key=lambda s: (s['assessment']['deficits'][port],
                         response_priority(s['assessment']), s['candidate']))
        if all(s['candidate'] != specialist['candidate'] for s in kept):
            kept.append(specialist)
    for state in ranked:
        if len(kept) == width:
            break
        if all(s['candidate'] != state['candidate'] for s in kept):
            kept.append(state)
    return sorted(kept, key=lambda s: (response_priority(s['assessment']), s['candidate']))
