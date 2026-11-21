"""Runner intent follows the canonical kernel without granting task authority."""
import unittest

from karma_adapter import KarmaAdapter
from kernel_boundary import KernelBoundary


class TaskContinuationAdapterTests(unittest.TestCase):
    def observation(self, disposition='continue', event='step'):
        return {'profile': 'task-continuation-v1', 'version': 1, 'disposition': disposition,
                'next_event': event, 'binding_current': True, 'checkpoint_current': True,
                'readiness_verified': False}

    def test_runnable_current_stage_allows_without_granting_permissions_or_budget(self):
        result = KarmaAdapter().task_continuation(self.observation())
        self.assertEqual(result['decision'], 'ALLOW')
        self.assertFalse(result['grants_permissions'])
        self.assertFalse(result['replenishes_budget'])

    def test_waits_and_external_decisions_bridge(self):
        for disposition, event in (('wait', 'continuation_probe'), ('approval', None), ('evidence', None)):
            self.assertEqual(KarmaAdapter().task_continuation(self.observation(disposition, event))['decision'], 'BRIDGE')

    def test_resume_needs_verified_fresh_readiness(self):
        value = self.observation('continue', 'resume')
        self.assertEqual(KarmaAdapter().task_continuation(value)['decision'], 'BRIDGE')
        value['readiness_verified'] = True
        self.assertEqual(KarmaAdapter().task_continuation(value)['decision'], 'ALLOW')

    def test_stop_finished_and_stale_binding_finish_runner_transition(self):
        for k in (.1, .15, .35):
            adapter = KarmaAdapter(KernelBoundary(k=k))
            for disposition in ('stopped', 'finished'):
                self.assertEqual(adapter.task_continuation(self.observation(disposition, None))['decision'], 'FIN')
            for key in ('binding_current', 'checkpoint_current'):
                value = self.observation(); value[key] = False
                self.assertEqual(adapter.task_continuation(value)['decision'], 'FIN')

    def test_ambiguous_or_forged_observations_are_rejected(self):
        for key, value in (('version', True), ('version', 2), ('disposition', 'other'),
                           ('next_event', 'execute'), ('readiness_verified', 1), ('binding_current', None)):
            invalid = self.observation(); invalid[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                KarmaAdapter().task_continuation(invalid)
        with self.assertRaises(ValueError):
            KarmaAdapter().task_continuation(self.observation('approval', 'step'))


if __name__ == '__main__':
    unittest.main()
