"""Exercise installed package data, actual solvers and exports without an LLM."""
import argparse
import json
import tempfile
from pathlib import Path


def run(directory, transmission=False):
    import feeder_agents
    from feeder_agents.evaluation import evaluate, verdict
    from feeder_agents.generation import generate_feeder
    from feeder_agents.hierarchy import HierarchicalSpec, generate_hierarchy, export_hierarchy, evaluate_hierarchy
    from feeder_agents.schemas import ExperimentSpec
    from feeder_agents.simulation import export_dss, simulate

    output = dict(package_file=feeder_agents.__file__, cases=[])
    for phase_mode in ('balanced', 'unbalanced'):
        spec = ExperimentSpec(n_buses=9, total_kw_min=120, total_kw_max=120, pv_ratio=.2,
                              phase_design={'mode': phase_mode}, max_repairs=0)
        model = generate_feeder(spec, 41)
        master = export_dss(model, directory/phase_mode)
        checked = verdict(evaluate(model, spec, simulate(master)), spec.mode)
        assert checked['accepted'], checked
        output['cases'].append(dict(family='single_voltage', phase_mode=phase_mode, buses=len(model.buses), accepted=True))
    spec = HierarchicalSpec(transformer_count=2, users=12, total_kw=48, pv_ratio=.2)
    model = generate_hierarchy(spec, 41)
    checked = evaluate_hierarchy(model, spec, simulate(export_hierarchy(model, directory/'hierarchy')))
    assert checked['accepted'], checked
    output['cases'].append(dict(family='hierarchical', buses=len(model.buses), accepted=True))
    if transmission:
        import numpy as np
        from feeder_agents.transmission import TransmissionSpec, generate_case, validate_case, export_case
        from feeder_agents.matpower import parse_matpower
        spec = TransmissionSpec(n_buses=9, total_mw=180)
        case, metadata = generate_case(spec, 41)
        checked, _ = validate_case(case, spec, metadata)
        assert checked['accepted'], checked
        filename = directory/'case_generated.m'
        export_case(case, filename)
        parsed = parse_matpower(filename)
        assert all(np.allclose(case[key], parsed[key], rtol=1e-10, atol=1e-10) for key in ('bus', 'gen', 'branch'))
        output['cases'].append(dict(family='transmission', buses=len(case['bus']), accepted=True, matpower_reloaded=True))
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--transmission', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='feeder_release_smoke_') as temp:
        result = run(Path(temp), args.transmission)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
