import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import provider_startup


GIB = 1024**3


def sample(wall_time=1001, *, free=28 * GIB, pressure=1, thermal=0):
    return {'wall_time': wall_time, 'thermal_state': thermal, 'memory_pressure_raw': pressure,
            'free_bytes': free, 'inactive_bytes': 0}


class ProviderStartupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.storage = Path(directory.name)
        clock = patch('provider_startup.time.time', return_value=1001)
        clock.start()
        self.addCleanup(clock.stop)
        collect = patch('monitoring._collect_hardware', return_value=(sample(), 'native-telemetry'))
        self.collect = collect.start()
        self.addCleanup(collect.stop)

    def test_normal_startup_uses_measured_hardware_and_memory_kernel(self):
        result = provider_startup.admission(20 * GIB, self.storage)
        self.assertTrue(result['admitted'])
        self.assertEqual(result['decision'], 'ALLOW')
        self.assertEqual(result['memory_allocation']['decision'], 'ALLOW')
        self.assertEqual(result['hardware']['sample_source'], 'host_on_demand')
        self.assertEqual(result['system_reserve_bytes'], 4 * GIB)
        self.collect.assert_called_once_with(2)
        self.assertEqual(list(self.storage.iterdir()), [])

    def test_pressure_blocks_even_when_cold_memory_fits(self):
        self.collect.return_value = sample(pressure=2), 'native-telemetry'
        result = provider_startup.admission(20 * GIB, self.storage)
        self.assertFalse(result['admitted'])
        self.assertEqual(result['decision'], 'BRIDGE')
        self.assertEqual(result['memory_allocation']['decision'], 'ALLOW')

    def test_insufficient_capacity_uses_existing_memory_adapter(self):
        self.collect.return_value = sample(free=21 * GIB), 'native-telemetry'
        result = provider_startup.admission(20 * GIB, self.storage)
        self.assertFalse(result['admitted'])
        self.assertTrue(result['hardware']['admitted'])
        self.assertEqual(result['decision'], result['memory_allocation']['decision'])
        self.assertNotEqual(result['decision'], 'ALLOW')
        self.assertEqual(result['available_bytes'], 21 * GIB)

    def test_admission_accepts_portable_available_memory(self):
        portable = {'profile': 'portable-memory-v1', 'wall_time': 1001,
                    'available_bytes': 28 * GIB, 'physical_memory_bytes': 64 * GIB}
        self.collect.return_value = portable, 'portable-telemetry'
        result = provider_startup.admission(20 * GIB, self.storage)
        self.assertTrue(result['admitted'])
        self.assertEqual(result['available_bytes'], 28 * GIB)

    def test_stale_saved_measurement_gets_a_new_observation(self):
        original = sample(990, free=50 * GIB)
        cached = self.storage / 'telemetry.json'
        cached.write_text(json.dumps(original))
        self.collect.return_value = sample(free=21 * GIB), 'native-telemetry'
        result = provider_startup.admission(20 * GIB, self.storage)
        self.assertFalse(result['admitted'])
        self.assertEqual(result['hardware']['sample']['wall_time'], 1001)
        self.assertEqual(result['available_bytes'], 21 * GIB)
        self.assertEqual(json.loads(cached.read_text()), original)

    def test_current_healthy_cache_cannot_replace_a_startup_observation(self):
        (self.storage / 'telemetry.json').write_text(json.dumps(sample(free=50 * GIB)))
        self.collect.return_value = sample(pressure=2), 'native-telemetry'
        result = provider_startup.admission(20 * GIB, self.storage)
        self.assertFalse(result['admitted'])
        self.assertEqual(result['hardware']['sample_source'], 'host_on_demand')

    def test_stale_on_demand_measurement_is_never_retimestamped(self):
        self.collect.return_value = sample(990), 'native-telemetry'
        result = provider_startup.admission(20 * GIB, self.storage)
        self.assertFalse(result['admitted'])
        self.assertFalse(result['hardware']['available'])
        self.assertIsNone(result['decision'])
        self.assertNotIn('memory_allocation', result)
        self.assertEqual(self.collect.return_value[0]['wall_time'], 990)

    def test_refresh_failure_cannot_admit_using_an_earlier_healthy_cache(self):
        (self.storage / 'telemetry.json').write_text(json.dumps(sample(1000)))
        self.collect.side_effect = subprocess.TimeoutExpired('telemetry', 2)
        result = provider_startup.admission(20 * GIB, self.storage)
        self.assertFalse(result['admitted'])
        self.assertFalse(result['hardware']['available'])
        self.assertIsNone(result['decision'])
        self.assertIn('No fresh hardware observation', result['reason'])

    def test_unavailable_telemetry_fails_closed(self):
        self.collect.side_effect = subprocess.TimeoutExpired('telemetry', 2)
        result = provider_startup.admission(20 * GIB, self.storage)
        self.assertFalse(result['admitted'])
        self.assertFalse(result['hardware']['available'])
        self.assertIn('Local telemetry refresh failed', result['reason'])
        self.assertIsNone(result['decision'])

    def test_invalid_estimates_and_storage_are_rejected_before_sampling(self):
        for value in (True, False, 0, -1, 1.5, float('nan'), float('inf'), '12', 2**53):
            with self.subTest(value=value), self.assertRaises(ValueError):
                provider_startup.admission(value, self.storage)
        with self.assertRaises(ValueError):
            provider_startup.admission(20 * GIB, Path('relative'))
        self.collect.assert_not_called()

    def test_cli_emits_json_without_reading_input_or_mutating_task_files(self):
        sentinel = self.storage / 'state.sqlite'
        sentinel.write_bytes(b'saved task and consumed budget')
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = provider_startup.main(['--cold-memory-bytes', str(20 * GIB),
                                          '--storage', str(self.storage)])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stream.getvalue())['admitted'])
        self.assertEqual(sentinel.read_bytes(), b'saved task and consumed budget')
        self.assertEqual(list(self.storage.iterdir()), [sentinel])

    def test_cli_uses_temporary_measurement_storage_when_none_is_supplied(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = provider_startup.main(['--cold-memory-bytes', str(20 * GIB)])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stream.getvalue())['admitted'])

    def test_invalid_cli_estimate_emits_a_refusal(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = provider_startup.main(['--cold-memory-bytes', '0'])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(stream.getvalue())['admitted'])
        self.collect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
