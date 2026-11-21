"""Negative remote admission frames retain their kernel verdict and evidence."""
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate, canonical
from karma_adapter import KarmaAdapter
from monitoring import ResourceWait
import provider_resources as resources
import remote_memory as memory
from test_remote_memory import observation, profile


class RemoteResourceTests(unittest.TestCase):
    def setUp(self):
        self.profile = profile()
        self.provider = SimpleNamespace(model=self.profile['name'], profile=self.profile,
                                        runtime_context_tokens=4096)

    def require(self, plan):
        with patch('remote_memory.admission', return_value=plan), \
                patch('monitoring.require_resources', side_effect=AssertionError('Wrong inference host')):
            return resources.require_resources(Path('/unused-fixture'), provider=self.provider)

    def test_direct_bridge_and_fin_preserve_exact_reason_category_and_stage_without_mutation(self):
        blocked = memory._blocked('Fresh SSH measurement unavailable; historical ALLOW is not authority')
        blocked['observation_failure'] = {'stage': 'identity_probe', 'failure_kind': 'timeout',
                                          'timeout_seconds': 20, 'elapsed_seconds': 20.01}
        terminal = dict(blocked, **KarmaAdapter().frame(memory.PROFILE, 2., 0.))
        self.assertEqual(terminal['decision'], 'FIN')
        for state in (blocked, terminal):
            before = canonical(state)
            with self.subTest(decision=state['decision']), self.assertRaises(ResourceWait) as stopped:
                self.require(state)
            self.assertEqual(stopped.exception.state['decision'], state['decision'])
            self.assertEqual(stopped.exception.state['reason'], state['reason'])
            self.assertEqual(stopped.exception.state['observation_failure'], state['observation_failure'])
            self.assertEqual(stopped.exception.category, 'measurement_unavailable')
            self.assertEqual(stopped.exception.state['runtime']['stage'], 'remote_admission')
            self.assertEqual(stopped.exception.state['runtime']['model'], self.provider.model)
            self.assertIs(stopped.exception.state['runtime']['inference_started'], False)
            self.assertEqual(canonical(state), before)

    def test_nested_capacity_stop_and_busy_frame_are_not_misclassified_as_missing_shape(self):
        sample = observation(self.profile)
        sample['gpu'].update(free_bytes=512 * 1024**2, used_bytes=24 * 1024**3 - 512 * 1024**2)
        capacity = memory.evaluate(self.profile, sample, 4096, now=101)
        with self.assertRaises(ResourceWait) as stopped:
            self.require(capacity)
        self.assertEqual(stopped.exception.category, 'model_admission')
        self.assertEqual(stopped.exception.state['decision'], capacity['decision'])
        busy = memory._blocked('The remote inference slot is busy', kind='runtime_busy')
        with self.assertRaises(ResourceWait) as stopped:
            self.require(busy)
        self.assertEqual(stopped.exception.category, 'runtime_busy')

    def test_malformed_or_bare_allow_frames_are_never_promoted_to_admission(self):
        plan = memory.evaluate(self.profile, observation(self.profile), 4096, now=101)
        self.assertTrue(self.require(plan)['admitted'])
        broken = [None, {}, {'hardware': []}, plan['hardware'],
                  dict(memory._blocked('unknown'), profile='wrong-profile'),
                  dict(memory._blocked('unknown'), admitted=0),
                  dict(memory._blocked('unknown'), decision='ALLOW')]
        for value in broken:
            with self.subTest(value=value), self.assertRaises(InvalidUpdate) as rejected:
                self.require(value)
            self.assertNotIsInstance(rejected.exception, ResourceWait)

    def test_committed_stale_observation_cannot_borrow_healthy_history_or_mac_ram(self):
        sample = observation(self.profile)
        plan = memory.evaluate(self.profile, sample, 4096, now=101)
        self.provider.memory_plan = plan
        before = copy.deepcopy(plan)
        with patch.object(memory.time, 'time', return_value=110), \
                patch('remote_memory.probe', side_effect=AssertionError('No hidden retry')), \
                patch('monitoring.require_resources', side_effect=AssertionError('Wrong inference host')):
            with self.assertRaises(ResourceWait) as stopped:
                resources.require_resources(Path('/unused-fixture'), provider=self.provider, committed=True)
        self.assertEqual(stopped.exception.category, 'measurement_unavailable')
        self.assertEqual(stopped.exception.state['runtime']['stage'], 'remote_dispatch')
        self.assertEqual(self.provider.memory_plan, before)

    def test_remote_recovery_preserves_failed_observation_without_unloading_or_dispatching(self):
        blocked = memory._blocked('Fresh measurement unavailable')
        audit = []
        with patch('provider_resources.require_resources', side_effect=ResourceWait(blocked)), \
                patch('runtime_memory.recover', side_effect=AssertionError('Wrong runtime')):
            result = resources.recover(Path('/unused-fixture'), lambda k, v: audit.append((k, v)),
                                       model=self.provider.model)
        self.assertFalse(result['admitted'])
        self.assertIs(result['automatic_inference'], False)
        self.assertEqual(result['max_release_actions'], 0)
        self.assertEqual(result['after'], blocked)
        self.assertEqual(audit[0][0], 'REMOTE_MEMORY_RECOVERY_OBSERVED')


if __name__ == '__main__':
    unittest.main()
