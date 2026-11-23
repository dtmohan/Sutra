import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate, canonical
from karma_adapter import KarmaAdapter
from kernel_boundary import KernelBoundary
from monitoring import ResourceWait
from paging import Budget
import remote_memory as memory
from remote_profiles import normalize


GIB = 1024**3


def profile():
    return normalize({'id': 'rtx3090', 'ssh_alias': 'sutra-rtx3090', 'port': 18082,
        'remote_port': 18080, 'api_key_file': '/private/remote.key',
        'model': 'qwen3-4b-rtx', 'model_path': r'C:\Users\Deepak\SutraRuntime\models\qwen3-4b.gguf',
        'model_sha256': '1' * 64, 'context_tokens': 8192,
        'gpu_uuid': 'GPU-b90a1b75-3646-10a8-9987-8f6c6ccfdee0',
        'executable_sha256': '2' * 64})


def observation(config=None):
    p = config or profile()
    return {'profile': memory.PROFILE, 'version': 1, 'nonce': '0' * 32,
        'profile_hash': p['profile_hash'], 'wall_time': 100., 'capture_started_at': 98.,
        'capture_seconds': 2., 'host_id': 'RTX-PC', 'boot_id': '2026-11-20T10:00:00Z',
        'listener': {'address': '127.0.0.1', 'port': p['remote_port'], 'pid': 1234},
        'process': {'pid': 1234, 'started_at': '2026-11-20T12:00:00Z',
            'executable': r'C:\Users\Deepak\SutraRuntime\bin\llama-server.exe',
            'executable_sha256': '2' * 64, 'model_path': p['model_path'],
            'model': p['model'], 'context_tokens': str(p['context_tokens']), 'gpu_layers': 'all', 'gpu_device': 'CUDA0'},
        'model': {'path': p['model_path'], 'sha256': p['model_sha256'], 'bytes': 2497280480,
            'mtime_ticks': '639000000000000000', 'creation_ticks': '638999990000000000',
            'digest_basis': 'fresh_full_hash', 'hash_ms': 1000.},
        'gpu': {'index': 0, 'uuid': p['gpu_uuid'], 'name': 'NVIDIA GeForce RTX 3090',
            'driver': '560.94', 'total_bytes': 24 * GIB, 'free_bytes': 18 * GIB,
            'used_bytes': 6 * GIB, 'process_pids': [321, 1234]},
        'ram': {'total_bytes': 32 * GIB, 'free_bytes': 12 * GIB},
        'server': {'models': [p['model']], 'model_path': p['model_path'], 'context_tokens': p['context_tokens'],
            'total_slots': 1, 'is_sleeping': False, 'slots': [{'id': 0, 'is_processing': False}]}}


class RemoteMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profile = profile()
        self.sample = observation(self.profile)
        with memory._verified_lock:
            memory._verified.clear()

    def tearDown(self):
        self.temp.cleanup()

    def evaluate(self, sample=None, requested=4096, **kwargs):
        return memory.evaluate(self.profile, sample or self.sample, requested, now=101, **kwargs)

    def test_loaded_weights_and_context_are_not_charged_twice(self):
        self.sample['model']['bytes'] = 18 * GIB
        self.sample['gpu'].update(free_bytes=2 * GIB, used_bytes=22 * GIB)
        original = copy.deepcopy(self.sample)
        result = self.evaluate()
        self.assertTrue(result['admitted'])
        self.assertEqual(result['estimated_increment_bytes'], 128 * 1024**2)
        self.assertIsNone(result['cold_estimate_bytes'])
        self.assertEqual(result['resident_context_tokens'], 8192)
        self.assertFalse(result['peak_memory_verified'])
        self.assertEqual(self.sample, original)

    def test_remote_gpu_and_host_ram_each_use_the_unchanged_kernel(self):
        adapter = KarmaAdapter(KernelBoundary(.3))
        result = self.evaluate(adapter=adapter)
        self.assertEqual(result['k'], .3)
        self.assertEqual(result['decision'], 'ALLOW')
        self.assertEqual(result['hardware']['gpu_allocation']['adapter'], 'karma')
        self.sample['gpu'].update(free_bytes=GIB, used_bytes=23 * GIB)
        self.assertNotEqual(self.evaluate()['decision'], 'ALLOW')
        self.sample = observation(self.profile)
        self.sample['ram']['free_bytes'] = 2 * GIB
        self.assertNotEqual(self.evaluate()['decision'], 'ALLOW')

    def test_device_runtime_file_and_listener_changes_all_fail_closed(self):
        changes = [('gpu', 'uuid', 'GPU-11111111-1111-1111-1111-111111111111'),
                   ('gpu', 'index', 1), ('gpu', 'process_pids', [321]),
                   ('listener', 'pid', 321), ('listener', 'address', '0.0.0.0'),
                   ('listener', 'port', 18081), ('process', 'executable_sha256', '3' * 64),
                   ('process', 'gpu_device', 'CPU'), ('process', 'gpu_layers', '0'),
                   ('process', 'model_path', r'C:\different.gguf'),
                   ('process', 'model', 'other'), ('model', 'sha256', '4' * 64),
                   ('model', 'path', r'C:\different.gguf'),
                   ('server', 'model_path', r'C:\different.gguf'),
                   ('server', 'models', ['other'])]
        for section, key, value in changes:
            with self.subTest(section=section, key=key):
                changed = copy.deepcopy(self.sample)
                changed[section][key] = value
                result = self.evaluate(changed)
                self.assertFalse(result['admitted'])
                self.assertEqual(result['decision'], 'BRIDGE')

    def test_missing_model_and_malformed_or_stale_measurements_do_not_establish_pressure(self):
        for key, value in (('wall_time', 90.), ('wall_time', 102.),
                           ('capture_seconds', 21), ('capture_seconds', float('nan')),
                           ('capture_started_at', 102.), ('profile_hash', 'x'), ('nonce', 'bad')):
            changed = copy.deepcopy(self.sample)
            changed[key] = value
            result = self.evaluate(changed)
            self.assertFalse(result['available'])
            self.assertFalse(result['admitted'])
            self.assertEqual(result['blocker'], 'measurement_unavailable')
        for section in ('model', 'process', 'gpu', 'ram', 'server', 'listener'):
            changed = copy.deepcopy(self.sample)
            del changed[section]
            self.assertFalse(self.evaluate(changed)['admitted'])

    def test_invalid_memory_counters_are_rejected_before_adapter_evaluation(self):
        for section, key, value in (('gpu', 'free_bytes', True), ('gpu', 'free_bytes', -1),
                ('gpu', 'used_bytes', 30 * GIB), ('gpu', 'free_bytes', 30 * GIB),
                ('gpu', 'free_bytes', 20 * GIB), ('ram', 'free_bytes', 40 * GIB),
                ('ram', 'total_bytes', 0), ('model', 'mtime_ticks', 639000000000000000)):
            changed = copy.deepcopy(self.sample)
            changed[section][key] = value
            result = self.evaluate(changed)
            self.assertFalse(result['admitted'])
            self.assertFalse(result['available'])

    def test_driver_reserved_vram_is_not_misclassified_or_counted_as_free(self):
        self.sample['gpu']['free_bytes'] -= 249 * 1024**2
        result = self.evaluate()
        self.assertTrue(result['admitted'])
        self.assertEqual(result['available_bytes'], 18 * GIB - 249 * 1024**2)

    def test_context_growth_and_changed_preallocated_pool_require_recovery(self):
        self.assertFalse(self.evaluate(requested=16384)['admitted'])
        self.assertTrue(self.evaluate(requested=8192)['admitted'])
        self.sample['server']['context_tokens'] = 4096
        self.assertFalse(self.evaluate(requested=2048)['admitted'])

    def test_larger_configured_resident_pool_admits_a_smaller_packet_context(self):
        p = normalize(dict(self.profile, context_tokens=32768, profile_hash=None))
        sample = observation(p)
        result = memory.evaluate(p, sample, 8192, now=101)
        self.assertTrue(result['admitted'])
        self.assertEqual(result['context_tokens'], 8192)
        self.assertEqual(result['resident_context_tokens'], 32768)

    def test_busy_or_unknown_remote_activity_cannot_be_mistaken_for_idle_residency(self):
        self.sample['server']['slots'][0]['is_processing'] = True
        blocked = self.evaluate()
        self.assertEqual(blocked['blocker'], 'runtime_busy')
        self.assertFalse(blocked['admitted'])
        self.sample['server']['slots'][0]['is_processing'] = None
        self.assertFalse(self.evaluate()['admitted'])
        self.sample['server']['slots'] = []
        self.assertFalse(self.evaluate()['admitted'])

    def test_nonce_binds_probe_and_large_incidental_metadata_is_not_retained(self):
        response = copy.deepcopy(self.sample)
        response['nonce'] = 'wrong'
        with patch('remote_control.ensure_tunnel'), patch('remote_control.run_ps', return_value=response), \
                patch.object(memory.secrets, 'token_hex', return_value='0' * 32):
            with self.assertRaisesRegex(InvalidUpdate, 'fresh observation'):
                memory.probe(self.profile)
        self.sample['incidental_server_text'] = 'x' * 100000
        result = self.evaluate()
        self.assertTrue(result['admitted'])
        self.assertNotIn('incidental_server_text', result['hardware']['sample'])
        self.assertLess(len(canonical(result)), 16384)

    def test_verify_instance_is_lightweight_and_never_uses_an_idle_claim(self):
        instance = self.evaluate()['instance']
        current = {key: instance[key] for key in ('pid', 'started_at', 'executable', 'host_id', 'boot_id', 'model_file')}
        current['address'] = '127.0.0.1'
        with patch('remote_control.run_ps', return_value=current) as ps, \
                patch('remote_control.request_json', side_effect=AssertionError('HTTP admission')):
            memory.verify_instance(self.profile, instance)
        self.assertNotIn('Get-FileHash', ps.call_args.args[1])
        current['pid'] += 1
        with patch('remote_control.run_ps', return_value=current):
            with self.assertRaisesRegex(InvalidUpdate, 'instance changed'):
                memory.verify_instance(self.profile, instance)
        current['pid'] -= 1
        current['model_file'] = dict(current['model_file'], mtime_ticks='639000000000000001')
        with patch('remote_control.run_ps', return_value=current):
            with self.assertRaisesRegex(InvalidUpdate, 'instance changed'):
                memory.verify_instance(self.profile, instance)

    def test_dispatch_preserves_payload_permissions_budget_and_holds_existing_lease(self):
        plan = self.evaluate()
        provider = SimpleNamespace(profile=self.profile, model=self.profile['name'],
            budget=Budget(8192, 1024), remote_instance=None, thinking=None,
            fresh_context_metadata=lambda: {})
        payload = {'request': 'review', 'recent_turns': ['retain this fact'],
                   'contract': {'constraints': {'network_allowed': False, 'edits_allowed': False}}}
        before = canonical(payload)
        budget = provider.budget
        env = {'SUTRA_RUNTIME_DIR': str(self.root / 'runtime'),
               'SUTRA_REMOTE_MODELS': json.dumps([self.profile['name']])}
        with patch.dict(os.environ, env), patch.object(memory, '_observe', return_value=plan), \
                patch('monitoring.require_resources', side_effect=AssertionError('Mac RAM gate')):
            with memory.dispatch(provider, self.root, 'call-one', 'system', payload, {}) as admitted:
                self.assertIs(admitted, plan)
                self.assertEqual(provider.remote_instance, plan['instance'])
                import runtime_memory
                with self.assertRaises(ResourceWait):
                    with runtime_memory.lease():
                        self.fail('Remote dispatch must hold the existing global inference lease')
        self.assertEqual(canonical(payload), before)
        self.assertIs(provider.budget, budget)
        self.assertEqual(provider.memory_plan['hardware']['sample']['gpu']['uuid'], self.profile['gpu_uuid'])

    def test_remote_identity_change_or_memory_block_prevents_any_model_dispatch(self):
        provider = SimpleNamespace(profile=self.profile, model=self.profile['name'],
            budget=Budget(8192, 1024), remote_instance={'pid': 999}, thinking=None,
            fresh_context_metadata=lambda: {})
        env = {'SUTRA_RUNTIME_DIR': str(self.root / 'runtime'),
               'SUTRA_REMOTE_MODELS': json.dumps([self.profile['name']])}
        with patch.dict(os.environ, env), patch.object(memory, '_observe', return_value=self.evaluate()):
            with self.assertRaisesRegex(ResourceWait, 'instance changed'):
                with memory.dispatch(provider, self.root, 'call-one', 'system', {}, {}):
                    self.fail('No model call may occur')
        with patch.dict(os.environ, env), patch.object(memory, '_observe', return_value=memory._blocked('stale')):
            with self.assertRaises(ResourceWait) as blocked:
                with memory.dispatch(provider, self.root, 'call-two', 'system', {}, {}):
                    self.fail('No model call may occur')
            self.assertFalse(blocked.exception.state['runtime']['inference_started'])

    def test_display_snapshot_expires_and_never_probes_or_releases_a_model(self):
        plan = self.evaluate()
        with patch.object(memory.time, 'time', return_value=101):
            memory._save(self.root, plan)
        with patch.object(memory, 'probe', side_effect=AssertionError('Monitor must not probe')):
            current = memory.snapshot(self.root, now=102)
            expired = memory.snapshot(self.root, now=110)
        self.assertTrue(current['available'])
        self.assertEqual(current['sample']['vram_total_bytes'], 24 * GIB)
        self.assertEqual(current['sample']['host_available_bytes'], 12 * GIB)
        self.assertFalse(expired['available'])
        self.assertFalse(expired['admitted'])
        self.assertIsNone(expired['decision'])
        self.assertEqual(expired['last_admission']['decision'], 'ALLOW')
        self.assertEqual(expired['last_admission']['model'], self.profile['name'])

    def test_invalid_display_file_is_unavailable_and_cannot_be_used_for_admission(self):
        path = self.root / 'remote-memory.json'
        for content in ('not JSON', '{}', '{"version":1,"plan":{"hardware":[]}}'):
            path.write_text(content)
            path.chmod(0o600)
            self.assertFalse(memory.snapshot(self.root)['available'])
        outside = self.root / 'outside.json'
        outside.write_text('{}')
        path.unlink()
        path.symlink_to(outside)
        self.assertFalse(memory.snapshot(self.root)['available'])

    def test_failed_refresh_retains_last_observation_without_admitting_from_it(self):
        plan = self.evaluate()
        with patch.object(memory.time, 'time', return_value=101):
            memory._save(self.root, plan)
        with patch.object(memory.time, 'time', return_value=102):
            blocked = memory._blocked('Remote host is unavailable')
            blocked['model'] = self.profile['name']
            memory._save(self.root, blocked)
        result = memory.snapshot(self.root, now=103)
        self.assertFalse(result['available'])
        self.assertFalse(result['admitted'])
        self.assertEqual(result['sample']['gpu_uuid'], self.profile['gpu_uuid'])
        self.assertEqual(result['last_admission']['decision'], 'BRIDGE')
        self.assertEqual(result['last_admission']['observed_at'], 102)

    def test_pre_post_check_never_refreshes_or_grants_from_a_stale_measurement(self):
        plan = self.evaluate()
        provider = SimpleNamespace(profile=self.profile, memory_plan=plan,
            remote_instance=plan['instance'], runtime_context_tokens=4096)
        with patch.object(memory, 'probe', side_effect=AssertionError('No implicit retry')), \
                patch.object(memory.time, 'time', return_value=101):
            self.assertTrue(memory.require_dispatch_fresh(provider)['admitted'])
        with patch.object(memory, 'probe', side_effect=AssertionError('No implicit retry')), \
                patch.object(memory.time, 'time', return_value=110):
            with self.assertRaises(ResourceWait) as blocked:
                memory.require_dispatch_fresh(provider)
        self.assertEqual(blocked.exception.state['runtime']['stage'], 'remote_pre_transport')
        self.assertFalse(blocked.exception.state['runtime']['inference_started'])

    def refresh_fixture(self, fresh=None):
        """A real private inference lease, with only hardware transport mocked."""
        now = [101.]
        latest = copy.deepcopy(self.sample) if fresh is None else fresh
        if fresh is None:
            latest.update(wall_time=110., capture_started_at=109., capture_seconds=1., nonce='1' * 32)
            latest['model'].update(hash_ms=0., digest_basis='verified_hash_reused_with_stat_and_instance')
        provider = SimpleNamespace(model=self.profile['name'], profile=copy.deepcopy(self.profile),
            budget=Budget(8192, 1024, 256), fresh_context_metadata=lambda: {},
            task_context={'fact': 'Cedar CED724', 'read_only': True, 'network_allowed': False},
            consumed={'calls': 3, 'tokens': 10240, 'source_bytes': 9000})
        memory._remember(self.profile, self.sample)
        mocks = [patch.dict(os.environ, SUTRA_RUNTIME_DIR=str(self.root / 'runtime'),
                    SUTRA_REMOTE_MODELS=json.dumps([self.profile['name']])),
                 patch('runtime_memory.dispatch_context', return_value=4096),
                 patch('remote_profiles.load', return_value=self.profile),
                 patch.object(memory.time, 'time', side_effect=lambda: now[0]),
                 patch.object(memory, 'probe', side_effect=[copy.deepcopy(self.sample), latest])]
        values = []
        for mocked in mocks:
            values.append(mocked.start())
            self.addCleanup(mocked.stop)
        return provider, now, values[-1]

    def test_age_only_refresh_holds_lease_keeps_context_permissions_spending_and_original_plan(self):
        from invocation_gateway import _permit
        import runtime_memory
        provider, now, probe = self.refresh_fixture()
        unchanged = copy.deepcopy((vars(provider.budget), provider.task_context, provider.consumed))
        permit = {'hash': 'committed-original', 'used': True, 'transport_seconds': 150}
        token = _permit.set(permit)
        self.addCleanup(_permit.reset, token)
        with memory.dispatch(provider, self.root, 'same-call', 'system', {}, {}) as original:
            before = copy.deepcopy(original)
            # Prefix counting may refine this logical packet allocation within
            # the same fixed, verified native context pool.
            provider.runtime_context_tokens = 5120
            now[0] = 110.
            with self.assertRaises(ResourceWait):
                with runtime_memory.lease():
                    self.fail('The existing shared inference lease was released')
            fresh = memory.require_dispatch_fresh(provider)
            self.assertTrue(fresh['admitted'])
            self.assertEqual(fresh['context_tokens'], 5120)
            self.assertEqual(fresh['instance'], before['instance'])
            self.assertEqual(original, before)
            self.assertEqual((vars(provider.budget), provider.task_context, provider.consumed), unchanged)
            self.assertEqual(permit, {'hash': 'committed-original', 'used': True, 'transport_seconds': 150})
            report = fresh['dispatch_refresh']
            self.assertEqual(report['reason'], 'sample_age_only')
            self.assertTrue(report['warm_only'])
            self.assertFalse(report['inference_started'])
            self.assertEqual((report['attempt'], report['max_attempts']), (1, 1))
            self.assertEqual(report['before']['hardware']['sample']['wall_time'], 100.)
            self.assertEqual(report['after']['hardware']['sample']['wall_time'], 110.)
            self.assertEqual(provider.dispatch_refresh, report)
            self.assertEqual(provider.memory_plan, fresh)
            self.assertTrue(memory.require_dispatch_fresh(provider)['admitted'])
            self.assertEqual(probe.call_count, 2)  # Initial admission plus one metadata refresh.
            self.assertEqual(probe.call_args.kwargs,
                             {'warm_binding': memory._cache_binding(self.sample)})
            now[0] = 120.
            with self.assertRaises(ResourceWait) as stopped:
                memory.require_dispatch_fresh(provider)
            self.assertEqual(stopped.exception.state['runtime']['stage'], 'remote_pre_transport')
            self.assertEqual(stopped.exception.state['dispatch_refresh'], report)
            self.assertEqual(probe.call_count, 2)
        self.assertIsNone(memory._dispatch_scope.get())

    def test_age_refresh_never_accepts_capacity_contention_identity_or_invalid_new_evidence(self):
        cases = ('capacity', 'busy', 'restarted', 'model', 'context', 'malformed', 'stale', 'replayed_nonce')
        for case in cases:
            with self.subTest(case=case):
                fresh = copy.deepcopy(self.sample)
                fresh.update(wall_time=110., capture_started_at=109., capture_seconds=1., nonce='1' * 32)
                fresh['model'].update(hash_ms=0., digest_basis='verified_hash_reused_with_stat_and_instance')
                if case == 'capacity':
                    fresh['gpu'].update(free_bytes=GIB, used_bytes=23 * GIB)
                elif case == 'busy':
                    fresh['server']['slots'][0]['is_processing'] = True
                elif case == 'restarted':
                    fresh['listener']['pid'] = fresh['process']['pid'] = 4321
                    fresh['gpu']['process_pids'] = [4321]
                elif case == 'model':
                    fresh['model']['sha256'] = 'f' * 64
                elif case == 'context':
                    fresh['server']['context_tokens'] = 4096
                elif case == 'malformed':
                    del fresh['model']['bytes']
                elif case == 'stale':
                    fresh.update(wall_time=100., capture_started_at=99.)
                else:
                    fresh['nonce'] = self.sample['nonce']
                provider, now, probe = self.refresh_fixture(fresh)
                with memory.dispatch(provider, self.root, 'same-call', 'system', {}, {}) as initial:
                    before = copy.deepcopy(initial)
                    now[0] = 110.
                    with self.assertRaises(ResourceWait) as stopped:
                        memory.require_dispatch_fresh(provider)
                    self.assertFalse(stopped.exception.state['admitted'])
                    self.assertEqual(stopped.exception.state['runtime']['stage'], 'remote_pre_transport')
                    self.assertFalse(stopped.exception.state['runtime']['inference_started'])
                    self.assertFalse(stopped.exception.state['dispatch_refresh']['after']['admitted'])
                    self.assertEqual(provider.memory_plan, before)
                    self.assertEqual(probe.call_count, 2)
                    with self.assertRaises(ResourceWait):
                        memory.require_dispatch_fresh(provider)
                    self.assertEqual(probe.call_count, 2)

    def test_reused_provider_does_not_attach_a_previous_calls_refresh_report(self):
        provider, now, probe = self.refresh_fixture()
        with memory.dispatch(provider, self.root, 'call-one', 'system', {}, {}):
            now[0] = 110.
            self.assertTrue(memory.require_dispatch_fresh(provider)['admitted'])
            self.assertIsNotNone(provider.dispatch_refresh)
        next_sample = copy.deepcopy(provider.memory_plan['hardware']['sample'])
        probe.side_effect = [next_sample]
        with memory.dispatch(provider, self.root, 'call-two', 'system', {}, {}):
            self.assertIsNone(provider.dispatch_refresh)
            current = memory.require_dispatch_fresh(provider)
            self.assertTrue(current['admitted'])
            self.assertNotIn('dispatch_refresh', current)
            self.assertNotIn('dispatch_refresh', provider.memory_plan)
        self.assertEqual(probe.call_count, 3)

    def test_only_age_can_refresh_an_originally_valid_same_instance_sample(self):
        for case in ('capacity', 'busy', 'bad_capture', 'missing', 'instance', 'context', 'profile', 'other_provider'):
            with self.subTest(case=case):
                provider, now, probe = self.refresh_fixture()
                with memory.dispatch(provider, self.root, 'same-call', 'system', {}, {}):
                    now[0] = 110.
                    sample = provider.memory_plan['hardware']['sample']
                    if case == 'capacity':
                        sample['gpu'].update(free_bytes=GIB, used_bytes=23 * GIB)
                    elif case == 'busy':
                        sample['server']['slots'][0]['is_processing'] = True
                    elif case == 'bad_capture':
                        sample['capture_seconds'] = 21.
                    elif case == 'missing':
                        sample.pop('model')
                    elif case == 'instance':
                        provider.remote_instance = dict(provider.remote_instance, pid=4321)
                    elif case == 'context':
                        provider.runtime_context_tokens = 16384
                    elif case == 'profile':
                        provider.profile = dict(provider.profile, profile_hash='f' * 64)
                    else:
                        provider = SimpleNamespace(**vars(provider))
                    with self.assertRaises(ResourceWait):
                        memory.require_dispatch_fresh(provider)
                    self.assertEqual(probe.call_count, 1)

    def test_refresh_requires_current_assignment_and_registered_profile(self):
        for case in ('revoked', 'changed'):
            with self.subTest(case=case):
                provider, now, probe = self.refresh_fixture()
                with memory.dispatch(provider, self.root, 'same-call', 'system', {}, {}):
                    now[0] = 110.
                    change = (patch.dict(os.environ, SUTRA_REMOTE_MODELS='[]') if case == 'revoked' else
                              patch('remote_profiles.load', return_value=dict(self.profile, profile_hash='f' * 64)))
                    with change, self.assertRaises(ResourceWait) as stopped:
                        memory.require_dispatch_fresh(provider)
                    self.assertEqual(probe.call_count, 1)
                    self.assertFalse(stopped.exception.state['dispatch_refresh']['after']['admitted'])

    def test_context_or_instance_mutation_during_refresh_cannot_rebind_the_dispatch(self):
        for field in ('context', 'instance'):
            with self.subTest(field=field):
                provider, now, probe = self.refresh_fixture()
                fresh = copy.deepcopy(self.sample)
                fresh.update(wall_time=110., capture_started_at=109., capture_seconds=1., nonce='1' * 32)
                fresh['model'].update(hash_ms=0., digest_basis='verified_hash_reused_with_stat_and_instance')
                with memory.dispatch(provider, self.root, 'same-call', 'system', {}, {}) as initial:
                    before = copy.deepcopy(initial)
                    def changed(*args, **kwargs):
                        if field == 'context':
                            provider.runtime_context_tokens += 1024
                        else:
                            provider.remote_instance = dict(provider.remote_instance, pid=4321)
                        return fresh
                    probe.side_effect = changed
                    now[0] = 110.
                    with self.assertRaises(ResourceWait) as stopped:
                        memory.require_dispatch_fresh(provider)
                    self.assertEqual(stopped.exception.state['blocker'], 'runtime_uncertain')
                    self.assertEqual(provider.memory_plan, before)
                    self.assertEqual(probe.call_count, 2)

    def test_cancellation_during_refresh_keeps_allocation_and_diagnostics_without_retry(self):
        from lifecycle import Interrupted
        provider, now, probe = self.refresh_fixture()
        unchanged = copy.deepcopy((vars(provider.budget), provider.task_context, provider.consumed))
        with self.assertRaises(Interrupted):
            with memory.dispatch(provider, self.root, 'same-call', 'system', {}, {}) as initial:
                before = copy.deepcopy(initial)
                now[0] = 110.
                probe.side_effect = Interrupted('Synthetic cancellation')
                memory.require_dispatch_fresh(provider)
        self.assertEqual(provider.memory_plan, before)
        self.assertEqual((vars(provider.budget), provider.task_context, provider.consumed), unchanged)
        self.assertEqual(probe.call_count, 2)
        report = provider.dispatch_refresh
        self.assertFalse(report['after']['admitted'])
        self.assertEqual(report['after']['observation_failure']['failure_kind'], 'interrupted')
        self.assertIsNone(memory._dispatch_scope.get())

    def test_successful_warm_only_probe_uses_existing_digest_and_fresh_counters(self):
        memory._remember(self.profile, self.sample)
        binding = memory._cache_binding(self.sample)
        fresh = copy.deepcopy(self.sample)
        fresh['model'].update(hash_ms=0., digest_basis='verified_hash_reused_with_stat_and_instance')
        fresh['gpu'].update(free_bytes=2 * GIB, used_bytes=22 * GIB)
        def capture(_profile, script, **kwargs):
            if '--query-gpu' in script:
                return copy.deepcopy(fresh)
            return {'version': 1, 'nonce': fresh['nonce'], 'binding': binding}
        with patch('remote_control.ensure_tunnel'), patch('remote_control.run_ps', side_effect=capture) as remote, \
                patch.object(memory, '_server', return_value=fresh['server']), \
                patch.object(memory, 'verify_instance') as verify, \
                patch.object(memory.secrets, 'token_hex', return_value='0' * 32):
            observed = memory.probe(self.profile, warm_binding=binding)
        self.assertEqual(remote.call_count, 2)
        verify.assert_called_once()
        self.assertEqual(observed['model']['digest_basis'], 'verified_hash_reused_with_stat_and_instance')
        self.assertEqual(observed['model']['hash_ms'], 0.)
        self.assertEqual(observed['gpu']['free_bytes'], 2 * GIB)
        self.assertEqual(observed['verification_budget']['kind'], 'verified_identity_reuse')
        self.assertTrue(memory.evaluate(self.profile, observed, 4096)['admitted'])

    def test_warm_only_probe_never_hashes_without_checkpoint_or_after_preflight_change(self):
        binding = memory._cache_binding(self.sample)
        with patch('remote_control.ensure_tunnel') as tunnel, patch('remote_control.run_ps') as remote:
            with self.assertRaisesRegex(InvalidUpdate, 'verified identity checkpoint'):
                memory.probe(self.profile, warm_binding=binding)
            tunnel.assert_not_called()
            remote.assert_not_called()
        memory._remember(self.profile, self.sample)
        changed = dict(binding, mtime_ticks='639000000000000001')
        with patch('remote_control.ensure_tunnel'), \
                patch.object(memory.secrets, 'token_hex', return_value='0' * 32), \
                patch('remote_control.run_ps', return_value={'version': 1, 'nonce': '0' * 32,
                                                           'binding': changed}) as remote:
            with self.assertRaisesRegex(InvalidUpdate, 'changed before the warm observation'):
                memory.probe(self.profile, warm_binding=binding)
            self.assertEqual(remote.call_count, 1)  # No GPU/hash probe was sent.

    def test_warm_only_probe_timeout_has_no_second_observation_or_cold_fallback(self):
        memory._remember(self.profile, self.sample)
        binding = memory._cache_binding(self.sample)
        error = TimeoutError('fixture timeout')
        with patch('remote_control.ensure_tunnel') as tunnel, \
                patch.object(memory.secrets, 'token_hex', return_value='0' * 32), \
                patch('remote_control.run_ps', side_effect=[{'version': 1, 'nonce': '0' * 32,
                    'binding': binding}, error]) as remote:
            with self.assertRaises(TimeoutError):
                memory.probe(self.profile, warm_binding=binding)
            self.assertEqual(tunnel.call_count, 1)
            self.assertEqual(remote.call_count, 2)

    def test_ram_checkpoint_reuse_requires_exact_instance_and_file_identity(self):
        sample = copy.deepcopy(self.sample)
        sample['model']['hash_ms'] = 0.
        server = copy.deepcopy(sample['server'])
        def capture(_profile, _script, **kwargs):
            if '--query-gpu' in _script:
                return copy.deepcopy(sample)
            return {'version': 1, 'nonce': sample['nonce'], 'binding': memory._cache_binding(sample)}
        with patch('remote_control.ensure_tunnel'), patch('remote_control.run_ps', side_effect=capture), \
                patch.object(memory, '_server', return_value=server), \
                patch.object(memory, 'verify_instance'), \
                patch.object(memory.secrets, 'token_hex', return_value='0' * 32):
            first = memory.probe(self.profile)
            self.assertEqual(first['model']['digest_basis'], 'fresh_full_hash')
            sample['model']['digest_basis'] = 'verified_hash_reused_with_stat_and_instance'
            warm = memory.probe(self.profile)
            self.assertEqual(warm['model']['digest_basis'], 'verified_hash_reused_with_stat_and_instance')
            sample['model']['mtime_ticks'] = '639000000000000001'
            with self.assertRaisesRegex(InvalidUpdate, 'verified checkpoint|cold-size preflight'):
                memory.probe(self.profile)
            sample['model']['mtime_ticks'] = self.sample['model']['mtime_ticks']
            sample['boot_id'] = '2026-11-20T13:00:00Z'
            with self.assertRaisesRegex(InvalidUpdate, 'verified checkpoint|cold-size preflight'):
                memory.probe(self.profile)

    def test_verified_digest_reuse_never_reuses_gpu_headroom_or_survives_process_restart(self):
        sample = copy.deepcopy(self.sample)
        sample['model']['hash_ms'] = 0.
        server = copy.deepcopy(sample['server'])
        def capture(_profile, script, **kwargs):
            if '--query-gpu' in script:
                return copy.deepcopy(sample)
            return {'version': 1, 'nonce': sample['nonce'], 'binding': memory._cache_binding(sample)}
        with patch('remote_control.ensure_tunnel'), \
                patch('remote_control.run_ps', side_effect=capture), \
                patch.object(memory, '_server', return_value=server), \
                patch.object(memory, 'verify_instance'), \
                patch.object(memory.secrets, 'token_hex', return_value='0' * 32):
            first = memory.probe(self.profile)
            self.assertTrue(memory.evaluate(self.profile, first, 4096)['admitted'])
            sample['model']['digest_basis'] = 'verified_hash_reused_with_stat_and_instance'
            sample['gpu'].update(free_bytes=512 * 1024**2, used_bytes=24 * GIB - 512 * 1024**2)
            constrained = memory.probe(self.profile)
            self.assertFalse(memory.evaluate(self.profile, constrained, 4096)['admitted'])
            # An on-disk plan still contains the digest; a new Python process
            # has no RAM checkpoint and cannot promote that plan to identity.
            memory._save(self.root, memory.evaluate(self.profile, first, 4096))
            memory._verified.clear()
            with self.assertRaisesRegex(InvalidUpdate, 'verified checkpoint|cold-size preflight'):
                memory.probe(self.profile)

    def test_verified_identity_cache_has_a_fixed_profile_bound(self):
        configs = []
        for index in range(40):
            config = profile()
            config = normalize(dict(config, profile_hash=None, id='device' + str(index),
                                    name='sutra:remote/device' + str(index)))
            configs.append(config)
            memory._remember(config, observation(config))
        self.assertEqual(len(memory._verified), memory.MAX_VERIFIED_MODELS)
        self.assertIsNone(memory._cached(configs[0]))
        self.assertIsNotNone(memory._cached(configs[-1]))

    def restoration(self):
        p = normalize(dict(self.profile, profile_hash=None, cold_gpu_bytes=8 * GIB,
            cold_host_bytes=8 * GIB, cold_context_tokens=32768))
        sample = observation(p)
        sample['gpu'].update(process_pids=[], free_bytes=23 * GIB, used_bytes=GIB)
        sample['ram']['free_bytes'] = 20 * GIB
        return p, sample

    def test_unverified_physical_residency_reserves_complete_restoration_envelopes(self):
        p, sample = self.restoration()
        result = memory.evaluate(p, sample, 4096, now=101)
        self.assertTrue(result['admitted'])
        self.assertFalse(result['resident'])
        self.assertTrue(result['restoration_required'])
        self.assertFalse(result['resident_allocation_included_in_observed_free_memory'])
        self.assertEqual(result['cold_estimate_bytes'], 8 * GIB)
        self.assertEqual(result['cold_host_estimate_bytes'], 8 * GIB)
        self.assertEqual(result['estimated_increment_bytes'], 8 * GIB + 128 * 1024**2)
        self.assertEqual(result['estimated_host_increment_bytes'], 8 * GIB + 128 * 1024**2)
        self.assertFalse(result['peak_memory_verified'])

    def test_restoration_cannot_admit_from_only_gpu_or_only_host_capacity(self):
        p, sample = self.restoration()
        sample['gpu'].update(free_bytes=4 * GIB, used_bytes=20 * GIB)
        self.assertFalse(memory.evaluate(p, sample, 4096, now=101)['admitted'])
        p, sample = self.restoration()
        sample['ram']['free_bytes'] = 4 * GIB
        self.assertFalse(memory.evaluate(p, sample, 4096, now=101)['admitted'])

    def test_unknown_or_undercovered_restoration_never_takes_resident_credit(self):
        p, sample = self.restoration()
        for change in ({'cold_gpu_bytes': 1}, {'cold_context_tokens': 4096}):
            # The low-level evaluator also rejects an invalid host descriptor;
            # registry normalization independently enforces valid coverage.
            unsafe = dict(p, **change)
            self.assertFalse(memory.evaluate(unsafe, sample, 4096, now=101)['admitted'])
        missing = dict(p)
        del missing['cold_host_bytes']
        self.assertFalse(memory.evaluate(missing, sample, 4096, now=101)['admitted'])

    def test_gpu_context_presence_only_reserves_margin_and_sleeping_is_not_a_loaded_model(self):
        p, sample = self.restoration()
        sample['gpu']['process_pids'] = [sample['process']['pid']]
        result = memory.evaluate(p, sample, 4096, now=101)
        self.assertTrue(result['admitted'])
        self.assertTrue(result['resident'])
        self.assertFalse(result['restoration_required'])
        self.assertEqual(result['estimated_increment_bytes'], p['working_set_bytes'])
        sample['server']['is_sleeping'] = True
        self.assertFalse(memory.evaluate(p, sample, 4096, now=101)['admitted'])


if __name__ == '__main__':
    unittest.main()
