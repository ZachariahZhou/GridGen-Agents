"""Joint line-type selection; probabilities are explicit engineering priors."""
import math

import numpy as np

from .rules import data

LINE_POLICY = 'voltage_conditioned_joint_catalogue_v1'


def select_line_profile(voltage_kv, length_km, dc_mw, power_factor, rng, catalogue=None):
    """Choose a complete R/X/C/rating tuple, never perturb individual fields.

    A provisional DC transfer determines required current with 20% headroom.
    Prefer fewer circuits; long loaded corridors favor lower resistance.
    All weights are synthesis assumptions, not calibrated population frequencies.
    The subsequent DC resize and AC validation remain authoritative.
    """
    catalogue = catalogue or data('transmission_line_families.json')
    candidates = catalogue['profiles'][str(voltage_kv)]
    required = abs(dc_mw) / (math.sqrt(3) * voltage_kv * power_factor * .8)
    currents = np.asarray([p['max_i_ka'] for p in candidates])
    resistances = np.asarray([p['r_ohm_km'] for p in candidates])
    circuits = np.maximum(1, np.ceil(required / currents))
    eligible = circuits <= 4
    # Preserve an explicitly over-capacity candidate for AC diagnosis if none
    # can carry the screening transfer within the existing four-circuit limit.
    if not eligible.any():
        eligible = currents == currents.max()
    length_load = min(length_km / 100., 3.) * min(required / currents.min(), 1.)
    logits = (-.7 * (circuits - 1) - .35 * np.log(currents / currents.min())
              - .35 * length_load * (resistances / resistances.min() - 1))
    probabilities = np.zeros(len(candidates))
    probabilities[eligible] = np.exp(logits[eligible] - logits[eligible].max())
    probabilities /= probabilities.sum()
    index = int(rng.choice(len(candidates), p=probabilities))
    return dict(candidates[index]), dict(
        policy=LINE_POLICY, provisional_dc_mw=float(dc_mw), required_current_ka=float(required),
        candidate_ids=[p['profile_id'] for p in candidates], probabilities=probabilities.tolist(),
        selected_probability=float(probabilities[index]),
        within_four_circuit_screen=bool(circuits[index] <= 4))


def matches_line_profile(evidence, catalogue):
    """Exact joint tuple and provenance check, including voltage applicability."""
    candidates = catalogue['profiles'].get(str(evidence.get('voltage_kv')), [])
    profile = next((p for p in candidates if p['profile_id'] == evidence.get('profile_id')), None)
    return profile is not None and all(evidence.get(k) == v for k, v in profile.items())
