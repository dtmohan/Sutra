"""An opted-in release handoff preserves an already joined read-only review."""
import json
import unittest
from unittest.mock import patch

import conversation_memory
import orchestration as machine
import orchestration_flow as flow
import retained_context as retained
import review_budget
import review_upgrade as upgrade
import test_orchestration as fixture
import test_retained_context as retained_fixture
import test_terminal_review_recovery as saved_fixture
from context_manager import InvalidUpdate
from invocation_gateway import InvocationLedger
from monitoring import ResourceWait
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


class ContextProfileUpgradeTests(unittest.TestCase):
    tearDown = retained_fixture.RetainedRecoveryTests.tearDown
    provider = retained_fixture.RetainedRecoveryTests.provider
    until = retained_fixture.RetainedRecoveryTests.until
    discover = retained_fixture.RetainedRecoveryTests.discover
    invoke_writer = retained_fixture.RetainedRecoveryTests.invoke_writer
    crashed_writer = retained_fixture.RetainedRecoveryTests.crashed_writer

    def setUp(self):
        retained_fixture.RetainedRecoveryTests.setUp(self)
        self.ws = self.ws.resolve()
        self.meta['workspace'] = str(self.ws)

    def step(self, out=None, **kwargs):
        kwargs.setdefault('chat_key', '1' * 64)
        result = fixture.OrchestrationTests.step(self, out, **kwargs)
        if result.get('binding'):
            self.meta['active'] = result['binding']['session']
        return result

    def paused_review(self, *, policy=upgrade.CONTEXT_PROFILE_PREDECESSOR):
        with patch('governance.policy_hash', return_value=policy):
            self.s.close()
            self.s = InvocationLedger(self.root / 'state.sqlite')
            packet, request, recall = self.crashed_writer()
            fid = machine.node(self.s, self.owner)['flow_id']
            flow.move(self.s, fid, 'SYNTHESIS', 'RETAINED_CONTEXT_REQUESTED', context_recall=recall)
            flow.move(self.s, fid, 'PAUSED', 'CONTEXT_STOPPED', workflow_stop={
                'status': 'context_budget', 'stage': 'SYNTHESIS', 'error': 'Fixture context allocation exceeded',
            })
            self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        self.meta = flow.flow_metadata(self.root, self.ws, fid)
        return fid, packet, request, recall

    def test_resume_native_review_preserves_handoff_pages_children_grants_and_spending(self):
        fid, packet, request, recall = self.paused_review()
        sid = self.sid
        original = machine.node(self.s, self.owner)
        before = review_budget.usage(self.s, sid)
        pins = self.s.pinned(sid)
        children = flow.info(self.s, fid)[1]['child_results']
        self.assertTrue(children)
        self.assertTrue(before['used']['calls'])
        self.assertTrue(before['used']['operations'])
        self.assertNotIn(recall['refs'][0], {page['ref'] for page in packet['retained_context']['pages']})
        handoff = request['conversation_id']
        conversation_sid = self.s.db.execute('SELECT session FROM invocations WHERE id=?', (handoff,)).fetchone()[0]
        with self.assertRaisesRegex(PageFault, 'unsupported historical policy'):
            conversation_memory.history(self.s, conversation_sid, handoff)
        repo_receipts = self.s.db.execute('SELECT id,record_json FROM repository_receipts ORDER BY seq').fetchall()
        before_receipts = self.s.db.execute(
            'SELECT id,record_json FROM invocation_receipts WHERE session=? ORDER BY seq', (sid,)).fetchall()
        with patch('review_upgrade.release_pair', return_value=True):
            paused = flow.response(self.s, fid)
            self.assertEqual(paused['recovery']['kind'], 'compatible_review_upgrade')
            with self.assertRaisesRegex(InvalidUpdate, 'requires confirmation'):
                self.step(paused, event='restart_review')
            accepted = self.step(paused, event='restart_review', confirmed=True)
            self.assertEqual(accepted['execution_id'], self.owner)
            stamp = upgrade.recorded(self.s, fid)
            with upgrade.use(self.s, stamp), machine.scope(self.owner):
                page = retained.lookup(self.s, sid, packet, recall['refs'][0])
                self.assertEqual(conversation_memory.history(self.s, conversation_sid, handoff)[0]['user'], self.prompt)
            resumed = self.step(accepted, event='resume')
            self.assertEqual(resumed['state'], 'SYNTHESIS')
            self.assertEqual(review_budget.usage(self.s, sid), before)
            self.assertEqual(self.s.pinned(sid), pins)
            self.assertEqual(machine.node(self.s, self.owner), original)
            self.tier = 'high'
            self.models.clear()
            done = self.until('DONE', resumed)
            self.assertEqual(done['execution_id'], self.owner)
            self.assertEqual(flow.info(self.s, fid)[1]['child_results'], children)
            self.assertTrue(done['data']['result']['admission']['admitted'])
            writer = next(p for stage, model in self.models if stage == 'synthesis'
                          for p in model.payloads if p.get('retained_context'))
            self.assertEqual(writer['route']['record']['choice']['model'], fixture.MODELS['high'])
            self.assertEqual(writer['request'], self.prompt)
            self.assertEqual(writer['contract']['constraints'], self.s.snapshot(sid)['constraints'])
            self.assertIn(page, writer['retained_context']['pages'])
            self.assertEqual(self.s.db.execute('SELECT id,record_json FROM repository_receipts ORDER BY seq').fetchall(), repo_receipts)
            receipts = self.s.db.execute(
                'SELECT id,record_json FROM invocation_receipts WHERE session=? ORDER BY seq', (sid,)).fetchall()
            self.assertEqual(receipts[:len(before_receipts)], before_receipts)
            self.assertTrue(all(json.loads(raw)['logic_hash'] == self.s.logic_hash
                                for _, raw in receipts[len(before_receipts):]))
            after = review_budget.usage(self.s, sid)
            self.assertEqual(after['limits'], before['limits'])
            self.assertGreater(after['used']['calls'], before['used']['calls'])
            self.assertTrue(self.s.verify_invocations(sid))
        with self.assertRaisesRegex(PageFault, 'unsupported historical policy'):
            conversation_memory.history(self.s, conversation_sid, handoff)

    def test_other_policy_does_not_admit_handoff_even_at_same_pause(self):
        fid, _, _, _ = self.paused_review(policy='2' * 64)
        with patch('review_upgrade.release_pair', return_value=True):
            self.assertIsNone(upgrade.offer(self.s, fid, flow.info(self.s, fid)[1]))


class ReviewerAdmissionUpgradeTests(unittest.TestCase):
    setUp = saved_fixture.TerminalReviewRecoveryTests.setUp
    tearDown = saved_fixture.TerminalReviewRecoveryTests.tearDown
    provider = saved_fixture.TerminalReviewRecoveryTests.provider
    step = saved_fixture.TerminalReviewRecoveryTests.step
    until = saved_fixture.TerminalReviewRecoveryTests.until
    checkpoint = saved_fixture.TerminalReviewRecoveryTests.checkpoint
    rows = saved_fixture.TerminalReviewRecoveryTests.rows
    chat = '4' * 64

    def test_paused_admitted_writer_hand_off_preserves_draft_and_ledger(self):
        self.s.close()
        with patch('governance.policy_hash', return_value=upgrade.REVIEWER_ADMISSION_PREDECESSOR):
            self.s = InvocationLedger(self.root / 'state.sqlite')
            saved, checkpoint = self.checkpoint()
            block = ResourceWait({'decision': 'BRIDGE', 'runtime': {
                'kind': 'model_capacity', 'inference_started': False}})
            with patch('report_review.run', side_effect=block), patch('provider_resources.recover', return_value=None):
                stopped = self.step(saved)
            sid = stopped['binding']['session']; fid = stopped['flow_id']
            rows, before = self.rows(sid), review_budget.usage(self.s, sid)
            self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        with patch('review_upgrade.release_pair', return_value=False):
            self.assertIsNone(upgrade.offer(self.s, fid, flow.info(self.s, fid)[1]))
        with patch('review_upgrade.release_pair', return_value=True):
            offered = self.step(stopped, event='inspect')
            self.assertEqual(offered['recovery']['kind'], 'compatible_review_upgrade')
            adopted = self.step(offered, event='restart_review', confirmed=True)
            self.assertEqual(adopted['flow_id'], fid)
            self.assertEqual(adopted['binding'], stopped['binding'])
            self.assertEqual(flow.info(self.s, fid)[1]['parent_checkpoint'], checkpoint)
            self.assertEqual(review_budget.usage(self.s, sid), before)
            self.assertEqual(self.rows(sid), rows)
            with patch('provider_resources.recover', return_value=None), patch('provider_resources.require_resources'):
                resumed = self.step(adopted, event='resume')
            with patch('chat_runtime.answer', side_effect=AssertionError('Admitted draft must not replay')):
                done = self.until('DONE', resumed)
            self.assertTrue(done['data']['result']['admission']['admitted'])
            self.assertEqual(done['data']['result']['invocation_id'], checkpoint['candidate_id'])
            self.assertEqual(self.rows(sid)[:len(rows)], rows)
            self.assertEqual(review_budget.usage(self.s, sid)['limits'], before['limits'])
            self.assertTrue(self.s.verify_invocations(sid))


class ClosedParentOutputUpgradeTests(unittest.TestCase):
    tearDown = fixture.OrchestrationTests.tearDown
    provider = fixture.OrchestrationTests.provider
    until = fixture.OrchestrationTests.until

    def setUp(self):
        fixture.OrchestrationTests.setUp(self)
        self.ws = self.ws.resolve()
        self.meta['workspace'] = str(self.ws)
        self.chat_key = 'b' * 64
        self.prompt = 'Explain the project architecture with diagrams. ' + self.prompt

    def step(self, out=None, **kwargs):
        return fixture.OrchestrationTests.step(self, out, chat_key=self.chat_key, **kwargs)

    def closed(self, prior):
        with patch('governance.policy_hash', return_value=prior):
            self.s.close()
            self.s = InvocationLedger(self.root / 'state.sqlite')
            joined = self.until('SYNTHESIS')
            fid = joined['flow_id']
            flow.move(self.s, fid, 'DONE', 'WORKFLOW_FINISHED',
                      invocation_id='f' * 32, observation_delivery='withheld')
            self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        _, data = flow.info(self.s, fid)
        self.assertTrue(data['child_results'])
        return fid, data

    def test_exact_closed_writer_upgrade_checks_the_report_and_preserves_original_memory(self):
        prior = conversation_memory.RELEASE_POLICIES['0.57.3']
        fid, data = self.closed(prior)
        owner = machine.node(self.s, data['root_node'])
        sid = owner['session']
        conversation = owner['context']['conversation_id']
        conversation_sid = self.s.db.execute(
            'SELECT session FROM invocations WHERE id=?', (conversation,)).fetchone()[0]
        before = review_budget.usage(self.s, sid)
        before_receipts = self.s.db.execute(
            'SELECT id,record_json FROM invocation_receipts WHERE session=? ORDER BY seq', (sid,)).fetchall()
        envelope = {'logic_hash': prior, 'orchestration': {'node_id': owner['id']}}
        self.assertTrue(conversation_memory.history(self.s, conversation_sid, conversation)[0]['legacy'])
        # The old writer's transport qualification is tested in the remote
        # transport suite. Here use a qualified receipt at that boundary.
        with patch('chat_runtime._parent_output_failure', return_value=(envelope, {}, '')) as failure:
            with patch('review_upgrade.release_pair', return_value=False):
                self.assertIsNone(upgrade.parent_origin(self.s, fid, data))
                self.assertIsNone(upgrade.offer(self.s, fid, data))
            failure.assert_not_called()
            with patch('review_upgrade.release_pair', return_value=True):
                scope = upgrade.parent_origin(self.s, fid, data)
                failure.assert_called_once_with(self.s, sid, 'f' * 32)
                self.assertEqual(scope['artifact_policies'], [prior])
                with upgrade.use(self.s, scope):
                    self.assertFalse(conversation_memory.history(self.s, conversation_sid, conversation)[0]['legacy'])
                    self.assertIsNone(upgrade.conversation_policy(self.s, 'other-chat-handoff'))
                    upgrade.confirm(self.s, scope, data, closed=True)
                self.assertTrue(conversation_memory.history(self.s, conversation_sid, conversation)[0]['legacy'])

                failure.return_value = (dict(envelope, orchestration={'node_id': 'another-owner'}), {}, '')
                self.assertIsNone(upgrade.parent_origin(self.s, fid, data))
                failure.side_effect = InvalidUpdate('Unattested writer transport')
                self.assertIsNone(upgrade.parent_origin(self.s, fid, data))
                with self.s.db:
                    self.s.db.execute('INSERT INTO invocation_quarantine VALUES (?, ?, ?)',
                                      ('f' * 32, 'Fixture revoked writer', 0))
                failure.reset_mock(side_effect=True)
                self.assertIsNone(upgrade.parent_origin(self.s, fid, data))
                failure.assert_not_called()
        self.assertEqual(machine.state(self.s, fid), 'DONE')
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertEqual(self.s.db.execute(
            'SELECT id,record_json FROM invocation_receipts WHERE session=? ORDER BY seq', (sid,)).fetchall(), before_receipts)

    def test_unknown_closed_policy_is_not_a_recovery_origin(self):
        fid, data = self.closed('3' * 64)
        with patch('review_upgrade.release_pair', return_value=True), \
                patch('chat_runtime._parent_output_failure') as failure:
            self.assertIsNone(upgrade.parent_origin(self.s, fid, data))
            failure.assert_not_called()


if __name__ == '__main__':
    unittest.main()
