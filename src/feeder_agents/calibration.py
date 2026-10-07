"""Empirical utility-feeder calibration; unsupported geography stays a prior."""
from __future__ import annotations

import json
import random
from importlib.resources import files
from statistics import fmean

PROFILE_ID = "epri_dpv_j1_k1"


def load_profile(profile_id: str = PROFILE_ID) -> dict:
    """Load the versioned, packaged J1/K1 profile (M1 is never training data)."""
    if profile_id != PROFILE_ID:
        raise ValueError(f"Unknown calibration profile: {profile_id}")
    return json.loads(files("feeder_agents").joinpath(f"data/calibration_{profile_id}.json").read_text())


def sample_blended(profile: dict, metric: str, rng: random.Random, *,
                   prior_value: float, empirical_weight: float = 0.7) -> float:
    """Mixture draw, rather than interpolating values and erasing observed tails.

    Weight is an explicit transfer-learning assumption, not estimated confidence.
    The caller supplies a prior draw in the same units as the empirical metric.
    """
    if not 0 <= empirical_weight <= 1:
        raise ValueError("empirical_weight must be in [0, 1]")
    values = profile["distributions"][metric]["values"]
    if not values:
        raise ValueError(f"Empty empirical distribution: {metric}")
    return float(rng.choice(values)) if rng.random() < empirical_weight else float(prior_value)


def normalized_weights(values: list[float]) -> list[float]:
    if not values or any(v <= 0 for v in values):
        raise ValueError("Weights must be positive and nonempty")
    mean = fmean(values)
    return [v / mean for v in values]


def distribution_distance(left: list[float], right: list[float]) -> float:
    """Exact 1D empirical Wasserstein distance, in the input variable's units."""
    if not left or not right:
        raise ValueError("Both distributions must be nonempty")
    a, b = sorted(left), sorted(right)
    i = j = 0
    prev = min(a[0], b[0])
    area = 0.0
    for x in sorted(set(a + b)):
        area += abs(i / len(a) - j / len(b)) * (x - prev)
        while i < len(a) and a[i] == x:
            i += 1
        while j < len(b) and b[j] == x:
            j += 1
        prev = x
    return area
