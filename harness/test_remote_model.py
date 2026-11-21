import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate, canonical, sha
from invocation_gateway import _permit, request_hash
from local_model import OutputLimitExceeded, StructuredOutputRejected
from remote_model import RemoteModel, _json_answer
import remote_profiles
from test_remote_profiles import descriptor


def stream(answer='{"answer":"done"}', *, finish='stop', usage=True, done=True,
           tool=False, counts=(91, 34)):
    events = [dict(choices=[dict(delta={'reasoning_content': 'Observed evidence. '}, finish_reason=None)])]
    delta = {'tool_calls': [{'id': 'not-a-host-action'}]} if tool else {'content': answer}
    events.append(dict(choices=[dict(delta=delta, finish_reason=finish)]))
    if usage:
        events.append(dict(choices=[], usage={'prompt_tokens': counts[0], 'completion_tokens': counts[1]},
                           timings={'prompt_n': counts[0], 'predicted_n': counts[1]}))
    data = b''.join(b'data: ' + json.dumps(event).encode() + b'\n\n' for event in events)
    return data + (b'data: [DONE]\n\n' if done else b'')


class RemoteTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calls = []
        cls.reply = stream()
        cls.status = 200

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                cls.calls.append({'path': self.path, 'body': body,
                                  'authorization': self.headers.get('Authorization')})
                if self.path == '/v1/chat/completions/input_tokens':
                    result = b'{"object":"response.input_tokens","input_tokens":91}'
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(result)))
                    self.end_headers()
                    self.wfile.write(result)
                    return
                self.send_response(cls.status)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(cls.reply)))
                self.end_headers()
                self.wfile.write(cls.reply)

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.worker.join(2)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.registry = self.root / 'registry.json'
        remote_profiles.write({'rtx3090': descriptor(self.root, port=self.server.server_port)}, self.registry)
        env = patch.dict(os.environ, SUTRA_REMOTE_REGISTRY=str(self.registry),
                         SUTRA_REMOTE_MODELS='["sutra:remote/rtx3090"]', SUTRA_THINKING='adaptive')
        env.start(); self.addCleanup(env.stop)
        guard = patch('remote_control.verify_server', return_value={'verified': True})
        guard.start(); self.addCleanup(guard.stop)
        tunnel = patch('remote_control.ensure_tunnel', return_value={'ready': True, 'owned': True})
        tunnel.start(); self.addCleanup(tunnel.stop)
        instance = patch('remote_memory.verify_instance', return_value=True)
        instance.start(); self.addCleanup(instance.stop)
        fresh = patch('remote_memory.require_dispatch_fresh', return_value=True)
        fresh.start(); self.addCleanup(fresh.stop)
        self.provider = RemoteModel(self.root / 'calls', 'sutra:remote/rtx3090')
        self.provider.runtime_context_tokens = 8192
        self.provider.remote_instance = {'pid': 42, 'boot_id': 'observed-test-boot'}
        type(self).calls = []
        type(self).reply = stream()
        type(self).status = 200
        self.system = 'Transport test system'
        self.payload = {'request': 'Return the observed answer.'}
        self.schema = {'type': 'object', 'properties': {'answer': {'type': 'string'}},
                       'required': ['answer'], 'additionalProperties': False}

    def call(self, permit=None):
        permit = permit if permit is not None else {
            'hash': request_hash('call', self.provider.model, self.system, self.payload, self.schema), 'used': False}
        token = _permit.set(permit)
        try:
            return self.provider.call('call', self.system, self.payload, self.schema)
        finally:
            _permit.reset(token)

    def test_real_sse_uses_gateway_permit_schema_actual_usage_and_private_auth(self):
        answer, record = self.call()
        self.assertEqual(answer, {'answer': 'done'})
        self.assertEqual(record['provider'], 'sutra_remote')
        self.assertEqual(record['response']['prompt_eval_count'], 91)
        self.assertEqual(record['response']['eval_count'], 34)
        self.assertEqual(record['transport']['phase'], 'complete')
        self.assertEqual(record['response']['message']['thinking'], 'Observed evidence. ')
        request = self.calls[0]
        self.assertEqual(request['authorization'], 'Bearer unit-test-remote-credential')
        self.assertEqual(request['path'], '/v1/chat/completions')
        self.assertEqual(request['body']['response_format']['json_schema']['schema'], self.schema)
        self.assertEqual(request['body']['max_tokens'], 2048)
        self.assertEqual(request['body']['temperature'], 0.6)
        self.assertNotIn('reasoning_budget_tokens', request['body'])
        self.assertNotIn('unit-test-remote-credential', json.dumps(record))
        trace = (self.root / 'calls/call.json').read_text()
        self.assertNotIn('unit-test-remote-credential', trace)
        self.assertNotIn('Return the observed answer', trace)

    def test_no_permit_or_replayed_permit_cannot_open_an_inference_request(self):
        with self.assertRaisesRegex(InvalidUpdate, 'gateway permit'):
            self.provider.call('call', self.system, self.payload, self.schema)
        self.assertEqual(self.calls, [])
        permit = {'hash': request_hash('call', self.provider.model, self.system, self.payload, self.schema), 'used': False}
        self.call(permit)
        with self.assertRaisesRegex(InvalidUpdate, 'gateway permit'):
            self.call(permit)
        self.assertEqual(len(self.calls), 1)

    def test_output_exhaustion_preserves_reasoning_and_measured_spending(self):
        type(self).reply = stream('', finish='length', counts=(91, 2048))
        with self.assertRaises(OutputLimitExceeded) as caught:
            self.call()
        record = caught.exception.call_record
        self.assertFalse(record['ok'])
        self.assertEqual(record['response']['eval_count'], 2048)
        self.assertTrue(record['response']['message']['thinking'])
        self.assertNotIn('answer', record)

    def test_missing_terminal_usage_or_done_is_not_a_completed_answer(self):
        for options in ({'usage': False}, {'done': False}, {'counts': (True, 12)}):
            type(self).reply = stream(**options)
            with self.subTest(options=options), self.assertRaises(RuntimeError) as caught:
                self.call()
            self.assertNotIn('answer', caught.exception.call_record)
            self.assertFalse(caught.exception.call_record['ok'])

    def test_transport_tool_calls_are_never_executed(self):
        type(self).reply = stream(tool=True)
        with self.assertRaisesRegex(RuntimeError, 'tool call'):
            self.call()

    def test_invalid_json_is_repairable_with_completed_usage(self):
        type(self).reply = stream('Not structured JSON')
        with self.assertRaises(StructuredOutputRejected) as caught:
            self.call()
        self.assertEqual(caught.exception.call_record['response']['eval_count'], 34)

    def test_http_failure_never_echoes_the_remote_body_or_key(self):
        type(self).status = 401
        type(self).reply = b'PRIVATE_SERVER_ERROR unit-test-remote-credential'
        with self.assertRaisesRegex(RuntimeError, 'HTTP 401') as caught:
            self.call()
        self.assertNotIn('PRIVATE_SERVER_ERROR', str(caught.exception))
        self.assertNotIn('unit-test-remote-credential', json.dumps(caught.exception.call_record))

    def test_profile_change_and_unassigned_model_do_not_post(self):
        with patch.dict(os.environ, SUTRA_REMOTE_MODELS='[]'):
            with self.assertRaisesRegex(InvalidUpdate, 'not assigned'):
                self.call()
        remote_profiles.write({'rtx3090': descriptor(self.root, port=self.server.server_port,
                                                     context_tokens=16384)}, self.registry)
        with self.assertRaisesRegex(InvalidUpdate, 'profile changed'):
            self.provider.fresh_context_metadata()
        self.assertEqual(self.calls, [])

    def test_overspending_cannot_be_hidden_in_a_successful_final_frame(self):
        type(self).reply = stream(counts=(91, 2049))
        with self.assertRaisesRegex(RuntimeError, 'allocation') as caught:
            self.call()
        self.assertEqual(caught.exception.call_record['response']['eval_count'], 2049)
        self.assertNotIn('answer', caught.exception.call_record)

    def test_remote_instance_replacement_withholds_answer_without_discarding_usage(self):
        with patch('remote_memory.verify_instance', side_effect=[True, InvalidUpdate('Remote instance changed')]):
            with self.assertRaisesRegex(InvalidUpdate, 'instance changed') as caught:
                self.call()
        self.assertEqual(caught.exception.call_record['response']['eval_count'], 34)
        self.assertNotIn('answer', caught.exception.call_record)

    def test_instance_change_or_stale_admission_before_post_never_starts_inference(self):
        from monitoring import ResourceWait
        with patch('remote_memory.verify_instance', side_effect=InvalidUpdate('Remote instance changed')):
            with self.assertRaises(InvalidUpdate):
                self.call()
        self.assertEqual(self.calls, [])
        state = {'decision': 'BRIDGE', 'reason': 'Fresh remote observation required',
                 'available': False, 'admitted': False}
        with patch('remote_memory.require_dispatch_fresh', side_effect=ResourceWait(state)):
            with self.assertRaises(ResourceWait) as caught:
                self.call()
        self.assertEqual(self.calls, [])
        self.assertGreater(caught.exception.call_record['conservative_input_charge'], 0)
        self.assertNotIn('response', caught.exception.call_record)

    def test_actual_thinking_stream_remains_visible_when_no_switch_is_supported(self):
        from unittest.mock import Mock
        from model_activity import ACTIVE
        activity = Mock()
        token = ACTIVE.set(activity)
        try:
            _, record = self.call()
        finally:
            ACTIVE.reset(token)
        activity.thinking.assert_called_once_with('Observed evidence. ')
        self.assertEqual(self.provider.context_metadata['thinking']['values'], [])
        self.assertFalse(record['reasoning_control']['switch_supported'])

    def recovered_parent(self):
        import chat_runtime
        from paging import Budget
        import remote_context

        identity = remote_context.PARENT_REASONING_IDENTITY
        profile = descriptor(self.root, port=self.server.server_port,
                             executable_sha256=identity['executable_sha256'], model_sha256=identity['model_sha256'])
        remote_profiles.write({'rtx3090': profile}, self.registry)
        self.provider = RemoteModel(self.root / 'calls', 'sutra:remote/rtx3090', review=True)
        self.provider.runtime_context_tokens = 8192
        self.provider.remote_instance = {'pid': 42, 'boot_id': 'observed-test-boot'}
        props = {'default_generation_settings': {'n_ctx': 8192},
                 'chat_template': 'Test pinned reasoning template <think> content </think>.'}
        for guard in (patch('remote_control.verify_server', return_value={'props': props}),
                      patch.dict(identity, template_sha256=sha(props['chat_template']))):
            guard.start()
            self.addCleanup(guard.stop)
        window = remote_context._descriptor(self.provider.profile, props)
        self.provider.context_metadata['_sutra_context_window'] = window
        self.provider.budget = Budget(8192, 4096, context_profile=2,
                                      reservation_limit=65536, context_window=window)
        self.system = chat_runtime.SYSTEM
        self.payload = {'request': 'Review the workspace architecture.',
                        'contract': {'goal': 'Review the architecture', 'task_id': 'test', 'constraints': {}},
                        'excerpts': {'a.py': 'def example(): pass'}, 'constraint_echo': [],
                        'report_schema_version': 2, 'parent_repair': {'version': 1},
                        'parent_draft_recovery': {'version': 1, 'failed_invocation': 'a' * 32,
                                                  'failed_receipt': 'b' * 64, 'reasoning_budget_tokens': 2048}}
        self.schema = chat_runtime.schema(self.payload)
        return props

    def test_recovered_parent_binds_same_native_reasoning_budget_for_count_and_generation(self):
        import remote_context
        self.recovered_parent()
        self.assertTrue(remote_context.supports_parent_output_recovery(self.provider.budget))
        proof = remote_context.check(self.provider.budget, self.system, self.payload, self.schema)
        self.provider.context_measurement = proof
        _, record = self.call()
        count_request, generation_request = self.calls
        self.assertEqual(count_request['path'], remote_context.COUNT_PATH)
        self.assertEqual(generation_request['path'], '/v1/chat/completions')
        self.assertEqual(count_request['body'], generation_request['body'])
        body = generation_request['body']
        self.assertEqual(proof['wire_sha256'], sha(canonical(body)))
        self.assertEqual(proof['reasoning_budget_tokens'], 2048)
        self.assertEqual(record['reasoning_control']['reasoning_budget_tokens'], 2048)
        self.assertEqual(body['reasoning_budget_tokens'], 2048)
        self.assertEqual(body['max_tokens'], 4096)
        self.assertNotIn('parent_draft_recovery', canonical(body['messages']))

    def test_recovered_parent_refuses_altered_proof_or_live_template_before_generation(self):
        import remote_context
        props = self.recovered_parent()
        proof = remote_context.check(self.provider.budget, self.system, self.payload, self.schema)
        self.provider.context_measurement = dict(proof, reasoning_budget_tokens=8192)
        with self.assertRaisesRegex(InvalidUpdate, 'proof differs from its reasoning budget'):
            self.call()
        self.provider.context_measurement = proof
        props['chat_template'] = 'A different template with <think> tags </think>.'
        with self.assertRaisesRegex(InvalidUpdate, 'reasoning template changed'):
            self.call()
        self.assertEqual([request['path'] for request in self.calls], [remote_context.COUNT_PATH])

    def test_reasoning_budget_requires_recovered_parent_role_and_pinned_exact_context(self):
        from paging import Budget
        import remote_context
        self.recovered_parent()
        budget = self.provider.budget
        for field, update in (('executable_sha256', '0' * 64), ('template_sha256', '0' * 64),
                              ('model_sha256', '0' * 64)):
            with self.subTest(field=field):
                changed_window = dict(budget.context_window, **{field: update})
                changed = Budget(8192, 4096, context_profile=2,
                                 reservation_limit=65536, context_window=changed_window)
                self.assertFalse(remote_context.supports_parent_output_recovery(changed))
                with self.assertRaisesRegex(InvalidUpdate, 'verified recovered parent draft'):
                    remote_context.check(changed, self.system, self.payload, self.schema)
        with self.assertRaisesRegex(InvalidUpdate, 'verified recovered parent draft'):
            remote_context.check(budget, 'Different role', self.payload, self.schema)
        with self.assertRaisesRegex(InvalidUpdate, 'verified recovered parent draft'):
            remote_context.check(budget, self.system, dict(self.payload, parent_repair=None), self.schema)
        self.assertEqual(self.calls, [])

    def test_strict_json_rejects_duplicate_nonfinite_and_fragment_salvage(self):
        for value in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":1e999}', 'Before {"x":1}', '[1]'):
            with self.subTest(value=value), self.assertRaises(StructuredOutputRejected):
                _json_answer(value)


if __name__ == '__main__':
    unittest.main()
