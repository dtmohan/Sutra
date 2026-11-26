"""Shared review controls use verified capabilities and preserve stage receipts."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from context_manager import ContextBudgetExceeded, InvalidUpdate, canonical, sha
from invocation_gateway import InvocationGateway, InvocationLedger
from paging import Budget
import remote_context
import review_stage


def stage(role='section_writer', mode='sectioned', *, version=4):
    return {'section_version': version, 'section_role': role, 'section_mode': mode}


class ReviewStageTests(unittest.TestCase):
    def test_v5_reviewer_reserves_room_for_uncontrolled_reasoning(self):
        role = 'section_reviewer'
        cases = (([False, True], 256, 512), ([True], 1024, 1024),
                 (['low', 'medium', 'high'], 1024, 1024), ([], 1024, 1024))
        for advertised, sectioned, full in cases:
            provider = SimpleNamespace(model='example', budget=Budget(16384, 2048),
                                       context_metadata={'thinking': {'values': advertised}})
            for mode, target in (('sectioned', sectioned), ('full', full)):
                with self.subTest(advertised=advertised, mode=mode):
                    self.assertEqual(review_stage.reviewer_output_limit(
                        provider, version=review_stage.VERSION, mode=mode), target)
                    original = provider.budget
                    provider.budget = original.with_limits(response_reserve=target)
                    payload = stage(role, mode, version=review_stage.VERSION)
                    review_stage.bind(provider, role, payload)
                    review_stage.require(provider.budget, role, provider.model, payload)
                    provider.budget = original

    def test_v5_uncontrolled_reasoning_pauses_before_binding_an_underfunded_reviewer(self):
        role = 'section_reviewer'
        budget = Budget(16384, 512)
        provider = SimpleNamespace(model='example', budget=budget,
                                   context_metadata={'thinking': {'values': [True]}})
        with self.assertRaisesRegex(ContextBudgetExceeded, 'needs 1,024 output tokens'):
            review_stage.reviewer_output_limit(provider, version=review_stage.VERSION, mode='sectioned')
        # Historical under-reserved v4 receipts still replay with their exact wire.
        self.assertEqual(review_stage.reviewer_output_limit(provider, version=4, mode='sectioned'), 256)
        provider.budget = budget.with_limits(response_reserve=256)
        legacy = stage(role)
        review_stage.bind(provider, role, legacy)
        review_stage.require(provider.budget, role, provider.model, legacy)
        forged = dict(legacy, section_version=review_stage.VERSION)
        with self.assertRaisesRegex(InvalidUpdate, 'output allocation'):
            review_stage.require(provider.budget, role, provider.model, forged)

    def test_advertised_controls_and_conservative_default(self):
        for advertised, expected in (([False, True], False), ([True], False), (['low', 'medium', 'high'], 'low'),
                                     (['high'], None), ([], None)):
            for role in review_stage.ROLES:
                with self.subTest(advertised=advertised, role=role):
                    budget = Budget(16384, 2048 if role == 'section_writer' else 512)
                    provider = SimpleNamespace(model='example', budget=budget, thinking=True,
                        context_metadata={'thinking': {'values': advertised}})
                    payload = stage(role)
                    review_stage.bind(provider, role, payload)
                    review_stage.require(budget, role, provider.model, payload)
                    self.assertEqual(provider.thinking, expected)
                    self.assertIsNone(payload['review_stage']['native_reasoning_tokens'])
                    self.assertEqual(payload['reasoning']['requested'], expected)
                    self.assertFalse(review_stage.full_draft_supported(provider))

    def test_native_cap_depends_on_attested_window_and_reply_allowance(self):
        window = dict(remote_context.PARENT_REASONING_IDENTITY, version=1,
                      provider=remote_context.PROVIDER, context_tokens=16384, port=8111,
                      reference='sutra:remote/test', model='example',
                      profile_hash='a' * 64, defaults_sha256='b' * 64)
        for version, role, mode, allowance, cap in (
                (4, 'section_writer', 'full', 4096, 1024),
                (4, 'section_writer', 'sectioned', 1536, 768),
                (4, 'section_reviewer', 'full', 512, 192),
                (5, 'section_reviewer', 'sectioned', 256, 128),
                (5, 'section_reviewer', 'full', 512, 192)):
            with self.subTest(version=version, role=role, mode=mode, allowance=allowance):
                budget = Budget(16384, allowance, context_profile=2,
                                reservation_limit=65536, context_window=window)
                provider = SimpleNamespace(model=window['reference'], budget=budget, context_metadata={})
                payload = stage(role, mode, version=version)
                review_stage.bind(provider, role, payload)
                review_stage.require(budget, role, provider.model, payload)
                self.assertEqual(payload['review_stage']['native_reasoning_tokens'], cap)
                if role == 'section_reviewer':
                    self.assertEqual(review_stage.reviewer_output_limit(provider, version=version, mode=mode),
                                     allowance)
                self.assertEqual(review_stage.full_draft_supported(provider), role == 'section_writer' and mode == 'full')
                with self.assertRaises(InvalidUpdate):
                    review_stage.require(budget, role, 'sutra:remote/other', payload)

    def test_verified_window_and_advertised_off_allow_full_on_another_provider(self):
        window = {'version': 1, 'provider': 'strata', 'port': 8111, 'model': 'example',
                  'started': 1, 'context_tokens': 16384, 'template_sha256': 'a' * 64}
        budget = Budget(16384, 4096, context_profile=2, reservation_limit=65536,
                        context_window=window)
        metadata = {'thinking': {'values': [False, True], 'default': True}}
        provider = SimpleNamespace(model='example', budget=budget, context_metadata=metadata)
        self.assertTrue(review_stage.full_draft_supported(provider))
        payload = stage(mode='full')
        review_stage.bind(provider, 'section_writer', payload)
        self.assertIs(provider.thinking, False)
        self.assertIsNone(payload['review_stage']['native_reasoning_tokens'])
        provider.budget = Budget(16384, 4096)
        self.assertFalse(review_stage.full_draft_supported(provider))
        provider.budget = SimpleNamespace(context_profile=2, response_reserve=4096, context_window=None)
        self.assertFalse(review_stage.full_draft_supported(provider))

    def test_tampered_stage_rejected_before_provider_or_ledger_work(self):
        provider = SimpleNamespace(model='example', budget=Budget(16384, 512), context_metadata={})
        role = 'section_reviewer'
        payload = stage(role)
        review_stage.bind(provider, role, payload)
        payload['review_stage']['native_reasoning_tokens'] = 192
        gateway = InvocationGateway('unused.sqlite', 'unused', provider)
        with self.assertRaisesRegex(InvalidUpdate, 'admitted provider control'):
            gateway.invoke(role, 'call', 'system', payload, {}, 'delivery')
        with patch('invocation_gateway.expected_request', return_value=('system', {})), \
                self.assertRaisesRegex(InvalidUpdate, 'admitted provider control'):
            InvocationLedger.register(None, 'run', 'call', role, payload, 'system', {},
                                      'delivery', provider.model, 1024, budget=vars(provider.budget))

    def test_receipt_replays_with_bound_budget_and_checks_delivery_hash(self):
        provider = SimpleNamespace(model='example', budget=Budget(16384, 512), context_metadata={})
        role = 'section_reviewer'
        payload = stage(role)
        review_stage.bind(provider, role, payload)
        raw = canonical(payload)
        envelope = {'role': role, 'model': provider.model, 'budget_profile': vars(provider.budget),
                    'payload_hash': sha(raw)}
        receipt = {'payload_hash': sha(raw)}
        review_stage.verify_committed(envelope, receipt, raw, payload)
        changed = dict(payload, section_version=3)
        with self.assertRaisesRegex(InvalidUpdate, 'changed after dispatch'):
            review_stage.verify_committed(envelope, receipt, canonical(changed), changed)
        changed = dict(envelope, budget_profile=vars(Budget(16384, 1024)))
        with self.assertRaisesRegex(InvalidUpdate, 'output allocation'):
            review_stage.verify_committed(changed, receipt, raw, payload)
