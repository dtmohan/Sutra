"""Bounded lookup metadata leaves room for earned source in selection v3."""
import copy
import json
import unittest
from unittest.mock import patch

import architecture_role as role
import context_window
import model_packet
from context_manager import ContextBudgetExceeded, InvalidUpdate, canonical, sha
from paging import Budget
from review_context import discovery_catalog
from review_role import compact
from test_context_window_recovery import CountService
from test_discovery_selection import LEGACY_FIXTURE


def payload(version):
    value = copy.deepcopy(LEGACY_FIXTURE['payload'])
    value.update(discovery_selection_version=version, discovery_symbol_bytes=512,
                 readable_symbols=[{'path': 'a.py', 'name': 'start', 'line': 1}])
    return value


def catalog_fixture():
    catalog = ['entry.js', 'native_provider.js', 'pkg/worker.py', 'pkg/receipt.py'] + [
        'harness/component_%03d_%s.py' % (number, 'lookup_metadata' * 2) for number in range(387)]
    data = {'sources': {'entry.js': "const native = require('./native_provider');\nfunction activate() {}\n",
                        'pkg/worker.py': 'from . import receipt\ndef execute(): pass\n'},
            'graph': {'nodes': [{'id': 'entry', 'name': 'activate', 'path': 'entry.js', 'line': 2},
                                {'id': 'worker', 'name': 'execute', 'path': 'pkg/worker.py', 'line': 2}],
                      'calls': [], 'diagnostics': [],
                      'module_dependencies': [{'source': 'entry.js', 'specifier': './native_provider', 'target': None},
                                              {'source': 'entry.js', 'specifier': '../private', 'target': 'private.js'}]}}
    return data, catalog


class DiscoveryCatalogViewTests(unittest.TestCase):
    def test_catalog_window_uses_authorized_hints_snapshot_and_import_candidates(self):
        data, catalog = catalog_fixture(); old_data, old_catalog = copy.deepcopy(data), list(catalog)
        window = discovery_catalog(data, catalog, 512, 'Explain the architecture.',
                                   'Inspect `native_provider.js` next.')
        self.assertEqual(window[0], 'native_provider.js')
        self.assertIn('entry.js', window)
        self.assertIn('pkg/worker.py', window)
        self.assertIn('pkg/receipt.py', window)
        self.assertNotIn('private.js', window)
        self.assertLess(len(window), len(catalog))
        self.assertLessEqual(len(canonical(window).encode()), 512)
        self.assertTrue(set(window) <= set(catalog))
        self.assertEqual(data, old_data); self.assertEqual(catalog, old_catalog)
        for invalid in (511, 4097, True, 1024.0):
            with self.assertRaises(InvalidUpdate):
                discovery_catalog(data, catalog, invalid, 'Explain the architecture.')

    def test_391_path_catalog_and_host_manifest_do_not_evict_eight_kib_source(self):
        data, catalog = catalog_fixture(); source = payload(3)
        self.assertEqual(len(catalog), 391)
        source['repository'] = {'catalog': catalog, 'total_files': len(catalog),
            'budget': {'decision': 'ALLOW'}, 'operations_remaining': 100,
            'last_tool': {'kind': 'static_map', 'target': '\n'.join(catalog), 'query': '',
                          'status': 'ok', 'matches_or_excerpts': 0, 'truncated': False}}
        source['catalog_window'] = discovery_catalog(data, catalog, 512, source['request'])
        source['discovery_catalog_bytes'] = 512
        source['evidence'] = [{'id': 'e' + str(index), 'path': 'entry.js', 'line': index + 1, 'column': 0,
            'text': 'x' * 1023 + '\n', 'end_position': {'line': index + 2, 'column': 0}, 'line_complete': True}
            for index in range(8)]
        source['active_source_page'] = {'path': 'entry.js', 'start_line': 1, 'column': 0,
            'end_position': {'line': 9, 'column': 0}, 'partial_page': False, 'next_fresh_position': None,
            'receipt': 'ledger-only-receipt', 'unit_ids': ['e' + str(index) for index in range(8)],
            'delivered_prefix_ids': [], 'total_units': 8, 'budget': {'host_only': True}}
        source['source_page_toc'] = [{'path': 'pkg/worker.py', 'start_line': 10, 'column': 4,
                                      'receipt': 'ledger-only-other', 'unread_units': 200}]
        before = copy.deepcopy(source)
        packet = model_packet.build(role.SYSTEM, source, role.schema(source))
        body = json.loads(packet['messages'][1]['content'])
        self.assertNotIn('catalog', body['repository'])
        self.assertEqual(body['repository']['catalog_window'], source['catalog_window'])
        self.assertEqual(body['repository']['total_files'], 391)
        self.assertTrue(body['repository']['catalog_window_partial'])
        self.assertEqual(body['repository']['last_tool']['target_file_count'], 391)
        self.assertNotIn('target', body['repository']['last_tool'])
        self.assertLess(len(canonical(body['repository']).encode()), 1000)
        self.assertEqual(sum(len(unit['text'].encode()) for unit in body['evidence']), 8192)
        self.assertEqual(set(body['source_page_view']),
                         {'path', 'start_line', 'column', 'end_position', 'partial_page', 'next_fresh_position'})
        self.assertIsNone(body['source_page_view']['next_fresh_position'])
        self.assertEqual(body['source_page_toc'], [{'path': 'pkg/worker.py', 'start_line': 10, 'column': 4}])
        self.assertNotIn('ledger-only', canonical(body))
        self.assertEqual(source, before)
        budget = Budget(32768, 2048, 256)
        budget.check(role.SYSTEM, source, role.schema(source))
        old = dict(source, discovery_selection_version=2)
        with self.assertRaises(ContextBudgetExceeded):
            budget.check(role.SYSTEM, old, role.schema(old))

    def test_actual_model_selected_feedback_and_cursor_are_preserved(self):
        source = payload(3); source['catalog_window'] = ['a.py']
        cursor = {'line': 22, 'column': 300, 'sha256': 'a' * 64}
        for kind in ('read', 'search', 'list'):
            source['repository']['last_tool'] = {'kind': kind, 'target': 'a.py' if kind == 'read' else '',
                'query': 'operation' if kind == 'search' else '', 'status': 'ok', 'matches_or_excerpts': 1,
                'truncated': True, 'next_cursor': cursor, 'files': ['b.py']}
            body = json.loads(model_packet.build(role.SYSTEM, source, role.schema(source))['messages'][1]['content'])
            self.assertEqual(body['repository']['last_tool'], source['repository']['last_tool'])

    def test_v3_schema_bounds_match_protocol_and_worst_case_compact_note_bytes(self):
        source = payload(3); source['catalog_window'] = ['a.py']
        schema = role.schema(source); packet = model_packet.build(role.SYSTEM, source, schema)
        fields = packet['schema']['properties']
        self.assertEqual(set(fields), {'tool', 'summary', 'remaining'})
        self.assertEqual(fields['summary']['maxLength'], 64)
        self.assertEqual(fields['remaining']['maxItems'], 1)
        self.assertEqual(fields['remaining']['items']['maxLength'], 59)
        fixed = len(canonical(compact({'summary': '', 'remaining': [''], 'findings': [], 'completed': []})).encode())
        self.assertEqual(fixed, model_packet.DISCOVERY_NOTE_OVERHEAD)
        for character in ('😀', '漢', '\x00', '\n', '"', '\\'):
            raw = {'summary': character * 64, 'remaining': [character * 59],
                   'tool': {'kind': 'read', 'target': 'a.py', 'query': '', 'line': 1}}
            bound, _ = model_packet.bind_output('architect', source, schema, raw)
            self.assertLessEqual(len(canonical(compact(bound)).encode()), 1600)
        raw = {'summary': 'Inspect the next caller.', 'remaining': ['Trace its callee.'],
               'tool': {'kind': 'read', 'target': 'b.py', 'query': '', 'line': 1}}
        # The bounded lookup window is not a replacement for host scope checks.
        model_packet.bind_output('architect', source, schema, raw)
        for key, bad in (('target', 'x' * 513), ('query', 'x' * 81), ('line', 1000001)):
            forged = copy.deepcopy(raw); forged['tool'][key] = bad
            with self.assertRaises(InvalidUpdate):
                model_packet.bind_output('architect', source, schema, forged)
        for digest in ('a' * 63,):
            forged = copy.deepcopy(raw); forged['tool']['cursor'] = {'line': 1, 'column': 0, 'sha256': digest}
            with self.assertRaises(InvalidUpdate):
                model_packet.bind_output('architect', source, schema, forged)
        for field, bad in (('summary', 'x' * 65), ('remaining', ['a', 'b'])):
            forged = copy.deepcopy(raw); forged[field] = bad
            with self.assertRaises(InvalidUpdate):
                model_packet.bind_output('architect', source, schema, forged)

    def test_v3_cursor_passes_shared_shape_and_role_acceptance_with_repository_sha_check(self):
        import repository_tools as repo
        from invocation_roles import validate_shape
        source = payload(3); source['catalog_window'] = ['a.py']
        schema = role.schema(source); packet = model_packet.build(role.SYSTEM, source, schema)
        cursor = {'line': 2, 'column': 0, 'sha256': 'a' * 64}
        raw = {'summary': 'Continue the earned source page.', 'remaining': [],
               'tool': {'kind': 'read', 'target': 'a.py', 'query': '', 'line': 2, 'cursor': cursor}}
        self.assertNotIn('pattern', packet['schema']['properties']['tool']['properties']['cursor']['properties']['sha256'])
        validate_shape(raw, packet['schema'])
        bound, _ = model_packet.bind_output('architect', source, schema, raw)
        previous = {'operation': {'kind': 'read', 'target': 'a.py'},
                    'result': {'status': 'ok', 'next_cursor': cursor}}
        with patch.object(role, 'request_check'), \
                patch.object(repo, 'settings', return_value={'auto_review': True, 'allowed_paths': ['.']}), \
                patch.object(repo, 'catalog_snapshot', return_value={'files': ['a.py'], 'excluded': {}}), \
                patch.object(repo, 'current_records', return_value=[previous]), \
                patch.object(repo, 'catalog', return_value=['a.py']):
            self.assertTrue(role.validate(None, 'fixture', source, bound)['admission']['admitted'])
            for digest in ('A' * 64, 'g' * 64):
                forged = copy.deepcopy(raw); forged['tool']['cursor']['sha256'] = digest
                # String shape remains compatible; the repository keeps its
                # exact lowercase hexadecimal continuation validation.
                validate_shape(forged, packet['schema'])
                rejected, _ = model_packet.bind_output('architect', source, schema, forged)
                with self.assertRaisesRegex(InvalidUpdate, 'Invalid read continuation version'):
                    role.validate(None, 'fixture', source, rejected)

    def test_mlx_projection_retains_explicit_note_instructions_when_string_bounds_are_removed(self):
        from decoder_schema import for_decoder, profile
        source = payload(3); source['catalog_window'] = ['a.py']
        packet = model_packet.build(role.SYSTEM, source, role.schema(source))
        compatibility = profile({'details': {'runner': 'mlx'}}, '0.40.16')
        decoder = for_decoder(packet['schema'], compatibility)
        fields = decoder['properties']
        self.assertNotIn('maxLength', fields['summary'])
        self.assertNotIn('maxLength', fields['remaining']['items'])
        self.assertEqual(fields['remaining']['maxItems'], 1)
        system = packet['messages'][0]['content']
        self.assertIn('Summary is an unverified working note of at most 64 characters, including spaces.', system)
        self.assertIn('remaining must be [] or one unresolved gap of at most 59 characters, including spaces.', system)
        self.assertEqual(packet['schema']['properties']['summary']['maxLength'], 64)
        self.assertEqual(packet['schema']['properties']['remaining']['items']['maxLength'], 59)

    def test_profile_two_packet_schema_charge_and_proof_remain_exact_after_v3(self):
        source = payload(2); schema = role.schema(source)
        packet = model_packet.build(role.SYSTEM, source, schema)
        self.assertEqual(sha(canonical(schema)), '5be92a179bbd7a089bda95bf8e6d2ba66c0ec8f487751ae3d5bf3ec626fe3212')
        self.assertEqual(sha(canonical(packet)), '567c94a461cee25107cb68afd8cec5dc54f8da54c340f6d10e9f3d12d1974c97')
        self.assertEqual(model_packet.descriptor(packet, 256)['input_reservation'], 3985)
        # Constructing a bounded v3 schema must not mutate shared legacy strings.
        modern = payload(3); modern['catalog_window'] = ['a.py']
        model_packet.build(role.SYSTEM, modern, role.schema(modern))
        self.assertEqual(packet, model_packet.build(role.SYSTEM, source, role.schema(source)))
        ignored = dict(source, catalog_window=['a.py'], discovery_catalog_bytes=512,
                       active_source_page={'path': 'a.py', 'receipt': 'hidden'})
        self.assertEqual(packet, model_packet.build(role.SYSTEM, ignored, role.schema(ignored)))
        counter = CountService(); profile = counter.descriptor()
        budget = Budget(8192, 512, 479, context_profile=2, reservation_limit=20000, context_window=profile)
        context_window._counts.clear()
        with patch.object(context_window, '_rpc', counter.rpc):
            proof = context_window.check(budget, role.SYSTEM, source, schema, thinking=False)
        identity = {'server_model': profile['model'], 'server_started': profile['started'],
                    'server_context_tokens': profile['context_tokens']}
        with patch.object(context_window, '_rpc', side_effect=AssertionError('Replay remains offline')):
            context_window.validate_proof(proof, budget, role.SYSTEM, ignored, schema, identity)
            with self.assertRaises(InvalidUpdate):
                context_window.validate_proof(proof, budget, role.SYSTEM, modern, role.schema(modern), identity)


if __name__ == '__main__':
    unittest.main()
