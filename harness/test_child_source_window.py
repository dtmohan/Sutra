"""Exact physical windows and financial source-page floors use distinct units."""
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from monitoring import ResourceWait
from paging import Budget
import context_window
import review_context
import subagent_runtime as child
import allocation_recovery
import workspace_allocation
from test_context_window_recovery import CountService
import test_saved_child_source_recovery as fixture


class ChildSourceWindowTests(unittest.TestCase):
    setUp = fixture.SavedChildSourceRecoveryTests.setUp
    prepared = fixture.SavedChildSourceRecoveryTests.prepared

    def probe(self, service):
        profile = service.descriptor()
        budget = Budget(profile['context_tokens'], 2048, 256, context_profile=2,
                        reservation_limit=self.available, context_window=profile)
        provider = SimpleNamespace(model='strata:profile/source-floor', budget=budget,
            context_metadata={'model_info': {'fixture.context_length': profile['context_tokens']},
                              '_sutra_runtime_cap': profile['context_tokens'],
                              '_sutra_context_window': profile})
        allocation = {'available_tokens': self.available,
                      'effective_context_tokens': profile['context_tokens'],
                      'model_context_tokens': profile['context_tokens'],
                      'review_context_allocation': profile['context_tokens'],
                      'attempt_slots_reserved': 1, 'repair_slots_reserved': 0,
                      'context_profile': 2, 'reservation_limit_units': self.available}
        self.provider = provider
        self.original_budget = copy.deepcopy(vars(provider.budget))
        with patch.object(child, '_context_budget', return_value=allocation), \
                patch.object(context_window, '_rpc', service.rpc):
            return child._fit_packet(None, 'source-window-fixture', copy.deepcopy(self.packet),
                                     self.data, provider, 1)

    def test_financial_floor_above_physical_tokens_keeps_exact_window_proof(self):
        service = CountService()
        with self.assertRaises(review_context.ReviewContextExceeded) as stopped:
            self.probe(service)
        details = stopped.exception.details
        proof = details['physical_fit']
        required = details['task_allocation_need']['required_context']
        self.assertEqual(details['failure_basis'], 'reservation_budget')
        self.assertGreater(required, self.available)
        self.assertGreater(required, proof['context_tokens'])
        self.assertEqual(details['task_allocation_need']['context_profile'], 2)
        self.assertEqual(proof['context_tokens'], service.descriptor()['context_tokens'])
        self.assertEqual(proof['prompt_tokens'], 500)
        self.assertEqual(proof['output_reserve'], 2048)
        self.assertEqual(proof['capacity']['decision'], 'ALLOW')
        self.assertTrue(details['minimum_packet']['source_bearing'])
        self.assertEqual(vars(self.provider.budget), self.original_budget)
        self.assertEqual(len(self.packet['child_checkpoint']), 16)
        self.assertTrue(any(row[2] == '/v1/messages/count_tokens' for row in service.calls))
        self.assertFalse(any(row[2] in ('/v1/messages', '/api/chat') for row in service.calls))

    def test_physical_overflow_cannot_request_financial_amendment(self):
        service = CountService(); service.prompt_tokens = service.descriptor()['context_tokens']
        with self.assertRaises(review_context.ReviewContextExceeded) as stopped:
            self.probe(service)
        self.assertEqual(stopped.exception.details['failure_basis'], 'physical_context')
        self.assertNotIn('task_allocation_need', stopped.exception.details)
        self.assertEqual(stopped.exception.details['physical_context']['prompt_tokens'], service.prompt_tokens)
        self.assertEqual(vars(self.provider.budget), self.original_budget)

    def test_financial_grant_requires_the_exact_valid_physical_observation(self):
        with self.assertRaises(review_context.ReviewContextExceeded) as stopped:
            self.probe(CountService())
        details = stopped.exception.details
        need = details['task_allocation_need']
        self.assertGreater(need['required_context'], need['model_context'])
        self.assertTrue(allocation_recovery.exact_context_fit(details, need))
        stop = {'status': 'context_budget', 'allocation': details, 'allocation_need': need}
        self.assertEqual(workspace_allocation._need(stop), ('tokens', need['minimum']))
        changes = [
            lambda value: value.pop('physical_fit'),
            lambda value: value['physical_fit']['capacity'].update(decision='BRIDGE'),
            lambda value: value['physical_fit'].update(prompt_tokens=0),
            lambda value: value['physical_fit'].update(prompt_tokens=8192),
            lambda value: value['physical_fit'].update(output_reserve=1),
        ]
        for mutate in changes:
            bad = copy.deepcopy(details)
            mutate(bad)
            with self.subTest(observation=bad.get('physical_fit')):
                self.assertFalse(allocation_recovery.exact_context_fit(bad, need))
                self.assertIsNone(workspace_allocation._need(dict(stop, allocation=bad)))

    def test_unavailable_exact_counter_remains_a_measurement_wait(self):
        service = CountService(); service.fail_counts = True
        with self.assertRaises(ResourceWait):
            self.probe(service)
        self.assertEqual(vars(self.provider.budget), self.original_budget)


if __name__ == '__main__':
    unittest.main()
