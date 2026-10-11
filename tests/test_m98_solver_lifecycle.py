"""Keep native engine failures inside a child process, without skipping real solves."""
import subprocess
import sys
import textwrap


def run_native_script(script):
    result = subprocess.run([sys.executable, '-X', 'faulthandler', '-c', textwrap.dedent(script)],
        capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr


def test_threaded_solves_release_contexts_and_preserve_results():
    run_native_script('''
        import gc
        import tempfile
        from concurrent.futures import ThreadPoolExecutor
        from pathlib import Path
        from opendssdirect import dss
        from opendssdirect.OpenDSSDirect import OpenDSSDirect
        from dss._cffi_api_util import CffiApiUtil
        from dss_python_backend.events import EventCallbackManager
        from feeder_agents.generation import generate_feeder
        from feeder_agents.schemas import ExperimentSpec
        from feeder_agents.simulation import export_dss, simulate, _simulate_with_engine

        registries = (OpenDSSDirect._ctx_to_dss, CffiApiUtil._ctx_to_util,
                      EventCallbackManager._ctx_to_manager)
        cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            masters = []
            expected = []
            for index, pv in enumerate((0, .25)):
                spec = ExperimentSpec(n_loads_min=4, n_loads_max=4,
                    total_kw_min=80, total_kw_max=80, pv_ratio=pv)
                master = export_dss(generate_feeder(spec, 97), Path(directory)/str(index))
                masters.append(master)
                # The previous implementation kept the fresh engine alive forever.
                # Compare every returned field against that same native execution.
                expected.append(_simulate_with_engine(master, dss.NewContext()))
            before = [len(registry) for registry in registries]
            for _ in range(3):
                with ThreadPoolExecutor(max_workers=4) as executor:
                    results = list(executor.map(simulate, masters*8))
                for index, result in enumerate(results):
                    assert result == expected[index % 2]
                    assert result['converged']
                    assert abs(result['load_kw']-80) < .001
                    assert abs(result['generation_kw']-(0 if index % 2 == 0 else 20)) < .001
                    assert abs(result['source_kw']+result['generation_kw']-
                        result['load_kw']-result['loss_kw']) < .001
            gc.collect()
        assert Path.cwd() == cwd
        assert [len(registry) for registry in registries] == before, before
    ''')


def test_failed_compile_releases_context_before_next_solve():
    run_native_script('''
        import gc
        import sys
        import tempfile
        from concurrent.futures import ThreadPoolExecutor
        from pathlib import Path
        from opendssdirect.OpenDSSDirect import OpenDSSDirect
        from dss._cffi_api_util import CffiApiUtil
        from feeder_agents.dss_worker import OpenDSSError
        from dss_python_backend.events import EventCallbackManager
        from feeder_agents.generation import generate_feeder
        from feeder_agents.schemas import ExperimentSpec
        from feeder_agents.simulation import export_dss, simulate

        unraisable = []
        retained_errors = []
        sys.unraisablehook = unraisable.append
        registries = (OpenDSSDirect._ctx_to_dss, CffiApiUtil._ctx_to_util,
                      EventCallbackManager._ctx_to_manager)
        before = [len(registry) for registry in registries]
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            invalid = directory/'invalid.dss'
            invalid.write_text('Clear\\nNew Circuit.bad basekv=10\\nInvalidCommand\\n')
            spec = ExperimentSpec(n_loads_min=4, n_loads_max=4,
                total_kw_min=80, total_kw_max=80)
            valid = export_dss(generate_feeder(spec, 97), directory/'valid')
            def fail_then_succeed(_):
                try:
                    simulate(invalid)
                except OpenDSSError as error:
                    assert error.backend_type == 'DSSException'
                    assert isinstance(error.backend_code, int)
                    # A UI/logger can retain tracebacks (and all local wrappers)
                    # long after the native context is released.
                    retained_errors.append(error)
                else:
                    raise AssertionError('Native compile error was swallowed')
                result = simulate(valid)
                assert result['converged']
                assert len(result['bus_voltage_pu']) == 5
                return result
            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(fail_then_succeed, range(16)))
            assert all(result == results[0] for result in results)
            assert [len(registry) for registry in registries] == before
            retained_errors.clear()
            gc.collect()
        assert not unraisable, [str(error.exc_value) for error in unraisable]
        assert [len(registry) for registry in registries] == before
    ''')


def test_budget_error_before_compile_releases_context_and_keeps_accounting():
    run_native_script('''
        import gc
        import tempfile
        from pathlib import Path
        from opendssdirect.OpenDSSDirect import OpenDSSDirect
        from dss._cffi_api_util import CffiApiUtil
        from dss_python_backend.events import EventCallbackManager
        from feeder_agents.generation import generate_feeder
        from feeder_agents.schemas import ExperimentSpec
        from feeder_agents.simulation import export_dss, simulate
        from feeder_agents.solver_accounting import (
            ACSearchBudgetExceeded, SolverMeter, solver_phase)

        registries = (OpenDSSDirect._ctx_to_dss, CffiApiUtil._ctx_to_util,
                      EventCallbackManager._ctx_to_manager)
        before = [len(registry) for registry in registries]
        with tempfile.TemporaryDirectory() as directory:
            spec = ExperimentSpec(n_loads_min=4, n_loads_max=4,
                total_kw_min=80, total_kw_max=80)
            master = export_dss(generate_feeder(spec, 97), Path(directory))
            with SolverMeter(max_search_ac_calls=0) as meter, solver_phase('search'):
                try:
                    simulate(master)
                except ACSearchBudgetExceeded:
                    pass
                else:
                    raise AssertionError('Search budget was bypassed')
            assert meter.snapshot()['total_ac_calls'] == 0
            with SolverMeter() as meter:
                assert simulate(master)['converged']
            assert meter.snapshot()['by_backend'] == {'opendss': 1}
        gc.collect()
        assert [len(registry) for registry in registries] == before
    ''')


def test_unvalidated_dependency_versions_fail_before_native_allocation():
    run_native_script('''
        from opendssdirect.OpenDSSDirect import OpenDSSDirect
        import feeder_agents.simulation as simulation
        before = len(OpenDSSDirect._ctx_to_dss)
        simulation.version = lambda name: 'unvalidated-version'
        try:
            simulation.simulate('unused.dss')
        except RuntimeError as error:
            assert 'context disposal must be validated' in str(error)
        else:
            raise AssertionError('Unsupported private disposal adapter was used')
        assert len(OpenDSSDirect._ctx_to_dss) == before
    ''')


def test_delayed_finalizers_do_not_unregister_or_release_twice():
    run_native_script('''
        import gc
        import sys
        from dss_python_backend.events import EventCallbackManager
        from feeder_agents.simulation import _fresh_dss_context

        unraisable = []
        sys.unraisablehook = unraisable.append
        unregister_calls = []
        releases = []
        with _fresh_dss_context() as engine:
            engine.Text.Command('New Circuit.finalizer basekv=10')
            api = engine._api_util
            ctx = api.ctx
            ffi = api.ffi
            manager = EventCallbackManager._ctx_to_manager[ctx]
            unregister = api.unregister_callbacks
            def counted_unregister():
                unregister_calls.append(True)
                unregister()
            class CountedFFI:
                def __getattr__(self, name):
                    return getattr(ffi, name)
                def release(self, pointer):
                    releases.append(pointer)
                    return ffi.release(pointer)
            api.unregister_callbacks = counted_unregister
            api.ffi = CountedFFI()
        assert unregister_calls == [True]
        assert releases == [ctx]
        # Tracebacks may keep either object alive. Their eventual finalizers must
        # be harmless even after a later engine has reused the native address.
        with _fresh_dss_context() as subsequent:
            subsequent.Text.Command('New Circuit.next basekv=10')
            api.__del__()
            manager.__del__()
            assert subsequent.Basic.NumCircuits() == 1
        assert unregister_calls == [True]
        assert releases == [ctx]
        assert ctx not in EventCallbackManager._ctx_to_manager
        del engine, api, manager, subsequent, unregister
        gc.collect()
        assert not unraisable, [str(error.exc_value) for error in unraisable]
    ''')
