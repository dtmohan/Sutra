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
                self.assertIn('VAL-271', canonical(packet['memory']['pinned_user_statements']))
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
