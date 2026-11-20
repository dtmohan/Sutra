"""Exercise the approved transport and ledger against a bounded HTTP/1.0 fixture."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import conversation
from context_manager import InvalidUpdate, canonical, sha
from invocation_gateway import InvocationLedger
from karma_adapter import KarmaAdapter
from local_model import StructuredOutputRejected
from model_routing import choose
from review_budget import usage
from strata_model import (HOST_JSON_DIRECTIVE, HOST_JSON_DIRECTIVE_BYTES, MODEL, StrataModel,
                          _host_json_answer, _host_json_text, _stream, append_catalog)


MODELS = {'low': 'installed-low', 'mid': 'installed-mid', 'high': MODEL}
SERVER_MODEL = 'qwen3.8-flash-next-q2_0'
HELLO = {'page_requests': [], 'intent': 'conversation', 'escalation_reason': 'none',
         'diagram': None, 'answer': 'Hello. How can I help?',
         'summary': 'A hello exchange.'}
PROMPT_VALIDATION = {'formats': ['json_object', 'json_schema'], 'method': 'prompt_and_validate',
                     'constrained_decoding': False, 'stream_buffered': True}


def frames(answer=HELLO, finish='stop', measured=True):
    initial = {'choices': [{'delta': {'role': 'assistant'}, 'finish_reason': None}]}
    text = {'choices': [{'delta': {'content': json.dumps(answer)}, 'finish_reason': None}]}
    final = {'choices': [{'delta': {}, 'finish_reason': finish}],
             'timings': {'predicted_per_second': 25.0}}
    if measured:
        final['usage'] = {'prompt_tokens': 124, 'completion_tokens': 30}
    return [initial, text, final]


def text_frames(text, finish='stop'):
    result = frames(finish=finish)
    result[1]['choices'][0]['delta']['content'] = text
    return result


class BenchmarkServer(BaseHTTPRequestHandler):
    def log_message(self, format_string, *args):
        pass

    def do_GET(self):
        if self.path != '/v1/status':
            self.send_error(404)
            return
        raw = json.dumps(self.server.status).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        if self.path != '/v1/chat/completions':
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.posts.append((body, self.headers.get('Authorization')))
        if self.server.post_error:
            self.send_error(401, self.headers['Authorization'])
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        self.wfile.write(b': keep-alive\n\n')
        next_frames = self.server.frames.pop(0) if self.server.frames else frames()
        for event in next_frames:
            self.wfile.write(b'data: ' + json.dumps(event).encode() + b'\n\n')
        self.wfile.write(b'data: [DONE]\n\n')


class StrataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = InvocationLedger(self.root / 'state.sqlite')
        self.addCleanup(self.ledger.close)
        self.server = HTTPServer(('127.0.0.1', 0), BenchmarkServer)
        self.server.status = {'service': 'strata', 'model': SERVER_MODEL, 'started': 42, 'loaded': True,
                              'engine': 'fixture-0.2.15', 'context': {'max_positions': 32768},
                              'activity': {'in_flight': 0}}
        self.server.frames, self.server.posts = [], []
        self.server.post_error = False
        worker = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        worker.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        env = {'SUTRA_STRATA_PORT': str(self.server.server_port), 'SUTRA_STRATA_MODEL': SERVER_MODEL,
               'SUTRA_STRATA_CONTEXT': '16384', 'SUTRA_STRATA_API_KEY': 'fixture-token',
               'SUTRA_THINKING': 'off', 'SUTRA_RUNTIME_DIR': ''}
        config = patch.dict(os.environ, env)
        config.start()
        self.addCleanup(config.stop)
        now = time.time()
        hardware = KarmaAdapter().hardware({
            'profile': 'portable-memory-v1', 'wall_time': now,
            'physical_memory_bytes': 64 * 1024 ** 3, 'available_bytes': 12 * 1024 ** 3,
        }, now)
        guard = patch('monitoring.require_resources', return_value=hardware)
        guard.start()
        self.addCleanup(guard.stop)

    def ask(self, prompt='Hello', provider=None):
        with patch('vscode_bridge.ollama', side_effect=AssertionError('Unexpected Ollama RPC from Strata')):
            return conversation.answer(self.ledger, {}, self.root,
                                       {'prompt': prompt, 'models': MODELS, 'tier': 'high'}, provider=provider)

    def test_explicit_high_uses_gateway_and_charges_measured_usage(self):
        result = self.ask()
        self.assertTrue(result['result']['admission']['admitted'])
        self.assertEqual(result['result']['accepted_output']['answer'], HELLO['answer'])
        self.assertEqual(result['model_capacity']['effective_context_tokens'], 16384)
        self.assertEqual(result['model_capacity']['source'], 'strata_metadata')
        request, auth = self.server.posts[0]
        self.assertEqual(auth, 'Bearer fixture-token')
        self.assertEqual(request['model'], SERVER_MODEL)
        self.assertEqual(request['reasoning_effort'], 'none')
        self.assertEqual(request['max_tokens'], 2048)
        self.assertEqual(request['response_format']['type'], 'json_schema')
        self.assertNotIn('context_binding', request['response_format']['json_schema']['schema']['properties'])
        self.assertNotIn('tools', request)
        self.assertEqual(usage(self.ledger, result['session'])['used']['tokens'], 154)
        self.assertTrue(self.ledger.verify_invocations(result['session'], complete=True))
        observed = result['result']['token_usage']['calls'][0]
        self.assertEqual(observed['source'], 'strata_response')
        self.assertEqual(observed['output_tokens'], 30)
        raw, = self.ledger.db.execute('SELECT record_json FROM invocations').fetchone()
        invocation = json.loads(raw)
        identity = invocation['model_identity']
        self.assertEqual(identity['server_model'], SERVER_MODEL)
        self.assertIsNone(identity['weights_sha256'])
        gate = invocation['orchestration']['frames'][0]
        self.assertEqual(gate['decision'], 'ALLOW')
        trace = json.loads(next((self.root / 'calls').glob('*.json')).read_text())
        self.assertEqual(trace['response']['timings']['predicted_per_second'], 25.0)
        self.assertEqual(trace['generation_limits'], {'max_tokens': 2048})
        self.assertNotIn('fixture-token', json.dumps(trace))

    def test_positive_adaptive_reasoning_leaves_budget_for_the_answer(self):
        from paging import Budget
        prompt = 'Compare the alternatives and explain the trade-offs.'
        with patch.dict(os.environ, SUTRA_THINKING='adaptive', SUTRA_STRATA_CONTEXT='32768'):
            provider = StrataModel(self.root / 'calls')
            provider.budget = Budget(32768, 8192)
            result = self.ask(prompt, provider)
        self.assertTrue(result['result']['admission']['admitted'])
        request, _ = self.server.posts[0]
        self.assertEqual(request['reasoning_effort'], 'low')
        self.assertEqual(request['max_tokens'], 3072)
        self.assertEqual(request['reasoning_budget_tokens'], 768)
        self.assertEqual(provider.budget.response_reserve, 8192)
        self.assertEqual(usage(self.ledger, result['session'])['used']['tokens'], 154)
        trace = json.loads(next((self.root / 'calls').glob('*.json')).read_text())
        self.assertEqual(trace['generation_limits'], {'max_tokens': 3072, 'reasoning_budget_tokens': 768})

    def test_adaptive_budget_does_not_override_explicit_thinking(self):
        with patch.dict(os.environ, SUTRA_THINKING='on'):
            self.assertTrue(self.ask()['result']['admission']['admitted'])
        request, _ = self.server.posts[0]
        self.assertEqual(request['reasoning_effort'], 'low')
        self.assertEqual(request['max_tokens'], 2048)
        self.assertNotIn('reasoning_budget_tokens', request)

    def test_backend_schema_rejection_has_one_governed_repair_and_no_credit(self):
        self.server.frames = [[{'error': {'code': 'structured_output_failed',
                                          'message': 'generated JSON lacked a required property'}}], frames()]
        result = self.ask()
        self.assertTrue(result['result']['admission']['admitted'])
        repaired, first = result['result']['token_usage']['calls']
        self.assertEqual(first['status'], 'quarantined')
        self.assertEqual(first['accounting_settlement']['basis'], 'reserved_unmeasured')
        self.assertEqual(repaired['accounting_settlement']['used_tokens'], 154)
        self.assertEqual(len(self.server.posts), 2)
        self.assertIn('Strata rejected the structured reply', self.server.posts[1][0]['messages'][0]['content'])
        self.assertNotIn('generated JSON lacked', json.dumps(self.server.posts))

    def test_backend_json_rejection_preserves_safe_cause_and_reported_usage(self):
        cause = "invalid JSON: Expecting ',' delimiter: line 17 column 9 (char 804)"
        self.server.frames = [[{'error': {'code': 'structured_output_failed',
                                        'message': 'model did not return valid JSON: ' + cause[len('invalid JSON: '):]},
                               'usage': {'prompt_tokens': 124, 'completion_tokens': 30,
                                         'untrusted_detail': 'private-model-text'}}], frames()]
        result = self.ask()
        self.assertTrue(result['result']['admission']['admitted'])
        repaired, first = result['result']['token_usage']['calls']
        self.assertEqual(first['status'], 'quarantined')
        self.assertEqual(first['source'], 'strata_response')
        self.assertEqual((first['input_tokens'], first['output_tokens']), (124, 30))
        self.assertEqual(first['accounting_settlement']['basis'], 'runtime_measured')
        self.assertEqual(first['accounting_settlement']['used_tokens'], 154)
        self.assertEqual(usage(self.ledger, result['session'])['used']['tokens'], 308)
        self.assertIn(cause, self.server.posts[1][0]['messages'][0]['content'])
        traces = [json.loads(path.read_text()) for path in (self.root / 'calls').glob('*.json')]
        rejected, = [trace for trace in traces if not trace['ok']]
        self.assertIn(cause, rejected['error'])
        self.assertEqual(rejected['response']['eval_count'], 30)
        self.assertNotIn('private-model-text', json.dumps(traces))
        self.assertTrue(self.ledger.verify_invocations(result['session'], complete=True))

    def test_verified_prompt_validation_uses_host_json_and_measured_repair(self):
        from model_packet import build, descriptor
        self.server.status['structured_output'] = dict(PROMPT_VALIDATION)
        self.server.frames = [text_frames('{"private-model-text":}'), frames()]
        result = self.ask()
        self.assertTrue(result['result']['admission']['admitted'])
        repaired, first = result['result']['token_usage']['calls']
        self.assertEqual(first['status'], 'quarantined')
        self.assertEqual(first['accounting_settlement']['basis'], 'runtime_measured')
        self.assertEqual(first['accounting_settlement']['used_tokens'], 154)
        self.assertEqual(usage(self.ledger, result['session'])['used']['tokens'], 308)
        self.assertEqual(len(self.server.posts), 2)
        self.assertIn('invalid JSON: Expecting value:', self.server.posts[1][0]['messages'][0]['content'])
        self.assertNotIn('private-model-text', self.server.posts[1][0]['messages'][0]['content'])
        sent = self.server.posts[0][0]
        self.assertNotIn('response_format', sent)
        self.assertEqual(sent['max_tokens'], 2048)
        self.assertEqual(sent['reasoning_effort'], 'none')
        raw, delivered = self.ledger.db.execute('SELECT i.record_json,d.payload_json FROM invocations i '
            'JOIN deliveries d ON d.id=i.delivery_id ORDER BY i.rowid LIMIT 1').fetchone()
        envelope = json.loads(raw)
        packet = build(conversation.SYSTEM, json.loads(delivered), envelope['schema'])
        inline_schema = json.loads(sent['messages'][0]['content'].split(HOST_JSON_DIRECTIVE)[1])
        self.assertEqual(inline_schema, packet['schema'])
        self.assertEqual(sent['messages'][0]['content'], packet['messages'][0]['content'] +
                         HOST_JSON_DIRECTIVE + canonical(packet['schema']))
        self.assertEqual(envelope['charge'], descriptor(packet, 256)['input_reservation'] + HOST_JSON_DIRECTIVE_BYTES)
        self.assertEqual(envelope['budget_profile']['template_reserve'], 256 + HOST_JSON_DIRECTIVE_BYTES)
        traces = [json.loads(path.read_text()) for path in (self.root / 'calls').glob('*.json')]
        self.assertNotIn('private-model-text', json.dumps(traces))
        for trace in traces:
            self.assertEqual(trace['structured_output'], {'mode': 'host_json',
                'basis': 'verified_prompt_and_validate', 'schema_sha256': sha(canonical(packet['schema'])),
                'normalization': 'none'})
        self.assertTrue(self.ledger.verify_invocations(result['session'], complete=True))

    def test_host_json_capability_is_refreshed_before_dispatch(self):
        provider = StrataModel(self.root / 'calls')
        self.assertFalse(provider.host_json_mode)
        self.server.status['structured_output'] = dict(PROMPT_VALIDATION)
        self.assertTrue(self.ask(provider=provider)['result']['admission']['admitted'])
        self.assertTrue(provider.host_json_mode)
        self.assertNotIn('response_format', self.server.posts[0][0])

    def test_unknown_and_constrained_providers_keep_the_provider_schema(self):
        for capability in (None, {'method': 'prompt_and_validate'},
                dict(PROMPT_VALIDATION, method='grammar'),
                dict(PROMPT_VALIDATION, constrained_decoding=True),
                dict(PROMPT_VALIDATION, stream_buffered=False),
                dict(PROMPT_VALIDATION, formats=['json_object'])):
            with self.subTest(capability=capability):
                self.server.status['structured_output'] = capability
                result = self.ask()
                self.assertTrue(result['result']['admission']['admitted'])
                sent = self.server.posts[-1][0]
                self.assertEqual(sent['response_format']['type'], 'json_schema')
                self.assertNotIn(HOST_JSON_DIRECTIVE, sent['messages'][0]['content'])

    def test_host_json_keeps_gateway_shape_checks_and_output_limit_precedence(self):
        self.server.status['structured_output'] = dict(PROMPT_VALIDATION)
        self.server.frames = [frames(dict(HELLO, context_binding='forged')), frames()]
        result = self.ask()
        self.assertTrue(result['result']['admission']['admitted'])
        self.assertEqual(result['result']['token_usage']['calls'][1]['status'], 'quarantined')
        self.assertEqual(usage(self.ledger, result['session'])['used']['tokens'], 308)
        self.server.frames = [text_frames('{"private-model-text":', finish='length')]
        result = self.ask()['result']
        self.assertFalse(result['admission']['admitted'])
        self.assertEqual(result['failure_kind'], 'output_limit')
        self.assertEqual(result['token_usage']['calls'][0]['accounting_settlement']['basis'], 'runtime_measured')
        self.assertEqual(len(self.server.posts), 3)

    def test_host_json_whole_fence_preserves_usage_and_records_wrapper_removal(self):
        from storage_policy import write_trace
        self.server.status['structured_output'] = dict(PROMPT_VALIDATION)
        original = ' \n```json\n' + json.dumps(HELLO) + '\n```\n '
        self.server.frames = [text_frames(original)]
        with patch('storage_policy.write_trace', wraps=write_trace) as trace_writer:
            result = self.ask()
        self.assertEqual(trace_writer.call_args.args[2]['response']['message']['content'], original)
        self.assertTrue(result['result']['admission']['admitted'])
        self.assertEqual(result['result']['accepted_output']['answer'], HELLO['answer'])
        self.assertEqual(usage(self.ledger, result['session'])['used']['tokens'], 154)
        trace = json.loads(next((self.root / 'calls').glob('*.json')).read_text())
        self.assertEqual(trace['structured_output']['normalization'], 'json_fence')

    def test_host_json_malformed_fence_is_repaired_with_measured_usage(self):
        self.server.status['structured_output'] = dict(PROMPT_VALIDATION)
        self.server.frames = [text_frames('```json\n{"private-model-text":}\n```'), frames()]
        result = self.ask()
        self.assertTrue(result['result']['admission']['admitted'])
        self.assertEqual(usage(self.ledger, result['session'])['used']['tokens'], 308)
        traces = [json.loads(path.read_text()) for path in (self.root / 'calls').glob('*.json')]
        rejected, = [trace for trace in traces if not trace['ok']]
        self.assertEqual(rejected['structured_output']['normalization'], 'json_fence')
        self.assertEqual(rejected['response']['eval_count'], 30)
        self.assertNotIn('private-model-text', json.dumps(traces))

    def test_backend_authentication_body_and_invalid_key_are_redacted(self):
        self.server.post_error = True
        result = self.ask()['result']
        self.assertIn('authentication failed', result['blocked'])
        self.assertNotIn('fixture-token', json.dumps(result))
        trace = next((self.root / 'calls').glob('*.json')).read_text()
        self.assertNotIn('fixture-token', trace)
        with patch.dict(os.environ, SUTRA_STRATA_API_KEY='invalid\nfixture-secret'):
            with self.assertRaisesRegex(InvalidUpdate, 'API key must use printable ASCII characters') as error:
                self.ask()
            self.assertNotIn('fixture-secret', str(error.exception))

    def test_output_exhaustion_is_withheld_and_keeps_the_reservation(self):
        self.server.frames = [[{'error': {'code': 'structured_output_failed',
            'message': 'structured output was incomplete (finish_reason=length); increase the output budget'}}]]
        result = self.ask()['result']
        self.assertFalse(result['admission']['admitted'])
        self.assertEqual(result['failure_kind'], 'output_limit')
        self.assertEqual(result['token_usage']['calls'][0]['accounting_settlement']['basis'], 'reserved_unmeasured')
        self.assertEqual(len(self.server.posts), 1)

    def test_output_exhaustion_keeps_its_classification_when_usage_is_reported(self):
        self.server.frames = [[{'error': {'code': 'structured_output_failed',
            'message': 'structured output was incomplete (finish_reason=length); increase the output budget'},
            'usage': {'prompt_tokens': 124, 'completion_tokens': 2048}}]]
        result = self.ask()['result']
        self.assertFalse(result['admission']['admitted'])
        self.assertEqual(result['failure_kind'], 'output_limit')
        observed, = result['token_usage']['calls']
        self.assertEqual(observed['accounting_settlement']['basis'], 'runtime_measured')
        self.assertEqual(observed['accounting_settlement']['used_tokens'], 2172)
        self.assertEqual(len(self.server.posts), 1)

    def test_unloaded_and_wrong_variant_do_not_post_or_reserve(self):
        self.server.status['loaded'] = False
        with self.assertRaisesRegex(InvalidUpdate, 'Load the local Strata model'):
            self.ask()
        self.server.status.update(loaded=True, model='unexpected')
        with self.assertRaisesRegex(InvalidUpdate, 'does not match'):
            self.ask()
        self.assertEqual(self.server.posts, [])
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM invocations').fetchone()[0], 0)

    def test_direct_call_needs_a_dispatch_permit_and_catalog_is_opt_in(self):
        provider = StrataModel(self.root / 'calls')
        with self.assertRaisesRegex(InvalidUpdate, '[Pp]ermit|[Gg]ateway'):
            provider.call('no-permit', 'system', {}, {})
        self.assertEqual(self.server.posts, [])
        row, = append_catalog({'models': [], 'errors': []})['models']
        self.assertEqual((row['name'], row['provider']), (MODEL, 'strata'))
        with patch.dict(os.environ, clear=True):
            self.assertEqual(append_catalog({'models': [], 'errors': []})['models'], [])
        self.assertEqual(choose('Hello', dict(MODELS, low=MODEL), 'low',
                                {'source_files':0,'selected_files':0,'unresolved_calls':0})['model'], MODEL)

    def test_reserved_provider_name_cannot_become_an_ollama_default(self):
        from vscode_bridge import handle

        def ollama(endpoint, payload=None):
            if endpoint == '/api/tags':
                return {'models': [{'name': 'normal:latest'}, {'name': MODEL}]}
            self.assertEqual(payload, {'model': 'normal:latest'})
            return {'capabilities': ['completion']}

        with patch('vscode_bridge.ollama', side_effect=ollama):
            result = handle(self.root, self.root, {'op': 'models'})
        self.assertEqual(result['suggested_tiers'], dict.fromkeys(MODELS, 'normal:latest'))
        self.assertEqual([row['name'] for row in result['models']], ['normal:latest', MODEL])


class StrataStreamFailureTests(unittest.TestCase):
    def reject(self, message, usage=None):
        event = {'error': {'code': 'structured_output_failed', 'message': message}}
        if usage is not None:
            event['usage'] = usage
        response = io.BytesIO(b'data: ' + json.dumps(event).encode() + b'\n\ndata: [DONE]\n\n')
        with self.assertRaises(StructuredOutputRejected) as caught:
            _stream(response, Mock(), time.monotonic(), 65536, lambda _: None)
        return caught.exception

    def test_schema_and_parser_details_do_not_echo_generated_keys_or_values(self):
        cases = (
            ('model did not return valid JSON: duplicate JSON key: private-source-key',
             'duplicate JSON key (key withheld)'),
            ("model output failed the JSON Schema at /private-source-path: 'private-source-value' is not of type 'object'",
             "value is not of type 'object'"),
            ("model output failed the JSON Schema at /private-source-path: 'private-source-key' is a required property",
             'required property missing'),
            ("model output failed the JSON Schema at /: Additional properties are not allowed ('private-source-key' was unexpected)",
             'additional properties are not allowed'),
            ('could not validate structured output: private-source-value',
             'could not validate structured output'),
            ('model did not return valid JSON: private-source-value\n\x1b[31mprivate-source-value',
             'invalid JSON (provider detail withheld)'),
        )
        for message, expected in cases:
            with self.subTest(message=message):
                error = self.reject(message)
                self.assertIn(expected, str(error))
                self.assertNotIn('private-source', str(error))
                self.assertNotIn('\n', str(error))
                self.assertNotIn('\x1b', str(error))
                self.assertLess(len(str(error)), 500)

    def test_unknown_oversized_and_nontext_provider_details_are_withheld(self):
        for message in ('private-source-value' * 500, {'source': 'private-source-value'}, None):
            with self.subTest(message_type=type(message)):
                error = self.reject(message)
                self.assertIn('provider detail withheld', str(error))
                self.assertNotIn('private-source', str(error))
                self.assertLess(len(str(error)), 500)
        repeated_types = "model output failed the JSON Schema at /: 'private-source-value' is not of type " + ", ".join(["'object'"] * 100)
        error = self.reject(repeated_types)
        self.assertIn('JSON Schema mismatch', str(error))
        self.assertNotIn('private-source', str(error))
        self.assertLess(len(str(error)), 500)

    def test_partial_and_invalid_error_usage_do_not_claim_completed_generation(self):
        error = self.reject('invalid JSON', {'prompt_tokens': 124, 'completion_tokens': True})
        self.assertEqual(error.strata_response, {'done': False, 'done_reason': 'structured_output_failed',
                                               'prompt_eval_count': 124, 'eval_count': None})
        for value in (None, {}, {'prompt_tokens': -1, 'completion_tokens': '30'},
                      {'prompt_tokens': 2 ** 53, 'completion_tokens': False}):
            with self.subTest(usage=value):
                self.assertIsNone(self.reject('invalid JSON', value).strata_response)

    def test_malformed_sse_json_reports_a_parser_location_without_raw_event(self):
        response = io.BytesIO(b'data: {"private-source-value":}\n\n')
        with self.assertRaisesRegex(RuntimeError, 'Invalid Strata stream JSON: Expecting value: line 1 column') as caught:
            _stream(response, Mock(), time.monotonic(), 65536, lambda _: None)
        self.assertNotIn('private-source-value', str(caught.exception))

    def test_host_json_rejects_ambiguous_or_nonfinite_values_without_salvage(self):
        cases = ('{"private-source-key":1,"private-source-key":2}',
                 '{"nested":{"private-source-key":1,"private-source-key":2}}',
                 '{"value":NaN}', '{"value":Infinity}', '{"value":-Infinity}', '{"value":1e999}',
                 '[]', 'null', 'true', '"private-source-value"',
                 '{} {}', '{}\nprivate-source-value', 'private-source-value {}',
                 'private-source-value\n```json\n{}\n```', '```json\n{}\n```\nprivate-source-value',
                 '```json\n{}\n```\n```json\n{}\n```')
        for text in cases:
            with self.subTest(text=text):
                projected, _ = _host_json_text(text)
                with self.assertRaises(StructuredOutputRejected) as caught:
                    _host_json_answer(projected)
                self.assertNotIn('private-source', str(caught.exception))
        for text in (' \n{"value":1}\t', '```json\n{"value":1}\n```', ' \n```\n{"value":1}\n```\n '):
            projected, normalization = _host_json_text(text)
            self.assertEqual(_host_json_answer(projected), {'value': 1})
            self.assertEqual(normalization, 'json_fence' if '```' in text else 'none')

    def test_structured_trace_metadata_is_bounded_and_omits_arbitrary_content(self):
        from storage_policy import write_trace
        with tempfile.TemporaryDirectory() as tmp:
            metadata = {'mode': 'host_json', 'basis': 'verified_prompt_and_validate',
                        'schema_sha256': 'a' * 64, 'normalization': 'json_fence', 'source': 'private-source-value'}
            write_trace(tmp, 'valid', {'provider': 'strata', 'structured_output': metadata})
            trace = json.loads((Path(tmp) / 'valid.json').read_text())
            self.assertNotIn('private-source', json.dumps(trace))
            self.assertEqual(set(trace['structured_output']), {'mode', 'basis', 'schema_sha256', 'normalization'})
            for index, invalid in enumerate((dict(metadata, mode='private-source-value'),
                    dict(metadata, basis='private-source-value'), dict(metadata, schema_sha256='private-source-value'))):
                write_trace(tmp, str(index), {'provider': 'strata', 'structured_output': invalid})
                trace = json.loads((Path(tmp) / (str(index) + '.json')).read_text())
                self.assertNotIn('structured_output', trace)
                self.assertNotIn('private-source', json.dumps(trace))

    def test_strata_template_allowance_is_validated_before_context_planning(self):
        from model_context import allocation
        metadata = {'model_info': {'strata.context_length': 32768}, '_sutra_provider': 'strata',
                    '_sutra_template_reserve': 256 + HOST_JSON_DIRECTIVE_BYTES}
        budget, _ = allocation(metadata)
        self.assertEqual(budget.template_reserve, 256 + HOST_JSON_DIRECTIVE_BYTES)
        for value in (True, None, '479', 479.0, 255, 4097):
            with self.subTest(value=value), self.assertRaisesRegex(InvalidUpdate, 'trusted Strata template allowance'):
                allocation(dict(metadata, _sutra_template_reserve=value))
        # This adapter-specific host setting does not affect other providers.
        budget, _ = allocation(dict(metadata, _sutra_provider='ollama', _sutra_template_reserve='ignored'))
        self.assertEqual(budget.template_reserve, 256)


if __name__ == '__main__':
    unittest.main()
