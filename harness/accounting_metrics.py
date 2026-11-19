"""Bounded, observe-only host timings; never model input or decision authority."""
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
import time

CURRENT = ContextVar('sutra_accounting_metrics', default=None)
SAMPLES = 256


def _state(store):
    value = getattr(store, '_accounting_metrics', None)
    if value is None:
        value = {'stages': {}, 'observer': None, 'observer_errors': 0}
        store._accounting_metrics = value
    return value


def subscribe(store, callback=None):
    if callback is not None and not callable(callback):
        raise TypeError('Timing observer must be callable')
    _state(store)['observer'] = callback


def record(store, stage, elapsed_ns, **details):
    store = store if store is not None else CURRENT.get()
    if store is None:
        return
    if type(elapsed_ns) is not int or elapsed_ns < 0 or not isinstance(stage, str):
        raise ValueError('Invalid host timing sample')
    state = _state(store)
    values = state['stages'].setdefault(stage, {'count': 0, 'total_ns': 0,
        'min_ns': elapsed_ns, 'max_ns': elapsed_ns, 'recent_ns': deque(maxlen=SAMPLES)})
    values['count'] += 1
    values['total_ns'] += elapsed_ns
    values['min_ns'] = min(values['min_ns'], elapsed_ns)
    values['max_ns'] = max(values['max_ns'], elapsed_ns)
    values['recent_ns'].append(elapsed_ns)
    if state['observer'] is not None:
        try:
            state['observer'](dict(details, stage=stage, elapsed_ns=elapsed_ns))
        except Exception:
            # Diagnostics cannot alter whether the host admits or stops work.
            state['observer_errors'] += 1


@contextmanager
def measure(store, stage, **details):
    token = CURRENT.set(store if store is not None else CURRENT.get())
    started = time.perf_counter_ns()
    try:
        yield
    finally:
        elapsed = time.perf_counter_ns() - started
        try:
            record(store, stage, elapsed, **details)
        finally:
            CURRENT.reset(token)


def snapshot(store):
    state = _state(store)
    return {'stages': {stage: dict(value, recent_ns=list(value['recent_ns']))
                       for stage, value in state['stages'].items()},
            'retained_samples_per_stage': SAMPLES,
            'observer_errors': state['observer_errors']}
