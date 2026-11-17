import unittest

from karma_adapter import KarmaAdapter
from kernel_boundary import KernelBoundary


class EvidenceAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.adapter = KarmaAdapter(KernelBoundary(k=0.23, sigma_clause=0.8))

    def decide(self, target='resolved', evidence='available', recover=False, exhausted=False):
        return self.adapter.evidence_admission(
            target_status=target,
            evidence_status=evidence,
            recovery_possible=recover,
            budget_exhausted=exhausted,
        )

    def test_available_evidence_for_resolved_target_is_admitted(self):
        frame = self.decide()
        self.assertEqual(frame['decision'], 'ALLOW')
        self.assertEqual(frame['adapter'], 'karma')
        self.assertEqual(frame['profile'], 'evidence-admission-v1')
        self.assertEqual(frame['profile_version'], 1)
        self.assertEqual(frame['target_status'], 'resolved')
        self.assertEqual(frame['evidence_status'], 'available')

    def test_missing_or_stale_evidence_bridges_with_a_recovery_path(self):
        for target, evidence in (
            ('missing', 'missing'),
            ('resolved', 'missing'),
            ('resolved', 'stale'),
        ):
            with self.subTest(target=target, evidence=evidence):
                frame = self.decide(target, evidence, recover=True)
                self.assertEqual(frame['decision'], 'BRIDGE')
                self.assertEqual(frame['omega'], 2 * self.adapter.boundary.k)
                self.assertTrue(frame['recovery_possible'])
                self.assertEqual(self.decide(target, evidence)['decision'], 'FIN')

    def test_invalid_evidence_or_target_cannot_be_admitted_through_recovery(self):
        for target, evidence in (('invalid', 'available'), ('resolved', 'invalid')):
            with self.subTest(target=target, evidence=evidence):
                self.assertEqual(self.decide(target, evidence, recover=True)['decision'], 'FIN')

    def test_cumulative_budget_exhaustion_takes_precedence(self):
        for target, evidence in (
            ('resolved', 'available'),
            ('resolved', 'stale'),
            ('invalid', 'invalid'),
        ):
            with self.subTest(target=target, evidence=evidence):
                frame = self.decide(target, evidence, recover=True, exhausted=True)
                self.assertEqual(frame['decision'], 'COMPOUND_FIN')
                self.assertEqual(frame['sigma'], self.adapter.boundary.sigma_clause)

    def test_requires_recognized_host_signals(self):
        for kwargs in (
            {'target': 'guessed'},
            {'evidence': 'model_confident'},
            {'recover': 1},
            {'exhausted': 0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.decide(**kwargs)
