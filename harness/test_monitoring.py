import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import monitoring


def sample(wall_time, *, thermal=0, pressure=1):
    return {'wall_time': wall_time, 'thermal_state': thermal, 'memory_pressure_raw': pressure,
            'free_bytes': 8 * 1024**3, 'inactive_bytes': 0}


class HeadlessTelemetryTests(unittest.TestCase):
    def setUp(self):
        storage = tempfile.TemporaryDirectory()
        self.addCleanup(storage.cleanup)
        self.root = Path(storage.name)
        self.file = self.root / 'telemetry.json'
        clock = patch('monitoring.time.time', return_value=1001)
        self.addCleanup(clock.stop)
        clock.start()
        collector = patch('monitoring._collect_hardware', return_value=(sample(1001), 'native-telemetry'))
        self.collect = collector.start()
        self.addCleanup(collector.stop)

    def write(self, value):
        self.file.write_text(json.dumps(value))

    def test_headless_admission_samples_locally_without_a_stream(self):
        self.assertFalse(monitoring.resource_state(self.root)['available'])
        self.collect.assert_not_called()
        state = monitoring.require_resources(self.root)
        self.assertTrue(state['admitted'])
        self.assertEqual(state['sample_source'], 'host_on_demand')
        self.assertEqual(state['collector'], 'native-telemetry')
        self.assertFalse(self.file.exists())
        self.collect.assert_called_once_with(2)

    def test_two_headless_checks_refresh_an_expired_stream_sample(self):
        original = sample(1000)
        self.write(original)
        self.assertEqual(monitoring.require_resources(self.root)['sample_source'], 'telemetry_file')
        self.collect.assert_not_called()
        with patch('monitoring.time.time', return_value=1020):
            self.collect.return_value = (sample(1020), 'native-telemetry')
            state = monitoring.require_resources(self.root)
        self.assertEqual(state['sample'], sample(1020))
        self.assertEqual(state['sample_source'], 'host_on_demand')
        self.assertEqual(json.loads(self.file.read_text()), original)

    def test_fresh_adverse_sample_preserves_the_existing_stop(self):
        original = sample(1000, pressure=2)
        self.write(original)
        with self.assertRaises(monitoring.ResourceWait) as stopped:
            monitoring.require_resources(self.root, newer_than=1000)
        self.assertEqual(stopped.exception.state['sample'], original)
        self.assertEqual(stopped.exception.state['decision'], 'BRIDGE')
        self.collect.assert_not_called()

    def test_future_malformed_and_invalid_expired_samples_cannot_fall_back(self):
        for original in (sample(1002), sample(1000, thermal=7), sample(990, pressure=0), {}):
            with self.subTest(sample=original):
                self.write(original)
                with self.assertRaises(monitoring.ResourceWait):
                    monitoring.require_resources(self.root)
        self.file.write_text('{')
        with self.assertRaises(monitoring.ResourceWait):
            monitoring.require_resources(self.root)
        self.collect.assert_not_called()

    def test_old_healthy_sample_can_be_replaced_when_recovery_needs_a_newer_one(self):
        self.write(sample(1000))
        self.collect.return_value = (sample(1001, pressure=2), 'native-telemetry')
        with self.assertRaises(monitoring.ResourceWait) as stopped:
            monitoring.require_resources(self.root, newer_than=1000)
        self.assertEqual(stopped.exception.state['sample_source'], 'host_on_demand')
        self.assertEqual(stopped.exception.state['decision'], 'BRIDGE')

    def test_on_demand_failure_and_expired_result_still_stop_admission(self):
        self.collect.side_effect = subprocess.TimeoutExpired('telemetry', 2)
        self.write(sample(990))
        with self.assertRaisesRegex(monitoring.ResourceWait, 'Local telemetry refresh failed'):
            monitoring.require_resources(self.root)
        self.collect.side_effect = None
        self.collect.return_value = (sample(990), 'native-telemetry')
        with self.assertRaisesRegex(monitoring.ResourceWait, 'stale'):
            monitoring.require_resources(self.root)


class HostCollectorTests(unittest.TestCase):
    def test_uses_bundled_native_one_shot_sampler_on_arm64_macos(self):
        with patch('monitoring.sys.platform', 'darwin'), patch('monitoring.platform.machine', return_value='arm64'):
            with patch('monitoring.subprocess.run') as run:
                run.return_value.stdout = json.dumps(sample(1001))
                value, collector = monitoring._collect_hardware()
        command = run.call_args.args[0]
        self.assertEqual(command, [str(Path(__file__).resolve().parent.parent/'bin'/'telemetry'), '--once'])
        self.assertEqual(run.call_args.kwargs['timeout'], 2)
        self.assertEqual(value, sample(1001))
        self.assertEqual(collector, 'native-telemetry')

    def test_uses_existing_portable_sampler_on_other_hosts(self):
        executable = Path(__file__).resolve().parent / 'fixture-node'
        with patch('monitoring.sys.platform', 'linux'), patch.dict(os.environ, KARMA_NODE=str(executable)):
            with patch('monitoring.subprocess.run') as run:
                run.return_value.stdout = json.dumps({'profile': 'portable-memory-v1'})
                _, collector = monitoring._collect_hardware()
        self.assertEqual(run.call_args.args[0], [
            str(executable), str(Path(__file__).resolve().with_name('telemetry_portable.js')), '--once'])
        self.assertEqual(collector, 'portable-telemetry')

    def test_one_shot_sampler_respects_a_shorter_remaining_timeout(self):
        with patch('monitoring.sys.platform', 'darwin'), patch('monitoring.platform.machine', return_value='arm64'):
            with patch('monitoring.subprocess.run') as run:
                run.return_value.stdout = '{}'
                monitoring._collect_hardware(.1)
        self.assertEqual(run.call_args.kwargs['timeout'], .1)


class TestProcessMonitoringTests(unittest.TestCase):
    def test_test_process_rechecks_resources_at_most_once_per_second(self):
        from host_actions import run_tests_snapshot
        tick = 0
        observed = []

        def select(_timeout):
            nonlocal tick
            tick += 1
            return []

        def check(_root, *, refresh, max_wait):
            observed.append((tick, refresh, max_wait))
            return {'admitted': tick < 20}

        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            with patch('host_actions.time.monotonic', side_effect=lambda: tick/10), \
                    patch('host_actions.selectors.DefaultSelector') as selector, \
                    patch('host_actions.subprocess.Popen') as process, \
                    patch('host_actions.resource_state', side_effect=check), \
                    patch('host_actions.os.killpg', create=True) as kill:
                selector.return_value.select.side_effect = select
                process.return_value.poll.return_value = 0
                result = run_tests_snapshot(root, {}, 'unused.py', 2500, root)
        self.assertEqual(observed, [(10, True, 1.5), (20, True, .5)])
        self.assertEqual(result['status'], 'resource_wait')
        kill.assert_called_once()
