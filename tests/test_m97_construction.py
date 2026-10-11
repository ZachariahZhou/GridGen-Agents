"""Failure-sensitive checks of the frozen construction benchmark evaluator."""
import copy
import importlib

import pytest


def evaluator():
    try:
        return importlib.import_module('feeder_agents.benchmark.m97_construction')
    except ModuleNotFoundError:
        pytest.fail('M97 independent construction evaluator has not been implemented')


def test_graph_descriptors_exclude_open_ties_from_energized_cycles():
    audit = evaluator().graph_descriptors(
        ['s', 'a', 'b'], [('s', 'a'), ('a', 'b')], [('s', 'b')], 's')
    assert audit['energized_cycle_rank'] == 0
    assert audit['physical_cycle_rank'] == 1
    assert audit['source_max_depth_hops'] == 2
    assert audit['energized_edges'] == 2


def test_failure_ledger_never_drops_constructor_or_export_failures():
    summary = evaluator().summarize_rows([
        {'domain': 'distribution', 'constructed': False, 'initial_accepted': False,
         'final_accepted': False, 'initial_export_pass': False, 'final_export_pass': False},
        {'domain': 'distribution', 'constructed': True, 'initial_accepted': True,
         'final_accepted': True, 'initial_export_pass': False, 'final_export_pass': False},
        {'domain': 'distribution', 'constructed': True, 'initial_accepted': False,
         'final_accepted': True, 'initial_export_pass': False, 'final_export_pass': True},
    ], expected_count=4)
    assert summary['expected'] == 4
    assert summary['completed'] == 3
    assert summary['missing'] == 1
    assert summary['constructed'] == 2
    assert summary['final_delivered'] == 1
    assert summary['constructor_failures'] == 1


def test_export_audit_rejects_load_relocation_with_unchanged_total(tmp_path):
    from feeder_agents.generation import generate_feeder
    from feeder_agents.schemas import ExperimentSpec
    from feeder_agents.simulation import export_dss
    module = evaluator()
    spec = ExperimentSpec(n_buses=7, total_kw_min=30, total_kw_max=30,
                          pv_ratio=.2, seed=17)
    feeder = generate_feeder(spec, 17)
    master = export_dss(feeder, tmp_path)
    clean = module.audit_dss_export(master, feeder.model_dump(), spec.model_dump(), 'distribution')
    assert clean['export_pass'], clean
    source = tmp_path / 'Loads.dss'
    first = feeder.loads[0]
    other_bus = feeder.loads[1].bus
    source.write_text(source.read_text().replace('bus1='+first.bus+'.', 'bus1='+other_bus+'.', 1))
    altered = module.audit_dss_export(master, feeder.model_dump(), spec.model_dump(), 'distribution')
    assert not altered['export_pass']
    assert not altered['checks']['export_load_locations']


def test_static_audit_rejects_wrong_count_and_pv_without_trusting_summary():
    from feeder_agents.generation import generate_feeder
    from feeder_agents.schemas import ExperimentSpec
    module = evaluator()
    spec = ExperimentSpec(n_buses=7, total_kw_min=30, total_kw_max=30, pv_ratio=.2)
    model = generate_feeder(spec, 17).model_dump()
    assert all(module.audit_model(model, spec.model_dump(), 'distribution')['checks'].values())
    changed = copy.deepcopy(model)
    changed['loads'][0]['pv_kw'] += 1
    changed['buses'].append(copy.deepcopy(changed['buses'][-1]))
    checked = module.audit_model(changed, spec.model_dump(), 'distribution')['checks']
    assert not checked['bus_count']
    assert not checked['pv_total']


def test_matpower_audit_rejects_changed_demand_even_when_powerflow_converges(tmp_path):
    from feeder_agents.transmission import TransmissionSpec, generate_case, export_case
    module = evaluator()
    spec = TransmissionSpec(n_buses=9, n_generators=3, total_mw=90, radius_km=10)
    case, metadata = generate_case(spec, 17)
    path = tmp_path / 'case_generated.m'
    export_case(case, path)
    clean = module.audit_transmission_export(path, case, metadata, spec.model_dump())
    assert clean['export_pass'], clean
    changed = copy.deepcopy(case)
    changed['bus'][0, 2] += 1
    export_case(changed, path)
    result = module.audit_transmission_export(path, case, metadata, spec.model_dump())
    assert not result['export_pass']
    assert not result['checks']['export_matches_json']
