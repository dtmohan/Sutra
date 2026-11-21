"""Measured source-delivery completion does not certify model understanding."""
import copy
import unittest

from karma_adapter import KarmaAdapter
from kernel_boundary import KernelBoundary


class ChildSourceFrontierTests(unittest.TestCase):
    def observation(self):
        return {'profile': 'child-source-frontier-v1', 'version': 1,
                'source_files': 2, 'nonblank_lines': 6, 'delivered_nonblank_lines': 5,
                'checkpoints_verified': True, 'sources_current': True}

    def test_missing_line_keeps_acquisition_open(self):
        result = KarmaAdapter().child_source_frontier(self.observation())
        self.assertEqual(result['decision'], 'ALLOW')
        self.assertEqual(result['suggested_recovery'], 'continue')

    def test_exact_authenticated_delivery_bridges_to_partial_parent_scope(self):
        value = self.observation(); value['delivered_nonblank_lines'] = 6
        for k in (.1, .15, .35):
            result = KarmaAdapter(KernelBoundary(k=k)).child_source_frontier(value)
            self.assertEqual(result['decision'], 'BRIDGE')
            self.assertEqual(result['suggested_recovery'], 'partial_handoff')
            for key in ('grants_permissions', 'semantic_completion_verified', 'task_closed'):
                self.assertFalse(result[key])
        self.assertEqual(value['nonblank_lines'], 6)

    def test_stale_or_unverified_delivery_stops_acquisition(self):
        for field in ('checkpoints_verified', 'sources_current'):
            value = self.observation(); value[field] = False
            result = KarmaAdapter().child_source_frontier(value)
            self.assertEqual(result['decision'], 'FIN')
            self.assertEqual(result['suggested_recovery'], 'inspect_evidence')

    def test_invalid_counts_and_fabricated_scope_are_rejected(self):
        cases = [('version', True), ('version', 2), ('profile', 'unknown'),
                 ('source_files', 0), ('nonblank_lines', 0), ('nonblank_lines', -1),
                 ('delivered_nonblank_lines', 7), ('delivered_nonblank_lines', True),
                 ('delivered_nonblank_lines', 5.0), ('sources_current', 1)]
        for key, value in cases:
            invalid = self.observation(); invalid[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                KarmaAdapter().child_source_frontier(invalid)
        invalid = copy.deepcopy(self.observation()); invalid['permission'] = 'write'
        with self.assertRaises(ValueError):
            KarmaAdapter().child_source_frontier(invalid)


if __name__ == '__main__':
    unittest.main()
