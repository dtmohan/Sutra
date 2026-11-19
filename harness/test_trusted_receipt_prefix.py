"""Explicit caller seal trust is bounded, local and never a default shortcut."""
import copy
import sqlite3
import unittest

import accounting_batches as batches
import orchestration as m
import test_accounting_batches as fixtures
from accounting_metrics import subscribe
from context_manager import InvalidUpdate, canonical
from invocation_gateway import InvocationLedger


class TrustedReceiptPrefixTests(unittest.TestCase):
    tearDown = fixtures.AccountingBatchTests.tearDown
    reopen = fixtures.AccountingBatchTests.reopen
    child = fixtures.AccountingBatchTests.child
    dispatch = fixtures.AccountingBatchTests.dispatch
    finish = fixtures.AccountingBatchTests.finish
    complete_one = fixtures.AccountingBatchTests.complete_one
    flush = fixtures.AccountingBatchTests.flush

    def setUp(self):
        fixtures.AccountingBatchTests.setUp(self)
        with self.s.db:
            for i in range(64):
                self.s._audit('PRE_ANCHOR_FIXTURE', {'i': i})
        self.complete_one()
        self.complete_one()
        self.flush()
        head = batches.checkpoints(self.s)[-1]
        self.anchor = {'seq': head['seq'], 'digest': head['digest']}
        self.reopen()
        self.samples = []
        subscribe(self.s, self.samples.append)

    def select(self):
        batches.recover(self.s, trusted_anchor=self.anchor, audit_prefix=True)
        return batches.trusted_audit_prefix(self.s)['head'][0]

    def external_append(self, event='EXTERNAL_APPEND'):
        other = InvocationLedger(self.path)
        try:
            with other.db:
                other._audit(event, {'event': event})
        finally:
            other.close()

    def external_corrupt(self, seq):
        schema = self.s.db.execute('PRAGMA schema_version').fetchone()[0]
        trigger = self.s.db.execute("SELECT sql FROM sqlite_master WHERE name='immutable_page_audit_UPDATE'").fetchone()[0]
        with sqlite3.connect(self.path) as other:
            other.execute('DROP TRIGGER immutable_page_audit_UPDATE')
            other.execute("UPDATE page_audit SET payload_json='{}' WHERE seq=?", (seq,))
            other.execute(trigger)
            other.execute('PRAGMA schema_version=' + str(schema))

    def audit_sample(self):
        return next(r for r in reversed(self.samples) if r['stage'].startswith('page_audit_'))

    def test_accounting_anchor_does_not_implicitly_select_receipt_prefix(self):
        batches.recover(self.s, trusted_anchor=self.anchor)
        self.assertIsNone(batches.trusted_audit_prefix(self.s))
        self.assertTrue(self.s.audit_valid())
        self.assertEqual(self.audit_sample()['stage'], 'page_audit_full')
        self.assertGreater(self.audit_sample()['rows_scanned'], 64)
        cursor = self.select()
        self.assertTrue(self.s.audit_valid())
        self.assertEqual(self.audit_sample()['stage'], 'page_audit_anchored_tail')
        self.assertEqual(self.audit_sample()['previous_seq'], cursor)
        self.assertEqual(self.audit_sample()['rows_scanned'], 0)
        self.assertTrue(self.s.audit_valid(deep=True))
        self.assertEqual(self.audit_sample()['stage'], 'page_audit_full')
        self.assertGreater(self.audit_sample()['rows_scanned'], 64)

    def test_each_external_commit_restarts_original_cursor_not_last_verified_tail(self):
        cursor = self.select()
        self.assertTrue(self.s.audit_valid())
        self.external_append('FIRST_EXTERNAL')
        self.assertTrue(self.s.audit_valid())
        self.assertEqual(self.audit_sample()['stage'], 'page_audit_anchored_tail')
        self.assertEqual(self.audit_sample()['rows_scanned'], 1)
        self.external_append('SECOND_EXTERNAL')
        self.assertTrue(self.s.audit_valid())
        self.assertEqual(self.audit_sample()['previous_seq'], cursor)
        self.assertEqual(self.audit_sample()['rows_scanned'], 2)

    def test_changed_canonical_suffix_is_rejected_even_with_restored_schema_cookie(self):
        cursor = self.select()
        self.external_append()
        self.assertTrue(self.s.audit_valid())
        self.external_corrupt(cursor + 1)
        self.assertFalse(self.s.audit_valid())

    def test_trusted_prefix_is_a_snapshot_full_audit_still_checks_its_source_bytes(self):
        cursor = self.select()
        old = self.s.db.execute("SELECT MIN(seq) FROM page_audit WHERE event='PRE_ANCHOR_FIXTURE'").fetchone()[0]
        self.assertLess(old, cursor)
        self.assertTrue(self.s.audit_valid())
        self.external_corrupt(old)
        # The caller explicitly trusts the historical prefix committed by its
        # seal. This is deliberately not a claim about current prefix bytes.
        self.assertTrue(self.s.audit_valid())
        self.assertEqual(self.audit_sample()['stage'], 'page_audit_anchored_tail')
        self.assertFalse(self.s.audit_valid(deep=True))

    def test_setting_anchor_clears_stronger_profile_even_without_a_database_write(self):
        self.select()
        self.assertTrue(self.s.audit_valid())
        before = self.s._audit_stamp()
        batches.set_trusted_anchor(self.s, self.anchor)
        self.assertEqual(self.s._audit_stamp(), before)
        self.assertIsNone(batches.trusted_audit_prefix(self.s))
        self.assertTrue(self.s.audit_valid())
        self.assertEqual(self.audit_sample()['stage'], 'page_audit_full')
        self.assertGreater(self.audit_sample()['rows_scanned'], 64)

    def test_profile_is_not_a_database_marker_or_transferable_connection_object(self):
        self.select()
        self.assertTrue(self.s.audit_valid())
        other = InvocationLedger(self.path)
        try:
            other_samples = []
            subscribe(other, other_samples.append)
            self.assertIsNone(batches.trusted_audit_prefix(other))
            self.assertTrue(other.audit_valid())
            self.assertEqual(other_samples[-1]['stage'], 'page_audit_full')
            other._accounting_trusted_anchor = dict(self.anchor)
            other._accounting_audit_prefix_enabled = True
            other._accounting_audit_prefix_profile = copy.deepcopy(self.s._accounting_audit_prefix_profile)
            self.assertFalse(other.audit_valid())
            # A separate connection may explicitly select the same external
            # anchor, but cannot accidentally inherit another connection's proof.
            batches.recover(other, trusted_anchor=self.anchor, audit_prefix=True)
            self.assertTrue(other.audit_valid())
        finally:
            other.close()

    def test_policy_or_selected_anchor_change_invalidates_unchanged_database_cache(self):
        self.select()
        self.assertTrue(self.s.audit_valid())
        logic = self.s.logic_hash
        self.s.logic_hash = 'different-runtime-policy'
        self.assertFalse(self.s.audit_valid())
        self.s.logic_hash = logic
        self.assertTrue(self.s.audit_valid())
        self.s._accounting_trusted_anchor = dict(self.anchor, digest='0' * 64)
        self.assertFalse(self.s.audit_valid())

    def test_tampered_selected_body_header_and_audit_anchor_cannot_reuse_cache(self):
        cursor = self.select()
        cases = [
            ('orchestration_accounting_checkpoints', 'record_json', 'seq', self.anchor['seq']),
            ('orchestration_accounting_seal_headers', 'record_json', 'seq', self.anchor['seq']),
            ('page_audit', 'payload_json', 'seq', cursor),
        ]
        for table, column, key, value in cases:
            with self.subTest(table=table):
                self.assertTrue(self.s.audit_valid())
                original = self.s.db.execute('SELECT ' + column + ' FROM ' + table + ' WHERE ' + key + '=?', (value,)).fetchone()[0]
                name = 'immutable_' + table + '_UPDATE'
                trigger = self.s.db.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()[0]
                with self.s.db:
                    self.s.db.execute('DROP TRIGGER ' + name)
                    self.s.db.execute('UPDATE ' + table + ' SET ' + column + '=? WHERE ' + key + '=?', (original + ' ', value))
                self.assertFalse(self.s.audit_valid())
                with self.s.db:
                    self.s.db.execute('UPDATE ' + table + ' SET ' + column + '=? WHERE ' + key + '=?', (original, value))
                    self.s.db.execute(trigger)

    def test_new_seals_never_promote_original_trust_and_model_switch_keeps_spending(self):
        cursor = self.select()
        original = m.node(self.s, self.root['id'])
        used = m.spent(self.s, self.root['id'])
        self.complete_one(model='larger-model')
        self.complete_one(model='small-model')
        self.flush()
        self.external_append()
        self.assertTrue(self.s.audit_valid())
        self.assertEqual(self.audit_sample()['previous_seq'], cursor)
        self.assertGreater(self.audit_sample()['rows_scanned'], 1)
        self.assertEqual(batches.trusted_audit_prefix(self.s)['head'][0], cursor)
        self.assertEqual(m.node(self.s, self.root['id']), original)
        self.assertEqual(m.spent(self.s, self.root['id'])['calls'], used['calls'] + 2)

    def test_deleted_selected_audit_cursor_cannot_be_recovered_or_cached(self):
        cursor = self.select()
        self.assertTrue(self.s.audit_valid())
        with self.s.db:
            self.s.db.execute('DROP TRIGGER immutable_page_audit_DELETE')
            self.s.db.execute('DELETE FROM page_audit WHERE seq=?', (cursor,))
        self.assertFalse(self.s.audit_valid())
        with self.assertRaises(InvalidUpdate):
            batches.recover(self.s, trusted_anchor=self.anchor, audit_prefix=True)
        self.assertFalse(self.s._accounting_audit_prefix_enabled)

    def test_invalid_opt_in_never_allocates_or_resets_spending(self):
        used = m.spent(self.s, self.root['id'])
        for kwargs in ({'audit_prefix': True}, {'trusted_anchor': self.anchor, 'audit_prefix': 'yes'}):
            with self.assertRaises(InvalidUpdate):
                batches.recover(self.s, **kwargs)
        self.assertEqual(m.spent(self.s, self.root['id']), used)
        self.assertIsNone(batches.trusted_audit_prefix(self.s))


if __name__ == '__main__':
    unittest.main()
