"""Receipt audit fast paths preserve strict treatment of unknown mutations."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from accounting_metrics import subscribe
from context_manager import canonical, sha
from paging import PagedContext


class ReceiptAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.sqlite'
        self.s = PagedContext(self.path)
        with self.s.db:
            for i in range(100):
                self.s._audit('FIXTURE', {'i': i})
        self.samples = []
        subscribe(self.s, self.samples.append)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def assert_last(self, stage, scanned):
        self.assertEqual(self.samples[-1]['stage'], stage)
        self.assertEqual(self.samples[-1]['rows_scanned'], scanned)

    def test_verified_own_append_hashes_only_its_tail_and_explicit_deep_replays(self):
        self.assertTrue(self.s.audit_valid())
        self.assert_last('page_audit_full', 100)
        with self.s.db:
            self.s._audit('NEXT', {'value': 101})
        with patch('paging.sha', wraps=sha) as digest:
            self.assertTrue(self.s.audit_valid())
            self.assertEqual(digest.call_count, 1)
        self.assert_last('page_audit_tail', 1)
        self.assertTrue(self.s.audit_valid())
        self.assert_last('page_audit_cache', 0)
        self.assertTrue(self.s.audit_valid(deep=True))
        self.assert_last('page_audit_full', 101)

    def test_other_table_write_does_not_rehash_immutable_page_prefix(self):
        self.assertTrue(self.s.audit_valid())
        self.s.create('another-task')
        self.assertTrue(self.s.audit_valid())
        self.assert_last('page_audit_tail', 0)

    def test_external_legitimate_append_requires_full_revalidation(self):
        self.assertTrue(self.s.audit_valid())
        other = PagedContext(self.path)
        try:
            with other.db:
                other._audit('ANOTHER_WRITER', {'value': 101})
        finally:
            other.close()
        self.assertTrue(self.s.audit_valid())
        self.assert_last('page_audit_full', 101)
        self.assertEqual(self.samples[-1]['reason'], 'external_commit')

    def test_reopened_connection_has_no_implicit_trusted_prefix(self):
        self.assertTrue(self.s.audit_valid())
        self.s.close()
        self.s = PagedContext(self.path)
        subscribe(self.s, self.samples.append)
        self.assertTrue(self.s.audit_valid())
        self.assert_last('page_audit_full', 100)

    def test_uncommitted_prefix_is_not_reused_after_rollback(self):
        self.assertTrue(self.s.audit_valid())
        self.s._audit('PENDING', {})
        self.assertTrue(self.s.audit_valid())
        self.assert_last('page_audit_tail', 1)
        self.s.db.rollback()
        self.assertTrue(self.s.audit_valid())
        self.assert_last('page_audit_full', 100)
        self.assertEqual(self.samples[-1]['reason'], 'prefix_mismatch_or_rollback')

    def test_own_restored_schema_cookie_does_not_hide_prefix_mutation(self):
        self.assertTrue(self.s.audit_valid())
        schema = self.s.db.execute('PRAGMA schema_version').fetchone()[0]
        trigger = self.s.db.execute("SELECT sql FROM sqlite_master WHERE name='immutable_page_audit_UPDATE'").fetchone()[0]
        with self.s.db:
            self.s.db.execute('DROP TRIGGER immutable_page_audit_UPDATE')
            self.s.db.execute("UPDATE page_audit SET payload_json='{}' WHERE seq=1")
            self.s.db.execute(trigger)
            self.s.db.execute('PRAGMA schema_version=' + str(schema))
        self.assertFalse(self.s.audit_valid())
        self.assert_last('page_audit_full', 1)

    def test_external_restored_schema_cookie_does_not_hide_prefix_mutation(self):
        self.assertTrue(self.s.audit_valid())
        schema = self.s.db.execute('PRAGMA schema_version').fetchone()[0]
        trigger = self.s.db.execute("SELECT sql FROM sqlite_master WHERE name='immutable_page_audit_UPDATE'").fetchone()[0]
        with sqlite3.connect(self.path) as other:
            other.execute('DROP TRIGGER immutable_page_audit_UPDATE')
            other.execute("UPDATE page_audit SET payload_json='{}' WHERE seq=1")
            other.execute(trigger)
            other.execute('PRAGMA schema_version=' + str(schema))
        self.assertFalse(self.s.audit_valid())
        self.assert_last('page_audit_full', 1)

    def test_missing_immutable_guard_prevents_prefix_reuse(self):
        self.assertTrue(self.s.audit_valid())
        self.s.db.execute('DROP TRIGGER immutable_page_audit_UPDATE')
        self.assertTrue(self.s.audit_valid())
        self.assert_last('page_audit_full', 100)
        with self.s.db:
            self.s._audit('NEXT', {})
        self.assertTrue(self.s.audit_valid())
        self.assert_last('page_audit_full', 101)

    def test_low_sequence_insert_is_not_hidden_by_tail_query(self):
        self.s.close()
        self.s = PagedContext(':memory:')
        subscribe(self.s, self.samples.append)
        raw = canonical({})
        with self.s.db:
            self.s.db.execute('INSERT INTO page_audit VALUES(?,?,?,?,?)',
                             (0, 'BAD_POSITION', raw, 'GENESIS', sha(canonical([0, 'BAD_POSITION', raw, 'GENESIS']))))
        self.assertFalse(self.s.audit_valid())
        self.assert_last('page_audit_full', 1)

    def test_low_sequence_insert_after_verified_prefix_is_rejected(self):
        for bad in (-1, 0):
            with self.subTest(seq=bad), tempfile.TemporaryDirectory() as folder:
                other = PagedContext(Path(folder) / 'state.sqlite')
                try:
                    with other.db:
                        for i in range(20):
                            other._audit('ORIGINAL', {'i': i})
                    self.assertTrue(other.audit_valid())
                    raw = canonical({})
                    with other.db:
                        other.db.execute('INSERT INTO page_audit VALUES(?,?,?,?,?)',
                                         (bad, 'INSERTED_BEHIND_PREFIX', raw, 'GENESIS',
                                          sha(canonical([bad, 'INSERTED_BEHIND_PREFIX', raw, 'GENESIS']))))
                    self.assertFalse(other.audit_valid())
                finally:
                    other.close()

    def test_zero_insert_after_verified_empty_prefix_is_rejected(self):
        self.s.close()
        self.s = PagedContext(':memory:')
        self.assertTrue(self.s.audit_valid())
        raw = canonical({})
        with self.s.db:
            self.s.db.execute('INSERT INTO page_audit VALUES(?,?,?,?,?)',
                             (0, 'INSERTED_AFTER_EMPTY', raw, 'GENESIS',
                              sha(canonical([0, 'INSERTED_AFTER_EMPTY', raw, 'GENESIS']))))
        self.assertFalse(self.s.audit_valid())


if __name__ == '__main__':
    unittest.main()
