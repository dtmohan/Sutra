"""Offline regressions for approved, receipt-backed document stage routing."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.sax.saxutils import escape

import conversation
import orchestration
import orchestration_flow
from context_manager import InvalidUpdate
from invocation_gateway import InvocationLedger, consume_dispatch
from paging import Budget, PageFault
from repository_tools import read_bytes
from review_budget import usage
from test_conversation import MODELS
from test_document_text import docx, word_xml


UNSUPPORTED = 'The rollout registers authorize all deployments at Site Z.'


class StageProvider:
    context_metadata = {'model_info': {'general.architecture': 'qwen', 'qwen.context_length': 65536}}

    def __init__(self, suite, role, model):
        self.suite = suite
        self.role = role
        self.model = model
        self.budget = Budget(32768, 4096, 256)

    def call(self, cid, system, payload, schema):
        consume_dispatch(cid, self.model, system, payload, schema)
        self.suite.calls.append((self.role, self.model, copy.deepcopy(payload)))
        if self.role == 'document_extract':
            if self.suite.omit_notes:
                return {'notes': []}, {'ok': True}
            quote = (self.suite.document_text.splitlines()[0][:180] if self.suite.forge_quote
                     else next(line for line in payload['section_text'].splitlines() if len(line.strip()) >= 8)[:180])
            if self.suite.forge_quote:
                self.suite.assertNotIn(quote, payload['section_text'])
            notes = [{'fact': 'The register records a checkpoint.', 'quote': quote}]
            if self.suite.duplicate_quotes:
                notes.append({'fact': 'A second note uses the same source.', 'quote': quote})
            return {'notes': notes}, {'ok': True}

        if self.role == 'document_verdict_reviewer':
            verdict = self.suite.review_verdict
            if verdict is None:
                verdict = 'synthesis_error' if UNSUPPORTED in payload['answer'] else 'supported'
            self.suite.review_verdicts.append(verdict)
            draft = next(packet for role, _, packet in reversed(self.suite.calls) if role == 'conversation')
            self.suite.assertEqual(payload['quotes'], {
                item['id']: item['quote'] for item in draft['document_synthesis']['evidence']
            })
            return {
                'verdict': verdict,
                'reason': {
                    'synthesis_error': 'The cited register does not grant deployment authorization at Site Z.',
                    'insufficient_evidence': 'The register does not give an approved rollout status.',
                    'supported': 'The cited register states how the warehouse rollout is checked.',
                }[verdict],
                'evidence_ids': payload['citation_ids'][:1] if verdict != 'insufficient_evidence' else [],
            }, {'ok': True}

        evidence = payload.get('document_synthesis', {}).get('evidence', [])
        citations = ' '.join('[' + item['id'] + ']' for item in evidence)
        reply = {'intent': 'conversation', 'answer': ('The rollout registers track checkpoints. ' + citations).strip(),
                 'summary': 'Reviewed the rollout registers.', 'escalation_reason': 'none',
                 'page_requests': [], 'diagram': None}
        if 'document_synthesis' in payload:
            reply['citation_ids'] = [item['id'] for item in evidence]
            if self.model in self.suite.synthesis_errors:
                reply['answer'] = (UNSUPPORTED + ' ' + citations).strip()
            if 'basis' in schema['properties']:
                reply['basis'] = f'An approved reviewer checks the rollout before changing status. [{evidence[0]["id"]}]'
        return reply, {'ok': True}


class DocumentStagesTests(unittest.TestCase):
    chat_key = 'a' * 64
    path = 'rollout-register.docx'
    prompt = 'Review rollout-register.docx and summarize the full rollout evidence, with citations.'

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.workspace = root / 'workspace'
        self.workspace.mkdir()
        self.storage = root / 'storage'
        self.storage.mkdir()
        self.ledger_path = self.storage / 'state.sqlite'
        self.ledger = InvocationLedger(self.ledger_path)
        self.addCleanup(lambda: self.ledger.close())
        self.metadata = {'workspace': str(self.workspace)}
        self.calls = []
        self.forge_quote = False
        self.omit_notes = False
        self.duplicate_quotes = False
        self.tier = 'auto'
        self.final_model = MODELS['low']
        self.synthesis_errors = set()
        self.review_verdict = None
        self.review_verdicts = []

    def provider(self, role):
        self.assertIn(role, ('conversation', 'document_extract', 'document_verdict_reviewer'))
        return {model: StageProvider(self, role, model) for model in MODELS.values()}

    def request(self, event, flow=None, **fields):
        request = {'event': event, 'chat_key': self.chat_key, 'models': MODELS,
                   'tier': self.tier, 'armed': False, **fields}
        if flow:
            request.setdefault('flow_id', flow['flow_id'])
            request.setdefault('cursor', flow['cursor'])
        return orchestration_flow.handle(self.ledger, self.metadata, self.workspace,
                                         self.storage, request, self.provider)

    def approved(self, *, long=True):
        paragraphs = [
            f'Register entry {n:03d}: The approved reviewer checks the warehouse rollout before changing status. '
            'The signed log retains its references and records unresolved tasks for the next shift.'
            for n in range(1, 193)
        ] if long else ['The approved rollout starts on Tuesday.']
        self.document_text = '\n'.join(paragraphs)
        xml = ''.join(f'<w:p><w:r><w:t>{escape(line)}</w:t></w:r></w:p>' for line in paragraphs)
        raw = docx(word_xml(xml))
        (self.workspace / self.path).write_bytes(raw)
        started = self.request('start', prompt=self.prompt)
        self.assertEqual(started['stop']['evidence_bridge']['required'], 'document_file')
        located = self.request('locate_document', started)
        self.assertEqual(located['stop']['document_lookup']['status'], 'found')
        with self.assertRaises(InvalidUpdate):
            self.request('approve_document', located, document_path=self.path, confirmed=False)
        self.assertEqual(self.calls, [])
        ready = self.request('approve_document', located, document_path=self.path, confirmed=True)
        self.assertEqual(ready['state'], 'CLASSIFYING')
        self.assertEqual(self.calls, [])
        _, data = orchestration_flow.info(self.ledger, ready['flow_id'])
        return ready, data['conversation_session'], len(raw)

    def next_capture(self, flow):
        for _ in range(4):
            captures = self.saved(flow).get('document_stage_receipts', [])
            flow = self.request('step', flow)
            if len(self.saved(flow).get('document_stage_receipts', [])) > len(captures):
                return flow
        self.fail('Document workflow did not commit an extraction receipt.')

    def saved(self, flow):
        return orchestration_flow.info(self.ledger, flow['flow_id'])[1]

    def drive(self, flow):
        for _ in range(30):
            if flow['state'] != 'CLASSIFYING':
                return flow
            flow = self.request('step', flow)
        self.fail('Document workflow did not reach a bounded terminal or paused state.')

    def interrupt_before(self, flow, event):
        move = orchestration_flow.move

        def interrupt(s, fid, target, kind, **data):
            if kind == event:
                raise RuntimeError(f'Injected interruption before {event}.')
            return move(s, fid, target, kind, **data)

        with patch('orchestration_flow.move', side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError, f'Injected interruption before {event}'):
                for _ in range(20):
                    flow = self.request('step', flow)
                    self.assertEqual(flow['state'], 'CLASSIFYING')
        return flow

    def assert_completed(self, flow, sid, original_bytes, synthesis_models=None):
        self.assertEqual(flow['state'], 'DONE')
        final = flow['conversation']
        self.assertEqual(final['session'], sid)
        self.assertTrue(final['result']['admission']['admitted'], final['result']['admission'])
        self.assertEqual(final['route']['model'], self.final_model)
        rendered = conversation.read(self.ledger, flow['conversation_id'])
        self.assertEqual(rendered['session'], sid)
        self.assertTrue(rendered['document_citations'])
        extracted = self.saved(flow)['document_stage_receipts']
        self.assertGreater(len(extracted), 1)
        captures = [packet for role, _, packet in self.calls if role == 'document_extract']
        self.assertTrue(all(packet['section_text'] in self.document_text for packet in captures))
        self.assertTrue(all(packet['section_text'] != self.document_text for packet in captures))
        by_section = {packet['section']['id']: packet['section_text'] for packet in captures}
        self.assertEqual(len(by_section), len(extracted))
        recovered_lines = [line for section in by_section.values() for line in section.splitlines() if line]
        self.assertEqual(recovered_lines, self.document_text.splitlines())
        syntheses = [(model, packet) for role, model, packet in self.calls if role == 'conversation']
        self.assertEqual([model for model, _ in syntheses], synthesis_models or [self.final_model])
        final_packet = syntheses[-1][1]
        synthesis = final_packet['document_synthesis']
        self.assertTrue(all(packet['document_synthesis'] == synthesis for _, packet in syntheses))
        self.assertEqual(len(synthesis['sections']), len(extracted))
        self.assertEqual(len(synthesis['evidence']), len(extracted))
        self.assertFalse(synthesis['missing_sections'])
        wire = conversation.ConversationModel(self.storage / 'calls').messages(conversation.SYSTEM, final_packet)
        proposed_quotes = [note['quote'] for note in synthesis['evidence']]
        unused_line = next(line for line in self.document_text.splitlines()
                           if not any(quote in line for quote in proposed_quotes))
        self.assertTrue(all(unused_line[:35] not in message['content'] for message in wire))
        self.assertTrue(all(note['quote'] in self.document_text for note in synthesis['evidence']))
        rejected = dict(final['result']['accepted_output'], answer='A fabricated citation [D999-E999].')
        with self.assertRaisesRegex(InvalidUpdate, 'document evidence IDs'):
            conversation.check_delivery(final_packet, rejected)
        cost = usage(self.ledger, sid)['used']
        self.assertEqual(cost['operations'], 1)
        self.assertEqual(cost['source_bytes'], original_bytes)
        self.assertEqual(cost['calls'], len(self.calls))
        self.assertGreater(cost['tokens'], 0)
        self.assertTrue(self.ledger.verify_invocations(sid))
        return extracted

    def test_auto_reuses_extraction_after_reopening_and_keeps_supported_low(self):
        with patch('document_source.read_bytes', wraps=read_bytes) as source_read:
            flow, sid, original_bytes = self.approved()
            flow = self.request('step', flow)
            self.assertIn('document_stage_plan', self.saved(flow))
            record_stage = orchestration_flow.move

            def stop_before_checkpoint(s, fid, target, kind, **data):
                if kind == 'DOCUMENT_STAGE_EXTRACTED':
                    raise RuntimeError('Injected interruption after model admission.')
                return record_stage(s, fid, target, kind, **data)

            with patch('orchestration_flow.move', side_effect=stop_before_checkpoint):
                with self.assertRaisesRegex(RuntimeError, 'after model admission'):
                    self.request('step', flow)
            self.assertFalse(self.saved(flow).get('document_stage_receipts'))
            first, = [row[0] for row in self.ledger.db.execute(
                'SELECT id FROM invocations WHERE session=?', (sid,))]
            before_reopen = usage(self.ledger, sid)
            self.assertEqual(before_reopen['used']['calls'], 1)
            original_contract = conversation.contract(self.ledger, sid)
            self.ledger.close()
            self.ledger = InvocationLedger(self.ledger_path)
            self.assertEqual(usage(self.ledger, sid), before_reopen)
            self.assertEqual(conversation.contract(self.ledger, sid), original_contract)
            (self.workspace / self.path).unlink()
            with self.assertRaises(InvalidUpdate):
                self.request('step', flow, chat_key='b' * 64)
            self.assertEqual(usage(self.ledger, sid), before_reopen)
            flow = self.next_capture(flow)
            self.assertEqual(self.saved(flow)['document_stage_receipts'], [first])
            self.assertEqual(usage(self.ledger, sid), before_reopen)
            flow = self.drive(flow)
            source_read.assert_called_once()

        self.assertEqual(self.assert_completed(flow, sid, original_bytes)[0], first)
        self.assertTrue(all(model == MODELS['low'] for role, model, _ in self.calls if role == 'document_extract'))
        self.assertEqual(self.review_verdicts, ['supported'])
        self.assertEqual(conversation.contract(self.ledger, sid), original_contract)

    def test_fixed_low_runs_both_stages_on_low(self):
        self.tier = 'low'
        self.final_model = MODELS['low']
        flow, sid, original_bytes = self.approved()
        self.assert_completed(self.drive(flow), sid, original_bytes)
        self.assertTrue(all(model == MODELS['low'] for _, model, _ in self.calls))

    def test_duplicate_exact_quotes_do_not_stop_extraction(self):
        self.duplicate_quotes = True
        flow, sid, original_bytes = self.approved()
        captures = self.assert_completed(self.drive(flow), sid, original_bytes)
        self.assertTrue(all(len(self.ledger.load_result(sid, iid)['notes']) == 2 for iid in captures))
        self.assertEqual(self.review_verdicts, ['supported'])

    def test_completed_verdict_remains_available_until_its_reviewer_is_quarantined(self):
        flow, sid, original_bytes = self.approved()
        final = self.drive(flow)
        self.assert_completed(final, sid, original_bytes)
        before = usage(self.ledger, sid)
        recovered = self.request('inspect', final)['conversation']
        self.assertEqual(recovered['result']['invocation_id'], final['conversation_id'])
        self.assertIn('basis', recovered['result']['accepted_output'])
        self.assertEqual(usage(self.ledger, sid), before)
        self.ledger.quarantine_invocation(sid, recovered['document_verification']['invocation_id'], 'Test quarantine')
        with self.assertRaises(PageFault):
            conversation.read(self.ledger, final['conversation_id'])
        with self.assertRaises(PageFault):
            conversation.session_for(self.ledger, {'prompt': 'Continue', 'parent': final['conversation_id']})

    def test_auto_rejects_a_cited_but_unsupported_draft_and_reuses_evidence(self):
        self.synthesis_errors = {MODELS['low']}
        self.final_model = MODELS['mid']
        with patch('document_source.read_bytes', wraps=read_bytes) as source_read:
            flow, sid, original_bytes = self.approved()
            original_contract = conversation.contract(self.ledger, sid)
            for _ in range(20):
                flow = self.request('step', flow)
                if self.saved(flow).get('document_candidate_id'):
                    break
                self.assertEqual(flow['state'], 'CLASSIFYING')
            candidate = self.saved(flow)['document_candidate_id']
            receipts = self.saved(flow)['document_stage_receipts']
            self.assertNotIn('conversation', flow)
            self.assertIsNone(flow['conversation_id'])
            with self.assertRaises((InvalidUpdate, PageFault)):
                conversation.read(self.ledger, candidate)
            with self.assertRaises(PageFault):
                conversation.session_for(self.ledger, {'prompt': 'Continue', 'parent': candidate})
            self.assertEqual(self.review_verdicts, [])
            pending_usage = usage(self.ledger, sid)
            self.assertEqual(pending_usage['used']['calls'], len(receipts) + 1)

            self.ledger.close()
            self.ledger = InvocationLedger(self.ledger_path)
            self.assertEqual(usage(self.ledger, sid), pending_usage)
            final = self.drive(flow)
            source_read.assert_called_once()

        self.assertEqual(self.assert_completed(final, sid, original_bytes, [MODELS['low'], MODELS['mid']]), receipts)
        self.assertEqual(self.review_verdicts, ['synthesis_error', 'supported'])
        self.assertEqual([model for role, model, _ in self.calls if role == 'document_verdict_reviewer'],
                         [MODELS['mid'], MODELS['low']])
        self.assertEqual(conversation.contract(self.ledger, sid), original_contract)
        with self.assertRaises((InvalidUpdate, PageFault)):
            conversation.read(self.ledger, candidate)
        self.assertNotEqual(final['conversation_id'], candidate)
        checked = final['conversation']['document_verification']
        self.assertEqual(checked['verdict'], 'supported')
        self.assertEqual(checked['synthesis_model'], MODELS['mid'])
        self.assertNotIn(UNSUPPORTED, final['conversation']['result']['accepted_output']['answer'])
        self.assertIn('An approved reviewer checks',
                      final['conversation']['result']['accepted_output']['basis'])
        published = conversation.cm.history(self.ledger, sid, final['conversation_id'])
        self.assertIn('An approved reviewer checks', published[0]['assistant'])

    def test_auto_stops_on_insufficient_evidence_without_switching_models(self):
        self.review_verdict = 'insufficient_evidence'
        flow, sid, original_bytes = self.approved()
        original_contract = conversation.contract(self.ledger, sid)
        final = self.drive(flow)
        self.assertIn(final['state'], ('PAUSED', 'DONE'))
        self.assertIsNone(final['conversation_id'])
        self.assertNotIn('conversation', final)
        self.assertEqual(self.review_verdicts, ['insufficient_evidence'])
        syntheses = [model for role, model, _ in self.calls if role == 'conversation']
        self.assertEqual(syntheses, [MODELS['low']])
        spending = usage(self.ledger, sid)['used']
        self.assertEqual(spending['calls'], len(self.calls))
        self.assertEqual(spending['operations'], 1)
        self.assertEqual(spending['source_bytes'], original_bytes)
        self.assertEqual(conversation.contract(self.ledger, sid), original_contract)
        self.assertTrue(self.ledger.verify_invocations(sid))

    def test_auto_cannot_escalate_without_call_allocation(self):
        self.synthesis_errors = {MODELS['low']}
        with patch.object(conversation, 'CONSTRAINTS', dict(conversation.CONSTRAINTS, review_calls=7)):
            flow, sid, original_bytes = self.approved()
            final = self.drive(flow)
            self.assertEqual(final['state'], 'PAUSED')
            self.assertIn('calls', final['stop']['error'])
            self.assertIsNone(final['conversation_id'])
            self.assertNotIn('conversation', final)
            self.assertEqual(self.review_verdicts, ['synthesis_error'])
            self.assertEqual([model for role, model, _ in self.calls if role == 'conversation'], [MODELS['low']])
            spending = usage(self.ledger, sid)
            self.assertEqual(spending['used']['calls'], spending['limits']['calls'])
            self.assertEqual(spending['used']['calls'], len(self.calls))
            self.assertEqual(spending['used']['operations'], 1)
            self.assertEqual(spending['used']['source_bytes'], original_bytes)
            self.assertTrue(self.ledger.verify_invocations(sid))

    def test_auto_reuses_verifier_receipt_after_interruption_before_publish(self):
        flow, sid, original_bytes = self.approved()
        flow = self.interrupt_before(flow, 'DOCUMENT_SYNTHESIS_PUBLISHED')

        candidate = self.saved(flow)['document_candidate_id']
        self.assertEqual(self.review_verdicts, ['supported'])
        with self.assertRaises((InvalidUpdate, PageFault)):
            conversation.read(self.ledger, candidate)
        spending = usage(self.ledger, sid)
        self.ledger.close()
        self.ledger = InvocationLedger(self.ledger_path)
        final = self.drive(flow)
        self.assertEqual(usage(self.ledger, sid), spending)
        self.assertEqual(self.review_verdicts, ['supported'])
        self.assertEqual(final['conversation_id'], candidate)
        self.assert_completed(final, sid, original_bytes)

    def test_auto_reuses_candidate_receipt_after_interruption_before_checkpoint(self):
        flow, sid, original_bytes = self.approved()
        flow = self.interrupt_before(flow, 'DOCUMENT_SYNTHESIS_CANDIDATE')

        self.assertFalse(self.saved(flow).get('document_candidate_id'))
        candidate, = self.ledger.db.execute(
            "SELECT id FROM invocations WHERE session=? AND json_extract(record_json,'$.role')='conversation'",
            (sid,),
        ).fetchone()
        spending = usage(self.ledger, sid)
        self.ledger.close()
        self.ledger = InvocationLedger(self.ledger_path)
        flow = self.request('step', flow)
        self.assertEqual(self.saved(flow)['document_candidate_id'], candidate)
        self.assertEqual(usage(self.ledger, sid), spending)
        final = self.drive(flow)
        self.assertEqual(final['conversation_id'], candidate)
        self.assert_completed(final, sid, original_bytes)

    def test_out_of_section_quote_pauses_without_synthesis_and_resume_preserves_charges(self):
        from document_stages import synthesis_packet

        flow, sid, original_bytes = self.approved()
        flow = self.next_capture(flow)
        data = self.saved(flow)
        committed = data['document_stage_receipts']
        with self.assertRaises((InvalidUpdate, PageFault)):
            synthesis_packet(self.ledger, sid, flow['flow_id'])
        self.forge_quote = True
        failed = self.drive(flow)
        self.assertEqual(failed['state'], 'PAUSED')
        self.assertEqual(failed['stop']['resume_phase'], 'CLASSIFYING')
        self.assertNotIn('conversation', failed)
        self.assertEqual(self.saved(failed)['document_stage_receipts'], committed)
        charged = usage(self.ledger, sid)
        self.assertGreater(charged['used']['calls'], len(committed))

        self.forge_quote = False
        self.ledger.close()
        self.ledger = InvocationLedger(self.ledger_path)
        resumed = self.request('resume', failed)
        self.assertEqual(usage(self.ledger, sid), charged)
        self.assertEqual(self.assert_completed(self.drive(resumed), sid, original_bytes)[:len(committed)], committed)

    def test_short_document_keeps_single_call_path(self):
        self.final_model = MODELS['low']
        flow, sid, _ = self.approved(long=False)
        final = self.drive(flow)
        self.assertEqual(final['state'], 'DONE')
        self.assertTrue(final['conversation']['result']['admission']['admitted'])
        self.assertEqual(len(self.calls), 1)
        role, model, delivery = self.calls[0]
        self.assertEqual((role, model), ('conversation', MODELS['low']))
        self.assertEqual(delivery['document_evidence']['text'], self.document_text)
        self.assertNotIn('document_synthesis', delivery)
        self.assertEqual(usage(self.ledger, sid)['used']['calls'], 1)

    def test_all_empty_sections_keep_charges_and_end_without_synthesis(self):
        self.omit_notes = True
        flow, sid, original_bytes = self.approved()
        final = self.drive(flow)
        self.assertEqual(final['state'], 'DONE')
        self.assertIn('no supporting quotations', final['request_notice'])
        self.assertNotIn('conversation', final)
        self.assertIsNone(final['conversation_id'])
        data = self.saved(final)
        stages = len(data['document_stage_plan']['sections'])
        self.assertGreater(stages, 1)
        self.assertEqual(len(data['document_stage_receipts']), stages)
        self.assertEqual([role for role, _, _ in self.calls], ['document_extract'] * stages)
        for receipt in data['document_stage_receipts']:
            self.assertEqual(self.ledger.load_result(sid, receipt), {'notes': []})
        charged = usage(self.ledger, sid)['used']
        self.assertEqual(charged['calls'], stages)
        self.assertEqual(charged['operations'], 1)
        self.assertEqual(charged['source_bytes'], original_bytes)
        self.assertGreater(charged['tokens'], 0)
        self.assertTrue(self.ledger.verify_invocations(sid))
