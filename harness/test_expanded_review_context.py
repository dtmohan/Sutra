"""The optional physical window does not replenish a saved review."""
import copy
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import chat_runtime
from context_manager import ContextBudgetExceeded
import expanded_review_context as profile
from invocation_gateway import InvocationLedger
import orchestration as machine
import review_budget
import review_context
from test_parent_source_funding import ParentSourceFundingTests as ReviewFixture


class ExpandedReviewContextTests(unittest.TestCase):
    setUp = ReviewFixture.setUp
    tearDown = ReviewFixture.tearDown
    step = ReviewFixture.step
    until = ReviewFixture.until
    provider = ReviewFixture.provider
    discover = ReviewFixture.discover
    prepared = ReviewFixture.prepared
    pay_to = ReviewFixture.pay_to
    _pay_to = ReviewFixture._pay_to
    rows = ReviewFixture.rows
    source_rows = ReviewFixture.source_rows

    def make_model(self, *, opted=True, ready=True):
        name = 'sutra:remote/large'
        window = {'version': 1, 'provider': 'sutra_remote', 'reference': name,
                  'port': 18092, 'model': 'qwen', 'context_tokens': profile.EXPANDED,
                  'profile_hash': 'a' * 64, 'model_sha256': 'b' * 64, 'executable_sha256': 'c' * 64,
                  'template_sha256': 'd' * 64, 'defaults_sha256': 'e' * 64}
        model = SimpleNamespace(model=name, profile={
            'context_tokens': profile.EXPANDED, 'profile_hash': window['profile_hash'],
            'cold_context_tokens': profile.EXPANDED if ready else profile.BASELINE,
        }, context_metadata={'_sutra_provider': 'sutra_remote', '_sutra_context_window': window,
                             'model_info': {'remote_cuda.context_length': profile.EXPANDED}})
        with patch.dict(os.environ, {profile.ENVIRONMENT: json.dumps([name] if opted else [])}):
            profile.prepare(model)
        return model

    def test_exact_fit_retry_after_reload_preserves_original_task(self):
        saved, packet, _ = self.prepared()
        sid = self.meta['active']; owner = saved['execution_id']
        before = review_budget.usage(self.s, sid)
        self.assertGreater(before['used']['tokens'], 0)
        node = machine.node(self.s, owner)
        original_packet = copy.deepcopy(packet)
        receipts, sources = self.rows(), self.source_rows()
        limits_seen = []

        def count(budget, *_, **__):
            limits_seen.append(budget.context_limit)
            if budget.context_limit == profile.BASELINE:
                failure = ContextBudgetExceeded('Exact retained input requires more positions')
                failure.physical_context = {'prompt_tokens': 33000, 'output_reserve': 4096,
                                            'context_slack_tokens': 8}
                raise failure

        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        with machine.scope(owner), patch('context_window.check', side_effect=count):
            model = self.make_model()
            chat_runtime.preflight(self.s, sid, packet, model, True)
        self.assertEqual(limits_seen, [profile.BASELINE, profile.EXPANDED])
        self.assertEqual(model.budget.context_limit, profile.EXPANDED)
        self.assertEqual(packet, original_packet)
        self.assertEqual(machine.node(self.s, owner), node)
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertEqual(self.rows(), receipts)
        self.assertEqual(self.source_rows(), sources)
        self.assertTrue(self.s.audit_valid())

    def test_no_upgrade_without_user_opt_in_gpu_coverage_or_remaining_allocation(self):
        saved, _, _ = self.prepared()
        sid = self.meta['active']
        with machine.scope(saved['execution_id']):
            for kwargs in ({'opted': False}, {'ready': False}, {}):
                model = self.make_model(**kwargs)
                allocation = review_context.context_budget(model, self.s, sid)
                self.assertEqual(model.budget.context_limit, profile.BASELINE)
                charge = 40000 if kwargs else allocation['available_tokens']
                self.assertFalse(profile.try_expand(self.s, sid, model, allocation, 46000, charge,
                                                    reason='exact_prompt_fit'))
                self.assertEqual(model.review_window_limit, profile.BASELINE)

    def test_mlx_requires_a_matching_full_measurement_and_fresh_memory_fit(self):
        model = SimpleNamespace(port=11434, model='qwen3.8:27b',
                                context_metadata={'details': {'runner': 'mlx'},
                                    'model_info': {'mlx.context_length': 262144},
                                    '_sutra_runtime_cap': 64000})
        with patch.dict(os.environ, {profile.ENVIRONMENT: json.dumps([model.model])}):
            profile.prepare(model)
        self.assertEqual(profile.ceiling(model), 32768)
        model.memory_profile = {'points': [{'covered_tokens': 65536}]}
        self.assertEqual(profile.ceiling(model), 64000)
        model.context_metadata['_sutra_runtime_cap'] = 65536
        self.assertEqual(profile.ceiling(model), 65536)
