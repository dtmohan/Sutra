"""Isolated candidate: bounded DAG verification, never cached live authority."""
import copy
import json
import os
import sqlite3
import unittest
from unittest.mock import patch

from paging import PageFault
from context_manager import InvalidUpdate
from invocation_gateway import InvocationLedger
import review_discovery as discovery
import test_discovery_selection as fixture


class ArchitectResultBatchTests(unittest.TestCase):
    setUp = fixture.SelectionWireTests.setUp
    tearDown = fixture.SelectionWireTests.tearDown
    provider = fixture.SelectionWireTests.provider
    step = fixture.SelectionWireTests.step
    until = fixture.SelectionWireTests.until

    def chain(self, count=4):
        self.actions = [{'kind': 'read', 'target': 'part0.py'}] * count
        current = self.until('SYNTHESIS')
        for _ in range(count * 4):
            if len(self.calls) >= count: break
            current = self.step(current)
            self.assertEqual(current['state'], 'SYNTHESIS', current.get('stop'))
        sid = self.meta['active']; key = self.calls[-1][0]['discovery_key']
        rows = discovery.history(self.s, sid, key)
        self.assertEqual(len(rows), count)
        self.assertTrue(all(row[2]['status'] == 'accepted' for row in rows))
        self.current = current
        return sid, [row[0] for row in rows]

    def test_twenty_node_chain_validates_twenty_instead_of_210_with_fresh_sweep(self):
        sid, ids = self.chain(20)
        original = self.s._load_result
        with patch.object(self.s, '_load_result', wraps=original) as visits:
            strict = [self.s.load_result(sid, iid) for iid in ids]
            self.assertEqual(visits.call_count, 210)
        with patch.object(self.s, '_load_result', wraps=original) as visits, \
                patch.object(self.s, '_architect_freshness', wraps=self.s._architect_freshness) as fresh:
            batched = self.s.load_architect_results(sid, ids)
            self.assertEqual(visits.call_count, 20)
            self.assertEqual(fresh.call_count, 1)
        self.assertEqual(batched, strict)
        self.assertTrue(self.s.verify_invocations(sid))

    def test_new_batch_revalidates_and_return_mutation_cannot_poison_another_result(self):
        sid, ids = self.chain()
        values = self.s.load_architect_results(sid, [ids[0], ids[-1], ids[0]])
        expected = copy.deepcopy(values[0])
        values[0]['summary'] = 'Untrusted caller mutation'
        self.assertEqual(values[2], expected)
        with patch.object(self.s, '_load_result', wraps=self.s._load_result) as visits:
            again = self.s.load_architect_results(sid, ids)
        self.assertEqual(visits.call_count, len(ids))
        self.assertEqual(again[0], expected)
        with self.assertRaises(PageFault): self.s.load_architect_results('wrong-session', ids)
        with self.assertRaises(PageFault): self.s.load_architect_results(sid, ['unknown-invocation'])
        self.assertEqual(self.s.load_architect_results(sid, (iid for iid in ids)), again)

    def test_empty_iterable_does_not_bootstrap_origin_or_inspect_database(self):
        sid = self.meta.get('active')
        with patch.object(self.s, 'origin', side_effect=AssertionError('Empty batch must not bootstrap')), \
                patch.object(self.s, '_result_batch_stamp', side_effect=AssertionError('Empty batch must not inspect DB')), \
                patch.object(self.s, '_architect_freshness', side_effect=AssertionError('Empty batch needs no freshness work')):
            self.assertEqual(self.s.load_architect_results(sid, []), [])
            self.assertEqual(self.s.load_architect_results(sid, iter(())), [])

    def test_source_change_after_ancestor_validation_rejects_even_with_same_size_and_mtime(self):
        sid, ids = self.chain(); original = self.s._load_result
        path = self.ws / 'part0.py'; raw = path.read_bytes(); stat = path.stat()
        def changed(*args, **kwargs):
            value = original(*args, **kwargs)
            if args[1] == ids[-1]:
                path.write_bytes(raw.replace(b'return', b'RETURN', 1))
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            return value
        with patch.object(self.s, '_load_result', side_effect=changed):
            with self.assertRaisesRegex((PageFault, InvalidUpdate), 'stale|changed|fabricated'):
                self.s.load_architect_results(sid, ids)

    def test_quarantined_ancestor_is_rejected_before_any_cached_proposal_returns(self):
        sid, ids = self.chain(); original = self.s._load_result
        def quarantined(*args, **kwargs):
            value = original(*args, **kwargs)
            if args[1] == ids[-1]: self.s.quarantine_invocation(sid, ids[0], 'Fixture negative dependency')
            return value
        with patch.object(self.s, '_load_result', side_effect=quarantined):
            with self.assertRaisesRegex(PageFault, 'quarantined'):
                self.s.load_architect_results(sid, ids)
        with self.assertRaisesRegex(PageFault, 'quarantined'):
            self.s.load_architect_results(sid, ids)

    def test_ledger_mutation_during_batch_is_not_promoted_to_trusted_success(self):
        sid, ids = self.chain(); original = self.s._load_result
        def changed(*args, **kwargs):
            value = original(*args, **kwargs)
            if args[1] == ids[-1]:
                with self.s.db: self.s._audit('BATCH_TEST_NO_AUTHORITY', {'test': True})
            return value
        with patch.object(self.s, '_load_result', side_effect=changed):
            with self.assertRaisesRegex(PageFault, 'changed during'):
                self.s.load_architect_results(sid, ids)

    def test_existing_transactions_keep_ownership_and_validate_unique_nodes(self):
        sid, ids = self.chain(); original = self.s._load_result
        for begin in ('BEGIN', 'BEGIN IMMEDIATE'):
            self.s.db.execute(begin); statements = []
            self.s.db.set_trace_callback(statements.append)
            try:
                changes = self.s.db.total_changes
                with patch.object(self.s, '_load_result', wraps=original) as visits:
                    values = self.s.load_architect_results(sid, ids)
                self.assertEqual(len(values), len(ids))
                self.assertEqual(visits.call_count, len(ids))
                self.assertTrue(self.s.db.in_transaction)
                self.assertEqual(self.s.db.total_changes, changes)
                self.assertFalse(any(sql.strip().upper().startswith(('BEGIN', 'COMMIT', 'ROLLBACK')) for sql in statements))
            finally:
                self.s.db.set_trace_callback(None); self.s.db.rollback()

    def test_real_register_and_finalize_batch_twenty_ancestors_inside_immediate_transaction(self):
        sid, ids = self.chain(20); phase = []; measured = []
        register, finalize = InvocationLedger.register, InvocationLedger.finalize
        bulk = InvocationLedger.load_architect_results
        def register_phase(store, *args, **kwargs):
            phase.append('register')
            try: return register(store, *args, **kwargs)
            finally: phase.pop()
        def finalize_phase(store, *args, **kwargs):
            phase.append('finalize')
            try: return finalize(store, *args, **kwargs)
            finally: phase.pop()
        def measured_bulk(store, session, invocation_ids):
            if not phase or invocation_ids != ids: return bulk(store, session, invocation_ids)
            self.assertTrue(store.db.in_transaction)
            statements = []; store.db.set_trace_callback(statements.append)
            before = store.db.total_changes
            try:
                with patch.object(store, '_load_result', wraps=store._load_result) as visits, \
                        patch.object(store, '_architect_freshness', wraps=store._architect_freshness) as freshness:
                    result = bulk(store, session, invocation_ids)
                self.assertTrue(store.db.in_transaction)
                self.assertEqual(store.db.total_changes, before)
                self.assertFalse(any(sql.strip().upper().startswith(('BEGIN', 'COMMIT', 'ROLLBACK')) for sql in statements))
                self.assertEqual(visits.call_count, 20)
                self.assertEqual(freshness.call_count, 1)
                measured.append((phase[-1], visits.call_count))
                return result
            finally:
                store.db.set_trace_callback(None)
        # This is a real governed invocation and repository effect, with the
        # gateway's actual register/finalize transactions left intact.
        current = self.current
        with patch.object(InvocationLedger, 'register', new=register_phase), \
                patch.object(InvocationLedger, 'finalize', new=finalize_phase), \
                patch.object(InvocationLedger, 'load_architect_results', new=measured_bulk):
            for _ in range(5):
                current = self.step(current)
                if len(self.calls) == 21: break
        self.assertEqual(len(self.calls), 21)
        self.assertIn(('register', 20), measured)
        self.assertIn(('finalize', 20), measured)
        self.assertFalse(self.s.db.in_transaction)
        self.assertTrue(self.s.verify_invocations(sid))

    def test_immediate_caller_transaction_excludes_competing_writer(self):
        sid, ids = self.chain(); original = self.s._load_result; blocked = []
        other = sqlite3.connect(self.s.path, timeout=0)
        def concurrent(*args, **kwargs):
            value = original(*args, **kwargs)
            if args[1] == ids[-1]:
                try:
                    other.execute('INSERT INTO invocation_quarantine VALUES(?,?,?)', (ids[0], 'Competing writer', 1))
                    other.commit()
                except sqlite3.OperationalError as exc:
                    self.assertIn('locked', str(exc)); blocked.append(True); other.rollback()
            return value
        self.s.db.execute('BEGIN IMMEDIATE')
        try:
            with patch.object(self.s, '_load_result', side_effect=concurrent):
                values = self.s.load_architect_results(sid, ids)
            self.assertEqual(len(values), len(ids)); self.assertEqual(blocked, [True])
            self.assertTrue(self.s.db.in_transaction)
        finally:
            other.close(); self.s.db.rollback()

    def test_uncommitted_quarantine_rejects_without_ending_callers_transaction(self):
        sid, ids = self.chain(); original = self.s._load_result
        def quarantined(*args, **kwargs):
            value = original(*args, **kwargs)
            if args[1] == ids[-1]:
                self.s.db.execute('INSERT INTO invocation_quarantine VALUES(?,?,?)', (ids[0], 'Uncommitted quarantine', 1))
            return value
        self.s.db.execute('BEGIN IMMEDIATE')
        try:
            with patch.object(self.s, '_load_result', side_effect=quarantined):
                with self.assertRaisesRegex(PageFault, 'quarantined'):
                    self.s.load_architect_results(sid, ids)
            self.assertTrue(self.s.db.in_transaction)
        finally:
            self.s.db.rollback()
        self.assertEqual(len(self.s.load_architect_results(sid, ids)), len(ids))

    def test_deferred_snapshot_does_not_preserve_success_after_caller_ends_transaction(self):
        sid, ids = self.chain(); original = self.s._load_result
        other = sqlite3.connect(self.s.path, timeout=0)
        def concurrent(*args, **kwargs):
            value = original(*args, **kwargs)
            if args[1] == ids[-1]:
                other.execute('INSERT INTO invocation_quarantine VALUES(?,?,?)', (ids[0], 'After read snapshot', 1))
                other.commit()
            return value
        self.s.db.execute('BEGIN')
        try:
            with patch.object(self.s, '_load_result', side_effect=concurrent):
                values = self.s.load_architect_results(sid, ids)
            # The preexisting strict loader sees the same caller-owned SQLite
            # snapshot. The batch does not claim to advance that snapshot.
            self.assertEqual(values[0], self.s.load_result(sid, ids[0]))
            self.assertTrue(self.s.db.in_transaction)
        finally:
            other.close(); self.s.db.rollback()
        with self.assertRaisesRegex(PageFault, 'quarantined'):
            self.s.load_architect_results(sid, ids)


if __name__ == '__main__': unittest.main()
