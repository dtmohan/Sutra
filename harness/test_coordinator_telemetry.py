"""Select fresh UI/daemon observations without turning sampler failure into health."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import monitoring
from test_monitoring import sample


class CoordinatorTelemetryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'coordinator').mkdir(mode=0o700)

    def write(self, value, *, daemon=False):
        file = self.root / ('coordinator/telemetry.json' if daemon else 'telemetry.json')
        file.write_text(json.dumps(value))
        if daemon: file.chmod(0o600)

    def test_daemon_keeps_monitor_fresh_when_the_ui_sample_expires(self):
        self.write(sample(980))
        self.write(sample(1000), daemon=True)
        state = monitoring.resource_state(self.root, now=1001)
        self.assertTrue(state['admitted'])
        self.assertEqual(state['sample_source'], 'coordinator_telemetry_file')
        self.assertEqual(state['sample']['wall_time'], 1000)

    def test_newest_pressure_wins_over_older_healthy_observations_from_either_writer(self):
        for daemon_is_newest in (False, True):
            with self.subTest(daemon_is_newest=daemon_is_newest):
                self.write(sample(1000, pressure=2), daemon=daemon_is_newest)
                self.write(sample(999), daemon=not daemon_is_newest)
                state = monitoring.resource_state(self.root, now=1001)
                self.assertFalse(state['admitted'])
                self.assertEqual(state['decision'], 'BRIDGE')
                self.assertEqual(state['sample']['wall_time'], 1000)

    def test_simultaneous_pressure_cannot_be_hidden_by_a_healthy_sample(self):
        self.write(sample(1000))
        self.write(sample(1000, pressure=2), daemon=True)
        self.assertEqual(monitoring.resource_state(self.root, now=1001)['decision'], 'BRIDGE')

    def test_invalid_ui_observation_can_use_an_independently_valid_daemon_sample(self):
        (self.root / 'telemetry.json').write_text('{')
        self.write(sample(1000), daemon=True)
        self.assertEqual(monitoring.resource_state(self.root, now=1001)['sample_source'], 'coordinator_telemetry_file')

    def test_both_expired_sources_still_require_a_real_on_demand_measurement(self):
        self.write(sample(980))
        self.write(sample(981), daemon=True)
        with patch('monitoring.time.time', return_value=1001), \
                patch('monitoring._collect_hardware', return_value=(sample(1001), 'native-telemetry')) as collect:
            state = monitoring.require_resources(self.root)
        self.assertEqual(state['sample_source'], 'host_on_demand')
        collect.assert_called_once_with(2)

    def test_symlinked_or_insecure_daemon_files_are_not_measurements(self):
        outside = self.root / 'outside.json'
        outside.write_text(json.dumps(sample(1000)))
        file = self.root / 'coordinator/telemetry.json'
        file.symlink_to(outside)
        self.assertFalse(monitoring.resource_state(self.root, now=1001)['available'])
        file.unlink()
        self.write(sample(1000), daemon=True)
        if os.name != 'nt':
            file.chmod(0o644)
            self.assertFalse(monitoring.resource_state(self.root, now=1001)['available'])


if __name__ == '__main__':
    unittest.main()
