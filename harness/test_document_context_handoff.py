"""Document extraction keeps conversational criteria separate from source evidence."""
import copy
import json
import unittest

import conversation
import document_stages as stages
from context_manager import InvalidUpdate, canonical
from invocation_gateway import InvocationLedger
from model_packet import build
from paging import Budget
from review_budget import usage
from test_conversation import Fake, MODELS
import test_document_stages as fixtures


class DocumentContextHandoffTests(unittest.TestCase):
    chat_key = 'a' * 64
    path = fixtures.DocumentStagesTests.path
    prompt = 'Review rollout-register.docx using the criteria above, with citations.'
    provider = fixtures.DocumentStagesTests.provider
    approved = fixtures.DocumentStagesTests.approved
    next_capture = fixtures.DocumentStagesTests.next_capture
    saved = fixtures.DocumentStagesTests.saved
    drive = fixtures.DocumentStagesTests.drive

    def setUp(self):
        fixtures.DocumentStagesTests.setUp(self)
        self.criteria = 'Keep the focus on signed approvals. Never treat an unresolved task as permission to deploy.'
        reply = conversation.answer(self.ledger, self.metadata, self.storage,
            {'prompt': self.criteria, 'models': MODELS, 'tier': 'low'}, Fake(),
            axis=conversation.chat_axis(self.workspace, self.chat_key))
        self.parent = reply['result']['invocation_id']
        self.sid = reply['session']

    def request(self, event, flow=None, **fields):
        if event == 'start':
            fields.setdefault('parent', self.parent)
        return fixtures.DocumentStagesTests.request(self, event, flow, **fields)

    def test_long_document_extractor_gets_prior_criteria_and_preserves_exact_sections(self):
        flow, sid, _ = self.approved()
        self.assertEqual(sid, self.sid)
        flow = self.next_capture(flow)
        packet = next(p for role, _, p in self.calls if role == 'document_extract')
        self.assertEqual(packet['retained_conversation_parent'], self.parent)
        self.assertIn(self.criteria, canonical(packet['memory']['pinned_user_statements']))
        self.assertIn(self.criteria, canonical(packet['memory']['recent_turns']))
        self.assertIn(packet['section_text'], self.document_text)
        wire = build(stages.SYSTEM, packet, stages.schema(packet))
        self.assertIn(self.criteria, canonical(wire['messages']))
        self.assertEqual(packet['section_text'], json.loads(wire['messages'][-1]['content'])['section_text'])
        from memory_layer import _wire_refs
        refs = _wire_refs(packet, {'role': stages.ROLE, 'model_interface': 1})
        self.assertTrue(any(key == 'wire:1' for key, _, _ in refs))
        self.assertTrue(any(key.endswith('/conversation_criteria_unverified') for key, _, _ in refs))
        host_only = copy.deepcopy(packet)
        host_only['memory']['host_private_not_delivered'] = 'Not part of the model context.'
        self.assertEqual(_wire_refs(host_only, {'role': stages.ROLE, 'model_interface': 1}), refs)
        receipt = self.saved(flow)['document_stage_receipts'][0]
        self.assertTrue(all(note['quote'] in packet['section_text']
                            for note in self.ledger.load_result(sid, receipt)['notes']))
        self.assertTrue(self.ledger.verify_invocations(sid))

    def test_reload_reuses_accepted_section_and_never_refunds_prior_conversation_or_extraction(self):
        flow, sid, _ = self.approved()
        flow = self.next_capture(flow)
        receipts = self.saved(flow)['document_stage_receipts'][:]
        before = usage(self.ledger, sid)
        self.ledger.close()
        self.ledger = InvocationLedger(self.ledger_path)
        self.assertEqual(usage(self.ledger, sid), before)
        flow = self.next_capture(flow)
        self.assertEqual(self.saved(flow)['document_stage_receipts'][:len(receipts)], receipts)
        captures = [p for role, _, p in self.calls if role == 'document_extract']
        self.assertEqual(len({p['section']['id'] for p in captures}), len(captures))
        self.assertTrue(all(self.criteria in canonical(p['memory']) for p in captures))
        self.assertEqual(usage(self.ledger, sid)['used']['calls'], before['used']['calls'] + 1)

    def test_conversation_criteria_cannot_be_cited_as_document_source(self):
        flow, sid, _ = self.approved()
        flow = self.next_capture(flow)
        packet = next(p for role, _, p in self.calls if role == 'document_extract')
        self.assertNotIn(self.criteria, packet['section_text'])
        with self.assertRaisesRegex(InvalidUpdate, 'exact quotation'):
            stages._check_notes(packet['section_text'], {'notes': [{'fact': 'An approval requirement.',
                                                                    'quote': self.criteria}]})
        forged = copy.deepcopy(packet)
        forged['retained_conversation_parent'] = '0' * 32
        saved, raw, _ = stages._committed(self.ledger, sid, flow['flow_id'])
        bad = dict(saved, parent=forged['retained_conversation_parent'])
        with self.assertRaisesRegex(InvalidUpdate, 'another conversation'):
            stages._delivery(self.ledger, sid, flow['flow_id'], bad, raw, saved['sections'][0],
                             budget=Budget(**packet['memory_budget']))

    def test_unmarked_legacy_plan_keeps_original_stateless_delivery(self):
        flow, sid, _ = self.approved()
        flow = self.next_capture(flow)
        saved, raw, _ = stages._committed(self.ledger, sid, flow['flow_id'])
        legacy = {k: v for k, v in saved.items() if k != 'extraction_context_version'}
        packet = stages._delivery(self.ledger, sid, flow['flow_id'], legacy, raw, legacy['sections'][0])
        self.assertNotIn('memory', packet)
        self.assertNotIn('memory_budget', packet)
        self.assertNotIn('retained_conversation_parent', packet)


if __name__ == '__main__':
    unittest.main()
