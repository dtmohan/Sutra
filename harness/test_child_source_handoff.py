"""A repeated source request may hand findings upward only after body delivery."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate, canonical, sha
import orchestration as machine
import review_budget
import static_runtime
import subagent_runtime as child
import test_orchestration as fixture


class ChildSourceHandoffTests(unittest.TestCase):
    setUp = fixture.OrchestrationTests.setUp
    tearDown = fixture.OrchestrationTests.tearDown
    provider = fixture.OrchestrationTests.provider
    step = fixture.OrchestrationTests.step
    until = fixture.OrchestrationTests.until

    def source(self, *, target_first):
        for index in range(4):
            target = f'def requested_{index}():\n    value = resolve_requested_{index}()\n    return value\n\n'
            main = ('def main():\n' + ''.join(f'    # source {index} step {line:04}: distinct code review context.\n' for line in range(600)) +
                    f'    return requested_{index}()\n\n')
            (self.ws / f'part{index}.py').write_text(target + main if target_first else main + target)
        out = self.until('CHILDREN')
        branch = out['children'][0]['id']
        path = machine.node(self.s, branch)['files'][0]
        number = Path(path).stem.removeprefix('part')
        return out, branch, path, f'requested_{number}'

    def historical(self, *, bare, target_first):
        out, branch, path, target = self.source(target_first=target_first)
        remaining = f'Inspect {target}.' if bare else f'Inspect {Path(path).stem}.{target}.'
        original = fixture.Child.call

        def continued(worker, cid, system, packet, schema):
            result, record = original(worker, cid, system, packet, schema)
            prior = {(item['path'], item['quote']) for note in packet.get('child_checkpoint', []) for item in note['citations']}
            choice = next(item for item in schema['properties']['citations']['items']['enum']
                          if (item['path'], item['quote']) not in prior)
            result.update(slice_status='continue', remaining=[remaining], citations=[copy.deepcopy(choice)])
            return result, record

        with patch.object(child, 'CHILD_EVIDENCE_VERSION', 3), patch.object(fixture.Child, 'call', continued):
            for _ in range(4):
                out = self.step(out)
                self.assertEqual(out['state'], 'CHILDREN', out)
                self.assertTrue(out['child']['result']['admission']['admitted'])
        self.assertEqual(machine.slice_cursor(self.s, branch)['ordinal'], 5)
        return out, branch, path, target, remaining

    def exercise(self, *, bare, target_first):
        out, branch, path, target, remaining = self.historical(bare=bare, target_first=target_first)
        sid = self.meta['active']
        before = review_budget.usage(self.s, sid)
        pinned = self.s.pinned(sid)
        permissions = machine.node(self.s, branch)['permissions']
        saved = copy.deepcopy(machine.slice_history(self.s, branch, machine.slice_cursor(self.s, branch)))
        original = fixture.Child.call
        seen = []

        def finish(worker, cid, system, packet, schema):
            self.assertEqual(packet['child_evidence_version'], 4)
            handoff = packet.get('child_source_handoff')
            seen.append(copy.deepcopy(packet))
            result, record = original(worker, cid, system, packet, schema)
            if handoff:
                self.assertEqual(schema['properties']['slice_status']['enum'], ['complete'])
                self.assertEqual(handoff['kernel']['decision'], 'BRIDGE')
                self.assertFalse(handoff['semantic_completion_verified'])
                self.assertEqual(handoff['observation']['delivered_targets'], handoff['observation']['requested_targets'])
                transition = next(event for event in reversed(machine.events(self.s, branch))
                                  if event['kind'] == 'CHILD_SOURCE_HANDOFF')
                self.assertEqual(transition['node_id'], branch)
                self.assertEqual(transition['from'], transition['to'])
                self.assertGreater(transition['at'], 0)
                self.assertEqual(transition['kernel'], handoff['kernel'])
                self.assertEqual(transition['data']['scope'], 'child_source_acquisition')
                self.assertFalse(transition['data']['task_closed'])
                self.assertEqual(transition['data']['handoff_hash'], sha(canonical(handoff)))
                node = machine.node(self.s, branch)
                data = static_runtime.load_partition(self.s, sid, packet['workspace'], packet['snapshot_receipts'], branch)
                forged = copy.deepcopy(packet)
                forged['child_source_handoff']['observed_repeats'] += 1
                with self.assertRaisesRegex(InvalidUpdate, 'handoff observation changed'):
                    child.request_check(self.s, sid, forged)
                forged_data = copy.deepcopy(data)
                forged_data['versions'][path] = '0' * 64
                with self.assertRaisesRegex(InvalidUpdate, 'source version changed'):
                    child.source_handoff(self.s, sid, packet, forged_data)
                result.update(slice_status='complete', remaining=[])
            else:
                self.assertEqual(schema['properties']['slice_status']['enum'], ['continue', 'complete'])
                self.assertIn(f'def {target}()', packet['excerpts'][path])
                self.assertIn('    return value', packet['excerpts'][path])
                result.update(slice_status='continue', remaining=[remaining])
            return result, record

        with patch.object(fixture.Child, 'call', finish):
            out = self.step(out)
            if bare:
                self.assertNotIn('child_source_handoff', seen[0], 'A repeated request is not a stop before its body was supplied')
                self.assertEqual(machine.state(self.s, branch), 'READY')
                out = self.step(out)
        self.assertEqual(machine.state(self.s, branch), 'DONE', out)
        self.assertIn('partial_handoff', out['child'])
        self.assertEqual(len(seen), 2 if bare else 1)
        self.assertTrue(seen[-1].get('child_source_handoff'))
        self.assertIn(remaining, canonical(seen[-1]['child_checkpoint']))
        self.assertEqual(machine.node(self.s, branch)['permissions'], permissions)
        self.assertEqual(self.s.pinned(sid), pinned)
        after = review_budget.usage(self.s, sid)
        self.assertEqual(after['limits'], before['limits'])
        self.assertEqual(after['used']['calls'], before['used']['calls'] + len(seen))
        self.assertGreater(after['used']['tokens'], before['used']['tokens'])
        for dimension in ('source_bytes', 'operations'):
            self.assertEqual(after['used'][dimension], before['used'][dimension])
        cursor = machine.slice_cursor(self.s, branch)
        self.assertEqual(machine.slice_history(self.s, branch, cursor)[:len(saved)], saved)
        self.assertTrue(self.s.verify_invocations(sid))
        return out, remaining

    def test_repeated_request_hands_off_after_whole_body_was_delivered(self):
        self.exercise(bare=False, target_first=True)

    def test_repeated_bare_request_gets_missing_body_before_partial_handoff(self):
        self.exercise(bare=True, target_first=False)

    def test_parent_join_keeps_partial_scope_and_the_unverified_remaining_check(self):
        out, remaining = self.exercise(bare=False, target_first=True)
        branch = out['children'][0]['id']
        parent = out['execution_id']
        self.until('SYNTHESIS', out)
        joined = next(event for event in reversed(machine.events(self.s, parent)) if event['kind'] == 'JOIN')
        notes = child.joined_results(self.s, self.meta['active'], parent, joined['data']['invocations'],
                                     self.prompt, note_version=3)
        partial = next(item for item in notes if item['branch_id'] == branch)
        self.assertEqual(partial['evidence_coverage']['review_scope'], 'bounded_partial_handoff')
        self.assertFalse(partial['evidence_coverage']['semantic_completion_verified'])
        self.assertFalse(partial['child_source_handoff']['semantic_completion_verified'])
        self.assertIn(remaining, partial['remaining_unverified'])


if __name__ == '__main__':
    unittest.main()
