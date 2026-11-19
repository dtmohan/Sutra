"""Optional read-only background audit with controller-owned quarantine delivery.

An externally anchored fast reader can request detailed source verification
without sharing its SQLite connection across threads. The controller must poll
before its next admission. Audit failures are findings, never repaired totals.
"""
from pathlib import Path
from contextlib import closing
import queue
import sqlite3
import tempfile
import threading
import time


class BackgroundAudit:
    def __init__(self):
        self._results = queue.Queue(maxsize=1)
        self._thread = None
        self._path = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, path):
        if self._thread is not None:
            raise RuntimeError('Poll the previous accounting audit before starting another')
        owner = path if hasattr(path, 'db') else None
        self._path = str(Path(owner.path if owner is not None else path).resolve())
        if owner is not None:
            owner._accounting_background_audit = self

        def work():
            from accounting_batches import verify, status
            from invocation_gateway import InvocationLedger
            from governance import policy_hash
            from context_manager import InvalidUpdate
            started = time.perf_counter_ns()
            # Skip constructors: they initialize writable schemas. This worker
            # opens an existing ledger with an explicitly read-only connection.
            store = None
            phase = 'snapshot'
            try:
                # A long read transaction against a DELETE-journal live ledger
                # can block inference receipts. Online backup releases source
                # read locks between small steps; audit the private snapshot.
                with tempfile.TemporaryDirectory(prefix='sutra-accounting-audit-') as folder:
                    snapshot = Path(folder) / 'ledger.sqlite'
                    # Mixed OS-readonly/readwrite descriptors trigger an EBADF
                    # advisory-lock race in this Mac's system SQLite 3.54.
                    # An existing-file RW descriptor with SQL query_only stays
                    # nonmutating while using the writer-compatible lock path.
                    with closing(sqlite3.connect(Path(self._path).as_uri() + '?mode=rw', uri=True, timeout=5)) as live:
                        live.execute('PRAGMA query_only=ON')
                        with closing(sqlite3.connect(snapshot)) as copied:
                            live.backup(copied, pages=128, sleep=.001)
                    store = object.__new__(InvocationLedger)
                    store.path = str(snapshot)
                    store.db = sqlite3.connect(snapshot.as_uri() + '?mode=ro', uri=True, timeout=5)
                    store.db.execute('PRAGMA query_only=ON')
                    store.logic_hash = policy_hash()
                    store._audit_verified = None
                    store._audit_file_identity = store._audit_identity()
                    store.accounting_read_only = True
                    try:
                        phase = 'verify'
                        verify(store, deep=True)
                        result = {'ok': True, 'head': status(store).get('head'), 'snapshot': True,
                                  'watermarks': dict(store._accounting_cache.marks),
                                  'source_heads': dict(store._accounting_cache.heads)}
                    finally:
                        store.close()
                        store = None
            except Exception as exc:
                finding = phase == 'verify' or isinstance(exc, InvalidUpdate)
                result = {'ok': False, 'finding': finding,
                          'error': type(exc).__name__ + ': ' + str(exc)[:512]}
            finally:
                if store is not None and hasattr(store, 'db'):
                    store.close()
            result.update(path=self._path, elapsed_ns=time.perf_counter_ns() - started)
            self._results.put(result)

        self._thread = threading.Thread(target=work, name='sutra-accounting-audit', daemon=True)
        self._thread.start()

    def poll(self, store):
        if self._path is None:
            return None
        if str(Path(store.path).resolve()) != self._path:
            raise ValueError('Accounting audit belongs to a different ledger')
        try:
            result = self._results.get_nowait()
        except queue.Empty:
            return None
        self._thread.join(timeout=1)
        self._thread = None
        self._path = None
        from accounting_metrics import record
        record(store, 'accounting_audit', result['elapsed_ns'], ok=result['ok'])
        if not result['ok'] and result.get('finding'):
            from accounting_batches import block_admission
            block_admission(store, result['error'])
        elif not result['ok']:
            store._accounting_audit_wait = result['error']
        else:
            store._accounting_audit_wait = None
        return result


def check_admission(store):
    audit = getattr(store, '_accounting_background_audit', None)
    if audit is not None:
        result = audit.poll(store)
        if result is not None:
            store._accounting_background_audit = None
    from accounting_batches import check_admission as check_accounting
    check_accounting(store)
    if getattr(store, '_accounting_audit_wait', None):
        from context_manager import InvalidUpdate
        raise InvalidUpdate('Accounting audit unavailable; retry the audit before admission: '
                            + store._accounting_audit_wait)
