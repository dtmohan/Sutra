"""Provenance, scoped meaning and finite structural checks, not NLP accuracy."""
import copy
import hashlib
import json
import unittest

from context_manager import InvalidUpdate
import text_coherence as coherence


def frame():
    return {'format': 1, 'binding': {'session': 'session1', 'task': 'task1', 'chat': 'chat1'},
            'active_context': 'current', 'contexts': [{'id': 'current', 'mode': 'factual'}],
            'sources': [], 'items': [], 'edges': [], 'required_question_ids': [], 'closed_question_ids': []}


def source(f, sid='source1', text='The delivery count is ten.', **kwargs):
    value = dict(id=sid, context_id='current', binding=copy.deepcopy(f['binding']),
                 authority='source', text=text, sha256=hashlib.sha256(text.encode()).hexdigest(),
                 version=1, current_version=1, receipt_id='receipt-' + sid)
    value.update(kwargs); f['sources'].append(value)
    return value


def item(f, iid='claim1', kind='claim', text='Delivery count is ten.', **kwargs):
    value = dict(id=iid, context_id='current', kind=kind, text=text)
    value.update(kwargs); f['items'].append(value)
    return value


def edge(f, left, right, kind='supports', **kwargs):
    value = dict(**{'from': left, 'to': right}, kind=kind)
    value.update(kwargs); f['edges'].append(value)


def codes(result): return {issue['code'] for issue in result['issues']}


class TextCoherenceTests(unittest.TestCase):
    def test_empty_projection_does_not_invent_semantic_coverage_or_open_work(self):
        result = coherence.assess(frame())
        self.assertEqual(result['status'], 'assessed')
        self.assertEqual(result['issues'], [])
        self.assertEqual(result['signals']['open_obligations'], 0)
        self.assertTrue(result['signals']['within_limits'])
        self.assertEqual(result['analysis_limits']['semantic_graph_coverage'], 'partial')
        self.assertEqual(result['analysis_limits']['free_text_coherence'], 'unassessed')

    def test_exact_unicode_span_proves_provenance_not_entailment_or_closure(self):
        f = frame(); s = source(f, text='😀 The count is ten.'); q = item(f, 'q', 'question', 'How many?')
        item(f, spans=[{'source_id': s['id'], 'start': 2, 'end': len(s['text']), 'quote': s['text'][2:]}])
        edge(f, 'claim1', q['id'], 'answers'); f['required_question_ids'] = ['q']
        before = copy.deepcopy(f); result = coherence.assess(f)
        self.assertEqual(f, before)
        self.assertEqual(result['source_linked_claim_ids'], ['claim1'])
        self.assertEqual(result['answered_claimed_unverified'], ['q'])
        self.assertEqual(result['open_obligations'], ['q'])
        self.assertEqual(result['analysis_limits']['source_entailment'], 'unverified')
        self.assertEqual(result, coherence.assess(f))

    def test_model_source_cannot_become_source_evidence_by_repetition(self):
        f = frame(); source(f, authority='model'); item(f); edge(f, 'claim1', 'source1')
        result = coherence.assess(f)
        self.assertEqual(result['source_linked_claim_ids'], [])
        self.assertEqual(result['unsupported_claim_ids'], ['claim1'])
        self.assertIn('unsupported_claim', codes(result))

    def test_missing_source_and_mismatched_span_are_distinct_recoverable_findings(self):
        f = frame(); source(f)
        item(f, 'missing', spans=[dict(source_id='absent', start=0, end=3, quote='ten')])
        item(f, 'wrong', spans=[dict(source_id='source1', start=0, end=3, quote='ten')])
        result = coherence.assess(f)
        self.assertEqual(result['signals']['missing_references'], 1)
        self.assertEqual(result['signals']['invalid_references'], 1)
        self.assertFalse(result['signals']['references_valid'])
        self.assertTrue({'missing_source', 'invalid_source_span'} <= codes(result))

    def test_wrong_digest_stale_version_and_foreign_chat_never_root_a_claim(self):
        for field, value, code in [('sha256', '0' * 64, 'source_digest_mismatch'),
                                    ('current_version', 2, 'stale_source'),
                                    ('binding', dict(session='session1', task='task1', chat='other'), 'source_binding_mismatch')]:
            with self.subTest(field=field):
                f = frame(); source(f, **{field: value}); item(f); edge(f, 'claim1', 'source1')
                result = coherence.assess(f)
                self.assertEqual(result['unsupported_claim_ids'], ['claim1'])
                self.assertIn(code, codes(result))
                if field == 'binding': self.assertFalse(result['signals']['binding_valid'])
                if field == 'current_version': self.assertFalse(result['signals']['sources_current'])

    def test_scoped_value_difference_is_an_indicator_only(self):
        f = frame()
        item(f, 'a', key='count', value=10, requires_evidence=False)
        item(f, 'b', key='count', value=11, requires_evidence=False)
        result = coherence.assess(f)
        self.assertEqual(result['signals']['conflict_indicators'], 1)
        conflict = next(x for x in result['issues'] if x['code'] == 'value_conflict_indicator')
        self.assertEqual(conflict['kind'], 'semantic_indicator')
        self.assertTrue(result['signals']['references_valid'])
        self.assertNotIn('decision', result)

    def test_hypothetical_different_subject_and_time_are_separate_contexts(self):
        for other in [dict(id='other', mode='hypothetical'),
                      dict(id='other', mode='factual', subject='a different subject'),
                      dict(id='other', mode='factual', time='last year')]:
            with self.subTest(context=other):
                f = frame(); f['contexts'].append(other)
                item(f, 'a', key='count', value=10, requires_evidence=False)
                item(f, 'b', context_id='other', key='count', value=11, requires_evidence=False)
                self.assertEqual(coherence.assess(f)['signals']['conflict_indicators'], 0)

    def test_hypothetical_premise_cannot_establish_a_fact_even_with_host_context_link(self):
        f = frame(); f['contexts'].append(dict(id='assumed', mode='hypothetical'))
        source(f, context_id='assumed'); item(f)
        edge(f, 'claim1', 'source1', origin='host', cross_context=True)
        result = coherence.assess(f)
        self.assertIn('hypothetical_premise', codes(result))
        self.assertEqual(result['unsupported_claim_ids'], ['claim1'])

    def test_importing_another_context_requires_explicit_host_registration(self):
        f = frame(); f['contexts'].append(dict(id='earlier', mode='factual'))
        source(f, context_id='earlier'); item(f); edge(f, 'claim1', 'source1')
        result = coherence.assess(f)
        self.assertEqual(result['signals']['cross_context_references'], 1)
        self.assertEqual(result['unsupported_claim_ids'], ['claim1'])
        f['edges'][0].update(origin='host', cross_context=True)
        self.assertEqual(coherence.assess(f)['source_linked_claim_ids'], ['claim1'])
        f['edges'][0]['origin'] = 'model'
        with self.assertRaisesRegex(InvalidUpdate, 'Only the host'):
            coherence.assess(f)

    def test_cycles_and_dependents_stay_unsupported_even_with_external_source(self):
        f = frame(); source(f)
        for iid in ('a', 'b', 'c'): item(f, iid)
        for a, b in [('a', 'b'), ('b', 'a'), ('a', 'source1'), ('c', 'a'), ('c', 'source1')]: edge(f, a, b)
        result = coherence.assess(f)
        self.assertEqual(result['signals']['circular_support_components'], 1)
        self.assertEqual(result['unsupported_claim_ids'], ['a', 'b', 'c'])
        self.assertFalse(result['signals']['support_acyclic'])

    def test_self_support_and_parked_unrelated_cycles(self):
        f = frame(); item(f, 'self'); edge(f, 'self', 'self')
        self.assertEqual(coherence.assess(f)['signals']['circular_support_components'], 1)
        f['contexts'].append(dict(id='parked', mode='factual'))
        f['items'][0]['context_id'] = 'parked'
        result = coherence.assess(f)
        self.assertTrue(result['signals']['support_acyclic'])
        self.assertEqual(result['coverage']['analyzed_items'], 0)

    def test_host_can_select_multiple_qualified_contexts_without_cross_scope_premises(self):
        f = frame(); f['contexts'].append(dict(id='earlier', mode='factual', time='last year'))
        for iid, count in [('old1', 10), ('old2', 11)]:
            item(f, iid, context_id='earlier', key='count', value=count, requires_evidence=False)
        self.assertEqual(coherence.assess(f)['signals']['conflict_indicators'], 0)
        f['relevant_context_ids'] = ['current', 'earlier']
        result = coherence.assess(f)
        self.assertEqual(result['signals']['conflict_indicators'], 1)
        self.assertEqual(result['coverage']['analyzed_items'], 2)
        self.assertEqual(result['signals']['cross_context_references'], 0)
        f['relevant_context_ids'] = ['unprovided']
        with self.assertRaisesRegex(InvalidUpdate, 'Unknown relevant'):
            coherence.assess(f)

    def test_unasked_questions_are_not_obligations_and_model_answers_do_not_close(self):
        f = frame(); source(f); item(f); item(f, 'q', 'question', 'What remains?')
        edge(f, 'claim1', 'source1'); edge(f, 'claim1', 'q', 'answers', origin='host')
        self.assertEqual(coherence.assess(f)['open_obligations'], [])
        f['required_question_ids'] = ['q']
        result = coherence.assess(f)
        self.assertEqual(result['answered_claimed_unverified'], ['q'])
        self.assertEqual(result['open_obligations'], ['q'])
        f['closed_question_ids'] = ['q']
        result = coherence.assess(f)
        self.assertEqual(result['open_obligations'], [])
        self.assertEqual(result['coverage']['host_closed_questions'], 1)

    def test_model_update_does_not_suppress_a_value_conflict_host_update_can(self):
        f = frame(); source(f)
        for iid, value in [('a', 10), ('b', 11)]:
            item(f, iid, key='count', value=value); edge(f, iid, 'source1')
        edge(f, 'b', 'a', 'supersedes')
        result = coherence.assess(f)
        self.assertEqual(result['signals']['conflict_indicators'], 1)
        self.assertEqual(result['signals']['model_closure_proposals'], 1)
        f['edges'][-1]['origin'] = 'host'
        self.assertEqual(coherence.assess(f)['signals']['conflict_indicators'], 0)
        self.assertEqual(len(f['items']), 2)  # History is retained, not erased.

    def test_circular_host_supersession_cannot_hide_conflicts(self):
        f = frame(); source(f)
        for iid, value in [('a', 10), ('b', 11)]:
            item(f, iid, key='count', value=value); edge(f, iid, 'source1')
        edge(f, 'a', 'b', 'supersedes', origin='host'); edge(f, 'b', 'a', 'supersedes', origin='host')
        result = coherence.assess(f)
        self.assertEqual(result['signals']['conflict_indicators'], 1)
        self.assertIn('invalid_supersession', codes(result))

    def test_missing_edge_and_unknown_obligation_are_explicit(self):
        f = frame(); item(f); edge(f, 'claim1', 'absent')
        f['required_question_ids'] = ['unprovided']
        result = coherence.assess(f)
        self.assertEqual(result['signals']['missing_references'], 2)
        self.assertEqual(result['open_obligations'], ['unprovided'])
        self.assertTrue({'unknown_question', 'missing_reference'} <= codes(result))

    def test_limits_return_partial_without_claiming_omitted_data_clear(self):
        fixtures = []
        f = frame(); f['items'] = [dict(id=str(n)) for n in range(129)]; fixtures.append(f)
        f = frame(); source(f, text='x' * 65537); fixtures.append(f)
        f = frame(); item(f, text='x' * 4097); fixtures.append(f)
        f = frame(); item(f, spans=[{}] * 9); fixtures.append(f)
        f = frame(); f['edges'] = [{}] * 257; fixtures.append(f)
        for f in fixtures:
            with self.subTest(keys=list(f)):
                result = coherence.assess(f)
                self.assertEqual(result['status'], 'partial')
                self.assertFalse(result['signals']['within_limits'])
                self.assertFalse(result['signals']['references_valid'])
                self.assertEqual(result['coverage']['analyzed_items'], 0)

    def test_schema_rejects_opaque_nested_values_duplicate_ids_and_unknown_fields(self):
        for change in ('nested', 'duplicate', 'unknown', 'boolean_format', 'control_id'):
            with self.subTest(change=change):
                f = frame(); item(f)
                if change == 'nested': f['items'][0]['value'] = {'nested': 'not a scalar'}
                elif change == 'duplicate': f['items'].append(copy.deepcopy(f['items'][0]))
                elif change == 'unknown': f['items'][0]['authority'] = 'host'
                elif change == 'boolean_format': f['format'] = True
                else: f['items'][0]['id'] = 'claim\nother'
                with self.assertRaises(InvalidUpdate): coherence.assess(f)

    def test_invalid_unicode_and_unbounded_versions_are_schema_errors(self):
        f = frame(); source(f); item(f, text='\ud800')
        with self.assertRaisesRegex(InvalidUpdate, 'UTF-8'):
            coherence.assess(f)
        f = frame(); source(f, version=2**10000)
        with self.assertRaisesRegex(InvalidUpdate, 'version'):
            coherence.assess(f)

    def test_maximum_graph_is_bounded_and_does_not_recurse(self):
        f = frame(); source(f)
        for n in range(128): item(f, str(n))
        for n in range(128):
            edge(f, str(n), str((n + 1) % 128)); edge(f, str(n), 'source1')
        result = coherence.assess(f)
        self.assertEqual(result['signals']['circular_support_components'], 1)
        self.assertEqual(result['signals']['unsupported_claims'], 128)
        self.assertEqual(len(result['issues']), coherence.LIMITS['issues'])
        self.assertGreater(result['omitted_issues'], 0)
        self.assertEqual(result['omitted_issue_item_ids'], 120)

    def test_long_identifiers_cannot_make_the_result_exceed_64k(self):
        f = frame(); source(f); claim = 'c' * 128; item(f, claim); edge(f, claim, 'source1')
        for n in range(127):
            qid = str(n).zfill(128); item(f, qid, 'question', 'Question')
            f['required_question_ids'].append(qid); edge(f, claim, qid, 'answers')
        result = coherence.assess(f)
        self.assertEqual(result['signals']['open_obligations'], 127)
        self.assertEqual(result['coverage']['answered_claimed_unverified'], 127)
        self.assertLessEqual(len(json.dumps(result).encode()), 65536)


if __name__ == '__main__': unittest.main()
