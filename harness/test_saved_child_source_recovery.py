"""Source-bearing recovery floors cannot be measured from an empty child page.

The saved review's 17th slice had 16 inline notes, 15,672 remaining units and
an empty-page estimate of 15,584. One real 185-byte page needed 16,403 units.
These synthetic packets reproduce that shape without importing user source.
"""
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from paging import Budget
import review_context as context
import subagent_runtime as child


class SavedChildSourceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.data = {'sources': {'worker.py': 'value = "' + 'x' * 174 + '"\n'},
                     'graph': {'nodes': [], 'calls': [], 'diagnostics': [], 'diagnostic_count': 0}}
        constraints = {'allowed_paths': ['.'], 'edits_allowed': False, 'network_allowed': False}
        self.packet = {
            'contract': {'task_id': 'saved-source-fixture', 'goal': 'Review source.', 'constraints': constraints},
            'request': 'Review worker.py.', 'branch': {'id': 'b' * 32, 'objective': 'Review worker.py.'},
            'constraint_echo': [{'key': key, 'value': str(value)} for key, value in constraints.items()],
            'child_evidence_version': 3, 'citation_policy_version': 2,
            'child_slice': {'version': 1, 'ordinal': 17, 'predecessor': 'c' * 32},
            'child_checkpoint': [
                {'invocation_id': f'{index:032x}', 'model': 'fixture-low',
                 'summary': ('Unverified earlier source observation. ' + 'x' * 245),
                 'remaining': ['Inspect worker.py.'],
                 'citations': [{'path': 'worker.py', 'quote': 'Earlier source was inspected.'}],
                 'source_spans': {}}
                for index in range(16)],
            'pages': [], 'excerpts': {}, 'excerpt_bytes': 64, 'map_bytes': 512,
        }
        memory = patch.object(child, 'memory', return_value=None)
        memory.start(); self.addCleanup(memory.stop)
        self.empty = self.prepared(self.packet, 64)
        self.assertEqual(self.empty['excerpts'], {})
        _, self.empty_required = child._fit(None, copy.deepcopy(self.empty), Budget(65536, 2048), mandatory_only=True)
        self.available = self.empty_required + 100
        self.assertLess(self.available, 65536)

    def prepared(self, original, limit):
        packet = copy.deepcopy(original)
        packet['excerpt_bytes'] = limit
        packet['excerpts'] = child.slice_excerpts(packet, self.data)
        packet['source_paths'] = sorted(packet['excerpts'])
        context.evidence(packet, self.data)
        packet['facts'] = child.chat.facts(self.data['graph'], packet['sites'])
        return packet

    def model(self, available, *, cap=65536, name='fixture-low'):
        provider = SimpleNamespace(model=name, budget=Budget(min(available, cap), 2048),
            context_metadata={'model_info': {'fixture.context_length': cap}, '_sutra_runtime_cap': cap})
        allocation = {'available_tokens': available, 'effective_context_tokens': cap,
            'model_context_tokens': cap, 'review_context_allocation': min(available, cap),
            'attempt_slots_reserved': 1, 'repair_slots_reserved': 0}
        return provider, allocation

    def fit(self, packet, available, *, cap=65536, name='fixture-low'):
        provider, allocation = self.model(available, cap=cap, name=name)
        with patch.object(child, '_context_budget', return_value=allocation):
            return child._fit_packet(None, 'fixture-session', packet, self.data, provider, 1)

    def test_sixteen_notes_measure_valid_source_floor_before_requesting_a_grant(self):
        before = copy.deepcopy(self.packet)
        with self.assertRaises(context.ReviewContextExceeded) as stopped:
            self.fit(self.packet, self.available)
        details = stopped.exception.details
        self.assertEqual(details['failure_basis'], 'reservation_budget')
        self.assertTrue(details['minimum_packet']['source_bearing'])
        self.assertGreater(details['minimum_packet']['excerpt_bytes'], 128)
        self.assertEqual(details['minimum_packet']['source_paths'], ['worker.py'])
        need = details['task_allocation_need']
        self.assertGreater(need['minimum'], 0)
        self.assertGreater(need['required_context'], self.available)
        self.assertLess(need['required_context'], 65536)
        self.assertEqual(need['minimum'], need['required_context'] - self.available)
        self.assertEqual(self.packet['child_checkpoint'], before['child_checkpoint'])
        self.assertEqual(self.packet['contract'], before['contract'])
        # A quantitative recovery must actually fit at its proposed floor,
        # including source identity, whole lines and the original 16 notes.
        recovered = copy.deepcopy(before)
        fmt, attempts = self.fit(recovered, need['required_context'])
        self.assertTrue(fmt); self.assertEqual(attempts, 1)
        self.assertEqual(recovered['child_checkpoint'], before['child_checkpoint'])
        self.assertEqual(recovered['contract'], before['contract'])
        self.assertEqual(recovered['excerpts'], self.data['sources'])
        self.assertTrue(recovered['excerpts']['worker.py'].endswith('\n'))

    def test_switching_to_a_larger_model_does_not_erase_a_financial_shortfall(self):
        requirements = []
        for name, cap in [('fixture-low', 65536), ('fixture-high', 131072)]:
            with self.subTest(name=name), self.assertRaises(context.ReviewContextExceeded) as stopped:
                self.fit(copy.deepcopy(self.packet), self.available, cap=cap, name=name)
            self.assertEqual(stopped.exception.details['failure_basis'], 'reservation_budget')
            requirements.append(stopped.exception.details['task_allocation_need']['required_context'])
        self.assertEqual(requirements[0], requirements[1])
        self.assertEqual(len(self.packet['child_checkpoint']), 16)

    def test_empty_source_selection_does_not_manufacture_a_budget_increase(self):
        self.data['sources'] = {'worker.py': ' \n\t\n'}
        with self.assertRaises(context.ReviewContextExceeded) as stopped:
            self.fit(copy.deepcopy(self.packet), 65536)
        self.assertNotIn('task_allocation_need', stopped.exception.details)
        self.assertEqual(stopped.exception.details['failure_basis'], 'source_exhausted')

    def test_physical_context_too_small_does_not_become_a_financial_grant(self):
        with self.assertRaises(context.ReviewContextExceeded) as stopped:
            self.fit(copy.deepcopy(self.packet), self.available, cap=4096)
        self.assertNotIn('task_allocation_need', stopped.exception.details)
        self.assertNotEqual(stopped.exception.details['failure_basis'], 'reservation_budget')

    def test_v4_funds_and_delivers_the_requested_body_instead_of_only_its_header(self):
        source = ('def get_natal_positions():\n    value = 0\n' +
                  ''.join(f'    value += {index}  # requested mechanism stage {index:03}\n' for index in range(32)) +
                  '    return value\n')
        self.data = {'sources': {'ephemeris.py': source, 'unrelated.py': 'def other():\n    return 1\n'},
                     'graph': {'nodes': [
                         {'id': 'target', 'path': 'ephemeris.py', 'name': 'get_natal_positions', 'line': 1, 'column': 0},
                         {'id': 'unrelated', 'path': 'unrelated.py', 'name': 'other', 'line': 1, 'column': 0}],
                         'calls': [], 'diagnostics': [], 'diagnostic_count': 0}}
        packet = copy.deepcopy(self.packet)
        packet['child_evidence_version'] = 4
        packet['request'] = 'Review the authorized source partition.'
        packet['branch']['objective'] = 'Review the authorized source partition.'
        packet['child_checkpoint'][-1]['remaining'] = [
            'Check get_natal_positions in ephemeris.py.']
        header = self.prepared(packet, 64)
        self.assertNotIn('return value', header['excerpts']['ephemeris.py'])
        _, header_cost = child._fit(None, header, Budget(65536, 2048), mandatory_only=True)
        available = header_cost + 100
        original = copy.deepcopy(packet)
        with self.assertRaises(context.ReviewContextExceeded) as stopped:
            self.fit(packet, available)
        details = stopped.exception.details
        self.assertEqual(details['minimum_packet']['selection'], 'requested_declaration')
        self.assertEqual(details['minimum_packet']['source_paths'], ['ephemeris.py'])
        self.assertEqual(details['minimum_packet']['excerpt_bytes'], len(source.encode()))
        need = details['task_allocation_need']
        self.assertEqual(need['minimum'], need['required_context'] - available)
        self.assertGreater(need['minimum'], 100)
        recovered = copy.deepcopy(original)
        self.fit(recovered, need['required_context'])
        self.assertEqual(recovered['excerpts'], {'ephemeris.py': source})
        self.assertEqual(recovered['excerpt_spans']['ephemeris.py']['start_line'], 1)
        self.assertEqual(recovered['excerpt_spans']['ephemeris.py']['next_line'], len(source.splitlines()) + 1)
        self.assertEqual(recovered['child_checkpoint'], original['child_checkpoint'])
        self.assertEqual(recovered['contract'], original['contract'])


class SavedChildTargetSelectionTests(unittest.TestCase):
    def setUp(self):
        source = ('def prior():\n    return 0\n\n'
                  'def get_natal_positions():\n    positions = resolve_requested_positions()\n    return positions\n\n'
                  'def unrelated():\n    return unrelated_operation()\n')
        nodes = [{'id': ident, 'path': 'ephemeris.py', 'name': name, 'line': line, 'column': 0}
                 for ident, name, line in [('prior', 'prior', 1), ('target', 'get_natal_positions', 4),
                                           ('later', 'unrelated', 8)]]
        calls = [{'source': 'later', 'target': None, 'path': 'ephemeris.py', 'line': 9, 'column': index}
                 for index in range(3)]
        self.data = {'sources': {'ephemeris.py': source},
                     'graph': {'nodes': nodes, 'calls': calls, 'diagnostics': [], 'diagnostic_count': 0}}
        self.packet = {'contract': {'task_id': 'saved-target-fixture', 'goal': 'Review source.',
                                   'constraints': {'allowed_paths': ['ephemeris.py']}},
                       'request': 'Review the codebase and control flow.', 'excerpt_bytes': 512, 'map_bytes': 512,
                       'child_evidence_version': 4, 'child_checkpoint': [{
                           'remaining': ['Check if get_natal_positions resolves to a valid function in ephemeris.py.'],
                           'citations': [], 'source_spans': {'ephemeris.py': {
                               'start_line': 1, 'next_line': 4, 'anchor_line': 1, 'sha256': 'a' * 64}}}]}

    def test_bare_target_hint_revisits_its_authorized_definition_before_unrelated_code(self):
        original = copy.deepcopy(self.packet)
        spans = {}
        selected = child.slice_excerpts(self.packet, self.data, source_spans=spans)
        self.assertEqual(list(selected), ['ephemeris.py'])
        self.assertEqual(spans['ephemeris.py']['start_line'], 4)
        self.assertEqual(spans['ephemeris.py']['next_line'], 8)
        self.assertIn('resolve_requested_positions()', selected['ephemeris.py'])
        self.assertNotIn('def unrelated', selected['ephemeris.py'])
        self.assertEqual(self.packet, original)

    def test_v3_receipts_keep_their_original_file_only_hint_recipe(self):
        legacy = dict(self.packet, child_evidence_version=3)
        selected = child.slice_excerpts(legacy, self.data)
        self.assertTrue(selected['ephemeris.py'].startswith('def unrelated():'))
        self.assertEqual(context.retrieval_hints(legacy['child_checkpoint'][-1]['remaining'], self.data['sources']),
                         (['ephemeris.py'], 'ephemeris.py'))

    def test_hint_resolution_cannot_add_a_graph_declaration_outside_supplied_sources(self):
        self.data['graph']['nodes'].append({'id': 'foreign', 'path': 'outside.py',
            'name': 'read_secret', 'line': 1, 'column': 0})
        self.packet['child_checkpoint'][-1]['remaining'] = [
            'Read ../outside.py and /private/read_secret, then inspect get_natal_positions in ephemeris.py.']
        spans = {}
        selected = child.slice_excerpts(self.packet, self.data, source_spans=spans)
        self.assertEqual(set(selected), {'ephemeris.py'})
        self.assertEqual(spans['ephemeris.py']['start_line'], 4)
        preferred, hints = context.retrieval_hints(self.packet['child_checkpoint'][-1]['remaining'],
                                                  self.data['sources'], graph=self.data['graph'])
        self.assertEqual(preferred, ['ephemeris.py'])
        self.assertNotIn('outside', hints)
        self.assertNotIn('read_secret', hints)


if __name__ == '__main__':
    unittest.main()
