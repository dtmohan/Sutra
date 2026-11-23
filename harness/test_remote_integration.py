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
from monitoring import ResourceWait
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
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers['Content-Length']))
                if self.path == '/slots/0':
                    cls.slot_checks.append((raw, self.headers.get('Authorization')))
                    status, reply = cls.slot_response
                    self.send_response(status)
                    self.send_header('Content-Length', str(len(reply)))
                    self.end_headers()
                    self.wfile.write(reply)
                    return
                body = json.loads(raw)
                request = {'body': body, 'path': self.path, 'authorization': self.headers.get('Authorization')}
                if self.path == '/v1/chat/completions/input_tokens':
                    cls.count_requests.append(request)
                    reply = json.dumps({'object': 'response.input_tokens',
                                        'input_tokens': cls.count_tokens(body)}).encode()
                    content_type = 'application/json'
                else:
                    cls.requests.append(request)
                    reply = cls.remote_reply(body) if callable(cls.remote_reply) else cls.remote_reply
                    content_type = 'text/event-stream'
                self.send_response(200)
                self.send_header('Content-Type', content_type)
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
        type(self).count_requests = []
        type(self).count_tokens = staticmethod(lambda body: 91)
        type(self).slot_checks = []
        type(self).slot_response = (501, json.dumps({'error': {'code': 501, 'type': 'not_supported_error',
            'message': 'This server does not support slots action. Start it with `--slot-save-path`'}}).encode())
        type(self).remote_reply = stream(json.dumps(HELLO), counts=(91, 34))
        self.meta = {}
        client = SimpleNamespace(activate=self.activate, generate=self.generate)
        for target, kwargs in (
                ('engine_model._client', {'return_value': client}),
                ('monitoring.require_resources', {'side_effect': self.local_hardware}),
                ('runtime_memory.fresh_resources', {'side_effect': self.local_hardware}),
                ('remote_memory.probe', {'side_effect': self.remote_hardware}),
                ('remote_memory.verify_instance', {'return_value': True}),
                ('remote_control.ensure_tunnel', {'return_value': {'ready': True, 'owned': True}}),
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
        sample['model']['hash_ms'] = 10.
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

    def enable_prefix_fixture(self):
        import remote_prefix

        fixture_template = 'Exact template from the fixture loopback server.'
        profiles = remote_profiles.write({name: remote_descriptor(self.root, identifier=name,
            port=self.server.server_port, model=f'fixture-{name}', model_sha256=digest * 64,
            context_tokens=32768, executable_sha256=remote_prefix.EXECUTABLE_SHA256,
            gpu_uuid='GPU-b90a1b75-3646-10a8-9987-8f6c6ccfdee0')
            for name, digest in (('rtx3090', 'a'), ('alternate', 'b'))}, self.registry)
        def native_server(profile):
            return {'props': {'chat_template': fixture_template,
                              'default_generation_settings': {'n_ctx': profile['context_tokens']}}}
        self.isolated_cache = True
        self.fixture_prefixes = []
        def observed(profile):
            sample = self.remote_hardware(profile)
            sample['process']['executable_sha256'] = profile['executable_sha256']
            sample['process']['prefix_cache_isolated'] = self.isolated_cache
            return sample
        for mocked in (patch.dict(os.environ, SUTRA_REMOTE_MODELS=canonical([p['name'] for p in profiles.values()]),
                                  SUTRA_REMOTE_REVIEW_PREFIX_REUSE_MODELS=canonical([p['name'] for p in profiles.values()])),
                       patch.object(remote_prefix, 'TEMPLATE_SHA256', sha(fixture_template)),
                       patch('remote_control.verify_server', side_effect=native_server),
                       patch('remote_memory.probe', side_effect=observed)):
            mocked.start()
            self.addCleanup(mocked.stop)

        def response(body):
            if set(body['response_format']['json_schema']['schema']['properties']) != {
                    'tool', 'summary', 'remaining', 'findings', 'completed', 'edges'}:
                raise AssertionError('Fixture requires an actual legacy architect decoder schema')
            scopes = self.fixture_prefixes
            prefix = body['messages'][0]['content'].splitlines()[0]
            hit = bool(body['cache_prompt'] and scopes and scopes[-1] == prefix)
            scopes.append(prefix if body['cache_prompt'] else None)
            target = 'allowed/entry.py' if len(scopes) % 2 else 'allowed/worker.py'
            result = {'tool': {'kind': 'read', 'target': target, 'query': '', 'line': 1},
                      'summary': 'Read the next approved source file.', 'remaining': [],
                      'findings': [], 'completed': [], 'edges': []}
            count = type(self).count_tokens(body)
            cached = 64 if hit else 0
            return stream(json.dumps(result), counts=(count, 34), timings={'cache_n': cached,
                          'prompt_n': count - cached, 'prompt_ms': 2., 'predicted_n': 34, 'predicted_ms': 4.})

        type(self).remote_reply = staticmethod(response)
        self.workspace = (self.root / 'repo').resolve()
        allowed = self.workspace / 'allowed'
        allowed.mkdir(parents=True)
        (allowed / 'entry.py').write_text('def entry():\n    return 7\n')
        (allowed / 'worker.py').write_text('def worker():\n    return 24\n')
        (self.workspace / 'outside.py').write_text('INACCESSIBLE_UNDER_REVIEW_SCOPE = 724\n')
        return profiles

    def new_prefix_review(self):
        from vscode_bridge import handle

        handle(self.root, self.workspace, {'op': 'auto_new', 'goal': 'Trace the actual project request flow.',
                                         'roots': ['allowed']})
        return json.loads((self.root / 'workspace.json').read_text())

    def architect(self, meta, profile):
        import architecture_runtime
        import orchestration
        from paging import Budget
        import remote_context

        provider = RemoteModel(self.root / 'calls', profile['name'], review=True)
        window = remote_context.discover(profile)
        self.assertIsNotNone(window)
        provider.budget = Budget(32768, 2048, context_profile=2,
                                 reservation_limit=200000, context_window=window)
        provider.context_metadata['_sutra_context_window'] = window
        node = orchestration.task(self.ledger, meta['active'])
        self.ledger.db.commit()
        with orchestration.scope(node['id']):
            return architecture_runtime.dispatch(self.ledger, meta, self.workspace, self.root,
                {'prompt': 'Read the approved source and trace the actual request flow.',
                 'model': profile['name'], 'mode': 'explore'}, provider=provider)

    def architect_records(self, out):
        result = out['result']
        self.assertTrue(result['admission']['admitted'], result)
        row = self.ledger.db.execute('SELECT i.record_json,r.record_json FROM invocations i '
            'JOIN invocation_receipts r ON r.invocation_id=i.id WHERE i.id=?', (result['invocation_id'],)).fetchone()
        return tuple(json.loads(value) for value in row)

    def test_certified_prefix_is_stable_within_scope_and_rotates_on_task_and_model_switch(self):
        import repository_tools

        profiles = self.enable_prefix_fixture()
        primary, alternate = profiles['rtx3090'], profiles['alternate']
        first_task = self.new_prefix_review()
        sid = first_task['active']
        original = usage(self.ledger, sid)
        contract = self.ledger.pinned(sid)
        first, first_receipt = self.architect_records(self.architect(first_task, primary))
        self.reload()
        warm, warm_receipt = self.architect_records(self.architect(first_task, primary))
        switched, switched_receipt = self.architect_records(self.architect(first_task, alternate))
        second_task = self.new_prefix_review()
        other, _ = self.architect_records(self.architect(second_task, primary))
        self.isolated_cache = False
        unverified, _ = self.architect_records(self.architect(second_task, primary))

        def prefix(envelope):
            return envelope['physical_context']['prefix_reuse']

        self.assertEqual(prefix(first), prefix(warm))
        self.assertEqual(len({prefix(first)['scope_sha256'], prefix(switched)['scope_sha256'],
                              prefix(other)['scope_sha256']}), 3)
        self.assertNotEqual(sid, second_task['active'])
        self.assertNotIn('prefix_reuse', unverified['physical_context'])
        self.assertEqual(len(self.requests), 5)
        for request, envelope in zip(self.requests, (first, warm, switched, other, unverified)):
            body = request['body']
            proof = envelope['physical_context']
            self.assertEqual(proof['wire_sha256'], sha(canonical(body)))
            self.assertIn(body, [item['body'] for item in self.count_requests],
                          'The exact counter and generation must see the same committed body')
            self.assertIs(body['cache_prompt'], 'prefix_reuse' in proof)
            self.assertNotIn('INACCESSIBLE_UNDER_REVIEW_SCOPE', canonical(body['messages']))
            if body['cache_prompt']:
                self.assertEqual(body['cache_reuse'], 0)
                self.assertTrue(body['messages'][0]['content'].startswith('[' + prefix(envelope)['scope_sha256'] + ']\n'))
            self.assertEqual(envelope['role'], 'architect')
            self.assertEqual(proof['prompt_tokens'], 91)
        for receipt in (first_receipt, warm_receipt, switched_receipt):
            self.assertEqual(receipt['token_usage']['input_tokens'], 91)
            self.assertEqual(receipt['accounting_settlement']['used_tokens'], 125)
        compact = json.loads((self.root / 'calls' / (warm['call_id'] + '.json')).read_text())
        self.assertEqual(compact['response']['timings']['cache_n'], 64)
        self.assertNotIn('messages', compact['response'])
        charged = usage(self.ledger, sid)
        self.assertEqual(charged['used']['tokens'], original['used']['tokens'] + 3 * 125)
        self.assertEqual(charged['used']['calls'], original['used']['calls'] + 3)
        self.assertEqual(charged['limits'], original['limits'])
        self.assertEqual(self.ledger.pinned(sid), contract)
        self.assertEqual(contract['constraints']['allowed_paths'], ['allowed'])
        for permission in ('edits_allowed', 'tests_allowed', 'network_allowed', 'dependencies_allowed'):
            self.assertFalse(contract['constraints'][permission])
        self.assertEqual(repository_tools.catalog(self.ledger, sid), ['allowed/entry.py', 'allowed/worker.py'])
        with patch.dict(os.environ, SUTRA_REMOTE_REVIEW_PREFIX_REUSE_MODELS='[]'), \
                patch('remote_context._identity', side_effect=AssertionError('Offline verification must not call the server')):
            self.reload()
            self.assertTrue(self.ledger.verify_invocations(sid))
            self.assertTrue(self.ledger.verify_invocations(second_task['active']))
            self.assertEqual(usage(self.ledger, sid), charged)
            self.assertTrue(self.ledger.audit_valid())

    def test_prefix_bound_transport_fault_never_posts_or_refunds_and_rechecks_instance(self):
        import remote_memory

        primary = self.enable_prefix_fixture()['rtx3090']
        meta = self.new_prefix_review()
        sid = meta['active']
        first, accepted = self.architect_records(self.architect(meta, primary))
        before = usage(self.ledger, sid)
        contract = self.ledger.pinned(sid)
        failure = remote_memory._blocked('The exact remote admission expired before POST.')
        failure['runtime'] = {'kind': 'remote_observation', 'stage': 'remote_pre_transport',
                              'inference_started': False, 'model': primary['name']}
        with patch('remote_memory.require_dispatch_fresh', side_effect=ResourceWait(failure)):
            with self.assertRaises(ResourceWait):
                self.architect(meta, primary)
        self.assertEqual(len(self.requests), 1)
        charged = usage(self.ledger, sid)
        self.assertEqual(charged['used']['calls'], before['used']['calls'] + 1)
        self.assertGreater(charged['used']['tokens'], before['used']['tokens'])
        self.assertEqual(charged['limits'], before['limits'])
        fault = json.loads(self.ledger.db.execute('SELECT record_json FROM invocation_receipts '
            'WHERE session=? ORDER BY seq DESC LIMIT 1', (sid,)).fetchone()[0])
        self.assertEqual(fault['status'], 'quarantined')
        self.assertEqual(fault['accounting_settlement']['basis'], 'reserved_unmeasured')
        self.reload()
        self.assertEqual(usage(self.ledger, sid), charged)
        self.assertEqual(self.ledger.pinned(sid), contract)
        self.assertEqual(json.loads(self.ledger.db.execute('SELECT record_json FROM invocation_receipts '
            'WHERE invocation_id=?', (first['id'],)).fetchone()[0]), accepted)
        next_call, _ = self.architect_records(self.architect(meta, primary))
        self.assertEqual(next_call['physical_context']['prefix_reuse'], first['physical_context']['prefix_reuse'])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(usage(self.ledger, sid)['used']['tokens'], charged['used']['tokens'] + 125)
        self.assertTrue(self.ledger.verify_invocations(sid))

    def test_prefix_rejects_foreign_authority_and_changes_on_a_narrower_contract(self):
        import orchestration
        import remote_context
        import remote_memory
        import remote_prefix
        from vscode_bridge import save_contract

        primary = self.enable_prefix_fixture()['rtx3090']
        first = self.new_prefix_review()['active']
        second = self.new_prefix_review()['active']
        window = remote_context.discover(primary)
        sample = remote_memory.probe(primary)
        instance = remote_memory.evaluate(primary, sample, 32768)['instance']
        restarted = copy.deepcopy(sample)
        replacement_pid = instance['pid'] + 1
        restarted['process'].update(pid=replacement_pid, started_at='2026-11-20T12:10:00Z')
        restarted['listener']['pid'] = replacement_pid
        restarted['gpu']['process_pids'].append(replacement_pid)
        replacement = remote_memory.evaluate(primary, restarted, 32768)['instance']
        run = self.ledger.begin_run(first, 'Inspect approved source.', [], None, max_calls=1, max_repairs=0)
        with orchestration.scope(orchestration.task(self.ledger, second)['id']):
            with self.assertRaisesRegex(InvalidUpdate, 'different task'):
                remote_prefix.binding(self.ledger, run, window, instance)
        with orchestration.scope(orchestration.task(self.ledger, first)['id']):
            previous = remote_prefix.binding(self.ledger, run, window, instance)
            self.assertNotEqual(remote_prefix.binding(self.ledger, run, window, replacement), previous)

        current = self.ledger.snapshot(first)
        narrowed = dict(current['constraints'], allowed_paths=['allowed/entry.py'])
        save_contract(self.ledger, first, {'goal': current['goal'], 'constraints': narrowed}, self.workspace)
        later = self.ledger.begin_run(first, 'Inspect approved source.', [], None, max_calls=1, max_repairs=0)
        with orchestration.scope(orchestration.task(self.ledger, first)['id']):
            self.assertNotEqual(remote_prefix.binding(self.ledger, later, window, instance), previous)
        self.assertEqual(orchestration.permissions(self.ledger, first)['read_paths'], ['allowed/entry.py'])
        self.assertEqual(self.requests, [])

        # An enabled profile must not turn a conversation into a cacheable review.
        type(self).remote_reply = stream(json.dumps(HELLO))
        asked = self.ask('mid')
        self.assertTrue(asked['result']['admission']['admitted'], asked)
        self.assertIs(self.requests[-1]['body']['cache_prompt'], False)

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

    def test_failed_fresh_observation_preserves_two_accepted_calls_and_resumes_across_models(self):
        first = self.ask('mid')
        second = self.ask('mid', first['result']['invocation_id'])
        session, parent = second['session'], second['result']['invocation_id']
        before = usage(self.ledger, session)
        contract = self.ledger.current_contract(session)
        packet_contract = self.payload(second)['contract']
        prefix = self.ledger.db.execute('SELECT id,record_json FROM invocation_receipts '
            'WHERE session=? ORDER BY seq', (session,)).fetchall()
        self.assertEqual(before['used']['calls'], 2)
        self.reload()
        with patch('remote_memory.probe', side_effect=InvalidUpdate('Fresh remote SSH inspection timed out')), \
                patch('monitoring.require_resources', side_effect=AssertionError('Mac cannot replace remote evidence')):
            with self.assertRaises(ResourceWait) as paused:
                self.ask('mid', parent)
        self.assertEqual(paused.exception.state['decision'], 'BRIDGE')
        self.assertEqual(paused.exception.category, 'measurement_unavailable')
        self.assertIn('SSH inspection timed out', paused.exception.state['reason'])
        self.assertIs(paused.exception.state['runtime']['inference_started'], False)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(usage(self.ledger, session), before)
        self.assertEqual(self.ledger.current_contract(session), contract)
        self.assertEqual(self.ledger.db.execute('SELECT id,record_json FROM invocation_receipts '
            'WHERE session=? ORDER BY seq', (session,)).fetchall(), prefix)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM invocations '
            'WHERE session=?', (session,)).fetchone()[0], 2)
        self.reload()
        switched = self.ask('high', parent)
        self.assertTrue(switched['result']['admission']['admitted'])
        self.assertEqual(self.payload(switched)['contract'], packet_contract)
        self.assertIn('Marigold', canonical(self.payload(switched)['memory']))
        self.assertEqual(usage(self.ledger, session)['used']['calls'], before['used']['calls'] + 1)
        self.assertEqual(usage(self.ledger, session)['used']['tokens'], before['used']['tokens'] + 154)
        fresh = self.ask('mid', switched['result']['invocation_id'])
        self.assertTrue(fresh['result']['admission']['admitted'])
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.payload(fresh)['contract'], packet_contract)
        self.assertEqual(self.ledger.db.execute('SELECT id,record_json FROM invocation_receipts '
            'WHERE session=? ORDER BY seq', (session,)).fetchall()[:len(prefix)], prefix)
        self.assertEqual(usage(self.ledger, session)['used']['tokens'], before['used']['tokens'] + 154 + 125)
        self.assertTrue(self.ledger.verify_invocations(session))

    def test_expiry_after_gateway_reservation_stops_post_and_keeps_consumed_allocation(self):
        first = self.ask('mid')
        session, parent = first['session'], first['result']['invocation_id']
        before = usage(self.ledger, session)
        contract = self.ledger.current_contract(session)
        import remote_memory
        state = remote_memory._blocked('The dispatch observation expired before transport')
        state['runtime'] = {'kind': 'remote_observation', 'stage': 'remote_pre_transport',
                            'inference_started': False, 'model': self.remote['name']}
        with patch('remote_memory.require_dispatch_fresh', side_effect=ResourceWait(state)):
            with self.assertRaises(ResourceWait) as stopped:
                self.ask('mid', parent)
        self.assertEqual(stopped.exception.state['reason'], state['reason'])
        self.assertEqual(stopped.exception.state['decision'], state['decision'])
        self.assertEqual(stopped.exception.state['runtime']['stage'], 'remote_pre_transport')
        self.assertIs(stopped.exception.state['runtime']['inference_started'], False)
        self.assertEqual(len(self.requests), 1)
        charged = usage(self.ledger, session)
        self.assertEqual(charged['used']['calls'], before['used']['calls'] + 1)
        self.assertGreater(charged['used']['tokens'], before['used']['tokens'])
        self.assertEqual(charged['limits'], before['limits'])
        self.assertEqual(self.ledger.current_contract(session), contract)
        receipt = json.loads(self.ledger.db.execute('SELECT record_json FROM invocation_receipts '
            'WHERE session=? ORDER BY seq DESC LIMIT 1', (session,)).fetchone()[0])
        self.assertEqual(receipt['status'], 'quarantined')
        self.assertNotEqual(receipt['invocation_id'], parent)
        self.assertEqual(stopped.exception.state['runtime']['invocation_id'], receipt['invocation_id'])
        waits = [json.loads(raw) for raw, in self.ledger.db.execute(
            'SELECT record_json FROM protocol_events ORDER BY rowid') if json.loads(raw)['kind'] == 'RESOURCE_WAIT']
        self.assertTrue(waits)
        self.assertIn(state['reason'], canonical(waits[-1]))
        gates = [json.loads(raw) for raw, in self.ledger.db.execute(
            "SELECT payload_json FROM page_audit WHERE event='RESOURCE_GATE' ORDER BY seq")]
        self.assertTrue(gates)
        self.assertIn(state['reason'], canonical(gates[-1]))
        self.reload()
        self.assertEqual(usage(self.ledger, session), charged)
        self.assertTrue(self.ledger.verify_invocations(session))

    def test_expired_rtx_sample_refreshes_the_same_reserved_review_call(self):
        import remote_memory

        primary = self.enable_prefix_fixture()['rtx3090']
        meta = self.new_prefix_review()
        session = meta['active']
        first, first_receipt = self.architect_records(self.architect(meta, primary))
        before = usage(self.ledger, session)
        contract = self.ledger.current_contract(session)
        self.reload()

        original_probe = remote_memory.probe
        original_check = remote_memory.require_dispatch_fresh
        refreshed = []

        def probe(profile, *, warm_binding=None):
            sample = original_probe(profile)
            if warm_binding is not None:
                refreshed.append(warm_binding)
                sample['nonce'] = 'f' * 32
                sample['model'].update(digest_basis='verified_hash_reused_with_stat_and_instance', hash_ms=0)
            return sample

        def delayed_check(provider):
            stamp = provider.memory_plan['hardware']['sample']['wall_time']
            with patch('remote_memory.time.time', return_value=stamp + remote_memory.MAX_SAMPLE_AGE + 1):
                return original_check(provider)

        with patch('remote_memory.probe', side_effect=probe), \
                patch('remote_memory.require_dispatch_fresh', side_effect=delayed_check):
            result = self.architect(meta, primary)
        self.assertTrue(result['result']['admission']['admitted'])
        second, settled = self.architect_records(result)
        refresh = settled['remote_admission_refresh']
        self.assertEqual(len(refreshed), 1)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(refresh['before']['instance'], refresh['after']['instance'])
        self.assertNotEqual(refresh['before']['hardware']['sample']['nonce'],
                            refresh['after']['hardware']['sample']['nonce'])
        self.assertEqual(second['physical_context']['prefix_reuse'], first['physical_context']['prefix_reuse'])
        self.assertIn('def entry()', canonical(self.requests[-1]['body']['messages']))
        self.assertNotIn('INACCESSIBLE_UNDER_REVIEW_SCOPE', canonical(self.requests[-1]['body']['messages']))
        self.assertEqual(self.ledger.current_contract(session), contract)
        self.assertEqual(settled['accounting_settlement']['used_tokens'], 91 + 34)
        after = usage(self.ledger, session)
        self.assertEqual(after['limits'], before['limits'])
        self.assertEqual(after['used']['calls'], before['used']['calls'] + 1)
        self.assertEqual(after['used']['tokens'], before['used']['tokens'] + 91 + 34)
        self.assertEqual(json.loads(self.ledger.db.execute(
            'SELECT record_json FROM invocation_receipts WHERE invocation_id=?', (first['id'],)).fetchone()[0]),
            first_receipt)
        self.assertTrue(self.ledger.verify_invocations(session))

    def test_effective_slot_save_refusal_keeps_review_cache_off(self):
        primary = self.enable_prefix_fixture()['rtx3090']
        meta = self.new_prefix_review()
        type(self).slot_response = (400, b'{"error":{"code":400,"type":"invalid_request_error","message":"Invalid action"}}')
        result = self.architect(meta, primary)
        self.assertTrue(result['result']['admission']['admitted'])
        invocation, receipt = self.architect_records(result)
        self.assertNotIn('prefix_reuse', invocation['physical_context'])
        self.assertFalse(self.requests[-1]['body']['cache_prompt'])
        self.assertEqual(self.slot_checks, [(b'', self.requests[-1]['authorization'])])
        self.assertEqual(receipt['accounting_settlement']['used_tokens'], 125)

    def test_committed_prefix_plan_records_the_final_rounded_context(self):
        import orchestration

        primary = self.enable_prefix_fixture()['rtx3090']
        meta = self.new_prefix_review()
        type(self).count_tokens = staticmethod(lambda body: 1785 if 'cache_reuse' in body else 1784)
        result = self.architect(meta, primary)
        self.assertTrue(result['result']['admission']['admitted'])
        invocation, receipt = self.architect_records(result)
        self.assertEqual(invocation['physical_context']['prompt_tokens'], 1785)
        dispatch = next(event['data'] for event in orchestration.events(
            self.ledger, invocation['orchestration']['node_id'])
            if event['kind'] == 'DISPATCH' and event['data']['resource_id'] == invocation['id'])
        memory = dispatch['resource']['runtime_memory']
        self.assertEqual(memory['context_tokens'], 5120)
        self.assertTrue(memory['prefix_isolation']['slot_saves_disabled'])
        self.assertEqual(receipt['accounting_settlement']['used_tokens'], 1785 + 34)

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
