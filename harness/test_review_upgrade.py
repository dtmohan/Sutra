"""An opted-in release handoff preserves an already joined read-only review."""
import json
import unittest
from unittest.mock import patch

import orchestration as machine
import orchestration_flow as flow
import review_budget
import review_upgrade as upgrade
import test_orchestration as fixture
from context_manager import InvalidUpdate
from invocation_gateway import InvocationLedger
from paging import PageFault
from vscode_bridge import handle, state


class ReviewUpgradeTests(unittest.TestCase):
    setUp = fixture.OrchestrationTests.setUp
    tearDown = fixture.OrchestrationTests.tearDown
    step = fixture.OrchestrationTests.step
    until = fixture.OrchestrationTests.until
    provider = fixture.OrchestrationTests.provider

    def paused_join(self):
        self.prompt = 'Explain the project architecture and tools logic with diagrams. ' + self.prompt
        with patch('governance.policy_hash', return_value=upgrade.PREDECESSOR):
            self.s.close()
            self.s = InvocationLedger(self.root / 'state.sqlite')
            handle(self.root, self.ws, {'op': 'auto_new', 'goal': self.prompt, 'roots': ['.']})
            self.meta = json.loads((self.root / 'workspace.json').read_text())
            sid = self.meta['active']
            started = self.step(event='start_review', binding=flow.identity(self.s, sid))
            joined = self.until('SYNTHESIS', started)
            fid = joined['flow_id']
            _, data = flow.info(self.s, fid)
            self.assertTrue(data['child_results'])
            flow.move(self.s, fid, 'PAUSED', 'RESOURCE_STOPPED', workflow_stop={
                'status': 'resource_wait', 'resume_phase': 'SYNTHESIS', 'error': 'Fixture probe timed out',
            })
            paused = flow.response(self.s, fid)
            self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        self.meta = json.loads((self.root / 'workspace.json').read_text())
        return paused, sid, data

    def test_confirmed_handoff_preserves_join_context_and_spending(self):
        paused, sid, original = self.paused_join()
        fid = paused['flow_id']
        root = original['root_node']
        before_usage = review_budget.usage(self.s, sid)
        before_receipts = self.s.db.execute(
            'SELECT id,record_json FROM invocation_receipts WHERE session=? ORDER BY seq', (sid,)).fetchall()
        before_contract = self.s.validation_contract(sid)
        owner = machine.node(self.s, root)
        candidates = original['child_results']
        with self.assertRaisesRegex(PageFault, 'stale contract or validator'):
            self.s.load_result(sid, candidates[0])

        with patch('review_upgrade.release_pair', return_value=True):
            notice = flow.response(self.s, fid)
            self.assertEqual(notice['recovery']['kind'], 'compatible_review_upgrade')
            with self.assertRaisesRegex(InvalidUpdate, 'requires confirmation'):
                self.step(notice, event='restart_review')
            handed_off = self.step(notice, event='restart_review', confirmed=True)
            self.assertEqual(handed_off['flow_id'], fid)
            self.assertEqual(handed_off['execution_id'], root)
            self.assertEqual(handed_off['state'], 'PAUSED')
            self.assertIsNone(handed_off['recovery'])
            self.assertEqual(review_budget.usage(self.s, sid), before_usage)
            self.assertEqual(machine.node(self.s, root), owner)
            self.assertEqual(self.s.validation_contract(sid), before_contract)
            self.assertEqual(state(self.s, self.meta, self.ws)['auto']['mapped_files'], 4)
            with self.assertRaisesRegex(PageFault, 'stale contract or validator'):
                self.s.load_result(sid, candidates[0])

            with patch('provider_resources.recover', return_value=None), patch('provider_resources.require_resources'):
                resumed = self.step(handed_off, event='resume')
            done = self.until('DONE', resumed)
            self.assertTrue(done['data']['result']['admission']['admitted'])
            self.assertTrue(done['data']['report_markdown'])
            self.assertEqual(done['flow_id'], fid)
            self.assertEqual(flow.info(self.s, fid)[1]['child_results'], candidates)
            after = self.s.db.execute(
                'SELECT id,record_json FROM invocation_receipts WHERE session=? ORDER BY seq', (sid,)).fetchall()
            self.assertEqual(after[:len(before_receipts)], before_receipts)
            self.assertTrue(all(json.loads(raw)['logic_hash'] == self.s.logic_hash
                                for _, raw in after[len(before_receipts):]))
            final_usage = review_budget.usage(self.s, sid)
            self.assertEqual(final_usage['limits'], before_usage['limits'])
            self.assertGreater(final_usage['used']['calls'], before_usage['used']['calls'])
            self.assertTrue(self.s.verify_invocations(sid))

    def test_source_change_prevents_handoff_under_approved_pair(self):
        paused, sid, original = self.paused_join()
        before_usage = review_budget.usage(self.s, sid)
        (self.ws / 'part0.py').write_text('def part0():\n    return 57\n')
        with patch('review_upgrade.release_pair', return_value=True):
            with self.assertRaisesRegex(InvalidUpdate, 'Static snapshot source changed'):
                self.step(paused, event='restart_review', confirmed=True)
            self.assertIsNone(upgrade.recorded(self.s, paused['flow_id']))
        self.assertEqual(flow.info(self.s, paused['flow_id'])[1]['child_results'], original['child_results'])
        self.assertEqual(review_budget.usage(self.s, sid), before_usage)


if __name__ == '__main__':
    unittest.main()
