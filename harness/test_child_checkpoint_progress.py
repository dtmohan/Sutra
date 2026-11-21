"""Child checkpoint telemetry measures accepted artifacts and observed cost."""
import copy
import json
import sqlite3
import unittest
from unittest.mock import patch

import child_checkpoint_progress as progress
from context_manager import canonical, sha
from invocation_gateway import InvocationLedger
from invocation_timing import reserved_seconds
from lifecycle import reconcile
import monitoring
import orchestration as machine
import review_budget
import test_orchestration as fixtures


FIELD = 'child_checkpoint_progress'


def citation(path, quote):
    return {'path': path, 'quote': quote}


class CheckpointProgressTests(unittest.TestCase):
    def setUp(self):
        self.envelope = {'id': 'a' * 32, 'role': 'subagent', 'model': 'fixture-large'}
        first = {'invocation_id': 'b' * 32, 'citations': [citation('a.py', 'same source')],
                 'remaining': ['Check prior branch.']}
        second = {'invocation_id': 'c' * 32, 'citations': [citation('b.py', 'older source')],
                  'remaining': ['Check something else.', 'Keep this check.']}
        self.payload = {
            'child_slice': {'version': 1, 'ordinal': 2, 'predecessor': second['invocation_id']},
            'recovery_checkpoint': [first],
            'child_checkpoint': [first, second],
        }
        self.receipt = {
            'invocation_id': self.envelope['id'],
            'role': self.envelope['role'],
            'status': 'accepted',
            'proposal': {
                'slice_status': 'continue',
                'citations': [citation('a.py', 'same source'), citation('a.py', 'same source'),
                              citation('b.py', 'same source')],
                'remaining': ['Check   prior branch.', 'Keep this check.', 'Read the next file.'],
            },
            'token_usage': {'input_tokens': 1400, 'output_tokens': 800},
        }

    def test_counts_exact_source_pairs_and_checks_across_recovery_history(self):
        first = progress.capture(self.envelope, self.payload, self.receipt, elapsed_seconds=1.25)
        self.assertEqual(first['mode'], 'observe_only')
        self.assertEqual(first['checkpoint'], 'continued')
        self.assertEqual(first['prior_checkpoints'], 2)
        self.assertEqual(first['evidence'], {'new_citations': 1, 'retained_citations': 1, 'citations': 2})
        self.assertEqual(first['next_checks'], {'new': 1, 'repeated': 2, 'retired': 1})
        self.assertEqual(first['semantic_assessment'], 'unassessed')
        self.assertNotIn('same source', canonical(first))
        self.assertNotIn('prior branch', canonical(first))
        slower = dict(self.receipt, token_usage={'input_tokens': 1400, 'output_tokens': 5800})
        later = progress.capture(self.envelope, self.payload, slower, elapsed_seconds=50)
        self.assertEqual(later, dict(first, cost={'input_tokens': 1400, 'output_tokens': 5800,
                                                 'elapsed_seconds': 50}))

    def test_only_sliced_child_calls_qualify_and_withholding_keeps_cost(self):
        self.assertIsNone(progress.capture(dict(self.envelope, role='architect'),
                                           self.payload, self.receipt))
        self.assertIsNone(progress.capture(self.envelope, {}, self.receipt))
        denied = dict(self.receipt, status='quarantined', proposal={'citations': 'invalid'},
                      token_usage={'input_tokens': 0, 'output_tokens': None})
        result = progress.capture(self.envelope, self.payload, denied, elapsed_seconds=2.5)
        self.assertEqual(result['checkpoint'], 'withheld')
        self.assertEqual(result['prior_checkpoints'], 2)
        self.assertIsNone(result['evidence'])
        self.assertIsNone(result['next_checks'])
        self.assertEqual(result['cost'], {'input_tokens': 0, 'output_tokens': None, 'elapsed_seconds': 2.5})

    def test_dashboard_reader_ignores_and_does_not_backfill_unverified_rows(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.executescript('''
            CREATE TABLE invocation_receipts(session TEXT, seq INTEGER, invocation_id TEXT, id TEXT, record_json TEXT);
            CREATE TABLE invocation_quarantine(invocation_id TEXT PRIMARY KEY, reason TEXT, created_at REAL);
        ''')
        observation = progress.capture(self.envelope, self.payload, self.receipt)
        raw = canonical(dict(self.receipt, **{FIELD: observation}))
        db.execute('INSERT INTO invocation_receipts VALUES (?,?,?,?,?)',
                   ('fixture', 1, self.envelope['id'], 'bad digest', raw))
        old = dict(self.receipt, invocation_id='d' * 32)
        historical = canonical(old)
        db.execute('INSERT INTO invocation_receipts VALUES (?,?,?,?,?)',
                   ('fixture', 2, old['invocation_id'], sha('invocation-receipt-v1:' + historical), historical))
        before = db.total_changes
        self.assertEqual(progress.latest(db), [])
        self.assertEqual(db.total_changes, before)


class CheckpointGatewayTests(unittest.TestCase):
    setUp = fixtures.OrchestrationTests.setUp
    tearDown = fixtures.OrchestrationTests.tearDown
    step = fixtures.OrchestrationTests.step
    until = fixtures.OrchestrationTests.until
    children = fixtures.OrchestrationTests.children
    provider = fixtures.OrchestrationTests.provider
    sources_for_slices = fixtures.OrchestrationTests.sources_for_slices

    def receipt(self, invocation_id):
        return self.s.db.execute(
            'SELECT record_json, id FROM invocation_receipts WHERE invocation_id=?', (invocation_id,)
        ).fetchone()

    def test_immutable_observation_survives_model_switch_and_respects_later_quarantine(self):
        self.sources_for_slices()
        ready = self.children()
        branch = ready['children'][0]['id']
        sid = self.meta['active']
        pinned = copy.deepcopy(self.s.pinned(sid))
        permissions = copy.deepcopy(machine.node(self.s, branch)['permissions'])
        original_call = fixtures.Child.call

        def checkpoint(worker, *args):
            proposal, record = original_call(worker, *args)
            proposal['slice_status'] = 'continue'
            record['response'] = {'done': True, 'prompt_eval_count': 144, 'eval_count': 81}
            return proposal, record

        with patch.object(fixtures.Child, 'call', checkpoint):
            partial = self.step(ready)
        self.assertEqual(partial['child']['slice_status'], 'checkpoint')
        accepted = partial['child']['result']
        iid = accepted['invocation_id']
        first_packet = self.models[-1][1].payloads[0]
        raw, digest = self.receipt(iid)
        self.assertEqual(sha('invocation-receipt-v1:' + raw), digest)
        first = json.loads(raw)[FIELD]
        self.assertEqual(first['checkpoint'], 'continued')
        self.assertEqual(first['prior_checkpoints'], 0)
        self.assertEqual(first['cost']['input_tokens'], 144)
        self.assertEqual(first['cost']['output_tokens'], 81)
        self.assertGreaterEqual(first['cost']['elapsed_seconds'], 0)
        self.assertNotIn(FIELD, accepted['accepted_output'])
        self.assertNotIn(FIELD, first_packet)
        self.assertEqual(self.s.load_result(sid, iid), accepted['accepted_output'])
        before = review_budget.usage(self.s, sid)
        branch_spent = machine.spent(self.s, branch)

        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        self.tier = 'high'
        second = self.step(partial)['child']['result']
        self.assertTrue(second['admission']['admitted'])
        final = json.loads(self.receipt(second['invocation_id'])[0])[FIELD]
        self.assertEqual(final['checkpoint'], 'completed')
        self.assertEqual(final['prior_checkpoints'], 1)
        self.assertEqual(final['model'], fixtures.MODELS['high'])
        self.assertEqual(final['next_checks']['repeated'], 1)
        packet = self.models[-1][1].payloads[0]
        self.assertEqual(packet['child_checkpoint'][0]['invocation_id'], iid)
        self.assertEqual(packet['contract'], first_packet['contract'])
        self.assertIn('CED724', canonical(packet['memory']))
        self.assertEqual(self.s.pinned(sid), pinned)
        self.assertEqual(machine.node(self.s, branch)['permissions'], permissions)
        self.assertEqual(machine.spent(self.s, branch)['calls'], branch_spent['calls'] + 1)
        after = review_budget.usage(self.s, sid)
        self.assertEqual(after['limits'], before['limits'])
        self.assertEqual(after['used']['calls'], before['used']['calls'] + 1)
        self.assertGreater(after['used']['tokens'], before['used']['tokens'])
        self.assertTrue(self.s.verify_invocations(sid))

        self.s.quarantine_invocation(sid, second['invocation_id'], 'test later quarantine')
        verified = monitoring.dashboard(self.root)['review_progress_observations']
        self.assertEqual(verified[1], first)
        self.assertEqual(verified[0], dict(final, gateway_status='quarantined', checkpoint='withheld',
                                           evidence=None, next_checks=None))
        self.assertEqual(self.receipt(iid), (raw, digest))
        self.assertEqual(review_budget.usage(self.s, sid), after)

    def test_observer_failure_leaves_child_admitted_and_reservation_settled(self):
        ready = self.children()
        branch = ready['children'][0]['id']
        sid = self.meta['active']
        before = review_budget.usage(self.s, sid)
        with patch.object(progress, 'capture', side_effect=RuntimeError('diagnostic failed')) as capture:
            out = self.step(ready)
        capture.assert_called_once()
        accepted = out['child']['result']
        self.assertTrue(accepted['admission']['admitted'])
        self.assertEqual(machine.state(self.s, branch), 'DONE')
        self.assertNotIn(FIELD, json.loads(self.receipt(accepted['invocation_id'])[0]))
        self.assertEqual(review_budget.usage(self.s, sid)['used']['calls'], before['used']['calls'] + 1)
        pending = self.s.db.execute('SELECT COUNT(*) FROM invocations i LEFT JOIN invocation_receipts r '
                                    'ON r.invocation_id=i.id WHERE r.id IS NULL').fetchone()[0]
        self.assertEqual(pending, 0)
        self.assertTrue(self.s.verify_invocations(sid))

    def test_reconciled_elapsed_time_is_unknown_and_reserved_spending_is_kept(self):
        ready = self.children()
        branch = ready['children'][0]['id']
        with patch.object(fixtures.Child, 'call', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.step(ready)
        raw = self.s.db.execute('SELECT i.record_json FROM invocations i LEFT JOIN invocation_receipts r '
                                'ON r.invocation_id=i.id WHERE r.id IS NULL').fetchone()[0]
        envelope = json.loads(raw)
        charged = machine.spent(self.s, branch)
        with patch.object(fixtures.Child, 'call', side_effect=AssertionError('no model replay')):
            reconcile(self.s, self.ws)
        receipt = json.loads(self.receipt(envelope['id'])[0])
        self.assertEqual(receipt['elapsed_seconds'], reserved_seconds(envelope))
        observed = receipt[FIELD]
        self.assertEqual(observed['checkpoint'], 'withheld')
        self.assertIsNone(observed['evidence'])
        self.assertEqual(observed['cost'], {'input_tokens': None, 'output_tokens': None, 'elapsed_seconds': None})
        self.assertEqual(machine.spent(self.s, branch), charged)


if __name__ == '__main__':
    unittest.main()
