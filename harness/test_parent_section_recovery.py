"""Adopt an exact closed writer failure without reopening its paid execution.

The gateway and accounting fixtures are real. Native RTX transport qualification
is mocked only at its already tested identity boundary; these tests make no
claim about a live model, tokenizer or remote-memory observation.
"""
from contextlib import contextmanager, nullcontext
import copy
import json
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate
from invocation_gateway import InvocationLedger
import orchestration as machine
import orchestration_flow as flow
import parent_synthesis
import review_budget
import review_upgrade
import test_orchestration as fixture
import test_parent_output_policy as output


class ParentSectionRecoveryTests(unittest.TestCase):
    setUp = output.ParentOutputPolicyTests.setUp
    tearDown = fixture.OrchestrationTests.tearDown
    provider = output.ParentOutputPolicyTests.provider
    rows = output.ParentOutputPolicyTests.rows
    until = fixture.OrchestrationTests.until

    def step(self, out=None, **changes):
        changes.setdefault('chat_key', 'c' * 64)
        if out and (out.get('recovery') or {}).get('terminal_review'):
            changes.setdefault('stack_cursor', out['recovery']['stack_cursor'])
        result = fixture.OrchestrationTests.step(self, out, **changes)
        if result.get('flow_id'):
            self.meta = flow.flow_metadata(self.root, self.ws, result['flow_id'])
        return result

    @contextmanager
    def qualified_transport(self):
        with patch('remote_profiles.is_remote', return_value=True), \
                patch('remote_context.supports_parent_output_recovery', return_value=True):
            yield

    def closed(self, *, children=False, policy=None, no_git=True):
        self.ws = self.ws.resolve()
        self.root = self.root.resolve()
        self.meta['workspace'] = str(self.ws)
        prior = policy or review_upgrade.SECTION_SYNTHESIS_PREDECESSOR
        self.s.close()
        with patch('governance.policy_hash', return_value=prior):
            self.s = InvocationLedger(self.root / 'state.sqlite')
            self.failed_writer_calls = 3
            provenance = patch('orchestration_flow.review_provenance', return_value=(None, None)) if no_git else nullcontext()
            with provenance:
                if children:
                    done = self.until('DONE')
                else:
                    def no_fork(store, meta, workspace, prompt, conversation_id, parent_id=None):
                        return machine.checked(store, parent_id), [], {'policy': 'fixture-no-child'}
                    with patch('orchestration_flow.plan', side_effect=no_fork):
                        done = self.until('DONE')
            self.assertFalse(done['data']['result']['admission']['admitted'])
            self.assertIsNone(done['data']['report_markdown'])
            self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        self.meta = flow.flow_metadata(self.root, self.ws, done['flow_id'])
        return done

    def test_exact_closed_zero_child_and_joined_failures_continue_without_refund(self):
        for children in (False, True):
            with self.subTest(children=children):
                if children:
                    self.tearDown()
                    self.setUp()
                done = self.closed(children=children, no_git=not children)
                fid, sid = done['flow_id'], self.meta['active']
                old_flow, data = flow.info(self.s, fid)
                old_root = machine.node(self.s, data['root_node'])
                original_events = machine.events(self.s, fid)
                original_execution = machine.events(self.s, old_root['id'])
                original_rows = self.rows(sid)
                consumed = review_budget.usage(self.s, sid)
                contract = self.s.pinned(sid)
                with patch('review_upgrade.release_pair', return_value=True), self.qualified_transport():
                    before = self.s.db.total_changes
                    notice = flow.response(self.s, fid)
                    self.assertEqual(self.s.db.total_changes, before)
                    self.assertEqual(notice['recovery']['kind'], 'parent_output_recovery')
                    self.assertIn('bounded sections', notice['recovery']['message'])
                    for changes in ({'confirmed': False}, {'armed': False}, {'stack_cursor': 'stale'}):
                        with self.assertRaises(InvalidUpdate):
                            self.step(notice, **dict({'event': 'continue_parent_report', 'confirmed': True}, **changes))
                    continued = self.step(notice, event='continue_parent_report', confirmed=True)
                    self.assertEqual(continued['state'], 'SYNTHESIS')
                    self.assertEqual(continued['continued_from'], fid)
                    self.assertNotEqual(continued['execution_id'], old_root['id'])
                    _, active = flow.info(self.s, continued['flow_id'])
                    stamp = review_upgrade.recorded(self.s, continued['flow_id'])
                    self.assertEqual(active['review_policy_upgrade'], stamp)
                    self.assertEqual(stamp['section_output_invocation'], done['data']['result']['invocation_id'])
                    self.assertEqual(active['section_plan']['failure'], stamp['section_output_invocation'])
                    self.assertEqual(active['section_plan']['source'], stamp['section_original_invocation'])
                    new_root = machine.node(self.s, continued['execution_id'])
                    self.assertEqual(new_root['parent_id'], old_root['parent_id'])
                    self.assertEqual(new_root['previous'], old_root['id'])
                    self.assertEqual(new_root['context'], old_root['context'])
                    self.assertEqual(new_root['permissions'], old_root['permissions'])
                    self.assertEqual(active.get('child_results', []), data.get('child_results', []))
                    self.assertEqual(active.get('git_provenance'), data.get('git_provenance'))
                    self.assertEqual(review_budget.usage(self.s, sid), consumed)
                    self.assertEqual(self.s.pinned(sid), contract)
                    self.assertEqual(self.rows(sid), original_rows)
                    self.assertEqual(machine.events(self.s, fid), original_events)
                    self.assertEqual(machine.events(self.s, old_root['id']), original_execution)
                    self.assertEqual(machine.state(self.s, old_root['id']), 'DONE')
                    self.assertIsNone(parent_synthesis.offer(self.s, old_flow, data))
                    self.s.close()
                    self.s = InvocationLedger(self.root / 'state.sqlite')
                    self.tier = 'high'
                    self.assertEqual(review_upgrade.recorded(self.s, continued['flow_id']), stamp)
                    self.assertEqual(review_budget.usage(self.s, sid), consumed)
                    self.assertTrue(self.s.verify_invocations(sid))
                    self.assertTrue(self.s.audit_valid())

    def test_unknown_policy_and_unpackaged_successor_are_not_eligible(self):
        done = self.closed()
        n, data = flow.info(self.s, done['flow_id'])
        with patch('review_upgrade.release_pair', return_value=False), self.qualified_transport():
            self.assertIsNone(parent_synthesis.offer(self.s, n, data))
        with patch('review_upgrade.SECTION_SYNTHESIS_PREDECESSOR', '0' * 64), \
                patch('review_upgrade.release_pair', return_value=True), self.qualified_transport():
            self.assertIsNone(parent_synthesis.offer(self.s, n, data))

    def test_unchanged_current_policy_is_eligible_without_a_release_exception(self):
        current = self.s.logic_hash
        done = self.closed(policy=current)
        n, data = flow.info(self.s, done['flow_id'])
        with patch('review_upgrade.release_pair', return_value=False), self.qualified_transport():
            self.assertIsNotNone(parent_synthesis.offer(self.s, n, data))

    def test_permission_revocation_and_calibration_changes_refuse_adoption(self):
        done = self.closed()
        n, data = flow.info(self.s, done['flow_id'])
        sid = self.meta['active']
        spent = review_budget.usage(self.s, sid)
        permissions = machine.permissions(self.s, sid)
        binding = machine.binding(self.s, sid)
        with patch('review_upgrade.release_pair', return_value=True), self.qualified_transport():
            with patch('orchestration.permissions', return_value=dict(permissions, read_paths=[])):
                self.assertIsNone(parent_synthesis.offer(self.s, n, data))
            with patch('orchestration.binding', return_value=dict(binding, calibration_hash='0' * 64)):
                self.assertIsNone(parent_synthesis.offer(self.s, n, data))
        self.assertEqual(review_budget.usage(self.s, sid), spent)

    def test_receipt_revocation_and_unrecorded_children_fail_closed(self):
        done = self.closed()
        fid, sid = done['flow_id'], self.meta['active']
        n, data = flow.info(self.s, fid)
        spent = review_budget.usage(self.s, sid)
        with patch('review_upgrade.release_pair', return_value=True), self.qualified_transport():
            stamp = review_upgrade.section_origin(self.s, fid, data)
            self.assertIsNotNone(stamp)
            with self.s.db:
                machine.create(self.s, 'branch', sid, data['root_node'], binding=stamp['previous'])
            self.assertIsNone(parent_synthesis.offer(self.s, n, data))
        self.assertEqual(review_budget.usage(self.s, sid), spent)

    def test_revoked_original_compact_receipt_is_not_adopted(self):
        done = self.closed()
        n, data = flow.info(self.s, done['flow_id'])
        with patch('review_upgrade.release_pair', return_value=True), self.qualified_transport():
            stamp = review_upgrade.section_origin(self.s, done['flow_id'], data)
            self.assertTrue(stamp['section_original_invocation'])
            with self.s.db:
                self.s.db.execute('INSERT INTO invocation_quarantine VALUES (?, ?, ?)',
                                  (stamp['section_original_invocation'], 'Fixture revoked original writer', 0))
            self.assertIsNone(parent_synthesis.offer(self.s, n, data))

    def test_plan_or_scope_changes_are_not_recorded_authority(self):
        done = self.closed()
        with patch('review_upgrade.release_pair', return_value=True), self.qualified_transport():
            notice = flow.response(self.s, done['flow_id'])
            continued = self.step(notice, event='continue_parent_report', confirmed=True)
            fid = continued['flow_id']
            _, data = flow.info(self.s, fid)
            self.assertIsNotNone(review_upgrade.recorded(self.s, fid, data))
            altered = copy.deepcopy(data)
            altered['section_plan']['source'] = 'f' * 32
            self.assertIsNone(review_upgrade.recorded(self.s, fid, altered))
            altered = copy.deepcopy(data)
            altered['review_policy_upgrade']['section_output_receipt'] = '0' * 64
            self.assertIsNone(review_upgrade.recorded(self.s, fid, altered))

    def test_changed_source_refuses_no_git_activation_without_charging_or_linking(self):
        done = self.closed()
        fid, sid = done['flow_id'], self.meta['active']
        _, data = flow.info(self.s, fid)
        self.assertNotIn('git_provenance', data)
        spent = review_budget.usage(self.s, sid)
        with patch('review_upgrade.release_pair', return_value=True), self.qualified_transport():
            notice = flow.response(self.s, fid)
            (self.ws / 'part0.py').write_text('def part0():\n    return 777\n')
            with self.assertRaisesRegex(InvalidUpdate, 'source changed'):
                self.step(notice, event='continue_parent_report', confirmed=True)
        self.assertEqual(review_budget.usage(self.s, sid), spent)
        self.assertEqual(machine.state(self.s, fid), 'DONE')
        self.assertFalse(self.s.db.execute(
            "SELECT 1 FROM orchestration_nodes WHERE json_extract(record_json,'$.parent_synthesis_predecessor.flow_id')=?",
            (fid,)).fetchone())


if __name__ == '__main__':
    unittest.main()
