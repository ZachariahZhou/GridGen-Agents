"""Regression for the native environment/thread fault through the public API."""
import subprocess
import sys
import textwrap

import pytest


def run_script(script, timeout=90):
    result = subprocess.run([sys.executable, '-X', 'faulthandler', '-c', textwrap.dedent(script)],
                            capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stdout + result.stderr


def test_environment_growth_and_new_agent_threads_preserve_solver_results():
    run_script('''
        import os
        os.environ['M100_BEFORE_IMPORT'] = '1'
        vector_size = (len(os.environ) + 1) * 8
        from concurrent.futures import ThreadPoolExecutor
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from feeder_agents.generation import generate_feeder
        from feeder_agents.schemas import ExperimentSpec
        from feeder_agents.simulation import export_dss, simulate, _fresh_dss_context

        # A third-party library may already have initialized native DSS in the
        # application. The supported API must remain safe even in that case.
        with _fresh_dss_context() as engine:
            engine.Text.Command('New Circuit.prior_import basekv=10')
        with TemporaryDirectory() as directory:
            spec = ExperimentSpec(n_loads_min=4, n_loads_max=4,
                                  total_kw_min=80, total_kw_max=80, pv_ratio=.25)
            master = export_dss(generate_feeder(spec, 97), Path(directory))
            baseline = simulate(master)
            from feeder_agents.dss_worker import worker_diagnostics
            original_worker = worker_diagnostics()
            for index in range(500):
                os.environ[f'M100_AFTER_IMPORT_{index}'] = 'x' * 128
            buffers = [bytearray(b'x' * (vector_size + delta))
                       for _ in range(100) for delta in range(-32, 33)]
            for _ in range(3):
                with ThreadPoolExecutor(max_workers=4) as pool:
                    results = list(pool.map(simulate, [master] * 8))
                assert all(result == baseline for result in results)
            assert baseline['converged'] and buffers
            final_worker = worker_diagnostics()
            assert final_worker['pid'] == original_worker['pid']
            assert final_worker['completed_solves'] == 25
            assert final_worker['native_contexts'] == [1, 1, 1]
            print('Environment/thread regression passed')
    ''')


SETUP = '''
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from feeder_agents.generation import generate_feeder
from feeder_agents.schemas import ExperimentSpec
from feeder_agents.simulation import export_dss, simulate
from feeder_agents import dss_worker as worker
from feeder_agents.solver_accounting import SolverMeter, solver_phase, ACSearchBudgetExceeded
directory = TemporaryDirectory()
spec = ExperimentSpec(n_loads_min=4, n_loads_max=4, total_kw_min=80, total_kw_max=80)
master = export_dss(generate_feeder(spec, 97), Path(directory.name))
'''


def test_budget_errors_and_compile_errors_preserve_caller_accounting():
    run_script(SETUP + '''
with SolverMeter(max_search_ac_calls=0) as meter, solver_phase('search'):
    try:
        simulate(master)
    except ACSearchBudgetExceeded:
        pass
    else:
        raise AssertionError('Budget exhaustion was ignored')
assert meter.snapshot()['total_ac_calls'] == 0 and worker._process is None
bad = Path(directory.name)/'bad.dss'
bad.write_text('Clear\\nNew Circuit.bad basekv=10\\nInvalidCommand\\n')
with SolverMeter(max_search_ac_calls=3) as outer, solver_phase('search'), SolverMeter() as inner:
    expected = simulate(master)
    pid = worker.worker_diagnostics()['pid']
    try:
        simulate(bad)
    except worker.OpenDSSError as error:
        assert error.backend_type == 'DSSException'
        assert error.backend_code is not None and 'InvalidCommand' in str(error)
    else:
        raise AssertionError('Invalid circuit accepted')
    assert simulate(master) == expected
assert outer.snapshot()['total_ac_calls'] == inner.snapshot()['total_ac_calls'] == 3
assert outer.snapshot()['by_phase'] == {'search': 3}
assert worker.worker_diagnostics()['pid'] == pid
assert worker.worker_diagnostics()['native_contexts'] == [1, 1, 1]
    ''')


@pytest.mark.parametrize('failure', ['exit', 'timeout'])
def test_worker_failure_is_reported_without_replay_and_next_call_can_recover(failure):
    script = SETUP + '''
import signal
import threading
baseline = simulate(master)
original_pid = worker.worker_diagnostics()['pid']
os.kill(original_pid, signal.SIGSTOP)
if FAILURE == 'timeout':
    worker._TIMEOUT = .15
else:
    killer = threading.Timer(.15, os.kill, args=(original_pid, signal.SIGKILL))
    killer.start()
with SolverMeter() as meter:
    try:
        simulate(master)
    except worker.OpenDSSError as error:
        assert 'not automatically retried' in str(error)
    else:
        raise AssertionError('Dead/stopped worker returned a valid power flow')
assert meter.snapshot()['total_ac_calls'] == 1
assert worker._process is None
try:
    os.waitpid(original_pid, os.WNOHANG)
except ChildProcessError:
    pass
else:
    raise AssertionError('Failed worker was not reaped')
worker._TIMEOUT = 60
assert simulate(master) == baseline
assert worker.worker_diagnostics()['pid'] != original_pid
    '''
    run_script('FAILURE = ' + repr(failure) + '\n' + script)


def test_worker_close_and_fork_do_not_share_parent_channel_or_lock():
    run_script(SETUP + '''
import json
baseline = simulate(master)
parent_pid = worker.worker_diagnostics()['pid']
report = Path(directory.name)/'fork.json'
with worker._lock:
    child = os.fork()
if child == 0:
    try:
        assert simulate(master) == baseline
        report.write_text(json.dumps(worker.worker_diagnostics()))
        worker.close_worker()
    except BaseException:
        os._exit(1)
    os._exit(0)
_, status = os.waitpid(child, 0)
assert status == 0
assert json.loads(report.read_text())['pid'] != parent_pid
assert worker.worker_diagnostics()['pid'] == parent_pid
assert simulate(master) == baseline
process = worker._process
worker.close_worker()
assert process.poll() == 0 and worker._process is None
assert simulate(master) == baseline
assert worker.worker_diagnostics()['pid'] != parent_pid
    ''')


def test_public_solve_does_not_import_native_backend_or_change_parent_environment():
    run_script(SETUP + '''
import sys
environment, cwd = dict(os.environ), Path.cwd()
assert 'opendssdirect' not in sys.modules
assert simulate(master)['converged']
assert 'opendssdirect' not in sys.modules and 'dss_python_backend' not in sys.modules
assert dict(os.environ) == environment and Path.cwd() == cwd
    ''')


def test_worker_supports_high_numbered_descriptors():
    run_script(SETUP + '''
handles = []
try:
    while not handles or handles[-1] < 1100:
        handles.append(os.open(os.devnull, os.O_RDONLY))
    assert simulate(master)['converged']
finally:
    worker.close_worker()
    for handle in handles:
        os.close(handle)
    ''')


def test_pipe_backpressure_obeys_the_send_deadline():
    run_script('''
        import os
        import fcntl
        import time
        from feeder_agents.dss_worker import _PipeChannel
        read_fd, write_fd = os.pipe()
        channel = _PipeChannel(read_fd, write_fd)
        capacity = fcntl.fcntl(write_fd, fcntl.F_GETPIPE_SZ)
        os.write(write_fd, b'x' * (capacity // 2))
        channel.settimeout(.1)
        started = time.monotonic()
        try:
            channel.sendall(b'x' * (capacity * 2))
        except TimeoutError:
            assert time.monotonic() - started < 2
        else:
            raise AssertionError('A full pipe accepted an oversized write')
        finally:
            channel.close()
    ''', timeout=4)


def test_failed_pipe_setup_does_not_leak_descriptors(monkeypatch):
    import os
    from feeder_agents import dss_worker as worker
    worker.close_worker()
    original_pipe, opened = os.pipe, []
    def failing_pipe():
        if opened:
            raise OSError(24, 'injected descriptor limit')
        pair = original_pipe()
        opened.extend(pair)
        return pair
    monkeypatch.setattr(worker.os, 'pipe', failing_pipe)
    try:
        with pytest.raises(worker.OpenDSSError):
            worker.worker_diagnostics()
        for descriptor in opened:
            with pytest.raises(OSError):
                os.fstat(descriptor)
    finally:
        for descriptor in opened:
            try:
                os.close(descriptor)
            except OSError:
                pass


def test_retiring_first_agent_thread_does_not_end_persistent_worker():
    run_script(SETUP + '''
from concurrent.futures import ThreadPoolExecutor
with ThreadPoolExecutor(max_workers=1) as pool:
    baseline = pool.submit(simulate, master).result()
    original = pool.submit(worker.worker_diagnostics).result()
assert simulate(master) == baseline
assert worker.worker_diagnostics()['pid'] == original['pid']
    ''')


def test_killing_owner_also_kills_a_stalled_native_worker():
    run_script('''
        import os
        import signal
        import subprocess
        import sys
        import time
        from feeder_agents.bounded_execution import _subreaper_scope
        code = ('from feeder_agents.dss_worker import worker_diagnostics; '
                'import time; print(worker_diagnostics()["pid"], flush=True); time.sleep(120)')
        with _subreaper_scope():
            owner = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, text=True)
            native = int(owner.stdout.readline())
            try:
                os.kill(native, signal.SIGSTOP)
                owner.kill()
                owner.wait(timeout=5)
                deadline = time.monotonic() + 3
                observed = None
                while time.monotonic() < deadline:
                    pid, status = os.waitpid(native, os.WNOHANG)
                    if pid:
                        observed = status
                        native = None
                        break
                    time.sleep(.01)
                assert observed is not None, 'Stalled native worker outlived its owner'
                assert os.WIFSIGNALED(observed) and os.WTERMSIG(observed) == signal.SIGKILL
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.wait()
                if native is not None:
                    os.kill(native, signal.SIGKILL)
                    os.waitpid(native, 0)
    ''')


def test_launcher_start_failure_can_recover_without_hanging():
    run_script('''
        from feeder_agents import dss_worker as worker
        original = worker.Thread.start
        def fail(self):
            raise RuntimeError('injected thread resource limit')
        worker.Thread.start = fail
        try:
            try:
                worker.worker_diagnostics()
            except worker.OpenDSSError:
                pass
            else:
                raise AssertionError('Thread creation failure ignored')
        finally:
            worker.Thread.start = original
        assert worker._launcher is None
        assert worker.worker_diagnostics()['native_contexts'] == [1, 1, 1]
    ''')


def test_interrupted_launch_reaps_the_abandoned_child():
    run_script('''
        import os
        import signal
        import threading
        import time
        from feeder_agents import dss_worker as worker
        original = worker.subprocess.Popen
        created, release = [], threading.Event()
        def delayed(*args, **kwargs):
            process = original(*args, **kwargs)
            created.append(process)
            os.kill(os.getpid(), signal.SIGINT)
            release.wait(timeout=5)
            return process
        worker.subprocess.Popen = delayed
        try:
            try:
                worker.worker_diagnostics()
            except KeyboardInterrupt:
                pass
            else:
                raise AssertionError('Interrupt was swallowed')
        finally:
            worker.subprocess.Popen = original
            release.set()
        assert len(created) == 1
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and created[0].returncode is None:
            time.sleep(.01)
        # Reading returncode avoids reaping on behalf of the implementation.
        assert created[0].returncode is not None, 'Abandoned launch was not reaped'
        assert worker.worker_diagnostics()['native_contexts'] == [1, 1, 1]
    ''')
