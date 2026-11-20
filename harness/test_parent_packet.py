"""Compact unverified child context without losing earlier source findings."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import chat_runtime as chat
from context_manager import canonical
from model_packet import build, child_note_view, select
from paging import Budget
from review_role import constraint_rows


def legacy_child_view(note):
    return select(note, ('files', 'answer_unverified', 'summary_unverified',
                         'remaining_unverified', 'citations', 'evidence_coverage'))


def payload():
    request = 'Explain the architecture with a concrete request flow.'
    contract = {'task_id': 'task', 'goal': request,
                'constraints': {'allowed_paths': ['.'], 'edits_allowed': False,
                                'network_allowed': False, 'output_format': 'architecture with diagram'}}
    notes = []
    for index in range(2):
        prior = [('Earlier finding %s-%s: the source delegates to the helper. ' % (index, number)) * 5
                 for number in range(3)]
        final = ('Final branch finding %s: validation precedes the return. ' % index) * 24
        summary = final[:320]
        notes.append({'invocation_id': 'receipt-%s' % index, 'branch_id': 'branch-%s' % index,
                      'model': 'historical-model', 'files': ['module%s.py' % index],
                      'answer_unverified': final, 'summary_unverified': '\n'.join([*prior, summary]),
                      'prior_summaries_unverified': prior, 'final_summary_unverified': summary,
                      'remaining_unverified': ['Inspect the helper return path.'],
                      'citations': [{'path': 'module%s.py' % index,
                                     'quote': '    # Historical source reference %s: ' % number + 'x' * 110}
                                    for number in range(12)],
                      'evidence_coverage': {'excerpt_files': ['module%s.py' % index],
                                            'exhaustive_semantic_review': False}})
    return {'parent_context_version': 2, 'contract': contract, 'constraint_echo': constraint_rows(contract),
            'request': request, 'route': {'record': {'choice': {'model': 'strata:high'}}},
            'excerpts': {'module0.py': 'def entry():\n    return 1\n'}, 'child_results': notes,
            'memory': {'pinned_user_statements': [{'quote': 'Do not edit or access the network.'}],
                       'recent_turns': [{'user': request, 'assistant_unverified': 'I will inspect source.'}],
                       'toc': [{'id': 'page-1', 'subject': 'Earlier request'}]}}


class ParentPacketTests(unittest.TestCase):
    def test_legacy_recovery_notes_do_not_change_historical_joined_projection(self):
        import subagent_runtime as child
        recovery = {'summary': 'Rejected-branch checkpoint finding.',
                    'citations': [{'path': 'entry.py', 'quote': 'recovery evidence'}]}
        previous = {'summary': 'Current-branch checkpoint finding.',
                    'citations': [{'path': 'entry.py', 'quote': 'current evidence'}]}
        final = {'answer': 'Final branch answer.', 'summary': 'Final summary.', 'remaining': [],
                 'citations': [{'path': 'entry.py', 'quote': 'final evidence'}]}
        delivered = {'branch': {'id': 'child', 'parent_id': 'parent'}, 'request': 'Explain the project.',
                     'source_paths': ['entry.py'], 'evidence_coverage': {},
                     'recovery_checkpoint': [recovery], 'child_checkpoint': [previous]}
        envelope = {'role': 'subagent', 'model': 'historical-model', 'delivery_id': 'delivery'}
        store = MagicMock()
        store.load_result.return_value = final

        def fetch(sql, parameters):
            value = envelope if 'FROM invocations' in sql else delivered
            return SimpleNamespace(fetchone=lambda: (canonical(value),))

        store.db.execute.side_effect = fetch
        parent = {'binding': {'session': 'task'}, 'session': 'task'}
        with patch('orchestration.node', return_value=parent), \
                patch('orchestration.binding', return_value=parent['binding']), \
                patch('orchestration.state', side_effect=lambda s,nid: 'DONE' if nid == 'child' else 'READY'), \
                patch('orchestration.events', return_value=[{'kind': 'JOIN', 'data': {'invocations': ['final']}}]):
            legacy = child.joined_results(store, 'task', 'parent', ['final'], delivered['request'])[0]
            current = child.joined_results(store, 'task', 'parent', ['final'], delivered['request'], note_version=2)[0]
        self.assertEqual(legacy['summary_unverified'], 'Current-branch checkpoint finding.\nFinal summary.')
        self.assertEqual(legacy['citations'], [*previous['citations'], *final['citations']])
        self.assertNotIn('prior_summaries_unverified', legacy)
        self.assertEqual(current['prior_summaries_unverified'], [recovery['summary'], previous['summary']])
        self.assertEqual(current['citations'], [*recovery['citations'], *previous['citations'], *final['citations']])
        self.assertEqual(child_note_view(current)['earlier_findings_unverified'], current['prior_summaries_unverified'])

    def test_compact_notes_preserve_prior_findings_but_do_not_repeat_final_summary_or_quotes(self):
        p = payload()
        original = copy.deepcopy(p)
        packet = build(chat.SYSTEM, p, chat.schema(p))
        body = json.loads(packet['messages'][1]['content'])
        for source, note in zip(p['child_results'], body['child_analysis_unverified']):
            self.assertEqual(note['answer_unverified'], source['answer_unverified'])
            self.assertEqual(note['earlier_findings_unverified'], source['prior_summaries_unverified'])
            self.assertEqual(note['citation_paths'], source['files'])
            self.assertEqual(note['remaining_unverified'], source['remaining_unverified'])
            self.assertEqual(note['evidence_coverage'], source['evidence_coverage'])
            self.assertNotIn('summary_unverified', note)
            self.assertNotIn('citations', note)
        self.assertEqual(body['request'], p['request'])
        self.assertEqual(body['task']['requirements'], p['contract']['constraints'])
        self.assertEqual(body['memory'], p['memory'])
        self.assertNotIn('parent_context_version', body)
        self.assertEqual(p, original)
        self.assertTrue(p['child_results'][0]['citations'][0]['quote'])
        self.assertEqual(p['child_results'][0]['invocation_id'], 'receipt-0')

    def test_missing_answer_or_unseparated_legacy_summary_keeps_the_only_findings(self):
        note = payload()['child_results'][0]
        for change in ({'answer_unverified': ''}, {'answer_unverified': None}):
            current = dict(note, **change)
            self.assertEqual(child_note_view(current)['summary_unverified'], note['summary_unverified'])
        legacy = dict(note)
        legacy.pop('prior_summaries_unverified')
        self.assertEqual(child_note_view(legacy)['summary_unverified'], note['summary_unverified'])
        distinct = dict(note, final_summary_unverified='A separate source finding absent from the final answer.')
        self.assertEqual(child_note_view(distinct)['summary_unverified'], distinct['final_summary_unverified'])
        self.assertEqual(child_note_view(distinct)['earlier_findings_unverified'], note['prior_summaries_unverified'])

    def test_historical_packet_projection_and_accounting_stay_unchanged(self):
        p = payload()
        p.pop('parent_context_version')
        fmt = chat.schema(p)
        expected = [legacy_child_view(note) for note in p['child_results']]
        before = Budget(32768, 8192, 479).charge(chat.SYSTEM, p, fmt)
        with patch('model_packet.child_note_view', side_effect=AssertionError('Legacy projection changed')):
            packet = build(chat.SYSTEM, p, fmt)
            self.assertEqual(Budget(32768, 8192, 479).charge(chat.SYSTEM, p, fmt), before)
        self.assertEqual(json.loads(packet['messages'][1]['content'])['child_analysis_unverified'], expected)
        self.assertEqual(packet['schema']['properties']['citations']['items']['type'], 'integer')

    def test_compaction_admits_more_fresh_source_under_the_same_packet_budget(self):
        graph = {'nodes': [], 'calls': [], 'diagnostics': [], 'module_dependencies': []}
        sources = {'module%s.py' % index:
                   'def entry():\n' + ''.join('    value_%s = %s\n' % (row, row) for row in range(240))
                   + '    return value_239\n' for index in range(6)}
        data = {'sources': sources, 'graph': graph}
        compact, verbose = payload(), payload()
        compact.pop('memory'); verbose.pop('memory')
        budget = Budget(32768, 8192, 479)
        provider = SimpleNamespace(budget=budget)
        with patch('model_packet.child_note_view', side_effect=legacy_child_view):
            chat.fit_packet(None, 'task', verbose, data, provider, real=False)
        chat.fit_packet(None, 'task', compact, data, provider, real=False)
        before = sum(len(value.encode()) for value in verbose['excerpts'].values())
        after = sum(len(value.encode()) for value in compact['excerpts'].values())
        self.assertGreaterEqual(after, before + 1500, (before, after))
        self.assertGreater(after, 3000)
        self.assertEqual(compact['contract'], verbose['contract'])
        self.assertEqual(compact['child_results'], verbose['child_results'])
        self.assertEqual(compact['memory_budget'], verbose['memory_budget'])
        self.assertLessEqual(budget.check(chat.SYSTEM, compact, chat.schema(compact))
                             + budget.response_reserve, budget.context_limit)


if __name__ == '__main__':
    unittest.main()
