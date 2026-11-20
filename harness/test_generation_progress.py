"""Literal streaming observations are finite and never a semantic stop rule."""
import copy
import json
import random
import unittest

from generation_progress import LIMITS, Progress, validate_snapshot


def phrase(index):
    return ('Observation number %03d revisits an evidence location and then considers the same procedural question.' % index)


def run(text, chunk=1024):
    progress = Progress(); alerts = 0
    for start in range(0, len(text), chunk):
        state = progress.feed(text[start:start + chunk]); validate_snapshot(state)
        alerts += int(state['alert'])
    state = progress.finish(); validate_snapshot(state)
    return progress, state, alerts + int(state['alert'])


class GenerationProgressTests(unittest.TestCase):
    def test_synthetic_long_repetitive_deliberation_flags_once_without_final_answer(self):
        # Synthetic, not copied from the user's private conversation. A long
        # generation cycles through 54 distinct long sentences 923 times total.
        text = ' '.join(phrase(n % 54) for n in range(923))
        observer, state, alerts = run(text, 997)
        self.assertEqual(state['long_units'], 923)
        self.assertEqual(state['novel_units'], 54)
        self.assertEqual(state['duplicate_units'], 869)
        self.assertEqual(alerts, 1)
        self.assertTrue(state['repetition_indicator'])
        self.assertTrue(state['current_repetition'])
        self.assertTrue(state['analysis_complete'])
        self.assertEqual(state['recent_duplicate_fraction'], 1.)
        self.assertEqual(state['received_chars'], len(text))
        self.assertNotIn('decision', state)
        self.assertNotIn('cancel', state)
        self.assertEqual(state['semantic_assessment'], 'unassessed')
        self.assertFalse(observer.finish()['alert'])

    def test_varied_analysis_and_refinement_do_not_repeat_exact_units(self):
        text = ' '.join(phrase(n) + ' The next measured value is %d.' % n for n in range(100))
        _, state, alerts = run(text, 31)
        self.assertEqual(alerts, 0)
        self.assertFalse(state['repetition_indicator'])
        self.assertEqual(state['duplicate_units'], 0)
        self.assertTrue(state['analysis_complete'])

    def test_greetings_and_short_units_do_not_satisfy_minimum_observation(self):
        for text in ('', 'Hello!', 'Hello! How are you? ' * 500):
            with self.subTest(size=len(text)):
                _, state, alerts = run(text)
                self.assertEqual(alerts, 0)
                self.assertFalse(state['repetition_indicator'])
                self.assertEqual(state['long_units'], 0)

    def test_small_repetition_is_not_enough_even_with_long_input(self):
        _, state, alerts = run((phrase(1) + ' ') * 31)
        self.assertGreater(state['observed_chars'], LIMITS['min_observed_chars'])
        self.assertEqual(state['long_units'], 31)
        self.assertFalse(state['repetition_indicator'])
        self.assertEqual(alerts, 0)

    def test_chunk_boundaries_have_no_unit_meaning_when_not_capped(self):
        text = ' '.join(phrase(n % 7) for n in range(60)) + ' Unfinished final observations use the supplied evidence conservatively'
        expected = run(text, len(text))[1]
        for chunk in (1, 2, 7, 37, 101, 1024):
            with self.subTest(chunk=chunk):
                result = run(text, chunk)[1]
                # finish() can be the first alert for a boundary in some inputs;
                # alert is an emission edge, not persistent observation state.
                for key in expected.keys() - {'alert'}:
                    self.assertEqual(result[key], expected[key], key)

    def test_character_floor_crossing_inside_pending_unicode_unit_latches_once(self):
        text = '😀 A different statement about evidence and retained context.\n' * 60
        observer, state, alerts = run(text, 64)  # Validates EVERY emitted feed.
        self.assertEqual(alerts, 1)
        self.assertTrue(state['repetition_indicator'])
        expected = run(text, len(text))[1]
        for key in expected.keys() - {'alert'}:
            self.assertEqual(state[key], expected[key], key)
        self.assertFalse(observer.snapshot()['alert'])

    def test_chunk_metamorphic_property_around_minimum_unit_and_character_floors(self):
        rng = random.Random(73)
        for length in (40, 41, 50, 60, 61, 63, 64, 96):
            with self.subTest(unit_length=length):
                text = ('😀 ' + 'x' * (length - 3) + '.\n') * 70
                expected = run(text, len(text))[1]
                observer = Progress(); start = 0; alerts = 0
                while start < len(text):
                    width = rng.randint(1, 173)
                    result = observer.feed(text[start:start + width]); validate_snapshot(result)
                    alerts += int(result['alert']); start += width
                result = observer.finish(); validate_snapshot(result)
                alerts += int(result['alert'])
                self.assertEqual(alerts, 1)
                for key in expected.keys() - {'alert'}:
                    self.assertEqual(result[key], expected[key], key)

    def test_unicode_whitespace_normalization_and_sentence_boundaries(self):
        sentence = ('这里保留独立的证据范围和具体来源，同时继续检查相同的推理步骤是否重复进行' * 2) + '。'
        # Each qualifying sentence is long enough without counting UTF-8 bytes.
        _, state, alerts = run((sentence + '\u2003') * 80, 3)
        self.assertGreater(state['long_units'], 32)
        self.assertTrue(state['repetition_indicator'])
        self.assertEqual(alerts, 1)
        observer = Progress()
        a = 'The evidence location remains unchanged while the next action is considered carefully.'
        observer.feed((a + '\n') * 16)
        result = observer.feed((a.replace(' ', '\u2003') + '\n') * 16)
        self.assertTrue(result['repetition_indicator'])
        validate_snapshot(result)

    def test_no_punctuation_uses_bounded_fixed_size_pieces(self):
        text = 'x' * (LIMITS['pending_chars'] * 32)
        observer, state, alerts = run(text, 511)
        self.assertEqual(state['long_units'], 32)
        self.assertEqual(state['novel_units'], 1)
        self.assertEqual(state['duplicate_units'], 31)
        self.assertEqual(alerts, 1)
        self.assertEqual(len(observer._pending), 0)

    def test_huge_feed_is_explicitly_sampled_without_retaining_or_scanning_it_all(self):
        observer = Progress(); text = 'x' * (LIMITS['feed_chars'] * 50)
        state = observer.feed(text)
        self.assertEqual(state['received_chars'], len(text))
        self.assertEqual(state['observed_chars'], LIMITS['feed_chars'])
        self.assertEqual(state['omitted_chars'], len(text) - LIMITS['feed_chars'])
        self.assertFalse(state['analysis_complete'])
        self.assertEqual(state['pending_chars'], 0)
        self.assertEqual(state['long_units'], 4)
        validate_snapshot(state)
        # A sampled gap does not concatenate the tail of a prefix with a later
        # feed to manufacture a sentence or a continuous recent-unit window.
        newer = observer.feed('A fresh bounded generation segment starts here with different text.')
        self.assertEqual(newer['recent_units'], 1)

    def test_long_history_retains_only_bounded_digests_and_pending_text(self):
        observer = Progress()
        for n in range(2000):
            state = observer.feed(phrase(n) + ' ')
            self.assertLessEqual(len(observer._digests), LIMITS['digest_units'])
            self.assertLessEqual(len(observer._counts), LIMITS['digest_units'])
            self.assertLessEqual(len(observer._recent), LIMITS['recent_units'])
            self.assertLessEqual(len(observer._pending), LIMITS['pending_chars'])
        self.assertEqual(state['long_units'], 2000)
        self.assertFalse(state['repetition_indicator'])
        self.assertLess(len(json.dumps(state)), 2048)
        self.assertFalse(hasattr(observer, '__dict__'))

    def test_repeated_quoted_material_is_only_an_indicator_not_a_semantic_verdict(self):
        quote = 'The caller explicitly requested this exact quotation to be repeated for formatting demonstrations.'
        _, state, alerts = run((quote + '\n') * 40)
        self.assertTrue(state['repetition_indicator'])
        self.assertEqual(alerts, 1)
        self.assertEqual(state['semantic_assessment'], 'unassessed')
        self.assertFalse(any(key in state for key in ('semantic_correctness_verified', 'action', 'decision', 'cancel')))

    def test_typical_code_and_repeated_identifiers_are_not_semantic_equivalence(self):
        code = '\n'.join('def calculate_%d(value):\n    return value + %d  # independent operation with a distinct measured constant' % (n, n)
                         for n in range(80))
        _, state, alerts = run(code, 73)
        self.assertEqual(alerts, 0)
        self.assertFalse(state['repetition_indicator'])

    def test_indicator_is_historical_current_window_can_recover(self):
        observer = Progress(); alerts = []
        alerts.append(observer.feed((phrase(1) + ' ') * 40)['alert'])
        state = observer.feed(' '.join(phrase(n + 100) for n in range(40)))
        alerts.append(state['alert'])
        self.assertEqual(alerts, [True, False])
        self.assertTrue(state['repetition_indicator'])
        self.assertFalse(state['current_repetition'])
        self.assertEqual(state['recent_novelty_fraction'], 1.)
        validate_snapshot(state)

    def test_invalid_unicode_nontext_and_post_finish_chunks_are_partial_not_errors(self):
        observer = Progress()
        state = observer.feed('prefix without a boundary' + '\ud800' + phrase(1))
        self.assertEqual(state['invalid_chars'], 1)
        self.assertFalse(state['analysis_complete'])
        validate_snapshot(state)
        state = observer.feed(object())
        self.assertEqual(state['invalid_feeds'], 1)
        validate_snapshot(state)
        observer.finish(); before = observer.snapshot()
        state = observer.feed('late chunk')
        self.assertTrue(state['finished'])
        self.assertEqual(state['long_units'], before['long_units'])
        self.assertEqual(state['omitted_chars'], before['omitted_chars'] + 10)
        self.assertFalse(state['alert'])
        validate_snapshot(state)

    def test_finish_flushes_pending_once_and_returned_limits_are_detached(self):
        observer = Progress(); observer.feed('A final observation with enough characters but no sentence delimiter')
        result = observer.finish()
        self.assertEqual(result['long_units'], 1)
        self.assertEqual(result['pending_chars'], 0)
        self.assertEqual(observer.finish(), result)
        result['limits']['feed_chars'] = 1
        self.assertEqual(observer.snapshot()['limits'], LIMITS)

    def test_snapshot_validator_rejects_raw_text_bad_counts_types_ratios_and_flags(self):
        _, valid, _ = run((phrase(1) + ' ') * 40)
        mutations = [dict(raw_text='do not retain this'), dict(received_chars=0),
                     dict(long_units=True), dict(long_units=2**53), dict(finished='yes'),
                     dict(recent_duplicate_fraction=float('nan')), dict(recent_novelty_fraction=.5),
                     dict(current_repetition=False), dict(repetition_indicator=False),
                     dict(analysis_complete=False), dict(semantic_assessment='verified'),
                     dict(pending_chars=1), dict(limits={**LIMITS, 'min_unit_chars': 1})]
        mutations.append(dict(novel_units=0, duplicate_units=valid['long_units']))
        mutations.append(dict(recent_duplicate_fraction=2**10000))
        for values in mutations:
            with self.subTest(values=values):
                bad = copy.deepcopy(valid); bad.update(values)
                with self.assertRaises(ValueError): validate_snapshot(bad)
        copied = validate_snapshot(valid); copied['limits']['feed_chars'] = 1
        self.assertEqual(valid['limits']['feed_chars'], LIMITS['feed_chars'])


if __name__ == '__main__': unittest.main()
