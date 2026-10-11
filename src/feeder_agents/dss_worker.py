"""Own native OpenDSS calls in one persistent process and its main thread.

The pinned Free Pascal backend can retain a stale environment vector when new
host threads enter it. A freshly executed worker owns initialization, solves and
destruction. Parent environment changes and Agent thread lifetimes cannot change
that worker's environment. No native handles or exception objects cross IPC.
"""
import atexit
import json
import os
from pathlib import Path
import selectors
import struct
import subprocess
import sys
from queue import Queue
from threading import Event, Lock, Thread
import time


_TIMEOUT = 60.0
_MAX_MESSAGE = 64 * 1024 * 1024
_lock = Lock()
_process = None
_channel = None
_launcher = None
_launch_queue = None


class OpenDSSError(RuntimeError):
    """A reported native error or a failed worker, never a valid power flow."""

    def __init__(self, message, *, backend_type=None, backend_code=None):
        super().__init__(message)
        self.backend_type = backend_type
        self.backend_code = backend_code


class _PipeChannel:
    """Private inherited pipes, with no socket/network permission requirement."""

    def __init__(self, read_fd, write_fd):
        self.read_fd, self.write_fd = read_fd, write_fd
        self.timeout = None
        os.set_blocking(read_fd, False)
        os.set_blocking(write_fd, False)

    def settimeout(self, timeout):
        self.timeout = timeout

    def recv(self, size):
        self._wait(self.read_fd, selectors.EVENT_READ, self.timeout)
        return os.read(self.read_fd, size)

    @staticmethod
    def _wait(descriptor, event, timeout):
        with selectors.DefaultSelector() as selector:
            selector.register(descriptor, event)
            if not selector.select(timeout):
                raise TimeoutError('OpenDSS worker pipe operation timed out')

    def sendall(self, data):
        remaining = memoryview(data)
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        while remaining:
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                raise TimeoutError('OpenDSS worker request timed out')
            self._wait(self.write_fd, selectors.EVENT_WRITE, left)
            try:
                written = os.write(self.write_fd, remaining[:65536])
            except BlockingIOError:
                continue
            remaining = remaining[written:]

    def close(self):
        for descriptor in (self.read_fd, self.write_fd):
            os.close(descriptor)


def _send(channel, payload):
    data = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    if len(data) > _MAX_MESSAGE:
        raise ValueError('OpenDSS worker message exceeds its size limit')
    channel.sendall(struct.pack('!I', len(data)) + data)


def _receive(channel, timeout=None):
    deadline = None if timeout is None else time.monotonic() + timeout
    def read(size):
        chunks = []
        while size:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError('OpenDSS worker response timed out')
            channel.settimeout(remaining)
            part = channel.recv(size)
            if not part:
                raise EOFError('OpenDSS worker connection closed')
            chunks.append(part)
            size -= len(part)
        return b''.join(chunks)
    size = struct.unpack('!I', read(4))[0]
    if size > _MAX_MESSAGE:
        raise ValueError('OpenDSS worker message exceeds its size limit')
    return json.loads(read(size))


def _reap(process):
    if process is not None:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def _close_locked():
    global _channel, _process
    channel, process = _channel, _process
    _channel = _process = None
    if channel is not None:
        channel.close()
    _reap(process)


def close_worker():
    """Close the owned connection and reap the worker; later calls may restart."""
    with _lock:
        _close_locked()


def _after_fork():
    # The forked process must not share a protocol stream, inherited lock, or
    # ownership of the parent's worker. Closing this duplicate FD is sufficient.
    global _channel, _process, _lock, _launcher, _launch_queue
    if _channel is not None:
        _channel.close()
    _channel = _process = None
    _lock = Lock()
    _launcher = _launch_queue = None


atexit.register(close_worker)
if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_after_fork)


class _LaunchJob:
    def __init__(self, arguments, environment, inherited):
        self.arguments, self.environment, self.inherited = arguments, environment, inherited
        self.lock, self.done = Lock(), Event()
        self.abandoned = False
        self.process = self.error = None


def _launch_loop(queue):
    while True:
        job = queue.get()
        process = error = None
        try:
            with job.lock:
                abandoned = job.abandoned
            if not abandoned:
                process = subprocess.Popen(job.arguments, pass_fds=job.inherited,
                    env=job.environment, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except BaseException as exc:
            error = exc
        finally:
            # The launch job owns these FDs until Popen completes, even if the
            # requesting Agent thread is interrupted while waiting for it.
            for descriptor in job.inherited:
                os.close(descriptor)
        with job.lock:
            abandoned = job.abandoned
            if not abandoned:
                job.process, job.error = process, error
            job.done.set()
        if abandoned:
            _reap(process)


def _launch_from_owner_thread(arguments, environment, inherited):
    """Linux parent-death signaling follows a thread, so keep its owner alive.

    This daemon only launches subprocesses, never loads or calls native DSS.
    It survives short-lived Agent threads and lives until the parent process
    exits; close_worker still closes/reaps the subprocess explicitly.
    """
    global _launcher, _launch_queue, _process
    try:
        if _launcher is None or not _launcher.is_alive():
            _launch_queue = Queue()
            _launcher = Thread(target=_launch_loop, args=(_launch_queue,),
                               name='GridGenOpenDSSLauncher', daemon=True)
            try:
                _launcher.start()
            except BaseException:
                _launcher = _launch_queue = None
                raise
    except BaseException:
        for descriptor in inherited:
            os.close(descriptor)
        raise
    job = _LaunchJob(arguments, environment, inherited)
    _launch_queue.put(job)
    try:
        if not job.done.wait(_TIMEOUT):
            raise TimeoutError('OpenDSS worker launch timed out')
        with job.lock:
            if job.error is not None:
                raise job.error
            # Register ownership before returning, closing the interruption
            # window between receiving a process and assigning it in the caller.
            _process = job.process
            job.process = None
    except BaseException:
        with job.lock:
            job.abandoned = True
            process, job.process = job.process, None
        _reap(process)
        raise


def _start_locked():
    global _channel, _process
    if _process is not None and _process.poll() is None:
        return
    _close_locked()
    # Absolute package location also supports source checkouts and arbitrary cwd.
    environment = os.environ.copy()
    source = str(Path(__file__).resolve().parent.parent)
    environment['PYTHONPATH'] = source + os.pathsep + environment.get('PYTHONPATH', '')
    request_read = request_write = reply_read = reply_write = None
    try:
        request_read, request_write = os.pipe()
        reply_read, reply_write = os.pipe()
        parent = _PipeChannel(reply_read, request_write)
        arguments = [sys.executable, '-m', 'feeder_agents.dss_worker',
                     str(request_read), str(reply_write), str(os.getpid())]
        inherited = (request_read, reply_write)
        request_read = reply_write = None  # Ownership transferred to launch job.
        _launch_from_owner_thread(arguments, environment, inherited)
        _channel = parent
        request_write = reply_read = None  # Ownership transferred to _channel.
    finally:
        for descriptor in (request_read, request_write, reply_read, reply_write):
            if descriptor is not None:
                os.close(descriptor)
    ready = _receive(parent, _TIMEOUT)
    if not ready.get('ready'):
        raise OpenDSSError('OpenDSS worker initialization failed: ' + ready.get('message', 'unknown error'))


def _request(payload):
    with _lock:
        try:
            _start_locked()
            _channel.settimeout(_TIMEOUT)
            _send(_channel, payload)
            reply = _receive(_channel, _TIMEOUT)
        except BaseException as exc:
            code = _process.poll() if _process is not None else None
            _close_locked()
            if not isinstance(exc, Exception):
                raise
            raise OpenDSSError(f'OpenDSS worker failed (exit={code}): {exc}. '
                               'The request was not automatically retried.') from exc
        if not reply.get('ok'):
            raise OpenDSSError('OpenDSS execution failed: ' + reply['message'],
                               backend_type=reply.get('type'), backend_code=reply.get('code'))
        return reply['result']


def solve(master):
    return _request(dict(operation='solve', master=str(Path(master).resolve())))


def worker_diagnostics():
    """Lifecycle evidence only; this performs no AC solve."""
    return _request(dict(operation='diagnostics'))


def _bind_owner(parent_pid):
    """Terminate even a stalled native call when the persistent owner exits."""
    if not sys.platform.startswith('linux'):
        raise RuntimeError('The isolated OpenDSS worker currently requires Linux lifecycle supervision')
    import ctypes
    import signal
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'Cannot bind OpenDSS worker lifetime to its owner')
    # Close the race in which the parent exits before PR_SET_PDEATHSIG is set.
    if os.getppid() != parent_pid:
        raise RuntimeError('OpenDSS worker owner exited during startup')


def _main(read_fd, write_fd, parent_pid):
    channel = _PipeChannel(read_fd, write_fd)
    try:
        _bind_owner(parent_pid)
        from .simulation import _fresh_dss_context, _simulate_with_engine, _dss_lifecycle
        # All native work, including first initialization and finalization, stays
        # on this process's main thread. No per-request Python threads are created.
        with _fresh_dss_context():
            pass
        _send(channel, dict(ready=True))
        completed = 0
        while True:
            try:
                request = _receive(channel)
            except EOFError:
                break
            try:
                if request['operation'] == 'solve':
                    with _fresh_dss_context() as engine:
                        result = _simulate_with_engine(Path(request['master']), engine)
                    completed += 1
                elif request['operation'] == 'diagnostics':
                    _, wrapper, api, events = _dss_lifecycle()
                    result = dict(pid=os.getpid(), completed_solves=completed,
                        native_contexts=[len(wrapper._ctx_to_dss), len(api._ctx_to_util),
                                         len(events._ctx_to_manager)])
                else:
                    raise ValueError('Unknown OpenDSS worker operation')
                _send(channel, dict(ok=True, result=result))
            except Exception as exc:
                code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
                _send(channel, dict(ok=False, type=type(exc).__name__, message=str(exc), code=code))
    except Exception as exc:
        try:
            _send(channel, dict(ready=False, message=str(exc)))
        except (OSError, ValueError):
            pass
    finally:
        channel.close()


if __name__ == '__main__':
    _main(int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]))
