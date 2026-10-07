"""Supervised reusable sample workers, separate from scientific specifications.

The supervisor owns only this batch's workers and descendants that remain in
their process groups. On Linux it adopts and reaps those orphaned descendants.
Detached sessions require stronger isolation and are outside this contract.
"""
from collections import deque
from dataclasses import asdict, dataclass
import ctypes
from contextlib import contextmanager
import math
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import time
import threading


INTERRUPTED_STATUSES = frozenset({'sample_timeout', 'batch_timeout', 'cancelled', 'supervisor_error'})
_SUBREAPER_LOCK = threading.Lock()
_SUBREAPER_USERS = 0
_SUBREAPER_PREVIOUS = 0


@dataclass(frozen=True)
class ExecutionLimits:
    sample_timeout_seconds: float = 900.
    batch_timeout_seconds: float | None = None
    terminate_grace_seconds: float = 1.
    worker_startup_timeout_seconds: float = 60.
    cancel_file: str | Path | None = None
    resume_interrupted: bool = False

    def __post_init__(self):
        for name in ('sample_timeout_seconds', 'batch_timeout_seconds',
                     'terminate_grace_seconds', 'worker_startup_timeout_seconds'):
            value = getattr(self, name)
            if name == 'batch_timeout_seconds' and value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} must be positive and finite')
        if not isinstance(self.resume_interrupted, bool):
            raise ValueError('resume_interrupted must be boolean')
        if self.cancel_file is not None:
            object.__setattr__(self, 'cancel_file', str(Path(self.cancel_file).resolve()))

    def payload(self):
        return asdict(self)


def _worker_loop(connection, function, parent_pid):
    if sys.platform.startswith('linux'):
        # Close the spawn/register race: an unregistered worker cannot outlive
        # its supervisor or begin a task after the supervisor has disappeared.
        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), 'Cannot bind worker lifetime to supervisor')
        if os.getppid() != parent_pid:
            return
    os.setsid()
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    connection.send(('ready', None))
    try:
        while True:
            job = connection.recv()
            if job is None:
                return
            index, payload = job
            try:
                function(payload)  # The workflow atomically commits its own artifacts.
            except BaseException as exc:
                connection.send(('failed', type(exc).__name__))
                return
            connection.send(('completed', index))
    finally:
        connection.close()


def _enable_subreaper():
    if sys.platform.startswith('linux'):
        if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), 'Cannot enable owned-descendant reaping')


@contextmanager
def _subreaper_scope():
    """Restore the caller's Linux setting; concurrent batches share one lease."""
    global _SUBREAPER_USERS, _SUBREAPER_PREVIOUS
    if not sys.platform.startswith('linux'):
        yield
        return
    libc = ctypes.CDLL(None, use_errno=True)
    with _SUBREAPER_LOCK:
        if not _SUBREAPER_USERS:
            previous = ctypes.c_int()
            if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) != 0:
                raise OSError(ctypes.get_errno(), 'Cannot read descendant-reaping policy')
            _SUBREAPER_PREVIOUS = previous.value
            _enable_subreaper()
        _SUBREAPER_USERS += 1
    try:
        yield
    finally:
        with _SUBREAPER_LOCK:
            _SUBREAPER_USERS -= 1
            if not _SUBREAPER_USERS:
                if libc.prctl(36, _SUBREAPER_PREVIOUS, 0, 0, 0) != 0:
                    raise OSError(ctypes.get_errno(), 'Cannot restore descendant-reaping policy')


def _process_identity(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return dict(pid=pid, parent_pid=int(fields[1]), group=int(fields[2]),
                    session=int(fields[3]), starttime=int(fields[19]))
    except (OSError, ValueError, IndexError):
        return None


def _reap_children(wait_target, grace):
    deadline = time.monotonic() + grace
    while True:
        try:
            child, _ = os.waitpid(wait_target, os.WNOHANG)
        except ChildProcessError:
            return
        if child:
            continue
        if time.monotonic() >= deadline:
            raise RuntimeError('Owned descendants did not exit after SIGKILL')
        time.sleep(.01)


def _cleanup_registered_worker(identity, grace):
    """Only stop the registered process incarnation, never a reused PID."""
    pid = identity['pid']
    current = _process_identity(pid)
    if current is not None and current['starttime'] != identity['starttime']:
        return
    if current is not None:
        try:
            if current['group'] == current['session'] == pid and pid != os.getpgrp():
                os.killpg(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif sys.platform.startswith('linux'):
        # The supervisor may have reaped the session leader just before dying.
        # Remaining members are adopted children in the registered session.
        adopted = set()
        for children in Path(f'/proc/{os.getpid()}/task').glob('*/children'):
            try: adopted.update(children.read_text().split())
            except FileNotFoundError: pass
        for value in adopted:
            child = _process_identity(int(value))
            if (child is not None and child['parent_pid'] == os.getpid()
                    and child['group'] == child['session'] == pid
                    and child['starttime'] >= identity['starttime']):
                try: os.kill(child['pid'], signal.SIGKILL)
                except ProcessLookupError: pass
    if sys.platform.startswith('linux'):
        _reap_children(-pid, grace)
        # A worker killed during bootstrap has not called setsid yet.
        if current is not None and current['group'] != pid:
            _reap_children(pid, grace)


def _stop_worker(worker, grace):
    process = worker['process']
    pid = process.pid
    owned_group = worker['group_owned']
    if not owned_group:
        try:
            owned_group = os.getpgid(pid) == pid
        except ProcessLookupError:
            pass
    owned_group = owned_group and pid != os.getpgrp()

    def send(sig):
        try:
            if owned_group:
                os.killpg(pid, sig)
            elif process.is_alive():
                os.kill(pid, sig)
        except ProcessLookupError:
            pass

    send(signal.SIGTERM)
    process.join(timeout=grace)
    # The leader may already have exited while descendants still own the group.
    send(signal.SIGKILL)
    process.join(timeout=grace)
    if process.is_alive():
        raise RuntimeError('Owned worker did not exit after SIGKILL')
    if owned_group and sys.platform.startswith('linux'):
        _reap_children(-pid, grace)
    code = process.exitcode
    worker['connection'].close()
    process.close()
    return code


def _supervise(function, payloads, count, limits, stop, report, batch_started):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    pool = []
    emitted = set()
    pending = deque(enumerate(payloads))
    context = multiprocessing.get_context('spawn')

    def emit(index, status, worker=None, started=False, **extra):
        record = dict(index=index, status=status, started=started, **extra)
        if worker is not None:
            record.update(worker_pid=worker['pid'],
                          elapsed_seconds=round(time.monotonic()-worker['started'], 6))
        emitted.add(index)
        report.send(('job', record))

    def launch():
        receive, send = context.Pipe()
        process = context.Process(target=_worker_loop, args=(send, function, os.getpid()))
        process.start()
        send.close()
        identity = _process_identity(process.pid)
        if identity is not None:
            report.send(('worker', identity))
        pool.append(dict(process=process, pid=process.pid, connection=receive, ready=False,
                         group_owned=False, job=None, started=time.monotonic()))

    def stop_worker(worker):
        code = _stop_worker(worker, limits.terminate_grace_seconds)
        report.send(('worker_stopped', worker['pid']))
        return code

    try:
        _enable_subreaper()
        while pending or any(w['job'] is not None for w in pool):
            # Read completion before testing deadlines so committed work wins a race.
            for worker in list(pool):
                connection = worker['connection']
                try:
                    if connection.poll():
                        kind, value = connection.recv()
                        if kind == 'ready':
                            worker['ready'] = worker['group_owned'] = True
                        elif kind == 'completed':
                            if worker['job'] is None or value != worker['job'][0]:
                                raise RuntimeError('Worker completion does not match its assigned sample')
                            emit(value, 'completed', worker, True)
                            worker['job'] = None
                            worker['ready'] = True
                        elif kind == 'failed':
                            code = stop_worker(worker)
                            if worker['job'] is not None:
                                emit(worker['job'][0], 'worker_error', worker, True, error_type=value, worker_exitcode=code)
                            pool.remove(worker)
                except (EOFError, BrokenPipeError, ConnectionResetError):
                    pass

            cancelled = stop.is_set() or (limits.cancel_file is not None and Path(limits.cancel_file).exists())
            expired = limits.batch_timeout_seconds is not None and time.monotonic()-batch_started >= limits.batch_timeout_seconds
            reason = 'cancelled' if cancelled else 'batch_timeout' if expired else None
            if reason:
                for worker in list(pool):
                    code = stop_worker(worker)
                    if worker['job'] is not None:
                        emit(worker['job'][0], reason, worker, True, worker_exitcode=code)
                    pool.remove(worker)
                while pending:
                    emit(pending.popleft()[0], reason)
                break

            for worker in list(pool):
                active = worker['job'] is not None
                elapsed = time.monotonic()-worker['started']
                timed_out = active and elapsed >= limits.sample_timeout_seconds
                startup_failed = not worker['ready'] and not active and elapsed >= limits.worker_startup_timeout_seconds
                dead = not worker['process'].is_alive()
                if timed_out or startup_failed or dead:
                    status = 'sample_timeout' if timed_out else 'worker_error'
                    code = stop_worker(worker)
                    if active:
                        emit(worker['job'][0], status, worker, True, worker_exitcode=code)
                    elif pending:
                        emit(pending.popleft()[0], status, error_type='WorkerStartupFailure', worker_exitcode=code)
                    pool.remove(worker)

            while pending and len(pool) < min(count, len(pending) + sum(w['job'] is not None for w in pool)):
                launch()
            for worker in pool:
                if pending and worker['ready'] and worker['job'] is None:
                    worker['job'] = pending.popleft()
                    worker['ready'] = False
                    worker['started'] = time.monotonic()
                    worker['connection'].send(worker['job'])
                    report.send(('started', dict(index=worker['job'][0], worker_pid=worker['pid'])))
            time.sleep(.02)
    except BaseException as exc:
        active = {worker['job'][0]: worker for worker in pool if worker['job'] is not None}
        exitcodes = {}
        # Never publish failure while a worker can still commit its sample.
        # Cleanup also precedes the caller's durable interruption marker.
        for worker in list(pool):
            exitcodes[worker['pid']] = stop_worker(worker)
            pool.remove(worker)
        for index in range(len(payloads)):
            if index not in emitted:
                worker = active.get(index)
                emit(index, 'supervisor_error', worker, worker is not None,
                     error_type=type(exc).__name__,
                     worker_exitcode=exitcodes.get(worker['pid']) if worker is not None else None)
    finally:
        for worker in pool:
            stop_worker(worker)
        report.send(('done', None))
        report.close()


def _run_bounded_jobs(function, payloads, workers, limits, cancel_event=None, *, batch_started=None, on_result=None):
    """Return execution records; targets must durably commit results themselves."""
    if not payloads:
        return []
    context = multiprocessing.get_context('spawn')
    stop = context.Event()
    receive, send = context.Pipe(duplex=False)
    supervisor = context.Process(target=_supervise,
        args=(function, payloads, workers, limits, stop, send,
              time.monotonic() if batch_started is None else batch_started))
    supervisor.start()
    send.close()
    records = {}
    owned = {}
    running = {}
    finished = False
    try:
        while not finished:
            try:
                if cancel_event is not None and cancel_event.is_set():
                    stop.set()
                if receive.poll(.05):
                    kind, value = receive.recv()
                    if kind == 'job':
                        records[value['index']] = value
                        running.pop(value['index'], None)
                        if on_result is not None:
                            on_result(value)
                    elif kind == 'worker':
                        owned[value['pid']] = value
                    elif kind == 'worker_stopped':
                        owned.pop(value, None)
                    elif kind == 'started':
                        running[value['index']] = value
                    elif kind == 'done':
                        finished = True
                elif not supervisor.is_alive():
                    break
            except KeyboardInterrupt:
                stop.set()
            except EOFError:
                break
    finally:
        stop.set()
        supervisor.join(timeout=max(2., (workers+1)*limits.terminate_grace_seconds*3))
        if supervisor.is_alive():
            supervisor.kill()
            supervisor.join(timeout=limits.terminate_grace_seconds)
        # This also runs when the supervisor unexpectedly exited or was killed.
        # Successfully cleaned workers were explicitly removed from the registry.
        for identity in owned.values():
            _cleanup_registered_worker(identity, limits.terminate_grace_seconds)
        receive.close()
        supervisor.close()
    for index in range(len(payloads)):
        if index not in records:
            records[index] = dict(index=index, status='supervisor_error', started=index in running,
                                  worker_pid=running.get(index, {}).get('worker_pid'),
                                  error_type='SupervisorExitedWithoutCompletion')
            if on_result is not None:
                on_result(records[index])
    return [records[index] for index in range(len(payloads))]



def run_bounded_jobs(function, payloads, workers, limits, cancel_event=None, *, batch_started=None, on_result=None):
    """Isolate native samples and restore caller process policy after cleanup."""
    with _subreaper_scope():
        return _run_bounded_jobs(function, payloads, workers, limits, cancel_event,
                                 batch_started=batch_started, on_result=on_result)
