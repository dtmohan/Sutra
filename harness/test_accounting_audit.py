"""Snapshot audits must not hold live writer locks or poison transient failures."""
import threading
import sqlite3
import unittest
from unittest.mock import patch

import accounting_batches as batches
from accounting_audit import BackgroundAudit
from context_manager import InvalidUpdate
import orchestration as m
import test_accounting_batches as fixtures


class BackgroundSnapshotTests(unittest.TestCase):
    setUp = fixtures.AccountingBatchTests.setUp
    tearDown = fixtures.AccountingBatchTests.tearDown
    child = fixtures.AccountingBatchTests.child

    def test_backup_copy_steps_can_overlap_live_writes_on_system_sqlite(self):
        self.s.db.execute('CREATE TABLE audit_padding(value BLOB)')
        self.s.db.execute('INSERT INTO audit_padding VALUES(zeroblob(4194304))')
        self.s.db.execute('CREATE TABLE audit_heartbeat(value INTEGER)')
        self.s.db.commit()
        original = sqlite3.connect
        source_uri = self.path.resolve().as_uri()
        before = m.spent(self.s, self.root['id'])

        for attempt in range(8):
            copying = threading.Event()

            class NotifyingConnection(sqlite3.Connection):
                def backup(self, *args, **kwargs):
                    copying.set()
                    # No progress callback: it masks the affected SQLite
                    # runtime's mixed RO/RW descriptor race.
                    return super().backup(*args, **kwargs)

            def connect(database, *args, **kwargs):
                if str(database).startswith(source_uri):
                    kwargs['factory'] = NotifyingConnection
                return original(database, *args, **kwargs)

            audit = BackgroundAudit()
            with patch('accounting_audit.sqlite3.connect', connect):
                audit.start(self.s)
                try:
                    self.assertTrue(copying.wait(3))
                    threading.Event().wait(.0001 if attempt % 2 else .001)
                    for ordinal in range(4):
                        with self.s.db:
                            self.s.db.execute('BEGIN IMMEDIATE')
                            self.s.db.execute('INSERT INTO audit_heartbeat VALUES(?)', (attempt * 4 + ordinal,))
                finally:
                    audit._thread.join(timeout=5)
            self.assertFalse(audit.running)
            self.assertTrue(audit.poll(self.s)['ok'])
        self.assertEqual(m.spent(self.s, self.root['id']), before)

    def test_slow_snapshot_audit_does_not_block_live_writer_commit(self):
        entered, release = threading.Event(), threading.Event()
        original = batches.verify

        def paused(store, deep=True):
            store.db.execute('BEGIN')
            store.db.execute('SELECT COUNT(*) FROM orchestration_nodes').fetchone()
            entered.set()
            if not release.wait(3):
                raise RuntimeError('Test controller did not release the audit')
            try:
                return original(store, deep=deep)
            finally:
                store.db.rollback()

        audit = BackgroundAudit()
        self.s.db.execute('PRAGMA busy_timeout=100')
        with patch('accounting_batches.verify', paused):
            audit.start(self.s)
            try:
                self.assertTrue(entered.wait(3))
                # An audit against the live DELETE-journal database would hold
                # its read lock and make this commit hit the short timeout.
                created = self.child()
                self.assertEqual(m.node(self.s, created['id'])['context'], self.root['context'])
            finally:
                release.set()
                audit._thread.join(timeout=5)
        self.assertFalse(audit.running)
        result = audit.poll(self.s)
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['snapshot'])

    def test_snapshot_resource_failure_pauses_without_corruption_finding(self):
        ready = self.child()
        before = m.spent(self.s, self.root['id'])
        audit = BackgroundAudit()
        with patch('accounting_audit.tempfile.TemporaryDirectory', side_effect=OSError('snapshot unavailable')):
            audit.start(self.s)
            audit._thread.join(timeout=5)
        result = audit.poll(self.s)
        self.assertFalse(result['ok'])
        self.assertFalse(result['finding'])
        with self.assertRaisesRegex(InvalidUpdate, 'audit unavailable'):
            m.checked(self.s, ready['id'])
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM orchestration_accounting_findings').fetchone()[0], 0)
        self.assertEqual(m.spent(self.s, self.root['id']), before)
        audit.start(self.s)
        audit._thread.join(timeout=5)
        self.assertTrue(audit.poll(self.s)['ok'])
        self.assertEqual(m.checked(self.s, ready['id'])['id'], ready['id'])
        self.assertEqual(m.spent(self.s, self.root['id']), before)


if __name__ == '__main__':
    unittest.main()
