"""Explicit native-chat policy restart preserves an unpublished paid task."""
import copy
from contextlib import contextmanager
import json
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import conversation_memory as memory
from context_manager import InvalidUpdate
from invocation_gateway import InvocationLedger
import model_identity
import orchestration as machine
import orchestration_flow as flow
import report_review
import review_budget
import task_stack
import test_orchestration as fixture
import test_parent_recovery as parent_fixture


class TerminalReviewRecoveryTests(unittest.TestCase):
    tearDown = fixture.OrchestrationTests.tearDown
    provider = parent_fixture.ParentRecoveryTests.provider
    until = fixture.OrchestrationTests.until
    rows = parent_fixture.ParentRecoveryTests.rows
    checkpoint = parent_fixture.ParentRecoveryTests.checkpoint
    chat = 'a' * 64

    def setUp(self):
        parent_fixture.ParentRecoveryTests.setUp(self)
        self.ws = self.ws.resolve(); self.root = self.root.resolve()
        self.meta['workspace'] = str(self.ws)

    def step(self, out=None, **changes):
        changes.setdefault('chat_key', self.chat)
        if out and (out.get('recovery') or {}).get('terminal_review'):
            changes.setdefault('stack_cursor', out['recovery']['stack_cursor'])
        result = fixture.OrchestrationTests.step(self, out, **changes)
        if result.get('flow_id'):
            self.meta = flow.flow_metadata(self.root, self.ws, result['flow_id'])
        return result

    def withheld(self):
        saved, point = self.checkpoint()
        with patch('report_review.run', side_effect=InvalidUpdate('Legacy selected reviewer unavailable')):
            done = self.step(saved)
        self.assertEqual(done['state'], 'DONE')
        self.assertFalse(done['data']['result']['admission']['admitted'])
        self.assertEqual(flow.info(self.s, done['flow_id'])[1]['parent_checkpoint'], point)
        return done

    @contextmanager
    def upgrade(self):
        old = self.s.logic_hash
        with patch('governance.policy_hash', return_value='f' * 64), \
                patch.object(memory, 'LEGACY_POLICIES', memory.LEGACY_POLICIES | {old}):
            self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite')
            yield

    def lookup(self, chat=None):
        return flow.handle(self.s, self.meta, self.ws, self.root,
                           {'event': 'recoverable_review', 'chat_key': chat or self.chat})

    def test_confirmed_latest_failed_review_restarts_same_paid_task_without_mutating_done(self):
        done = self.withheld(); sid = self.meta['active']; fid = done['flow_id']
        events = machine.events(self.s, fid); parent = flow.info(self.s, fid)[1]['root_node']
        execution = machine.events(self.s, parent); rows = self.rows(sid)
        spent = review_budget.usage(self.s, sid); contract = self.s.pinned(sid)
        with self.upgrade():
            before = self.s.db.total_changes
            offered = self.lookup()
            self.assertEqual(self.s.db.total_changes, before)
            self.assertEqual(offered['flow_id'], fid)
            self.assertTrue(offered['recovery']['terminal_review'])
            self.assertEqual(offered['prompt'], self.prompt)
            with self.assertRaisesRegex(InvalidUpdate, 'explicit confirmation'):
                self.step(offered, event='restart_review')
            with self.assertRaisesRegex(InvalidUpdate, 'user-armed'):
                self.step(offered, event='restart_review', confirmed=True, armed=False)
            with self.assertRaisesRegex(InvalidUpdate, 'stale'):
                self.step(offered, event='restart_review', confirmed=True, cursor='stale')
            self.assertEqual(review_budget.usage(self.s, sid), spent)
            restarted = self.step(offered, event='restart_review', confirmed=True)
            self.assertNotEqual(restarted['flow_id'], fid)
            self.assertEqual(flow.info(self.s, restarted['flow_id'])[0]['restarted_from'], fid)
            self.assertEqual(self.meta['active'], sid)
            self.assertEqual(self.s.pinned(sid), contract)
            self.assertEqual(review_budget.usage(self.s, sid), spent)
            self.assertEqual(machine.events(self.s, fid), events)
            self.assertEqual(machine.events(self.s, parent), execution)
            self.assertEqual(self.rows(sid), rows)
            stack = task_stack.view(self.s, self.ws, self.chat)
            self.assertEqual(stack['focused'], restarted['flow_id'])
            self.assertEqual(stack['last_transition']['kernel']['decision'], 'ALLOW')
            self.assertIsNone(self.lookup().get('recovery'))  # The new open request owns focus.
            bound = self.step(restarted)
            self.assertEqual(bound['state'], 'WORKSPACE')
            current = machine.node(self.s, bound['execution_id'])
            self.assertEqual(current['session'], sid)
            self.assertEqual(current['permissions'], machine.node(self.s, parent)['permissions'])
            self.assertEqual(review_budget.usage(self.s, sid), spent)
            self.assertEqual(machine.events(self.s, fid), events)
            self.assertEqual(machine.events(self.s, parent), execution)

    def test_no_terminal_recovery_without_policy_change_or_in_another_chat(self):
        done = self.withheld()
        self.assertIsNone(self.lookup().get('flow_id'))
        with self.upgrade():
            self.assertIsNone(self.lookup('b' * 64).get('flow_id'))
            self.assertEqual(self.lookup()['flow_id'], done['flow_id'])
            current = machine.binding(self.s, self.meta['active'])
            with patch.object(machine, 'binding', return_value=dict(current, calibration_hash='e' * 64)):
                self.assertIsNone(self.lookup().get('flow_id'))

    def test_newer_restart_of_older_origin_cannot_override_latest_unpublished_intent(self):
        create = machine.create
        def equal_clock(*args, **kwargs):
            with patch.object(machine, 'time', SimpleNamespace(time=lambda: 1000.)):
                return create(*args, **kwargs)
        with patch.object(machine, 'create', side_effect=equal_clock):
            older = self.until('CHILDREN'); older_sid = self.meta['active']
            latest = self.withheld(); latest_sid = self.meta['active']; latest_fid = latest['flow_id']
            self.assertNotEqual(older_sid, latest_sid)
            self.assertEqual(task_stack.view(self.s, self.ws, self.chat)['focused'], older['flow_id'])
            events = machine.events(self.s, latest_fid)
            execution_id = flow.info(self.s, latest_fid)[1]['root_node']
            execution = machine.events(self.s, execution_id)
            spent = review_budget.usage(self.s, latest_sid); prefix = self.rows(latest_sid)
            with self.upgrade():
                # Recreate b720: a newly created policy restart of the older
                # original ask, with new governed source charges but no report.
                old_offer = flow.response(self.s, older['flow_id'])
                successor = self.step(old_offer, event='restart_review')
                self.assertEqual(flow.info(self.s, successor['flow_id'])[0]['restarted_from'], older['flow_id'])
                bound = self.step(successor)
                mapped = self.step(self.step(bound))
                flow.move(self.s, successor['flow_id'], 'PAUSED', 'BRIDGE_INTERRUPTED',
                    workflow_stop={'status': 'interrupted', 'resume_phase': mapped['state'], 'automatic_retry': False})
                older_spent = review_budget.usage(self.s, older_sid)
                self.assertGreater(older_spent['used']['source_bytes'], 0)
                offered = self.lookup()
                self.assertEqual(offered['flow_id'], latest_fid)
                self.assertEqual(offered['recovery']['parked_flow_id'], successor['flow_id'])
                self.assertEqual(machine.node(self.s, latest_fid)['created_at'], 1000.)
                self.assertEqual(machine.node(self.s, successor['flow_id'])['created_at'], 1000.)
                with self.assertRaisesRegex(InvalidUpdate, 'Task stack changed'):
                    self.step(offered, event='restart_review', confirmed=True, stack_cursor='stale')
                # The existing stack kernel refuses to park busy older work.
                old_root = flow.info(self.s, successor['flow_id'])[1]['root_node']
                with self.s.db: machine.transition(self.s, old_root, 'RUNNING', 'FIXTURE_ACTIVE_WORK')
                node_count = self.s.db.execute('SELECT COUNT(*) FROM orchestration_nodes').fetchone()[0]
                with self.assertRaisesRegex(InvalidUpdate, 'active work'):
                    self.step(offered, event='restart_review', confirmed=True)
                self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM orchestration_nodes').fetchone()[0], node_count)
                with self.s.db: machine.transition(self.s, old_root, 'READY', 'FIXTURE_WORK_SETTLED')
                restarted = self.step(offered, event='restart_review', confirmed=True)
                self.assertEqual(flow.info(self.s, restarted['flow_id'])[0]['restarted_from'], latest_fid)
                self.assertEqual(self.meta['active'], latest_sid)
                self.assertEqual(review_budget.usage(self.s, latest_sid), spent)
                self.assertEqual(review_budget.usage(self.s, older_sid), older_spent)
                self.assertEqual(self.rows(latest_sid), prefix)
                self.assertEqual(machine.state(self.s, successor['flow_id']), 'PAUSED')
                self.assertEqual(machine.events(self.s, latest_fid), events)
                self.assertEqual(machine.events(self.s, execution_id), execution)

    def test_newer_ask_or_an_open_focused_task_blocks_old_terminal_restart(self):
        done = self.withheld(); sid = self.meta['active']
        with self.upgrade():
            offered = self.lookup()
            new = self.step(event='start', prompt='A newer workspace architecture question.')
            self.assertNotEqual(self.lookup().get('flow_id'), done['flow_id'])
            with self.assertRaises(InvalidUpdate): self.step(offered, event='restart_review', confirmed=True)
            if new['state'] not in machine.TERMINAL: self.step(new, event='cancel')
            self.assertIsNone(self.lookup().get('flow_id'))
            self.assertEqual(machine.state(self.s, done['flow_id']), 'DONE')

    def test_published_done_cancelled_and_changed_contract_do_not_recover(self):
        saved, _ = self.checkpoint(); done = self.until('DONE', saved)
        self.assertTrue(done['data']['result']['admission']['admitted'])
        with self.upgrade(): self.assertIsNone(self.lookup().get('flow_id'))

    def test_cancelled_request_does_not_offer_terminal_recovery(self):
        saved, _ = self.checkpoint()
        cancelled = self.step(saved, event='cancel')
        self.assertEqual(cancelled['state'], 'CANCELLED')
        with self.upgrade(): self.assertIsNone(self.lookup().get('flow_id'))

    def test_changed_contract_blocks_failed_terminal_restart(self):
        done = self.withheld(); sid = self.meta['active']
        from vscode_bridge import save_contract
        save_contract(self.s, sid, {'goal': 'A different task', 'constraints': self.s.snapshot(sid)['constraints']}, self.ws)
        with self.upgrade():
            self.assertIsNone(self.lookup().get('flow_id'))
            self.assertIsNone(flow.recovery(self.s, done['flow_id']))

    def test_uncertain_missing_foreign_or_cyclic_original_ask_fails_closed(self):
        machine.initialize(self.s)
        with self.s.db:
            foreign = machine.create(self.s, 'flow', workspace=str(self.ws), chat_key='b' * 64, prompt=self.prompt)
            child = machine.create(self.s, 'flow', workspace=str(self.ws), chat_key=self.chat,
                                   prompt=self.prompt, restarted_from=foreign['id'])
        with self.assertRaisesRegex(InvalidUpdate, 'original request or chat'):
            flow._original_ask(self.s, child)
        with self.assertRaises(InvalidUpdate):
            flow._original_ask(self.s, dict(child, restarted_from='0' * 32))
        cyclic = dict(child, restarted_from=child['id'])
        with patch.object(machine, 'node', return_value=cyclic), self.assertRaisesRegex(InvalidUpdate, 'cycle'):
            flow._original_ask(self.s, cyclic)

    def test_reviewer_metadata_outage_pauses_without_page_retry_and_resumes_same_candidate(self):
        saved, point = self.checkpoint(); sid = self.meta['active']; rows = self.rows(sid)
        before = review_budget.usage(self.s, sid); original = report_review.run
        def real_selection(*args, **kwargs):
            kwargs['provider'] = None
            return original(*args, **kwargs)
        with patch('report_review.run', side_effect=real_selection), \
                patch('report_review.choose', side_effect=model_identity.ReviewerUnavailable('auto',
                    [{'model': 'strata:high', 'status': 'unavailable', 'stage': 'identity', 'error_kind': 'InvalidUpdate'}])):
            paused = self.step(saved)
        self.assertEqual(paused['state'], 'PAUSED')
        self.assertEqual(paused['stop']['status'], 'reviewer_unavailable')
        data = flow.info(self.s, paused['flow_id'])[1]
        self.assertEqual(data['parent_checkpoint'], point)
        self.assertIsNone(data.get('claim_review_retry'))
        self.assertEqual(self.rows(sid), rows)
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite'); self.tier = 'high'
        with patch('chat_runtime.answer', side_effect=AssertionError('Do not replay the paid draft')):
            done = self.until('DONE', self.step(paused, event='resume'))
        self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertEqual(done['data']['result']['invocation_id'], point['candidate_id'])
        self.assertEqual(self.rows(sid)[:len(rows)], rows)
        self.assertTrue(self.s.verify_invocations(sid))


if __name__ == '__main__': unittest.main()
