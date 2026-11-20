"""Versioned source selection prioritizes mechanisms and complete delivered spans."""
import copy
import unittest

import chat_runtime as chat
import review_context as context
import review_followup
import subagent_runtime as child
from context_manager import InvalidUpdate, sha


def graph(nodes, counts=None):
    calls = [{'id': '%s-%s' % (name, i), 'source': name, 'path': 'flow.py', 'line': 1, 'target': None}
             for name, count in (counts or {}).items() for i in range(count)]
    return {'nodes': nodes, 'calls': calls, 'diagnostics': [], 'module_dependencies': []}


def packet(**changes):
    return dict({'contract': {'task_id': 'task', 'goal': 'Explain the architecture flow.', 'constraints': {}},
                 'request': 'Explain the architecture flow.', 'excerpt_bytes': 120,
                 'map_bytes': 4096, 'child_evidence_version': 3, 'child_checkpoint': []}, **changes)


class MechanismSourceTests(unittest.TestCase):
    def test_shared_rank_exposes_entry_bodies_over_empty_classes_lexical_helpers_and_callbacks(self):
        source = ('class FlowError(Exception): pass\n\ndef latest_chat_flow():\n    return lookup()\n\n'
                  'def run():\n    return _handle()\n\ndef _handle():\n    prepare()\n    return dispatch()\n')
        nodes = [{'id': name, 'name': name, 'path': 'flow.py', 'line': line, 'column': 0}
                 for name, line in [('FlowError', 1), ('latest_chat_flow', 3), ('run', 6), ('_handle', 9)]]
        nodes.append({'id': 'callback', 'name': '<anonymous@20>', 'path': 'flow.py', 'line': 20})
        g = graph(nodes, {'latest_chat_flow': 2, 'run': 5, '_handle': 20, 'callback': 100})
        self.assertEqual(context.ranked_declarations(g, 'flow.py', 'Explain architecture flow.')[0]['name'], '_handle')
        self.assertEqual(context.ranked_declarations(g, 'flow.py', 'Trace flow.run.', 'Inspect flow._handle.')[0]['name'], '_handle')
        self.assertEqual(context.ranked_declarations(g, 'flow.py', 'Trace flow.latest_chat_flow.')[0]['name'], 'latest_chat_flow')
        pages = chat.excerpts({'flow.py': source}, 'Explain architecture flow.', 500, g, recipe_version=3)
        self.assertTrue(pages['flow.py'].startswith('def _handle():'))
        inventory = context.overview({'sources': {'flow.py': source}, 'graph': g}, 4096, version=3)
        self.assertEqual(inventory['modules'][0]['declarations'][0], '_handle')
        self.assertEqual(review_followup._symbols(g, ['flow.py'], 'Explain architecture flow.', version=3)['flow.py'][0], ['_handle', 9])

    def test_full_span_cursor_advances_three_pages_without_citation_overlap(self):
        source = 'def _handle():\n' + ''.join('    stage_%s = dispatch(%s)\n' % (i, i) for i in range(30))
        g = graph([{'id': 'handle', 'name': '_handle', 'path': 'flow.py', 'line': 1, 'column': 0}], {'handle': 30})
        data, p, spans_seen = {'sources': {'flow.py': source}, 'graph': g}, packet(), []
        previous_end = 1
        for i in range(3):
            spans = {}
            page = child.slice_excerpts(p, data, source_spans=spans)['flow.py']
            span = spans['flow.py']
            self.assertEqual(span['start_line'], previous_end)
            self.assertEqual(span['sha256'], sha(page))
            self.assertTrue(page.endswith('\n'))
            self.assertNotIn(span, spans_seen)
            previous_end = span['next_line']; spans_seen.append(span)
            p['child_checkpoint'].append({'remaining': ['Continue flow._handle.'],
                # Deliberately repeat the first citation; paging must ignore it.
                'citations': [{'path': 'flow.py', 'quote': 'def _handle():'}], 'source_spans': spans})
        self.assertGreater(previous_end, 10)

    def test_explicit_earlier_symbol_revisits_then_continues_inside_its_span(self):
        source = ('def target():\n    first_effect()\n    second_effect()\n    return final_effect()\n\n'
                  'def later():\n    return unrelated_effect()\n')
        g = graph([{'id': 'target', 'name': 'target', 'path': 'flow.py', 'line': 1, 'column': 0},
                   {'id': 'later', 'name': 'later', 'path': 'flow.py', 'line': 6, 'column': 0}], {'later': 10})
        p = packet(excerpt_bytes=40, child_checkpoint=[{'remaining': ['Inspect flow.target.'], 'citations': [],
            'source_spans': {'flow.py': {'start_line': 6, 'next_line': 8, 'anchor_line': 6, 'sha256': 'old'}}}])
        data = {'sources': {'flow.py': source}, 'graph': g}
        spans = {}; first = child.slice_excerpts(p, data, source_spans=spans)['flow.py']
        self.assertTrue(first.startswith('def target():'))
        p['child_checkpoint'].append({'remaining': ['Continue flow.target.'], 'citations': [], 'source_spans': spans})
        following = child.slice_excerpts(p, data)['flow.py']
        self.assertNotIn('def target():', following)
        self.assertNotIn('unrelated_effect()', following)

    def test_v2_and_legacy_recipes_keep_their_original_declaration_choice(self):
        source = 'def latest_flow():\n    return lookup()\n\ndef _handle():\n    return dispatch()\n'
        g = graph([{'id': 'lookup', 'name': 'latest_flow', 'path': 'flow.py', 'line': 1},
                   {'id': 'handle', 'name': '_handle', 'path': 'flow.py', 'line': 4}], {'handle': 20})
        for version in (1, 2):
            p = packet(child_evidence_version=version, excerpt_bytes=100)
            self.assertTrue(child.slice_excerpts(p, {'sources': {'flow.py': source}, 'graph': g})['flow.py'].startswith('def latest_flow():'))

    def test_recorded_span_metadata_is_checked_and_does_not_mutate_deliveries(self):
        source = 'def _handle():\n    return dispatch()\n'
        data = {'sources': {'flow.py': source}, 'graph': graph([
            {'id': 'handle', 'name': '_handle', 'path': 'flow.py', 'line': 1}], {'handle': 1})}
        p = packet(); p['excerpts'] = child.slice_excerpts(p, data); context.evidence(p, data)
        self.assertEqual(p['excerpt_spans']['flow.py']['next_line'], 3)
        saved = copy.deepcopy(p); context.check(p, data); self.assertEqual(p, saved)
        p['excerpt_spans']['flow.py']['next_line'] += 1
        with self.assertRaisesRegex(InvalidUpdate, 'source spans'):
            context.check(p, data)

    def test_authorized_read_inside_a_body_preserves_the_exact_earned_line(self):
        source = 'def dispatch():\n    return engine_rule()\n\ndef engine_rule():\n    return 724\n'
        g = graph([{'id': 'dispatch', 'name': 'dispatch', 'path': 'flow.py', 'line': 1},
                   {'id': 'rule', 'name': 'engine_rule', 'path': 'flow.py', 'line': 4}], {'dispatch': 10})
        p = packet(parent_context_version=3)
        p.pop('child_evidence_version')
        data = {'sources': {'flow.py': source}, 'graph': g,
                'followups': {'flow.py': {'line': 5, 'text': '    return 724\n', 'line_complete': True}}}
        spans = {}; selected = chat.packet_excerpts(p, data, source_spans=spans)
        self.assertEqual(selected['flow.py'], '    return 724\n')
        self.assertEqual(spans['flow.py']['start_line'], 5)


if __name__ == '__main__':
    unittest.main()
