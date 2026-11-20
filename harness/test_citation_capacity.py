"""Valid fresh references are bounded by supplied choices and output capacity."""
import copy
import unittest

import chat_runtime as chat
from context_manager import InvalidUpdate
from invocation_roles import validate_shape
from model_packet import build, bind_output


class CitationCapacityTests(unittest.TestCase):
    def setUp(self):
        self.p = {'contract': {'task_id': 'task', 'goal': 'Explain the supplied code.', 'constraints': {}},
                  'constraint_echo': [], 'request': 'Explain the supplied code.',
                  'route': {'record': {'choice': {'model': 'strata:high'}}},
                  'excerpts': {f'module{i}.py': f'def run_{i}():\n    return {i}\n' for i in range(7)}}
        self.proposal = {'task_id': 'task', 'constraints': [], 'answer': 'These are local source findings.',
                         'summary': 'Local findings.', 'remaining': [], 'complete': False,
                         'citations': chat.citation_options(self.p)[:5]}

    def test_new_policy_accepts_five_valid_choices_and_preserves_legacy_schema(self):
        with self.assertRaises(InvalidUpdate):
            validate_shape(self.proposal, chat.schema(self.p))
        self.p['citation_policy_version'] = 2
        fmt = chat.schema(self.p)
        validate_shape(self.proposal, fmt)
        self.assertEqual(fmt['properties']['citations']['maxItems'], 14)
        self.assertEqual(chat.schema(dict(self.p, citation_policy_version=1))['properties']['citations']['maxItems'], 4)

    def test_wire_ids_round_trip_to_only_supplied_source_choices(self):
        self.p['citation_policy_version'] = 2
        fmt = chat.schema(self.p)
        packet = build(chat.SYSTEM, self.p, fmt)
        raw = {'answer': 'These are local source findings.', 'remaining': [], 'citations': [1, 2, 3, 4, 7]}
        validate_shape(raw, packet['schema'])
        restored, _ = bind_output('chat_reviewer', self.p, fmt, raw, packet['version'])
        validate_shape(restored, fmt)
        self.assertEqual(restored['citations'], [chat.citation_options(self.p)[i - 1] for i in raw['citations']])
        bad = copy.deepcopy(raw)
        bad['citations'].append(15)
        with self.assertRaises(InvalidUpdate): validate_shape(bad, packet['schema'])
        self.assertNotIn('citation_policy_version', packet['messages'][1]['content'])


if __name__ == '__main__':
    unittest.main()
