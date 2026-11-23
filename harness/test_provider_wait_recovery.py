"""Provider timeouts save the classifier request, without retrying inference."""
import copy
import json
import os
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate
from invocation_gateway import InvocationLedger, consume_dispatch
from model_packet import build
import orchestration_flow as flow
import conversation
import review_budget
import runtime_memory
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
        self.assert_saved_timeout_recovery()

    def test_standalone_auto_greeting_preserves_parent_and_focused_review(self):
        self.ws = self.ws.resolve()
        self.fail_provider = False
        self.fact = 'Remember that Cedar uses ticket CED724.'
        chat_key = 'a' * 64
        with patch.object(self, 'provider', side_effect=AssertionError('Greeting needs no model')):
            cold = self.step(prompt='hello there', armed=False, chat_key=chat_key, tier='auto',
                             models=None, local_greeting=True)
        self.assertEqual(cold['state'], 'DONE')
        self.assertIsNone(cold['local_greeting']['parent'])
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM invocations').fetchone()[0], 0)
        first = self.step(prompt=self.fact, armed=False, chat_key=chat_key)
        parent = first['conversation_id']
        session = first['conversation']['session']
        waiting = self.step(prompt='Review this codebase and give a detailed overview.',
                            chat_key=chat_key, armed=False)
        review = self.step(waiting, chat_key=chat_key, armed=True)
        self.assertEqual(review['state'], 'WORKSPACE')
        task = review['binding']['session']
        spending = {sid: review_budget.usage(self.s, sid) for sid in (session, task)}
        contract = self.s.pinned(task)

        with patch.object(self, 'provider', side_effect=AssertionError('Greeting needs no model')), \
                patch('repository_tools.execute', side_effect=AssertionError('Greeting needs no source reads')):
            done = self.step(prompt='  Hello there!  ', armed=False, chat_key=chat_key,
                             parent=parent, tier='auto', models=None, local_greeting=True)
            self.assertEqual(done['state'], 'DONE')
            self.assertEqual(done['local_greeting']['parent'], parent)
            self.assertIsNone(done['conversation_id'])
            self.assertNotIn('conversation', done)
            self.assertEqual(done['task_stack'], review['task_stack'])
            self.assertEqual(done['focused_flow']['cursor'], review['cursor'])
            self.assertEqual(flow.latest_chat_flow(self.s, self.ws, chat_key)[0]['id'], review['flow_id'])
            row = flow.m.events(self.s, done['flow_id'])[-1]
            self.assertEqual(row['kind'], 'LOCAL_GREETING')
            inspected = self.step(done, event='inspect', chat_key=chat_key)
            self.assertEqual(inspected['local_greeting'], done['local_greeting'])
            with self.assertRaisesRegex(InvalidUpdate, 'belongs to another Sutra chat'):
                self.step(done, event='inspect', chat_key='b' * 64)
            with self.assertRaisesRegex(InvalidUpdate, 'standalone Sutra Auto greeting'):
                self.step(prompt='Hello there! Please review the repo.', armed=False, chat_key=chat_key,
                          tier='auto', models=None, local_greeting=True)
        self.assertEqual(self.s.pinned(task), contract)
        self.assertEqual({sid: review_budget.usage(self.s, sid) for sid in (session, task)}, spending)

    def test_explicit_resume_of_timed_out_greeting_uses_local_response(self):
        from vscode_bridge import handle as bridge
        self.ws = self.ws.resolve()
        self.fail_provider = False
        self.fact = 'Remember that Cedar uses ticket CED724.'
        chat_key = 'a' * 64
        first = self.step(prompt=self.fact, armed=False, chat_key=chat_key)
        session = first['conversation']['session']
        parent = first['conversation_id']
        self.fail_provider = True
        self.phase = 'response_headers'
        paused = self.step(prompt='hello there', parent=parent, armed=False, chat_key=chat_key)
        self.assertEqual(paused['stop']['status'], 'provider_wait')
        failed = paused['stop']['failed_invocation_id']
        charged = review_budget.usage(self.s, session)
        self.assertEqual(charged['used']['calls'], 2)
        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')

        runner = {'version': 1, 'request_id': 'c' * 32, 'request_digest': 'e' * 64}
        request = {'op': 'orchestrate', 'event': 'resume', 'flow_id': paused['flow_id'],
                   'cursor': paused['cursor'], 'chat_key': chat_key, 'tier': 'auto',
                   'armed': False, 'local_greeting': True, 'task_runner': runner}
        with patch('api_runtime.provider_for', side_effect=AssertionError('Greeting needs no model retry')), \
                patch('lifecycle.reconcile', side_effect=AssertionError('Greeting needs no workspace reconciliation')):
            done = bridge(self.root, self.ws, request)
            receipt = done['continuation']['receipt']
            summarized = bridge(self.root, self.ws, {'op': 'orchestrate', 'event': 'runner_summary',
                'flow_id': done['flow_id'], 'cursor': done['cursor'], 'chat_key': chat_key,
                'binding': done['binding'], 'task_runner': runner, 'continuation_receipt': receipt})
        self.assertEqual(done['flow_id'], paused['flow_id'])
        self.assertEqual(done['state'], 'DONE')
        self.assertEqual(done['local_greeting']['parent'], parent)
        self.assertTrue(done['local_greeting']['resumed'])
        self.assertEqual(done['continuation']['next_event'], None)
        self.assertEqual([call['invocation_id'] for call in summarized['runner_usage']['calls']], [failed])
        self.assertIsNone(done['task_stack']['focused'])
        self.assertEqual(flow.latest_chat_flow(self.s, self.ws, chat_key)[0]['id'], first['flow_id'])
        self.assertEqual(review_budget.usage(self.s, session), charged)
        receipt, = self.s.db.execute('SELECT record_json FROM invocation_receipts WHERE session=? AND invocation_id=?',
                                     (session, failed)).fetchone()
        self.assertEqual(json.loads(receipt)['status'], 'quarantined')
        self.assertTrue(self.s.verify_invocations(session))

    def test_saved_explicit_review_skips_repeating_its_timed_out_routing_call(self):
        self.fail_provider = True
        self.phase = 'response_headers'
        chat_key = 'a' * 64
        with patch.object(flow, 'explicit_workspace_review', return_value=None):
            paused = self.step(prompt='Review this codebase and give a detailed overview.',
                               chat_key=chat_key, armed=False)
        self.assertEqual(paused['stop']['status'], 'provider_wait')
        _, data = flow.info(self.s, paused['flow_id'])
        session = data['conversation_session']
        charged = review_budget.usage(self.s, session)
        self.assertEqual(charged['used']['calls'], 1)
        self.assertGreater(charged['used']['tokens'], 0)
        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        with patch.object(self, 'provider', side_effect=AssertionError('No routing retry needed')):
            resumed = self.step(paused, event='resume', chat_key=chat_key, armed=False, tier='high')
            waiting = self.step(resumed, chat_key=chat_key, armed=False, tier='high')
        self.assertEqual(waiting['state'], 'NEED_ARM')
        self.assertEqual(waiting['flow_id'], paused['flow_id'])
        self.assertIsNone(waiting['conversation_id'])
        self.assertEqual(review_budget.usage(self.s, session), charged)
        self.assertTrue(self.s.verify_invocations(session))

    def assert_saved_timeout_recovery(self):
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

    def test_shared_runtime_timeout_resumes_saved_workflow_with_resident_model(self):
        original_provider = self.provider
        observations = []
        metadata = {'details': {'format': 'gguf', 'runner': 'ggml'},
                    'model_info': {'general.architecture': 'qwen3',
                                   'qwen3.context_length': 32768}}
        healthy = {'available': True, 'admitted': True, 'decision': 'ALLOW',
                   'sample': {'wall_time': 1, 'free_bytes': 64 * runtime_memory.GIB,
                              'inactive_bytes': 0}}
        resident = [{'name': name, 'digest': 'fixture-' + name,
                     'size': runtime_memory.GIB, 'context_length': 32768,
                     'runner': 'ggml', 'expires_at': '9999-01-01T00:00:00Z'}
                    for name in (MODELS['low'], MODELS['high'])]

        def provider(stage):
            model = original_provider(stage)
            model.port = 11434
            model.memory_weights = runtime_memory.GIB
            model.context_metadata = metadata
            call = model.call

            def dispatched(cid, system, packet, schema):
                try:
                    with runtime_memory.dispatch(model, self.root, cid, system, packet, schema):
                        runtime_memory.mark_transport_started(model, cid)
                        result = call(cid, system, packet, schema)
                        model.runtime_completed = True
                        return result
                finally:
                    observations.append((model.model, runtime_memory.read_state()))

            model.call = dispatched
            return model

        self.provider = provider
        environment = {'SUTRA_RUNTIME_DIR': str(self.root / 'runtime'),
                       'SUTRA_MEMORY_OWNERSHIP': 'shared'}
        with patch.dict(os.environ, environment), \
                patch.object(runtime_memory, 'fresh_resources', return_value=healthy), \
                patch.object(runtime_memory, 'running', return_value=resident), \
                patch.object(runtime_memory, 'api', side_effect=AssertionError('Unexpected HTTP')) as api:
            # Reuse the full saved-workflow assertions, including ledger reload,
            # immutable receipts, pinned context and every consumed budget.
            self.assert_saved_timeout_recovery()
            api.assert_not_called()
            self.assertEqual([model for model, _ in observations],
                             [MODELS['low'], MODELS['low'], MODELS['high']])
            interrupted = observations[1][1]
            self.assertTrue(interrupted['active']['uncertain'])
            self.assertEqual(interrupted['interrupted']['latest_model'], MODELS['low'])
            self.assertFalse(interrupted['interrupted']['backend_completion_verified'])
            final = runtime_memory.read_state()
            self.assertIsNone(final['active'])
            self.assertIsNone(final['owned'])
            self.assertEqual(final['interrupted'], interrupted['interrupted'])
            self.assertEqual(final['plan']['model'], MODELS['high'])
            self.assertEqual(final['plan']['decision'], 'ALLOW')

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
