"""Accepted child gaps may guide discovery without extending its source scope."""
import copy
import unittest

from context_manager import canonical, sha
from review_followup import key, planning_candidates


class FollowupHintsTests(unittest.TestCase):
    def setUp(self):
        self.paths = [f'src/a{index:02d}.py' for index in range(50)] + [
            'harness/orchestration_flow.py', 'harness/context_manager.py']
        self.p = {'request': 'Explain the architecture and tools.',
                  'contract': {'goal': 'Explain the architecture', 'contract_hash': 'contract'},
                  'workspace': '/project', 'review_flow': 'flow', 'snapshot_owner': 'owner',
                  'snapshot_receipts': ['receipt'], 'parent_context_version': 2,
                  'child_results': [{'remaining_unverified': [
                      'Trace orchestration_flow.advance and context_manager.py before connecting the request path.',
                      'Read /private/outside.py and ../private.py.']}]}

    def test_requested_catalog_modules_reach_planner_before_original_top40(self):
        before = copy.deepcopy(self.p)
        candidates, hints = planning_candidates(self.p, self.paths, {'nodes': [], 'calls': []})
        self.assertEqual(set(hints['paths']), {'harness/orchestration_flow.py', 'harness/context_manager.py'})
        self.assertIn('orchestration_flow.advance', hints['references'])
        self.assertEqual(set(candidates[:2]), set(hints['paths']))
        self.assertEqual(len(candidates), 40)
        self.assertTrue(set(candidates) <= set(self.paths))
        self.assertNotIn('/private/outside.py', candidates)
        self.assertEqual(self.p, before)

    def test_legacy_candidate_order_and_key_remain_unchanged(self):
        p = dict(self.p)
        del p['parent_context_version']
        from review_context import ordered_sources, query
        expected = ordered_sources(self.paths, {}, query(p))[:40]
        self.assertEqual(planning_candidates(p, self.paths, {}), (expected, []))
        old_key = sha(canonical([p['contract']['contract_hash'], p['workspace'], p['request'],
                                p['review_flow'], None, p['snapshot_owner'], p['snapshot_receipts']]))
        self.assertEqual(key(p), old_key)
        self.assertNotEqual(key(self.p), old_key)

    def test_hints_cannot_add_a_path_outside_catalog(self):
        p = copy.deepcopy(self.p)
        p['child_results'] = [{'remaining_unverified': ['Read secret.read and /private/context_manager.py.']}]
        paths = ['src/public.py']
        self.assertEqual(planning_candidates(p, paths, {}), (paths, {'paths': [], 'references': []}))


if __name__ == '__main__':
    unittest.main()
