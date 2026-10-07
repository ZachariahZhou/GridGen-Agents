"""Bind constant-PQ model elements to actual DSS terminal measurements."""
import math


def element_injections_match(feeder, result, *, split_phases=False):
    expected = {key: {} for key in ('load_element_kw', 'load_element_kvar',
                                  'generation_element_kw', 'generation_element_kvar')}
    element_count = 0
    for load in feeder.loads:
        values = [(f'{load.id}_p{p.phase}', p.kw, p.kvar, p.pv_kw) for p in load.phase_powers] if split_phases else [
            (load.id, load.kw, load.kvar, load.pv_kw)]
        for name, p, q, pv in values:
            element_count += 1
            name = name.casefold()
            expected['load_element_kw'][name] = p
            expected['load_element_kvar'][name] = q
            if pv > 0:
                expected['generation_element_kw']['pv_'+name] = pv
                # Exported model=1 generators operate at fixed unity PF.
                expected['generation_element_kvar']['pv_'+name] = 0.
    checks = {}
    for key, targets in expected.items():
        actual = result.get(key)
        checks[key] = isinstance(actual, dict) and set(actual) == set(targets) and all(
            isinstance(actual[name], (int, float)) and not isinstance(actual[name], bool)
            and math.isfinite(actual[name]) and abs(actual[name]-value) <= max(1e-4, abs(value)*1e-5)
            for name, value in targets.items())
    checks['unique_element_names'] = len(expected['load_element_kw']) == element_count
    return checks
