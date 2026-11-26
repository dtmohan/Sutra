"""Native worktree proposals, exact approval and interruption recovery."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate
import host_actions
from lifecycle import Interrupted, reconcile
import orchestration as machine
from test_conversation import Fake as Router, MODELS
from test_native_runtime import NativeModel
import test_task_stack_workflow as fixture
from vscode_bridge import handle as bridge


@unittest.skipUnless(shutil.which('git'), 'Git is needed for the project worktree')
class ProjectWorkflowTests(unittest.TestCase):
    chat_key = fixture.OpenTaskWorkflowTests.chat_key
    request = fixture.OpenTaskWorkflowTests.request
    reload = fixture.OpenTaskWorkflowTests.reload
    spent = fixture.OpenTaskWorkflowTests.spent
    advance_until = fixture.OpenTaskWorkflowTests.advance_until
    sources = {'prices.py': 'def price():\n    return 1\n',
               'total.py': 'from prices import price\n\ndef total():\n    return price() + 1\n'}
    ask = ('Fix both prices.py and total.py so their combined result is 5. '
           'Run the project tests using python3 -m unittest discover -s tests.')
    edits = [{'target': 'prices.py', 'old': 'return 1', 'new': 'return 3'},
             {'target': 'total.py', 'old': 'price() + 1', 'new': 'price() + 2'}]

    def setUp(self):
        fixture.OpenTaskWorkflowTests.setUp(self)
        for name, source in self.sources.items():
            (self.workspace / name).write_text(source)
        (self.workspace / 'tests').mkdir()
        (self.workspace / 'tests/test_total.py').write_text(
            'import unittest\nfrom total import total\n\n'
            'class Total(unittest.TestCase):\n    def test_value(self):\n'
            '        self.assertEqual(total(), 5)\n')
        self.git('init', '-q')
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 'commit', '--no-gpg-sign', '-qm', 'Initial task')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.workspace), *args], text=True).strip()

    def provider(self, stage):
        name = MODELS[self.tier]
        if stage == 'conversation':
            # A generic read disposition may not suppress the explicit user action.
            model = Router(name, lambda result, _: result.update(
                intent='workspace', workspace_request={'operation': 'read', 'scope': 'workspace', 'paths': []}))
        else:
            self.assertEqual(stage, 'coder')
            model = NativeModel(name, fault=lambda result: result.update(
                action={'kind': 'worktree_patch', 'edits': self.edits}))
        self.models.append((stage, model))
        return model

    def propose(self):
        start = self.request('start', prompt=self.ask, armed=False)
        self.assertEqual(start['state'], 'NEED_ARM')
        waiting = self.request('step', start)
        self.assertEqual(waiting['state'], 'NEED_PERMISSION')
        self.assertEqual(waiting['permission']['operation'], 'project_patch')
        self.assertFalse(any(role == 'coder' for role, _ in self.models))
        with self.assertRaisesRegex(InvalidUpdate, 'exact named'):
            self.request('grant', waiting, grant={'operation': 'project_patch', 'paths': ['prices.py']})
        approved = self.request('grant', waiting, grant={
            'operation': 'project_patch', 'paths': sorted(self.sources), 'test_paths': []})
        sid = approved['binding']['session']
        self.assertFalse(self.ledger.pinned(sid)['constraints']['edits_allowed'])
        self.assertFalse(self.ledger.pinned(sid)['constraints']['tests_allowed'])
        before = self.spent(approved)
        self.reload()
        self.tier = 'high'
        current = self.request('inspect', approved)
        self.assertEqual(self.spent(current), before)
        action = self.advance_until('AWAITING_ACTION', current)
        self.assertEqual(action['flow_id'], start['flow_id'])
        self.assertEqual(action['execution_id'], approved['execution_id'])
        self.assertEqual(self.models[-1][1].model, MODELS['high'])
        args = dict(action['binding'], flow_id=action['flow_id'], chat_key=self.chat_key,
                    invocation_id=action['pending_action'])
        scoped = bridge(self.storage, self.workspace, dict(args,op='state'))
        self.assertEqual(scoped['calibration']['binding']['hash'], args['calibration_hash'])
        self.assertEqual(scoped['contract']['contract_hash'], args['contract_hash'])
        preview = bridge(self.storage, self.workspace, dict(args, op='native_preview'))
        self.assertEqual(preview['base_commit'], self.git('rev-parse', 'HEAD'))
        self.assertEqual(preview['test_target'], 'tests')
        self.assertEqual([row['target'] for row in preview['changes']], sorted(self.sources))
        self.assertFalse((self.storage / 'actions').exists())
        return action, dict(args, plan_digest=preview['plan_digest']), preview

    @unittest.skipUnless(sys.platform == 'darwin', 'Project tests use the macOS sandbox')
    def test_plan_bound_multifile_execution_settles_once(self):
        action, args, preview = self.propose()
        sid, iid = args['session'], args['invocation_id']
        before = self.spent(action)
        with patch('host_actions.require_resources', return_value={'admitted': True, 'decision': 'ALLOW'}):
            with self.assertRaisesRegex(InvalidUpdate, 'changed after approval'):
                bridge(self.storage, self.workspace, dict(args, op='native_execute', plan_digest='0'*64))
            self.assertEqual(self.spent(action), before)
            result = bridge(self.storage, self.workspace, dict(args, op='native_execute'))['action_receipt']
        self.assertEqual(result['kind'], 'worktree_patch')
        self.assertEqual(result['result']['status'], 'process_exited_zero', result)
        self.assertFalse(result['result']['assertions_verified'])
        worktree = Path(result['result']['worktree'])
        self.assertIn('return 3', (worktree / 'prices.py').read_text())
        self.assertIn('price() + 2', (worktree / 'total.py').read_text())
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual(self.git('worktree', 'list', '--porcelain').count('worktree '), 1)
        used = self.spent(action)
        self.assertEqual(used['operations'] - before['operations'], 1)
        self.assertEqual(used['source_bytes'] - before['source_bytes'], preview['source_bytes'])
        self.reload()
        resumed = bridge(self.storage, self.workspace, {
            'op':'orchestrate', 'event':'recoverable_review','chat_key':self.chat_key})
        self.assertEqual(resumed['action_receipt'], result)
        self.assertEqual(resumed['task_stack']['open_tasks'], [])
        replay = host_actions.execute(self.ledger, sid, iid, self.workspace, self.storage,
                                      resource_check=False, plan_digest=args['plan_digest'])
        self.assertEqual(replay['id'], result['id'])
        self.assertEqual(self.spent(action), used)
        self.assertEqual(machine.spent(self.ledger, action['execution_id'])['actions'], 1)
        self.assertTrue(self.ledger.audit_valid(deep=True))
        self.assertTrue(self.ledger.verify_invocations(sid))

    @unittest.skipUnless(sys.platform == 'darwin', 'Project tests use the macOS sandbox')
    def test_interruption_keeps_reservation_and_does_not_replay(self):
        action, args, _ = self.propose()
        sid, iid = args['session'], args['invocation_id']
        with patch('project_worktree.run', side_effect=Interrupted('After commit')) as dispatch:
            with self.assertRaises(Interrupted):
                host_actions.execute(self.ledger, sid, iid, self.workspace, self.storage,
                                     resource_check=False, plan_digest=args['plan_digest'])
        dispatch.assert_called_once()
        used = self.spent(action)
        raw = self.ledger.db.execute('SELECT record_json FROM host_actions').fetchone()[0]
        self.assertGreater(used['seconds'], json.loads(raw)['task_cost']['seconds'])
        self.reload()
        look = {'op':'orchestrate','event':'recoverable_review','chat_key':self.chat_key}
        foreign = bridge(self.storage, self.workspace, dict(look,chat_key='b'*64))
        self.assertNotIn('action_receipt', foreign)
        self.assertEqual(machine.state(self.ledger,action['flow_id']), 'EXECUTING_ACTION')
        status = bridge(self.storage, self.workspace, look)
        self.assertEqual(reconcile(self.ledger, self.workspace), [])
        self.assertEqual(self.spent(action), used)
        self.assertEqual(status['state'], 'DONE')
        self.assertEqual(status['action_receipt']['result']['status'], 'interrupted')
        self.assertFalse(status['action_receipt']['result']['assertions_verified'])
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertTrue(self.ledger.audit_valid(deep=True))
