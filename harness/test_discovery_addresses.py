"""Versioned discovery addresses expose snapshot syntax without changing v1."""
import copy
import json
import unittest
from unittest.mock import patch

import architecture_role as role
import context_window
import model_packet
from context_manager import InvalidUpdate, canonical, sha
from paging import Budget
from invocation_gateway import InvocationGateway
from review_context import discovery_symbols, overview
from test_context_window_recovery import CountService
from test_discovery_selection import LEGACY_FIXTURE
import test_discovery_selection as selection_fixture


def snapshot():
    sources, nodes, calls = {}, [], []
    declarations = {
        'entry.js': [('activate', 24, 50),
                     *[('activate.helper_' + str(n), 50 + n, 1) for n in range(12)],
                     ('activate.request', 215, 30), ('activate.invoke', 955, 20)],
        'analysis.py': [('main', 6, 60), *[('helper_' + str(n), 30 + n, 1) for n in range(12)]],
        'tools.py': [('execute', 10, 40), *[('helper_' + str(n), 30 + n, 1) for n in range(12)]],
        'router.py': [('dispatch', 20, 35), *[('helper_' + str(n), 30 + n, 1) for n in range(12)]],
    }
    for path, values in declarations.items():
        lines = ['// snapshot source' if path.endswith('.js') else '# snapshot source'] * 1000
        for name, line, fanout in values:
            identity = path + ':' + name
            leaf = name.rsplit('.', 1)[-1]
            lines[line - 1] = ('function ' + leaf + '() {}' if path.endswith('.js') else 'def ' + leaf + '(): pass')
            nodes.append({'id': identity, 'path': path, 'name': name, 'line': line, 'column': 0})
            calls.extend({'source': identity, 'target': None, 'path': path} for _ in range(fanout))
        sources[path] = '\n'.join(lines) + '\n'
    # A graph entry outside the selected source snapshot grants no address.
    nodes.append({'id': 'outside', 'path': 'unread.js', 'name': 'install', 'line': 700, 'column': 0})
    return {'sources': sources, 'graph': {'nodes': nodes, 'calls': calls, 'diagnostics': [],
                                         'diagnostic_count': 0, 'module_dependencies': []}}


def selection_payload(version):
    value = copy.deepcopy(LEGACY_FIXTURE['payload'])
    value['discovery_selection_version'] = version
    return value


class DiscoveryAddressTests(unittest.TestCase):
    def test_exact_snapshot_addresses_include_relevant_declarations_beyond_first_six(self):
        data = snapshot()
        before = copy.deepcopy(data)
        addresses = discovery_symbols(data, 4096, 'Explain the architecture and tool request flow.')
        self.assertIn({'path': 'entry.js', 'name': 'activate.request', 'line': 215}, addresses)
        self.assertIn({'path': 'entry.js', 'name': 'activate.invoke', 'line': 955}, addresses)
        self.assertNotIn('unread.js', {row['path'] for row in addresses})
        for address in addresses:
            node = next(node for node in data['graph']['nodes']
                        if all(node[key] == address[key] for key in ('path', 'name', 'line')))
            self.assertIn(node['name'].rsplit('.', 1)[-1], data['sources'][node['path']].splitlines()[node['line'] - 1])
        self.assertEqual(data, before)
        full = discovery_symbols(data, 8192, 'Explain the architecture.')
        self.assertGreater(sum(row['path'] == 'entry.js' for row in full), 6)

    def test_address_budget_preserves_whole_entries_and_explicit_latest_symbol_priority(self):
        data = snapshot()
        large = discovery_symbols(data, 8192, 'Explain the architecture.')
        small = discovery_symbols(data, 512, 'Explain the architecture.')
        self.assertLessEqual(len(canonical(small).encode()), 512)
        self.assertEqual(small, large[:len(small)])
        focused = discovery_symbols(data, 512, 'Explain the architecture.',
                                    'Inspect `entry.js` and `helper_11`.')
        self.assertEqual(focused[0], {'path': 'entry.js', 'name': 'activate.helper_11', 'line': 61})
        for invalid in (511, 8193, True, 1024.0):
            with self.assertRaises(InvalidUpdate):
                discovery_symbols(data, invalid, 'Explain the architecture.')

    def test_profile_two_projects_only_addresses_and_keeps_three_response_fields(self):
        data = snapshot(); payload = selection_payload(2)
        payload['repository']['catalog'] = sorted(data['sources']) + ['unread.js']
        payload['repository']['total_files'] = len(payload['repository']['catalog'])
        payload['discovery_symbol_bytes'] = 4096
        payload['readable_symbols'] = discovery_symbols(data, 4096, payload['request'])
        payload['readable_symbols'][0]['receipt_id'] = 'host-only-receipt'
        schema = role.schema(payload); packet = model_packet.build(role.SYSTEM, payload, schema)
        body = json.loads(packet['messages'][1]['content'])
        self.assertEqual(set(packet['schema']['properties']), {'tool', 'summary', 'remaining'})
        self.assertTrue(body['readable_symbols'])
        self.assertTrue(all(set(row) == {'path', 'name', 'line'} for row in body['readable_symbols']))
        self.assertNotIn('discovery_symbol_bytes', body)
        self.assertIn('Continuation is optional.', packet['messages'][0]['content'])
        self.assertIn('does not require scanning the entire file', packet['messages'][0]['content'])
        self.assertIn('jump directly to a supplied symbol line', packet['messages'][0]['content'])
        raw = {'tool': {'kind': 'read', 'target': 'entry.js', 'query': '', 'line': 215},
               'summary': 'Inspect the request handler directly.', 'remaining': ['Trace the next caller/callee link.']}
        bound, fields = model_packet.bind_output('architect', payload, schema, raw)
        for field in ('findings', 'edges', 'completed'):
            self.assertEqual(fields[field], []); self.assertEqual(bound[field], [])
        self.assertFalse(bound['complete'])
        with self.assertRaises(InvalidUpdate):
            model_packet.bind_output('architect', payload, schema, dict(raw, readable_symbols=[]))

    def test_profile_one_packet_schema_charge_and_offline_physical_proof_are_unchanged(self):
        payload = selection_payload(1); schema = role.schema(payload)
        packet = model_packet.build(role.SYSTEM, payload, schema)
        self.assertEqual(sha(canonical(schema)), '5be92a179bbd7a089bda95bf8e6d2ba66c0ec8f487751ae3d5bf3ec626fe3212')
        self.assertEqual(sha(canonical(packet)), '47a379c6909bd0e422860d50e66c4f921bef3690e04a26a96594ea6456a292f3')
        self.assertEqual(model_packet.descriptor(packet, 256)['input_reservation'], 3164)
        self.assertEqual(Budget(32768, 1024, 256).charge(role.SYSTEM, payload, schema), 3164)
        unused = dict(payload, discovery_symbol_bytes=4096,
                      readable_symbols=[{'path': 'unread.js', 'name': 'host_only', 'line': 77}])
        self.assertEqual(model_packet.build(role.SYSTEM, unused, role.schema(unused)), packet)
        counter = CountService(); profile = counter.descriptor()
        budget = Budget(4096, 512, 479, context_profile=2, reservation_limit=20000, context_window=profile)
        context_window._counts.clear()
        with patch.object(context_window, '_rpc', counter.rpc):
            proof = context_window.check(budget, role.SYSTEM, payload, schema, thinking=False)
        identity = {'server_model': profile['model'], 'server_started': profile['started'],
                    'server_context_tokens': profile['context_tokens']}
        with patch.object(context_window, '_rpc', side_effect=AssertionError('Replay must remain offline')):
            context_window.validate_proof(proof, budget, role.SYSTEM, unused, schema, identity)

    def test_profile_two_retains_midline_suffix_positions_without_inventing_end_markers(self):
        payload = selection_payload(2)
        text = 'tail' * 60
        payload['evidence'] = [
            {'id': 'suffix', 'path': 'a.py', 'line': 7, 'column': 2000, 'text': text,
             'end_position': {'line': 7, 'column': 2000 + len(text)}, 'line_complete': False},
            {'id': 'plain', 'path': 'b.py', 'line': 1, 'text': 'body'},
            {'id': 'eof', 'path': 'a.py', 'line': 8, 'column': 5, 'text': 'last',
             'end_position': None, 'line_complete': True},
        ]
        packet = model_packet.build(role.SYSTEM, payload, role.schema(payload))
        body = json.loads(packet['messages'][1]['content'])
        suffix, plain, eof = body['evidence']
        self.assertEqual(suffix, payload['evidence'][0])
        self.assertEqual(plain['column'], 0)
        self.assertNotIn('end_position', plain); self.assertNotIn('line_complete', plain)
        self.assertEqual(eof, payload['evidence'][2])
        self.assertIn('zero-based source character offset', packet['messages'][0]['content'])
        old = dict(payload, discovery_selection_version=1)
        old_packet = model_packet.build(role.SYSTEM, old, role.schema(old))
        old_body = json.loads(old_packet['messages'][1]['content'])
        self.assertTrue(all(not set(row).intersection({'column', 'end_position', 'line_complete'})
                            for row in old_body['evidence']))
        clean = copy.deepcopy(old)
        for row in clean['evidence']:
            for key in ('column', 'end_position', 'line_complete'):
                row.pop(key, None)
        self.assertEqual(old_packet, model_packet.build(role.SYSTEM, clean, role.schema(clean)))

    def test_existing_overview_three_still_contains_names_without_new_address_fields(self):
        data = snapshot(); data['graph']['nodes'] = [node for node in data['graph']['nodes'] if node['path'] in data['sources']]
        result = overview(data, 8192, 'Explain the architecture.', version=3)
        self.assertTrue(all(isinstance(name, str) for module in result['modules'] for name in module['declarations']))
        self.assertFalse(any('readable_symbols' in module or 'symbols' in module for module in result['modules']))


class DiscoveryAddressBindingTests(unittest.TestCase):
    setUp = selection_fixture.SelectionWireTests.setUp
    tearDown = selection_fixture.SelectionWireTests.tearDown
    step = selection_fixture.SelectionWireTests.step
    until = selection_fixture.SelectionWireTests.until
    provider = selection_fixture.SelectionWireTests.provider
    discover = selection_fixture.SelectionWireTests.discover

    def test_real_discovery_request_rejects_changed_or_unsnapshotted_symbol_addresses(self):
        invoke = InvocationGateway.invoke
        checked = []
        def inspect(gateway, name, call_id, system, payload, schema, delivery):
            if name == 'architect':
                self.assertEqual(payload['discovery_selection_version'], 3)
                self.assertTrue(payload['readable_symbols'])
                role.request_check(self.s, self.meta['active'], payload)
                forged = copy.deepcopy(payload)
                forged['readable_symbols'][0]['line'] += 1
                with self.assertRaisesRegex(InvalidUpdate, 'symbol'):
                    role.request_check(self.s, self.meta['active'], forged)
                forged = copy.deepcopy(payload)
                forged['readable_symbols'].append({'path': 'unread.js', 'name': 'install', 'line': 700})
                with self.assertRaisesRegex(InvalidUpdate, 'symbol'):
                    role.request_check(self.s, self.meta['active'], forged)
                checked.append(call_id)
            return invoke(gateway, name, call_id, system, payload, schema, delivery)
        with patch.object(InvocationGateway, 'invoke', inspect):
            _, packet = self.discover()
        self.assertEqual(len(checked), 3)
        self.assertEqual(packet['discovery_coverage']['stop_reason'], 'evidence_complete')
        self.assertTrue(self.s.verify_invocations(self.meta['active']))


if __name__ == '__main__':
    unittest.main()
