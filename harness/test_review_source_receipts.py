"""Scope, freshness and integrity checks for retry source commitments."""
import unittest

from context_manager import ContextBudgetExceeded, InvalidUpdate
from invocation_gateway import InvocationLedger
import orchestration as machine
import orchestration_flow as flow
import review_budget
from review_source_receipts import acquire
import test_orchestration as fixtures


class ReviewSourceReceiptTests(unittest.TestCase):
    setUp = fixtures.OrchestrationTests.setUp
    tearDown = fixtures.OrchestrationTests.tearDown
    step = fixtures.OrchestrationTests.step
    until = fixtures.OrchestrationTests.until
    provider = fixtures.OrchestrationTests.provider

    def set_up_review(self):
        self.review = self.until('SYNTHESIS')
        self.sid = self.meta['active']
        self.candidates = [f'part{i}.py' for i in range(4)]
        _, lineage = flow.info(self.s, self.review['flow_id'])
        self.request = {
            'prompt': self.prompt,
            'conversation_id': self.review['conversation_id'],
            'orchestration_parent': self.review['execution_id'],
            'child_results': lineage.get('child_results', []),
        }

    def sources(self, **kwargs):
        with machine.scope(self.review['execution_id']):
            return acquire(self.s, self.meta, self.ws, self.root, self.request,
                           self.candidates, real=False, **kwargs)

    def test_reopen_reuses_exact_receipts_without_a_second_charge(self):
        self.set_up_review()
        receipts, _, plan = self.sources()
        charged = review_budget.usage(self.s, self.sid)
        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        # A rejected preflight must precede even revalidation of an existing
        # snapshot. No source-preparation receipt owns model capacity.
        seen = []

        def reject(files):
            seen.extend(files)
            raise ContextBudgetExceeded('mandatory context does not fit')

        with self.assertRaises(ContextBudgetExceeded):
            self.sources(before_read=reject)
        self.assertEqual(seen, plan['selected'])
        again, _, _ = self.sources()
        self.assertEqual(again, receipts)
        self.assertEqual(review_budget.usage(self.s, self.sid), charged)
        self.assertTrue(self.s.audit_valid())

    def test_grown_source_compiles_and_charges_a_fresh_snapshot(self):
        self.set_up_review()
        old, _, _ = self.sources()
        used = review_budget.usage(self.s, self.sid)['used']
        (self.ws / 'part0.py').write_text('def changed_entry():\n    return 724\n' + '#grown source\n' * 50)
        fresh, data, _ = self.sources()
        self.assertNotEqual(fresh, old)
        self.assertIn('changed_entry', data['sources']['part0.py'])
        charged = review_budget.usage(self.s, self.sid)['used']
        self.assertGreater(charged['source_bytes'], used['source_bytes'])
        self.assertEqual(charged['operations'], used['operations'] + 1)
        self.assertEqual(self.sources()[0], fresh)

    def test_corrupt_receipt_cannot_be_substituted_with_a_fresh_read(self):
        self.set_up_review()
        old, _, _ = self.sources()
        spent = review_budget.usage(self.s, self.sid)
        with self.s.db:
            self.s.db.execute('DROP TRIGGER immutable_repository_receipts_UPDATE')
            self.s.db.execute(
                "UPDATE repository_receipts SET record_json=record_json || ' ' WHERE id=?", (old[0],))
        with self.assertRaisesRegex(InvalidUpdate, 'Repository receipt corrupted'):
            self.sources()
        self.assertEqual(review_budget.usage(self.s, self.sid), spent)

    def test_foreign_snapshot_cannot_become_a_prepared_retry(self):
        self.set_up_review()
        self.sources()
        owner = self.review['execution_id']
        recorded = next(event['data'] for event in reversed(machine.events(self.s, owner))
                        if event['kind'] == 'REVIEW_SOURCE_PREPARED')
        # The structural ancestry and scope still matter when all receipt
        # digests are intact and a child has compiled some of the same bytes.
        foreign, = self.s.db.execute(
            """SELECT r.id FROM repository_receipts r JOIN repository_ops o ON o.id=r.operation_id
               WHERE o.session=? AND json_extract(o.record_json,'$.kind')='static_map'
               AND json_extract(o.record_json,'$.orchestration.node_id')!=?
               LIMIT 1""", (self.sid, owner)).fetchone()
        with self.s.db:
            machine.transition(self.s, owner, 'READY', 'REVIEW_SOURCE_PREPARED',
                               dict(recorded, receipts=[foreign]))
        spent = review_budget.usage(self.s, self.sid)
        with self.assertRaisesRegex(InvalidUpdate, 'belongs to another orchestration node'):
            self.sources()
        self.assertEqual(review_budget.usage(self.s, self.sid), spent)


if __name__ == '__main__':
    unittest.main()
