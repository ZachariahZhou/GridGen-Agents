import copy

import pytest


def test_witness_targets_exclude_trivial_baseline_without_tuning_width():
    from feeder_agents.benchmark.m97_response import target_intervals
    targets = target_intervals({'a': 1.18, 'b': .93}, .15)
    assert targets[0]['lower'] == pytest.approx(1.153)
    assert targets[1]['upper'] == pytest.approx(.9405)
    with pytest.raises(ValueError, match='trivial'):
        target_intervals({'a': 1.00001, 'b': .99999}, .15)


def test_certification_checks_components_not_only_aggregate():
    from feeder_agents.benchmark.m97_response import components_agree
    full = {'probes': {'a': {'valid': True, 'signed_response': {'b1.1': .2, 'b1.2': .1}}}}
    half = copy.deepcopy(full)
    assert components_agree(full, half, .03)
    half['probes']['a']['signed_response'] = {'b1.1': .1, 'b1.2': .2}
    assert not components_agree(full, half, .03)


def test_public_payload_has_no_private_certificate_or_action():
    from feeder_agents.benchmark.m97_response import public_payload, validate_public
    plan = dict(base_spec={})
    task = public_payload('n1', 't1', 'voltage_q', 'low', 'geometry', plan)
    assert set(task) == {'network_id', 'task_id', 'response_kind', 'coupling', 'witness_family', 'plan'}
    assert validate_public(task)
    with pytest.raises(ValueError, match='private'):
        validate_public(dict(task, witness_actions=[{'kind': 'scale_layout', 'factor': 1.2}]))


def test_continuous_map_roundtrip_and_protected_contract():
    from feeder_agents.benchmark.m97_continuous import apply_coordinates, recover_coordinates
    from feeder_agents.response_measurement import create_model
    from feeder_agents.schemas import ExperimentSpec
    import networkx as nx
    spec = ExperimentSpec(n_buses=13, total_kw_min=100, total_kw_max=100, seed=987,
                          scenario={'kind': 'rural', 'engineering_profile': 'rural'})
    model = create_model(spec)
    tree = nx.bfs_tree(nx.Graph((e.bus1, e.bus2) for e in model['feeder'].lines), model['feeder'].source_bus)
    path = max(nx.single_source_shortest_path(tree, model['feeder'].source_bus).values(), key=len)
    children = [path[1], path[-1]]
    changed = apply_coordinates(model, spec, children, [1.08, .91])
    actual, residual = recover_coordinates(model, changed, children)
    assert actual == pytest.approx([1.08, .91], abs=1e-8)
    assert residual < 1e-8
    assert [l.kw for l in changed['feeder'].loads] == [l.kw for l in model['feeder'].loads]
    assert [e.conductor for e in changed['feeder'].lines] == [e.conductor for e in model['feeder'].lines]


def test_transmission_random_adapter_restores_on_error():
    from feeder_agents.benchmark.m97_response import response_adapter
    import feeder_agents.response_design as runner
    before = runner.propose_response_actions
    with pytest.raises(RuntimeError):
        with response_adapter('random', 9, 'transmission'):
            assert runner.propose_response_actions is not before
            raise RuntimeError('forced')
    assert runner.propose_response_actions is before


def test_coordinate_domain_rejects_unauthorized_direction_and_bounds():
    from feeder_agents.benchmark.m97_continuous import action_uses_variables
    assert action_uses_variables({'kind': 'scale_subtree', 'child_bus': 'b1'}, ['b1', 'b2'])
    assert not action_uses_variables({'kind': 'scale_subtree', 'child_bus': 'b3'}, ['b1', 'b2'])
    assert not action_uses_variables({'kind': 'replace_conductor'}, ['b1', 'b2'])
    assert not action_uses_variables({'kind': 'compound', 'actions': [
        {'kind': 'scale_subtree', 'child_bus': 'b1'}, {'kind': 'scale_layout'}]}, ['b1', 'b2'])


def test_duplicate_evaluation_cache_does_not_treat_missing_as_zero_cost():
    from feeder_agents.benchmark.m97_continuous import EvaluationCache
    calls = []
    cache = EvaluationCache(lambda x: calls.append(list(x)) or {'value': sum(x)})
    assert cache([1., 1.]) == cache([1., 1.])
    assert len(calls) == 1 and cache.hits == 1
    with pytest.raises(ValueError):
        cache([float('nan'), 1.])


def _runner():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1]/'scripts/run_m97_response.py'
    spec = importlib.util.spec_from_file_location('m97_runner_tests', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runner_rejects_live_import_when_snapshot_is_required(tmp_path, monkeypatch):
    from feeder_agents.artifacts import atomic_json, digest, file_digest
    module = _runner()
    (tmp_path/'execution_scripts').mkdir()
    script = tmp_path/'execution_scripts/run_m97_response.py'
    script.write_bytes(__import__('pathlib').Path(module.__file__).read_bytes())
    protocol = dict(source_files={}, runtime={}, script_hash=file_digest(script))
    atomic_json(tmp_path/'protocol.json', protocol)
    atomic_json(tmp_path/'protocol_checksum.json', dict(sha256=digest(protocol)))
    monkeypatch.setattr(module, 'runtime_versions', lambda: {})
    with pytest.raises(ValueError, match='active|Active'):
        module.verify(tmp_path)


def test_job_identity_binds_task_arm_seed_and_parameterization():
    module = _runner()
    job = dict(kind='response', task={'task_id': 't'}, arm='full', seed=None, budget=160)
    assert module.expected_job_identity(job) != module.expected_job_identity(dict(job, arm='fixed'))
    first = dict(kind='continuous', task={'children': ['a','b']}, arm='guided', seed=None, budget=160)
    assert module.expected_job_identity(first) != module.expected_job_identity(dict(first, task={'children': ['b','c']}))


def test_restricted_variables_are_filtered_before_joint_shortlist(monkeypatch):
    from feeder_agents.benchmark.m97_continuous import restricted_proposer
    import feeder_agents.response_flexibility as flex
    import feeder_agents.response_compensation as compensation
    moves = [dict(kind='scale_subtree', child_bus=f'b{i}') for i in range(20)]
    monkeypatch.setattr(flex, 'distribution_actions', lambda *args, **kwargs: moves)
    monkeypatch.setattr(compensation, 'compensation_actions', lambda *args, **kwargs: moves)
    with restricted_proposer(['b18', 'b19']):
        assert [a['child_bus'] for a in flex.distribution_actions()] == ['b18', 'b19']
        assert [a['child_bus'] for a in compensation.compensation_actions()] == ['b18', 'b19']
    assert len(flex.distribution_actions()) == 20


def test_core_artifact_audit_detects_selected_model_tampering(tmp_path):
    from feeder_agents.benchmark.m97_response import core_artifacts, verify_artifacts
    (tmp_path/'selected').mkdir()
    model = tmp_path/'selected/feeder.json'; model.write_text('{"nodes":37}')
    inventory = core_artifacts(tmp_path)
    verify_artifacts(tmp_path, inventory)
    model.write_text('{"nodes":38}')
    with pytest.raises(ValueError, match='artifact'):
        verify_artifacts(tmp_path, inventory)
