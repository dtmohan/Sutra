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
GOAL_PRIORITY = ('Before release, I need people who enter birth times in different time zones to be able to trust '
                 'the result. What would you fix first for that, and why?')
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

    def test_goal_preface_reuses_observations_and_never_proposes_a_new_run(self):
        observed = dict(OBSERVED,observations=[*OBSERVED['observations'],
            {'kind':'interaction','text':'The same synthetic birth date and time produced identical visible results '
                                        'with UTC offsets +00:00 and +05:30.'}])
        with patch('browser_runner.run',return_value=observed) as run:
            original = self.request('step',self.approve(self.awaiting()))
            reused = self.advance_until('SYNTHESIS',self.request('start',prompt=GOAL_PRIORITY))
            self.assertTrue(reused['browser_observation']['reused'])
            self.assertEqual(reused['browser_observation']['receipt_id'],original['browser_observation']['receipt_id'])
            self.request('step',reused)
            self.assertEqual(self.calls[-1]['request'],GOAL_PRIORITY)
            self.entry.write_text(HTML+'<p>Updated source.</p>')
            changed = self.advance_until('DONE',self.request('start',prompt=GOAL_PRIORITY))
            self.assertIn('earlier source version',changed['request_notice'])
            self.assertNotIn('browser_proposal',changed)
            self.assertEqual(len(self.calls),2)
            run.assert_called_once()

    def test_unrelated_goal_preface_stays_in_conversation_after_browser_review(self):
        from test_conversation import Fake as Router

        with patch('browser_runner.run',return_value=OBSERVED) as run:
            self.request('step',self.approve(self.awaiting()))
            router = Router(MODELS[self.tier])
            with patch.object(self,'provider',return_value=router) as provider:
                unrelated = 'I am doing my tax return. What would you fix first for that?'
                reply = self.request('start',prompt=unrelated)
                self.assertEqual(reply['state'],'DONE')
                self.assertNotIn('browser_observation',reply)
                self.assertNotIn('browser_proposal',reply)
                self.assertEqual(router.payloads[0]['request'],unrelated)
                provider.assert_called_once_with('conversation')
            run.assert_called_once()

    def test_published_judgment_follows_verified_receipt_across_reload_and_model_switch(self):
        from browser_review import request_check
        from orchestration_flow import browser_prior_judgment, info, move
        observed = {'status':'observed','observations':[
            {'kind':'layout','text':'The canvas overflowed the phone viewport.','selectors':['face'],
             'measurement':{'probe':'canvas_overflow','viewport':390,'layout_width':390,
                            'canvas_width':800,'left':0,'right':800}},
            {'kind':'runtime','text':'The browser showed literal Markdown fencing.',
             'measurement':{'probe':'visible_placeholder'}}]}
        why = 'Phone readers need to see the entire face to interpret the result.'
        next_prompt = 'What would you improve next for the watch on a phone?'
        calls = self.calls

        class JudgmentCritic(Critic):
            def call(self,cid,system,payload,schema):
                consume_dispatch(cid,self.model,system,payload,schema)
                calls.append(payload)
                value = {'priority':'O2'}
                if payload['judgment_version']==3:
                    value = {'goal':'Make the watch readable on a phone.',
                             'next_action':'fit_canvas_to_viewport','reason':why}
                    if payload.get('prior_browser_judgment'):
                        value['comparison'] = 'Clipped readings matter more than visual polish.'
                return value,{'call_id':cid,'ok':True,'answer':value}

        with patch.object(self,'provider',side_effect=lambda stage: JudgmentCritic(MODELS[self.tier],calls)), \
                patch('browser_runner.run',return_value=observed) as run:
            ready = self.awaiting()
            move(self.ledger,ready['flow_id'],'NEED_TOOL_APPROVAL','FIXTURE_LEGACY_BROWSER_FLOW',
                 browser_judgment_version=2)
            measured = self.approve(self.inspect(ready))
            original = self.request('step',measured)
            original_spending = self.spent(original)
            self.reload()
            self.tier = 'high'
            ready = self.advance_until('SYNTHESIS',self.request('start',prompt=PRIORITY))
            self.assertTrue(ready['browser_observation']['reused'])
            unchanged = dict(priority='O2',request=ASK)
            _,data = info(self.ledger,ready['flow_id'])
            self.assertEqual(data['prior_browser_judgment'],unchanged)
            self.assertEqual(browser_prior_judgment(self.ledger,ready['flow_id'],data),unchanged)
            reply = self.request('step',ready)
            self.assertEqual(calls[-1]['prior_browser_judgment'],unchanged)
            self.assertEqual(reply['browser_result']['model'],MODELS['high'])
            self.assertEqual(self.spent(original),original_spending)
            self.assertEqual(self.spent(reply)['operations'],0)
            sid = ready['binding']['session']
            self.assertTrue(self.ledger.verify_invocations(sid))
            forged = dict(calls[-1],prior_browser_judgment=dict(unchanged,priority='O1'))
            with self.assertRaisesRegex(InvalidUpdate,'completed observation'):
                request_check(self.ledger,sid,forged)
            ready = self.advance_until('SYNTHESIS',self.request('start',prompt=next_prompt))
            latest = self.request('step',ready)
            self.assertEqual(calls[-1]['prior_browser_judgment'],
                             dict(priority='O1',request=PRIORITY,original_request=ASK,why_first=why))
            self.assertEqual(latest['browser_result']['judgment'],dict(priority='O1',why_first=why))
            _,data = info(self.ledger,latest['flow_id'])
            self.ledger.quarantine_invocation(latest['binding']['session'],data['invocation_id'],'fixture')
            ready = self.advance_until('SYNTHESIS',self.request('start',prompt=PRIORITY))
            self.assertTrue(ready['browser_observation']['reused'])
            self.request('step',ready)
            self.assertIsNone(calls[-1]['prior_browser_judgment'])
            self.entry.write_text(HTML+'<p>New source.</p>')
            changed = self.advance_until('NEED_TOOL_APPROVAL',self.request('start',prompt=PRIORITY))
            _,data = info(self.ledger,changed['flow_id'])
            self.assertNotIn('prior_browser_judgment',data)
            run.assert_called_once()

    def test_followup_keeps_original_goal_when_published_rationale_is_missing(self):
        from browser_review import critique, request_check, schema, system_for
        from model_packet import build
        from orchestration_flow import info

        class Reviewer(Critic):
            def call(self,cid,system,payload,schema):
                consume_dispatch(cid,self.model,system,payload,schema)
                self.calls.append(payload)
                value = {'goal':'Check the behavior a visitor relies on.',
                         'next_action':'check_primary_user_flow','reason':'A known result would make the next check useful.'}
                return value,{'call_id':cid,'ok':True,'answer':value}

        def incomplete_publication(*args):
            result, report = critique(*args)
            self.assertTrue(result['admission']['admitted'])
            return result, dict(report,judgment={'priority':report['judgment']['priority']})

        with patch.object(self,'provider',side_effect=lambda stage: Reviewer(MODELS[self.tier],self.calls)), \
                patch('browser_runner.run',return_value=dict(OBSERVED,observation_profile='browser-probes-v2')) as run:
            measured = self.approve(self.awaiting())
            with patch('browser_review.critique',side_effect=incomplete_publication):
                original = self.request('step',measured)
            self.assertEqual(original['browser_result']['judgment'],{'priority':'scope'})
            reused = self.advance_until('SYNTHESIS',self.request('start',prompt=PRIORITY))
            self.assertTrue(reused['browser_observation']['reused'])
            self.assertEqual(reused['browser_observation']['receipt_id'],original['browser_observation']['receipt_id'])
            _,data = info(self.ledger,reused['flow_id'])
            self.assertNotIn('prior_browser_judgment',data)
            self.request('step',reused)
            initial, followup = self.calls
            self.assertNotIn('original_browser_request',initial)
            self.assertIsNone(followup['prior_browser_judgment'])
            self.assertEqual(followup['original_browser_request'],ASK)
            fmt = schema(followup)
            wire = json.loads(build(system_for(followup),followup,fmt)['messages'][1]['content'])
            self.assertEqual(wire['original_user_request'],ASK)
            self.assertEqual(wire['current_user_request'],PRIORITY)
            self.assertNotIn('previous_recommendation',wire)
            self.assertNotIn('comparison',fmt['properties'])
            sid = reused['binding']['session']
            forged = dict(followup,original_browser_request='A different project goal.')
            with self.assertRaisesRegex(InvalidUpdate,'completed observation'):
                request_check(self.ledger,sid,forged)
            run.assert_called_once()

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
