"""Size-aware remote identity windows with fresh final hardware evidence.

All SSH/device operations and clocks are fixtures; no remote host is used.
"""
import copy
import re
import subprocess
import unittest
from unittest.mock import patch

import remote_memory as memory
import test_remote_memory as baseline
from context_manager import InvalidUpdate, canonical
from lifecycle import Interrupted


class Clock:
    def __init__(self):
        self.seconds = 1000.

    def now(self):
        return self.seconds

    def advance(self, seconds):
        self.seconds += seconds


class RemoteVerificationTests(unittest.TestCase):
    def setUp(self):
        self.config = baseline.profile()
        self.sample = baseline.observation(self.config)
        self.sample['model']['bytes'] = 19403684480
        self.clock = Clock()
        self.ps_calls = []
        self.extra_counter_delay = 0
        self.full_hash_seconds = 49.929097
        self.changed_after_preflight = False
        self.bad_hash = False
        # This test process never accesses a running worker's checkpoint cache.
        with memory._verified_lock:
            memory._verified.clear()

    def ps(self, profile, script, timeout_seconds, *, reuse_tunnel):
        self.assertTrue(reuse_tunnel)
        nonce = re.search(r"nonce='([a-f0-9]{32})'", script)[1]
        self.ps_calls.append({'timeout_seconds': timeout_seconds, 'script': script, 'nonce': nonce})
        if 'cold-size preflight' not in script:
            self.clock.advance(.4)
            return {'version': 1, 'nonce': nonce, 'binding': memory._cache_binding(self.sample)}
        response = copy.deepcopy(self.sample)
        response['nonce'] = nonce
        checkpoint = memory._cached(self.config)
        reuse = checkpoint is not None and checkpoint['binding'] == memory._cache_binding(response)
        duration = .001 if reuse else self.full_hash_seconds
        response['model']['hash_ms'] = duration * 1000
        response['model']['digest_basis'] = ('verified_hash_reused_with_stat_and_instance'
                                             if reuse else 'fresh_full_hash')
        if self.changed_after_preflight:
            response['model']['mtime_ticks'] = '639000000000000001'
        if self.bad_hash:
            response['model']['sha256'] = '9' * 64
        self.clock.advance(duration + 1.2 + self.extra_counter_delay)
        return response

    def server(self, profile, deadline=None):
        self.assertIsNotNone(deadline)
        self.clock.advance(.2)
        return copy.deepcopy(self.sample['server'])

    def verify(self, profile, instance, *, timeout_seconds=5):
        self.assertGreater(timeout_seconds, 0)
        self.clock.advance(.2)
        return instance

    def probe(self):
        with patch.object(memory.time, 'time', side_effect=self.clock.now), \
                patch.object(memory.time, 'monotonic', side_effect=self.clock.now), \
                patch.object(memory.secrets, 'token_hex', side_effect=lambda size: f'{len(self.ps_calls):032x}'), \
                patch('remote_control.ensure_tunnel'), \
                patch('remote_control.run_ps', side_effect=self.ps), \
                patch.object(memory, '_server', side_effect=self.server), \
                patch.object(memory, 'verify_instance', side_effect=self.verify):
            return memory.probe(self.config)

    def evaluate(self, sample):
        return memory.evaluate(self.config, sample, 4096, now=self.clock.now())

    def test_real_file_sizes_derive_distinct_bounded_deadlines(self):
        q38 = memory.verification_budget(19403684480, 'fresh_full_hash')
        q36 = memory.verification_budget(20542732576, 'fresh_full_hash')
        self.assertEqual((q38['rpc_seconds'], q38['capture_seconds']), (150, 170))
        self.assertEqual((q36['rpc_seconds'], q36['capture_seconds']), (159, 179))
        self.assertFalse(q38['ceiling_applied'])
        maximum = memory.verification_budget(2**53 - 1, 'fresh_full_hash')
        self.assertEqual((maximum['rpc_seconds'], maximum['capture_seconds']), (160, 180))
        self.assertTrue(maximum['ceiling_applied'])
        for value in (True, 0, -1, 1.0, '19403684480', float('inf'), 2**53):
            with self.subTest(value=value), self.assertRaises(ValueError):
                memory.verification_budget(value, 'fresh_full_hash')

    def test_fifty_second_hash_retains_fresh_counter_admission_and_full_identity(self):
        result = self.probe()
        self.assertGreater(result['capture_seconds'], 49)
        self.assertLess(result['counter_age_upper_seconds'], 2)
        self.assertEqual(result['model']['sha256'], self.config['model_sha256'])
        self.assertTrue(self.evaluate(result)['admitted'])
        self.assertEqual(self.ps_calls[0]['timeout_seconds'], 5)
        self.assertEqual(self.ps_calls[1]['timeout_seconds'], 150)
        self.assertIsNotNone(memory._cached(self.config))
        self.assertIn("Get-FileHash -LiteralPath $modelPath", self.ps_calls[1]['script'])
        self.assertLess(self.ps_calls[1]['script'].index('Get-FileHash -LiteralPath $modelPath'),
                        self.ps_calls[1]['script'].index('$gpuRaw='))

    def test_warm_checkpoint_has_twenty_second_window_and_remeasures_capacity(self):
        first = self.probe()
        cached_before = memory._cached(self.config)
        self.sample['gpu'].update(free_bytes=512 * 1024**2,
                                 used_bytes=24 * 1024**3 - 512 * 1024**2)
        warm = self.probe()
        self.assertEqual(warm['verification_budget']['capture_seconds'], 20)
        self.assertEqual(warm['verification_budget']['rpc_seconds'], 20)
        self.assertLessEqual(self.ps_calls[-1]['timeout_seconds'], 20)
        self.assertEqual(warm['model']['digest_basis'], 'verified_hash_reused_with_stat_and_instance')
        self.assertEqual(memory._cached(self.config), cached_before)
        self.assertFalse(self.evaluate(warm)['admitted'])
        self.assertNotEqual(self.evaluate(warm)['decision'], 'ALLOW')
        self.assertTrue(self.evaluate(first)['available'])

    def test_process_restart_forces_another_full_hash_not_cache_injection(self):
        self.probe()
        self.sample['process']['started_at'] = '2026-11-20T12:30:00Z'
        restarted = self.probe()
        self.assertEqual(restarted['model']['digest_basis'], 'fresh_full_hash')
        self.assertEqual(self.ps_calls[-1]['timeout_seconds'], 150)
        self.assertTrue(self.evaluate(restarted)['admitted'])

    def test_changed_file_between_preflight_and_full_capture_is_rejected(self):
        self.changed_after_preflight = True
        with self.assertRaisesRegex(InvalidUpdate, 'changed after cold-size preflight'):
            self.probe()
        self.assertIsNone(memory._cached(self.config))

    def test_wrong_hash_does_not_establish_a_verified_checkpoint(self):
        self.bad_hash = True
        result = self.probe()
        self.assertFalse(self.evaluate(result)['admitted'])
        self.assertIsNone(memory._cached(self.config))

    def test_slow_gpu_ram_or_transport_tail_remains_stale_after_cold_hash(self):
        self.extra_counter_delay = 8.1
        result = self.probe()
        self.assertGreater(result['counter_age_upper_seconds'], 8)
        self.assertFalse(self.evaluate(result)['admitted'])
        self.assertEqual(self.evaluate(result)['decision'], 'BRIDGE')
        self.assertIsNone(memory._cached(self.config))

    def test_freshness_expires_eight_seconds_after_final_counters_even_with_long_window(self):
        result = self.probe()
        self.clock.advance(8)
        self.assertFalse(self.evaluate(result)['admitted'])
        self.assertFalse(self.evaluate(result)['available'])

    def test_cold_capture_and_hash_bounds_cannot_be_expanded_by_sample_fields(self):
        result = self.probe()
        for update in ({'capture_seconds': 171},
                       {'verification_budget': dict(result['verification_budget'], capture_seconds=900)},
                       {'counter_age_upper_seconds': float('nan')},
                       {'counter_age_upper_seconds': -1}):
            with self.subTest(update=update):
                changed = copy.deepcopy(result)
                changed.update(update)
                self.assertFalse(self.evaluate(changed)['admitted'])
        changed = copy.deepcopy(result)
        changed['model']['hash_ms'] = 151000
        self.assertFalse(self.evaluate(changed)['admitted'])

    def test_warm_capture_does_not_inherit_the_cold_deadline(self):
        self.probe()
        warm = self.probe()
        warm['capture_seconds'] = 21
        self.assertFalse(self.evaluate(warm)['admitted'])

    def test_wrong_preflight_nonce_path_executable_or_size_never_yields_hash_authority(self):
        valid = {'version': 1, 'nonce': '0' * 32, 'binding': memory._cache_binding(self.sample)}
        for key, value in (('nonce', 'wrong'), ('version', True)):
            changed = copy.deepcopy(valid)
            changed[key] = value
            with self.subTest(key=key), self.assertRaises(InvalidUpdate):
                memory._preflight_binding(self.config, changed, '0' * 32)
        for key, value in (('bytes', True), ('bytes', -1), ('model_path', r'C:\other.gguf'),
                           ('executable_sha256', 'a' * 64), ('mtime_ticks', 123)):
            changed = copy.deepcopy(valid)
            changed['binding'][key] = value
            with self.subTest(key=key), self.assertRaises((InvalidUpdate, ValueError)):
                memory._preflight_binding(self.config, changed, '0' * 32)

    def test_no_implicit_extension_after_deadline_or_cancellation(self):
        with patch.object(memory.time, 'monotonic', return_value=20):
            with self.assertRaisesRegex(InvalidUpdate, 'bounded verification window'):
                memory._remaining(5, 20)
        with patch('remote_control.ensure_tunnel'), \
                patch('remote_control.run_ps', side_effect=Interrupted('cancelled')):
            with self.assertRaises(Interrupted):
                memory.probe(self.config)
        self.assertIsNone(memory._cached(self.config))

    def test_same_kernel_capacity_constraints_and_state_for_short_and_long_identity_windows(self):
        cold = self.probe()
        current = self.evaluate(cold)
        legacy = copy.deepcopy(cold)
        legacy['capture_seconds'] = 2
        legacy['capture_started_at'] = legacy['wall_time'] - .5
        legacy['model']['hash_ms'] = 500
        legacy.pop('verification_budget')
        legacy.pop('counter_age_upper_seconds')
        prior = self.evaluate(legacy)
        for key in ('decision', 'estimated_increment_bytes', 'estimated_host_increment_bytes',
                    'system_reserve_bytes', 'context_tokens', 'gpu_allocation', 'host_allocation'):
            self.assertEqual(canonical(current[key]), canonical(prior[key]))
        before = canonical(self.sample)
        self.evaluate(cold)
        self.assertEqual(canonical(self.sample), before)

    def timeout_probe(self, timeout_seconds):
        self.clock.advance(timeout_seconds)
        try:
            raise subprocess.TimeoutExpired('never-retain-this-command', timeout_seconds,
                stderr=b'Bearer never-retain-this-secret\nSUTRA_REMOTE_STAGE:gpu_inventory\n')
        except subprocess.TimeoutExpired:
            raise InvalidUpdate('Remote host inspection timed out or its pinned SSH connection is unavailable') from None

    def test_warm_probe_timeout_recovers_once_from_a_fresh_observation(self):
        self.probe()
        original = self.ps
        attempts = []

        def capture(profile, script, timeout_seconds, **kwargs):
            if '--query-gpu' in script:
                attempts.append(re.search(r"nonce='([a-f0-9]{32})'", script)[1])
                if len(attempts) == 1:
                    self.timeout_probe(timeout_seconds)
            return original(profile, script, timeout_seconds, **kwargs)

        self.ps = capture
        warm = self.probe()
        self.assertEqual(len(set(attempts)), 2)
        self.assertEqual(warm['nonce'], attempts[-1])
        self.assertLess(warm['capture_seconds'], 5)
        self.assertTrue(self.evaluate(warm)['admitted'])
        recovery = self.evaluate(warm)['hardware']['sample']['recovered_identity_timeout']
        self.assertEqual(recovery['stage'], 'identity_probe')
        self.assertEqual(recovery['failure_kind'], 'timeout')
        self.assertNotIn('never-retain', canonical(recovery))
        self.clock.advance(8)
        self.assertFalse(self.evaluate(warm)['admitted'])

    def test_warm_retry_rejects_a_different_instance_without_rehashing(self):
        self.probe()
        original = self.ps

        def capture(profile, script, timeout_seconds, **kwargs):
            if '--query-gpu' in script:
                self.sample['process']['pid'] += 1
                self.timeout_probe(timeout_seconds)
            return original(profile, script, timeout_seconds, **kwargs)

        self.ps = capture
        with self.assertRaisesRegex(InvalidUpdate, 'identity changed before the warm inspection retry'):
            self.probe()
        self.assertEqual(len(self.ps_calls), 4)

    def test_probe_timeout_lasts_one_bounded_retry_and_retains_sanitized_causes(self):
        self.probe()
        original = self.ps
        attempts = []

        def capture(profile, script, timeout_seconds, **kwargs):
            if '--query-gpu' not in script:
                return original(profile, script, timeout_seconds, **kwargs)
            attempts.append(timeout_seconds)
            self.timeout_probe(timeout_seconds)

        self.ps = capture
        with self.assertRaises(InvalidUpdate) as failed:
            self.probe()
        self.assertEqual(len(attempts), 2)
        for seconds in attempts:
            self.assertAlmostEqual(seconds, 19.6)
        with patch.object(memory, 'probe', side_effect=failed.exception):
            state = memory._observe(self.config, 4096)
        self.assertEqual(state['decision'], 'BRIDGE')
        self.assertEqual(state['blocker'], 'measurement_unavailable')
        diagnostics = state['observation_failure']
        self.assertEqual(diagnostics['stage'], 'identity_probe')
        self.assertEqual(diagnostics['last_remote_step'], 'gpu_inventory')
        self.assertEqual(diagnostics['failure_kind'], 'timeout')
        self.assertEqual(diagnostics['cause_type'], 'TimeoutExpired')
        self.assertAlmostEqual(diagnostics['timeout_seconds'], 19.6)
        self.assertEqual(diagnostics['elapsed_seconds'], 19.6)
        self.assertEqual(diagnostics['prior_timeout']['failure_kind'], 'timeout')
        self.assertNotIn('never-retain', canonical(state))

    def test_cold_timeout_or_insufficient_bridge_time_is_not_retried(self):
        original = self.ps
        attempts = []

        def capture(profile, script, timeout_seconds, **kwargs):
            if '--query-gpu' not in script:
                return original(profile, script, timeout_seconds, **kwargs)
            attempts.append(timeout_seconds)
            self.timeout_probe(timeout_seconds)

        self.ps = capture
        with self.assertRaisesRegex(InvalidUpdate, 'timed out'):
            self.probe()
        self.assertEqual(attempts, [150])
        self.ps = original
        self.probe()
        self.ps = capture
        with patch('lifecycle.remaining', return_value=39), self.assertRaisesRegex(InvalidUpdate, 'timed out'):
            self.probe()
        self.assertEqual(len(attempts), 2)
        self.assertAlmostEqual(attempts[-1], 19.6)

    def test_preflight_failure_is_distinct_and_unknown_stderr_never_becomes_a_step(self):
        def unavailable(*args, **kwargs):
            try:
                raise OSError('never-retain-this-path')
            except OSError:
                raise InvalidUpdate('Remote host inspection is unavailable') from None
        self.ps = unavailable
        with self.assertRaises(InvalidUpdate) as failed:
            self.probe()
        with patch.object(memory, 'probe', side_effect=failed.exception):
            state = memory._observe(self.config, 4096)
        self.assertEqual(state['observation_failure']['stage'], 'identity_preflight')
        self.assertEqual(state['observation_failure']['failure_kind'], 'unavailable')
        self.assertNotIn('last_remote_step', state['observation_failure'])
        self.assertNotIn('never-retain', canonical(state))


if __name__ == '__main__':
    unittest.main()
