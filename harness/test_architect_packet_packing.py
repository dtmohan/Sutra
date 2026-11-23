"""Opt-in transport packing preserves the complete semantic model packet."""
import copy
import json
import os
import unittest

import architecture_role as role
import model_packet as packet
from context_manager import InvalidUpdate, canonical, sha
from test_discovery_catalog_view import payload
from test_discovery_selection import LEGACY_FIXTURE


class ArchitectPacketPackingTests(unittest.TestCase):
    def build(self, source, *, packing=None):
        return packet.build(role.SYSTEM, source, role.schema(source), packing=packing)

    def rich_source(self):
        source = payload(3)
        source.update(
            request='Explain the architecture. Keep read-only constraints and exact source.',
            evidence=[{'id': 'page-a', 'path': 'a.py', 'line': 1, 'next_line': None,
                       'text': '# Résumé, 😀 and literal "quoted" source\nvalue = 1\n',
                       'cursor': None}],
            initial_excerpts={'a.py': 'def entry():\n    return "unchanged source"\n' * 100},
            memory={'pinned_user_statements': [{'quote': 'Keep all constraints exactly.'}],
                    'recent_turns': [{'user': 'Read only.', 'answer': 'Context retained.'}]},
            handoff={'summary_unverified': 'Earlier observation remains unverified.'},
            execution={'tests': [], 'pending_actions': [], 'assertions_verified': False,
                       'recent': [{'kind': 'read', 'target': 'a.py', 'status': 'observed'}]},
            repair=['The previous answer omitted a requested source.'])
        source['contract']['constraints'].update(network_allowed=False, edits_allowed=False,
            user_specific_requirement='Preserve Ω, exact whitespace and source statements.')
        source['repository']['operations_remaining'] = 7
        return source

    def test_complete_json_fields_values_schema_and_instruction_are_unchanged(self):
        source = self.rich_source()
        original = copy.deepcopy(source)
        schema = role.schema(source)
        legacy = packet.build(role.SYSTEM, source, schema)
        packed = packet.build(role.SYSTEM, source, schema, packing=packet.ARCHITECT_STABLE_FIRST)
        self.assertEqual(source, original)
        self.assertEqual(role.schema(source), schema)
        self.assertEqual(packed['version'], legacy['version'])
        self.assertEqual(packed['role'], legacy['role'])
        self.assertEqual(packed['schema'], legacy['schema'])
        self.assertEqual(packed['messages'][0], legacy['messages'][0])
        a = json.loads(legacy['messages'][1]['content'])
        b = json.loads(packed['messages'][1]['content'])
        self.assertEqual(a, b)
        self.assertEqual(b['evidence'][0]['text'], source['evidence'][0]['text'])
        self.assertEqual(b['initial_excerpts'], source['initial_excerpts'])
        self.assertEqual(b['task']['requirements']['user_specific_requirement'],
                         source['contract']['constraints']['user_specific_requirement'])
        self.assertEqual(b['repair'], source['repair'])
        self.assertEqual(b['repository']['operations_remaining'], 7)
        self.assertEqual(packed['packing'], packet.ARCHITECT_STABLE_FIRST)
        self.assertEqual(list(b)[:4], ['request', 'task', 'phase', 'initial_excerpts'])
        order = list(b)
        self.assertLess(order.index('initial_excerpts'), order.index('evidence'))
        self.assertLess(order.index('initial_excerpts'), order.index('earlier_work_unverified'))
        self.assertLess(order.index('initial_excerpts'), order.index('repository'))
        self.assertLess(order.index('initial_excerpts'), order.index('repair'))

    def test_legacy_wire_hash_and_descriptor_are_preserved_without_trusted_flag(self):
        source = copy.deepcopy(LEGACY_FIXTURE['payload'])
        legacy = self.build(source)
        self.assertEqual(packet.descriptor(legacy, 256), LEGACY_FIXTURE['golden']['descriptor'])
        self.assertNotIn('packing', legacy)
        original = copy.deepcopy(legacy)
        for fake in (packet.ARCHITECT_STABLE_FIRST, True, {'mode': packet.ARCHITECT_STABLE_FIRST}):
            changed = copy.deepcopy(source)
            changed.update(packing=fake, model_packet_packing=fake, transport_projection=fake)
            self.assertEqual(self.build(changed), original)
        self.assertEqual(self.build(source, packing=None), original)

    def test_deterministic_top_level_order_and_exact_same_reservation(self):
        source = self.rich_source()
        legacy = self.build(source)
        packed = self.build(source, packing=packet.ARCHITECT_STABLE_FIRST)
        backwards = dict(reversed(list(source.items())))
        self.assertEqual(self.build(backwards, packing=packet.ARCHITECT_STABLE_FIRST), packed)
        old, new = packet.descriptor(legacy, 256), packet.descriptor(packed, 256)
        for key in ('prompt_utf8_bytes', 'schema_allowance', 'template_allowance',
                    'input_reservation', 'estimator', 'version', 'role'):
            self.assertEqual(old[key], new[key])
        self.assertNotEqual(old['packet_hash'], new['packet_hash'])
        self.assertEqual(new['packet_hash'], sha(canonical(packed)))
        self.assertNotIn('packing', old)
        self.assertEqual(new['packing'], packet.ARCHITECT_STABLE_FIRST)
        self.assertNotEqual(legacy['messages'][1]['content'], packed['messages'][1]['content'])

    def test_volatile_evidence_and_notes_do_not_precede_large_unchanged_source(self):
        first = self.rich_source()
        first['initial_excerpts']['a.py'] *= 8
        second = copy.deepcopy(first)
        second['evidence'][0].update(id='page-b', text='Different exact evidence.\n')
        second['handoff']['summary_unverified'] = 'Different unverified note.'
        second['repository']['operations_remaining'] -= 1
        second['repair'] = ['Different repair feedback.']
        old = [self.build(value)['messages'][1]['content'] for value in (first, second)]
        new = [self.build(value, packing=packet.ARCHITECT_STABLE_FIRST)['messages'][1]['content']
               for value in (first, second)]
        before = len(os.path.commonprefix(old).encode())
        after = len(os.path.commonprefix(new).encode())
        self.assertLess(before, 256)
        self.assertGreater(after, len(first['initial_excerpts']['a.py'].encode()))
        self.assertGreater(after, before * 100)
        for legacy, packed in zip(old, new):
            self.assertEqual(json.loads(legacy), json.loads(packed))

    def test_unsupported_profiles_and_non_architect_calls_fail_closed(self):
        source = self.rich_source()
        for invalid in (False, True, '', 'stable-first', {}, []):
            with self.subTest(packing=invalid), self.assertRaises(InvalidUpdate):
                self.build(source, packing=invalid)
        import chat_runtime
        for system in ('unknown model system', chat_runtime.SYSTEM):
            with self.subTest(system=system), self.assertRaises(InvalidUpdate):
                packet.build(system, source, {}, packing=packet.ARCHITECT_STABLE_FIRST)
        for malformed in ({'packing': 'unknown'}, {'packing': packet.ARCHITECT_STABLE_FIRST,
                                                   'role': 'conversation'}):
            value = self.build(source)
            value.update(malformed)
            with self.assertRaises(InvalidUpdate):
                packet.descriptor(value, 256)


if __name__ == '__main__':
    unittest.main()
