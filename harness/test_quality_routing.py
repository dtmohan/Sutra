"""A new bound review intent changes synthesis routing, not admission rights."""
import unittest

from context_manager import InvalidUpdate
from model_routing import choose


class QualityRoutingTests(unittest.TestCase):
    models = {'low': 'small', 'mid': 'medium', 'high': 'large'}
    prompt = 'Walk me through the tools logic in detail and draw a few block diagrams.'

    def signals(self, sources=8, selected=2, **extra):
        return dict(source_files=sources, selected_files=selected, unresolved_calls=3, **extra)

    def test_historical_route_without_intent_is_unchanged(self):
        signals = self.signals()
        self.assertEqual(choose(self.prompt, self.models, 'auto', signals), {
            'policy': 'local-tier-routing-v2', 'tier': 'low', 'model': 'small',
            'mode': 'auto', 'models': dict(self.models), 'signals': signals,
            'reason': 'focused request', 'semantic_complexity_verified': False,
            'automatic_escalation': True,
        })

    def test_fresh_source_report_routes_synthesis_to_high_without_keyword(self):
        signals = self.signals(review_intent='architecture')
        result = choose(self.prompt, self.models, 'auto', signals)
        self.assertEqual(result['policy'], 'local-tier-routing-v3')
        self.assertEqual(result['tier'], 'high')
        self.assertEqual(result['model'], 'large')
        self.assertEqual(result['reason'], 'structured architecture synthesis after fresh source')
        self.assertEqual(result['signals'], signals)
        self.assertFalse(result['semantic_complexity_verified'])

    def test_no_fresh_source_still_starts_low(self):
        for sources, selected in ((0, 0), (8, 0), (0, 2)):
            with self.subTest(sources=sources, selected=selected):
                result = choose(self.prompt, self.models, 'auto',
                                self.signals(sources, selected, review_intent='architecture'))
                self.assertEqual(result['tier'], 'low')
                self.assertEqual(result['reason'], 'small-model first')

    def test_fixed_tiers_keep_the_users_override(self):
        for mode in ('low', 'mid', 'high'):
            with self.subTest(mode=mode):
                result = choose(self.prompt, self.models, mode, self.signals(review_intent='architecture'))
                self.assertEqual(result['tier'], mode)
                self.assertEqual(result['model'], self.models[mode])
                self.assertEqual(result['reason'], 'explicit user override')
                self.assertFalse(result['automatic_escalation'])

    def test_resource_fallback_remains_authoritative(self):
        for mode in ('resource-low', 'resource-mid'):
            with self.subTest(mode=mode):
                result = choose(self.prompt, self.models, mode, self.signals(review_intent='architecture'))
                self.assertEqual(result['tier'], mode.removeprefix('resource-'))
                self.assertEqual(result['reason'], 'governed resource fallback')
                self.assertFalse(result['automatic_escalation'])

    def test_unknown_intent_cannot_select_a_quality_profile(self):
        for intent in (None, '', 'ordinary', 'high', True, {}, []):
            with self.subTest(intent=intent), self.assertRaisesRegex(InvalidUpdate, 'review intent'):
                choose(self.prompt, self.models, 'auto', self.signals(review_intent=intent))


if __name__ == '__main__':
    unittest.main()
