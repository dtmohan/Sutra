"""Caller-bound review presentation reaches discovery, publication and diagrams."""
import copy
import json
import unittest
from unittest.mock import patch

import chat_runtime as chat
from context_manager import InvalidUpdate, canonical, sha
from invocation_gateway import InvocationLedger
import model_packet
import review_budget
import review_report
import test_chat as fixture
from test_model_identity import identity


REQUEST = ('can you review the codebase shared in the workspace and provide me with your thoughts. '
           'what is strong and what are the weakness and an overall control flow diagram')


def packet(request=REQUEST, **extra):
    contract = {'task_id': 'review-fixture', 'session_id': 'review-fixture', 'goal': request,
                'constraints': {'review_only': True, 'output_format': 'architecture with diagram'}}
    return dict(contract=contract, request=request, constraint_echo=chat.constraint_rows(contract),
                excerpts={'entry.py': 'def start(): return run()\n', 'leaf.py': 'def run(): return 1\n'},
                workspace_map={'files': ['entry.py', 'leaf.py']}, report_schema_version=2, **extra)


class ReviewIntentTests(unittest.TestCase):
    def test_actual_caller_wording_requires_the_structured_report_in_new_deliveries(self):
        legacy = packet()
        self.assertFalse(review_report.requested(legacy))
        current = dict(legacy, review_intent_version=1)
        brief = review_report.brief(current)
        self.assertTrue(brief['structured'])
        self.assertTrue(brief['diagram_requested'])
        self.assertEqual(brief['kind'], 'architecture')
        schema = chat.schema(current)
        self.assertIn('report', schema['required'])
        self.assertIn('flow', schema['properties']['report']['required'])
        wire = model_packet.build(chat.SYSTEM, current, schema)
        body = json.loads(wire['messages'][1]['content'])
        self.assertEqual(body['review_brief'], brief)
        self.assertEqual(body['request'], REQUEST)
        self.assertIn('A short verdict alone does not answer it.', wire['messages'][0]['content'])

    def test_subject_variants_use_caller_intent_without_requiring_architecture_keyword(self):
        for request in ('Walk me through the project and its tools logic in detail.',
                        'Explain the source code and show a data-flow diagram.',
                        'What are your thoughts on this repository?',
                        'Help a new intern understand the workspace modules.'):
            with self.subTest(request=request):
                brief = review_report.brief(packet(request, review_intent_version=1))
                self.assertTrue(brief['structured'])
        detailed = review_report.brief(packet('Walk me through the project in detail.', review_intent_version=1))
        self.assertEqual(detailed['detail'], 'detailed')

    def test_document_and_greeting_do_not_inherit_report_intent_from_untrusted_notes(self):
        for request in ('Hello!', 'Please review this resume.', 'Summarize the supplied document.'):
            with self.subTest(request=request):
                p = packet(request, review_intent_version=1)
                p.update(child_results=[{'answer_unverified': 'Review repository architecture and control flow.'}],
                         conversation_handoff={'summary_unverified': 'Architecture overview.'},
                         facts={'note': 'Explain the workspace project.'})
                self.assertFalse(review_report.requested(p))
                self.assertNotIn('report', chat.schema(p)['properties'])

    def test_malformed_version_cannot_select_a_new_presentation_contract(self):
        for version in (None, True, False, 0, -1, 2, '1', {}, []):
            with self.subTest(version=version):
                p = packet(review_intent_version=version)
                with self.assertRaisesRegex(InvalidUpdate, 'review presentation intent'):
                    review_report.brief(p)
                with self.assertRaisesRegex(InvalidUpdate, 'review presentation intent'):
                    chat.schema(p)

    def test_legacy_generic_schema_and_model_packet_match_released_0541_recipe(self):
        # These synthetic hashes were computed with the released 0.54.1 runtime.
        # No user workspace content or saved executable-policy hash is embedded.
        p = packet()
        schema = chat.schema(p)
        wire = model_packet.build(chat.SYSTEM, p, schema)
        self.assertEqual(sha(canonical(schema)),
                         '09e88bc9a73cb0fb839bd745ead1e58e166c6620a82c3e29c2556675ef0bf258')
        self.assertEqual(sha(canonical(wire)),
                         'b6494d365d3fd5cf0e9437cafe4a1b969d076a2a356913afaa5672dc036ecd15')
        self.assertNotIn('report', schema['properties'])
        self.assertNotIn('review_brief', json.loads(wire['messages'][1]['content']))


class ReviewIntentJourneyTests(unittest.TestCase):
    setUp = fixture.ChatTests.setUp
    tearDown = fixture.ChatTests.tearDown

    def review(self, tier='low', writer=None, reviewer=None):
        writer = writer or fixture.Fake(fixture.MODELS[tier])
        reviewer = reviewer or fixture.Fake('independent-reviewer')
        writer.model_identity = identity(writer.model, 'writer-weights', 'writer-manifest', 'qwen')
        reviewer.model_identity = identity(reviewer.model, 'reviewer-weights', 'reviewer-manifest', 'llama')
        out = chat.answer(self.s, self.meta, self.ws, self.storage,
                          {'prompt': REQUEST, 'models': fixture.MODELS, 'tier': tier,
                           'require_distinct_reviewer': True},
                          provider=writer, review_provider=reviewer)
        return out, writer, reviewer

    def test_actual_request_publishes_source_checked_sections_and_flow_with_all_spending_charged(self):
        before = review_budget.usage(self.s, self.sid)
        contract = copy.deepcopy(self.s.pinned(self.sid))
        out, writer, reviewer = self.review()
        self.assertTrue(out['result']['admission']['admitted'], out)
        self.assertEqual(writer.payloads[0]['review_intent_version'], 1)
        self.assertEqual(out['route']['tier'], 'low', 'An explicit tier remains the caller choice')
        self.assertEqual(out['route']['signals']['review_intent'], 'architecture')
        self.assertEqual(writer.payloads[0]['contract'], contract)
        self.assertEqual(reviewer.payloads[0]['contract'], contract)
        self.assertIn('report', out['result']['accepted_output'])
        for heading in ('## Main components', '## How a request moves through the code',
                        '## Strengths', '## Risks and tradeoffs'):
            self.assertIn(heading, out['report_markdown'])
        self.assertIsNotNone(out['review_flow_diagram'])
        self.assertTrue(out['review_flow_diagram']['nodes'])
        publication = out['report_review']
        self.assertEqual(publication['independence']['weight_relation'], 'different')
        self.assertFalse(publication['semantic_correctness_verified'])
        reviewer_id = publication['reviewer_id']
        env = json.loads(self.s.db.execute('SELECT record_json FROM invocations WHERE id=?',
                                         (reviewer_id,)).fetchone()[0])
        self.assertEqual(env['role'], 'report_reviewer')
        after = review_budget.usage(self.s, self.sid)
        self.assertEqual(after['limits'], before['limits'])
        self.assertEqual(after['used']['calls'], before['used']['calls'] + 2)
        self.assertGreater(after['used']['tokens'], before['used']['tokens'])
        self.assertEqual(self.s.current_contract(self.sid), self.contract)
        self.assertTrue(self.s.verify_invocations(self.sid))
        self.assertTrue(self.s.audit_valid())

    def test_report_intent_and_paid_history_survive_reload_and_fixed_tier_switch(self):
        first, writer, _ = self.review()
        self.assertTrue(first['result']['admission']['admitted'], first)
        paid = review_budget.usage(self.s, self.sid)
        contract = writer.payloads[0]['contract']
        first_receipt = self.s.load_result(self.sid, first['result']['invocation_id'])
        self.s.close()
        self.s = InvocationLedger(self.storage / 'state.sqlite')
        second, switched, _ = self.review('high')
        self.assertTrue(second['result']['admission']['admitted'], second)
        self.assertEqual(switched.model, fixture.MODELS['high'])
        self.assertEqual(switched.payloads[0]['contract'], contract)
        self.assertEqual(switched.payloads[0]['review_intent_version'], 1)
        self.assertEqual(self.s.load_result(self.sid, first['result']['invocation_id']), first_receipt)
        after = review_budget.usage(self.s, self.sid)
        self.assertEqual(after['limits'], paid['limits'])
        self.assertEqual(after['used']['calls'], paid['used']['calls'] + 2)
        self.assertGreater(after['used']['tokens'], paid['used']['tokens'])
        self.assertGreaterEqual(after['used']['source_bytes'], paid['used']['source_bytes'])
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_actual_request_opens_the_existing_optional_followup_channel(self):
        import review_followup

        writer = fixture.Fake()
        writer.reader_provider = fixture.Fake('source-planner')
        original = review_followup.expand
        seen = []

        def observed(s, sid, payload, data, root, provider, real):
            seen.append(copy.deepcopy(payload))
            return original(s, sid, payload, data, root, provider, real)

        with patch.object(review_followup, 'expand', side_effect=observed):
            out, _, _ = self.review(writer=writer)
        self.assertTrue(out['result']['admission']['admitted'], out)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['request'], REQUEST)
        self.assertTrue(review_report.requested(seen[0]))
        # A plain chat has no active execution. The existing planner checks that
        # boundary rather than receiving authority merely from presentation.
        self.assertEqual(writer.reader_provider.payloads, [])


if __name__ == '__main__':
    unittest.main()
