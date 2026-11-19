"""Local ledger concurrency without changing verification or durability rules.

WAL readers retain a consistent snapshot without holding up receipt commits.
An older rollback-journal ledger may be busy during the one-time transition;
leave its mode intact and try on a later connection instead of interrupting work.
"""
import sqlite3
import time
from pathlib import Path


def read_connection(path, timeout=.2):
    """SQL-readonly diagnostics for live and quiescent local WAL ledgers.

    Apple system SQLite cannot always open a quiescent WAL file through an OS
    read-only descriptor when its coordination sidecars have been removed by
    the last writer. An existing-file RW descriptor plus query_only permits
    those SQLite coordination files, without allowing application SQL writes.
    A genuinely read-only location can still use SQLite's read-only open path.
    Neither mode creates a missing ledger or changes its journal policy.
    """
    uri = Path(path).resolve().as_uri()
    last = None
    for mode in ('rw', 'ro'):
        db = None
        try:
            db = sqlite3.connect(uri + '?mode=' + mode, uri=True, timeout=timeout)
            db.execute('PRAGMA query_only=ON')
            # Force actual schema access: connect() can defer a WAL-open error.
            db.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone()
            return db
        except sqlite3.OperationalError as exc:
            last = exc
            if db is not None:
                db.close()
    raise last


def initialize(store):
    db = store.db
    started = time.perf_counter_ns()
    original_timeout = db.execute('PRAGMA busy_timeout').fetchone()[0]
    mode = db.execute('PRAGMA journal_mode').fetchone()[0].lower()
    before = mode
    deferred = False
    try:
        if store.path != ':memory:' and mode != 'wal':
            # Mode migration needs an exclusive lock. Do not make an existing
            # task wait for a long reader just to change the journal policy.
            db.execute('PRAGMA busy_timeout=50')
            try:
                mode = db.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower()
            except sqlite3.OperationalError as exc:
                if 'locked' not in str(exc).lower() and 'busy' not in str(exc).lower():
                    raise
                deferred = True
                mode = db.execute('PRAGMA journal_mode').fetchone()[0].lower()
        # Keep the previous FULL durability level. WAL is a concurrency change,
        # not permission to acknowledge receipts with weaker synchronization.
        db.execute('PRAGMA synchronous=FULL')
        store._sqlite_journal = {'mode': mode, 'previous_mode': before,
                                 'migration_deferred': deferred,
                                 'synchronous': 'FULL'}
    finally:
        db.execute('PRAGMA busy_timeout=' + str(original_timeout))
        from accounting_metrics import record
        record(store, 'sqlite_journal_setup', time.perf_counter_ns() - started,
               mode=mode, previous_mode=before, migration_deferred=deferred)
