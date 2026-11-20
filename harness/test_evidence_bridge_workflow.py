import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import conversation
from context_manager import InvalidUpdate, canonical
from invocation_gateway import InvocationLedger
import orchestration
import orchestration_flow
from review_budget import usage
from test_conversation import Fake, MODELS
from test_document_text import docx, word_xml


class EvidenceBridgeWorkflowTests(unittest.TestCase):
    chat_key = 'a' * 64
    resume_request = 'Can you look at the résumé now and comment on post-silicon validation roles?'
    document = 'Document text:\nValidated PCIe on first silicon and reduced triage time by 17%.'
    resume_file = 'Candidate_Post_Silicon_Resume_ATS (1).docx'
    workspace_resume_request = (
        f'can you review this resume please?\n{resume_file}\n'
        'suggest changes for a more impactful employer reach'
    )
    file_text = 'Validated PCIe on first silicon and reduced triage time by 17%.'

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.workspace = root / 'repo'
        self.storage = root / 'store'
        self.workspace.mkdir()
        self.storage.mkdir()
        (self.workspace / 'entry.py').write_text('def entry():\n    return 42\n')
        self.metadata = {'workspace': str(self.workspace)}
        self.ledger = InvocationLedger(self.storage / 'state.sqlite')
        self.addCleanup(lambda: self.ledger.close())
        self.tier = 'low'
        self.models = []
        self.router = None

    def request(self, event, flow=None, **fields):
        request = {'event': event, 'chat_key': self.chat_key, 'models': MODELS,
                   'tier': self.tier, 'armed': False}
        if flow is not None:
            request.update(flow_id=flow['flow_id'], cursor=flow['cursor'])
        request.update(fields)
        return orchestration_flow.handle(self.ledger, self.metadata, self.workspace, self.storage,
                                         request, self.provider)

    def provider(self, stage):
        self.assertEqual(stage, 'conversation')
        model = Fake(MODELS[self.tier], self.router)
        self.models.append(model)
        return model

    def seed_context(self):
        prior = 'The candidate prefers concise feedback. Target role is validation engineer VAL-271.'
        self.router = lambda result, _: result.update(facts=['Target role is validation engineer VAL-271.'])
        out = self.request('start', prompt=prior)
        self.router = None
        self.assertTrue(out['conversation']['result']['admission']['admitted'])
        return out['conversation_id'], out['conversation']['session']

    def decision(self, flow, kind):
        events = orchestration.events(self.ledger, flow['flow_id'])
        return next(event['kernel']['decision'] for event in reversed(events) if event['kind'] == kind)

    def write_resume(self, relative_path):
        path = self.workspace / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = docx(word_xml(f'<w:p><w:r><w:t>{self.file_text}</w:t></w:r></w:p>'))
        path.write_bytes(raw)
        return raw

    def assert_evidence_bridge(self, flow, target_kind):
        self.assertEqual(flow['state'], 'PAUSED')
        self.assertEqual(flow['stop']['status'], 'evidence_bridge')
        self.assertEqual(flow['stop']['resume_phase'], 'CLASSIFYING')
        bridge = flow['stop']['evidence_bridge']
        self.assertEqual(bridge['target_kind'], target_kind)
        self.assertEqual(bridge['decision'], 'BRIDGE')
        self.assertEqual(self.decision(flow, 'EVIDENCE_BRIDGE'), 'BRIDGE')

    def test_document_evidence_resumes_same_target_context_and_spend_on_higher_tier(self):
        parent, sid = self.seed_context()
        before = usage(self.ledger, sid)
        caption = conversation.contract(self.ledger, sid)
        self.assertEqual(before['used']['calls'], 1)

        with patch('repository_tools.execute', side_effect=AssertionError('Unexpected document repository operation')):
            paused = self.request('start', prompt=self.resume_request, parent=parent)
            self.assert_evidence_bridge(paused, 'document')
            self.assertEqual(paused['stop']['budget'], before)
            self.assertEqual(paused['task_stack']['focused'], paused['flow_id'])
            self.assertEqual(len(self.models), 1)

            for fields in (
                {'chat_key': 'b' * 64},
                {'cursor': '0' * 64},
            ):
                with self.subTest(fields=fields), self.assertRaises(InvalidUpdate):
                    self.request('supply_evidence', paused, evidence_text=self.document, **fields)
                self.assertEqual(usage(self.ledger, sid), before)
                self.assertEqual(self.request('inspect', paused)['cursor'], paused['cursor'])

            self.ledger.close()
            self.ledger = InvocationLedger(self.storage / 'state.sqlite')
            self.tier = 'high'
            resumed = self.request('supply_evidence', paused, evidence_text=self.document)
            self.assertEqual(resumed['flow_id'], paused['flow_id'])
            self.assertEqual(resumed['state'], 'CLASSIFYING')
            self.assertEqual(usage(self.ledger, sid), before)
            self.assertEqual(len(self.models), 1)
            self.assertEqual(self.decision(resumed, 'EVIDENCE_SUPPLIED'), 'ALLOW')
            with self.assertRaises(InvalidUpdate):
                self.request('supply_evidence', paused, evidence_text='Document text:\nReplayed input.')

            def write_review(result, packet):
                self.assertEqual(packet['request'], self.resume_request)
                self.assertIn('17%', packet['document_evidence']['text'])
                result['answer'] = 'Lead with the PCIe validation accomplishment and explain the 17% improvement.'

            self.router = write_review
            finished = self.request('step', resumed)

        self.assertEqual(finished['state'], 'DONE')
        self.assertEqual(finished['conversation']['session'], sid)
        self.assertTrue(finished['conversation']['result']['admission']['admitted'])
        self.assertIn('17% improvement', finished['conversation']['result']['accepted_output']['answer'])
        self.assertEqual(len(self.models), 2)
        self.assertEqual(self.models[-1].model, MODELS['high'])
        packet = self.models[-1].payloads[0]
        self.assertIn('VAL-271', canonical(packet['memory']['pinned_user_statements']))
        self.assertEqual(packet['contract'], caption)
        after = usage(self.ledger, sid)
        self.assertEqual(after['limits'], before['limits'])
        self.assertEqual(after['used']['calls'], before['used']['calls'] + 1)
        self.assertGreater(after['used']['tokens'], before['used']['tokens'])
        self.assertEqual(after['used']['operations'], before['used']['operations'])
        self.assertTrue(self.ledger.verify_invocations(sid))

    def test_workspace_docx_requires_exact_read_approval_and_resumes_existing_conversation(self):
        from repository_tools import read_bytes

        parent, sid = self.seed_context()
        contract = conversation.contract(self.ledger, sid)
        before = usage(self.ledger, sid)
        relative_path = 'applications/' + self.resume_file
        raw = self.write_resume(relative_path)
        self.write_resume('applications/another candidate.docx')

        with patch('repository_tools.execute', side_effect=AssertionError('Document review entered repository mapping')), \
                patch('document_source.read_bytes', wraps=read_bytes) as source_read:
            paused = self.request('start', prompt=self.workspace_resume_request, parent=parent)
            self.assert_evidence_bridge(paused, 'document')
            self.assertEqual(paused['stop']['evidence_bridge']['target'], self.resume_file)
            self.assertEqual(paused['stop']['evidence_bridge']['required'], 'document_file')
            self.assertEqual(self.request('resume', paused)['cursor'], paused['cursor'])
            with self.assertRaises(InvalidUpdate):
                self.request('approve_document', paused, document_path=relative_path, confirmed=True)
            located = self.request('locate_document', paused)
            self.assertEqual(located['stop']['document_lookup']['paths'], [relative_path])
            self.assertEqual(located['stop']['document_lookup']['status'], 'found')
            self.assertEqual(self.decision(located, 'DOCUMENT_LOCATED'), 'BRIDGE')
            for fields in (
                {'confirmed': False},
                {'confirmed': 'true'},
                {'document_path': 'applications/another candidate.docx'},
                {'document_path': '../outside.docx'},
                {'chat_key': 'b' * 64},
                {'cursor': paused['cursor']},
            ):
                approval = {'document_path': relative_path, 'confirmed': True, **fields}
                with self.subTest(approval=approval), self.assertRaises(InvalidUpdate):
                    self.request('approve_document', located, **approval)
            source_read.assert_not_called()
            self.assertEqual(usage(self.ledger, sid), before)
            self.assertEqual(len(self.models), 1)

            self.ledger.close()
            self.ledger = InvocationLedger(self.storage / 'state.sqlite')
            self.tier = 'high'
            resumed = self.request('approve_document', located, document_path=relative_path, confirmed=True)
            self.assertEqual(resumed['flow_id'], located['flow_id'])
            self.assertEqual(resumed['state'], 'CLASSIFYING')
            self.assertEqual(self.decision(resumed, 'EVIDENCE_SUPPLIED'), 'ALLOW')
            self.assertEqual(source_read.call_args.args, (self.workspace, relative_path))
            charged = usage(self.ledger, sid)
            self.assertEqual(charged['limits'], before['limits'])
            self.assertEqual(charged['used']['calls'], before['used']['calls'])
            self.assertEqual(charged['used']['tokens'], before['used']['tokens'])
            self.assertEqual(charged['used']['operations'], before['used']['operations'] + 1)
            self.assertEqual(charged['used']['source_bytes'], before['used']['source_bytes'] + len(raw))
            with self.assertRaises(InvalidUpdate):
                self.request('approve_document', located, document_path=relative_path, confirmed=True)

            def review(result, packet):
                self.assertEqual(packet['request'], self.workspace_resume_request)
                self.assertEqual(packet['document_evidence']['text'], self.file_text)
                self.assertEqual(packet['contract'], contract)
                self.assertEqual(packet['memory']['invocation_id'], parent)
                self.assertFalse(packet['memory']['pinned_user_statements'])
                result['answer'] = 'Lead with PCIe validation and quantify the 17% triage improvement.'

            self.router = review
            finished = self.request('step', resumed)
            source_read.assert_called_once()

        self.assertEqual(finished['state'], 'DONE')
        self.assertEqual(finished['conversation']['session'], sid)
        self.assertTrue(finished['conversation']['result']['admission']['admitted'])
        self.assertEqual(self.models[-1].model, MODELS['high'])
        self.assertEqual(len(self.models), 2)
        after = usage(self.ledger, sid)
        for dimension in ('operations', 'source_bytes'):
            self.assertEqual(after['used'][dimension], charged['used'][dimension])
        self.assertEqual(after['used']['calls'], charged['used']['calls'] + 1)
        self.assertGreater(after['used']['tokens'], charged['used']['tokens'])
        document = self.ledger.document(sid, 'document-bridge:' + located['flow_id'])
        origin = json.loads(document['source_json'])
        self.assertEqual(document['authority'], 'tool')
        self.assertEqual(origin['path'], relative_path)
        self.assertEqual(origin['source_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertTrue(self.ledger.verify_invocations(sid))

    def test_workspace_docx_missing_and_ambiguous_require_user_evidence(self):
        with patch('document_source.read_bytes', side_effect=AssertionError('Document bytes read during lookup')):
            paused = self.request('start', prompt=self.workspace_resume_request)
            missing = self.request('locate_document', paused)
            self.assertEqual(missing['stop']['document_lookup']['status'], 'missing')
            self.assertEqual(missing['stop']['budget']['used']['operations'], 0)
            self.assertIn('not found', missing['stop']['error'])

            first, second = ('drafts/' + self.resume_file, 'final/' + self.resume_file)
            self.write_resume(first)
            self.write_resume(second)
            choices = self.request('locate_document', missing)
            self.assertEqual(choices['stop']['document_lookup']['status'], 'ambiguous')
            self.assertEqual(choices['stop']['document_lookup']['paths'], [first, second])
            self.assertEqual(choices['stop']['budget']['used']['operations'], 0)
            with self.assertRaises(InvalidUpdate):
                self.request('approve_document', choices, document_path=self.resume_file, confirmed=True)

        resumed = self.request('approve_document', choices, document_path=second, confirmed=True)
        self.assertEqual(resumed['state'], 'CLASSIFYING')
        _, details = orchestration_flow.info(self.ledger, resumed['flow_id'])
        saved = self.ledger.document(details['conversation_session'], details['document_evidence']['key'])
        self.assertEqual(json.loads(saved['source_json'])['path'], second)

    def test_admitted_read_of_a_named_docx_uses_document_approval(self):
        filename = 'replacement-plan.docx'
        self.write_resume(filename)
        self.router = lambda result, _: result.update(intent='workspace', workspace_request={
            'operation': 'read', 'paths': [filename],
        })
        prompt = f'Any red flags here? {filename}'
        with patch('document_source.read_bytes', side_effect=AssertionError('Read before user approval')):
            paused = self.request('start', prompt=prompt)
            self.assert_evidence_bridge(paused, 'document')
            self.assertEqual(paused['stop']['evidence_bridge']['required'], 'document_file')
            located = self.request('locate_document', paused)
            self.assertEqual(located['stop']['document_lookup']['paths'], [filename])
            self.assertEqual(located['stop']['budget']['used']['operations'], 0)

    def test_workspace_docx_followup_reuses_approved_evidence_across_reload_and_tier(self):
        parent, sid = self.seed_context()
        self.write_resume(self.resume_file)
        paused = self.request('start', prompt=self.workspace_resume_request, parent=parent)
        located = self.request('locate_document', paused)
        supplied = self.request('approve_document', located, document_path=self.resume_file, confirmed=True)
        self.router = lambda result, _: result.update(answer='Initial résumé feedback.')
        initial = self.request('step', supplied)
        self.assertEqual(initial['state'], 'DONE')
        original = self.models[-1].payloads[0]['document_evidence']
        self.router = lambda result, _: result.update(answer='Please share the résumé text.')
        with patch('orchestration_flow.retained_chat_document', return_value=None):
            missed = self.request('start', prompt='Can you do a deeper analysis based on the résumé?',
                                  parent=initial['conversation_id'])
            self.assertEqual(missed['state'], 'DONE')
            self.assertTrue(missed['conversation']['result']['admission']['admitted'])
            self.assertNotIn('document_evidence', self.models[-1].payloads[0])
        saved_budget = usage(self.ledger, sid)
        saved_contract = conversation.contract(self.ledger, sid)

        self.ledger.close()
        self.ledger = InvocationLedger(self.storage / 'state.sqlite')
        self.tier = 'high'
        (self.workspace / self.resume_file).unlink()
        prompt = 'can you do a deeper analysis on the resume comparing to market requirements [search web if needed]'

        def targeted_review(result, packet):
            self.assertEqual(packet['request'], prompt)
            self.assertEqual(packet['contract'], saved_contract)
            self.assertNotIn('VAL-271', canonical(packet['memory']))
            self.assertIn('Initial résumé feedback.', [
                item['assistant_unverified'] for item in packet['memory']['recent_turns']
            ])
            for field in ('key', 'version', 'digest', 'text'):
                self.assertEqual(packet['document_evidence'][field], original[field])
            self.assertEqual(packet['document_evidence']['text'], self.file_text)
            self.assertEqual(packet['document_evidence']['retained_from'], expected_parent)
            result['answer'] = 'Target PCIe validation roles at semiconductor vendors and platform integrators.'

        self.router = targeted_review
        expected_parent = missed['conversation_id']
        with patch('document_source.locate_document', side_effect=AssertionError('Document was located again')), \
                patch('document_source.read_bytes', side_effect=AssertionError('Document was read again')), \
                patch('repository_tools.execute', side_effect=AssertionError('Follow-up entered repository mapping')):
            followup = self.request('start', prompt=prompt, parent=expected_parent)
            if followup['state'] == 'CLASSIFYING':
                followup = self.request('step', followup)

            self.assertEqual(followup['state'], 'DONE')
            self.assertEqual(followup['conversation']['session'], sid)
            self.assertTrue(followup['conversation']['result']['admission']['admitted'],
                            followup['conversation']['result']['admission']['errors'])
            self.assertEqual(self.decision(followup, 'EVIDENCE_REPAGED'), 'ALLOW')
            self.assertIn('PCIe validation roles', followup['conversation']['result']['accepted_output']['answer'])

            prompt = 'What employers and roles should I target based on this résumé?'
            expected_parent = followup['conversation_id']
            self.router = lambda result, _: result.update(answer='PCIe appears in 73% of current postings.')
            rejected = self.request('start', prompt=prompt, parent=expected_parent)
            self.assertFalse(rejected['conversation']['result']['admission']['admitted'])
            self.assertEqual(rejected['state'], 'DONE')
            self.router = targeted_review
            next_turn = self.request('start', prompt=prompt, parent=expected_parent)
            if next_turn['state'] == 'CLASSIFYING':
                next_turn = self.request('step', next_turn)

        self.assertEqual(next_turn['state'], 'DONE')
        self.assertEqual(next_turn['conversation']['session'], sid)
        self.assertTrue(next_turn['conversation']['result']['admission']['admitted'],
                        next_turn['conversation']['result']['admission']['errors'])
        self.assertEqual(self.models[-1].model, MODELS['high'])
        packet = self.models[-1].payloads[0]
        invalid = dict(packet, document_evidence={**packet['document_evidence'], 'retained_from': initial['conversation_id']})
        invalid['context_binding'] = conversation.binding(invalid)
        with self.assertRaisesRegex(InvalidUpdate, 'does not match the retained conversation context'):
            conversation.request_check(self.ledger, sid, invalid)
        after = usage(self.ledger, sid)
        self.assertEqual(after['limits'], saved_budget['limits'])
        self.assertEqual(after['used']['calls'], saved_budget['used']['calls'] + 5)
        self.assertGreater(after['used']['tokens'], saved_budget['used']['tokens'])
        for dimension in ('operations', 'source_bytes'):
            self.assertEqual(after['used'][dimension], saved_budget['used'][dimension])
        self.assertTrue(self.ledger.verify_invocations(sid))

        self.router = None
        previous_model_count = len(self.models)
        fresh = self.request('start', prompt='Please reread this résumé from disk after my edits.',
                             parent=next_turn['conversation_id'])
        if fresh['state'] == 'CLASSIFYING':
            self.request('step', fresh)
        for model in self.models[previous_model_count:]:
            for delivered in model.payloads:
                self.assertNotIn('document_evidence', delivered)

    def test_implicit_followup_repages_an_approved_docx_after_classifying(self):
        self.write_resume(self.resume_file)
        paused = self.request('start', prompt=self.workspace_resume_request)
        located = self.request('locate_document', paused)
        supplied = self.request('approve_document', located, document_path=self.resume_file, confirmed=True)
        self.router = lambda result, _: result.update(answer='Initial review complete.')
        initial = self.request('step', supplied)
        self.assertEqual(initial['state'], 'DONE')
        self.assertTrue(initial['conversation']['result']['admission']['admitted'])
        sid = initial['conversation']['session']
        parent = initial['conversation_id']
        prior = self.models[-1].payloads[0]['document_evidence']
        before = usage(self.ledger, sid)
        contract = conversation.contract(self.ledger, sid)
        (self.workspace / self.resume_file).unlink()
        prompt = 'What would a skeptical board push back on?'

        def classify(result, packet):
            self.assertEqual(packet['request'], prompt)
            self.assertEqual(packet['document_offer']['ref']['retained_from'], parent)
            self.assertNotIn('document_evidence', packet)
            self.assertNotIn(self.file_text, canonical(packet))
            wire = canonical(conversation.ConversationModel(self.storage/'calls').messages(conversation.SYSTEM, packet))
            self.assertIn('previous_snapshot', wire)
            self.assertNotIn(packet['document_offer']['ref']['digest'], wire)
            self.assertNotIn(self.file_text, wire)
            schema = conversation.model_schema(packet, conversation.schema(packet))
            self.assertIn('document_request', schema['required'])
            self.assertEqual(schema['properties']['document_request']['enum'], ['previous_snapshot', 'none'])
            result.update(intent='document', document_request='previous_snapshot')

        def review(result, packet):
            self.assertEqual(packet['request'], prompt)
            self.assertEqual(packet['contract'], contract)
            evidence = packet['document_evidence']
            for key in ('key', 'version', 'digest', 'text'):
                self.assertEqual(evidence[key], prior[key])
            self.assertEqual(evidence['retained_from'], parent)
            self.assertEqual(evidence['selected_by'], selected)
            result.update(answer='The board may ask how the triage improvement was measured.')

        with patch('document_source.locate_document', side_effect=AssertionError('Repeated document lookup')), \
                patch('document_source.read_bytes', side_effect=AssertionError('Repeated source read')), \
                patch('repository_tools.execute', side_effect=AssertionError('Follow-up entered workspace mapping')):
            self.router = classify
            followup = self.request('start', prompt=prompt, parent=parent)
            self.assertEqual(followup['state'], 'CLASSIFYING')
            selected = followup['routing_step']['result']['invocation_id']
            self.assertEqual(self.decision(followup, 'EVIDENCE_REPAGED'), 'ALLOW')
            self.assertEqual(usage(self.ledger, sid)['used']['calls'], before['used']['calls'] + 1)

            self.ledger.close()
            self.ledger = InvocationLedger(self.storage / 'state.sqlite')
            self.tier = 'high'
            self.router = review
            finished = self.request('step', followup)

        self.assertEqual(finished['state'], 'DONE')
        self.assertEqual(finished['conversation']['session'], sid)
        self.assertTrue(finished['conversation']['result']['admission']['admitted'])
        self.assertIn('triage improvement', finished['conversation']['result']['accepted_output']['answer'])
        self.assertEqual(self.models[-1].model, MODELS['high'])
        delivered = self.models[-1].payloads[0]
        substituted = dict(delivered, document_evidence={**delivered['document_evidence'], 'selected_by': parent})
        substituted['context_binding'] = conversation.binding(substituted)
        with self.assertRaises(InvalidUpdate):
            conversation.request_check(self.ledger, sid, substituted)
        after = usage(self.ledger, sid)
        self.assertEqual(after['limits'], before['limits'])
        self.assertEqual(after['used']['calls'], before['used']['calls'] + 2)
        self.assertGreater(after['used']['tokens'], before['used']['tokens'])
        for dimension in ('operations', 'source_bytes'):
            self.assertEqual(after['used'][dimension], before['used'][dimension])
        self.assertTrue(self.ledger.verify_invocations(sid))

        def greet(result, packet):
            self.assertNotIn('document_offer', packet)
            self.assertNotIn('document_evidence', packet)
            result.update(answer='Hello!')

        self.router = greet
        greeted = self.request('start', prompt='Hello!', parent=finished['conversation_id'])
        self.assertEqual(greeted['state'], 'DONE')
        self.assertEqual(greeted['conversation']['result']['accepted_output']['answer'], 'Hello!')

    def test_implicit_document_offer_is_scoped_and_does_not_force_document_routing(self):
        self.write_resume(self.resume_file)
        paused = self.request('start', prompt=self.workspace_resume_request)
        located = self.request('locate_document', paused)
        supplied = self.request('approve_document', located, document_path=self.resume_file, confirmed=True)
        self.router = lambda result, _: result.update(answer='Initial review complete.')
        initial = self.request('step', supplied)
        parent = initial['conversation_id']
        self.assertEqual(initial['state'], 'DONE')
        foreign_start = len(self.models)

        def unrelated_reply(result, packet):
            self.assertNotIn('document_evidence', packet)
            self.assertNotIn(self.file_text, canonical(packet))
            result.update(answer='An independent reply.')

        self.router = unrelated_reply
        with patch('document_source.read_bytes', side_effect=AssertionError('Unexpected source read')):
            try:
                foreign = self.request('start', prompt='What would a skeptical board push back on?',
                                       parent=parent, chat_key='b' * 64)
                if foreign['state'] == 'CLASSIFYING':
                    self.request('step', foreign, chat_key='b' * 64)
            except InvalidUpdate:
                pass  # A foreign lineage may be refused before dispatch.
            for model in self.models[foreign_start:]:
                for packet in model.payloads:
                    self.assertFalse(packet.get('document_offer'))
                    self.assertNotIn('document_evidence', packet)
                    self.assertNotIn(self.file_text, canonical(packet))

            unrelated = self.request('start', prompt='Explain what a load balancer does.', parent=parent)
            if unrelated['state'] == 'CLASSIFYING':
                unrelated = self.request('step', unrelated)
            self.assertEqual(unrelated['state'], 'DONE')
            self.assertEqual(unrelated['conversation']['result']['accepted_output']['intent'], 'conversation')
            self.assertEqual(unrelated['conversation']['result']['accepted_output']['document_request'], 'none')

    def test_multiple_recent_documents_do_not_get_an_implicit_snapshot_offer(self):
        self.router = lambda result, _: result.update(answer='Initial review complete.')
        parent = None
        for name in ('Option-A.docx', 'Option-B.docx'):
            self.write_resume(name)
            flow = self.request('start', prompt=f'Review {name}.', parent=parent)
            flow = self.request('locate_document', flow)
            flow = self.request('approve_document', flow, document_path=name, confirmed=True)
            flow = self.request('step', flow)
            self.assertEqual(flow['state'], 'DONE')
            parent = flow['conversation_id']

        def clarify(result, packet):
            self.assertNotIn('document_offer', packet)
            self.assertNotIn('document_evidence', packet)
            result.update(intent='document', context_question='Which draft should I use first?')

        self.router = clarify
        with patch('document_source.read_bytes', side_effect=AssertionError('Ambiguous target read')):
            flow = self.request('start', prompt='What would you change?', parent=parent)
        self.assertEqual(flow['state'], 'PAUSED')
        self.assertEqual(flow['stop']['error'], 'Which draft should I use first?')

    def test_workspace_docx_evidence_does_not_cross_chat_or_document_targets(self):
        self.write_resume(self.resume_file)
        paused = self.request('start', prompt=self.workspace_resume_request)
        located = self.request('locate_document', paused)
        supplied = self.request('approve_document', located, document_path=self.resume_file, confirmed=True)
        initial = self.request('step', supplied)
        parent = initial['conversation_id']
        self.assertEqual(initial['state'], 'DONE')

        def independent_reply(result, packet):
            self.assertNotIn('document_evidence', packet)
            result['answer'] = 'An independent reply.'

        self.router = independent_reply
        with patch('document_source.read_bytes', side_effect=AssertionError('Unexpected document content read')):
            unrelated = self.request('start', prompt='How does the résumé parser work in this codebase?', parent=parent)
            if unrelated['state'] == 'CLASSIFYING':
                unrelated = self.request('step', unrelated)
            self.assertEqual(unrelated['state'], 'DONE')
            self.assertTrue(unrelated['conversation']['result']['admission']['admitted'])

            new_name = 'Other_Candidate.docx'
            other_doc = self.request('start', prompt=f'Please review {new_name}.', parent=parent)
            self.assert_evidence_bridge(other_doc, 'document')
            self.assertEqual(other_doc['stop']['evidence_bridge']['target'], new_name)

            previous_model_count = len(self.models)
            try:
                separate_chat = self.request('start', prompt='What roles fit this résumé?', parent=parent,
                                             chat_key='b' * 64)
                if separate_chat['state'] == 'CLASSIFYING':
                    separate_chat = self.request('step', separate_chat, chat_key='b' * 64)
            except InvalidUpdate:
                pass  # A foreign lineage may be refused before model dispatch.
            for model in self.models[previous_model_count:]:
                for delivered in model.payloads:
                    self.assertNotIn('document_evidence', delivered)

    def test_named_document_and_its_followup_exclude_the_prior_document_topic(self):
        self.write_resume(self.resume_file)
        old_prompt = self.workspace_resume_request + '\nKeep feedback focused on private project RESUME-043.'
        self.router = lambda result, _: result.update(answer='Prior findings RESUME-054.',
                                                     summary='Prior summary RESUME-065.')
        old = self.request('start', prompt=old_prompt)
        old = self.request('locate_document', old)
        old = self.request('approve_document', old, document_path=self.resume_file, confirmed=True)
        old = self.request('step', old)
        old_receipt, sid = old['conversation_id'], old['conversation']['session']
        before = usage(self.ledger, sid)

        filename = 'next-paper.docx'
        body = 'The paper relates trajectory drift to an invariant structure.'
        raw = docx(word_xml(f'<w:p><w:r><w:t>{body}</w:t></w:r></w:p>'))
        (self.workspace / filename).write_bytes(raw)
        prompt = f'What is your opinion on this document? - {self.workspace / filename}'

        def review(result, packet):
            messages = conversation.ConversationModel(self.storage / 'calls').messages(conversation.SYSTEM, packet)
            self.assertEqual(json.loads(messages[1]['content'])['document_text'], body)
            wire = canonical(messages)
            self.assertNotIn(self.resume_file, wire)
            for marker in ('RESUME-043', 'RESUME-054', 'RESUME-065'):
                self.assertNotIn(marker, wire)
            self.assertEqual(packet['memory']['toc'], [])
            if packet['document_evidence'].get('retained_from'):
                self.assertIn('Paper review PAPER-076.', wire)
            else:
                self.assertEqual(packet['memory']['invocation_id'], old_receipt)
                self.assertEqual(packet['memory']['omitted_turns'], 1)
            result.update(answer='Paper review PAPER-076.')

        self.router = review
        new = self.request('start', prompt=prompt, parent=old_receipt)
        new = self.request('locate_document', new)
        new = self.request('approve_document', new, document_path=filename, confirmed=True)
        new = self.request('step', new)
        self.assertTrue(new['conversation']['result']['admission']['admitted'])
        self.assertEqual(new['conversation']['session'], sid)
        self.assertEqual(conversation.cm.history(self.ledger, sid, new['conversation_id'])[-1]['id'], old_receipt)

        self.tier = 'high'
        with patch('document_source.read_bytes', side_effect=AssertionError('Snapshot re-read')):
            followup = self.request('start', prompt='Review it again.', parent=new['conversation_id'])
            if followup['state'] == 'CLASSIFYING':
                followup = self.request('step', followup)
        self.assertTrue(followup['conversation']['result']['admission']['admitted'])
        self.assertEqual(followup['conversation']['session'], sid)
        self.assertEqual(self.models[-1].model, MODELS['high'])
        after = usage(self.ledger, sid)
        self.assertEqual(after['used']['operations'], before['used']['operations'] + 1)
        self.assertEqual(after['used']['source_bytes'], before['used']['source_bytes'] + len(raw))
        self.assertEqual(after['used']['calls'], before['used']['calls'] + 2)
        self.assertTrue(self.ledger.verify_invocations(sid))

    def test_absolute_opinion_request_recovers_a_saved_paste_only_bridge(self):
        self.write_resume(self.resume_file)
        logical = str(self.workspace / self.resume_file)
        prompt = f'what is your opinon on this document? - {logical}'
        self.router = lambda result, _: result.update(intent='document', answer='Need the document.')
        with patch('orchestration_flow.document_file_target', return_value=None), \
                patch('orchestration_flow.document_input_notice', return_value=None):
            old = self.request('start', prompt=prompt)
        self.assertEqual(old['state'], 'PAUSED')
        self.assertEqual(old['stop']['evidence_bridge']['required'], 'document_text')
        sid = old['conversation']['session']
        before = usage(self.ledger, sid)
        self.assertEqual(before['used']['calls'], 1)
        self.ledger.close()
        self.ledger = InvocationLedger(self.storage / 'state.sqlite')

        with patch('document_source.read_bytes', side_effect=AssertionError('Read before approval')):
            located = self.request('locate_document', old)
        self.assertEqual(located['flow_id'], old['flow_id'])
        self.assertEqual(located['stop']['document_lookup']['paths'], [self.resume_file])
        self.assertEqual(usage(self.ledger, sid), before)
        approved = self.request('approve_document', located, document_path=self.resume_file, confirmed=True)
        self.assertEqual(approved['state'], 'CLASSIFYING')
        self.router = lambda result, packet: result.update(answer='The supplied evidence supports a review.')
        done = self.request('step', approved)
        self.assertTrue(done['conversation']['result']['admission']['admitted'])
        self.assertEqual(done['conversation']['session'], sid)
        packet = self.models[-1].payloads[-1]
        self.assertEqual(packet['request'], prompt)
        self.assertEqual(packet['document_evidence']['text'], self.file_text)
        after = usage(self.ledger, sid)
        self.assertEqual(after['limits'], before['limits'])
        self.assertEqual(after['used']['calls'], 2)
        self.assertEqual(after['used']['operations'], 1)

    def test_absolute_opinion_request_needs_no_model_or_read_to_locate(self):
        self.write_resume(self.resume_file)
        prompt = f'what is your opinon on this document? - {self.workspace / self.resume_file}'
        with patch.object(self, 'provider', side_effect=AssertionError('Model call before file approval')), \
                patch('document_source.read_bytes', side_effect=AssertionError('Read before approval')):
            paused = self.request('start', prompt=prompt)
            self.assertEqual(paused['state'], 'PAUSED')
            self.assertEqual(paused['stop']['evidence_bridge']['required'], 'document_file')
            located = self.request('locate_document', paused)
        self.assertEqual(located['stop']['document_lookup']['paths'], [self.resume_file])
        self.assertEqual(located['stop']['budget']['used']['calls'], 0)
        self.assertEqual(located['stop']['budget']['used']['source_bytes'], 0)

    def test_saved_paste_only_bridge_can_locate_original_workspace_docx(self):
        self.write_resume(self.resume_file)
        with patch('orchestration_flow.document_file_target', return_value=None):
            paused = self.request('start', prompt=self.workspace_resume_request)
        self.assertEqual(paused['stop']['evidence_bridge']['required'], 'document_text')

        self.ledger.close()
        self.ledger = InvocationLedger(self.storage / 'state.sqlite')
        inspected = self.request('inspect', paused)
        self.assertEqual(inspected['prompt'], self.workspace_resume_request)
        located = self.request('locate_document', inspected)
        self.assertEqual(located['flow_id'], paused['flow_id'])
        self.assertEqual(located['stop']['document_lookup']['paths'], [self.resume_file])
        self.assertEqual(len(self.models), 0)

    def test_unsupported_document_keeps_paste_route(self):
        (self.workspace / 'resume.pdf').write_bytes(b'%PDF fixture')
        with patch('document_source.locate_document', side_effect=AssertionError('Unsupported file lookup')):
            paused = self.request('start', prompt='Please review resume.pdf')
            self.assert_evidence_bridge(paused, 'document')
            self.assertEqual(paused['stop']['evidence_bridge']['required'], 'document_text')
            self.assertEqual(self.request('locate_document', paused)['cursor'], paused['cursor'])
            resumed = self.request('supply_evidence', paused, evidence_text=self.document)
            self.assertEqual(resumed['state'], 'CLASSIFYING')

    def test_changed_workspace_docx_stops_before_model_and_requires_new_lookup(self):
        budgeted = self.write_resume(self.resume_file)
        paused = self.request('start', prompt=self.workspace_resume_request)
        located = self.request('locate_document', paused)
        (self.workspace / self.resume_file).write_bytes(budgeted[:-1])
        stopped = self.request('approve_document', located, document_path=self.resume_file, confirmed=True)
        self.assertEqual(stopped['state'], 'PAUSED')
        self.assertEqual(stopped['stop']['document_lookup']['status'], 'read_failed')
        self.assertEqual(len(self.models), 0)
        self.assertFalse(self.ledger.db.execute('SELECT 1 FROM documents').fetchone())
        self.assertEqual(stopped['stop']['budget']['used']['operations'], 1)
        self.assertEqual(stopped['stop']['budget']['used']['source_bytes'], len(budgeted))
        with self.assertRaises(InvalidUpdate):
            self.request('approve_document', stopped, document_path=self.resume_file, confirmed=True)

        self.write_resume(self.resume_file)
        located_again = self.request('locate_document', stopped)
        recovered = self.request('approve_document', located_again, document_path=self.resume_file, confirmed=True)
        self.assertEqual(recovered['state'], 'CLASSIFYING')
        _, details = orchestration_flow.info(self.ledger, recovered['flow_id'])
        spend = usage(self.ledger, details['conversation_session'])
        self.assertEqual(spend['used']['operations'], 2)
        self.assertEqual(spend['used']['source_bytes'], 2 * len(budgeted))

    def test_missing_scope_requires_confirmation_before_workspace_arming_or_reads(self):
        self.router = lambda result, _: result.update(
            intent='workspace', workspace_request={'operation': 'read', 'paths': []})

        with patch('repository_tools.execute', side_effect=AssertionError('Workspace read before scope approval')):
            paused = self.request('start', prompt='Can you review the implementation?')
            self.assert_evidence_bridge(paused, 'workspace')
            before = usage(self.ledger, paused['conversation']['session'])
            self.assertEqual(before['used']['calls'], 1)
            scoped = self.request('supply_evidence', paused, evidence_text='Use this workspace')
            self.assertEqual(scoped['flow_id'], paused['flow_id'])
            self.assertEqual(scoped['state'], 'NEED_ARM')
            self.assertEqual(usage(self.ledger, paused['conversation']['session']), before)
            self.assertEqual(len(self.models), 1)
            with self.assertRaises(InvalidUpdate):
                self.request('step', scoped)

        bound = self.request('step', scoped, armed=True)
        self.assertEqual(bound['state'], 'WORKSPACE')
        self.assertEqual(self.ledger.pinned(bound['binding']['session'])['constraints']['allowed_paths'], ['.'])

    def test_explicit_workspace_target_enters_existing_arming_path(self):
        self.router = lambda result, _: result.update(
            intent='workspace', workspace_request={'operation': 'read', 'scope': 'workspace', 'paths': []})
        with patch('repository_tools.execute', side_effect=AssertionError('Workspace read before arming')):
            admitted = self.request('start', prompt='Review this workspace.')
        self.assertEqual(admitted['state'], 'NEED_ARM')
        self.assertIsNone(admitted['stop'])
        self.assertEqual(len(self.models), 1)

    def test_broad_workspace_request_uses_user_target_instead_of_router_filenames(self):
        self.router = lambda result, _: result.update(
            intent='workspace', workspace_request={
                'operation': 'read', 'paths': ['app.py', 'config.py', 'tools.py']})
        prompt = ('can you review the workspace project and go through the tools logic and help me '
                  'understand the architecture in detail? a few block diagrams would be great')
        with patch('repository_tools.execute', side_effect=AssertionError('Workspace read before arming')):
            admitted = self.request('start', prompt=prompt)
        self.assertEqual(admitted['state'], 'NEED_ARM')
        _, details = orchestration_flow.info(self.ledger, admitted['flow_id'])
        self.assertEqual(details['source_roots'], ['.'])
        self.assertEqual(details['permission'], {'operation': 'read', 'scope': 'workspace', 'paths': []})
        session = admitted['conversation']['session']
        before = usage(self.ledger, session)
        self.tier = 'high'
        bound = self.request('step', admitted, armed=True)
        self.assertEqual(bound['state'], 'WORKSPACE')
        self.assertEqual(self.ledger.pinned(bound['binding']['session'])['constraints']['allowed_paths'], ['.'])
        self.assertEqual(usage(self.ledger, session), before)
        self.assertEqual(len(self.models), 1)

    def test_router_cannot_ignore_current_user_filename_scope(self):
        self.router = lambda result, _: result.update(
            intent='workspace', workspace_request={'operation': 'read', 'scope': 'workspace', 'paths': []})
        with patch('repository_tools.execute', side_effect=AssertionError('Workspace read before scope confirmation')):
            paused = self.request('start', prompt='Review only entry.py from this workspace.')
        self.assert_evidence_bridge(paused, 'workspace')
        sid = paused['conversation']['session']
        before = usage(self.ledger, sid)
        scoped = self.request('supply_evidence', paused, evidence_text='Workspace files:\nentry.py')
        bound = self.request('step', scoped, armed=True)
        self.assertEqual(self.ledger.pinned(bound['binding']['session'])['constraints']['allowed_paths'], ['entry.py'])
        self.assertEqual(usage(self.ledger, sid), before)

    def test_workspace_evidence_can_narrow_scope_without_spending(self):
        self.router = lambda result, _: result.update(
            intent='workspace', workspace_request={'operation': 'read', 'paths': []})
        paused = self.request('start', prompt='Can you review the implementation?')
        sid = paused['conversation']['session']
        before = usage(self.ledger, sid)
        with self.assertRaises(InvalidUpdate):
            self.request('supply_evidence', paused, evidence_text='Workspace files:\n../private.py')
        scoped = self.request('supply_evidence', paused, evidence_text='Workspace files:\nentry.py')
        self.assertEqual(scoped['flow_id'], paused['flow_id'])
        self.assertEqual(scoped['state'], 'NEED_ARM')
        self.assertEqual(usage(self.ledger, sid), before)
        bound = self.request('step', scoped, armed=True)
        self.assertEqual(self.ledger.pinned(bound['binding']['session'])['constraints']['allowed_paths'], ['entry.py'])

    def test_exhausted_conversation_budget_prevents_document_recovery_dispatch(self):
        with patch.dict(conversation.CONSTRAINTS, {'review_calls': 1}):
            parent, sid = self.seed_context()
            before = usage(self.ledger, sid)
            self.assertEqual(before['used']['calls'], before['limits']['calls'])
            with patch('repository_tools.execute', side_effect=AssertionError('Repository read after budget exhaustion')):
                stopped = self.request('start', prompt=self.resume_request, parent=parent)
            self.assertIn(stopped['state'], orchestration.TERMINAL)
            decisions = [event['kernel']['decision'] for event in orchestration.events(self.ledger, stopped['flow_id'])
                         if event['kernel'] and event['kernel']['profile'] == 'evidence-admission-v1']
            self.assertEqual(decisions, ['COMPOUND_FIN'])
            self.assertEqual(usage(self.ledger, sid), before)
            self.assertEqual(len(self.models), 1)
