"""Receipt read snapshots must not block independent committed work."""
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

from context_manager import ContextStore
from invocation_gateway import InvocationLedger
from ledger_sqlite import read_connection
from unittest.mock import patch


class LedgerSQLiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.sqlite'

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_local_ledger_uses_wal_with_full_durability(self):
        s = InvocationLedger(self.path)
        try:
            self.assertEqual(s.db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
            self.assertEqual(s.db.execute('PRAGMA synchronous').fetchone()[0], 2)
            self.assertEqual(s.db.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            self.assertEqual(s.db.execute('PRAGMA busy_timeout').fetchone()[0], 5000)
            self.assertFalse(s._sqlite_journal['migration_deferred'])
        finally:
            s.close()

    def test_legacy_migration_preserves_file_and_existing_records(self):
        s = InvocationLedger(self.path)
        s.create('saved')
        with s.db:
            s._audit('SAVED_CONTEXT', {'project': 'Cedar', 'network_allowed': False,
                                      'consumed_tokens': 724})
        original = s.db.execute('SELECT * FROM page_audit').fetchall()
        s.db.execute('PRAGMA journal_mode=DELETE')
        s.close()
        inode = self.path.stat().st_ino
        s = InvocationLedger(self.path)
        try:
            self.assertEqual(s.db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
            self.assertEqual(s.db.execute('SELECT * FROM page_audit').fetchall(), original)
            self.assertEqual(self.path.stat().st_ino, inode)
            self.assertTrue(s.audit_valid(deep=True))
        finally:
            s.close()

    def test_read_snapshot_remains_consistent_while_another_receipt_commits(self):
        s = InvocationLedger(self.path)
        try:
            with s.db:
                s._audit('BEFORE', {'budget': 1})
            s.db.execute('BEGIN')
            self.assertEqual(s.db.execute('SELECT COUNT(*) FROM page_audit').fetchone()[0], 1)
            done = threading.Event()
            errors = []

            def writer():
                other = None
                try:
                    other = InvocationLedger(self.path)
                    with other.db:
                        other._audit('RECEIPT', {'budget': 2})
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    if other is not None:
                        other.close()
                    done.set()

            thread = threading.Thread(target=writer)
            thread.start()
            completed_during_snapshot = done.wait(2)
            # Read remains at its original snapshot, even after the writer commits.
            self.assertEqual(s.db.execute('SELECT COUNT(*) FROM page_audit').fetchone()[0], 1)
            s.db.execute('ROLLBACK')
            thread.join(10)
            self.assertTrue(completed_during_snapshot, 'Receipt blocked behind verifier snapshot')
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(s.db.execute('SELECT COUNT(*) FROM page_audit').fetchone()[0], 2)
            self.assertTrue(s.audit_valid(deep=True))
        finally:
            s.close()

    def test_busy_legacy_mode_migration_defers_without_changing_work(self):
        s = ContextStore(self.path)
        s.create('saved')
        s.db.execute('PRAGMA journal_mode=DELETE')
        s.db.execute('BEGIN')
        self.assertEqual(s.db.execute('SELECT id FROM sessions').fetchone()[0], 'saved')
        other = None
        try:
            began = time.monotonic()
            other = ContextStore(self.path)
            self.assertLess(time.monotonic() - began, 1)
            self.assertTrue(other._sqlite_journal['migration_deferred'])
            self.assertEqual(other.db.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
            self.assertEqual(other.db.execute('SELECT id FROM sessions').fetchone()[0], 'saved')
            self.assertEqual(other.db.execute('PRAGMA busy_timeout').fetchone()[0], 5000)
        finally:
            if other is not None:
                other.close()
            s.db.execute('ROLLBACK')
            s.close()
        reopened = ContextStore(self.path)
        try:
            self.assertEqual(reopened.db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        finally:
            reopened.close()

    def test_memory_ledger_keeps_native_journal_and_integrity(self):
        s = InvocationLedger(':memory:')
        try:
            self.assertEqual(s.db.execute('PRAGMA journal_mode').fetchone()[0], 'memory')
            s.create('memory')
            with s.db:
                s._audit('MEMORY', {})
            self.assertTrue(s.audit_valid(deep=True))
        finally:
            s.close()

    def test_readonly_independent_audit_needs_no_migration_or_writes(self):
        s = InvocationLedger(self.path)
        with s.db:
            s._audit('WITNESS', {})
        s.close()
        ro = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)
        try:
            ro.execute('PRAGMA query_only=ON')
            self.assertEqual(ro.execute('SELECT COUNT(*) FROM page_audit').fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                ro.execute("INSERT INTO sessions VALUES('forbidden',0)")
        finally:
            ro.close()

    def test_quiescent_wal_diagnostics_read_actual_data_but_cannot_write(self):
        s = InvocationLedger(self.path)
        s.create('retained')
        with s.db:
            s._audit('RETAINED', {'consumed': 724, 'network': False})
        original = s.db.execute('SELECT * FROM page_audit').fetchall()
        s.close()  # Last close removes sidecars on the affected runtime.
        db = read_connection(self.path)
        try:
            self.assertEqual(db.execute('PRAGMA query_only').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT * FROM page_audit').fetchall(), original)
            self.assertEqual(db.execute('SELECT id FROM sessions').fetchone()[0], 'retained')
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("INSERT INTO sessions VALUES('forbidden',0)")
        finally:
            db.close()

    def test_diagnostic_reader_never_creates_a_missing_ledger(self):
        with self.assertRaises(sqlite3.OperationalError):
            read_connection(self.path)
        self.assertFalse(self.path.exists())

    def test_monitor_opens_quiescent_wal_without_mutating_receipts(self):
        from monitoring import dashboard
        s = InvocationLedger(self.path)
        with s.db:
            s._audit('MONITOR', {'consumed': 724})
        original = s.db.execute('SELECT * FROM page_audit').fetchall()
        s.close()
        with patch('monitoring.resource_state', return_value={'profile': 'fixture'}), \
                patch('runtime_memory.snapshot', return_value={}):
            result = dashboard(self.path.parent)
        self.assertEqual(result['invocations']['total'], 0)
        db = read_connection(self.path)
        try:
            self.assertEqual(db.execute('SELECT * FROM page_audit').fetchall(), original)
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
