import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from context_manager import InvalidUpdate
import engine_client as client
from monitoring import ResourceWait


def frame(value): return json.dumps(value).encode() + b'\n'


class SocketFixture:
    def __init__(self, reply): self.reply = reply; self.sent = b''; self.closed = False
    def settimeout(self, seconds): self.timeout = seconds
    def connect(self, endpoint): self.endpoint = endpoint
    def sendall(self, value): self.sent += value
    def makefile(self, mode): return io.BytesIO(self.reply)
    def close(self): self.closed = True


class EngineClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.record = {'version': client.VERSION, 'token': 'a' * 64,
                       'socket': str(client.socket_path(self.root)), 'pid': 12345,
                       'runtime_hash': 'b' * 64, 'registry': str(self.root / 'models.json'),
                       'python': sys.executable}

    def exchange(self, raw, request=None, **options):
        connection = SocketFixture(raw)
        with patch.object(client.socket, 'socket', return_value=connection):
            result = client._exchange(self.record, request or {'op': 'status'}, **options)
        self.assertTrue(connection.closed)
        return result, connection

    def test_authenticated_bounded_frames_keep_thinking_separate(self):
        thoughts, progress = [], []
        result, connection = self.exchange(
            frame({'event': 'thinking', 'text': 'Local diagnostic'}) +
            frame({'event': 'progress', 'data': {'read': 4, 'total': 4}}) +
            frame({'event': 'result', 'data': {'ok': True, 'content': '{}'}}),
            {'op': 'generate', 'ref': 'sutra:engine/test'},
            on_thinking=thoughts.append, on_progress=progress.append)
        self.assertEqual(result['content'], '{}')
        self.assertEqual(thoughts, ['Local diagnostic'])
        self.assertEqual(progress, [{'read': 4, 'total': 4}])
        sent = json.loads(connection.sent)
        self.assertEqual(sent['token'], self.record['token'])
        self.assertEqual(sent['version'], client.VERSION)

    def test_invalid_or_incomplete_response_frames_fail(self):
        for raw in (b'', b'not-json\n', b'[]\n', frame({'event': 'unknown'}),
                    frame({'event': 'thinking', 'text': 1}),
                    frame({'event': 'result', 'data': []}), b'x' * (client.FRAME_LIMIT + 1)):
            with self.subTest(raw=raw[:80]), self.assertRaises((client.EngineServiceError, ValueError)):
                self.exchange(raw)

    def test_missing_line_terminator_cannot_finish_an_exchange(self):
        with self.assertRaises(client.EngineServiceError):
            self.exchange(json.dumps({'event': 'result', 'data': {'ok': True}}).encode())

    def test_native_waits_are_recoverable_and_other_errors_are_specific(self):
        for status in ('busy', 'paused', 'timeout', 'uncertain'):
            with self.subTest(status=status), self.assertRaises(ResourceWait):
                self.exchange(frame({'event': 'result', 'data': {'ok': False, 'status': status,
                                                                 'decision': 'BRIDGE', 'error': 'Wait for resources'}}))
        with self.assertRaisesRegex(client.EngineServiceError, 'profile changed'):
            self.exchange(frame({'event': 'result', 'data': {'ok': False, 'status': 'changed',
                                                             'error': 'The profile changed'}}))

    def test_request_frame_size_is_checked_before_sending(self):
        socket = SocketFixture(b'')
        with patch.object(client.socket, 'socket', return_value=socket):
            with self.assertRaisesRegex(client.EngineServiceError, 'request exceeds'):
                client._exchange(self.record, {'op': 'generate', 'payload': 'x' * client.FRAME_LIMIT})
        self.assertEqual(socket.sent, b'')
        self.assertTrue(socket.closed)

    def test_generation_timeout_preserves_partial_native_usage_for_gateway_settlement(self):
        usage = {'prompt_tokens': 13, 'completion_tokens': 7, 'finish_reason': 'cancelled'}
        reply = frame({'event': 'result', 'data': {'ok': False, 'status': 'timeout',
                                                  'error': 'Time slice reached', 'usage': usage}})
        with self.assertRaisesRegex(TimeoutError, 'Time slice reached') as raised:
            self.exchange(reply, {'op': 'generate'})
        self.assertEqual(raised.exception.engine_usage, usage)
        with self.assertRaises(ResourceWait):
            self.exchange(reply, {'op': 'load'})

    def test_metadata_is_private_bounded_and_bound_to_the_socket_directory(self):
        file = self.root / 'worker.json'
        file.write_text(json.dumps(self.record)); file.chmod(0o600)
        self.assertEqual(client.metadata(self.root)['token'], self.record['token'])
        file.chmod(0o644)
        with self.assertRaises(client.EngineServiceError): client.metadata(self.root)
        file.chmod(0o600)
        file.write_text(json.dumps(dict(self.record, socket='/tmp/foreign.sock')))
        with self.assertRaises(client.EngineServiceError): client.metadata(self.root)
        file.write_bytes(b'x' * 8193)
        with self.assertRaises(client.EngineServiceError): client.metadata(self.root)

    def test_loaded_and_generated_instance_bindings_are_checked(self):
        profile = {'name': 'sutra:engine/test', 'profile_hash': 'a' * 64}
        with patch.object(client, 'ensure_service', return_value=self.record), \
                patch.object(client, '_exchange', return_value={'ok': True, 'profile_hash': 'b' * 64}):
            with self.assertRaisesRegex(client.EngineServiceError, 'Loaded engine profile differs'):
                client.activate(profile)
        with patch.object(client, 'ensure_service', return_value=self.record), \
                patch.object(client, '_exchange', return_value={'ok': True, 'instance_id': 'b' * 32}):
            with self.assertRaisesRegex(client.EngineServiceError, 'instance changed'):
                client.generate(profile, [], 4, False, {}, expected_instance_id='a' * 32)

    def test_generation_transport_allows_native_timeout_and_stop_drain_to_finalize_usage(self):
        profile = {'name': 'sutra:engine/test', 'profile_hash': 'a' * 64}
        with patch.object(client, 'ensure_service', return_value=self.record), \
                patch.object(client, '_exchange', return_value={'ok': True}) as exchange:
            client.generate(profile, [], 4, False, {})
        self.assertGreater(exchange.call_args.kwargs['seconds'], 150 + 10)

    def test_runtime_identity_includes_admission_dependencies_and_policy_data(self):
        # Redirect __file__ into a disposable package fixture. Production sources
        # and the active service are never rewritten to exercise upgrade identity.
        package = self.root / 'package'
        harness = package / 'harness'
        harness.mkdir(parents=True)
        (harness / 'engine_client.py').write_text('# transport v1\n')
        dependency = harness / 'provider_startup.py'
        dependency.write_text('# admission v1\n')
        policies = harness / 'profiles'
        policies.mkdir()
        policy = policies / 'resource.json'
        policy.write_text('{"reserve": 4}\n')
        with patch.object(client, '__file__', str(harness / 'engine_client.py')):
            initial = client.runtime_hash()
            dependency.write_text('# admission v2\n')
            admission_changed = client.runtime_hash()
            self.assertNotEqual(initial, admission_changed)
            policy.write_text('{"reserve": 6}\n')
            self.assertNotEqual(admission_changed, client.runtime_hash())
            baseline = client.runtime_hash()
            (harness / 'test_fake.py').write_text('# fixture change is not executable worker policy\n')
            self.assertEqual(baseline, client.runtime_hash())

    def test_matching_live_worker_is_reused_without_spawning_a_child(self):
        with patch.object(client, 'directory', return_value=self.root), \
                patch.object(client, 'metadata', return_value=self.record), \
                patch.object(client, '_exchange', return_value={'ok': True, 'runtime_hash': 'b' * 64}), \
                patch.object(client, 'runtime_hash', return_value='b' * 64), \
                patch('engine_profiles.registry_path', return_value=Path(self.record['registry'])), \
                patch.dict(os.environ, SUTRA_ENGINE_PYTHON=sys.executable), \
                patch.object(client.subprocess, 'Popen') as spawn:
            self.assertEqual(client.ensure_service(), self.record)
            spawn.assert_not_called()

    def test_busy_worker_cannot_be_replaced_for_another_registry(self):
        with patch.object(client, 'directory', return_value=self.root), \
                patch.object(client, 'metadata', return_value=self.record), \
                patch.object(client, '_exchange', return_value={'ok': True, 'runtime_hash': 'b' * 64,
                                                               'busy': True}) as exchange, \
                patch.object(client, 'runtime_hash', return_value='b' * 64), \
                patch('engine_profiles.registry_path', return_value=self.root / 'new-models.json'), \
                patch.dict(os.environ, SUTRA_ENGINE_PYTHON=sys.executable), \
                patch.object(client.subprocess, 'Popen') as spawn:
            with self.assertRaises(ResourceWait): client.ensure_service()
            self.assertEqual(exchange.call_count, 1)
            spawn.assert_not_called()

    def test_idle_worker_registry_change_starts_one_owned_child_with_clean_environment(self):
        new_registry = self.root / 'new-models.json'
        new_record = dict(self.record, registry=str(new_registry), pid=54321)
        requests = []
        def exchange(record, request, **options):
            requests.append(request['op'])
            return {'ok': True, 'runtime_hash': 'b' * 64, 'busy': False}
        child = SimpleNamespace(pid=54321, poll=lambda: None)
        with patch.object(client, 'directory', return_value=self.root), \
                patch.object(client, 'metadata', side_effect=[self.record, new_record]), \
                patch.object(client, '_exchange', side_effect=exchange), \
                patch.object(client, 'runtime_hash', return_value='b' * 64), \
                patch('engine_profiles.registry_path', return_value=new_registry), \
                patch.dict(os.environ, SUTRA_ENGINE_PYTHON=sys.executable, STRATA_COMPONENTS_DIR='/foreign',
                           PYTHONPATH='/foreign', PYTHONHOME='/foreign', VIRTUAL_ENV='/foreign'), \
                patch.object(client.subprocess, 'Popen', return_value=child) as spawn:
            self.assertEqual(client.ensure_service(), new_record)
            spawn.assert_called_once()
            options = spawn.call_args.kwargs
            for key in ('STRATA_COMPONENTS_DIR', 'PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV'):
                self.assertNotIn(key, options['env'])
            self.assertEqual(requests, ['ping', 'shutdown', 'ping'])

    def test_unavailable_status_does_not_start_a_worker(self):
        with patch.object(client, 'directory', return_value=self.root), \
                patch.object(client, 'metadata', side_effect=FileNotFoundError), \
                patch.object(client.subprocess, 'Popen') as spawn:
            self.assertFalse(client.status()['running'])
            spawn.assert_not_called()


if __name__ == '__main__': unittest.main()
