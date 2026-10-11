"""Context-local accounting of requested AC evaluations, not Newton iterations."""
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps


_meters = ContextVar('feeder_solver_meters', default=())
_phase = ContextVar('feeder_solver_phase', default='unclassified')


class ACSearchBudgetExceeded(Exception):
    """The next search solve was prevented; partial evidence cannot be accepted."""


class SolverMeter:
    def __init__(self, max_search_ac_calls=None):
        self.limit = max_search_ac_calls
        self.events = []
        self.phases = Counter()
        self.backends = Counter()

    def __enter__(self):
        self.token = _meters.set((*_meters.get(), self))
        return self

    def __exit__(self, *exc):
        _meters.reset(self.token)

    def snapshot(self):
        return dict(total_ac_calls=len(self.events), by_phase=dict(self.phases),
                    by_backend=dict(self.backends), max_search_ac_calls=self.limit,
                    search_remaining=None if self.limit is None else self.limit-self.phases['search'],
                    unit='One requested AC snapshot evaluation, including failed attempts; excludes internal Newton iterations and OpenDSS voltage-base initialization.',
                    scope='Calls executed in this context; saved historical accounting is not new work.')


@contextmanager
def solver_phase(name):
    token = _phase.set(name)
    try:
        yield
    finally:
        _phase.reset(token)


def record_ac_solve(backend):
    meters, phase = _meters.get(), _phase.get()
    # Check every enclosing scope before incrementing any of them.
    for meter in meters:
        if phase == 'search' and meter.limit is not None and meter.phases[phase] >= meter.limit:
            raise ACSearchBudgetExceeded(f'Search AC evaluation limit {meter.limit} exhausted')
    for meter in meters:
        meter.phases[phase] += 1
        meter.backends[backend] += 1
        meter.events.append(dict(index=len(meter.events)+1, phase=phase, backend=backend))


def metered_pypower(runpf):
    @wraps(runpf)
    def solve(*args, **kwargs):
        record_ac_solve('pypower')
        return runpf(*args, **kwargs)
    return solve
