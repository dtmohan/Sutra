"""Page coverage, immutable evidence, interruption and cross-model publication."""
import copy
import json
import unittest
from unittest.mock import patch

import claim_pages as pages
import report_review as review
import review_budget
import test_chat
from test_chat import Fake
from test_model_identity import identity
from context_manager import InvalidUpdate, ContextBudgetExceeded
from invocation_gateway import InvocationLedger
from paging import Budget


class PartitionTests(unittest.TestCase):
    def test_all_unicode_and_long_line_source_is_retained(self):
        legacy = {'excerpts': {'a.py': 'λ' * 1500 + '\nlast\n'},
                  'excerpt_locations': {'a.py': {'start_line': 7}}, 'claim_source_version': 2}
        source = dict(legacy, evidence_line_version=1)
        values = pages.inventory('a' * 32, source)
        self.assertEqual(list(pages.inventory('a' * 32, legacy).values())[-1]['end_line'], 9)
        # A legacy source-aware packet still uses its original locations.
        self.assertNotEqual(pages.source_hash(source), pages.source_hash(legacy))
        self.assertEqual(''.join(v['text'] for v in values.values()), source['excerpts']['a.py'])
        self.assertEqual(next(iter(values.values()))['start_line'], 7)
        self.assertEqual(list(values.values())[-1]['end_line'], 8)
        self.assertEqual(values, pages.inventory('a' * 32, source))
        self.assertFalse(set(values) & set(pages.inventory('b' * 32, source)))
        claims = {'one': {'paths': ['a.py']}, 'two': {'paths': ['a.py']}, 'missing': {'paths': ['b.py']}}
        result = pages.partition(claims, values, lambda p: len(p['evidence']) <= 1 and len(p['claims']) <= 1)
        pairs = [(c, e) for p in result for c in p['claims'] for e in p['evidence']]
        self.assertEqual(len(pairs), len(values) * 2)
        self.assertEqual(len(pairs), len(set(pairs)))
        self.assertIn({'claims': ['missing'], 'evidence': []}, result)

    def test_cannot_drop_a_claim_to_fit(self):
        with self.assertRaises(ContextBudgetExceeded):
            pages.partition({'c': {'paths': ['p']}}, {'e': {'path': 'p'}}, lambda p: False)


class ClaimPageTests(unittest.TestCase):
    setUp = test_chat.ChatTests.setUp
    tearDown = test_chat.ChatTests.tearDown
    ask = test_chat.ChatTests.ask

    def candidate(self):
        writer = Fake()
        writer.model_identity = identity(writer.model, 'writer-weights')
        with patch('report_review.run', side_effect=InvalidUpdate('defer checker')):
            result = self.ask('Explain the architecture.', tier='low', provider=writer)
        return result['result']['invocation_id']

    def reviewer(self, name='reviewer', weights='reviewer-weights'):
        model = Fake(name)
        model.model_identity = identity(name, weights)
        # One claim per page under a 128-token output share. The host checks
        # the actual input bound separately, without increasing task budgets.
        model.budget = Budget(6000, 128, 256)
        return model

    def test_paging_coverage_ids_and_model_projection(self):
        from model_packet import build
        cid = self.candidate(); model = self.reviewer()
        result = review.run(self.s, self.sid, self.storage, cid, model, strict=True)
        self.assertTrue(result['admission']['admitted'], result)
        plan = pages.find(self.s, self.sid, cid)
        self.assertGreater(len(plan['pages']), 1)
        draft, _, source = review.candidate(self.s, self.sid, cid)
        catalog = pages.inventory(cid, source)
        pages.validate_plan(plan, cid, draft, source, review.claims(draft['report'], source['excerpts']), catalog)
        broken = copy.deepcopy(plan); broken['pages'].pop()
        with self.assertRaises(InvalidUpdate):
            pages.validate_plan(broken, cid, draft, source, review.claims(draft['report'], source['excerpts']), catalog)
        for payload in model.payloads:
            wire = build(review.SYSTEM, payload, review.schema(payload))
            body = json.loads(wire['messages'][1]['content'])
            self.assertEqual(body['source_evidence'], payload['source_evidence'])
            self.assertIn('Use at most one nonempty channel', wire['messages'][0]['content'])
            self.assertIn('Do not return a verdict', wire['messages'][0]['content'])
            for field in ('contract', 'constraint_echo', 'review_plan', 'reviewer_selection'):
                self.assertNotIn(field, body)
            for eid, entry in payload['source_evidence'].items():
                self.assertEqual(entry, catalog[eid])
                self.assertEqual(entry['text'], source['excerpts'][entry['path']][entry['excerpt_start']:entry['excerpt_end']])
        report, meta = review.published(self.s, self.sid, cid, result['invocation_id'])
        self.assertEqual(meta['review_page_count'], len(plan['pages']))
        self.assertTrue(meta['all_reviewers_distinct'])
        from review_report import markdown
        self.assertIn('file://', markdown(report))
        for entry in report['claim_evidence']:
            self.assertIn('#L' + str(entry['start_line']), markdown(report))
            file_lines = (self.ws / entry['path']).read_text().splitlines()
            excerpt = source['excerpts'][entry['path']]
            start = source['excerpt_locations'][entry['path']]['start_line']
            self.assertEqual(file_lines[start-1], excerpt.splitlines()[0])
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_invalid_ids_exhaust_only_current_page_without_refund_or_restart(self):
        cid = self.candidate(); model = self.reviewer()
        def corrupt(value, p):
            value['claims'][next(iter(value['claims']))]['evidence_ids'] = ['forged']
        model.review_fault = corrupt
        before = review_budget.usage(self.s, self.sid)
        result = review.run(self.s, self.sid, self.storage, cid, model)
        self.assertFalse(result['admission']['admitted'])
        self.assertEqual(len(model.payloads), 2)
        self.assertEqual(len(result['token_usage']['calls']), 2)
        after = review_budget.usage(self.s, self.sid)
        self.assertEqual(after['used']['calls'], before['used']['calls'] + 2)
        self.assertGreater(after['used']['tokens'], before['used']['tokens'])
        with self.assertRaisesRegex(InvalidUpdate, 'repair exhausted'):
            review.run(self.s, self.sid, self.storage, cid, self.reviewer('other'))
        self.assertEqual(after, review_budget.usage(self.s, self.sid))
        with self.assertRaises(InvalidUpdate):
            review.published(self.s, self.sid, cid, result['invocation_id'])

    def test_interruption_reload_and_model_switch_reuse_pages_and_keep_budget(self):
        cid = self.candidate(); model = self.reviewer('first')
        original = self.s.begin_run
        count = 0
        def stop(*a, **kw):
            nonlocal count
            count += 1
            if count == 2: raise InterruptedError('between governed pages')
            return original(*a, **kw)
        contract = self.s.pinned(self.sid)
        with patch.object(self.s, 'begin_run', side_effect=stop), self.assertRaises(InterruptedError):
            review.run(self.s, self.sid, self.storage, cid, model, strict=True)
        plan = pages.find(self.s, self.sid, cid)
        old = review.page_receipts(self.s, self.sid, plan)
        self.assertEqual(len(old), 1)
        with self.assertRaisesRegex(InvalidUpdate, 'incomplete'):
            review.published(self.s, self.sid, cid, next(iter(old.values())))
        before = review_budget.usage(self.s, self.sid)
        self.s.close(); self.s = InvocationLedger(self.storage/'state.sqlite')
        second = self.reviewer('second', 'other-weights')
        final = review.run(self.s, self.sid, self.storage, cid, second, strict=True)
        self.assertEqual(final['review_pages']['reused'], 1)
        self.assertFalse(any(p['review_page'] in old for p in second.payloads))
        self.assertEqual(self.s.pinned(self.sid), contract)
        after = review_budget.usage(self.s, self.sid)
        self.assertEqual(after['limits'], before['limits'])
        self.assertEqual(after['used']['calls'], before['used']['calls'] + len(second.payloads))
        self.assertGreater(after['used']['tokens'], before['used']['tokens'])
        self.assertTrue(all(p['contract'] == model.payloads[0]['contract'] for p in second.payloads))
        report, publication = review.published(self.s, self.sid, cid, final['invocation_id'])
        self.assertEqual({p['reviewer_model'] for p in publication['reviewer_selections']}, {'first', 'second'})
        cached = review.run(self.s, self.sid, self.storage, cid, self.reviewer('third'), strict=True)
        self.assertEqual(cached['token_usage']['total_calls'], 0)
        self.assertEqual(after, review_budget.usage(self.s, self.sid))
        with self.assertRaisesRegex(InvalidUpdate, 'incomplete'):
            review.published(self.s, self.sid, cid, next(iter(old.values())))
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_remote_length_recovery_refines_saved_page_and_reuses_accepted_receipt(self):
        from local_model import OutputLimitExceeded

        cid = self.candidate()
        draft, _, source = review.candidate(self.s, self.sid, cid)
        claims = review.claims(draft['report'], pages.source_paths(source))
        evidence = pages.inventory(cid, source)
        # An older plan may have put multiple claims on a page.
        old = pages.create(self.s, self.sid, cid, draft, source, claims, evidence,
                           lambda page: len(page['evidence']) <= 1, False)
        self.assertGreaterEqual(len(old['pages']), 2)

        class Remote(Fake):
            context_metadata = {'thinking': {'values': [], 'default': None}}

            def call(self, call_id, system, packet, schema):
                proposal, record = super().call(call_id, system, packet, schema)
                if packet['review_plan'] == old['id'] and packet['review_page'] == 1:
                    failure = OutputLimitExceeded('Fixture exhausted a saved multi-claim page')
                    failure.call_record = {'provider': 'sutra_remote', 'response': {
                        'done': True, 'prompt_eval_count': 330, 'eval_count': self.budget.response_reserve}}
                    raise failure
                return proposal, record

        reviewer = Remote('sutra:remote/fixture')
        with patch.dict('os.environ', SUTRA_THINKING='model-default'):
            paused = review.run(self.s, self.sid, self.storage, cid, reviewer)
        stop = paused['claim_review_stop']
        self.assertEqual(stop['status'], 'claim_review_output_limit')
        self.assertEqual(stop['review_page'], 2)
        first = review.page_receipts(self.s, self.sid, old)
        self.assertEqual(set(first), {0})
        before = review_budget.usage(self.s, self.sid)
        pinned = self.s.pinned(self.sid)
        with patch.dict('os.environ', SUTRA_THINKING='model-default'), self.assertRaises(InterruptedError):
            with patch.object(self.s, 'begin_run', side_effect=InterruptedError('after refinement commit')):
                review.run(self.s, self.sid, self.storage, cid, reviewer, retry=stop)
        refinement = pages.find(self.s, self.sid, cid)
        self.assertNotEqual(refinement['id'], old['id'])
        self.assertEqual(refinement['refinement']['failed_invocation'], stop['failed_invocation'])
        self.assertEqual(review_budget.usage(self.s, self.sid), before)
        self.assertEqual(review.page_receipts(self.s, self.sid, refinement), first)
        self.s.close()
        self.s = InvocationLedger(self.storage/'state.sqlite')
        resumed = Remote(reviewer.model)
        with patch.dict('os.environ', SUTRA_THINKING='model-default'):
            final = review.run(self.s, self.sid, self.storage, cid, resumed, retry=stop)
        report, publication = review.published(self.s, self.sid, cid, final['invocation_id'])
        self.assertEqual(publication['total'], len(claims))
        self.assertEqual(set(publication['claim_reviews']), set(claims))
        self.assertEqual(publication['reviewer_ids'][0], first[0])
        self.assertEqual(final['review_pages']['reused'], 1)
        self.assertEqual(final['review_pages']['plan_id'], refinement['id'])
        self.assertTrue(report['report']['overview'])
        self.assertTrue(all(p['review_page'] != 0 for p in resumed.payloads))
        self.assertTrue(all(p['reasoning']['requested'] is None for p in resumed.payloads))
        self.assertEqual(self.s.pinned(self.sid), pinned)
        after = review_budget.usage(self.s, self.sid)
        self.assertEqual(after['limits'], before['limits'])
        self.assertEqual(after['allocation_version'], before['allocation_version'])
        self.assertEqual(after['used']['source_bytes'], before['used']['source_bytes'])
        self.assertEqual(after['used']['calls'], before['used']['calls'] + len(resumed.payloads))
        self.assertTrue(self.s.verify_invocations(self.sid))
        self.assertTrue(self.s.audit_valid())

    def test_chat_retry_reuses_saved_draft_and_unfinished_review(self):
        import chat_runtime
        from test_chat import MODELS
        original = self.s.begin_run
        reviews = 0
        def stop(*a, **kw):
            nonlocal reviews
            if 'source evidence' in a[1]:
                reviews += 1
                if reviews == 2: raise InterruptedError('bridge interrupted between pages')
            return original(*a, **kw)
        request = {'prompt': 'Explain the architecture.', 'models': MODELS, 'tier': 'low'}
        writer = Fake()
        with patch.object(self.s, 'begin_run', side_effect=stop), self.assertRaises(InterruptedError):
            chat_runtime.answer(self.s, self.meta, self.ws, self.storage, request, writer, self.reviewer())
        before = review_budget.usage(self.s, self.sid)
        self.s.close(); self.s = InvocationLedger(self.storage/'state.sqlite')
        new_writer = Fake(MODELS['high'])
        reviewer = self.reviewer('after-restart')
        result = chat_runtime.answer(self.s, self.meta, self.ws, self.storage,
                                     dict(request, tier='high'), new_writer, reviewer)
        self.assertTrue(result['result']['admission']['admitted'], result)
        self.assertEqual(new_writer.payloads, [])
        self.assertFalse(any(p['review_page'] == 0 for p in reviewer.payloads))
        after = review_budget.usage(self.s, self.sid)
        self.assertEqual(after['limits'], before['limits'])
        self.assertEqual(after['used']['calls'], before['used']['calls'] + len(reviewer.payloads))
        self.assertIsNotNone(result['report_markdown'])

    def test_foreign_claim_ids_and_tampered_source_are_rejected(self):
        cid = self.candidate(); model = self.reviewer(); model.budget = Fake.budget
        result = review.run(self.s, self.sid, self.storage, cid, model)
        p = model.payloads[0]
        altered = copy.deepcopy(p)
        eid = next(iter(altered['source_evidence']))
        altered['source_evidence'][eid]['text'] = 'fabricated source'
        with self.assertRaisesRegex(InvalidUpdate, 'binding changed'):
            review.request_check(self.s, self.sid, altered)
        checked = 0
        for payload in model.payloads:
            for key, claim in payload['claims'].items():
                foreign = next((e for e, item in payload['source_evidence'].items() if item['path'] not in claim['paths']), None)
                if foreign:
                    value = copy.deepcopy(self.s.load_result(self.sid, result['invocation_id']))
                    value['claims'][key]['evidence_ids'] = [foreign]
                    with self.assertRaisesRegex(InvalidUpdate, 'claim page scope'):
                        review.validate(self.s, self.sid, payload, value)
                    checked += 1
        self.assertGreater(checked, 0)
        (self.ws/'leaf.py').write_text('def run(): return 999\n')
        with self.assertRaises(InvalidUpdate):
            review.published(self.s, self.sid, cid, result['invocation_id'])

    def test_new_request_after_completed_plan_survives_cold_ledger_commits(self):
        cid = self.candidate(); model = self.reviewer()
        review.run(self.s, self.sid, self.storage, cid, model)
        self.s.close(); self.s = InvocationLedger(self.storage/'state.sqlite')
        _, _, source = review.candidate(self.s, self.sid, cid)
        new_request = dict(source, request='A different architecture question.')
        original = review.candidate
        def restore(*a):
            result = original(*a)
            with self.s.db: self.s._audit('TEST_RESTORE', {'session': self.sid})
            return result
        with patch.object(review, 'candidate', side_effect=restore):
            self.assertIsNone(review.pending_candidate(self.s, self.sid, new_request))

    def test_contradiction_on_later_page_vetoes_earlier_support(self):
        cid = self.candidate(); model = self.reviewer()
        original = pages.partition
        def split(claims, evidence, fits):
            return original(claims, evidence, lambda p: len(p['evidence']) <= 1 and fits(p))
        def contradictory(value, p):
            if any(e['path'] == 'leaf.py' for e in p['source_evidence'].values()) and 'overview' in value['claims']:
                value['claims']['overview']['verdict'] = 'contradicted'
        model.review_fault = contradictory
        with patch('claim_pages.partition', side_effect=split):
            result = review.run(self.s, self.sid, self.storage, cid, model)
        _, meta = review.published(self.s, self.sid, cid, result['invocation_id'])
        self.assertTrue(any(c['claim'] == 'overview' and c['verdict'] == 'contradicted' for c in meta['omitted']))


if __name__ == '__main__': unittest.main()
