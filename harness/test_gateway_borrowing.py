"""Explicit host ledger borrowing retains authority, ownership and spending."""
import copy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import accounting_batches as batches
import conversation as chat
from context_manager import InvalidUpdate
from invocation_gateway import InvocationGateway, InvocationLedger
from review_budget import usage
from test_conversation import Fake, MODELS


class BorrowedGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.storage = self.root / 'state'
        self.storage.mkdir()
        self.s = InvocationLedger(self.storage / 'state.sqlite')
        self.meta = {'workspace': str(self.workspace)}

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def ask(self, prompt='Hello', provider=None, parent=None, session=None, tier='low'):
        request = {'prompt': prompt, 'models': MODELS, 'tier': tier, 'parent': parent}
        if session:
            request['conversation_session'] = session
        return chat.answer(self.s, self.meta, self.storage, request,
                           provider=provider or Fake())

    def parser_gateway(self, **kwargs):
        self.s.create('parser')
        run = self.s.begin_run('parser', 'Prompt', [], None, purpose='parser')
        return InvocationGateway(kwargs.get('path', self.s.path), run, Fake(),
                                 ledger=kwargs.get('ledger', self.s))

    def unused_invoke(self, gateway):
        return gateway.invoke('parser', 'unused', '', {}, {}, 'unused')

    def test_borrowed_store_remains_open_and_no_transaction_crosses_provider(self):
        seen = []
        def inspect(answer, payload):
            seen.append(self.s.db.in_transaction)
            self.assertIsNotNone(self.s.db.execute('SELECT 1').fetchone())
        result = self.ask(provider=Fake(fault=inspect))
        self.assertTrue(result['result']['admission']['admitted'])
        self.assertEqual(seen, [False])
        self.assertFalse(self.s.db.in_transaction)
        self.assertEqual(self.s.db.execute('SELECT 1').fetchone(), (1,))

    def test_rejects_outstanding_transaction_without_committing_or_closing_it(self):
        gateway = self.parser_gateway()
        self.s.db.execute('BEGIN')
        with self.assertRaisesRegex(InvalidUpdate, 'outstanding caller transaction'):
            self.unused_invoke(gateway)
        self.assertTrue(self.s.db.in_transaction)
        self.s.db.rollback()

    def test_rejects_wrong_path_without_creating_a_database(self):
        wrong = self.storage / 'wrong.sqlite'
        gateway = self.parser_gateway(path=wrong)
        with self.assertRaisesRegex(InvalidUpdate, 'database path differs'):
            self.unused_invoke(gateway)
        self.assertFalse(wrong.exists())

    def test_rejects_changed_observed_inode(self):
        gateway = self.parser_gateway()
        with patch.object(self.s, '_audit_identity', return_value=('replacement', 1)):
            with self.assertRaisesRegex(InvalidUpdate, 'database identity changed'):
                self.unused_invoke(gateway)

    def test_rejects_stale_policy_and_closed_connection(self):
        gateway = self.parser_gateway()
        logic = self.s.logic_hash
        self.s.logic_hash = '0' * 64
        try:
            with self.assertRaisesRegex(InvalidUpdate, 'policy changed'):
                self.unused_invoke(gateway)
        finally:
            self.s.logic_hash = logic
        self.s.close()
        with self.assertRaisesRegex(InvalidUpdate, 'connection is unavailable'):
            self.unused_invoke(gateway)

    def test_rechecks_policy_after_provider_and_retains_pending_consumption(self):
        logic = self.s.logic_hash
        def change_policy(answer, payload):
            self.s.logic_hash = 'changed-during-provider'
        try:
            with self.assertRaisesRegex(InvalidUpdate, 'policy changed'):
                self.ask(provider=Fake(fault=change_policy))
        finally:
            self.s.logic_hash = logic
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM invocations').fetchone()[0], 1)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM invocation_receipts').fetchone()[0], 0)
        sid = self.s.db.execute('SELECT session FROM invocations').fetchone()[0]
        self.assertGreater(usage(self.s, sid)['used']['tokens'], 0)
        self.assertFalse(self.s.db.in_transaction)

    def test_rejects_active_node_from_another_session(self):
        from orchestration import scope
        result = self.ask()
        node = self.s.db.execute('SELECT id FROM orchestration_nodes WHERE session=?',
                                (result['session'],)).fetchone()[0]
        gateway = self.parser_gateway()
        with scope(node), self.assertRaisesRegex(InvalidUpdate, 'orchestration scope differs'):
            self.unused_invoke(gateway)

    def test_provider_cannot_leave_a_transaction_for_receipt_to_commit(self):
        def leave_open(answer, payload):
            self.s.db.execute('BEGIN')
        with self.assertRaisesRegex(InvalidUpdate, 'outstanding caller transaction'):
            self.ask(provider=Fake(fault=leave_open))
        self.assertTrue(self.s.db.in_transaction)
        self.s.db.rollback()
        self.assertGreater(self.s.db.execute('SELECT COUNT(*) FROM orchestration_charges').fetchone()[0], 0)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM invocation_receipts').fetchone()[0], 0)

    def test_external_user_update_during_provider_is_quarantined_and_charged(self):
        def amend(answer, payload):
            other = InvocationLedger(self.s.path)
            try:
                sid = other.db.execute('SELECT session FROM protocol_runs ORDER BY created_at DESC LIMIT 1').fetchone()[0]
                statement = 'Keep network disabled.'
                other.apply_user_patch(sid, 'concurrent-user-amendment', statement,
                    [{'key': 'network_allowed', 'value': 'false', 'quote': statement}],
                    other.snapshot(sid)['revision'])
            finally:
                other.close()
        with self.assertRaisesRegex(InvalidUpdate, 'contract or context binding mismatch'):
            self.ask(provider=Fake(fault=amend))
        import json
        session, raw = self.s.db.execute('SELECT session,record_json FROM invocation_receipts').fetchone()
        receipt = json.loads(raw)
        self.assertEqual(receipt['status'], 'quarantined')
        self.assertTrue(any('revision changed' in e for e in receipt['admission']['errors']))
        self.assertGreater(usage(self.s, session)['used']['calls'], 0)
        self.assertFalse(self.s.snapshot(session)['constraints']['network_allowed'])
        self.assertTrue(self.s.verify_invocations(session))

    def test_profile_context_and_consumed_budget_survive_model_switch(self):
        first = self.ask('Remember Cedar-724. No network, edits, tests or dependencies.')
        sid, parent = first['session'], first['result']['invocation_id']
        with self.s.db:
            batches.flush(self.s, 'borrowed-test-controller')
        seq, digest = self.s.db.execute('SELECT seq,digest FROM orchestration_accounting_checkpoints ORDER BY seq DESC LIMIT 1').fetchone()
        anchor = {'seq': seq, 'digest': digest}
        batches.recover(self.s, trusted_anchor=anchor, audit_prefix=True)
        before = usage(self.s, sid)
        origin = copy.deepcopy(self.s.origin(sid))
        constraints = copy.deepcopy(self.s.snapshot(sid)['constraints'])
        observed = []
        def inspect(answer, payload):
            observed.append((self.s.db.in_transaction, self.s._accounting_audit_prefix_enabled,
                             dict(self.s._accounting_trusted_anchor)))
        second = self.ask('Continue with the saved Cedar-724 context.',
            provider=Fake(model='medium', fault=inspect), parent=parent, session=sid, tier='mid')
        self.assertTrue(second['result']['admission']['admitted'])
        self.assertTrue(second['memory_used'])
        self.assertEqual(observed, [(False, True, anchor)])
        self.assertEqual(self.s._accounting_trusted_anchor, anchor)
        self.assertTrue(self.s._accounting_audit_prefix_enabled)
        self.assertEqual(self.s.origin(sid), origin)
        self.assertEqual(self.s.snapshot(sid)['constraints'], constraints)
        after = usage(self.s, sid)
        for key in ('limits', 'base_limits', 'allocation_version'):
            self.assertEqual(after[key], before[key])
        for key, amount in before['used'].items():
            self.assertGreaterEqual(after['used'][key], amount)
        self.assertGreater(after['used']['calls'], before['used']['calls'])
        self.assertTrue(self.s.audit_valid(deep=True))
        self.assertTrue(self.s._accounting_audit_prefix_enabled)
        self.assertEqual(self.s._accounting_trusted_anchor, anchor)

    def test_transport_timeout_is_settled_and_different_model_does_not_refund(self):
        def timeout(answer, payload):
            raise TimeoutError('synthetic provider timeout')
        first = self.ask(provider=Fake(fault=timeout))
        self.assertFalse(first['result']['admission']['admitted'])
        self.assertEqual(first['result']['failure_kind'], 'timeout')
        sid = first['session']
        before = usage(self.s, sid)
        self.assertGreater(before['used']['calls'], 0)
        self.assertGreater(before['used']['tokens'], 0)
        second = self.ask('Continue this saved request.', provider=Fake(model='medium'),
                          session=sid, tier='mid')
        self.assertTrue(second['result']['admission']['admitted'])
        after = usage(self.s, sid)
        for key, amount in before['used'].items():
            self.assertGreaterEqual(after['used'][key], amount)
        self.assertTrue(self.s.verify_invocations(sid))

    def test_provider_reselecting_a_valid_anchor_is_rejected_without_refund(self):
        first = self.ask('Remember Cedar-724.')
        sid, parent = first['session'], first['result']['invocation_id']
        with self.s.db:
            batches.flush(self.s, 'original-controller-selection')
        seq, digest = self.s.db.execute('SELECT seq,digest FROM orchestration_accounting_checkpoints ORDER BY seq DESC LIMIT 1').fetchone()
        original = {'seq': seq, 'digest': digest}
        batches.recover(self.s, trusted_anchor=original, audit_prefix=True)
        before = usage(self.s, sid)
        def reselect(answer, payload):
            with self.s.db:
                batches.flush(self.s, 'different-explicit-controller-selection')
            new_seq, new_digest = self.s.db.execute('SELECT seq,digest FROM orchestration_accounting_checkpoints ORDER BY seq DESC LIMIT 1').fetchone()
            self.assertNotEqual(new_seq, original['seq'])
            batches.recover(self.s, trusted_anchor={'seq': new_seq, 'digest': new_digest}, audit_prefix=True)
        with self.assertRaisesRegex(InvalidUpdate, 'binding changed during invocation'):
            self.ask('Continue the same context.', provider=Fake(fault=reselect),
                     parent=parent, session=sid)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM invocation_receipts').fetchone()[0], 1)
        self.assertGreater(usage(self.s, sid)['used']['calls'], before['used']['calls'])
        self.assertTrue(self.s.verify_invocations(sid, complete=False))

    def test_provider_cannot_clear_only_the_original_audit_profile(self):
        first = self.ask()
        sid, parent = first['session'], first['result']['invocation_id']
        with self.s.db:
            batches.flush(self.s, 'original-selection')
        seq, digest = self.s.db.execute('SELECT seq,digest FROM orchestration_accounting_checkpoints ORDER BY seq DESC LIMIT 1').fetchone()
        anchor = {'seq': seq, 'digest': digest}
        batches.recover(self.s, trusted_anchor=anchor, audit_prefix=True)
        def clear_profile(answer, payload):
            batches.recover(self.s, trusted_anchor=anchor, audit_prefix=False)
        with self.assertRaisesRegex(InvalidUpdate, 'binding changed during invocation'):
            self.ask('Continue.', provider=Fake(fault=clear_profile), parent=parent, session=sid)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM invocation_receipts').fetchone()[0], 1)
        self.assertTrue(self.s.verify_invocations(sid, complete=False))

    def test_deep_audit_then_external_write_restores_only_selected_profile_before_writer(self):
        first = self.ask('Remember Cedar-724.')
        sid, parent = first['session'], first['result']['invocation_id']
        with self.s.db:
            batches.flush(self.s, 'deep-profile-original')
        seq, digest = self.s.db.execute('SELECT seq,digest FROM orchestration_accounting_checkpoints ORDER BY seq DESC LIMIT 1').fetchone()
        anchor = {'seq': seq, 'digest': digest}
        batches.recover(self.s, trusted_anchor=anchor, audit_prefix=True)
        self.assertTrue(batches.verify(self.s, deep=True))
        self.assertEqual(self.s._accounting_cache.recovery_mode, 'strict')
        other = InvocationLedger(self.s.path)
        try:
            with other.db:
                other._audit('BORROWED_GATEWAY_EXTERNAL_APPEND', {'unrelated_host_bookkeeping': True})
        finally:
            other.close()
        def inspect(answer, payload):
            self.assertFalse(self.s.db.in_transaction)
            self.assertEqual(self.s._accounting_cache.recovery_mode, 'trusted_seal')
        with patch('accounting_batches._rebuild', side_effect=AssertionError('unexpected strict rebuild')):
            second = self.ask('Continue Cedar-724.', provider=Fake(fault=inspect),
                              parent=parent, session=sid)
        self.assertTrue(second['result']['admission']['admitted'])
        self.assertEqual(self.s._accounting_trusted_anchor, anchor)
        self.assertTrue(self.s._accounting_audit_prefix_enabled)


if __name__ == '__main__':
    unittest.main()
