"""Provider timeouts save the classifier request, without retrying inference."""
import copy
import json
import unittest

from context_manager import InvalidUpdate
from invocation_gateway import InvocationLedger, consume_dispatch
from model_packet import build
import orchestration_flow as flow
import conversation
import review_budget
import test_orchestration as fixture
from test_conversation import Fake, MODELS


class TimedOut(Fake):
    model_interface = 1

    def __init__(self, model, phase='response_headers'):
        super().__init__(model)
        self.phase = phase

    def call(self, cid, system, packet, schema):
        consume_dispatch(cid, self.model, system, packet, schema,
                         packet=build(system, packet, schema))
        self.payloads.append(copy.deepcopy(packet))
        error = TimeoutError('timed out')
        error.call_record = {'call_id': cid, 'ok': False,
                             'transport': {'version': 1, 'phase': self.phase,
                                                   'headers_received': self.phase == 'stream',
                                                   'frames_received': 2 if self.phase == 'stream' else 0,
                                                   'content_bytes': 37 if self.phase == 'stream' else 0,
                                                   'reasoning_bytes': 11 if self.phase == 'stream' else 0}}
        if self.phase is None:
            error.call_record.pop('transport')
        raise error


class ProviderWaitRecoveryTests(unittest.TestCase):
    setUp = fixture.OrchestrationTests.setUp
    tearDown = fixture.OrchestrationTests.tearDown
    step = fixture.OrchestrationTests.step

    def provider(self, stage):
        self.assertEqual(stage, 'conversation')
        if self.fail_provider:
            model = TimedOut(MODELS[self.tier], self.phase)
        else:
            model = Fake(MODELS[self.tier], self.reply)
        self.models.append((stage, model))
        return model

    def reply(self, result, packet):
        result['summary'] = packet['request']
        result['answer'] = 'Hello! I am ready to help.'
        if packet['request'] == self.fact:
            result['facts'] = [self.fact]

    def test_charged_timeout_reloads_and_resumes_same_parent_on_another_model(self):
        self.fail_provider = False
        self.phase = 'response_headers'
        self.fact = 'Remember that Cedar uses ticket CED724.'
        chat_key = 'a' * 64
        first = self.step(prompt=self.fact, armed=False, chat_key=chat_key)
        parent = first['conversation_id']
        sid = first['conversation']['session']
        contract = conversation.contract(self.s, sid)
        before = review_budget.usage(self.s, sid)

        self.fail_provider = True
        greeting = 'Hello Karma - how are you?'
        paused = self.step(prompt=greeting, parent=parent, armed=False, chat_key=chat_key)
        self.assertEqual(paused['state'], 'PAUSED')
        stop = paused['stop']
        self.assertEqual(stop['status'], 'provider_wait')
        self.assertEqual(stop['resume_phase'], 'CLASSIFYING')
        self.assertIs(stop['automatic_retry'], False)
        self.assertIsNone(paused['conversation_id'])
        self.assertNotIn('conversation', paused)
        failed = stop['failed_invocation_id']
        _, data = flow.info(self.s, paused['flow_id'])
        self.assertEqual(data['conversation_session'], sid)
        self.assertFalse(data.get('routing_continuation'))
        self.assertEqual(flow.m.node(self.s, paused['flow_id'])['conversation_parent'], parent)
        self.assertEqual(self.models[-1][1].payloads[0]['request'], greeting)
        self.assertEqual(paused['claim_review_usage']['total_calls'], 1)
        self.assertEqual(json.loads(self.s.db.execute('SELECT record_json FROM invocation_receipts WHERE session=? AND invocation_id=?',(sid,failed)).fetchone()[0])['status'], 'quarantined')
        charged = review_budget.usage(self.s, sid)
        self.assertEqual(charged['used']['calls'], before['used']['calls'] + 1)
        self.assertGreater(charged['used']['tokens'], before['used']['tokens'])
        self.assertEqual(charged['limits'], before['limits'])
        self.assertEqual(conversation.contract(self.s, sid), contract)
        prefix = self.s.db.execute('SELECT id,record_json FROM invocation_receipts WHERE session=? ORDER BY seq', (sid,)).fetchall()
        with self.assertRaisesRegex(InvalidUpdate, 'Workflow paused'):
            self.step(paused, armed=False, chat_key=chat_key)
        self.assertEqual(review_budget.usage(self.s, sid), charged)

        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        self.fail_provider = False
        self.tier = 'high'
        resumed = self.step(paused, event='resume', armed=False, chat_key=chat_key)
        self.assertEqual(resumed['state'], 'CLASSIFYING')
        self.assertEqual(review_budget.usage(self.s, sid), charged)
        done = self.step(resumed, armed=False, chat_key=chat_key)
        self.assertEqual(done['state'], 'DONE')
        self.assertEqual(done['conversation']['session'], sid)
        packet = self.models[-1][1].payloads[0]
        self.assertEqual(packet['request'], greeting)
        self.assertEqual(packet['contract'], contract)
        self.assertEqual(packet['memory']['invocation_id'], parent)
        self.assertIn(self.fact, [row['quote'] for row in packet['memory']['pinned_user_statements']])
        self.assertEqual(self.models[-1][1].model, MODELS['high'])
        self.assertEqual(self.s.db.execute('SELECT id,record_json FROM invocation_receipts WHERE session=? ORDER BY seq', (sid,)).fetchall()[:len(prefix)], prefix)
        final = review_budget.usage(self.s, sid)
        self.assertEqual(final['used']['calls'], charged['used']['calls'] + 1)
        self.assertGreater(final['used']['tokens'], charged['used']['tokens'])
        self.assertEqual(final['used']['source_bytes'], charged['used']['source_bytes'])
        self.assertEqual(final['used']['operations'], charged['used']['operations'])
        self.assertEqual(final['limits'], charged['limits'])
        self.assertTrue(self.s.verify_invocations(sid))

    def test_non_timeout_rejection_keeps_existing_terminal_behavior(self):
        self.fail_provider = False
        self.fact = ''
        self.reply = lambda result, packet: result.update(answer='')
        out = self.step(prompt='Hello', armed=False)
        self.assertEqual(out['state'], 'DONE')
        self.assertFalse(out['conversation']['result']['admission']['admitted'])
        self.assertIsNone(out['stop'])

    def test_stream_timeout_does_not_claim_generation_never_started(self):
        self.fail_provider = True
        self.phase = 'stream'
        self.fact = ''
        out = self.step(prompt='Hello', armed=False)
        self.assertEqual(out['state'], 'PAUSED')
        stop = out['stop']
        self.assertEqual(stop['status'], 'provider_wait')
        self.assertIn('response stream', stop['error'])
        self.assertNotIn('generation was not observed', stop['error'])
        self.assertEqual(stop['transport_failure']['content_bytes'], 37)
        self.assertEqual(stop['transport_failure']['reasoning_bytes'], 11)
        self.assertEqual(len(self.models), 1)
        sid = flow.info(self.s, out['flow_id'])[1]['conversation_session']
        receipt = json.loads(self.s.db.execute('SELECT record_json FROM invocation_receipts WHERE session=?', (sid,)).fetchone()[0])
        self.assertEqual(receipt['status'], 'quarantined')
        self.assertIsNone(receipt['proposal'])
        self.assertEqual(review_budget.usage(self.s, sid)['used']['calls'], 1)

    def test_legacy_timeout_without_phase_does_not_invent_a_queue_cause(self):
        self.fail_provider = True
        self.phase = None
        self.fact = ''
        out = self.step(prompt='Hello', armed=False)
        self.assertEqual(out['state'], 'PAUSED')
        self.assertIn('Queueing and generation state are unknown', out['stop']['error'])
        self.assertNotIn('transport_failure', out['stop'])
        self.assertEqual(len(self.models), 1)


if __name__ == '__main__':
    unittest.main()
