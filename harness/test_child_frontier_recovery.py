"""Real source shape from the three-file review: bodies, imports, then handoff."""
import copy
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate, sha
from invocation_gateway import InvocationLedger
import orchestration as machine
import review_budget
import subagent_runtime as child
import test_orchestration as fixture


class ChildFrontierRecoveryTests(unittest.TestCase):
    setUp = fixture.OrchestrationTests.setUp
    tearDown = fixture.OrchestrationTests.tearDown
    provider = fixture.OrchestrationTests.provider
    step = fixture.OrchestrationTests.step
    until = fixture.OrchestrationTests.until

    def tiny_project(self):
        for path in self.ws.iterdir():
            path.unlink()
        sources = {'main.py': 'from service import summarize\n\ndef run(values):\n    return summarize(values)\n',
                   'service.py': 'from store import retain\n\ndef summarize(values):\n    result = sum(values)\n    retain(result)\n    return result\n',
                   'store.py': 'RESULTS = []\n\ndef retain(value):\n    RESULTS.append(value)\n'}
        for path, text in sources.items():
            (self.ws / path).write_text(text)
        return sources

    def test_saved_v4_bodies_resume_with_unseen_headers_then_one_frontier_handoff(self):
        sources = self.tiny_project()
        out = self.until('CHILDREN'); first = out['children'][0]['id']; sid = self.meta['active']
        contract = self.s.pinned(sid); original = fixture.Child.call; seen = []
        remaining = ['Check imported summarize in the workspace and verify the RESULTS global declaration.']

        def review(worker, cid, system, packet, schema):
            result, record = original(worker, cid, system, packet, schema)
            seen.append(copy.deepcopy(packet))
            notes = packet.get('child_checkpoint', [])
            if packet.get('child_source_handoff'):
                handoff = packet['child_source_handoff']
                self.assertEqual(handoff['basis'], 'source_frontier_exhausted')
                self.assertEqual(handoff['kernel']['decision'], 'BRIDGE')
                self.assertEqual(handoff['observation']['nonblank_lines'], handoff['observation']['delivered_nonblank_lines'])
                self.assertFalse(handoff['semantic_completion_verified'])
                self.assertEqual(schema['properties']['slice_status']['enum'], ['complete'])
                result.update(slice_status='complete', remaining=['The parent must connect this partition to its sibling source.'])
            else:
                if notes:
                    for path, text in packet['excerpts'].items():
                        self.assertIn(sources[path].splitlines()[0], text)
                else:
                    for path, text in packet['excerpts'].items():
                        self.assertNotIn(sources[path].splitlines()[0], text)
                result.update(slice_status='continue', remaining=remaining)
            return result, record

        with patch.object(fixture.Child, 'call', review):
            with patch.object(child, 'CHILD_EVIDENCE_VERSION', 4):
                out = self.step(out)
            self.assertEqual(machine.slice_cursor(self.s, first)['ordinal'], 2)
            after_body = review_budget.usage(self.s, sid)
            cursor = copy.deepcopy(machine.slice_cursor(self.s, first))
            permissions = machine.node(self.s, first)['permissions']
            old_invocation = machine.slice_history(self.s, first, machine.slice_cursor(self.s, first))[0]['invocation_id']
            original_delivery = copy.deepcopy(seen[-1])
            self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite'); self.tier = 'high'
            out = self.step(out)
            self.assertEqual(machine.slice_cursor(self.s, first)['ordinal'], 3)
            self.assertEqual(seen[-1]['child_evidence_version'], 5)
            self.assertFalse(seen[-1].get('child_source_handoff'))
            self.assertEqual(seen[-1]['child_slice'], cursor)
            out = self.step(out)
            self.assertEqual(machine.state(self.s, first), 'DONE')
            self.assertIn('partial_handoff', out['child'])
            after = review_budget.usage(self.s, sid)
            self.assertEqual(after['used']['calls'], after_body['used']['calls'] + 2)
            for dimension in ('operations', 'source_bytes'):
                self.assertEqual(after['used'][dimension], after_body['used'][dimension])
            self.assertEqual(after['limits'], after_body['limits'])
            self.assertGreater(after['used']['tokens'], after_body['used']['tokens'])
            self.assertEqual(machine.node(self.s, first)['permissions'], permissions)
            self.assertEqual(self.s.pinned(sid), contract)
            finished = self.until('DONE', out)
        self.assertTrue(finished['data']['result']['admission']['admitted'])
        for item in finished['children']:
            events = [event for event in machine.events(self.s, item['id']) if event['kind'] == 'CHILD_SOURCE_HANDOFF']
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]['data']['observation']['profile'], 'child-source-frontier-v1')
        self.assertEqual(self.s.load_result(sid, old_invocation)['slice_status'], 'continue')
        _, _, replayed = machine._child_receipt(self.s, machine.node(self.s, first), old_invocation)
        self.assertEqual(replayed, original_delivery)
        self.assertTrue(self.s.verify_invocations(sid))

    def test_v4_replay_remains_exhausted_while_v5_recovers_union_gaps(self):
        sources = self.tiny_project()
        data = {'sources': {path: sources[path] for path in ('main.py', 'store.py')},
                'graph': {'nodes': [{'name': 'run', 'path': 'main.py', 'line': 3, 'column': 0},
                                   {'name': 'retain', 'path': 'store.py', 'line': 3, 'column': 0}], 'calls': []}}
        notes = [{'remaining': ['Inspect the imports and RESULTS global.'], 'source_spans': {
            path: {'start_line': 3, 'next_line': 5, 'anchor_line': 3,
                   'sha256': sha(''.join(text.splitlines(keepends=True)[2:4]))}
            for path, text in data['sources'].items()}}]
        packet = {'child_evidence_version': 4, 'contract': {'goal': 'Review the architecture.'},
                  'request': 'Review the architecture.', 'child_checkpoint': notes, 'excerpt_bytes': 512}
        self.assertEqual(child.slice_excerpts(packet, data), {})
        current = dict(packet, child_evidence_version=5); spans = {}
        recovered = child.slice_excerpts(current, data, source_spans=spans)
        self.assertEqual(set(recovered), set(data['sources']))
        for path in recovered:
            self.assertEqual(recovered[path].splitlines()[0], sources[path].splitlines()[0])
            self.assertEqual(spans[path]['start_line'], 1)
        current['child_checkpoint'] = notes + [{'remaining': notes[0]['remaining'], 'source_spans': spans}]
        self.assertEqual(child._source_frontier(current, data), (6, 6))
        tampered = copy.deepcopy(current); tampered['child_checkpoint'][0]['source_spans']['main.py']['sha256'] = '0' * 64
        with self.assertRaisesRegex(InvalidUpdate, 'accepted source span'):
            child._source_frontier(tampered, data)


if __name__ == '__main__':
    unittest.main()
