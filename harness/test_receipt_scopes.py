"""Scoped verification retains every check without scanning unrelated audits."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from accounting_metrics import subscribe
from context_manager import canonical
from invocation_gateway import InvocationLedger
from runtime import ingest
from test_governance import ScriptModel


class ReceiptScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.sqlite'
        self.s = InvocationLedger(self.path)
        self.s.create('target')
        self.events = []
        subscribe(self.s, self.events.append)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def parser_run(self, session):
        run = self.s.begin_run(session, 'A supplied prompt', [], None, purpose='parser')
        self.s.event(run, 'FIN', {'reason': 'fixture without dispatch'})
        return run

    def measured(self, stage):
        return [e for e in self.events if e['stage'] == stage][-1]

    def plan(self, sql, params):
        return ' '.join(str(row[-1]) for row in self.s.db.execute(
            'EXPLAIN QUERY PLAN ' + sql, params))

    def test_mirror_queries_use_session_and_run_expression_indexes(self):
        run = self.parser_run('target')
        commit = self.plan("SELECT payload_json FROM page_audit WHERE event='RUN_COMMIT' "
            "AND json_extract(payload_json,'$.session')=? ORDER BY seq", ('target',))
        events = self.plan("SELECT payload_json FROM page_audit WHERE event='PROTOCOL_EVENT' "
            "AND json_extract(payload_json,'$.run_id')=? ORDER BY seq", (run,))
        self.assertIn('receipt_run_commits_session', commit)
        self.assertIn('receipt_protocol_events_run', events)
        self.assertIn('receipt_protocol_runs_session', self.plan(
            'SELECT id,config_json FROM protocol_runs WHERE session=?', ('target',)))
        self.assertIn('receipt_invocations_session', self.plan(
            'SELECT COUNT(*) FROM invocations WHERE session=?', ('target',)))

    def test_unrelated_history_does_not_expand_scoped_mirror_rows(self):
        self.parser_run('target')
        self.assertTrue(self.s.verify_protocol('target'))
        before = self.measured('protocol_verification')
        for n in range(150):
            session = 'unrelated-' + str(n)
            self.s.create(session)
            self.parser_run(session)
        self.assertTrue(self.s.verify_protocol('target'))
        after = self.measured('protocol_verification')
        self.assertEqual(after['run_rows'], 1)
        self.assertEqual(after['audit_event_rows'], 3)
        self.assertEqual(after['protocol_event_rows'], 2)
        for key in ('contract_rows', 'assertion_rows', 'run_rows',
                    'audit_event_rows', 'protocol_event_rows'):
            self.assertEqual(before[key], after[key])

    def test_targeted_protocol_event_rewrite_is_still_detected(self):
        run = self.parser_run('target')
        self.assertTrue(self.s.verify_protocol('target'))
        with self.s.db:
            self.s.db.execute('DROP TRIGGER immutable_protocol_events_UPDATE')
            self.s.db.execute('UPDATE protocol_events SET record_json=? WHERE run_id=? AND seq=2',
                (canonical({'kind': 'ALLOW', 'data': {'forged': True}}), run))
        self.assertFalse(self.s.verify_protocol('target'))
        self.assertFalse(self.measured('protocol_verification')['ok'])

    def test_duplicate_target_run_commit_is_still_detected(self):
        run = self.parser_run('target')
        self.assertTrue(self.s.verify_protocol('target'))
        with self.s.db:
            self.s._audit('RUN_COMMIT', {'run_id': run, 'session': 'target',
                                         'config': self.s.run(run)['config']})
        self.assertFalse(self.s.verify_protocol('target'))

    def test_missing_or_unknown_target_session_is_rejected(self):
        self.assertFalse(self.s.verify_protocol('absent'))
        self.assertFalse(self.s.verify_invocations('absent'))
        self.assertFalse(self.s.db.in_transaction)

    def test_invocation_verification_uses_one_read_snapshot(self):
        self.parser_run('target')
        original = self.s._verify_protocol
        observed = []

        def in_snapshot(session, stats=None):
            observed.append(self.s.db.in_transaction)
            return original(session, stats)

        with patch.object(self.s, '_verify_protocol', in_snapshot):
            self.assertTrue(self.s.verify_invocations('target'))
        self.assertEqual(observed, [True])
        self.assertFalse(self.s.db.in_transaction)
        receipt = self.measured('invocation_verification')
        self.assertTrue(receipt['ok'])
        self.assertEqual(receipt['receipt_rows'], 0)

    def test_nested_verification_preserves_callers_transaction(self):
        self.parser_run('target')
        self.s.db.execute('BEGIN')
        self.assertTrue(self.s.verify_invocations('target'))
        self.assertTrue(self.s.db.in_transaction)
        self.s.db.rollback()

    def test_concurrent_append_is_seen_on_next_snapshot_without_false_failure(self):
        self.s.db.execute('PRAGMA journal_mode=WAL')
        run = self.parser_run('target')
        original = self.s.verify_protocol
        committed = []

        def after_protocol(session):
            result = original(session)
            other = sqlite3.connect(str(self.path), timeout=1)
            try:
                with other:
                    other.execute('INSERT INTO invocations VALUES(?,?,?,?,?,?,?)',
                        ('new-pending', 'target', run, 'concurrent', 'missing', '{}', 'pending'))
                committed.append(True)
            finally:
                other.close()
            return result

        with patch.object(self.s, 'verify_protocol', after_protocol):
            self.assertTrue(self.s.verify_invocations('target'))
        self.assertEqual(committed, [True])
        self.assertFalse(self.s.verify_invocations('target'))
        self.assertTrue(self.s.verify_invocations('target', complete=False))

    def test_verify_does_not_rewind_an_outer_select_cursor(self):
        self.parser_run('target')
        for n in range(5):
            self.s.create('cursor-' + str(n))
        cursor = self.s.db.execute('SELECT id FROM sessions ORDER BY id')
        seen = []
        for session, in cursor:
            seen.append(session)
            self.assertTrue(self.s.verify_invocations('target'))
        self.assertEqual(seen, ['cursor-' + str(n) for n in range(5)] + ['target'])

    def test_pending_invocations_respect_complete_flag_and_session(self):
        run = self.parser_run('target')
        with self.s.db:
            self.s.db.execute('INSERT INTO invocations VALUES(?,?,?,?,?,?,?)',
                ('pending', 'target', run, 'pending-call', 'not-delivered', '{}', 'pending'))
        self.assertFalse(self.s.verify_invocations('target'))
        self.assertTrue(self.s.verify_invocations('target', complete=False))
        self.s.create('empty')
        self.assertTrue(self.s.verify_invocations('empty'))

    def test_accepted_receipt_hash_and_proposal_are_still_checked(self):
        model = ScriptModel(lambda n, p: {'operations': [
            {'key': 'network_allowed', 'value': 'false', 'source_unit': 0}]})
        self.assertTrue(ingest(self.s, 'target', 'No network access.', model)['committed'])
        self.assertTrue(self.s.verify_invocations('target'))
        stats = self.measured('invocation_verification')
        self.assertEqual(stats['receipt_rows'], 1)
        self.assertEqual(stats['invocation_count'], 1)
        raw = self.s.db.execute('SELECT record_json FROM invocation_receipts').fetchone()[0]
        record = json.loads(raw)
        record['proposal'] = {'forged': True}
        with self.s.db:
            self.s.db.execute('DROP TRIGGER immutable_invocation_receipts_UPDATE')
            self.s.db.execute('UPDATE invocation_receipts SET record_json=?', (canonical(record),))
        self.assertFalse(self.s.verify_invocations('target'))

    def test_malformed_receipt_fails_closed_and_releases_read_snapshot(self):
        run = self.parser_run('target')
        with self.s.db:
            self.s.db.execute('INSERT INTO invocation_receipts VALUES(?,?,?,?,?)',
                ('target', 1, 'missing', 'malformed', '{'))
        self.assertFalse(self.s.verify_invocations('target'))
        self.assertFalse(self.s.db.in_transaction)


if __name__ == '__main__':
    unittest.main()
