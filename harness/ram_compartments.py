"""Bounded decoded JSON copies for one persistent host worker.

Durable rows and their existing validators remain authoritative. This module
retains decoded values only. Callers receive independent copies on every hit.
"""
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
import json
import sys
from threading import RLock
from time import perf_counter_ns


CURRENT = ContextVar('sutra_ram_compartments', default=None)
COUNTERS = ('active_hits', 'warm_hits', 'misses', 'bypasses', 'evictions',
            'decode_ns', 'copy_ns', 'lookup_ns')


def _counters():
    return dict.fromkeys(COUNTERS, 0)


def _size(value, seen=None):
    """Count retained Python objects once within an entry, including strings."""
    seen = set() if seen is None else seen
    marker = id(value)
    if marker in seen:
        return 0
    seen.add(marker)
    amount = sys.getsizeof(value)
    if isinstance(value, dict):
        amount += sum(_size(key, seen) + _size(item, seen) for key, item in value.items())
    elif isinstance(value, (list, tuple)):
        amount += sum(_size(item, seen) for item in value)
    return amount


def _detach(value):
    """Copy JSON containers while retaining immutable scalar values."""
    if isinstance(value, dict):
        return {key: _detach(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_detach(item) for item in value]
    return value


class Compartments:
    def __init__(self, max_bytes):
        self._lock = RLock()
        self._request_lock = RLock()
        self._active = OrderedDict()
        self._warm = OrderedDict()
        self._active_bytes = 0
        self._warm_bytes = 0
        self._totals = _counters()
        self._request_stats = None
        self._last_request = _counters()
        self._requests = 0
        self._clear_reason = None
        self.resize(max_bytes)

    def _count(self, key, amount=1):
        self._totals[key] += amount
        if self._request_stats is not None:
            self._request_stats[key] += amount

    def _trim(self, incoming=0):
        while self._active_bytes + self._warm_bytes + incoming > self.max_bytes:
            if self._warm:
                _, (_, size) = self._warm.popitem(last=False)
                self._warm_bytes -= size
            elif self._active:
                _, (_, size) = self._active.popitem(last=False)
                self._active_bytes -= size
            else:
                break
            self._count('evictions')

    def resize(self, max_bytes):
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError('RAM compartment allocation must be a nonnegative integer in bytes')
        with self._lock:
            self.max_bytes = max_bytes
            self._trim()

    def clear(self, reason='host_requested'):
        with self._lock:
            self._count('evictions', len(self._active) + len(self._warm))
            self._active.clear()
            self._warm.clear()
            self._active_bytes = self._warm_bytes = 0
            self._clear_reason = str(reason)[:200]

    @contextmanager
    def request(self, scope):
        hash(scope)
        if CURRENT.get() is not None and CURRENT.get()[0] is self:
            raise RuntimeError('RAM compartment requests cannot be nested on the same worker')
        with self._request_lock:
            with self._lock:
                self._warm.update(self._active)
                self._warm_bytes += self._active_bytes
                self._active.clear()
                self._active_bytes = 0
                self._request_stats = _counters()
                self._requests += 1
            token = CURRENT.set((self, scope))
            try:
                yield self
            finally:
                CURRENT.reset(token)
                with self._lock:
                    self._last_request = dict(self._request_stats)
                    self._request_stats = None

    def decode(self, request_scope, scope, raw):
        hash(scope)
        if isinstance(raw, bytearray):
            raw = bytes(raw)
        key = (request_scope, scope, raw)
        started = perf_counter_ns()
        with self._lock:
            entry = self._active.pop(key, None)
            if entry is not None:
                self._active[key] = entry
                self._count('active_hits')
            else:
                entry = self._warm.pop(key, None)
                if entry is not None:
                    self._warm_bytes -= entry[1]
                    self._active[key] = entry
                    self._active_bytes += entry[1]
                    self._count('warm_hits')
                else:
                    self._count('misses')
            self._count('lookup_ns', perf_counter_ns() - started)
        if entry is not None:
            started = perf_counter_ns()
            value = _detach(entry[0])
            with self._lock:
                self._count('copy_ns', perf_counter_ns() - started)
            return value

        started = perf_counter_ns()
        try:
            value = json.loads(raw)
        finally:
            with self._lock:
                self._count('decode_ns', perf_counter_ns() - started)
        with self._lock:
            if self.max_bytes == 0 or sys.getsizeof(raw) > self.max_bytes:
                self._count('bypasses')
                return value
        # Include a conservative mapping-node/entry allowance as well as the
        # actual Python objects. The cache does not claim this is process RSS.
        try:
            retained = _size((key, value)) + 256
        except RecursionError:
            with self._lock:
                self._count('bypasses')
            return value
        with self._lock:
            if retained > self.max_bytes:
                self._count('bypasses')
                return value
        started = perf_counter_ns()
        try:
            detached = _detach(value)
        except RecursionError:
            with self._lock:
                self._count('bypasses')
            return value
        finally:
            with self._lock:
                self._count('copy_ns', perf_counter_ns() - started)
        with self._lock:
            if retained > self.max_bytes:
                self._count('bypasses')
                return detached
            self._trim(retained)
            self._active[key] = (value, retained)
            self._active_bytes += retained
        return detached

    def snapshot(self):
        with self._lock:
            return dict(self._totals, enabled=self.max_bytes > 0, max_bytes=self.max_bytes,
                        requests=self._requests, active_entries=len(self._active),
                        warm_entries=len(self._warm), active_bytes=self._active_bytes,
                        warm_bytes=self._warm_bytes,
                        retained_bytes=self._active_bytes + self._warm_bytes,
                        byte_accounting='recursive_sys_getsizeof_plus_entry_allowance',
                        last_request=dict(self._last_request), clear_reason=self._clear_reason)


def decode(scope, raw):
    current = CURRENT.get()
    if current is None:
        return json.loads(raw)
    cache, request_scope = current
    return cache.decode(request_scope, scope, raw)
