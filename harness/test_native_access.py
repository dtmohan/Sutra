import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import conversation
import native_access
from context_manager import InvalidUpdate
from invocation_gateway import InvocationLedger
from paging import PageFault
from review_budget import usage
from test_conversation import Fake, MODELS
from vscode_bridge import handle


class NativeAccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name);self.ws=self.root/'repo';self.ws.mkdir();self.storage=self.root/'store'
        (self.ws/'code.py').write_text('x=1\n');(self.ws/'test_code.py').write_text('import unittest\n')
        self.st=handle(self.storage,self.ws,{'op':'auto_new','goal':'Keep goal and constraints across models','roots':['.']})
        self.s=InvocationLedger(self.storage/'state.sqlite');self.meta=json.loads((self.storage/'workspace.json').read_text())
    def tearDown(self):self.s.close();self.tmp.cleanup()
    def request(self, prompt='Edit code.py', operation='edit', paths=None, tests=None):
        note=conversation.answer(self.s,self.meta,self.storage,{'prompt':prompt,'models':MODELS,'tier':'low'},Fake())
        return {'op':'native_permissions','session':self.st['session'],'contract_hash':self.st['contract']['contract_hash'],
                'calibration_hash':self.st['calibration']['binding']['hash'],'operation':operation,'paths':paths or ['code.py'],
                'test_paths':tests or [],'conversation_id':note['result']['invocation_id']}
    def test_grant_preserves_goal_other_constraints_and_spent_budget(self):
        req=self.request();before=usage(self.s,self.st['session']);old=self.s.snapshot(self.st['session'])
        result=handle(self.storage,self.ws,req);new=self.s.snapshot(self.st['session'])
        self.assertEqual(old['goal'],new['goal']);self.assertEqual(old['task_id'],new['task_id'])
        for k in ('network_allowed','dependencies_allowed','review_calls','review_tokens','review_seconds'):
            self.assertEqual(old['constraints'][k],new['constraints'][k])
        self.assertEqual(before['used'],usage(self.s,self.st['session'])['used'])
        self.assertTrue(new['constraints']['edits_allowed']);self.assertFalse(new['constraints']['tests_allowed'])
        self.assertEqual(new['constraints']['allowed_paths'],['code.py'])
        self.assertTrue(result['contract']['contract']['constraints']['native_task'])
        self.assertEqual(conversation.workspace_handoff(self.s,self.st['session'],req['conversation_id'],'Edit code.py')['authority'],'model_unverified')
        with self.assertRaises(InvalidUpdate):conversation.workspace_handoff(self.s,self.st['session'],req['conversation_id'],'Different request')
    def test_later_test_permission_can_add_a_file_from_original_read_scope(self):
        first=self.request();self.st=handle(self.storage,self.ws,first)
        self.meta=json.loads((self.storage/'workspace.json').read_text())
        before=usage(self.s,self.st['session'])
        request=self.request('Run test_code.py','test',['code.py','test_code.py'],['test_code.py'])
        result=handle(self.storage,self.ws,request)
        self.assertEqual(result['read_scope'],['.'])
        self.assertTrue(result['contract']['contract']['constraints']['tests_allowed'])
        self.assertEqual(before['used'],usage(self.s,self.st['session'])['used'])

    def test_automatic_goal_keeps_long_prompt_without_character_truncation(self):
        goal='Review project; '+('retain detail '*90)+' Never alter the kernel.'
        self.assertGreater(len(goal),1000)
        result=handle(self.storage,self.ws,{'op':'auto_new','goal':goal,'roots':['.']})
        self.assertEqual(result['contract']['contract']['goal'],goal)

    def test_network_scope_and_stale_binding_cannot_be_widened(self):
        for change in ({'paths':['../outside.py']},{'paths':['code.ts']},{'contract_hash':'forged'},{'operation':'shell'},{'operation':'test','test_paths':[]}):
            req=self.request();req.update(change)
            with self.assertRaises(InvalidUpdate):handle(self.storage,self.ws,req)
        self.assertFalse(self.s.snapshot(self.st['session'])['constraints']['edits_allowed'])
    def test_test_permission_does_not_grant_edits(self):
        req=self.request('Run test_code.py','test',['code.py','test_code.py'],['test_code.py'])
        result=handle(self.storage,self.ws,req);c=result['contract']['contract']['constraints']
        self.assertTrue(c['tests_allowed']);self.assertFalse(c['edits_allowed']);self.assertEqual(c['test_paths'],['test_code.py'])
    def test_handoff_after_automatic_task_creation_is_explicitly_bound(self):
        note=conversation.answer(self.s,{},self.storage,{'prompt':'Review this workspace','models':MODELS,'tier':'low'},Fake())
        new=handle(self.storage,self.ws,{'op':'auto_new','goal':'Review this workspace','roots':['.'],'conversation_id':note['result']['invocation_id']})
        self.assertTrue(conversation.workspace_handoff(self.s,new['session'],note['result']['invocation_id'],'Review this workspace'))
        self.s.quarantine_invocation(note['session'],note['result']['invocation_id'],'Rejected note')
        with self.assertRaises(PageFault):conversation.workspace_handoff(self.s,new['session'],note['result']['invocation_id'],'Review this workspace')
    def test_interruption_after_contract_change_cannot_reset_allocations(self):
        req=self.request();before=usage(self.s,self.st['session'])
        original=self.s._audit
        def fail(event,payload):
            if event=='NATIVE_PERMISSION_GRANT':raise RuntimeError('Interrupted')
            return original(event,payload)
        with patch.object(self.s,'_audit',side_effect=fail),self.assertRaises(RuntimeError):native_access.grant(self.s,self.meta,self.ws,req)
        self.assertEqual(before['used'],usage(self.s,self.st['session'])['used'])

    def test_ollama_connection_refused_has_actionable_recovery(self):
        from unittest.mock import patch
        from vscode_bridge import ollama
        with patch('http.client.HTTPConnection.request',side_effect=ConnectionRefusedError):
            with self.assertRaisesRegex(InvalidUpdate,'Start the Ollama app.*Retry model discovery'):ollama('/api/tags')
