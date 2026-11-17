"""Exercise a length-limited claim review through Pause and Resume."""
import json
import unittest
from unittest.mock import patch

import claim_pages
import orchestration
import review_budget
import test_orchestration as fixture
from budget_accounting import used as settled_tokens
from context_manager import InvalidUpdate
from invocation_gateway import InvocationLedger
from local_model import OutputLimitExceeded
from test_chat import Fake


class ClaimReviewer(Fake):
    context_metadata = {'thinking': {'values': [False, True], 'default': True}}
    exhaust_bounded_retry = False

    def call(self, cid, system, packet, schema):
        answer, record = super().call(cid, system, packet, schema)
        scope = packet.get('review_scope')
        if scope and scope['page'] == scope['pages'] and (self.thinking is True or self.exhaust_bounded_retry):
            failure = OutputLimitExceeded('Fixture exhausted the shared output allowance')
            failure.call_record = {
                'call_id': cid, 'ok': False,
                'response': {'done': True, 'done_reason': 'length',
                             'prompt_eval_count': 400, 'eval_count': self.budget.response_reserve},
            }
            raise failure
        return answer, record


class ClaimReviewRecoveryTests(unittest.TestCase):
    setUp = fixture.OrchestrationTests.setUp
    tearDown = fixture.OrchestrationTests.tearDown
    step = fixture.OrchestrationTests.step
    until = fixture.OrchestrationTests.until
    exhaust_bounded_retry = False

    def provider(self, stage):
        if stage != 'synthesis':
            return fixture.OrchestrationTests.provider(self, stage)
        reviewer = ClaimReviewer(fixture.MODELS[self.tier])
        reviewer.exhaust_bounded_retry = self.exhaust_bounded_retry
        return reviewer

    def invocations(self, sid):
        return [(json.loads(envelope), json.loads(packet), json.loads(receipt))
                for envelope, packet, receipt in self.s.db.execute("""
                    SELECT i.record_json, d.payload_json, r.record_json
                    FROM invocations i JOIN deliveries d ON i.delivery_id=d.id
                    JOIN invocation_receipts r ON r.invocation_id=i.id
                    WHERE i.session=? ORDER BY i.rowid
                """, (sid,))]

    def repository_receipts(self, sid):
        return self.s.db.execute(
            'SELECT id FROM repository_receipts WHERE session=? ORDER BY seq', (sid,)).fetchall()

    def pause_before_retry(self):
        self.prompt = ('Review the architecture. Explain request flow. Remember project Cedar, ticket CED724. '
                       'Read source only. No network or new dependencies.')
        # Keep the real page partitioner and its coverage checks. Limit fixture
        # pages to one claim so a settled page exists before the final length stop.
        partition = claim_pages.partition

        def split(claims, evidence, fits):
            return partition(claims, evidence, lambda page: len(page['claims']) == 1 and fits(page))

        with patch('claim_pages.partition', side_effect=split), patch.dict('os.environ', SUTRA_THINKING='on'):
            ready = self.until('SYNTHESIS')
            paused = self.step(ready)
        self.assertEqual(paused['state'], 'PAUSED', paused.get('stop'))
        self.assertEqual(paused['stop']['status'], 'claim_review_output_limit')
        self.assertEqual(paused['stop']['resume_phase'], 'SYNTHESIS')
        return paused

    def test_resume_reuses_draft_and_settled_pages_only_retrying_length_stop(self):
        paused = self.pause_before_retry()
        sid = self.meta['active']
        contract = self.s.pinned(sid)
        before = review_budget.usage(self.s, sid)
        execution_id = paused['execution_id']
        execution = orchestration.node(self.s, execution_id)
        execution_spent = orchestration.spent(self.s, execution_id)
        reads = self.repository_receipts(sid)
        calls = self.invocations(sid)
        [(draft, draft_packet, _)] = [call for call in calls if call[0]['role'] == 'chat_reviewer']
        self.assertEqual(draft_packet['request'], self.prompt)
        self.assertIn('CED724', json.dumps(draft_packet['memory']))
        review_calls = [call for call in calls if call[0]['role'] == 'report_reviewer']
        self.assertGreater(len(review_calls), 1)
        self.assertTrue(all(r['status'] == 'accepted' for _, _, r in review_calls[:-1]))
        _, failed_packet, failure = review_calls[-1]
        self.assertEqual(failure['status'], 'quarantined')
        self.assertIsNone(failure['proposal'])
        self.assertIs(failed_packet['reasoning']['requested'], True)
        self.assertGreater(failure['token_usage']['output_tokens'], 0)
        self.assertEqual(failed_packet['candidate_id'], draft['id'])
        self.assertNotIn('checkpoint', self.meta.get('chat', {}))

        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        self.meta = json.loads((self.root / 'workspace.json').read_text())
        with self.assertRaisesRegex(InvalidUpdate, 'user-armed'):
            self.step(paused, event='resume', armed=False)
        self.assertEqual(review_budget.usage(self.s, sid), before)

        # Deliberately leave the explicit On preference in place. The saved
        # recovery must request thinking off for the incomplete page.
        with patch.dict('os.environ', SUTRA_THINKING='on'):
            resumed = self.step(paused, event='resume')
            self.assertEqual(review_budget.usage(self.s, sid), before)
            self.assertEqual(orchestration.spent(self.s, execution_id), execution_spent)
            done = self.until('DONE', resumed)

        self.assertTrue(done['data']['result']['admission']['admitted'], done['data']['result']['admission'])
        self.assertEqual(done['data']['result']['invocation_id'], draft['id'])
        self.assertTrue(done['data']['report_markdown'])
        self.assertEqual(done['binding'], paused['binding'])
        self.assertEqual(done['flow_id'], paused['flow_id'])
        self.assertEqual(done['execution_id'], execution_id)
        self.assertEqual(orchestration.node(self.s, execution_id), execution)
        self.assertEqual(self.s.pinned(sid), contract)
        self.assertFalse(contract['constraints']['edits_allowed'])
        self.assertFalse(contract['constraints']['network_allowed'])

        after_calls = self.invocations(sid)
        self.assertEqual(after_calls[:len(calls)], calls)
        [(retry, retry_packet, retry_receipt)] = after_calls[len(calls):]
        self.assertEqual(retry['role'], 'report_reviewer')
        self.assertEqual(retry_receipt['status'], 'accepted')
        self.assertIs(retry_packet['reasoning']['requested'], False)
        publication = done['data']['report_review']
        self.assertEqual(publication['reviewer_ids'], [inv['id'] for inv, _, _ in review_calls[:-1]] + [retry['id']])
        self.assertTrue(done['data']['result']['accepted_output']['claim_evidence'])
        for field in ('candidate_id', 'review_plan', 'review_page', 'claims',
                      'source_evidence', 'contract', 'constraint_echo'):
            self.assertEqual(retry_packet[field], failed_packet[field], field)
        self.assertEqual(self.repository_receipts(sid), reads)

        after = review_budget.usage(self.s, sid)
        self.assertEqual(after['limits'], before['limits'])
        self.assertEqual(after['allocation_version'], before['allocation_version'])
        self.assertEqual(after['used']['calls'], before['used']['calls'] + 1)
        self.assertEqual(after['used']['operations'], before['used']['operations'])
        self.assertEqual(after['used']['source_bytes'], before['used']['source_bytes'])
        self.assertEqual(after['used']['tokens'] - before['used']['tokens'], settled_tokens(retry, retry_receipt))
        after_execution = orchestration.spent(self.s, execution_id)
        self.assertEqual(after_execution['calls'], execution_spent['calls'] + 1)
        self.assertEqual(after_execution['tokens'] - execution_spent['tokens'], settled_tokens(retry, retry_receipt))
        self.assertTrue(self.s.verify_invocations(sid))
        self.assertTrue(self.s.audit_valid())

    def test_changed_source_blocks_saved_claim_retry_before_dispatch(self):
        paused = self.pause_before_retry()
        sid = self.meta['active']
        reads = self.repository_receipts(sid)
        calls = self.invocations(sid)
        before = review_budget.usage(self.s, sid)
        _, packet, _ = calls[-1]
        source = self.ws / next(iter(packet['source_evidence'].values()))['path']
        source.write_text(source.read_text().replace('return ', 'return 9 * ', 1))

        resumed = self.step(paused, event='resume')
        stopped = self.step(resumed) if resumed['state'] == 'SYNTHESIS' else resumed
        self.assertEqual(stopped['state'], 'PAUSED', stopped.get('stop'))
        self.assertEqual(stopped['stop']['status'], 'provenance_changed')
        self.assertTrue(stopped['recovery']['restart_allowed'])
        self.assertNotIn('checkpoint', self.meta.get('chat', {}))
        self.assertEqual(self.invocations(sid), calls)
        self.assertEqual(self.repository_receipts(sid), reads)
        self.assertEqual(review_budget.usage(self.s, sid), before)

    def test_second_length_stop_settles_final_attempt_without_another_pause(self):
        paused = self.pause_before_retry()
        sid = self.meta['active']
        before = review_budget.usage(self.s, sid)['used']
        calls = self.invocations(sid)
        self.exhaust_bounded_retry = True

        resumed = self.step(paused, event='resume')
        done = self.step(resumed)
        self.assertEqual(done['state'], 'DONE', done.get('stop'))
        self.assertFalse(done['data']['result']['admission']['admitted'])
        self.assertIsNone(done['data']['report_markdown'])
        self.assertIsNone(done['stop'])
        after_calls = self.invocations(sid)
        self.assertEqual(after_calls[:len(calls)], calls)
        [(retry, packet, receipt)] = after_calls[len(calls):]
        self.assertEqual(retry['role'], 'report_reviewer')
        self.assertIs(packet['reasoning']['requested'], False)
        self.assertEqual(receipt['status'], 'quarantined')
        self.assertIsNone(receipt['proposal'])
        after = review_budget.usage(self.s, sid)['used']
        self.assertEqual(after['calls'], before['calls'] + 1)
        self.assertEqual(after['tokens'] - before['tokens'], settled_tokens(retry, receipt))
