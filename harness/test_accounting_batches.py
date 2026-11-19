"""Observable accounting invariants across batching, interruption and recovery."""
import copy
import json
import random
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import accounting_batches as batches
import orchestration as m
import review_budget
from context_manager import InvalidUpdate, canonical, sha
from invocation_gateway import InvocationLedger


LIMITS = {'operations': 10000, 'calls': 10000, 'tokens': 1000000,
          'source_bytes': 1024 * 100000, 'seconds': 100000, 'actions': 10000}
COST = {'operations': 1, 'calls': 1, 'tokens': 256,
        'source_bytes': 100, 'seconds': 10, 'actions': 0}


class AccountingBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.sqlite'
        self.s = InvocationLedger(self.path)
        batches.configure(self.s, 2)
        self.sid = 'batch-task'
        self.s.create(self.sid)
        values = {'task_id': self.sid, 'goal': 'Remember Cedar CED724. Review only.',
                  'native_task': True, 'review_only': True, 'allowed_paths': ['a.py'],
                  'network_allowed': False, 'dependencies_allowed': False,
                  'edits_allowed': False, 'tests_allowed': False,
                  'max_tools': 10000, 'review_calls': 10000,
                  'review_tokens': 1000000, 'review_source_kib': 100000,
                  'review_seconds': 100000, 'review_page_kib': 512,
                  'max_actions': 10000}
        statement = canonical(values)
        ops = []
        for key, value in values.items():
            encoded = (('true' if value else 'false') if type(value) is bool
                       else ','.join(value) if isinstance(value, list) else str(value))
            ops.append({'key': key, 'value': encoded, 'quote': statement})
        self.s.apply_user_patch(self.sid, 'fixture', statement, ops, 0)
        self.s.pinned(self.sid)
        context = {'project': 'Cedar', 'ticket': 'CED724',
                   'latest_turn': 'Review this architecture.'}
        with self.s.db:
            self.root = m.create(self.s, 'accounting_test', self.sid,
                                 binding=m.binding(self.s, self.sid),
                                 permissions=m.permissions(self.s, self.sid),
                                 limits=dict(LIMITS), goal=values['goal'], context=context,
                                 context_hash=sha(canonical(context)))

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def reopen(self):
        self.s.close()
        self.s = InvocationLedger(self.path)

    def child(self, parent=None, cost=None, limits=None):
        parent = parent or self.root
        cost = copy.deepcopy(cost or COST)
        with self.s.db:
            return m.create(self.s, 'execution', parent['session'], parent['id'],
                            binding=copy.deepcopy(parent['binding']),
                            permissions=copy.deepcopy(parent['permissions']),
                            limits=limits or {k: max(1, value) for k, value in cost.items()},
                            goal=parent['goal'], context=copy.deepcopy(parent.get('context')),
                            context_hash=parent.get('context_hash', sha(canonical(parent.get('context')))))

    def dispatch(self, child, cost=None, kind='retrieval', model='small', resource_id=None):
        with self.s.db:
            self.s.db.execute('BEGIN IMMEDIATE')
            with m.scope(child['id']):
                return m.admit(self.s, child['session'], kind,
                               resource_id or 'request-' + child['id'],
                               copy.deepcopy(cost or COST), paths=('a.py',),
                               model=model, resource={'decision': 'ALLOW'})

    def finish(self, child, owner, elapsed=1):
        with self.s.db:
            m.settle(self.s, owner, 'receipt-' + owner['charge_id'], elapsed)
            m.transition(self.s, child['id'], 'DONE', 'TEST_COMPLETED')

    def complete_one(self, model='small', elapsed=1, kind='retrieval'):
        child = self.child()
        owner = self.dispatch(child, model=model, kind=kind)
        self.finish(child, owner, elapsed)
        return child, owner

    def flush(self):
        with self.s.db:
            self.s.db.execute('BEGIN IMMEDIATE')
            return batches.flush(self.s)

    def full_spent(self, nid):
        """Independent exhaustive source-table reference, never a projection."""
        used = {key: 0 for key in m.node(self.s, nid)['limits']}
        rows = self.s.db.execute('''
            WITH RECURSIVE owned(id) AS (
              SELECT ? UNION ALL
              SELECT n.id FROM orchestration_nodes n JOIN owned ON n.parent_id=owned.id
            )
            SELECT c.cost_json,r.record_json
            FROM orchestration_charges c JOIN owned ON c.node_id=owned.id
            LEFT JOIN orchestration_settlements r ON r.charge_id=c.id
        ''', (nid,))
        for charge, settlement in rows:
            value = json.loads(settlement)['used'] if settlement else json.loads(charge)
            for key, count in value.items():
                used[key] += count
        return used

    def active_ids(self, nid):
        return {item['id'] if isinstance(item, dict) else item[0]
                if isinstance(item, tuple) else item for item in m.active_children(self.s, nid)}

    def test_many_rotations_match_source_totals_without_resetting_allocation(self):
        contract = self.s.pinned(self.sid)
        original = copy.deepcopy(self.root)
        for i in range(9):
            self.complete_one(model='small' if i < 4 else 'large', elapsed=i + 1)
            self.assertEqual(m.spent(self.s, self.root['id']), self.full_spent(self.root['id']))
            self.assertEqual(m.spent(self.s, self.root['id'])['calls'], i + 1)
        self.assertGreaterEqual(len(batches.checkpoints(self.s)), 4)
        self.assertEqual(self.s.pinned(self.sid), contract)
        self.assertEqual(m.node(self.s, self.root['id']), original)
        self.assertEqual(self.active_ids(self.root['id']), set())
        self.assertTrue(batches.verify(self.s, deep=True))

    def test_request_and_late_settlement_tails_stay_within_committed_batch_bounds(self):
        pending = []
        for _ in range(8):
            child = self.child()
            pending.append((child, self.dispatch(child)))
            state = batches.status(self.s)
            self.assertLess(state['tail_requests'], 2)
            self.assertLess(state['tail_changes'], 4)
        self.assertEqual(m.spent(self.s, self.root['id'])['calls'], 8)
        self.assertEqual(batches.status(self.s)['pending_charges'], 8)
        self.reopen()
        self.assertEqual(batches.status(self.s)['batch_size'], 2)
        for child, owner in pending:
            self.finish(child, owner)
            state = batches.status(self.s)
            self.assertLess(state['tail_requests'], 2)
            self.assertLess(state['tail_changes'], 4)
        self.assertEqual(batches.status(self.s)['pending_charges'], 0)
        self.assertEqual(m.spent(self.s, self.root['id']), self.full_spent(self.root['id']))

    def test_repeated_empty_flush_does_not_rotate_or_change_spending(self):
        self.complete_one()
        first = self.flush()
        history = batches.checkpoints(self.s)
        used = m.spent(self.s, self.root['id'])
        self.assertEqual(self.flush(), first)
        self.assertEqual(batches.checkpoints(self.s), history)
        self.assertEqual(m.spent(self.s, self.root['id']), used)

    def test_reopen_retains_context_permissions_spending_and_checkpoint_prefix(self):
        for i in range(5):
            self.complete_one(model='small' if i < 3 else 'large')
        self.flush()
        before = m.spent(self.s, self.root['id'])
        history = copy.deepcopy(batches.checkpoints(self.s))
        contract = self.s.pinned(self.sid)
        self.reopen()
        self.assertEqual(m.node(self.s, self.root['id']), self.root)
        self.assertEqual(self.s.pinned(self.sid), contract)
        self.assertEqual(m.spent(self.s, self.root['id']), before)
        self.assertEqual(batches.checkpoints(self.s)[:len(history)], history)
        self.complete_one(model='large')
        after = m.spent(self.s, self.root['id'])
        self.assertEqual(after['calls'], before['calls'] + 1)
        self.assertEqual(after['tokens'], before['tokens'] + COST['tokens'])
        self.assertEqual(after['source_bytes'], before['source_bytes'] + COST['source_bytes'])
        self.assertEqual(self.root['permissions'], m.node(self.s, self.root['id'])['permissions'])
        self.assertTrue(batches.verify(self.s, deep=True))

    def test_unfinished_dispatch_survives_checkpoint_and_reopen_conservatively(self):
        child = self.child()
        owner = self.dispatch(child)
        self.flush()
        self.reopen()
        self.assertEqual(m.state(self.s, child['id']), 'RUNNING')
        self.assertEqual(m.spent(self.s, self.root['id']), COST)
        self.assertEqual(m.spent(self.s, child['id']), COST)
        self.finish(child, owner, elapsed=None)
        self.assertEqual(m.spent(self.s, self.root['id']), COST)
        self.assertTrue(batches.verify(self.s, deep=True))

    def test_late_settlement_of_sealed_charge_counts_once(self):
        first = self.child()
        owner = self.dispatch(first)
        self.flush()
        for _ in range(5):
            self.complete_one()
        self.reopen()
        before = m.spent(self.s, self.root['id'])
        self.finish(first, owner, elapsed=3)
        after = m.spent(self.s, self.root['id'])
        self.assertEqual(after['seconds'], before['seconds'] - COST['seconds'] + 3)
        for key in set(COST) - {'seconds'}:
            self.assertEqual(after[key], before[key])
        with self.s.db:
            m.settle(self.s, owner, 'duplicate-receipt', elapsed=999)
        self.assertEqual(m.spent(self.s, self.root['id']), after)
        self.assertEqual(after, self.full_spent(self.root['id']))
        self.assertTrue(batches.verify(self.s, deep=True))

    def test_terminal_unsettled_work_is_not_refunded_or_reserved_again(self):
        child = self.child()
        owner = self.dispatch(child)
        with self.s.db:
            m.transition(self.s, child['id'], 'CANCELLED', 'HOST_CANCELLED')
        self.flush()
        self.reopen()
        self.assertEqual(m.spent(self.s, self.root['id']), COST)
        self.assertNotIn(child['id'], self.active_ids(self.root['id']))
        frame = m.gates(self.s, self.root['id'], {key: 0 for key in COST})[0]
        self.assertEqual(frame['consumed'], COST)
        self.assertEqual(frame['reserved_for_other_children'], {key: 0 for key in COST})
        with self.s.db:
            m.settle(self.s, owner, 'late-cancelled-receipt', elapsed=None)
        self.assertEqual(m.spent(self.s, self.root['id']), COST)

    def test_cancel_before_dispatch_only_releases_unused_reservation(self):
        self.complete_one()
        before = m.spent(self.s, self.root['id'])
        queued = self.child()
        self.assertIn(queued['id'], self.active_ids(self.root['id']))
        with self.s.db:
            m.transition(self.s, queued['id'], 'CANCELLED', 'CANCELLED_BEFORE_DISPATCH')
        self.flush()
        self.assertEqual(m.spent(self.s, self.root['id']), before)
        self.assertNotIn(queued['id'], self.active_ids(self.root['id']))
        frame = m.gates(self.s, self.root['id'], {key: 0 for key in COST})[0]
        self.assertEqual(frame['reserved_for_other_children'], {key: 0 for key in COST})

    def test_nested_scope_preserves_ancestor_spending_and_reservations(self):
        limits = {key: max(1, value * 8) for key, value in COST.items()}
        group = self.child(limits=limits)
        child = self.child(parent=group)
        owner = self.dispatch(child)
        self.finish(child, owner)
        self.flush()
        self.assertEqual(m.spent(self.s, group['id']), self.full_spent(group['id']))
        self.assertEqual(m.spent(self.s, self.root['id']), self.full_spent(self.root['id']))
        frames = m.gates(self.s, group['id'], {key: 0 for key in COST})
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0]['consumed'], frames[1]['consumed'])
        self.assertEqual(frames[1]['reserved_for_other_children'], {key: 0 for key in COST})
        self.assertIn(group['id'], self.active_ids(self.root['id']))
        self.reopen()
        self.assertEqual(m.spent(self.s, group['id']), self.full_spent(group['id']))

    def test_atomic_rollback_discards_charge_projection_and_rotated_checkpoint(self):
        self.complete_one()
        self.flush()
        before = m.spent(self.s, self.root['id'])
        history = copy.deepcopy(batches.checkpoints(self.s))
        child = self.child()
        with self.assertRaisesRegex(RuntimeError, 'injected crash'):
            with self.s.db:
                self.s.db.execute('BEGIN IMMEDIATE')
                with m.scope(child['id']):
                    m.admit(self.s, self.sid, 'retrieval', 'rolled-back-request', COST,
                            paths=('a.py',), model='large', resource={'decision': 'ALLOW'})
                batches.flush(self.s)
                raise RuntimeError('injected crash after rotation')
        self.assertEqual(m.spent(self.s, self.root['id']), before)
        self.assertEqual(m.state(self.s, child['id']), 'READY')
        self.assertEqual(batches.checkpoints(self.s), history)
        self.assertIsNone(self.s.db.execute('SELECT 1 FROM orchestration_charges WHERE resource_id=?',
                                           ('rolled-back-request',)).fetchone())
        self.reopen()
        self.assertEqual(m.spent(self.s, self.root['id']), before)
        self.finish(child, self.dispatch(child, model='large'))
        self.assertEqual(m.spent(self.s, self.root['id'])['calls'], before['calls'] + 1)

    def test_configuration_cannot_reset_a_live_batch_budget(self):
        self.complete_one()
        before = m.spent(self.s, self.root['id'])
        with self.assertRaises((InvalidUpdate, ValueError)):
            batches.configure(self.s, 4)
        self.reopen()
        self.assertEqual(m.spent(self.s, self.root['id']), before)

    def test_rolled_back_rowids_cannot_reuse_an_uncommitted_spending_projection(self):
        self.complete_one()
        child = self.child()
        with self.assertRaises(RuntimeError):
            with self.s.db:
                self.s.db.execute('BEGIN IMMEDIATE')
                with m.scope(child['id']):
                    m.admit(self.s, self.sid, 'retrieval', 'uncommitted-rowid', COST,
                            paths=('a.py',), resource={'decision': 'ALLOW'})
                batches.flush(self.s)
                raise RuntimeError('rollback')
        # Dispatch immediately, without calling spent() or reopening first. SQLite
        # may reuse the rolled-back rowids; the old projection is not authority.
        owner = self.dispatch(child, model='large')
        self.finish(child, owner)
        self.assertEqual(m.spent(self.s, self.root['id'])['calls'], 2)
        self.assertEqual(m.spent(self.s, self.root['id']), self.full_spent(self.root['id']))
        self.assertTrue(batches.verify(self.s, deep=True))

    def test_descendant_and_charge_lookups_have_supporting_indexes(self):
        for table, column in (('orchestration_nodes', 'parent_id'),
                              ('orchestration_charges', 'node_id')):
            with self.subTest(table=table):
                prefixes = []
                for row in self.s.db.execute('PRAGMA index_list(' + table + ')'):
                    fields = list(self.s.db.execute('PRAGMA index_info(' + row[1] + ')'))
                    if fields:
                        prefixes.append(fields[0][2])
                self.assertIn(column, prefixes)
                plan = self.s.db.execute('EXPLAIN QUERY PLAN SELECT id FROM ' + table
                                         + ' WHERE ' + column + '=?', (self.root['id'],)).fetchall()
                self.assertTrue(any('SEARCH' in row[-1] for row in plan), plan)

    def test_two_connections_observe_each_others_committed_batch_costs(self):
        second = InvocationLedger(self.path)
        try:
            self.complete_one(model='small')
            self.assertEqual(m.spent(second, self.root['id']), self.full_spent(self.root['id']))
            for _ in range(3):
                self.complete_one(model='large')
            self.flush()
            self.assertEqual(m.spent(second, self.root['id']), self.full_spent(self.root['id']))
            self.assertTrue(batches.verify(second, deep=True))
        finally:
            second.close()

    def test_fast_path_does_not_walk_sealed_orchestration_history(self):
        for _ in range(8):
            self.complete_one()
        self.flush()
        # Warm and cold spending reads must use the verified projection rather
        # than invoke the legacy recursive Python descendant walker.
        original = m.descendants
        try:
            def forbidden(*args, **kwargs):
                raise AssertionError('historical descendant walk on accounting fast path')
            m.descendants = forbidden
            self.assertEqual(m.spent(self.s, self.root['id'])['calls'], 8)
            self.reopen()
            self.assertEqual(m.spent(self.s, self.root['id'])['calls'], 8)
        finally:
            m.descendants = original

    def test_scope_and_new_contract_still_gate_dispatch_after_many_batches(self):
        for _ in range(6):
            self.complete_one()
        child = self.child()
        before = m.spent(self.s, self.root['id'])
        with self.assertRaisesRegex(InvalidUpdate, 'scope|read_paths'):
            with self.s.db, m.scope(child['id']):
                m.admit(self.s, self.sid, 'retrieval', 'out-of-scope', COST,
                        paths=('secret.py',), resource={'decision': 'ALLOW'})
        state = self.s.snapshot(self.sid)
        statement = 'The current review is now restricted to b.py.'
        self.s.apply_user_patch(self.sid, 'revoke-a', statement,
                               [{'key': 'allowed_paths', 'value': 'b.py', 'quote': statement}],
                               state['revision'])
        with self.assertRaisesRegex(InvalidUpdate, 'contract|policy|calibration'):
            with self.s.db, m.scope(child['id']):
                m.admit(self.s, self.sid, 'retrieval', 'stale-permissions', COST,
                        paths=('a.py',), model='large', resource={'decision': 'ALLOW'})
        self.assertEqual(m.spent(self.s, self.root['id']), before)

    def test_native_task_keeps_special_review_usage_and_extra_action_seconds(self):
        with self.s.db:
            task = m.task(self.s, self.sid)
        expected = dict(review_budget.usage(self.s, self.sid)['used'], actions=0)
        self.assertEqual(m.spent(self.s, task['id']), expected)
        # A synthetic generic retrieval charge must not become a second source of
        # task model/source spending: native task totals come from review_budget.
        child = self.child(parent=task)
        owner = self.dispatch(child)
        self.finish(child, owner)
        self.flush()
        self.assertEqual(m.spent(self.s, task['id']), expected)
        # Legacy edit/test seconds are added separately to the task total.
        with self.s.db:
            child = m.create(self.s, 'execution', self.sid, task['id'],
                             binding=task['binding'], permissions=task['permissions'],
                             limits=dict(LIMITS), goal=task['goal'], context={},
                             context_hash=sha(canonical({})))
            self.s.db.execute('INSERT INTO orchestration_charges VALUES(?,?,?,?,?)',
                              ('legacy-edit', child['id'], 'edit', 'legacy-action', canonical(COST)))
            m.transition(self.s, child['id'], 'RUNNING', 'DISPATCH',
                         {'charge_id': 'legacy-edit', 'resource_id': 'legacy-action',
                          'kind': 'edit', 'frames': [{'decision': 'ALLOW', 'reserved_next': COST}]})
        # Close/reopen forces a source-verified migration/rebuild of direct legacy data.
        self.reopen()
        self.assertEqual(m.spent(self.s, task['id'])['seconds'], expected['seconds'] + COST['seconds'])

    def test_generated_sequences_equal_exhaustive_totals_across_reopens(self):
        rng = random.Random(724)
        for i in range(24):
            child = self.child()
            owner = self.dispatch(child, model='small' if i % 3 else 'large')
            if i % 4 == 0:
                with self.s.db:
                    m.transition(self.s, child['id'], 'CANCELLED', 'CANCELLED_IN_FLIGHT')
            else:
                self.finish(child, owner, elapsed=rng.randint(1, 9))
            if i % 5 == 0:
                self.flush()
                self.reopen()
            self.assertEqual(m.spent(self.s, self.root['id']), self.full_spent(self.root['id']))
        self.assertTrue(batches.verify(self.s, deep=True))

    def checkpoint_fixture(self):
        for _ in range(4):
            self.complete_one()
        self.flush()
        self.assertTrue(batches.verify(self.s, deep=True))

    def test_checkpoint_tables_are_append_only(self):
        self.checkpoint_fixture()
        for table in ('orchestration_accounting_config', 'orchestration_accounting_checkpoints'):
            for operation in ('UPDATE', 'DELETE'):
                with self.subTest(table=table, operation=operation):
                    with self.assertRaises(sqlite3.IntegrityError):
                        if operation == 'UPDATE':
                            self.s.db.execute('UPDATE ' + table + " SET record_json='{}'")
                        else:
                            self.s.db.execute('DELETE FROM ' + table)
                    self.s.db.rollback()

    def test_changed_checkpoint_is_rejected_on_warm_read(self):
        self.checkpoint_fixture()
        self.assertEqual(m.spent(self.s, self.root['id'])['calls'], 4)
        with self.s.db:
            self.s.db.execute('DROP TRIGGER immutable_orchestration_accounting_checkpoints_UPDATE')
            self.s.db.execute("UPDATE orchestration_accounting_checkpoints SET record_json='{}' "
                              'WHERE seq=(SELECT MAX(seq) FROM orchestration_accounting_checkpoints)')
        with self.assertRaises(InvalidUpdate):
            m.spent(self.s, self.root['id'])
        with self.assertRaises(InvalidUpdate):
            batches.verify(self.s, deep=True)

    def test_self_consistent_checkpoint_rewrite_does_not_forge_source_totals(self):
        self.checkpoint_fixture()
        seq, raw, _ = self.s.db.execute('SELECT seq,record_json,digest FROM '
                                       'orchestration_accounting_checkpoints ORDER BY seq DESC LIMIT 1').fetchone()
        record = json.loads(raw)
        record['roots'][self.root['id']]['used']['tokens'] -= 1
        forged = canonical(record)
        with self.s.db:
            self.s.db.execute('DROP TRIGGER immutable_orchestration_accounting_checkpoints_UPDATE')
            self.s.db.execute('UPDATE orchestration_accounting_checkpoints SET record_json=?,digest=? WHERE seq=?',
                              (forged, sha('accounting-checkpoint-v1:' + forged), seq))
        with self.assertRaisesRegex(InvalidUpdate, 'authoritative source history'):
            self.reopen()
            m.spent(self.s, self.root['id'])

    def test_sealed_source_rewrite_is_detected_by_deep_verification(self):
        self.checkpoint_fixture()
        cid, raw = self.s.db.execute('SELECT id,cost_json FROM orchestration_charges ORDER BY rowid LIMIT 1').fetchone()
        value = json.loads(raw)
        value['tokens'] += 1
        with self.s.db:
            self.s.db.execute('DROP TRIGGER immutable_orchestration_charges_UPDATE')
            self.s.db.execute('UPDATE orchestration_charges SET cost_json=? WHERE id=?',
                              (canonical(value), cid))
        with self.assertRaises(InvalidUpdate):
            batches.verify(self.s, deep=True)

    def test_checkpoint_table_drop_cannot_hide_its_audit_anchor(self):
        self.checkpoint_fixture()
        with self.s.db:
            self.s.db.execute('DROP TABLE orchestration_accounting_checkpoints')
        with self.assertRaisesRegex(InvalidUpdate, 'audit anchor'):
            self.reopen()
            batches.verify(self.s, deep=True)

    def test_orphan_legacy_charge_cannot_be_certified_by_a_checkpoint(self):
        child = self.child()
        with self.s.db:
            self.s.db.execute('INSERT INTO orchestration_charges VALUES(?,?,?,?,?)',
                              ('orphan', child['id'], 'retrieval', 'orphan-request', canonical(COST)))
        with self.assertRaisesRegex(InvalidUpdate, 'uncommitted dispatch'):
            batches.verify(self.s, deep=True)
        with self.assertRaisesRegex(InvalidUpdate, 'uncommitted dispatch'):
            self.flush()

    def test_legacy_database_without_batch_schema_keeps_all_existing_spending(self):
        # Create the pre-batching original schema/data shape, with no checkpoint
        # or audit anchors to mistake for a valid downgrade of a newer ledger.
        children = [self.child() for _ in range(5)]
        self.assertEqual(batches.checkpoints(self.s), [])
        with self.s.db:
            self.s.db.execute('DROP TABLE orchestration_accounting_checkpoints')
            self.s.db.execute('DROP TABLE orchestration_accounting_config')
            for i, child in enumerate(children):
                cid, resource = 'old-charge-' + str(i), 'old-request-' + str(i)
                self.s.db.execute('INSERT INTO orchestration_charges VALUES(?,?,?,?,?)',
                                  (cid, child['id'], 'retrieval', resource, canonical(COST)))
                previous = 'GENESIS'

                def event(seq, source, target, kind, data):
                    nonlocal previous
                    value = {'node_id': child['id'], 'seq': seq, 'from': source,
                             'to': target, 'kind': kind, 'data': data,
                             'kernel': None, 'previous': previous, 'at': time.time()}
                    raw = canonical(value)
                    digest = sha('orchestration-event-v1:' + raw)
                    self.s.db.execute('INSERT INTO orchestration_events VALUES(?,?,?,?)',
                                      (child['id'], seq, raw, digest))
                    previous = digest

                event(1, 'READY', 'RUNNING', 'DISPATCH',
                      {'charge_id': cid, 'resource_id': resource, 'kind': 'retrieval',
                       'frames': [{'decision': 'ALLOW', 'reserved_next': COST}]})
                if i < 3:
                    settled = {'charge_id': cid, 'receipt': 'old-receipt-' + str(i),
                               'used': dict(COST, seconds=1)}
                    raw = canonical(settled)
                    self.s.db.execute('INSERT INTO orchestration_settlements VALUES(?,?,?)',
                                      (cid, raw, sha('orchestration-settlement-v1:' + raw)))
                    event(2, 'RUNNING', 'READY', 'SETTLED', settled)
                    event(3, 'READY', 'DONE', 'OLD_COMPLETION', {})
                else:
                    event(2, 'RUNNING', 'CANCELLED', 'OLD_CANCELLATION', {})
        self.reopen()
        expected = {key: value * 5 for key, value in COST.items()}
        expected['seconds'] = 23
        self.assertEqual(m.spent(self.s, self.root['id']), expected)
        self.assertEqual(m.node(self.s, self.root['id']), self.root)
        self.assertTrue(batches.verify(self.s, deep=True))
        self.flush()
        self.reopen()
        self.assertEqual(m.spent(self.s, self.root['id']), expected)
        self.complete_one(model='large')
        self.assertEqual(m.spent(self.s, self.root['id'])['calls'], 6)

    def test_matching_checkpoint_and_audit_payload_cannot_hide_a_broken_audit_hash(self):
        self.checkpoint_fixture()
        seq, raw = self.s.db.execute('SELECT seq,record_json FROM orchestration_accounting_checkpoints '
                                     'ORDER BY seq DESC LIMIT 1').fetchone()
        value = json.loads(raw)
        value['reason'] = 'rewritten metadata'
        raw = canonical(value)
        digest = sha('accounting-checkpoint-v1:' + raw)
        anchor = self.s.db.execute("SELECT seq FROM page_audit WHERE event='ORCHESTRATION_ACCOUNTING_CHECKPOINT' "
                                   "AND json_extract(payload_json,'$.seq')=?", (seq,)).fetchone()[0]
        with self.s.db:
            self.s.db.execute('DROP TRIGGER immutable_orchestration_accounting_checkpoints_UPDATE')
            self.s.db.execute('DROP TRIGGER immutable_page_audit_UPDATE')
            self.s.db.execute('UPDATE orchestration_accounting_checkpoints SET record_json=?,digest=? WHERE seq=?',
                              (raw, digest, seq))
            # The checkpoint digest and its anchor payload agree, but the audit
            # event hash still commits to the original payload.
            self.s.db.execute('UPDATE page_audit SET payload_json=? WHERE seq=?',
                              (canonical({'seq': seq, 'digest': digest}), anchor))
        self.assertFalse(self.s.audit_valid())
        with self.assertRaisesRegex(InvalidUpdate, 'audit chain'):
            batches.verify(self.s, deep=True)

    def test_external_prefix_rewrite_is_caught_even_when_schema_version_is_restored(self):
        self.checkpoint_fixture()
        before = m.spent(self.s, self.root['id'])
        schema = self.s.db.execute('PRAGMA schema_version').fetchone()[0]
        old_data_version = self.s.db.execute('PRAGMA data_version').fetchone()[0]
        first = self.s.db.execute('SELECT rowid,charge_id,record_json FROM orchestration_settlements '
                                  'ORDER BY rowid LIMIT 1').fetchone()
        last = self.s.db.execute('SELECT rowid,record_json,digest FROM orchestration_settlements '
                                 'ORDER BY rowid DESC LIMIT 1').fetchone()
        self.assertNotEqual(first[0], last[0])
        trigger = self.s.db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' "
                                    "AND name='immutable_orchestration_settlements_UPDATE'").fetchone()[0]
        rewritten = json.loads(first[2])
        rewritten['used']['tokens'] -= 1
        raw = canonical(rewritten)
        other = sqlite3.connect(self.path)
        try:
            with other:
                other.execute('DROP TRIGGER immutable_orchestration_settlements_UPDATE')
                other.execute('UPDATE orchestration_settlements SET record_json=?,digest=? WHERE charge_id=?',
                              (raw, sha('orchestration-settlement-v1:' + raw), first[1]))
                other.execute(trigger)
                other.execute('PRAGMA schema_version=' + str(schema))
        finally:
            other.close()
        restored = self.s.db.execute('PRAGMA schema_version').fetchone()[0]
        self.assertNotEqual(self.s.db.execute('PRAGMA data_version').fetchone()[0], old_data_version)
        self.assertEqual(self.s.db.execute('SELECT rowid,record_json,digest FROM orchestration_settlements '
                                           'ORDER BY rowid DESC LIMIT 1').fetchone(), last)
        if restored != schema:
            # Some system SQLite builds enable defensive mode and deliberately
            # make schema_version writes a no-op. Still check rejection there;
            # the exact same-schema attack is exercised on permissive builds.
            with self.assertRaises(InvalidUpdate):
                m.spent(self.s, self.root['id'])
            self.skipTest('SQLite defensive mode disallows restoring schema_version')
        with self.assertRaises(InvalidUpdate):
            m.spent(self.s, self.root['id'])
        self.assertEqual(before['calls'], 4)

    def test_all_accounting_sources_reject_zero_negative_and_lower_rowid_inserts(self):
        self.checkpoint_fixture()
        pending = []
        for _ in range(2):
            child = self.child()
            pending.append((child, self.dispatch(child)))
        event_children = [self.child(), self.child()]

        def source_values(table, ordinal):
            if table == 'orchestration_nodes':
                value = copy.deepcopy(self.root)
                value.update(id='rowid-node-' + str(ordinal), parent_id=self.root['id'])
                raw = canonical(value)
                return value['id'], self.root['id'], self.sid, raw, sha('orchestration-node-v1:' + raw)
            if table == 'orchestration_charges':
                return ('rowid-charge-' + str(ordinal), pending[ordinal][0]['id'], 'retrieval',
                        'rowid-request-' + str(ordinal), canonical(COST))
            if table == 'orchestration_settlements':
                cid = pending[ordinal][1]['charge_id']
                raw = canonical({'charge_id': cid, 'receipt': 'rowid-receipt', 'used': COST})
                return cid, raw, sha('orchestration-settlement-v1:' + raw)
            child = event_children[ordinal]
            raw = canonical({'node_id': child['id'], 'seq': 1, 'from': 'READY', 'to': 'READY',
                             'kind': 'ROWID_TEST', 'data': {}, 'kernel': None,
                             'previous': 'GENESIS', 'at': time.time()})
            return child['id'], 1, raw, sha('orchestration-event-v1:' + raw)

        for table in ('orchestration_nodes', 'orchestration_charges',
                      'orchestration_settlements', 'orchestration_events'):
            columns = [row[1] for row in self.s.db.execute('PRAGMA table_info(' + table + ')')]
            sql = ('INSERT INTO ' + table + '(rowid,' + ','.join(columns) + ') VALUES('
                   + ','.join('?' for _ in range(len(columns) + 1)) + ')')
            for mode in ('zero', 'negative', 'lower-positive'):
                with self.subTest(table=table, mode=mode):
                    self.s.db.execute('BEGIN IMMEDIATE')
                    try:
                        if mode == 'lower-positive':
                            maximum = self.s.db.execute('SELECT MAX(rowid) FROM ' + table).fetchone()[0]
                            self.s.db.execute(sql, (maximum + 5,) + source_values(table, 0))
                            rowid = maximum + 3
                            ordinal = 1
                        else:
                            rowid, ordinal = (0 if mode == 'zero' else -1), 0
                        with self.assertRaisesRegex(sqlite3.IntegrityError, 'non-monotonic accounting source'):
                            self.s.db.execute(sql, (rowid,) + source_values(table, ordinal))
                    finally:
                        self.s.db.rollback()
        self.assertTrue(batches.verify(self.s, deep=True))

    def test_standalone_flush_audit_failure_rolls_back_checkpoint_and_recovers(self):
        self.complete_one()
        self.assertFalse(self.s.db.in_transaction)
        before = m.spent(self.s, self.root['id'])
        history = batches.checkpoints(self.s)
        original_audit = self.s._audit

        def fail_checkpoint(event, payload):
            if event == 'ORCHESTRATION_ACCOUNTING_CHECKPOINT':
                raise RuntimeError('injected checkpoint audit failure')
            return original_audit(event, payload)

        with patch.object(self.s, '_audit', side_effect=fail_checkpoint):
            with self.assertRaisesRegex(RuntimeError, 'checkpoint audit failure'):
                batches.flush(self.s)
        self.assertFalse(self.s.db.in_transaction)
        self.assertEqual(batches.checkpoints(self.s), history)
        self.assertEqual(m.spent(self.s, self.root['id']), before)
        self.assertTrue(batches.verify(self.s, deep=True))
        batches.flush(self.s)
        self.assertEqual(len(batches.checkpoints(self.s)), len(history) + 1)
        self.assertFalse(self.s.db.in_transaction)
        self.reopen()
        self.assertEqual(m.spent(self.s, self.root['id']), before)

    def test_configuration_changes_append_epochs_and_preserve_previous_choices(self):
        prior = self.s.db.execute('SELECT id,record_json,digest FROM orchestration_accounting_config ORDER BY id').fetchall()
        batches.configure(self.s, 4)
        batches.configure(self.s, 8)
        after = self.s.db.execute('SELECT id,record_json,digest FROM orchestration_accounting_config ORDER BY id').fetchall()
        self.assertEqual(after[:len(prior)], prior)
        self.assertEqual(len(after), len(prior) + 2)
        self.assertEqual(batches.status(self.s)['batch_size'], 8)
        self.complete_one()
        with self.assertRaises(InvalidUpdate):
            batches.configure(self.s, 16)
        self.assertEqual(self.s.db.execute('SELECT id,record_json,digest FROM orchestration_accounting_config ORDER BY id').fetchall(), after)


if __name__ == '__main__':
    unittest.main()
