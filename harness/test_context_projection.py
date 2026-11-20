"""Regressions at the transport boundary, rather than only host memory state."""
import copy
import json
import unittest
from unittest.mock import patch

import architecture_role
import claim_pages
import memory_layer
import model_packet
import test_architecture as fixture
from invocation_gateway import consume_dispatch


class ReferenceModel(fixture.Model):
    model_interface = 1

    def call(self, cid, system, payload, schema):
        packet = model_packet.build(system, payload, schema)
        self.packet = packet
        with patch('test_architecture.consume_dispatch',
                   side_effect=lambda *args: consume_dispatch(*args, packet=packet)):
            result, record = super().call(cid, system, payload, schema)
        for key in model_packet.host_fields(packet['role'], payload, schema, packet['version'], result):
            result.pop(key)
        for path, finding in result['findings'].items():
            choices = schema['properties']['findings']['properties'][path]['properties']['quote']['enum']
            finding['quote'] = choices.index(finding['quote'])
        record['response'] = {'done': True, 'prompt_eval_count': 100, 'eval_count': 60}
        return result, record


class ArchitectureProjectionTests(unittest.TestCase):
    setUp = fixture.ArchitectureTests.setUp
    tearDown = fixture.ArchitectureTests.tearDown
    step = fixture.ArchitectureTests.step

    def test_quote_ids_restore_exact_source_and_verify_receipt_chain(self):
        self.step(fixture.Model())
        self.step(fixture.Model('read', 'broker.py'))
        model = ReferenceModel('report', '')
        out = self.step(model)
        self.assertTrue(out['result']['admission']['admitted'], out['result']['admission'])
        self.assertEqual(model.packet['version'], model_packet.RETAINED_ARCHITECT_VERSION)
        body = json.loads(model.packet['messages'][1]['content'])
        self.assertEqual(body['retained_context']['totals']['notes'], 2)
        self.assertIn('orders.audit.v3', json.dumps(body))
        self.assertTrue(all(isinstance(f['quote'], str)
                            for f in out['result']['accepted_output']['findings'].values()))
        self.assertTrue(self.s.verify_invocations(self.sid))
        packet = model.payloads[0]
        envelope = {'role': 'architect', 'model_interface': {'version': model.packet['version']}}
        refs = memory_layer._wire_refs(packet, envelope)
        self.assertTrue(any(r[0].startswith('wire:') for r in refs))
        changed = copy.deepcopy(packet)
        changed['hidden_host_note'] = 'THIS NEVER REACHED THE MODEL'
        self.assertEqual(refs, memory_layer._wire_refs(changed, envelope))

    def test_independent_claim_inventory_includes_exact_disjoint_sources_only(self):
        source = {'excerpts': {'entry.py': 'def entry():\n    return 1\n'},
                  'snapshot_receipts': ['snapshot'], 'retained_context_version': 1,
                  'retained_context': {'pages': [
                      {'kind': 'working_note_unverified', 'note': {'summary': 'UNVERIFIED CLAIM'}},
                      {'kind': 'retained_source_unit', 'evidence': {
                          'id': 'late-window', 'path': 'entry.py', 'line': 93,
                          'text': 'def finish():\n    return "EARLY READ ANCHOR"\n'}}]}}
        inventory = claim_pages.inventory('candidate', source)
        values = list(inventory.values())
        self.assertTrue(any('EARLY READ ANCHOR' in row['text'] and row['start_line'] == 93 for row in values))
        self.assertNotIn('UNVERIFIED CLAIM', json.dumps(values))
        baseline = claim_pages.source_hash(source)
        source['retained_context']['pages'][1]['evidence']['text'] += '# changed\n'
        self.assertNotEqual(baseline, claim_pages.source_hash(source))

    def test_optional_wire_diagnostic_failure_does_not_reject_or_refund(self):
        self.step(fixture.Model())
        self.step(fixture.Model('read', 'broker.py'))
        before = self.s.db.execute('SELECT count(*) FROM invocation_receipts').fetchone()[0]
        with patch('memory_layer._wire_refs', side_effect=KeyError('diagnostic fixture')):
            out = self.step(ReferenceModel('report', ''))
        self.assertTrue(out['result']['admission']['admitted'], out['result']['admission'])
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM invocation_receipts').fetchone()[0], before + 1)
        self.assertTrue(self.s.verify_invocations(self.sid))
        events = memory_layer.snapshot(self.s.db)
        self.assertEqual(events['diagnostic_issues'], 1)
        self.assertTrue(any(e['kind'] == 'working_set_unavailable' for e in events['events']))


if __name__ == '__main__':
    unittest.main()
