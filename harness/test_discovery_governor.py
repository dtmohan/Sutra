"""Actual-source progress is distinct from receipts, recalls and model claims."""
import copy
import hashlib
import json
import random
import unittest

from discovery_governor import DEFAULT_POLICY, LIMITS, _add_interval, assess


VERSION = hashlib.sha256(b'synthetic source file').hexdigest()


def frame(steps=None):
    return {'format': 1, 'binding': {'session': 's1', 'task': 't1',
            'stage': 'discovery', 'contract_hash': 'contract1'}, 'steps': steps or []}


def record(index, kind='read', target='a.py', query='', text='source line\n',
           line=1, column=0, version=VERSION, **result_overrides):
    excerpts = [] if text is None else [{'id': 'volatile-evidence-' + str(index), 'path': target,
        'sha256': version, 'line': line, 'column': column, 'text': text}]
    result = {'status': 'ok', 'excerpts': excerpts, 'versions': {target: version},
              'truncated': False, 'scanned_files': 1}
    result.update(result_overrides)
    return {'kind': 'repository', 'record': {'id': 'receipt-' + str(index),
        'operation': {'session': 's1', 'contract_hash': 'contract1', 'kind': kind,
                      'target': target, 'query': query, 'line': line}, 'result': result}}


class DiscoveryGovernorTests(unittest.TestCase):
    def test_empty_frame_has_no_nudge_or_semantic_claim(self):
        result = assess(frame())
        self.assertEqual(result['status'], 'green')
        self.assertIsNone(result['host_delta'])
        self.assertEqual(result['action'], 'continue')
        self.assertEqual(result['coverage']['semantic_progress'], 'unassessed')
        self.assertNotIn('decision', result)
        self.assertNotIn('done', result)
        self.assertNotIn('permissions', result)

    def test_changing_receipt_and_evidence_ids_does_not_invent_progress(self):
        steps = [record(n) for n in range(5)]
        result = assess(frame(steps))
        self.assertEqual(result['counts']['new_source_spans'], 1)
        self.assertEqual(result['counts']['repeated_source_spans'], 4)
        self.assertEqual(result['counts']['progress_operations'], 1)
        self.assertEqual(result['counts']['repeated_requests'], 4)
        self.assertEqual(result['repeated_request_streak'], 4)
        self.assertEqual(result['same_request_streak'], 4)
        self.assertEqual(result['status'], 'partial_synthesis')
        self.assertEqual(result['action'], 'synthesize_partial')
        self.assertNotIn('FIN', json.dumps(result))

    def test_yellow_then_two_progress_operations_clear_warning(self):
        steps = [record(n) for n in range(3)]
        yellow = assess(frame(steps))
        self.assertEqual(yellow['status'], 'yellow')
        self.assertIsNotNone(yellow['host_delta'])
        steps.append(record(3, line=2))
        self.assertEqual(assess(frame(steps))['status'], 'yellow')
        steps.append(record(4, line=3))
        recovered = assess(frame(steps))
        self.assertEqual(recovered['status'], 'green')
        self.assertIsNone(recovered['host_delta'])

    def test_replayed_same_receipt_is_not_a_new_operation_or_stall(self):
        one = record(1)
        result = assess(frame([one] * 30))
        self.assertEqual(result['counts']['repository_operations'], 1)
        self.assertEqual(result['counts']['replayed_receipts'], 29)
        self.assertEqual(result['no_progress_streak'], 0)
        changed = copy.deepcopy(one); changed['record']['result']['status'] = 'failed'
        with self.assertRaises(ValueError):
            assess(frame([one, changed]))

    def test_located_subset_and_repartition_are_already_covered(self):
        steps = [record(0, text='abcdefghij'), record(1, text='cde', column=2),
                 record(2, text='abcde'), record(3, text='fghij', column=5)]
        result = assess(frame(steps))
        self.assertEqual(result['counts']['new_source_spans'], 1)
        self.assertEqual(result['counts']['repeated_source_spans'], 3)
        self.assertEqual(result['counts']['no_progress_operations'], 3)

    def test_adjacent_intervals_union_then_subspan_is_not_new(self):
        steps = [record(0, text='abcde'), record(1, text='fghij', column=5),
                 record(2, text='cdefgh', column=2)]
        result = assess(frame(steps))
        self.assertEqual(result['counts']['new_source_spans'], 2)
        self.assertEqual(result['counts']['repeated_source_spans'], 1)

    def test_interval_union_matches_independent_covered_position_oracle(self):
        generator = random.Random(404); intervals = []; covered = set()
        for _ in range(1000):
            start = generator.randrange(10000); end = start + generator.randrange(1, 20)
            positions = set(range(start, end))
            expected_new = bool(positions - covered)
            self.assertEqual(_add_interval(intervals, (1, start), (1, end)), expected_new)
            covered.update(positions)
            projected = set()
            for a, b in intervals:
                projected.update(range(a[1], b[1]))
            self.assertEqual(projected, covered)
            self.assertTrue(all(left[1] < right[0] for left, right in zip(intervals, intervals[1:])))

    def test_maximum_disjoint_span_exposure_stays_bounded(self):
        steps = []
        for n in range(LIMITS['steps']):
            step = record(n, text=None)
            step['record']['result']['excerpts'] = [
                {'path': 'a.py', 'sha256': VERSION, 'line': n * 64 + k * 2 + 1,
                 'column': 0, 'text': 'x'} for k in range(LIMITS['excerpts_per_step'])]
            steps.append(step)
        result = assess(frame(steps))
        self.assertEqual(result['counts']['new_source_spans'], 4096)
        self.assertEqual(result['counts']['progress_operations'], 128)
        self.assertEqual(result['status'], 'green')

    def test_unicode_crlf_and_multiline_coordinates_preserve_progress(self):
        steps = [record(0, text='😀x\r\ny\u2028z'), record(1, text='y\u2028z', line=2),
                 record(2, text='z', line=3)]
        result = assess(frame(steps))
        self.assertEqual(result['counts']['new_source_spans'], 1)
        self.assertEqual(result['counts']['repeated_source_spans'], 2)

    def test_explicit_end_position_takes_precedence_at_crlf_split(self):
        first = record(0, text='x\r')
        first['record']['result']['excerpts'][0]['end_position'] = {'line': 1, 'column': 2}
        second = record(1, text='\n', column=2)
        second['record']['result']['excerpts'][0]['end_position'] = {'line': 2, 'column': 0}
        result = assess(frame([first, second]))
        self.assertEqual(result['counts']['new_source_spans'], 2)

    def test_source_version_and_path_are_part_of_identity(self):
        changed = hashlib.sha256(b'changed source').hexdigest()
        result = assess(frame([record(0), record(1, version=changed), record(2, target='b.py')]))
        self.assertEqual(result['counts']['new_source_spans'], 3)
        self.assertEqual(result['counts']['repeated_requests'], 0)

    def test_cursor_advancement_and_same_line_continuation_are_progress(self):
        steps = []
        for n in range(5):
            step = record(n, text='abcd', column=n * 4,
                          next_cursor={'line': 1, 'column': (n + 1) * 4, 'sha256': VERSION})
            if n:
                step['record']['operation']['cursor'] = {'line': 1, 'column': n * 4, 'sha256': VERSION}
            steps.append(step)
        result = assess(frame(steps))
        self.assertEqual(result['counts']['cursor_advances'], 5)
        self.assertEqual(result['counts']['progress_operations'], 5)
        self.assertEqual(result['status'], 'green')

    def test_identical_cursor_is_not_progress_on_repeated_receipts(self):
        steps = [record(n, text='abcd', next_cursor={'line': 1, 'column': 4, 'sha256': VERSION})
                 for n in range(5)]
        result = assess(frame(steps))
        self.assertEqual(result['counts']['cursor_advances'], 1)
        self.assertEqual(result['status'], 'partial_synthesis')

    def test_exhaustive_empty_search_closes_option_once_per_source_scope(self):
        steps = [record(n, kind='search', query='literal needle', text=None) for n in range(5)]
        result = assess(frame(steps))
        self.assertEqual(result['counts']['empty_search_closures'], 1)
        self.assertEqual(result['counts']['progress_operations'], 1)
        self.assertEqual(result['status'], 'partial_synthesis')
        steps.append(record(6, kind='search', query='different needle', text=None))
        self.assertEqual(assess(frame(steps))['counts']['empty_search_closures'], 2)

    def test_empty_search_with_no_scan_or_truncation_does_not_close_option(self):
        steps = [record(0, kind='search', text=None, scanned_files=0),
                 record(1, kind='search', text=None, truncated=True)]
        result = assess(frame(steps))
        self.assertEqual(result['counts']['empty_search_closures'], 0)
        self.assertEqual(result['counts']['progress_operations'], 0)

    def test_same_empty_query_at_changed_source_version_can_close_again(self):
        changed = hashlib.sha256(b'new').hexdigest()
        result = assess(frame([record(0, kind='search', text=None),
                               record(1, kind='search', text=None, version=changed)]))
        self.assertEqual(result['counts']['empty_search_closures'], 2)

    def test_cycling_old_requests_is_distinct_from_same_request_persistence(self):
        steps = [record(n, kind='search', query='needle' + str(n % 2), text=None)
                 for n in range(6)]
        result = assess(frame(steps))
        self.assertEqual(result['repeated_request_streak'], 4)
        self.assertEqual(result['same_request_streak'], 0)
        self.assertEqual(result['counts']['empty_search_closures'], 2)
        self.assertEqual(result['status'], 'partial_synthesis')

    def test_admitted_read_profile_is_part_of_request_identity(self):
        first = record(0); second = record(1)
        first['record']['operation']['read_profile'] = {'version': 1, 'max_bytes': 512, 'max_lines': 16}
        second['record']['operation']['read_profile'] = {'version': 1, 'max_bytes': 1024, 'max_lines': 16}
        result = assess(frame([first, second]))
        self.assertEqual(result['counts']['progress_operations'], 1)
        self.assertEqual(result['counts']['repeated_requests'], 0)
        self.assertEqual(result['counts']['no_progress_operations'], 1)

    def test_search_snippet_without_column_is_not_assumed_at_column_zero(self):
        hit = record(1, kind='search', text='target')
        hit['record']['result']['excerpts'][0].pop('column')
        result = assess(frame([record(0, text='prefix'), hit, copy.deepcopy(hit)]))
        self.assertEqual(result['counts']['new_source_spans'], 1)
        self.assertEqual(result['counts']['new_search_snippets'], 1)
        self.assertEqual(result['coverage']['source_location_coverage'], 'partial')
        self.assertEqual(result['counts']['replayed_receipts'], 1)

    def test_real_repository_search_shape_never_places_snippet_at_line_start(self):
        body = 'a' * 200 + 'needle' + 'z' * 120 + '\n'
        start = max(0, body.index('needle') - 60)
        snippet = body[start:start + 240]
        hit = record(0, kind='search', query='needle', text=snippet)
        hit['record']['result']['excerpts'][0].pop('column')
        repeated = record(2, kind='search', query='needle', text=snippet)
        repeated['record']['result']['excerpts'][0].pop('column')
        # The unknown-column hit precedes a genuine legacy read of the prefix.
        # Treating that hit as column 0 would hide this new read exposure.
        prefix = record(1, text=body[:100])
        prefix['record']['result']['excerpts'][0].pop('column')
        result = assess(frame([hit, prefix, repeated]))
        self.assertEqual(result['counts']['new_search_snippets'], 1)
        self.assertEqual(result['counts']['repeated_search_snippets'], 1)
        self.assertEqual(result['counts']['new_source_spans'], 1)
        self.assertEqual(result['counts']['repeated_source_spans'], 0)
        self.assertEqual(result['counts']['progress_operations'], 2)
        self.assertEqual(result['counts']['no_progress_operations'], 1)
        self.assertEqual(result['coverage']['unlocated_search_snippets'], 2)
        self.assertFalse(result['coverage']['unlocated_snippets_merged_into_source_ranges'])

    def test_search_snippet_same_as_read_text_is_still_not_an_exact_range(self):
        hit = record(1, kind='search', text='needle')
        hit['record']['result']['excerpts'][0].pop('column')
        result = assess(frame([record(0, text='needle'), hit]))
        self.assertEqual(result['counts']['new_source_spans'], 1)
        self.assertEqual(result['counts']['new_search_snippets'], 1)
        self.assertEqual(result['coverage']['source_location_coverage'], 'partial')

    def test_list_exposure_counts_once_but_does_not_mean_file_inspection(self):
        steps = [record(n, kind='list', text=None, files=['a.py', 'b.py']) for n in range(5)]
        result = assess(frame(steps))
        self.assertEqual(result['counts']['new_list_paths'], 2)
        self.assertEqual(result['frontier']['observed_read_files'], 0)
        self.assertEqual(result['status'], 'partial_synthesis')

    def test_recall_is_separate_neutral_and_not_semantic_progress(self):
        recalls = [{'kind': 'recall', 'ref': 'memory:page1'}] * 50
        result = assess(frame([record(0)] + recalls))
        self.assertEqual(result['counts']['recalls'], 50)
        self.assertEqual(result['counts']['repeated_recalls'], 49)
        self.assertEqual(result['counts']['progress_operations'], 1)
        self.assertEqual(result['no_progress_streak'], 0)
        self.assertEqual(result['status'], 'green')
        steps = [record(0), record(1)] + recalls + [record(2)]
        self.assertEqual(assess(frame(steps))['status'], 'yellow')

    def test_no_model_notes_remaining_or_closure_can_change_assessment(self):
        f = frame([record(n) for n in range(5)])
        before = assess(f)
        for step in f['steps']:
            step['record']['operation']['model'] = 'a-stronger-model'
            step['record']['result']['model_remaining'] = []
            step['record']['result']['model_done'] = True
        self.assertEqual(before, assess(f))
        f['remaining'] = []
        with self.assertRaises(ValueError):
            assess(f)

    def test_reconstruction_reload_model_switch_and_input_immutability(self):
        f = frame([record(n) for n in range(5)])
        before = copy.deepcopy(f); expected = assess(f)
        restored = json.loads(json.dumps(f))
        self.assertEqual(expected, assess(restored))
        self.assertEqual(f, before)
        self.assertLess(len(json.dumps(expected)), 10000)
        self.assertNotIn('source line', json.dumps(expected))

    def test_scoped_task_and_stage_binding_cannot_leak_through_module_state(self):
        stalled = frame([record(n) for n in range(5)])
        assess(stalled)
        other = frame([record(50)]); other['binding']['task'] = 'different-task'
        self.assertEqual(assess(other)['status'], 'green')
        other['binding']['stage'] = 'different-stage'
        self.assertEqual(assess(other)['counts']['progress_operations'], 1)
        for key in ('session', 'contract_hash'):
            bad = copy.deepcopy(stalled); bad['binding'][key] = 'different'
            with self.subTest(key=key), self.assertRaises(ValueError):
                assess(bad)

    def test_versioned_policy_and_measured_complexity_are_explicit(self):
        f = frame([record(n) for n in range(7)])
        f['policy'] = {'version': 1, 'yellow_after': 3, 'synthesize_after': 6,
                       'recover_after': 2, 'window': 8}
        f['complexity'] = {'catalog_files': 1000, 'functions': 9000, 'frontier_files': 999}
        result = assess(f)
        self.assertEqual(result['status'], 'partial_synthesis')
        self.assertEqual(result['policy']['synthesize_after'], 6)
        self.assertEqual(result['frontier']['catalog_files'], 1000)
        self.assertEqual(result['frontier']['observed_read_files'], 1)
        self.assertNotIn('semantic_complete', result['frontier'])
        for policy in ({'version': 2}, {'yellow_after': 4, 'synthesize_after': 2},
                       {'window': True}, {'synthesize_after': 100000}, {'remaining': 0}):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                assess(dict(f, policy=policy))

    def test_history_tail_is_bounded_and_reports_missing_baseline(self):
        f = frame([record(n) for n in range(10000)])
        result = assess(f)
        self.assertEqual(result['counts']['repository_operations'], LIMITS['steps'])
        self.assertEqual(result['coverage']['omitted_steps'], 10000 - LIMITS['steps'])
        self.assertFalse(result['coverage']['complete_input_history'])
        self.assertEqual(result['counts']['new_source_spans'], 1)

    def test_oversized_text_is_not_hashed_or_treated_as_known_stall(self):
        steps = [record(n, text='x' * (LIMITS['span_chars'] + 1)) for n in range(5)]
        result = assess(frame(steps))
        self.assertFalse(result['coverage']['complete_input_history'])
        self.assertEqual(result['coverage']['observed_text_chars'], 0)
        self.assertEqual(result['counts']['unknown_operations'], 5)
        self.assertEqual(result['no_progress_streak'], 0)
        self.assertEqual(result['status'], 'green')

    def test_excerpt_and_version_caps_make_partial_scope_visible(self):
        step = record(0, kind='search', text=None)
        step['record']['result']['versions'] = {'p%d.py' % n: VERSION for n in range(1000)}
        result = assess(frame([step]))
        self.assertEqual(result['counts']['empty_search_closures'], 0)
        self.assertEqual(result['counts']['omitted_versions'], 744)
        self.assertEqual(result['counts']['unknown_operations'], 1)
        step = record(0)
        step['record']['result']['excerpts'] *= 1000
        result = assess(frame([step]))
        self.assertEqual(result['counts']['omitted_excerpts'], 1000 - LIMITS['excerpts_per_step'])
        self.assertFalse(result['coverage']['complete_input_history'])

    def test_global_text_work_is_capped_and_unknown_does_not_build_streak(self):
        steps = [record(n, text='x' * LIMITS['span_chars'], line=n + 1) for n in range(128)]
        result = assess(frame(steps))
        self.assertLessEqual(result['coverage']['observed_text_chars'], LIMITS['text_chars'])
        self.assertGreater(result['counts']['unknown_operations'], 0)
        self.assertEqual(result['no_progress_streak'], 0)

    def test_invalid_schemas_positions_and_huge_integers_fail_without_state(self):
        for value in (None, {}, {'format': True}, dict(frame(), steps='model said done'),
                      dict(frame(), binding={'session': 's1'})):
            with self.subTest(value=value), self.assertRaises(ValueError):
                assess(value)
        for key, value in [('line', True), ('line', -1), ('line', 10**10000)]:
            bad = record(0); bad['record']['result']['excerpts'][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                assess(frame([bad]))
        bad = record(0); bad['record']['result']['excerpts'][0]['end_position'] = {'line': 1, 'column': 0}
        with self.assertRaises(ValueError):
            assess(frame([bad]))
        self.assertEqual(assess(frame())['status'], 'green')


if __name__ == '__main__':
    unittest.main()
