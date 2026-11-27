"""Governed runtime approval, evidence projection and same-chat recovery."""
import json
import os
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate
from invocation_gateway import consume_dispatch
from lifecycle import Interrupted
import orchestration
from paging import Budget
from test_conversation import MODELS
import test_task_stack_workflow as fixture
from vscode_bridge import handle as bridge, request_environment


ASK = 'can you launch the application and critique the code based on its observed behaviors?'
FOLLOWUP = 'can you render it in a browser and provide your feedback after running the app?'
PRIORITY = 'What would you fix first, and why?'
HTML = '<canvas id="face" width="800"></canvas>\n<button id="next">Next</button>\n'
OBSERVED = {'status':'observed','observations':[
    {'kind':'layout','text':'Canvas overflows the 390 px viewport.','selectors':['face']},
    {'kind':'control','text':'Next changed the displayed step.','selectors':['next']},
    {'kind':'network','text':'The runner blocked a request under its offline policy.'},
]}


class Critic:
    budget = Budget(16384,1536,256)

    def __init__(self, model, calls):
        self.model, self.calls = model, calls

    def call(self, cid, system, payload, schema):
        consume_dispatch(cid,self.model,system,payload,schema)
        self.calls.append(payload)
        value = {'summary':'The watch loaded and overflowed on the narrow viewport.',
                 'findings':[{'evidence_id':'O1','source_line':payload['source_excerpts'][0]['line'],
                              'assessment':'The fixed canvas width exceeds the observed viewport.',
                              'recommendation':'Constrain the CSS display width to its container.'}]}
        return value,{'call_id':cid,'ok':True,'answer':value}


class BrowserWorkflowTests(unittest.TestCase):
    chat_key = fixture.OpenTaskWorkflowTests.chat_key
    request = fixture.OpenTaskWorkflowTests.request
    reload = fixture.OpenTaskWorkflowTests.reload
    spent = fixture.OpenTaskWorkflowTests.spent
    advance_until = fixture.OpenTaskWorkflowTests.advance_until

    def setUp(self):
        fixture.OpenTaskWorkflowTests.setUp(self)
        self.entry = self.workspace/'index.html'
        self.entry.write_text(HTML)
        self.calls = []

    def provider(self, stage):
        self.assertEqual(stage,'synthesis','Explicit browser routing should use the host')
        return Critic(MODELS[self.tier],self.calls)

    def awaiting(self):
        started = self.request('start',prompt=ASK,armed=False)
        self.assertEqual(started['state'],'NEED_ARM')
        with self.assertRaisesRegex(InvalidUpdate,'user-armed'):
            self.request('step',started,armed=False)
        ready = self.advance_until('NEED_TOOL_APPROVAL',started)
        self.assertEqual(ready['browser_proposal']['entry'],'index.html')
        self.assertFalse(ready['browser_proposal']['network_allowed'])
        self.assertFalse(self.calls)
        return ready

    def approve(self, ready, *, scope=None):
        plan = ready['browser_proposal']
        digest = plan['plan_digest']
        proof = {'version':1,'flow_id':ready['flow_id'],'cursor':ready['cursor'],
                 'binding':ready['binding'],'plan_digest':digest}
        with patch.dict(os.environ,SUTRA_BROWSER_AUTHORIZATION=json.dumps(scope or proof)), \
                patch('monitoring.require_resources',return_value={'decision':'ALLOW','admitted':True}):
            return self.request('browser_grant',ready,plan_digest=digest)

    def inspect(self, ready):
        return bridge(self.storage,self.workspace,{'op':'orchestrate','event':'inspect',
                      'chat_key':self.chat_key,'flow_id':ready['flow_id']})

    def test_browser_approval_and_model_switch_keep_live_evidence_bound(self):
        with patch('browser_runner.run',return_value=OBSERVED) as run:
            ready = self.awaiting()
            before = self.spent(ready)
            with self.assertRaisesRegex(InvalidUpdate,'explicit approval'):
                self.request('step',ready)
            with self.assertRaisesRegex(InvalidUpdate,'requires approval'):
                self.approve(ready,scope={'plan_digest':ready['browser_proposal']['plan_digest']})
            self.assertFalse(run.called)
            self.assertEqual(self.spent(ready),before)
            self.reload()
            ready = self.inspect(ready)
            measured = self.approve(ready)
            self.assertEqual(measured['state'],'SYNTHESIS')
            run.assert_called_once_with(HTML.encode())
            receipt = measured['browser_observation']
            self.assertEqual(receipt['observations'][0]['id'],'O1')
            self.assertIsNone(receipt['diagnostic'])
            used = self.spent(measured)
            self.assertEqual(used['operations']-before['operations'],1)
            self.assertEqual(used['source_bytes']-before['source_bytes'],len(HTML.encode()))
            self.reload()
            self.tier = 'high'
            same = self.inspect(measured)
            self.assertEqual(same['browser_observation'],receipt)
            done = self.request('step',same)
            self.assertEqual(done['state'],'DONE')
            self.assertEqual(done['browser_result']['model'],MODELS['high'])
            self.assertIn('O1',done['browser_result']['markdown'])
            self.assertEqual(self.calls[0]['receipt_id'],receipt['receipt_id'])
            self.assertEqual(len(receipt['observations']),3)
            self.assertEqual([item['id'] for item in self.calls[0]['observations']],['O1','O2'])
            run.assert_called_once()
        sid = ready['binding']['session']
        self.assertTrue(self.ledger.verify_invocations(sid))
        self.assertTrue(self.ledger.audit_valid(deep=True))
        saved = bridge(self.storage,self.workspace,{'op':'orchestrate','event':'recoverable_review',
                       'chat_key':self.chat_key})
        self.assertEqual(saved['browser_observation'],receipt)
        self.assertEqual(saved['browser_result'],done['browser_result'])

    def test_changed_source_needs_fresh_approval_and_interruption_never_runs_again(self):
        ready = self.awaiting()
        previous = ready['browser_proposal']['plan_digest']
        self.entry.write_text(HTML+'\n<p>Changed</p>')
        before = self.spent(ready)
        with patch('browser_runner.run',side_effect=Interrupted('fixture')) as run:
            updated = self.approve(ready)
            self.assertNotEqual(updated['browser_proposal']['plan_digest'],previous)
            self.assertEqual(updated['state'],'NEED_TOOL_APPROVAL')
            self.assertEqual(self.spent(updated),before)
            self.assertFalse(run.called)
            with self.assertRaises(Interrupted):
                self.approve(updated)
            used = self.spent(updated)
            self.reload()
            self.assertEqual(orchestration.state(self.ledger,ready['flow_id']),'EXECUTING_ACTION')
            recovered = self.inspect(ready)
            self.assertEqual(recovered['state'],'DONE')
            self.assertEqual(recovered['browser_observation']['status'],'interrupted')
            self.assertEqual(self.spent(ready),used)
            self.assertEqual(self.inspect(ready)['browser_observation'],recovered['browser_observation'])
            run.assert_called_once()

    def test_busy_synthesis_keeps_browser_receipt_and_spending(self):
        from review_budget import BudgetStop, decision
        ready = self.awaiting()
        with patch('browser_runner.run',return_value=OBSERVED) as run:
            measured = self.approve(ready)
            before = self.spent(measured)
            blocked = decision(self.ledger,ready['binding']['session'],{'calls':1000000})
            with patch('browser_review.critique',side_effect=BudgetStop(blocked)), \
                    patch('workspace_allocation.try_amend',return_value=None):
                paused = self.request('step',measured)
            self.assertEqual(paused['state'],'PAUSED')
            self.assertEqual(paused['stop']['status'],'task_budget')
            self.assertEqual(paused['browser_observation'],measured['browser_observation'])
            self.assertEqual(self.spent(paused),before)
            run.assert_called_once()

    def test_private_worker_permit_is_scoped_to_one_request(self):
        key = 'SUTRA_BROWSER_AUTHORIZATION'
        with patch.dict(os.environ):
            os.environ.pop(key,None)
            with request_environment({key:'permit'},{}):
                self.assertEqual(os.environ.get(key),'permit')
            with request_environment({key:None},{}):
                self.assertNotIn(key,os.environ)

    def test_excerpts_attach_to_their_dom_observations(self):
        from browser_review import _excerpts
        html = b'<canvas id="watch"></canvas>' + b'\n'*15 + b'<button id="toggle">Tropical</button>'
        canvas, button = _excerpts(html,{'watch':{'O1','O7'},'toggle':{'O3'}})
        self.assertEqual(canvas['observations'],['O1','O7'])
        self.assertEqual(button['observations'],['O3'])

    def saved_static_catalog(self):
        from orchestration_flow import move
        static = self.request('start',prompt='Review this codebase and give an overview.')
        bound = self.advance_until('WORKSPACE',static)
        move(self.ledger,bound['flow_id'],'SYNTHESIS','FIXTURE_SOURCE_REVIEWED')
        move(self.ledger,bound['flow_id'],'DONE','FIXTURE_REVIEW_FINISHED')

    def test_explicit_followup_reuses_only_matching_browser_evidence(self):
        self.saved_static_catalog()
        routed = self.request('start',prompt=FOLLOWUP,armed=False)
        self.assertEqual(routed['state'],'NEED_ARM')
        ready = self.advance_until('NEED_TOOL_APPROVAL',routed)
        self.assertEqual(ready['browser_proposal']['entry'],'index.html')
        with patch('browser_runner.run',return_value=OBSERVED) as run:
            measured = self.approve(ready)
            original = self.request('step',measured)
            original_spending = self.spent(original)
            self.reload()
            self.tier = 'high'
            followup = self.request('start',prompt=PRIORITY,armed=False)
            self.assertEqual(followup['state'],'NEED_ARM')
            with self.assertRaisesRegex(InvalidUpdate,'user-armed'):
                self.request('step',followup,armed=False)
            reused = self.advance_until('SYNTHESIS',followup)
            self.assertTrue(reused['browser_observation']['reused'])
            self.assertEqual(reused['browser_observation']['receipt_id'],original['browser_observation']['receipt_id'])
            reply = self.request('step',reused)
            self.assertEqual(reply['browser_result']['model'],MODELS['high'])
            self.assertEqual(self.spent(original),original_spending)
            self.assertEqual(self.spent(reply)['operations'],0)
            self.entry.write_text(HTML+'\n<p>Changed source</p>')
            newer = self.advance_until('NEED_TOOL_APPROVAL',self.request('start',prompt=PRIORITY))
            self.assertNotEqual(newer['browser_proposal']['source_sha256'],ready['browser_proposal']['source_sha256'])
            self.assertNotIn('browser_observation',newer)
            run.assert_called_once()
        self.assertTrue(self.ledger.verify_invocations(reply['binding']['session']))

    def test_ambiguous_followup_pauses_until_same_chat_names_a_file(self):
        (self.workspace/'other.html').write_text(HTML)
        self.saved_static_catalog()
        with patch('browser_runner.run') as run:
            unresolved = self.request('start',prompt=FOLLOWUP)
            self.assertEqual(unresolved['state'],'PAUSED')
            self.assertEqual(unresolved['stop']['evidence_bridge']['target_kind'],'browser')
            self.assertEqual(self.calls,[])
            with self.assertRaisesRegex(InvalidUpdate,'another Sutra chat'):
                self.request('supply_evidence',unresolved,chat_key='b'*64,evidence_text='other.html')
            with self.assertRaisesRegex(InvalidUpdate,'workspace-relative HTML'):
                self.request('supply_evidence',unresolved,evidence_text='../outside.html')
            named = self.request('supply_evidence',unresolved,evidence_text='other.html')
            ready = self.advance_until('NEED_TOOL_APPROVAL',named)
            self.assertEqual(ready['flow_id'],unresolved['flow_id'])
            self.assertEqual(ready['browser_proposal']['entry'],'other.html')
            run.assert_not_called()

    def test_open_feedback_uses_one_model_question_and_separate_approval(self):
        from test_conversation import Fake as Router
        self.saved_static_catalog()
        offered = []
        question = 'Does the watch canvas fit at 390 pixels wide?'

        def plan(result, packet):
            offered.append(packet['browser_inquiry'])
            result['intent'] = 'workspace'
            result['browser_probe'] = {'entry':'index.html','working_question':question}

        with patch.object(self,'provider',side_effect=lambda stage: Router(MODELS[self.tier],plan)), \
                patch('browser_runner.run') as run:
            feedback = self.request('start',prompt='Would the watch be readable on a phone?')
            self.assertEqual(feedback['state'],'NEED_ARM')
            ready = self.advance_until('NEED_TOOL_APPROVAL',feedback)
            self.assertEqual(ready['browser_question'],question)
            self.assertEqual(offered,[{'version':2,'flow_id':feedback['flow_id'],'entry':'index.html'}])
            run.assert_not_called()
