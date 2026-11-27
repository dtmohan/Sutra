"""A conversation proposal is bound to one offered file and performs no work."""
import copy
from pathlib import Path
import tempfile
import unittest

import conversation
from context_manager import InvalidUpdate
from invocation_gateway import InvocationLedger, consume_dispatch
from model_packet import build
import orchestration
from review_budget import usage
from test_conversation import Fake, MODELS


PROMPT = 'Would the watch still be readable on a phone at a 390 px viewport?'
ENTRY = 'index.html'
QUESTION = 'Does the watch canvas actually fit a 390 px viewport?'
CHAT = 'a' * 64


class CompactModel(Fake):
    model_interface = 1

    def __init__(self, output):
        super().__init__()
        self.output = output

    def call(self, cid, system, payload, schema):
        self.packet = build(system, payload, schema)
        consume_dispatch(cid, self.model, system, payload, schema, packet=self.packet)
        self.payloads.append(payload)
        return self.output, {'ok': True, 'response': {'done': True, 'prompt_eval_count': 330, 'eval_count': 60}}


class ConversationBrowserProbeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name)
        self.workspace = directory / 'workspace'
        self.workspace.mkdir()
        self.storage = directory / 'storage'
        self.storage.mkdir()
        self.ledger = InvocationLedger(self.storage / 'state.sqlite')
        self.addCleanup(self.ledger.close)
        self.axis = conversation.chat_axis(self.workspace, CHAT)
        self.sid = conversation.open_session(self.ledger, PROMPT, self.axis)
        self.inquiry = self.offer(version=1)
        self.request = {'prompt': PROMPT, 'tier': 'low', 'models': MODELS,
                        'conversation_session': self.sid, 'browser_inquiry': self.inquiry}
        self.meta = {'workspace': str(self.workspace)}

    def offer(self, version):
        with self.ledger.db:
            flow = orchestration.create(self.ledger, 'flow', workspace=str(self.workspace),
                                        chat_key=CHAT, prompt=PROMPT, conversation_parent=None)
            inquiry = {'version': version, 'entry': ENTRY, 'flow_id': flow['id']}
            orchestration.transition(self.ledger, flow['id'], 'CLASSIFYING', 'CONVERSATION_BOUND',
                                     {'conversation_session': self.sid})
            orchestration.transition(self.ledger, flow['id'], 'CLASSIFYING', 'BROWSER_INQUIRY_OFFERED',
                                     {'browser_inquiry': inquiry})
        return inquiry

    def ask(self, provider, request=None, *, orchestrated=True):
        return conversation.answer(self.ledger, self.meta, self.storage, request or self.request,
                                   provider, axis=self.axis, orchestrated=orchestrated)

    def test_scoped_probe_is_an_optional_single_call_with_no_work(self):
        def propose(result, payload):
            result.update(intent='workspace', answer='I would check what fits on a phone.',
                          browser_probe={'entry': ENTRY, 'working_question': QUESTION})

        model = Fake(fault=propose)
        out = self.ask(model)
        self.assertTrue(out['result']['admission']['admitted'])
        self.assertFalse(out['workspace_tools_used'])
        self.assertEqual(out['result']['accepted_output']['browser_probe'],
                         {'entry': ENTRY, 'working_question': QUESTION})
        self.assertIsNone(out['routing_followup'])
        self.assertEqual(usage(self.ledger, self.sid)['used']['calls'], 1)
        self.assertEqual(usage(self.ledger, self.sid)['used']['operations'], 0)
        self.assertNotIn('active', self.meta)

        payload = model.payloads[0]
        wire = build(conversation.SYSTEM, payload, conversation.schema(payload))
        fmt = wire['schema']
        note = wire['messages'][0]['content']
        self.assertEqual(fmt['properties']['browser_probe']['properties']['entry']['enum'], [ENTRY])
        self.assertNotIn('browser_probe', fmt['required'])
        self.assertIn('"browser_inquiry":{"entry":"index.html"}', note)
        self.assertNotIn(self.inquiry['flow_id'], note)
        self.assertNotIn('Browser testing and arbitrary package installation are not available.', note)
        self.assertTrue(self.ledger.verify_invocations(self.sid))

    def test_fresh_answer_can_use_the_current_evidence_without_a_proposal(self):
        out = self.ask(Fake())
        self.assertEqual(out['result']['accepted_output']['intent'], 'conversation')
        self.assertNotIn('browser_probe', out['result']['accepted_output'])
        ordinary = {key: value for key, value in self.request.items() if key != 'browser_inquiry'}
        model = Fake()
        self.ask(model, ordinary)
        payload = model.payloads[0]
        wire = build(conversation.SYSTEM, payload, conversation.schema(payload))
        self.assertNotIn('browser_probe', wire['schema']['properties'])
        self.assertNotIn('browser_inquiry', wire['messages'][0]['content'])

    def test_compact_feedback_offers_no_legacy_actions_and_attaches_host_intent(self):
        request = dict(self.request, browser_inquiry=self.offer(version=2))
        answer = {'answer': 'The fixed drawing size alone is inconclusive.', 'summary': 'Viewport fit is uncertain.'}
        model = CompactModel(dict(answer, browser_probe={'entry': ENTRY, 'working_question': QUESTION}))
        probe = self.ask(model, request)['result']['accepted_output']
        self.assertEqual(probe['intent'], 'workspace')
        self.assertEqual(probe['browser_probe']['working_question'], QUESTION)
        self.assertEqual(set(model.packet['schema']['properties']), {'answer', 'summary', 'browser_probe'})
        instructions = model.packet['messages'][0]['content']
        self.assertNotIn('workspace_request', instructions)
        self.assertNotIn('browser testing and arbitrary package installation', instructions.lower())
        self.assertIn('browser_probe', instructions)
        result = self.ask(CompactModel(answer), request)['result']['accepted_output']
        self.assertEqual(result['intent'], 'conversation')
        self.assertEqual(result['escalation_reason'], 'none')
        self.assertEqual(result['page_requests'], [])
        self.assertIsNone(result['diagram'])
        self.assertNotIn('browser_probe', result)
        self.assertTrue(self.ledger.verify_invocations(self.sid))

    def test_unoffered_target_or_direct_request_cannot_inject_a_capability(self):
        with self.assertRaisesRegex(InvalidUpdate, 'requires an offer'):
            self.ask(Fake(), orchestrated=False)
        tampered = dict(self.request, browser_inquiry=dict(self.inquiry, entry='archive/index.html'))
        with self.assertRaisesRegex(InvalidUpdate, 'differs from its host offer'):
            self.ask(Fake(), tampered)
        model = Fake()
        self.ask(model)
        payload = copy.deepcopy(model.payloads[0])
        payload['conversation_axis'] = conversation.chat_axis(self.workspace, 'b' * 64)
        with self.assertRaisesRegex(InvalidUpdate, 'belongs to another conversation'):
            conversation.check_browser_inquiry(self.ledger, self.sid, payload)

    def test_proposals_cannot_compete_with_other_work_or_name_two_questions(self):
        model = Fake()
        out = self.ask(model)
        payload = model.payloads[0]
        proposal = dict(out['result']['accepted_output'], intent='workspace',
                        browser_probe={'entry': ENTRY, 'working_question': QUESTION})
        with self.assertRaisesRegex(InvalidUpdate, 'on its own'):
            conversation.check_delivery(payload, dict(proposal, workspace_request={'operation': 'read', 'paths': [ENTRY]}))
        probe = dict(proposal['browser_probe'], working_question='Does it fit? Do controls work?')
        with self.assertRaisesRegex(InvalidUpdate, 'one short browser question'):
            conversation.check_delivery(payload, dict(proposal, browser_probe=probe))


if __name__ == '__main__':
    unittest.main()
