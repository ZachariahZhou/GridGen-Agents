"""Explicit, uncalibrated construction and peak-snapshot scene assumptions."""
import json
import random
from importlib.resources import files

from .schemas import ExperimentSpec, Feeder


def _profile(filename: str, name: str) -> dict:
    profiles = json.loads(files('feeder_agents').joinpath('data', filename).read_text())
    if name not in profiles:
        raise ValueError(f'Unknown research profile {name!r} in {filename}')
    return profiles[name]


def construction_profile(name: str) -> dict:
    """Return modifiers of the existing grade catalog, not equipment ratings."""
    return _profile('construction_profiles.json', name)


def scene_profile(name: str) -> dict:
    return _profile('scene_profiles.json', name)


def _checked_profile(spec: ExperimentSpec) -> dict:
    name = spec.scenario.engineering_profile
    profile = scene_profile(name)
    if name != 'generic' and profile['kind'] != spec.scenario.kind:
        raise ValueError('engineering_profile must match scenario.kind')
    return profile


def apply_scene_profile(feeder: Feeder, spec: ExperimentSpec) -> Feeder:
    """Copy a feeder and annotate construction/categories without changing power.

    Generic intentionally preserves legacy cases exactly. A dedicated seeded RNG
    isolates category sampling from geometry and peak-load allocation.
    """
    profile = _checked_profile(spec)
    if spec.scenario.engineering_profile == 'generic':
        return feeder
    result = feeder.model_copy(deep=True)
    for line in result.lines + result.tie_lines:
        line.construction = profile['construction']
    mix = profile['category_mix']
    categories = list(mix)
    rng = random.Random(f'scene_profile:{feeder.seed}:{profile["profile_id"]}')
    # Sorting makes labels stable when a serialized feeder reorders its loads.
    for load in sorted(result.loads, key=lambda load: load.id):
        load.category = rng.choices(categories, weights=list(mix.values()), k=1)[0]
    result.design_evidence['scene_profile'] = {
        **profile,
        'engineering_profile': spec.scenario.engineering_profile,
        'seed': feeder.seed,
        'category_assignment': 'seeded_per_load_probability; peak_snapshot_only',
        'category_counts': {category: sum(load.category == category for load in result.loads)
                            for category in categories},
    }
    if profile['scope'] not in result.assumptions:
        result.assumptions.append(profile['scope'])
    return result


def scene_contract_matches(feeder: Feeder, spec: ExperimentSpec) -> bool:
    """Validate actual scene attributes; provenance alone is never sufficient."""
    try:
        profile = _checked_profile(spec)
    except ValueError:
        return False
    if any(line.construction != profile['construction']
           for line in feeder.lines + feeder.tie_lines):
        return False
    if any(load.category not in profile['category_mix'] for load in feeder.loads):
        return False
    if spec.scenario.engineering_profile == 'generic':
        return True
    evidence = feeder.design_evidence.get('scene_profile')
    return isinstance(evidence, dict) and all(
        evidence.get(key) == value for key, value in {
            'profile_id': profile['profile_id'], 'kind': spec.scenario.kind,
            'construction': profile['construction'],
        }.items())
