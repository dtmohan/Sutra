"""New discovery notes use the existing compact byte allowance, not character slices."""
import copy
import json
import unittest
from unittest.mock import patch

import architecture_role as role
import context_window
import model_packet
import repository_tools as repo
import test_discovery_selection as fixture
from context_manager import InvalidUpdate, canonical, sha
from decoder_schema import for_decoder, profile
from paging import Budget
from review_role import compact
from test_context_window_recovery import CountService


def payload(note_version=None):
    value = copy.deepcopy(fixture.LEGACY_FIXTURE['payload'])
    value.update(discovery_selection_version=3, discovery_symbol_bytes=512,
                 readable_symbols=[{'path': 'a.py', 'name': 'start', 'line': 1}],
                 catalog_window=['a.py'])
    if note_version is not None:
        value['discovery_note_version'] = note_version
    return value


def reply(summary='Read the next caller.', remaining=None):
    return {'summary': summary, 'remaining': [] if remaining is None else remaining,
            'tool': {'kind': 'read', 'target': 'a.py', 'query': '', 'line': 1}}


class DiscoveryNoteBudgetTests(unittest.TestCase):
    def test_new_note_profile_keeps_tool_bounds_and_soft_guidance_through_mlx_projection(self):
        source = payload(2); schema = role.schema(source)
        packet = model_packet.build(role.SYSTEM, source, schema)
        fields = packet['schema']['properties']
        self.assertNotIn('maxLength', fields['summary'])
        self.assertNotIn('maxItems', fields['remaining'])
        self.assertNotIn('maxLength', fields['remaining']['items'])
        model_packet.bind_output('architect', source, schema, reply('s' * 87, ['g' * 90, 'h' * 75]))
        decoder = for_decoder(packet['schema'], profile({'details': {'runner': 'mlx'}}, '0.40.16'))
        self.assertNotIn('maxLength', decoder['properties']['summary'])
        system = packet['messages'][0]['content']
        self.assertIn('prefer 64 characters or fewer', system)
        self.assertNotIn('at most 64 characters', system)
        self.assertNotIn('1600', system)
        self.assertNotIn('byte', system)
        for key, value in (('target', 'x' * 513), ('query', 'x' * 81), ('line', 1000001)):
            raw = reply(); raw['tool'][key] = value
            with self.assertRaises(InvalidUpdate):
                model_packet.bind_output('architect', source, schema, raw)
        raw = reply(); raw['tool']['cursor'] = {'line': 1, 'column': 0, 'sha256': 'a' * 63}
        with self.assertRaises(InvalidUpdate):
            model_packet.bind_output('architect', source, schema, raw)
        raw = reply(); raw['findings'] = []
        with self.assertRaises(InvalidUpdate):
            model_packet.bind_output('architect', source, schema, raw)

    def test_released_v3_without_note_flag_keeps_exact_schema_packet_charge_and_offline_proof(self):
        source = payload(); schema = role.schema(source)
        packet = model_packet.build(role.SYSTEM, source, schema)
        self.assertEqual(sha(canonical(packet['schema'])),
                         '045bf8e52bd5948b8636ff337db34bf022207ee702bd64de660bbd583c8fd877')
        self.assertEqual(sha(canonical(packet)),
                         '68098f2eda5d2c309c2a9216a41156a1bbcc68e076f49b61e425aba24b3b7e40')
        self.assertEqual(model_packet.descriptor(packet, 256)['input_reservation'], 5025)
        for raw in (reply('s' * 87), reply(remaining=['g' * 90]), reply(remaining=['a', 'b'])):
            with self.assertRaises(InvalidUpdate):
                model_packet.bind_output('architect', source, schema, raw)
        counter = CountService(); window = counter.descriptor()
        budget = Budget(8192, 512, 479, context_profile=2, reservation_limit=20000, context_window=window)
        context_window._counts.clear()
        with patch.object(context_window, '_rpc', counter.rpc):
            proof = context_window.check(budget, role.SYSTEM, source, schema, thinking=False)
        identity = {'server_model': window['model'], 'server_started': window['started'],
                    'server_context_tokens': window['context_tokens']}
        with patch.object(context_window, '_rpc', side_effect=AssertionError('Historical replay stays offline')):
            context_window.validate_proof(proof, budget, role.SYSTEM, source, schema, identity)
            with self.assertRaises(InvalidUpdate):
                context_window.validate_proof(proof, budget, role.SYSTEM, payload(2), role.schema(payload(2)), identity)
        modern = payload(2)
        model_packet.build(role.SYSTEM, modern, role.schema(modern))
        self.assertEqual(packet, model_packet.build(role.SYSTEM, source, role.schema(source)))
        # The new flag cannot alter either earlier selection protocol.
        for version in (1, 2):
            earlier = dict(source, discovery_selection_version=version)
            flagged = dict(earlier, discovery_note_version=2)
            self.assertEqual(model_packet.build(role.SYSTEM, earlier, role.schema(earlier)),
                             model_packet.build(role.SYSTEM, flagged, role.schema(flagged)))


class DiscoveryNoteGatewayTests(unittest.TestCase):
    setUp = fixture.SelectionWireTests.setUp
    tearDown = fixture.SelectionWireTests.tearDown
    provider = fixture.SelectionWireTests.provider
    step = fixture.SelectionWireTests.step
    until = fixture.SelectionWireTests.until
    discover = fixture.SelectionWireTests.discover

    def architect_receipts(self):
        return [json.loads(row[0]) for row in self.s.db.execute(
            "SELECT r.record_json FROM invocation_receipts r JOIN invocations i ON i.id=r.invocation_id "
            "WHERE json_extract(i.record_json,'$.role')='architect' ORDER BY i.rowid").fetchall()]

    def test_87_character_summary_and_90_75_character_gaps_pass_real_gateway_and_read_admission(self):
        self.fault = lambda raw, _: raw.update(summary='s' * 87, remaining=['g' * 90, 'h' * 75])
        _, source = self.discover(); sid = self.meta['active']
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(source['discovery_coverage']['stop_reason'], 'evidence_complete')
        self.assertEqual(source['discovery_coverage']['read_files'], ['part0.py', 'part1.py'])
        for receipt, (packet, wire, raw) in zip(self.architect_receipts(), self.calls):
            self.assertEqual(packet['discovery_note_version'], 2)
            self.assertEqual(set(wire['schema']['properties']), {'tool', 'summary', 'remaining'})
            self.assertEqual(receipt['status'], 'accepted')
            self.assertEqual(receipt['proposal']['summary'], 's' * 87)
            self.assertEqual(receipt['proposal']['remaining'], ['g' * 90, 'h' * 75])
            self.assertLessEqual(len(canonical(compact(receipt['proposal'])).encode()), 1600)
            self.assertEqual(receipt['model_response_hash'], sha(canonical(raw)))
        self.assertTrue(self.s.verify_invocations(sid))

    def assert_note_quarantined_without_repository_effect(self, summary):
        self.fault = lambda raw, _: raw.update(summary=summary, remaining=[])
        _, source = self.discover(); sid = self.meta['active']
        self.assertEqual(len(self.calls), 3)  # The existing rejection ceiling is unchanged.
        self.assertEqual(source['discovery_coverage']['stop_reason'], 'proposal_rejected')
        self.assertEqual(source['discovery_coverage']['tool_results'], 0)
        for receipt, (packet, _, raw) in zip(self.architect_receipts(), self.calls):
            self.assertEqual(packet['discovery_note_version'], 2)
            self.assertEqual(receipt['status'], 'quarantined')
            self.assertIn('1600 bytes', ' '.join(receipt['admission']['errors']))
            bound, _ = model_packet.bind_output('architect', packet, role.schema(packet), raw)
            self.assertGreater(len(canonical(compact(bound)).encode()), 1600)
        self.assertFalse(any(row['operation'].get('invocation_id') for row in repo.records(self.s, sid)))
        self.assertTrue(self.s.verify_invocations(sid))

    def test_exact_existing_compact_byte_boundary_is_admitted_without_truncation(self):
        fixed = len(canonical(compact({'summary': '', 'remaining': [], 'findings': [], 'completed': []})).encode())
        summary = 's' * (1600 - fixed)
        self.actions = [{'kind': 'clarify'}]
        self.fault = lambda raw, _: raw.update(summary=summary, remaining=[])
        _, source = self.discover()
        self.assertEqual(source['discovery_coverage']['stop_reason'], 'clarification')
        receipts = self.architect_receipts()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]['status'], 'accepted')
        self.assertEqual(receipts[0]['proposal']['summary'], summary)
        self.assertEqual(len(canonical(compact(receipts[0]['proposal'])).encode()), 1600)

    def test_actual_compact_note_over_1600_bytes_is_rejected_by_role_after_shape_binding(self):
        self.assert_note_quarantined_without_repository_effect('s' * 1600)

    def test_canonical_unicode_escaping_counts_toward_existing_byte_allowance(self):
        summary = '😀' * 124
        self.assertLess(len(summary.encode()), 1600)
        self.assert_note_quarantined_without_repository_effect(summary)


if __name__ == '__main__':
    unittest.main()
