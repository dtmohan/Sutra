import copy
import sqlite3
import unittest
from unittest.mock import patch

import allocation_recovery as a
import orchestration as m
import orchestration_flow as flow
import review_budget
import test_orchestration as fixtures
from context_manager import ContextBudgetExceeded, InvalidUpdate, canonical
from invocation_gateway import InvocationLedger


class AllocationTests(unittest.TestCase):
    setUp = fixtures.OrchestrationTests.setUp
    tearDown = fixtures.OrchestrationTests.tearDown
    provider = fixtures.OrchestrationTests.provider
    step = fixtures.OrchestrationTests.step
    until = fixtures.OrchestrationTests.until

    def pause(self):
        out = self.until('SYNTHESIS')
        with patch('chat_runtime.answer', side_effect=ContextBudgetExceeded('Pinned context cannot fit')):
            return self.step(out)

    def preview(self, out, **kw):
        return self.step(out, event='allocation_preview', armed=False, **kw)

    def grant(self, out, **kw):
        return self.step(out, event='allocation_grant', armed=False,
                         proposal=out['allocation_proposal'], **kw)

    def test_preview_confirmation_reload_and_model_switch_preserve_task(self):
        out = self.pause(); sid = self.meta['active']
        before = review_budget.usage(self.s, sid); contract = self.s.pinned(sid)
        events = m.events(self.s, out['flow_id']); calls = len(self.models)
        preview = self.preview(out)
        self.assertEqual(m.events(self.s, out['flow_id']), events)
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertEqual(len(self.models), calls)
        with self.assertRaisesRegex(InvalidUpdate, 'explicit user confirmation'):
            self.grant(preview)
        approved = self.grant(preview, confirmed=True)
        budget = review_budget.usage(self.s, sid)
        self.assertEqual(approved['state'], 'PAUSED')
        self.assertEqual(budget['used'], before['used'])
        self.assertEqual(budget['limits'], preview['allocation_proposal']['after'])
        self.assertEqual(budget['base_limits'], before['limits'])
        self.assertEqual(budget['allocation_version'], 1)
        self.assertEqual(self.s.pinned(sid), contract)
        self.assertEqual(len(self.models), calls)
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.db.execute('DELETE FROM allocation_amendments')
        self.s.db.rollback()
        self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite')
        self.assertEqual(review_budget.usage(self.s, sid), budget)
        self.tier = 'high'
        resumed = self.step(approved, event='resume')
        self.assertEqual(resumed['conversation_id'], out['conversation_id'])
        done = self.step(resumed)
        self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertEqual(self.s.pinned(sid), contract)
        self.assertGreater(review_budget.usage(self.s, sid)['used']['calls'], before['used']['calls'])
        model = [p for stage, p in self.models if stage == 'synthesis'][-1]
        self.assertEqual(model.model, fixtures.MODELS['high'])
        self.assertIn('CED724', canonical(model.payloads[-1]['memory']))
        self.assertEqual(model.payloads[-1]['contract'], contract)
        self.assertTrue(self.s.audit_valid())

    def test_stale_tampered_and_replayed_proposals_do_not_grant(self):
        out = self.pause(); preview = self.preview(out)
        forged = copy.deepcopy(preview); forged['allocation_proposal']['after']['tokens'] += 1
        with self.assertRaisesRegex(InvalidUpdate, 'stale or changed'):
            self.grant(forged, confirmed=True)
        approved = self.grant(preview, confirmed=True)
        budget = review_budget.usage(self.s, self.meta['active'])
        with self.assertRaisesRegex(InvalidUpdate, 'stale or replayed'):
            self.grant(preview, confirmed=True)
        with self.assertRaisesRegex(InvalidUpdate, 'stale or changed'):
            self.grant(dict(approved, allocation_proposal=preview['allocation_proposal']), confirmed=True)
        self.assertEqual(review_budget.usage(self.s, self.meta['active']), budget)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM allocation_amendments').fetchone()[0], 1)

    def test_changed_spending_or_policy_invalidates_reviewed_preview(self):
        out = self.pause(); preview = self.preview(out)
        before = review_budget.usage(self.s, self.meta['active'])
        changed = copy.deepcopy(before); changed['used']['tokens'] += 1
        with patch('review_budget.usage', return_value=changed):
            with self.assertRaisesRegex(InvalidUpdate, 'stale or changed'):
                self.grant(preview, confirmed=True)
        old = self.s.logic_hash
        try:
            self.s.logic_hash = 'f' * 64
            with self.assertRaisesRegex(InvalidUpdate, 'stale or changed'):
                self.grant(preview, confirmed=True)
        finally:
            self.s.logic_hash = old
        self.assertEqual(review_budget.usage(self.s, self.meta['active']), before)

    def test_budget_increase_does_not_widen_child_limits_or_permissions(self):
        out = self.pause(); sid = self.meta['active']
        parent = flow.info(self.s, out['flow_id'])[1]['root_node']
        nodes = [m.node(self.s, nid) for nid in m.descendants(self.s, parent)]
        prior = m.effective_limits(self.s, nodes[0])
        preview = self.preview(out, additions={'tokens': 50000})
        approved=self.grant(preview, confirmed=True)
        successor=m.node(self.s,approved['execution_id'])
        self.assertEqual(m.effective_limits(self.s,nodes[0]),prior)
        self.assertEqual(m.state(self.s,parent),'DONE')
        self.assertEqual(successor['previous'],parent)
        self.assertEqual(successor['limits']['tokens'],prior['tokens']-m.spent(self.s,parent)['tokens']+50000)
        for n in nodes[1:]:
            self.assertEqual(m.effective_limits(self.s, n), n['limits'])
        self.assertEqual([m.node(self.s, n['id']) for n in nodes], nodes)
        self.assertFalse(m.permissions(self.s, sid)['network_allowed'])
        self.assertEqual(m.permissions(self.s, sid)['write_paths'], [])

    def test_child_increase_keeps_checkpoint_reservations_and_spending(self):
        from paging import Budget
        from review_context import ReviewContextExceeded

        # The forced continuation must have an unread source page after the
        # first accepted span; the shared two-line fixture is already exhausted.
        for index in range(4):
            body = ''.join(f'    value += {line}\n' for line in range(180))
            (self.ws / f'part{index}.py').write_text(
                f'def part{index}():\n    value = {index}\n' + body + '    return value\n')
        out = self.until('CHILDREN'); sid = self.meta['active']
        child, sibling = [row['id'] for row in out['children']]
        execution = out['execution_id']
        original_call = fixtures.Child.call

        def checkpoint(worker, *args):
            result, receipt = original_call(worker, *args)
            result['slice_status'] = 'continue'
            return result, receipt

        with patch.object(fixtures.Child, 'call', checkpoint):
            out = self.step(out)
        delivered = self.models[-1][1].payloads[0]
        self.assertTrue(any(span['next_line'] <= len((self.ws / path).read_text().splitlines())
                            for path, span in delivered['excerpt_spans'].items()))
        cursor = m.slice_cursor(self.s, child)
        checkpoint = m.slice_history(self.s, child, cursor)
        spent = {ident: m.spent(self.s, ident) for ident in (child, sibling, execution)}
        limits = {ident: m.effective_limits(self.s, m.node(self.s, ident)) for ident in spent}
        contract = self.s.pinned(sid)
        left = int(limits[child]['tokens'] - spent[child]['tokens'])
        required = left + 300
        sizing = {'available_tokens': left, 'effective_context_tokens': required + 1000,
                  'branch_id': child, 'task_allocation_need': {
                      'dimension': 'tokens', 'required_context': required, 'slots': 1}}
        error = ReviewContextExceeded(sizing, Budget(left, 2048, 384), 'Measured minimum')
        with patch('subagent_runtime.run', side_effect=error):
            out = self.step(out)
        self.assertEqual(out['stop']['child_context']['branch_id'], child)
        with self.assertRaisesRegex(InvalidUpdate, 'Only the measured child token'):
            self.preview(out, additions={'calls': 1})
        preview = self.preview(out)
        amount = preview['allocation_proposal']['additions']['tokens']
        self.assertEqual(amount, 300)
        approved = self.grant(preview, confirmed=True)
        self.assertEqual(approved['execution_id'], execution)
        self.assertEqual(approved['stop']['child_allocation_granted'], amount)
        self.assertEqual([row['id'] for row in approved['children']], [child, sibling])
        for ident in spent:
            expected = dict(limits[ident])
            if ident != sibling:
                expected['tokens'] += amount
            self.assertEqual(m.effective_limits(self.s, m.node(self.s, ident)), expected)
            self.assertEqual(m.spent(self.s, ident), spent[ident])
        self.assertEqual(m.slice_history(self.s, child, cursor), checkpoint)
        with self.assertRaisesRegex(InvalidUpdate, 'already has the measured capacity'):
            self.preview(approved)
        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        self.tier = 'high'
        resumed = self.step(approved, event='resume')
        done = self.until('DONE', resumed)
        worker = next(worker for stage, worker in self.models
                      if stage == 'subagent' and worker.model == fixtures.MODELS['high'])
        self.assertEqual(worker.payloads[0]['child_slice'], cursor)
        self.assertEqual(self.s.pinned(sid), contract)
        self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertTrue(self.s.verify_invocations(sid))
        self.assertTrue(self.s.audit_valid())

    def test_invalid_additions_active_work_and_nonbudget_stops_rejected(self):
        out = self.pause(); budget = review_budget.usage(self.s, self.meta['active'])
        for changes in ({'tokens': -1}, {'tokens': True}, {'tokens': 1.5}, {'tokens': 1000000},
                        {'network_allowed': 1}, {'tokens': 0}, {}):
            with self.subTest(changes=changes), self.assertRaises(InvalidUpdate):
                self.preview(out, additions=changes)
        parent = flow.info(self.s, out['flow_id'])[1]['root_node']
        with patch.object(m, 'state', side_effect=lambda s, nid: 'RUNNING' if nid == parent else 'PAUSED'):
            with self.assertRaisesRegex(InvalidUpdate, 'active task'):
                self.preview(out)
        flow.move(self.s, out['flow_id'], 'PAUSED', 'TEST_STOP', workflow_stop={'status': 'resource_wait'})
        with self.assertRaisesRegex(InvalidUpdate, 'does not request task allocation'):
            self.preview(flow.response(self.s, out['flow_id']))
        self.assertEqual(review_budget.usage(self.s, self.meta['active']), budget)

    def test_policy_restart_preserves_allocation_and_usage(self):
        import conversation_memory as cm
        out = self.pause(); sid = self.meta['active']; contract = self.s.pinned(sid)
        old = self.s.logic_hash
        with patch('governance.policy_hash', return_value='f' * 64), patch.object(cm, 'LEGACY_POLICIES', cm.LEGACY_POLICIES | {old}):
            self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite')
            preview = self.preview(out)
            approved = self.grant(preview, confirmed=True)
            before = review_budget.usage(self.s, sid)
            restarted = self.step(approved, event='restart_review')
            self.assertEqual(review_budget.usage(self.s, sid), before)
            self.tier = 'high'; done = self.until('DONE', restarted)
            self.assertTrue(done['data']['result']['admission']['admitted'])
            self.assertEqual(self.s.pinned(sid), contract)
            after = review_budget.usage(self.s, sid)
            self.assertEqual(after['limits'], before['limits'])
            self.assertGreater(after['used']['tokens'], before['used']['tokens'])
            self.assertIn('CED724', canonical(self.models[-1][1].payloads[-1]['memory']))

    def test_task_budget_stop_is_recoverable_without_model_dispatch(self):
        out = self.until('SYNTHESIS')
        with patch('chat_runtime.answer', side_effect=review_budget.BudgetStop({'decision': 'FIN', 'limiting_dimension': 'tokens'})):
            out = self.step(out)
        self.assertEqual(out['stop']['status'], 'task_budget')
        self.assertIn('allocation_proposal', self.preview(out))

    def test_failed_commit_rolls_back_amendment_and_receipts_together(self):
        out = self.pause(); preview = self.preview(out)
        sid = self.meta['active']; before = review_budget.usage(self.s, sid)
        history = m.events(self.s, out['flow_id']); original = m.transition
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('simulated interruption before commit')
        with patch.object(m, 'transition', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'simulated interruption'):
                self.grant(preview, confirmed=True)
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertEqual(m.events(self.s, out['flow_id']), history)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM allocation_amendments').fetchone()[0], 0)
        self.assertEqual(self.grant(preview, confirmed=True)['allocation_amendment']['version'], 1)

    def test_unsettled_work_blocks_grant_even_when_node_is_ready(self):
        out = self.pause(); sid = self.meta['active']
        root = flow.info(self.s, out['flow_id'])[1]['root_node']
        with self.s.db:
            with m.scope(root):m.admit(self.s, sid, 'model', 'interrupted', {'tokens': 100, 'calls': 1})
            m.transition(self.s, root, 'READY', 'TEST_INCOMPLETE_RECOVERY')
        with self.assertRaisesRegex(InvalidUpdate, 'interrupted work'):
            self.preview(out)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM allocation_amendments').fetchone()[0], 0)

    def test_repository_gate_uses_accepted_total_and_retains_count(self):
        from repository_tools import usage
        out = self.pause(); sid = self.meta['active']
        before = usage(self.s, sid)
        preview = self.preview(out, additions={'operations': 100})
        self.grant(preview, confirmed=True)
        after = usage(self.s, sid)
        self.assertEqual(after['used'], before['used'])
        self.assertEqual(after['limit'], before['limit'] + 100)


if __name__ == '__main__':
    unittest.main()
