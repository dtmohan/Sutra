"""Bounded, best-effort diagnostic events. Never a source of protocol authority."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
import sys
import time

ACTIVE = ContextVar('sutra_model_activity', default=None)
MAX_THINKING = 16000


class Activity:
    def __init__(self, envelope):
        self.identity = {key: envelope.get(key) for key in
                         ('id', 'model', 'role', 'run_id', 'session')}
        self.identity['node_id'] = (envelope.get('orchestration') or {}).get('node_id')
        self.seq = 0
        self.pending = ''
        self.shown = 0
        self.total = 0
        self.last = 0
        self.progress = None
        self.progress_final = None
        try:
            from generation_progress import Progress
            self.progress = Progress()
        except Exception:
            pass  # Optional observation must not prevent model dispatch.

    def emit(self, kind, **data):
        self.seq += 1
        if os.environ.get('SUTRA_ACTIVITY') != '1':
            return
        event = dict(self.identity, seq=self.seq, kind=kind, **data)
        try:
            sys.stderr.write('SUTRA_EVENT ' + json.dumps(event, ensure_ascii=True) + '\n')
            sys.stderr.flush()
        except (OSError, ValueError):
            pass  # A closed diagnostic consumer must not alter a policy decision.

    def thinking(self, text):
        self.total += len(text)
        if self.progress is not None:
            try:self._progress_notice(self.progress.feed(text))
            except Exception:self.progress = None
        available = max(0, MAX_THINKING - self.shown - len(self.pending))
        self.pending += text[:available]
        if time.monotonic() - self.last >= .2 or len(self.pending) >= 2048:
            self.flush()

    def flush(self):
        while self.pending:
            part, self.pending = self.pending[:2048], self.pending[2048:]
            self.shown += len(part)
            self.emit('thinking', text=part)
        self.last = time.monotonic()

    def _progress_notice(self, value):
        if value.get('alert'):
            from karma_adapter import KarmaAdapter
            self.emit('progress', generation_progress=value,
                      kernel=KarmaAdapter().generation_progress(value))

    def progress_snapshot(self, *, finish=False):
        if self.progress is None:
            return None
        try:
            if finish and self.progress_final is None:
                self.progress_final = self.progress.finish()
                self._progress_notice(self.progress_final)
            value = self.progress_final if self.progress_final is not None else self.progress.snapshot()
            return value if value.get('received_chars') else None
        except Exception:
            self.progress = None
            return None

    def finish(self, admitted, usage):
        self.flush()
        progress = self.progress_snapshot(finish=True)
        self.emit('finish', status='accepted' if admitted else 'withheld',
                  usage=usage, thinking_chars=self.total,
                  display_truncated=self.total > self.shown,
                  **({'generation_progress': progress} if progress is not None else {}))


@contextmanager
def scope(envelope):
    activity = Activity(envelope)
    token = ACTIVE.set(activity)
    activity.emit('start', status='generating',
                  budget=envelope.get('budget_profile', {}))
    try:
        yield activity
    finally:
        activity.flush()
        ACTIVE.reset(token)


def thinking_control(info, preference):
    """Use advertised controls, without inventing a named reasoning level."""
    values = info.get('thinking', {}).get('values', [])
    if preference == 'off':
        return False  # Ollama uses its lowest level if disabling is unsupported.
    if preference == 'model-default':
        return None
    if preference == 'adaptive':
        from reasoning_policy import capabilities, make
        return make(capabilities(info), preference)['requested']
    if preference != 'on':
        raise ValueError('Unknown thinking preference')
    if True in values:
        return True
    for level in ('low', 'medium', 'high', 'xhigh'):
        if level in values:
            return level
    return next((v for v in values if isinstance(v, str)), False)
