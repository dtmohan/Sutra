import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from engine_client import _exchange, metadata, signed, VERSION
import engine_profiles
from engine_service import EngineWorker, WorkerError, serve
from test_engine_profiles import descriptor


class FakeParser:
    def __init__(self):
        self.tokens = []; self.stopped = False

    def feed(self, token):
        if token == 0:
            self.stopped = True
        elif not self.stopped:
            self.tokens.append(token)
        return {'content': '' if token == 0 else chr(token), 'thinking': ''}

    def finish(self): return {'content': '', 'thinking': ''}

    def response(self): return {'content': ''.join(chr(token) for token in self.tokens), 'thinking': ''}


class FakeCodec:
    stop_token_ids = [0]
    prompt_count = 3

    def __init__(self, path): self.path = path

    def encode_messages(self, messages, **options): return [1] * self.prompt_count

    def parser(self, **options): return FakeParser()


class FakeEngine:
    created = []

    def __init__(self, profile):
        self.profile = profile; self.stopped = False; self.ready = False
        self.generations = 0; self.fail_start = False
        self.entered = threading.Event(); self.release = None; self.cancel_observed = threading.Event()
        self.generated = None; self.finish = 'eos'; self.tokens = [123, 125]
        type(self).created.append(self)

    def start(self, **options):
        if self.fail_start:
            raise RuntimeError('Owned engine failed to load')
        self.ready = True

    def metadata(self):
        return {'pid': 12345, 'generation_ready': self.ready, 'running': self.ready,
                'context_tokens': self.profile['context_tokens'], 'owned': True}

    def generate(self, tokens, maximum, **options):
        self.generations += 1; self.entered.set()
        if self.release is not None:
            while not self.release.wait(.01):
                if options['cancel_event'].is_set():
                    self.cancel_observed.set()
                    return {'metrics': {'generated': 0, 'finish': 'cancelled'},
                            'finish': 'cancelled', 'cancelled': True}
        for token in self.tokens:
            options['on_token'](token)
        if options.get('on_progress'):
            options['on_progress']({'read': len(tokens), 'total': len(tokens)})
        count = len(self.tokens) if self.generated is None else self.generated
        return {'metrics': {'generated': count, 'finish': self.finish,
                            'prompt_tokens': len(tokens)}, 'finish': self.finish, 'cancelled': False}

    def stop(self): self.stopped = True; self.ready = False


class BlockingLoadEngine(FakeEngine):
    """A bounded fake cold load that only completes by observing cancellation."""
    def start(self, **options):
        self.entered.set()
        until = time.monotonic() + 5
        while time.monotonic() < until:
            if options['cancel_event'].wait(.01):
                self.cancel_observed.set()
                raise RuntimeError('Owned cold load observed cancellation')
        raise TimeoutError('Fake cold load exceeded its test deadline')


class EngineServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.registry = self.root / 'models.json'
        self.profiles = engine_profiles.write({name: descriptor(self.root, name, context)
            for name, context in (('low', 16384), ('high', 32768))}, self.registry)
        FakeEngine.created = []
        self.worker = EngineWorker(self.root / 'worker', self.registry, process_factory=FakeEngine,
                                   codec_factory=FakeCodec, admission=lambda *args, **kwargs: {'admitted': True})
        self.addCleanup(self.worker._stop)

    def request(self, operation, name='low', **extra):
        profile = self.profiles[name]
        request = {'op': operation, 'ref': profile['name'], 'profile_hash': profile['profile_hash']}
        if operation == 'generate':
            request.update(messages=[{'role': 'user', 'content': 'Hello'}], max_tokens=16,
                           context_tokens=profile['context_tokens'], schema={'type': 'object'},
                           thinking=False, expected_instance_id=self.worker.instance_id)
        request.update(extra)
        return request

    def operation(self, request, cancel=None):
        return self.worker.operation(request, lambda *args, **kwargs: None, cancel or threading.Event())

    def test_loading_switching_and_warm_reuse_only_manage_owned_children(self):
        first = self.operation(self.request('load'))
        self.assertEqual(first['profile_hash'], self.profiles['low']['profile_hash'])
        self.assertFalse(first['busy'])
        self.assertEqual(len(FakeEngine.created), 1)
        warm = self.operation(self.request('load'))
        self.assertEqual(first['instance_id'], warm['instance_id'])
        self.assertEqual(len(FakeEngine.created), 1)
        switched = self.operation(self.request('load', 'high'))
        self.assertTrue(FakeEngine.created[0].stopped)
        self.assertNotEqual(first['instance_id'], switched['instance_id'])
        self.assertEqual(switched['context_tokens'], 32768)
        self.assertEqual(len(FakeEngine.created), 2)

    def test_failed_load_cannot_return_a_stale_ready_instance(self):
        self.operation(self.request('load'))
        def fail(profile):
            engine = FakeEngine(profile); engine.fail_start = True; return engine
        self.worker.process_factory = fail
        with self.assertRaisesRegex(RuntimeError, 'failed to load'):
            self.operation(self.request('load', 'high'))
        observed = self.worker.status()
        self.assertIsNone(observed['instance_id'])
        self.assertIsNone(observed['profile'])
        self.assertTrue(FakeEngine.created[-1].stopped)

    def test_changed_profile_and_generation_instance_are_rejected(self):
        with self.assertRaisesRegex(WorkerError, 'profile changed'):
            self.operation(self.request('load', profile_hash='f' * 64))
        self.assertEqual(FakeEngine.created, [])
        self.operation(self.request('load'))
        with self.assertRaisesRegex(WorkerError, 'instance changed'):
            self.operation(self.request('generate', expected_instance_id='f' * 32))
        self.assertEqual(self.worker.engine.generations, 0)

    def test_denied_cold_load_returns_to_idle_without_a_model(self):
        self.worker.admission = lambda *args, **kwargs: {
            'admitted': False, 'decision': 'BRIDGE', 'reason': 'Fresh pressure check'}
        with self.assertRaisesRegex(WorkerError, 'Fresh pressure'):
            self.operation(self.request('load'))
        self.assertEqual(self.worker.phase, 'idle')
        self.assertFalse(self.worker.lock.locked())
        self.assertIsNone(self.worker.profile)
        self.assertEqual(FakeEngine.created, [])

    def test_cancelled_load_stops_the_owned_child_and_never_commits_readiness(self):
        cancel = threading.Event()
        def cancelled_start(engine, **options):
            self.assertIs(options['cancel_event'], cancel)
            cancel.set()
            engine.ready = True
        with patch.object(FakeEngine, 'start', cancelled_start):
            with self.assertRaisesRegex(WorkerError, 'loading was cancelled'):
                self.worker.operation(self.request('load'), lambda *args, **kwargs: None, cancel)
        self.assertTrue(FakeEngine.created[-1].stopped)
        self.assertIsNone(self.worker.instance_id)
        self.assertEqual(self.worker.phase, 'idle')
        self.assertFalse(self.worker.lock.locked())
        self.operation(self.request('load'))
        self.assertIsNotNone(self.worker.instance_id)

    def test_actual_tokenized_context_is_checked_before_generation(self):
        self.operation(self.request('load'))
        self.worker.codec.prompt_count = 100
        with self.assertRaisesRegex(WorkerError, 'Actual tokenized prompt'):
            self.operation(self.request('generate', context_tokens=110, max_tokens=16))
        self.assertEqual(self.worker.engine.generations, 0)

    def test_generation_counts_and_finish_are_observed(self):
        self.operation(self.request('load'))
        result = self.operation(self.request('generate'))
        self.assertEqual(result['content'], '{}')
        self.assertEqual(result['prompt_tokens'], 3)
        self.assertEqual(result['completion_tokens'], 2)
        self.assertEqual(result['finish_reason'], 'stop')

    def test_native_done_counts_include_tokens_drained_after_a_stop(self):
        self.operation(self.request('load'))
        self.worker.engine.tokens = [123, 125, 0]
        self.worker.engine.generated = 5
        self.worker.engine.finish = 'stop_token'
        result = self.operation(self.request('generate'))
        self.assertEqual(result['completion_tokens'], 5)
        self.assertEqual(result['content'], '{}')
        self.assertEqual(result['finish_reason'], 'stop')

    def test_drained_timeout_passes_native_usage_to_the_client_without_completion(self):
        self.operation(self.request('load'))
        directory, record = self.start_server()
        error = TimeoutError('Owned generation time slice reached')
        error.engine_result = {'metrics': {'generated': 7}, 'cancelled': True, 'tokens': [123, 125]}
        with patch.object(self.worker.engine, 'generate', side_effect=error):
            with self.assertRaisesRegex(TimeoutError, 'time slice reached') as raised:
                _exchange(record, self.request('generate'))
        self.assertEqual(raised.exception.engine_usage['prompt_tokens'], 3)
        self.assertEqual(raised.exception.engine_usage['completion_tokens'], 7)
        self.assertEqual(raised.exception.engine_usage['finish_reason'], 'cancelled')
        self.assertFalse(self.worker.lock.locked())
        self.assertEqual(self.worker.phase, 'idle')

    def test_timeout_without_a_new_observation_cannot_reuse_a_previous_generation_count(self):
        self.operation(self.request('load'))
        self.operation(self.request('generate'))
        self.assertIsNotNone(self.worker.last_generation)
        with patch.object(self.worker.engine, 'generate', side_effect=TimeoutError('No drained usage')):
            with self.assertRaises(TimeoutError):
                self.operation(self.request('generate'))
        self.assertIsNone(self.worker.last_generation)

    def test_unknown_finish_and_invalid_controls_cannot_claim_completion(self):
        self.operation(self.request('load'))
        self.worker.engine.finish = 'mystery'
        with self.assertRaisesRegex(WorkerError, 'known completion reason'):
            self.operation(self.request('generate'))
        for changes in ({'context_tokens': 0}, {'context_tokens': False}, {'thinking': 1}):
            with self.subTest(changes=changes), self.assertRaises(WorkerError):
                self.operation(self.request('generate', **changes))

    def test_owned_dead_engine_can_be_reloaded_without_returning_its_old_instance(self):
        first = self.operation(self.request('load'))
        engine = self.worker.engine; engine.ready = False
        recovered = self.operation(self.request('load'))
        self.assertTrue(engine.stopped)
        self.assertNotEqual(first['instance_id'], recovered['instance_id'])
        self.assertEqual(len(FakeEngine.created), 2)

    def test_busy_worker_allows_status_but_refuses_another_load_or_unload(self):
        self.operation(self.request('load'))
        engine = self.worker.engine
        engine.release = threading.Event()
        outcome = []
        thread = threading.Thread(target=lambda: outcome.append(self.operation(self.request('generate'))))
        thread.start()
        try:
            self.assertTrue(engine.entered.wait(2))
            started = time.monotonic()
            self.assertTrue(self.operation({'op': 'status'})['busy'])
            self.assertLess(time.monotonic() - started, .2)
            for operation in ('load', 'unload'):
                with self.assertRaisesRegex(WorkerError, 'active operation'):
                    self.operation(self.request(operation))
            self.assertFalse(engine.stopped)
        finally:
            engine.release.set(); thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome[0]['content'], '{}')

    def start_server(self):
        ready, stop = threading.Event(), threading.Event()
        directory = self.root / 'server'
        context = patch('engine_service.runtime_hash', return_value='b' * 64)
        context.start(); self.addCleanup(context.stop)
        thread = threading.Thread(target=serve, args=(directory, self.registry),
                                  kwargs={'worker': self.worker, 'ready': ready, 'stop': stop}, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(3))
        self.server_stop, self.server_thread = stop, thread
        def cleanup():
            stop.set(); thread.join(4)
            self.assertFalse(thread.is_alive())
        self.addCleanup(cleanup)
        return directory, metadata(directory)

    def begin_cold_socket_load(self):
        import socket
        self.worker.process_factory = BlockingLoadEngine
        directory, record = self.start_server()
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(peer.close)
        peer.connect(record['socket'])
        request = self.request('load')
        request = signed({'version': VERSION, 'nonce': 'c' * 32, 'sent_at': int(time.time()), 'request': request}, record['token'])
        peer.sendall(json.dumps(request).encode() + b'\n')
        until = time.monotonic() + 2
        while self.worker.loading_engine is None and time.monotonic() < until:
            time.sleep(.01)
        engine = self.worker.loading_engine
        self.assertIsNotNone(engine, 'The fake cold load must begin before cancellation')
        self.assertTrue(engine.entered.wait(2))
        self.assertIsNone(self.worker.engine, 'An unready cold child must not be published')
        return directory, peer, engine

    def test_socket_disconnect_during_cold_load_stops_owned_child_before_reuse(self):
        _, peer, engine = self.begin_cold_socket_load()
        peer.close()
        self.assertTrue(engine.cancel_observed.wait(2), 'Peer disconnect must reach the cold-load cancel event')
        until = time.monotonic() + 2
        while self.worker.lock.locked() and time.monotonic() < until:
            time.sleep(.01)
        self.assertFalse(self.worker.lock.locked())
        self.assertTrue(engine.stopped)
        self.assertIsNone(self.worker.loading_engine)
        self.assertIsNone(self.worker.instance_id)
        self.assertIsNone(self.worker.profile)
        self.assertEqual(self.worker.phase, 'idle')
        self.worker.process_factory = FakeEngine
        recovered = self.operation(self.request('load'))
        self.assertEqual(recovered['profile_hash'], self.profiles['low']['profile_hash'])
        self.assertTrue(self.worker.engine.metadata()['generation_ready'])

    def test_service_stop_during_cold_load_cancels_and_joins_before_removing_identity(self):
        directory, _, engine = self.begin_cold_socket_load()
        self.server_stop.set()
        self.server_thread.join(3)
        self.assertFalse(self.server_thread.is_alive(), 'Service shutdown must join the cancelled load')
        self.assertTrue(engine.cancel_observed.is_set())
        self.assertTrue(engine.stopped)
        self.assertFalse(self.worker.lock.locked())
        self.assertIsNone(self.worker.loading_engine)
        self.assertIsNone(self.worker.engine)
        self.assertIsNone(self.worker.instance_id)
        self.assertFalse((directory / 'worker.json').exists(), 'No stale worker identity may survive shutdown')

    def test_replayed_authenticated_request_is_rejected_before_operation_dispatch(self):
        _, record = self.start_server()
        with patch('engine_client.secrets.token_hex', return_value='e' * 32):
            self.assertTrue(_exchange(record, {'op': 'status'})['ok'])
            with self.assertRaisesRegex(RuntimeError, 'nonce was replayed'):
                _exchange(record, {'op': 'status'})
        self.assertIsNone(self.worker.engine)

    def test_real_socket_authentication_and_result_framing(self):
        from engine_client import EngineServiceError
        directory, record = self.start_server()
        wrong = dict(record, token='f' * 64)
        with self.assertRaises(EngineServiceError):
            _exchange(wrong, {'op': 'status'})
        result = _exchange(record, {'op': 'ping'})
        self.assertEqual(result['runtime_hash'], 'b' * 64)
        loaded = _exchange(record, self.request('load'))
        result = _exchange(record, self.request('generate', expected_instance_id=loaded['instance_id']))
        self.assertEqual(result['content'], '{}')
        self.assertEqual(self.worker.engine.generations, 1)

    def test_disconnected_client_cancels_once_before_stream_reuse(self):
        import socket
        directory, record = self.start_server()
        loaded = _exchange(record, self.request('load'))
        engine = self.worker.engine; engine.release = threading.Event()
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        peer.connect(record['socket'])
        request = self.request('generate', expected_instance_id=loaded['instance_id'])
        request = signed({'version': VERSION, 'nonce': 'd' * 32, 'sent_at': int(time.time()), 'request': request}, record['token'])
        peer.sendall(json.dumps(request).encode() + b'\n')
        self.assertTrue(engine.entered.wait(2)); peer.close()
        self.assertTrue(engine.cancel_observed.wait(3))
        for _ in range(100):
            if not self.worker.lock.locked(): break
            time.sleep(.01)
        self.assertFalse(self.worker.lock.locked())
        self.assertEqual(engine.generations, 1)
        engine.release.set()
        result = _exchange(record, self.request('generate', expected_instance_id=loaded['instance_id']))
        self.assertEqual(result['content'], '{}')
        self.assertEqual(engine.generations, 2)


if __name__ == '__main__': unittest.main()
