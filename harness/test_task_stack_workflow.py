import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from context_manager import ContextBudgetExceeded, InvalidUpdate
from invocation_gateway import InvocationLedger
import orchestration as machine
import orchestration_flow as workflow
import review_budget
from test_chat import Fake as Reviewer
from test_conversation import Fake as Router, MODELS
from test_host_actions import SOURCE
from test_native_runtime import NativeModel
from test_orchestration import Child
from vscode_bridge import handle as bridge


class OpenTaskWorkflowTests(unittest.TestCase):
    chat_key = 'a' * 64
    other_chat = 'b' * 64
    review_ask = 'Review the Cedar architecture. Keep Cedar issue CED724 read only. No network or new dependencies.'
    edit_ask = 'Fix the retry off-by-one in src/retry.py for Fir issue FIR821.'
    third_ask = 'Review the Maple project for MAP263. Keep the task read only.'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.workspace, self.storage = base / 'repository', base / 'storage'
        self.workspace.mkdir()
        self.storage.mkdir()
        for i in range(4):
            (self.workspace / f'part{i}.py').write_text(f'def part{i}():\n    return {i}\n')
        (self.workspace / 'src').mkdir()
        (self.workspace / 'src/retry.py').write_text(SOURCE)
        self.metadata = {'workspace': str(self.workspace)}
        self.ledger = InvocationLedger(self.storage / 'state.sqlite')
        self.addCleanup(lambda: self.ledger.close())
        self.tier = 'low'
        self.models = []
        self.coder_action = 'edit'

    def provider(self, stage):
        name = MODELS[self.tier]
        if stage == 'conversation':
            def route(result, packet):
                prompt = packet['request']
                result['intent'] = 'workspace'
                if prompt == self.edit_ask:
                    result['workspace_request'] = {'operation': 'edit', 'paths': ['src/retry.py']}
            model = Router(name, route)
        elif stage == 'subagent':
            model = Child(name)
        elif stage == 'coder':
            model = NativeModel(name, self.coder_action)
        else:
            model = Reviewer(name)
        self.models.append((stage, model))
        return model

    def request(self, event, flow=None, **fields):
        request = {'event': event, 'chat_key': self.chat_key, 'tier': self.tier,
                   'models': MODELS, 'armed': True}
        if flow is not None:
            request.update(flow_id=flow['flow_id'], cursor=flow['cursor'])
            binding = flow.get('binding') or flow.get('bound_task')
            if binding is not None:
                request['binding'] = binding
        request.update(fields)
        return workflow.handle(self.ledger, self.metadata, self.workspace, self.storage, request, self.provider)

    def advance_until(self, phase, flow):
        for _ in range(30):
            if flow['state'] == phase:
                return flow
            if flow['state'] in machine.TERMINAL or flow['state'] == 'PAUSED':
                admission = flow.get('conversation', {}).get('result', {}).get('admission')
                self.fail(f"Expected {phase}, got {flow['state']} ({flow.get('stop') or admission})")
            flow = self.request('step', flow)
        self.fail(f'Workflow failed to reach {phase}: {flow}')

    def index(self, chat_key=None):
        from task_stack import view
        return view(self.ledger, self.workspace, chat_key or self.chat_key)

    def focus(self, flow):
        return self.request('task_focus', flow, stack_cursor=self.index()['cursor'])

    def close_task(self, flow):
        return self.request('task_close', flow, stack_cursor=self.index()['cursor'])

    def reload(self):
        self.ledger.close()
        self.ledger = InvocationLedger(self.storage / 'state.sqlite')
        self.metadata = {'workspace': str(self.workspace)}

    def spent(self, flow):
        sid = flow['binding']['session']
        return review_budget.usage(self.ledger, sid)['used']

    def scoped_coding(self):
        fir = self.advance_until('NEED_PERMISSION', self.request('start', prompt=self.edit_ask))
        fir = self.request('grant', fir, grant={'operation': 'edit', 'paths': ['src/retry.py'], 'test_paths': []})
        return self.advance_until('CODING', fir)

    def test_parked_reviews_and_coding_permissions_survive_reload_and_model_switch(self):
        cedar = self.advance_until('CHILDREN', self.request('start', prompt=self.review_ask))
        cedar = self.request('step', cedar)
        cedar_sid = cedar['binding']['session']
        cedar_contract = self.ledger.pinned(cedar_sid)
        cedar_spent = self.spent(cedar)
        self.assertGreater(cedar_spent['calls'], 0)
        with patch('subagent_runtime.run', side_effect=ContextBudgetExceeded('Fixture context allocation exhausted')):
            cedar = self.request('step', cedar)
        self.assertEqual(cedar['state'], 'PAUSED')
        self.assertEqual(cedar['stop']['resume_phase'], 'CHILDREN')
        self.assertEqual(self.spent(cedar), cedar_spent)

        self.tier = 'mid'
        fir = self.scoped_coding()
        fir_sid = fir['binding']['session']
        fir_contract = self.ledger.pinned(fir_sid)
        fir_spent = self.spent(fir)
        self.assertNotEqual(cedar_sid, fir_sid)
        self.assertEqual(fir_contract['constraints']['allowed_paths'], ['src/retry.py'])
        self.assertTrue(fir_contract['constraints']['edits_allowed'])
        self.assertFalse(cedar_contract['constraints']['edits_allowed'])

        maple = self.advance_until('MAPPING', self.request('start', prompt=self.third_ask))
        self.assertEqual(self.index()['focused'], maple['flow_id'])
        self.assertEqual({row['flow_id'] for row in self.index()['open_tasks']},
                         {cedar['flow_id'], fir['flow_id'], maple['flow_id']})
        self.assertEqual(self.spent(cedar), cedar_spent)
        self.assertEqual(self.spent(fir), fir_spent)

        self.reload()
        self.tier = 'high'
        self.assertEqual(self.index()['focused'], maple['flow_id'])
        before_switch = len(self.models)
        self.focus(cedar)
        self.assertEqual(len(self.models), before_switch)
        self.assertEqual(self.index()['focused'], cedar['flow_id'])
        self.assertEqual(self.spent(cedar), cedar_spent)
        self.assertEqual(self.ledger.pinned(cedar_sid), cedar_contract)
        cedar = self.request('inspect', cedar)
        cedar = self.request('resume', cedar)
        self.assertEqual(self.spent(cedar), cedar_spent)
        done = self.advance_until('DONE', cedar)
        self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertGreater(self.spent(cedar)['calls'], cedar_spent['calls'])
        resumed_workers = [model for stage, model in self.models[before_switch:] if stage == 'subagent']
        self.assertTrue(resumed_workers)
        for model in resumed_workers:
            self.assertEqual(model.model, MODELS['high'])
            self.assertEqual(model.payloads[0]['contract'], cedar_contract)
            self.assertEqual(model.payloads[0]['memory']['recent_turns'][-1]['user'], self.review_ask)
        self.assertEqual(self.index()['focused'], maple['flow_id'])
        self.assertEqual(self.spent(fir), fir_spent)

        self.reload()
        self.close_task(maple)
        self.assertEqual(self.index()['focused'], fir['flow_id'])
        self.assertEqual(self.ledger.pinned(fir_sid), fir_contract)
        self.assertEqual(self.spent(fir), fir_spent)
        fir = self.request('inspect', fir)
        proposed = self.request('step', fir)
        self.assertEqual(proposed['state'], 'AWAITING_ACTION')
        coder = next(model for stage, model in reversed(self.models) if stage == 'coder')
        self.assertEqual(coder.model, MODELS['high'])
        self.assertEqual(coder.calls[0]['request'], self.edit_ask)
        self.assertEqual(coder.calls[0]['contract']['constraints'], fir_contract['constraints'])
        self.assertEqual(coder.calls[0]['conversation_handoff']['invocation_id'], fir['conversation_id'])
        self.assertGreater(self.spent(fir)['calls'], fir_spent['calls'])
        used = self.spent(fir)
        self.close_task(proposed)
        self.assertFalse(self.index()['open_tasks'])
        self.assertEqual(self.spent(fir), used)
        self.assertEqual((self.workspace / 'src/retry.py').read_text(), SOURCE)
        self.assertTrue(self.ledger.verify_invocations(cedar_sid))
        self.assertTrue(self.ledger.verify_invocations(fir_sid))

    def test_parked_and_foreign_flow_cannot_dispatch_or_be_closed_with_stale_authority(self):
        cedar = self.advance_until('MAPPING', self.request('start', prompt=self.review_ask))
        old_index = self.index()['cursor']
        maple = self.advance_until('MAPPING', self.request('start', prompt=self.third_ask))
        before = self.spent(cedar)
        before_calls = len(self.models)
        with self.assertRaises(InvalidUpdate):
            self.request('step', cedar)
        with self.assertRaises(InvalidUpdate):
            self.request('task_focus', cedar, stack_cursor=self.index(self.other_chat)['cursor'], chat_key=self.other_chat)
        with self.assertRaises(InvalidUpdate):
            self.request('task_close', maple, stack_cursor=old_index)
        self.assertEqual(self.index()['focused'], maple['flow_id'])
        self.assertEqual(self.spent(cedar), before)
        self.assertEqual(len(self.models), before_calls)
        self.close_task(cedar)
        self.assertEqual(self.index()['focused'], maple['flow_id'])
        self.assertEqual(self.spent(cedar), before)
        self.assertEqual(machine.state(self.ledger, cedar['flow_id']), 'CANCELLED')

    @unittest.skipIf(os.name == 'nt', 'Atomic source edits require macOS or Linux')
    def test_parked_action_waits_for_focus_and_continuation_keeps_original_ledger(self):
        fir = self.request('step', self.scoped_coding())
        self.assertEqual(fir['state'], 'AWAITING_ACTION')
        fid = fir['flow_id']
        sid = fir['binding']['session']
        contract = self.ledger.pinned(sid)
        action_request = dict(fir['binding'], flow_id=fid, chat_key=self.chat_key,
                              invocation_id=fir['data']['result']['invocation_id'])
        preview = bridge(self.storage, self.workspace, dict(action_request, op='native_preview'))
        self.assertEqual(preview['target'], 'src/retry.py')
        maple = self.advance_until('MAPPING', self.request('start', prompt=self.third_ask))
        maple_spent = self.spent(maple)
        with self.assertRaisesRegex(InvalidUpdate, 'parked'):
            bridge(self.storage, self.workspace, dict(action_request, op='native_execute'))
        self.assertEqual((self.workspace / 'src/retry.py').read_text(), SOURCE)

        self.reload()
        self.focus(fir)
        with patch('host_actions.require_resources', return_value={'admitted': True, 'decision': 'ALLOW'}):
            result = bridge(self.storage, self.workspace, dict(action_request, op='native_execute'))
        self.assertEqual(result['action_receipt']['result']['status'], 'applied')
        self.assertEqual(result['task_stack']['focused'], maple['flow_id'])
        spent = self.spent(fir)
        committed = self.ledger.pinned(sid)
        self.assertEqual(committed['contract_hash'], contract['contract_hash'])
        self.assertEqual(committed['constraints'], contract['constraints'])
        self.assertEqual(machine.spent(self.ledger, fir['execution_id'])['actions'], 1)

        self.tier = 'high'
        self.coder_action = 'report'
        continuation = self.request('start_existing', prompt=self.edit_ask,
                                    conversation_id=fir['conversation_id'], action_flow_id=fid)
        continuation = self.advance_until('CODING', continuation)
        self.assertEqual(continuation['binding'], fir['binding'])
        self.assertEqual(self.spent(continuation), spent)
        done = self.request('step', continuation)
        self.assertEqual(done['state'], 'DONE')
        self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertEqual(self.ledger.pinned(sid), committed)
        self.assertGreater(self.spent(continuation)['calls'], spent['calls'])
        self.assertEqual(self.index()['focused'], maple['flow_id'])
        self.assertEqual(self.spent(maple), maple_spent)
