"""Cross-provider continuity through the real gateway; only devices are fixtures.

The remote provider uses a real private HTTP/SSE connection and its production
permit consumer. No GPU, SSH credentials or installed model are used here.
"""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import conversation
from context_manager import InvalidUpdate, canonical, sha
import engine_profiles
from invocation_gateway import InvocationLedger
from karma_adapter import KarmaAdapter
import model_identity
import provider_resources
from remote_model import RemoteModel
import remote_profiles
from review_budget import BudgetStop, usage
from test_engine_profiles import descriptor as engine_descriptor
from test_remote_memory import observation
from test_remote_model import stream
from test_remote_profiles import descriptor as remote_descriptor


HELLO = {'page_requests': [], 'intent': 'conversation', 'escalation_reason': 'none',
         'diagram': None, 'answer': 'Marigold. I will keep the answer brief.',
         'summary': 'The project nickname is Marigold. User wants brief answers.'}


class RemoteIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                cls.requests.append({'body': body, 'path': self.path,
                                     'authorization': self.headers.get('Authorization')})
                reply = stream(json.dumps(HELLO), counts=(91, 34))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(2)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.registry = self.root / 'remote-models.json'
        self.engine_registry = self.root / 'engine-models.json'
        self.remote = remote_profiles.write({'rtx3090': remote_descriptor(
            self.root, port=self.server.server_port, context_tokens=32768,
            gpu_uuid='GPU-b90a1b75-3646-10a8-9987-8f6c6ccfdee0',
            executable_sha256='2' * 64)}, self.registry)['rtx3090']
        self.local = engine_profiles.write({'local': engine_descriptor(
            self.root, 'local', 32768)}, self.engine_registry)['local']
        self.models = {'low': self.local['name'], 'mid': self.remote['name'],
                       'high': self.local['name']}
        environment = patch.dict(os.environ, {
            'SUTRA_ENGINE_REGISTRY': str(self.engine_registry),
            'SUTRA_ENGINE_MODELS': json.dumps([self.local['name']]),
            'SUTRA_ENGINE_APPROVALS': json.dumps({self.local['name']: self.local['profile_hash']}),
            'SUTRA_ENGINE_ONLY': '0', 'SUTRA_STRATA_ONLY': '0', 'SUTRA_STRATA_MODELS': '[]',
            'SUTRA_REMOTE_REGISTRY': str(self.registry),
            'SUTRA_REMOTE_MODELS': json.dumps([self.remote['name']]), 'SUTRA_REMOTE_ONLY': '0',
            'SUTRA_RUNTIME_DIR': str(self.root / 'runtime'), 'SUTRA_THINKING': 'off',
            'SUTRA_WORKSPACE_ALLOCATION': '{"enabled":false}',
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.ledger = InvocationLedger(self.root / 'state.sqlite')
        self.addCleanup(lambda: self.ledger.close())
        self.local_requests = []
        self.local_measurements = []
        type(self).requests = []
        self.meta = {}
        client = SimpleNamespace(activate=self.activate, generate=self.generate)
        for target, kwargs in (
                ('engine_model._client', {'return_value': client}),
                ('monitoring.require_resources', {'side_effect': self.local_hardware}),
                ('runtime_memory.fresh_resources', {'side_effect': self.local_hardware}),
                ('remote_memory.probe', {'side_effect': self.remote_hardware}),
                ('remote_memory.verify_instance', {'return_value': True}),
                ('remote_control.verify_server', {'return_value': {'verified': True}}),
                ('vscode_bridge.ollama', {'side_effect': AssertionError('Ollama is not used by these profiles')})):
            guard = patch(target, **kwargs)
            guard.start()
            self.addCleanup(guard.stop)

    def local_hardware(self, *args, **kwargs):
        self.local_measurements.append(True)
        now = time.time()
        return KarmaAdapter().hardware({'profile': 'portable-memory-v1', 'wall_time': now,
            'physical_memory_bytes': 64 * 1024**3, 'available_bytes': 40 * 1024**3}, now)

    def remote_hardware(self, profile):
        sample = observation(profile)
        now = time.time()
        sample.update(wall_time=now, capture_started_at=now - .1, capture_seconds=.1)
        sample['process']['context_tokens'] = str(profile['context_tokens'])
        sample['server']['context_tokens'] = profile['context_tokens']
        return sample

    def activate(self, profile):
        return {'profile_hash': profile['profile_hash'], 'instance_id': profile['profile_hash'][:32],
                'context_tokens': profile['context_tokens'], 'pid': 4321}

    def generate(self, profile, messages, max_tokens, thinking, schema, **options):
        self.local_requests.append(copy.deepcopy(messages))
        return {'content': json.dumps(HELLO), 'thinking': '', 'prompt_tokens': 124,
                'completion_tokens': 30, 'finish_reason': 'stop',
                'instance_id': profile['profile_hash'][:32]}

    def ask(self, tier='low', parent=None, prompt=None):
        return conversation.answer(self.ledger, self.meta, self.root,
            {'prompt': prompt or ('The project nickname is Marigold. Keep answers brief.'
                                  if parent is None else 'What nickname did I give the project?'),
             'models': self.models, 'tier': tier, 'parent': parent, 'one_step': True})

    def payload(self, result):
        row = self.ledger.db.execute('SELECT d.payload_json FROM invocations i JOIN deliveries d '
            'ON d.id=i.delivery_id WHERE i.id=?', (result['result']['invocation_id'],)).fetchone()
        return json.loads(row[0])

    def reload(self):
        self.ledger.close()
        self.ledger = InvocationLedger(self.root / 'state.sqlite')

    def test_local_remote_local_reload_preserves_exact_context_authority_and_settled_spending(self):
        first = self.ask()
        self.assertTrue(first['result']['admission']['admitted'], first)
        session, parent = first['session'], first['result']['invocation_id']
        contract = self.ledger.current_contract(session)
        first_payload = self.payload(first)
        spent = usage(self.ledger, session)
        self.assertEqual(spent['used']['tokens'], 154)
        self.reload()
        self.assertEqual(usage(self.ledger, session), spent)
        remote = self.ask('mid', parent)
        self.assertTrue(remote['result']['admission']['admitted'], remote)
        self.assertEqual(remote['model_capacity']['source'], 'remote_gpu_metadata')
        self.assertEqual(remote['session'], session)
        self.assertEqual(self.payload(remote)['contract'], first_payload['contract'])
        self.assertIn('Marigold', canonical(self.payload(remote)['memory']['recent_turns']))
        self.assertIn('Marigold', canonical(self.requests[0]['body']['messages']),
                      'The retained fact must reach the actual remote wire request')
        self.assertEqual(self.requests[0]['path'], '/v1/chat/completions')
        self.assertEqual(self.requests[0]['authorization'], 'Bearer unit-test-remote-credential')
        self.assertEqual(usage(self.ledger, session)['used']['tokens'], 279)
        parent = remote['result']['invocation_id']
        self.reload()
        last = self.ask('high', parent)
        self.assertTrue(last['result']['admission']['admitted'], last)
        self.assertTrue(last['memory_used'])
        self.assertEqual(last['session'], session)
        self.assertEqual(self.payload(last)['contract'], first_payload['contract'])
        self.assertIn('Marigold', canonical(self.local_requests[-1]))
        self.assertEqual(self.ledger.current_contract(session), contract)
        self.assertEqual(usage(self.ledger, session)['used']['calls'], 3)
        self.assertEqual(usage(self.ledger, session)['used']['tokens'], 433)
        self.assertEqual(usage(self.ledger, session)['limits'], spent['limits'])
        constraints = self.ledger.snapshot(session)['constraints']
        self.assertFalse(constraints['edits_allowed'])
        self.assertFalse(constraints['network_allowed'])
        self.assertFalse(constraints['tests_allowed'])
        self.assertTrue(self.ledger.verify_invocations(session))

    def test_exhausted_parent_allocation_cannot_reset_by_switching_to_remote_after_reload(self):
        with patch.dict(conversation.CONSTRAINTS, review_calls=3):
            parent = None
            for tier in ('low', 'mid', 'high'):
                result = self.ask(tier, parent)
                self.assertTrue(result['result']['admission']['admitted'], result)
                parent = result['result']['invocation_id']
            session = result['session']
            before = usage(self.ledger, session)
            self.assertEqual(before['used']['calls'], before['limits']['calls'])
            self.reload()
            posted = len(self.requests)
            with self.assertRaises(BudgetStop):
                self.ask('mid', parent)
            self.assertEqual(len(self.requests), posted)
            self.assertEqual(usage(self.ledger, session), before)
            self.assertTrue(self.ledger.verify_invocations(session))

    def test_revoked_remote_assignment_blocks_post_without_erasing_history_or_spending(self):
        first = self.ask('mid')
        self.assertTrue(first['result']['admission']['admitted'], first)
        session, parent = first['session'], first['result']['invocation_id']
        before, contract = usage(self.ledger, session), self.ledger.current_contract(session)
        self.reload()
        with patch.dict(os.environ, SUTRA_REMOTE_MODELS='[]'):
            with self.assertRaisesRegex(InvalidUpdate, 'not assigned'):
                self.ask('mid', parent)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(usage(self.ledger, session), before)
        self.assertEqual(self.ledger.current_contract(session), contract)
        self.assertEqual(self.ledger.load_result(session, parent)['answer'], HELLO['answer'])
        self.assertTrue(self.ledger.verify_invocations(session))

    def test_remote_resource_checks_never_consult_mac_inference_memory(self):
        with patch('monitoring.require_resources', side_effect=AssertionError('Wrong host')):
            hardware = provider_resources.require_resources(self.root, model=self.remote['name'])
            self.assertTrue(hardware['admitted'])
            self.assertEqual(hardware['sample']['gpu']['uuid'], self.remote['gpu_uuid'])
            result = self.ask('mid')
        self.assertTrue(result['result']['admission']['admitted'], result)
        self.assertEqual(self.local_measurements, [])
        self.assertEqual(len(self.requests), 1)

    def test_remote_gpu_plan_does_not_become_a_local_runtime_memory_plan(self):
        import remote_memory
        import runtime_memory
        result = self.ask('mid')
        self.assertTrue(result['result']['admission']['admitted'], result)
        with patch('runtime_memory.running', return_value=[]):
            local = runtime_memory.snapshot()
        remote = remote_memory.snapshot(self.root)
        self.assertEqual(remote['model'], self.remote['name'])
        self.assertEqual(remote['plan']['provider'], 'remote_llama')
        self.assertNotEqual((local.get('plan') or {}).get('provider'), 'remote_llama',
                            'The remote GPU allocation must not be attributed to Mac/Ollama memory')

    def test_remote_reference_and_host_change_do_not_falsely_establish_distinct_weights(self):
        # Build a real content-addressed Ollama manifest for the same GGUF.
        directory = self.root / 'ollama'
        blobs = directory / 'blobs'
        blobs.mkdir(parents=True)
        manifest = {'layers': [{'mediaType': 'application/vnd.ollama.image.model',
                               'digest': 'sha256:' + self.remote['model_sha256']}]}
        raw = canonical(manifest)
        digest = sha(raw)
        (blobs / ('sha256-' + digest)).write_text(raw)
        local = model_identity.from_metadata('qwen3:4b', {'digest': 'sha256:' + digest},
            {'model_info': {'general.architecture': 'qwen3'}}, directory)
        remote = remote_profiles.identity(self.remote['name'], self.remote)
        alias = dict(self.remote, id='same-weights', ssh_alias='another-trusted-pc')
        alias.pop('name'); alias.pop('profile_hash')
        alias = remote_profiles.normalize(alias)
        other = remote_profiles.identity(alias['name'], alias)
        self.assertNotEqual(remote['remote_profile_hash'], other['remote_profile_hash'])
        self.assertEqual(model_identity.compare(local, remote)['weight_relation'], 'same')
        self.assertEqual(model_identity.compare(remote, other)['weight_relation'], 'same')
        self.assertFalse(model_identity.compare(remote, other)['statistical_independence_verified'])

    def test_automatic_reviewer_ignores_registered_but_unassigned_remote_profile(self):
        mapper = dict(model_identity.unknown('mapper'), weights_sha256='b' * 64, family='qwen')
        local = dict(model_identity.unknown('local-reviewer'), weights_sha256='c' * 64, family='qwen')
        remote = dict(model_identity.unknown(self.remote['name']), weights_sha256='d' * 64, family='mistral')
        candidates = {'models': [{'name': 'local-reviewer', 'eligible': True, 'size': 1000},
                                  {'name': self.remote['name'], 'eligible': True, 'size': 1}], 'errors': []}
        with patch.dict(os.environ, SUTRA_REMOTE_MODELS='[]'), \
                patch('model_catalog.combined', return_value=candidates), \
                patch('model_identity.observe', side_effect=lambda name, *args: {
                    'local-reviewer': local, self.remote['name']: remote}[name]) as observed:
            selected, separation = model_identity.choose(mapper)
        self.assertEqual(selected, 'local-reviewer')
        self.assertEqual(separation['weight_relation'], 'different')
        self.assertEqual([call.args[0] for call in observed.call_args_list], ['local-reviewer'])


if __name__ == '__main__':
    unittest.main()
