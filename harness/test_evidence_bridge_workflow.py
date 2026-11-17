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


class EvidenceBridgeWorkflowTests(unittest.TestCase):
    chat_key = 'a' * 64
    resume_request = 'Can you look at the résumé now and comment on post-silicon validation roles?'
    document = 'Document text:\nValidated PCIe on first silicon and reduced triage time by 17%.'

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
