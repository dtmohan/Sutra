import tempfile
import unittest
from pathlib import Path

from context_manager import InvalidUpdate
from invocation_gateway import InvocationLedger
import orchestration
import task_stack


class TaskStackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name) / 'workspace'
        self.path = Path(temporary.name) / 'ledger.sqlite'
        self.s = InvocationLedger(self.path)
        self.addCleanup(lambda: self.s.close())
        self.chat_key = 'chat-a'

    def flow(self, prompt, workspace=None):
        with self.s.db:
            flow = orchestration.create(self.s, 'flow', workspace=str(workspace or self.workspace), prompt=prompt)
            orchestration.transition(self.s, flow['id'], 'CLASSIFYING', 'REQUEST')
            orchestration.transition(self.s, flow['id'], 'NEED_ARM', 'CLASSIFIED')
        return flow['id']

    def view(self):
        return task_stack.view(self.s, self.workspace, self.chat_key)

    def select(self, flow_id, *, attach=False):
        with self.s.db:
            return task_stack.select(self.s, self.workspace, self.chat_key, flow_id, self.view()['cursor'], attach=attach)

    def close(self, flow_id):
        with self.s.db:
            return task_stack.close(self.s, self.workspace, self.chat_key, flow_id, self.view()['cursor'])

    def assert_order(self, projection, *flow_ids):
        self.assertEqual([row['flow_id'] for row in projection['open_tasks']], list(flow_ids))
        self.assertEqual(projection['focused'], flow_ids[0] if flow_ids else None)
        self.assertEqual([row['focused'] for row in projection['open_tasks']], [True] + [False] * (len(flow_ids) - 1) if flow_ids else [])

    def test_index_reloads_and_selects_or_closes_out_of_order(self):
        a, b, c = (self.flow(title) for title in ('Architecture', 'Résumé', 'Timing'))
        first = self.select(a, attach=True)
        cursor = first['open_tasks'][0]['cursor']
        self.select(b, attach=True)
        self.assert_order(self.select(c, attach=True), c, b, a)
        self.s.close()
        self.s = InvocationLedger(self.path)
        self.assert_order(self.view(), c, b, a)
        with self.assertRaisesRegex(InvalidUpdate, 'parked'):
            task_stack.assert_focused(self.s, self.workspace, self.chat_key, a)
        returned = self.select(a)
        self.assert_order(returned, a, c, b)
        self.assertEqual(returned['last_transition']['kernel']['decision'], 'ALLOW')
        self.assertEqual(returned['open_tasks'][0]['cursor'], cursor)
        self.assert_order(self.close(b), a, c)
        self.assert_order(self.close(a), c)
        self.assertEqual(task_stack.owner(self.s, self.workspace, b), self.chat_key)
        self.assertFalse(self.s.db.execute('SELECT 1 FROM orchestration_charges').fetchone())

    def test_terminal_flow_is_hidden_and_can_be_closed_after_reload(self):
        a, b = self.flow('Old'), self.flow('New')
        self.select(a, attach=True)
        self.select(b, attach=True)
        with self.s.db:
            orchestration.transition(self.s, b, 'CANCELLED', 'CANCELLED')
        self.s.close()
        self.s = InvocationLedger(self.path)
        self.assert_order(self.view(), a)
        self.assert_order(self.close(b), a)
        self.assertEqual(self.view()['last_transition']['flow_id'], b)

    def test_binding_guards_leave_the_focus_and_ledgers_untouched(self):
        a, b = self.flow('Old'), self.flow('New')
        self.select(a, attach=True)
        saved = self.view()
        with self.assertRaises(task_stack.TaskStackStop) as stale, self.s.db:
            task_stack.select(self.s, self.workspace, self.chat_key, b, None, attach=True)
        self.assertEqual(stale.exception.frame['decision'], 'FIN')
        with self.assertRaises(task_stack.TaskStackStop) as wrong_chat, self.s.db:
            task_stack.select(self.s, self.workspace, 'chat-A', a, None, attach=True)
        self.assertEqual(wrong_chat.exception.frame['decision'], 'FIN')
        wrong_workspace = self.flow('Other workspace', self.workspace / 'other')
        with self.assertRaisesRegex(InvalidUpdate, 'this workspace'), self.s.db:
            task_stack.select(self.s, self.workspace, self.chat_key, wrong_workspace, saved['cursor'], attach=True)
        self.assertEqual(self.view(), saved)

    def test_open_action_blocks_switching_and_close_until_settled(self):
        a, b = self.flow('Edit'), self.flow('Review')
        self.select(a, attach=True)
        with self.s.db:
            for phase in ('BINDING', 'WORKSPACE', 'CODING', 'AWAITING_ACTION', 'EXECUTING_ACTION'):
                orchestration.transition(self.s, a, phase, 'TEST_FIXTURE')
        for change in (lambda: self.select(b, attach=True), lambda: self.close(a)):
            with self.assertRaises(task_stack.TaskStackStop) as stopped:
                change()
            self.assertEqual(stopped.exception.frame['decision'], 'BRIDGE')
        self.assert_order(self.view(), a)
        with self.s.db:
            orchestration.transition(self.s, a, 'DONE', 'WORKFLOW_FINISHED')
        self.assert_order(self.select(b, attach=True), b)

    def test_failed_workflow_close_rolls_back_registry_close(self):
        a = self.flow('Edit')
        previous = self.select(a, attach=True)
        with self.assertRaisesRegex(RuntimeError, 'failure'), self.s.db:
            task_stack.close(self.s, self.workspace, self.chat_key, a, previous['cursor'])
            raise RuntimeError('simulated workflow close failure')
        self.assertEqual(self.view(), previous)
