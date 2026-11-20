"""Writer packing retains a separate reviewer share without changing spending."""
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import chat_runtime as chat
import context_window
from paging import Budget
from model_identity import unknown
import review_context
from review_role import constraint_rows
from test_context_window_recovery import CountService
import test_chat as chat_fixture
from test_model_packet import WireModel


class ParentPackingBudgetTests(unittest.TestCase):
    def setUp(self):
        self.counter = CountService()
        self.window = dict(self.counter.descriptor(), context_tokens=32768)
        self.memory = {'pinned_user_statements': [{'quote': 'Read only. No network.'}],
                       'recent_turns': [{'user': 'Explain the architecture. Read only. No network.'}]}
        self.contract = {'task_id': 'task', 'goal': 'Explain the architecture. Read only. No network.',
                         'constraints': {'allowed_paths': ['.'], 'edits_allowed': False,
                                         'network_allowed': False}}
        self.task_budget = {'limits': {'tokens': 250000}, 'used': {'tokens': 117099}}
        self.data = {'sources': {'entry.py': 'def main():\n    return run()\n'},
                     'graph': {'nodes': [], 'calls': [], 'diagnostics': [], 'module_dependencies': []}}

    def fit(self, *, report_version=2, exact=True):
        metadata = {'model_info': {'fixture.context_length': 32768}, '_sutra_runtime_cap': 32768}
        budget = Budget(32768, 2048, 479)
        if exact:
            metadata['_sutra_context_window'] = self.window
            budget = Budget(32768, 2048, 479, context_profile=2,
                            reservation_limit=132901, context_window=self.window)
        provider = SimpleNamespace(model='fixture', budget=budget, context_metadata=metadata)
        p = {'contract': copy.deepcopy(self.contract), 'request': self.contract['goal'],
             'constraint_echo': constraint_rows(self.contract), 'parent_context_version': 3}
        if report_version is not None:
            p['report_schema_version'] = report_version
        calls, before = [], copy.deepcopy(self.task_budget)
        allocate = review_context.context_budget

        def record(*args, **kwargs):
            value = allocate(*args, **kwargs)
            calls.append((kwargs['slots'], value))
            return value

        context_window._counts.clear()
        with patch('review_budget.usage', return_value=self.task_budget), \
                patch('orchestration.task', return_value={'id': 'owner'}), \
                patch('orchestration.gates', return_value=[self.task_budget]), \
                patch.object(review_context, 'context_budget', side_effect=record), \
                patch.object(chat, 'conversation_pages', return_value=copy.deepcopy(self.memory)), \
                patch.object(context_window, '_rpc', self.counter.rpc):
            schema, attempts = chat.fit_packet(None, 'task', p, self.data, provider, real=True)
        self.assertEqual(self.task_budget, before)
        self.assertEqual(p['contract'], self.contract)
        self.assertEqual(p['constraint_echo'], constraint_rows(self.contract))
        self.assertEqual(p['memory'], self.memory)
        self.assertEqual(p['excerpts']['entry.py'], self.data['sources']['entry.py'])
        self.assertFalse(p['contract']['constraints']['edits_allowed'])
        self.assertFalse(p['contract']['constraints']['network_allowed'])
        self.assertTrue(schema['properties']['report'])
        return provider, p, attempts, calls

    def test_new_report_exact_profile_packs_writer_half_and_keeps_three_bounded_attempts(self):
        provider, p, attempts, calls = self.fit()
        self.assertEqual([slots for slots, _ in calls], [2])
        self.assertEqual(provider.budget.reservation_limit, 132901 // 2)
        self.assertEqual(attempts, 3)
        allocation = calls[0][1]
        self.assertEqual(allocation['reviewer_slots_reserved'], 1)
        self.assertEqual(allocation['repair_slots_reserved'], 0)
        self.assertEqual(allocation['available_tokens'], 132901)
        self.assertLessEqual(provider.budget.input_limit * 2, allocation['available_tokens'])
        self.assertEqual(p['memory_budget']['reservation_limit'], 66450)
        self.assertTrue(self.counter.calls)
        self.assertTrue(all(path != '/v1/chat/completions' for _, _, path, _ in self.counter.calls))

    def test_historical_report_or_three_field_budget_retains_four_share_recipe(self):
        for report_version, exact in ((None, True), (1, True), (2, False)):
            with self.subTest(report_version=report_version, exact=exact):
                provider, _, attempts, calls = self.fit(report_version=report_version, exact=exact)
                self.assertEqual([slots for slots, _ in calls], [4])
                self.assertEqual(calls[0][1]['reviewer_slots_reserved'], 1)
                self.assertEqual(calls[0][1]['repair_slots_reserved'], 2)
                self.assertEqual(attempts, 3)
                if exact:
                    self.assertEqual(provider.budget.reservation_limit, 132901 // 4)
                else:
                    self.assertEqual(set(vars(provider.budget)),
                                     {'context_limit', 'response_reserve', 'template_reserve'})


class ParentRetryPackingTests(unittest.TestCase):
    setUp = chat_fixture.ChatTests.setUp
    tearDown = chat_fixture.ChatTests.tearDown

    def test_failed_writer_settles_then_refits_before_next_dispatch(self):
        service = CountService()
        profile = dict(service.descriptor(), context_tokens=32768)
        model = WireModel('strata:profile/test-high')
        model.context_metadata = {'model_info': {'fixture.context_length': 32768},
                                  '_sutra_runtime_cap': 32768, '_sutra_context_window': profile}
        model.budget = Budget(32768, 2048, 479, context_profile=2,
                              reservation_limit=125000, context_window=profile)
        model.model_identity = dict(unknown(model.model), server_model=profile['model'],
                                    server_started=profile['started'],
                                    server_context_tokens=profile['context_tokens'])
        allocations = []

        def reply(proposal, packet):
            allocations.append(model.budget.reservation_limit)
            choices = chat.citation_options(packet)
            proposal['citations'] = [choices.index(item) + 1 for item in proposal['citations']]
            if len(allocations) == 1:
                proposal['answer'] = ''

        model.after = reply
        fit = chat.fit_packet
        context_window._counts.clear()
        with patch('api_runtime.provider_for', return_value=model), \
                patch('monitoring.require_resources', return_value={'admitted': True}), \
                patch('review_followup.expand', return_value=({}, None)), \
                patch.object(context_window, '_rpc', service.rpc), \
                patch.object(chat, 'fit_packet', wraps=fit) as fitted:
            out = chat.answer(self.s, self.meta, self.ws, self.storage,
                {'prompt': 'Explain the architecture.', 'models': dict(chat_fixture.MODELS, low=model.model),
                 'tier': 'low', '_parent_step': True})
        self.assertTrue(out['result']['admission']['admitted'], out)
        self.assertTrue(out['parent_checkpoint'])
        self.assertEqual(fitted.call_count, 2)
        self.assertEqual(len(allocations), 2)
        self.assertEqual(allocations[1], allocations[0] - 240)
        self.assertEqual(out['result']['token_usage']['total_calls'], 2)
        self.assertTrue(self.s.verify_invocations(self.sid))
        self.assertTrue(all(not packet['contract']['constraints']['edits_allowed'] for packet in model.payloads))


if __name__ == '__main__':
    unittest.main()
