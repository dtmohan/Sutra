"""Exercise the approved transport and ledger against a bounded HTTP/1.0 fixture."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import conversation
from context_manager import InvalidUpdate
from invocation_gateway import InvocationLedger
from karma_adapter import KarmaAdapter
from model_routing import choose
from review_budget import usage
from strata_model import MODEL, StrataModel, append_catalog


MODELS = {'low': 'installed-low', 'mid': 'installed-mid', 'high': MODEL}
SERVER_MODEL = 'qwen3.8-flash-next-q2_0'
HELLO = {'page_requests': [], 'intent': 'conversation', 'escalation_reason': 'none',
         'diagram': None, 'answer': 'Hello. How can I help?',
         'summary': 'A hello exchange.'}


def frames(answer=HELLO, finish='stop', measured=True):
    initial = {'choices': [{'delta': {'role': 'assistant'}, 'finish_reason': None}]}
    text = {'choices': [{'delta': {'content': json.dumps(answer)}, 'finish_reason': None}]}
    final = {'choices': [{'delta': {}, 'finish_reason': finish}],
             'timings': {'predicted_per_second': 25.0}}
    if measured:
        final['usage'] = {'prompt_tokens': 124, 'completion_tokens': 30}
    return [initial, text, final]


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


if __name__ == '__main__':
    unittest.main()
