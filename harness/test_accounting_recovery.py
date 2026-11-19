"""Reservation carry, trusted recovery and timely checkpoint regressions."""
import copy
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import accounting_batches as batches
import orchestration as m
import test_accounting_batches as fixtures
from context_manager import InvalidUpdate, canonical, sha
from invocation_gateway import InvocationLedger


class AccountingRecoveryTests(unittest.TestCase):
    # Reuse setup helpers without inheriting and rediscovering the original tests.
    setUp = fixtures.AccountingBatchTests.setUp
    tearDown = fixtures.AccountingBatchTests.tearDown
    reopen = fixtures.AccountingBatchTests.reopen
    child = fixtures.AccountingBatchTests.child
    dispatch = fixtures.AccountingBatchTests.dispatch
    finish = fixtures.AccountingBatchTests.finish
    complete_one = fixtures.AccountingBatchTests.complete_one
    flush = fixtures.AccountingBatchTests.flush
    full_spent = fixtures.AccountingBatchTests.full_spent
    active_ids = fixtures.AccountingBatchTests.active_ids

    def anchor(self):
        last = batches.checkpoints(self.s)[-1]
        return {'seq': last['seq'], 'digest': last['digest']}

    def checkpoint_with_pending(self):
        for _ in range(5):
            self.complete_one()
        pending = self.child()
        owner = self.dispatch(pending)
        self.flush()
        return pending, owner, self.anchor()

    def test_seal_carries_explicit_pending_cost_and_owner_commitment(self):
        pending, owner, _ = self.checkpoint_with_pending()
        seal = batches.checkpoints(self.s)[-1]
        self.assertEqual(seal['version'], 2)
        entries = seal['pending_entries']
        self.assertIn(owner['charge_id'], entries)
        entry = entries[owner['charge_id']]
        self.assertEqual(entry['node_id'], pending['id'])
        self.assertEqual(entry['kind'], 'retrieval')
        self.assertEqual(entry['declared'], fixtures.COST)
        self.assertEqual(entry['used'], fixtures.COST)
        self.assertFalse(entry['settled'])
        self.assertTrue(entry['dispatch_receipt'])
        self.assertEqual(entry['owner_digest'], sha('orchestration-node-v1:' + canonical(pending)))
        self.assertIn('recovery_state', seal)
        self.assertEqual(seal['pending_count'], len(entries))

    def test_explicit_anchor_restores_pending_costs_without_historical_rebuild(self):
        pending, owner, _ = self.checkpoint_with_pending()
        terminal = self.child()
        terminal_owner = self.dispatch(terminal)
        with self.s.db:
            m.transition(self.s, terminal['id'], 'CANCELLED', 'TEST_INTERRUPTED_AFTER_DISPATCH')
        self.flush()
        anchor = self.anchor()
        used = m.spent(self.s, self.root['id'])
        root = m.node(self.s, self.root['id'])
        contract = self.s.pinned(self.sid)
        self.reopen()
        with patch.object(batches, '_rebuild', side_effect=AssertionError('historical rebuild on anchored path')):
            batches.recover(self.s, trusted_anchor=anchor)
            self.assertEqual(m.spent(self.s, self.root['id']), used)
            self.assertEqual(m.spent(self.s, pending['id']), fixtures.COST)
            self.assertEqual(m.spent(self.s, terminal['id']), fixtures.COST)
            self.assertEqual(m.state(self.s, pending['id']), 'RUNNING')
            self.assertEqual(m.state(self.s, terminal['id']), 'CANCELLED')
            self.assertEqual(m.node(self.s, self.root['id']), root)
            self.assertEqual(self.s.pinned(self.sid), contract)
            self.assertEqual(batches.status(self.s)['pending_charges'], 2)
        self.assertEqual(terminal_owner['node_id'], terminal['id'])
        self.assertEqual(owner['node_id'], pending['id'])

    def test_default_recovery_does_not_silently_trust_a_database_seal(self):
        self.checkpoint_with_pending()
        self.reopen()
        with patch.object(batches, '_rebuild', wraps=batches._rebuild) as rebuild:
            batches.recover(self.s)
            self.assertGreaterEqual(rebuild.call_count, 1)
        self.assertTrue(batches.verify(self.s, deep=True))

    def test_legacy_v1_seal_remains_readable_before_a_v2_pending_seal(self):
        self.complete_one()
        basis = batches._projection(self.s).basis()
        record = {key: basis[key] for key in
                  ('watermarks', 'source_heads', 'roots', 'pending_count', 'pending_hash')}
        record.update(version=1, seq=1, previous='GENESIS', batch_size=2,
                      reason='legacy_format_fixture', at=time.time())
        raw = canonical(record)
        digest = sha('accounting-checkpoint-v1:' + raw)
        with self.s.db:
            self.s.db.execute('INSERT INTO orchestration_accounting_checkpoints VALUES(?,?,?)', (1, raw, digest))
            self.s._audit('ORCHESTRATION_ACCOUNTING_CHECKPOINT', {'seq': 1, 'digest': digest})
        used = self.full_spent(self.root['id'])
        self.reopen()
        batches.recover(self.s)
        self.assertEqual(m.spent(self.s, self.root['id']), used)
        pending = self.child()
        owner = self.dispatch(pending)
        self.flush()
        seals = batches.checkpoints(self.s)
        self.assertEqual(seals[0]['version'], 1)
        self.assertEqual(seals[0]['digest'], digest)
        self.assertEqual(seals[-1]['version'], 2)
        self.assertIn(owner['charge_id'], seals[-1]['pending_entries'])
        self.assertEqual(m.spent(self.s, self.root['id']), self.full_spent(self.root['id']))
        self.assertTrue(batches.verify(self.s, deep=True))

    def test_wrong_or_unknown_external_anchor_does_not_release_work(self):
        pending, _, anchor = self.checkpoint_with_pending()
        before = self.full_spent(self.root['id'])
        for bad in ({'seq': anchor['seq'], 'digest': '0' * 64},
                    {'seq': anchor['seq'] + 10, 'digest': anchor['digest']},
                    {'seq': True, 'digest': anchor['digest']}):
            with self.subTest(anchor=bad):
                self.reopen()
                with self.assertRaises(InvalidUpdate):
                    batches.recover(self.s, trusted_anchor=bad)
                self.assertEqual(self.full_spent(self.root['id']), before)
                self.assertEqual(m.node(self.s, pending['id'])['context_hash'], pending['context_hash'])

    def test_late_settlement_after_anchor_applies_once_across_another_reopen(self):
        pending, owner, anchor = self.checkpoint_with_pending()
        before = m.spent(self.s, self.root['id'])
        self.reopen()
        batches.recover(self.s, trusted_anchor=anchor)
        self.finish(pending, owner, elapsed=3)
        after = m.spent(self.s, self.root['id'])
        self.assertEqual(after['seconds'], before['seconds'] - fixtures.COST['seconds'] + 3)
        for name in set(after) - {'seconds'}:
            self.assertEqual(after[name], before[name])
        self.flush()
        next_anchor = self.anchor()
        self.reopen()
        batches.recover(self.s, trusted_anchor=next_anchor)
        with self.s.db:
            m.settle(self.s, owner, 'repeated-old-receipt', elapsed=999)
        self.assertEqual(m.spent(self.s, self.root['id']), after)
        self.assertEqual(after, self.full_spent(self.root['id']))
        self.assertTrue(batches.verify(self.s, deep=True))

    def test_cancelled_unknown_work_keeps_full_cost_after_anchored_recovery(self):
        pending, owner, anchor = self.checkpoint_with_pending()
        self.reopen()
        batches.recover(self.s, trusted_anchor=anchor)
        before = m.spent(self.s, self.root['id'])
        with self.s.db:
            m.settle(self.s, owner, 'cancelled-unmeasured', elapsed=None)
            m.transition(self.s, pending['id'], 'CANCELLED', 'TEST_CANCELLED')
        self.assertEqual(m.spent(self.s, self.root['id']), before)
        self.flush()
        anchor = self.anchor()
        self.reopen()
        batches.recover(self.s, trusted_anchor=anchor)
        self.assertEqual(m.spent(self.s, self.root['id']), before)
        self.assertEqual(m.state(self.s, pending['id']), 'CANCELLED')

    def test_model_change_after_anchored_recovery_preserves_contract_and_spending(self):
        _, _, anchor = self.checkpoint_with_pending()
        original_root = copy.deepcopy(self.root)
        before = m.spent(self.s, self.root['id'])
        self.reopen()
        batches.recover(self.s, trusted_anchor=anchor)
        completed, _ = self.complete_one(model='larger-local-model')
        after = m.spent(self.s, self.root['id'])
        self.assertEqual(after['calls'], before['calls'] + 1)
        self.assertEqual(after['tokens'], before['tokens'] + fixtures.COST['tokens'])
        self.assertEqual(m.node(self.s, self.root['id']), original_root)
        self.assertEqual(completed['permissions'], original_root['permissions'])
        self.assertEqual(completed['context_hash'], original_root['context_hash'])
        self.assertEqual(after, self.full_spent(self.root['id']))

    def test_stale_trusted_anchor_verifies_newer_work_instead_of_rebuilding_history(self):
        _, _, anchor = self.checkpoint_with_pending()
        self.complete_one(model='large')
        self.complete_one(model='large')
        self.flush()
        before = m.spent(self.s, self.root['id'])
        self.reopen()
        with patch.object(batches, '_rebuild', side_effect=AssertionError('full replay after an explicit anchor')):
            batches.recover(self.s, trusted_anchor=anchor)
            self.assertEqual(m.spent(self.s, self.root['id']), before)
        self.assertEqual(before, self.full_spent(self.root['id']))

    def test_competing_writer_refresh_verifies_only_since_the_trusted_head(self):
        _, _, anchor = self.checkpoint_with_pending()
        batches.recover(self.s, trusted_anchor=anchor)
        before = m.spent(self.s, self.root['id'])
        reader = self.s
        other = InvocationLedger(self.path)
        try:
            self.s = other
            for _ in range(3):
                self.complete_one(model='large')
            expected = self.full_spent(self.root['id'])
        finally:
            self.s = reader
            other.close()
        with patch.object(batches, '_rebuild', side_effect=AssertionError('full replay during anchored refresh')):
            self.assertEqual(m.spent(self.s, self.root['id']), expected)
        self.assertEqual(expected['calls'], before['calls'] + 3)
        self.assertEqual(m.node(self.s, self.root['id']), self.root)

    def test_trusted_recovery_cannot_silently_use_rewritten_pending_permissions(self):
        pending, _, anchor = self.checkpoint_with_pending()
        altered = copy.deepcopy(pending)
        altered['permissions']['read_paths'] = ['.']
        raw = canonical(altered)
        with self.s.db:
            self.s.db.execute('DROP TRIGGER immutable_orchestration_nodes_UPDATE')
            self.s.db.execute('UPDATE orchestration_nodes SET record_json=?,digest=? WHERE id=?',
                              (raw, sha('orchestration-node-v1:' + raw), pending['id']))
        self.reopen()
        # A caller-approved seal authenticates its saved owner digest, not an
        # arbitrary replacement source row bearing a newly computed digest.
        with self.assertRaises(InvalidUpdate):
            batches.recover(self.s, trusted_anchor=anchor)
            m.gates(self.s, pending['id'], fixtures.COST)

    def test_valid_new_contract_still_requires_rebinding_after_trusted_recovery(self):
        _, _, anchor = self.checkpoint_with_pending()
        original_spent = m.spent(self.s, self.root['id'])
        self.reopen()
        batches.recover(self.s, trusted_anchor=anchor)
        with self.s.db:
            revision = self.s.snapshot(self.sid)['revision']
            self.s.apply_user_patch(self.sid, 'steer', 'Review a document instead.',
                                    [{'key': 'goal', 'value': 'Review a document instead.',
                                      'quote': 'Review a document instead.'}], revision)
        with self.assertRaises(InvalidUpdate):
            m.gates(self.s, self.root['id'], fixtures.COST)
        self.assertEqual(self.full_spent(self.root['id']), original_spent)

    def test_trusted_anchor_is_connection_local_and_not_a_database_permission(self):
        _, _, anchor = self.checkpoint_with_pending()
        batches.set_trusted_anchor(self.s, anchor)
        batches.recover(self.s, trusted_anchor=anchor)
        self.assertEqual(batches.status(self.s)['recovery_mode'], 'trusted_seal')
        self.reopen()
        with patch.object(batches, '_rebuild', wraps=batches._rebuild) as replay:
            batches.recover(self.s)
            self.assertGreaterEqual(replay.call_count, 1)
        self.assertEqual(batches.status(self.s)['recovery_mode'], 'strict')

    def test_idle_seal_carries_active_work_without_changing_its_allocation(self):
        pending = self.child()
        owner = self.dispatch(pending)
        state = batches.status(self.s)
        before = m.spent(self.s, self.root['id'])
        original = copy.deepcopy(self.root)
        now = state['last_activity_at']
        self.assertIsNone(batches.seal_if_idle(self.s, idle_seconds=30, max_age_seconds=300, now=now + 29))
        digest = batches.seal_if_idle(self.s, idle_seconds=30, max_age_seconds=300, now=now + 31)
        self.assertTrue(digest)
        last = batches.checkpoints(self.s)[-1]
        self.assertIn(owner['charge_id'], last['pending_entries'])
        self.assertEqual(m.spent(self.s, self.root['id']), before)
        self.assertEqual(m.node(self.s, self.root['id']), original)
        self.assertEqual(m.state(self.s, pending['id']), 'RUNNING')
        self.reopen()
        self.assertEqual(m.spent(self.s, self.root['id']), before)
        self.finish(pending, owner, elapsed=None)
        self.assertEqual(m.spent(self.s, self.root['id']), before)

    def test_max_age_seals_a_busy_tail_before_it_becomes_idle(self):
        self.complete_one()
        state = batches.status(self.s)
        opened = state['batch_opened_at']
        self.assertIsNotNone(opened)
        before = m.spent(self.s, self.root['id'])
        with patch('time.time', return_value=opened + 299):
            with self.s.db:
                m.transition(self.s, self.root['id'], 'READY', 'TEST_ACTIVITY')
            batches.status(self.s)
        digest = batches.seal_if_idle(self.s, idle_seconds=30, max_age_seconds=300, now=opened + 300)
        self.assertTrue(digest)
        self.assertEqual(m.spent(self.s, self.root['id']), before)
        self.assertEqual(m.node(self.s, self.root['id'])['context'], self.root['context'])

    def test_idle_tick_on_empty_tail_does_not_emit_another_checkpoint(self):
        self.complete_one()
        self.flush()
        count = len(batches.checkpoints(self.s))
        state = batches.status(self.s)
        digest = batches.seal_if_idle(self.s, idle_seconds=30, max_age_seconds=300,
                                      now=state['last_activity_at'] + 1000)
        self.assertIsNone(digest)
        self.assertEqual(len(batches.checkpoints(self.s)), count)

    def test_reopen_does_not_mistake_a_new_unsealed_tail_for_legacy_migration(self):
        pending = self.child()
        self.dispatch(pending)
        self.assertEqual(batches.checkpoints(self.s), [])
        before = self.full_spent(self.root['id'])
        self.reopen()
        self.assertEqual(batches.checkpoints(self.s), [])
        state = batches.status(self.s)
        self.assertEqual(state['tail_requests'], 1)
        self.assertEqual(m.spent(self.s, self.root['id']), before)
        self.assertTrue(batches.seal_if_idle(self.s, now=state['last_activity_at'] + 31))
        self.assertEqual(batches.checkpoints(self.s)[-1]['reason'], 'idle')

    def test_failed_idle_checkpoint_rolls_back_its_anchor_and_preserves_cost(self):
        pending = self.child()
        self.dispatch(pending)
        state = batches.status(self.s)
        before = self.full_spent(self.root['id'])
        count = len(batches.checkpoints(self.s))
        audit = self.s._audit

        def fail_checkpoint(event, payload):
            if event == 'ORCHESTRATION_ACCOUNTING_CHECKPOINT':
                raise RuntimeError('injected idle seal write failure')
            return audit(event, payload)

        with patch.object(self.s, '_audit', side_effect=fail_checkpoint):
            with self.assertRaisesRegex(RuntimeError, 'injected idle seal'):
                batches.seal_if_idle(self.s, idle_seconds=30, max_age_seconds=300,
                                      now=state['last_activity_at'] + 31)
        self.assertFalse(self.s.db.in_transaction)
        self.assertEqual(len(batches.checkpoints(self.s)), count)
        self.assertEqual(m.spent(self.s, self.root['id']), before)
        self.assertTrue(self.s.audit_valid())

    def test_deep_audit_failure_blocks_admission_even_after_in_memory_cache_reset(self):
        pending, _, anchor = self.checkpoint_with_pending()
        batches.recover(self.s, trusted_anchor=anchor)
        old = self.s.db.execute('SELECT charge_id,record_json,digest FROM orchestration_settlements '
                                'ORDER BY rowid LIMIT 1').fetchone()
        value = json.loads(old[1])
        value['used']['tokens'] -= 1
        raw = canonical(value)
        with self.s.db:
            self.s.db.execute('DROP TRIGGER immutable_orchestration_settlements_UPDATE')
            self.s.db.execute('UPDATE orchestration_settlements SET record_json=?,digest=? WHERE charge_id=?',
                              (raw, sha('orchestration-settlement-v1:' + raw), old[0]))
        with self.assertRaises(InvalidUpdate):
            batches.verify(self.s, deep=True)
        self.s._accounting_cache = None
        with self.assertRaises(InvalidUpdate):
            m.gates(self.s, self.root['id'], dict(fixtures.COST, actions=0))
        # Restoring the source does not silently lift the existing-store stop.
        with self.s.db:
            self.s.db.execute('UPDATE orchestration_settlements SET record_json=?,digest=? WHERE charge_id=?',
                              (old[1], old[2], old[0]))
        with self.assertRaises(InvalidUpdate):
            m.spent(self.s, self.root['id'])
        retained = json.loads(self.s.db.execute('SELECT record_json FROM orchestration_nodes WHERE id=?',
                                                (pending['id'],)).fetchone()[0])
        self.assertEqual(retained['permissions'], pending['permissions'])
        # Reopening is not an operator-approved dismissal of a recorded finding.
        with self.assertRaises(InvalidUpdate):
            self.reopen()
            batches.recover(self.s)

    def test_budget_gate_and_pure_kernel_are_measured_as_separate_stages(self):
        import accounting_metrics as metrics
        before = m.spent(self.s, self.root['id'])
        frames = m.gates(self.s, self.root['id'], fixtures.COST)
        self.assertTrue(all(frame['decision'] == 'ALLOW' for frame in frames))
        snapshot = metrics.snapshot(self.s)
        self.assertGreaterEqual(snapshot['stages']['budget_gate']['count'], 1)
        self.assertGreaterEqual(snapshot['stages']['kernel']['count'], 1)
        for stage in ('kernel', 'budget_gate'):
            self.assertGreaterEqual(snapshot['stages'][stage]['total_ns'], 0)
            self.assertGreaterEqual(snapshot['stages'][stage]['min_ns'], 0)
            self.assertLessEqual(snapshot['stages'][stage]['min_ns'], snapshot['stages'][stage]['max_ns'])
        self.assertEqual(m.spent(self.s, self.root['id']), before)

    def test_sealing_timing_does_not_change_consumption(self):
        import accounting_metrics as metrics
        self.complete_one()
        used = m.spent(self.s, self.root['id'])
        self.flush()
        stages = metrics.snapshot(self.s)['stages']
        self.assertGreaterEqual(stages['accounting_seal']['count'], 1)
        self.assertGreaterEqual(stages['accounting_seal']['total_ns'], 0)
        self.assertEqual(m.spent(self.s, self.root['id']), used)

    def test_metric_samples_are_bounded_while_totals_keep_every_observation(self):
        import accounting_metrics as metrics
        for i in range(400):
            metrics.record(self.s, 'fixture_sample', i)
        snapshot = metrics.snapshot(self.s)
        value = snapshot['stages']['fixture_sample']
        self.assertEqual(value['count'], 400)
        self.assertEqual(value['total_ns'], sum(range(400)))
        self.assertEqual(value['min_ns'], 0)
        self.assertEqual(value['max_ns'], 399)
        self.assertLessEqual(len(value['recent_ns']), snapshot['retained_samples_per_stage'])

    def test_metric_observer_failure_cannot_reject_valid_admission(self):
        import accounting_metrics as metrics

        def broken_observer(record):
            raise RuntimeError('diagnostic consumer unavailable')

        metrics.subscribe(self.s, broken_observer)
        before = m.spent(self.s, self.root['id'])
        frames = m.gates(self.s, self.root['id'], fixtures.COST)
        self.assertTrue(all(frame['decision'] == 'ALLOW' for frame in frames))
        self.assertEqual(m.spent(self.s, self.root['id']), before)
        self.assertGreaterEqual(metrics.snapshot(self.s)['stages']['kernel']['count'], 1)

    def test_idle_worker_refuses_another_workspaces_storage(self):
        from vscode_bridge import seal_idle_worker
        (self.path.parent / 'workspace.json').write_text(json.dumps({'workspace': '/another/workspace'}))
        with self.assertRaisesRegex(InvalidUpdate, 'another workspace'):
            seal_idle_worker(self.path.parent, self.path.parent / 'workspace')

    def test_idle_worker_skips_a_busy_request_without_mutation(self):
        from platform_support import lock_file
        from vscode_bridge import seal_idle_worker
        self.complete_one()
        before = m.spent(self.s, self.root['id'])
        count = len(batches.checkpoints(self.s))
        with lock_file(self.path.parent / 'bridge.lock'):
            result = seal_idle_worker(self.path.parent, self.path.parent / 'workspace')
        self.assertEqual(result, {'sealed': False, 'busy': True})
        self.assertEqual(m.spent(self.s, self.root['id']), before)
        self.assertEqual(len(batches.checkpoints(self.s)), count)

    def test_idle_worker_only_seals_bookkeeping_and_preserves_pending_charge(self):
        from vscode_bridge import seal_idle_worker
        pending = self.child()
        owner = self.dispatch(pending)
        view = batches.status(self.s)
        before = m.spent(self.s, self.root['id'])
        with patch('time.time', return_value=view['last_activity_at'] + 31), \
                patch('vscode_bridge.dispatch', side_effect=AssertionError('idle model dispatch')), \
                patch('repository_tools.execute', side_effect=AssertionError('idle source operation')):
            result = seal_idle_worker(self.path.parent, self.path.parent / 'workspace')
        self.assertTrue(result['sealed'])
        self.assertEqual(result['pending_charges'], 1)
        self.assertEqual(m.spent(self.s, self.root['id']), before)
        self.assertEqual(m.node(self.s, self.root['id'])['permissions'], self.root['permissions'])
        self.assertIn(owner['charge_id'], batches.checkpoints(self.s)[-1]['pending_entries'])

    def test_idle_worker_keeps_polling_an_unsealed_metadata_only_tail(self):
        from vscode_bridge import seal_idle_worker
        state = batches.status(self.s)
        self.assertTrue(state['tail_open'])
        self.assertEqual(state['tail_requests'], 0)
        self.assertEqual(state['tail_changes'], 0)
        with patch('time.time', return_value=state['last_activity_at'] + 1):
            result = seal_idle_worker(self.path.parent, self.path.parent / 'workspace')
        self.assertFalse(result['sealed'])
        self.assertFalse(result['empty'])
        with patch('time.time', return_value=state['last_activity_at'] + 31):
            result = seal_idle_worker(self.path.parent, self.path.parent / 'workspace')
        self.assertTrue(result['sealed'])
        self.assertTrue(result['empty'])
        self.assertEqual(m.spent(self.s, self.root['id'])['calls'], 0)

    def test_background_audit_uses_read_only_connection_and_preserves_pending_work(self):
        from accounting_audit import BackgroundAudit
        _, _, _ = self.checkpoint_with_pending()
        before = m.spent(self.s, self.root['id'])
        version = self.s.db.execute('PRAGMA data_version').fetchone()[0]
        changes = self.s.db.total_changes
        audit = BackgroundAudit()
        audit.start(self.s)
        audit._thread.join(timeout=5)
        self.assertFalse(audit.running)
        result = audit.poll(self.s)
        self.assertTrue(result['ok'], result)
        self.assertGreaterEqual(result['elapsed_ns'], 0)
        self.assertEqual(self.s.db.execute('PRAGMA data_version').fetchone()[0], version)
        self.assertEqual(self.s.db.total_changes, changes)
        self.assertEqual(m.spent(self.s, self.root['id']), before)
        self.assertEqual(batches.status(self.s)['pending_charges'], 1)

    def test_background_findings_block_the_next_admission_before_any_new_charge(self):
        from accounting_audit import BackgroundAudit
        pending, owner, _ = self.checkpoint_with_pending()
        ready = self.child()
        first = self.s.db.execute('SELECT charge_id,record_json FROM orchestration_settlements '
                                  'ORDER BY rowid LIMIT 1').fetchone()
        value = json.loads(first[1])
        value['used']['tokens'] -= 1
        raw = canonical(value)
        with self.s.db:
            self.s.db.execute('DROP TRIGGER immutable_orchestration_settlements_UPDATE')
            self.s.db.execute('UPDATE orchestration_settlements SET record_json=?,digest=? WHERE charge_id=?',
                              (raw, sha('orchestration-settlement-v1:' + raw), first[0]))
        count = self.s.db.execute('SELECT COUNT(*) FROM orchestration_charges').fetchone()[0]
        audit = BackgroundAudit()
        audit.start(self.s)
        audit._thread.join(timeout=5)
        self.assertFalse(audit.running)
        with self.assertRaises(InvalidUpdate):
            self.dispatch(ready, resource_id='must-not-be-admitted')
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM orchestration_charges').fetchone()[0], count)
        self.assertIsNone(self.s.db.execute('SELECT 1 FROM orchestration_charges WHERE resource_id=?',
                                           ('must-not-be-admitted',)).fetchone())
        charge = self.s.db.execute('SELECT cost_json FROM orchestration_charges WHERE id=?',
                                   (owner['charge_id'],)).fetchone()
        self.assertEqual(json.loads(charge[0]), fixtures.COST)
        self.s._accounting_cache = None
        with self.assertRaises(InvalidUpdate):
            m.gates(self.s, self.root['id'], fixtures.COST)
        retained = json.loads(self.s.db.execute('SELECT record_json FROM orchestration_nodes WHERE id=?',
                                                (pending['id'],)).fetchone()[0])
        self.assertEqual(retained['context_hash'], pending['context_hash'])

    def test_a_finding_in_a_rolled_back_effect_is_persisted_without_its_partial_charge(self):
        self.complete_one()
        pending = self.child()
        self.dispatch(pending)
        ready = self.child()
        before = self.full_spent(self.root['id'])
        count = self.s.db.execute('SELECT COUNT(*) FROM orchestration_charges').fetchone()[0]
        with self.assertRaisesRegex(InvalidUpdate, 'reject effect'):
            with self.s.db:
                self.s.db.execute('BEGIN IMMEDIATE')
                with m.scope(ready['id']):
                    m.admit(self.s, ready['session'], 'retrieval', 'rolled-back-effect', fixtures.COST,
                            paths=('a.py',), model='small', resource={'decision': 'ALLOW'})
                batches.block_admission(self.s, 'test finding before effect commit')
                raise InvalidUpdate('reject effect')
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM orchestration_charges').fetchone()[0], count)
        self.reopen()
        finding = self.s.db.execute('SELECT record_json FROM orchestration_accounting_findings').fetchall()
        self.assertEqual(len(finding), 1)
        with self.assertRaisesRegex(InvalidUpdate, 'admission blocked'):
            m.gates(self.s, self.root['id'], fixtures.COST)
        self.assertIsNone(self.s.db.execute('SELECT 1 FROM orchestration_charges WHERE resource_id=?',
                                           ('rolled-back-effect',)).fetchone())
        self.assertEqual(self.full_spent(self.root['id']), before)

    def test_bad_previous_hash_in_new_warm_event_latches_a_durable_stop(self):
        pending, _, anchor = self.checkpoint_with_pending()
        batches.recover(self.s, trusted_anchor=anchor)
        history = m.events(self.s, pending['id'])
        seq = history[-1]['seq'] + 1
        forged = {'node_id': pending['id'], 'seq': seq, 'from': 'RUNNING', 'to': 'RUNNING',
                  'kind': 'TEST_FORGED_PREVIOUS', 'data': {}, 'kernel': None,
                  'previous': 'f' * 64, 'at': time.time()}
        raw = canonical(forged)
        with self.s.db:
            self.s.db.execute('INSERT INTO orchestration_events VALUES(?,?,?,?)',
                              (pending['id'], seq, raw, sha('orchestration-event-v1:' + raw)))
        # The row's own digest is valid, but it does not extend its owner chain.
        with self.assertRaises(InvalidUpdate):
            batches.status(self.s)
        self.assertTrue(getattr(self.s, '_accounting_blocked', None))
        self.assertTrue(self.s.db.execute('SELECT 1 FROM orchestration_accounting_findings').fetchone())
        with self.assertRaises(InvalidUpdate):
            self.reopen()
            m.gates(self.s, self.root['id'], fixtures.COST)

    def test_headerless_forged_checkpoint_cannot_be_ignored_by_warm_reads(self):
        _, _, anchor = self.checkpoint_with_pending()
        batches.recover(self.s, trusted_anchor=anchor)
        seal = copy.deepcopy(batches.checkpoints(self.s)[-1])
        seal.pop('digest')
        seal.update(seq=anchor['seq'] + 1, previous=anchor['digest'], at=time.time())
        seal['roots'][self.root['id']]['used']['tokens'] = 0
        seal['payload_hash'] = batches._payload_hash(seal)
        raw = canonical(seal)
        digest = sha('accounting-checkpoint-v2:' + canonical(batches._header(seal)))
        with self.s.db:
            self.s.db.execute('INSERT INTO orchestration_accounting_checkpoints VALUES(?,?,?)',
                              (seal['seq'], raw, digest))
        # A correctly hashed body without its compact header or audit anchor is
        # a committed mismatch, not an absent hint that the reader may skip.
        with self.assertRaises(InvalidUpdate):
            batches.status(self.s)
        self.assertTrue(getattr(self.s, '_accounting_blocked', None))
        with self.assertRaises(InvalidUpdate):
            batches.checkpoints(self.s)
        with self.assertRaises(InvalidUpdate):
            m.gates(self.s, self.root['id'], fixtures.COST)
        self.assertTrue(self.s.db.execute('SELECT 1 FROM orchestration_accounting_findings').fetchone())
        with self.assertRaises(InvalidUpdate):
            self.reopen()
            m.gates(self.s, self.root['id'], fixtures.COST)

    def test_terminal_owners_late_settlement_restarts_the_exact_idle_interval(self):
        pending = self.child()
        owner = self.dispatch(pending)
        with self.s.db:
            m.transition(self.s, pending['id'], 'CANCELLED', 'TEST_TERMINAL_BEFORE_LATE_SETTLEMENT')
        self.flush()
        previous = batches.status(self.s)
        settled_at = previous['last_activity_at'] + 1000
        before = m.spent(self.s, self.root['id'])
        with patch('time.time', return_value=settled_at):
            with self.s.db:
                m.settle(self.s, owner, 'late-unmeasured-terminal-result', elapsed=None)
        row = self.s.db.execute('SELECT record_json FROM orchestration_settlements WHERE charge_id=?',
                                (owner['charge_id'],)).fetchone()
        self.assertEqual(json.loads(row[0])['at'], settled_at)
        self.reopen()
        state = batches.status(self.s)
        self.assertEqual(state['last_activity_at'], settled_at)
        self.assertEqual(state['batch_opened_at'], settled_at)
        self.assertEqual(state['pending_charges'], 0)
        self.assertEqual(m.spent(self.s, self.root['id']), before)
        self.assertIsNone(batches.seal_if_idle(self.s, idle_seconds=30,
                                              now=settled_at + 29.999))
        self.assertTrue(batches.seal_if_idle(self.s, idle_seconds=30,
                                            now=settled_at + 30))
        self.assertEqual(m.spent(self.s, self.root['id']), before)

    def test_other_open_connection_observes_a_durable_finding_before_admission(self):
        self.complete_one()
        before = self.full_spent(self.root['id'])
        other = InvocationLedger(self.path)
        try:
            self.assertTrue(all(frame['decision'] == 'ALLOW'
                                for frame in m.gates(other, self.root['id'], fixtures.COST)))
            batches.block_admission(self.s, 'test finding visible across workers')
            with self.assertRaisesRegex(InvalidUpdate, 'admission blocked'):
                m.gates(other, self.root['id'], fixtures.COST)
            self.assertEqual(self.full_spent(self.root['id']), before)
        finally:
            other.close()


class IdleWorkerFramesTests(unittest.TestCase):
    def test_in_memory_stream_preserves_frames_without_idle_polling(self):
        from vscode_bridge import worker_frames
        stream = io.StringIO('first\nsecond\nlast')
        self.assertEqual(list(worker_frames(stream, lambda: self.fail('in-memory idle callback'))),
                         ['first\n', 'second\n', 'last'])

    def test_real_pipe_handles_partial_unicode_and_idles_on_controller_thread(self):
        from vscode_bridge import worker_frames
        read_fd, write_fd = os.pipe()
        first_idle = threading.Event()
        partial_ready = threading.Event()
        finish_partial = threading.Event()
        controller = threading.get_ident()
        callbacks, failures = [], []
        message = '{"text":"α🙂"}\nsecond\nlast'.encode()
        split = message.index('🙂'.encode()) + 2

        def writer():
            try:
                if not first_idle.wait(2):
                    raise AssertionError('controller never observed idle input')
                os.write(write_fd, message[:split])
                partial_ready.set()
                if not finish_partial.wait(2):
                    raise AssertionError('partial UTF-8 did not leave controller responsive')
                os.write(write_fd, message[split:])
            except BaseException as error:
                failures.append(error)
            finally:
                os.close(write_fd)

        def idle():
            callbacks.append(threading.get_ident())
            first_idle.set()
            if len(callbacks) >= 2 and partial_ready.is_set():
                finish_partial.set()

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        with os.fdopen(read_fd, 'r', encoding='utf-8') as stream:
            frames = list(worker_frames(stream, idle, poll_seconds=.01))
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(frames, ['{"text":"α🙂"}\n', 'second\n', 'last'])
        self.assertGreaterEqual(len(callbacks), 2)
        self.assertTrue(all(identity == controller for identity in callbacks))

    def test_invalid_utf8_is_reported_as_a_bounded_input_error(self):
        from vscode_bridge import worker_frames
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b'\xff\n')
        os.close(write_fd)
        with os.fdopen(read_fd, 'r', encoding='utf-8') as stream:
            frames = list(worker_frames(stream, lambda: None, poll_seconds=.01))
        self.assertEqual(len(frames), 1)
        self.assertIsInstance(frames[0], ValueError)

    def test_idle_worker_does_not_create_a_database_for_empty_storage(self):
        from vscode_bridge import seal_idle_worker
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = seal_idle_worker(root, root / 'workspace')
            self.assertEqual(result, {'sealed': False, 'empty': True})
            self.assertFalse((root / 'state.sqlite').exists())


if __name__ == '__main__':
    unittest.main()
