import copy,json,unittest
from unittest.mock import patch
import conversation as chat
import conversation_memory as cm
import conversation_routing as routing
import orchestration_flow as flow
import orchestration as machine
from context_manager import InvalidUpdate,canonical,ContextBudgetExceeded
from invocation_gateway import InvocationLedger
from monitoring import ResourceWait
from paging import Budget,PageFault
from review_budget import usage,BudgetStop
import test_conversation as fixture
from test_conversation import Fake,MODELS
import test_conversation_memory as memory_fixture


def escalate(result,payload):
    result.update(escalation_reason='reasoning_capacity',answer='Draft requiring stronger reasoning.')


class RoutingTests(unittest.TestCase):
    setUp=fixture.ConversationTests.setUp
    tearDown=fixture.ConversationTests.tearDown
    ask=fixture.ConversationTests.ask

    def route(self,prompt='Explain this reasoning problem.',providers=None,**kw):
        request=dict(prompt=prompt,models=MODELS,tier='auto',**kw)
        return chat.answer(self.s,self.meta,self.storage,request,providers or Fake())

    def test_auto_starts_small_even_for_keywords_and_long_prompts(self):
        for prompt in ['hello there','Discuss architecture, security and concurrency.', 'Compare approaches. '*35]:
            model=Fake();out=self.route(prompt,model)
            self.assertEqual(out['route']['tier'],'low')
            self.assertEqual(len(model.payloads),1)
            self.assertTrue(out['result']['admission']['admitted'])
            self.assertNotIn('active',self.meta)

    def test_reasoning_escalation_keeps_prompt_memory_contract_and_spent_budget(self):
        first=self.ask('Remember project Cedar, CED724. Never change the STP kernel. No network or dependencies.')
        sid=first['session'];parent=first['result']['invocation_id'];before=usage(self.s,sid)['used']
        small,medium=Fake(fault=escalate),Fake('medium')
        out=self.route(providers={'small':small,'medium':medium},parent=parent)
        self.assertEqual(out['session'],sid);self.assertEqual(out['route']['tier'],'mid')
        self.assertEqual(len(small.payloads),1);self.assertEqual(len(medium.payloads),1)
        a,b=small.payloads[0],medium.payloads[0]
        for key in ('request','contract','context_binding','workspace_context'):
            self.assertEqual(a[key],b[key])
        self.assertEqual(a['memory']['pinned_user_statements'],b['memory']['pinned_user_statements'])
        self.assertIn('CED724',canonical(b['memory']))
        self.assertFalse(b['contract']['constraints']['network_allowed'])
        self.assertFalse(b['contract']['constraints']['edits_allowed'])
        self.assertEqual(usage(self.s,sid)['used']['calls'],before['calls']+2)
        self.assertGreater(usage(self.s,sid)['used']['tokens'],before['tokens'])
        rows=cm.history(self.s,sid,out['result']['invocation_id'])
        self.assertEqual(len(rows),2)  # Intermediate draft is not conversation memory.
        self.assertNotIn('Draft requiring',canonical(rows))
        self.assertEqual(out['result']['token_usage']['totals']['calls'],2)
        for raw, in self.s.db.execute('SELECT record_json FROM invocations WHERE session=?',(sid,)):
            self.assertEqual(json.loads(raw)['task_budget_kernel']['decision'],'ALLOW')

    def test_requested_context_precedes_escalation_and_survives_switch(self):
        first,parent=memory_fixture.MemoryTests.fill(self);target=[]
        def page_then_escalate(result,p):
            escalate(result,p)
            if not p['requested_memory_pages']:
                target.append(p['memory']['toc'][-1]['id']);result['page_requests']=[target[0]]
        small,medium=Fake(fault=page_then_escalate),Fake('medium')
        out=self.route('Tell me about that earlier topic.',{'small':small,'medium':medium},parent=parent)
        self.assertTrue(out['result']['admission']['admitted'],out['result'])
        self.assertEqual(len(small.payloads),2);self.assertEqual(len(medium.payloads),1)
        for p in [small.payloads[1],medium.payloads[0]]:
            self.assertIn(target[0],[r['id'] for r in p['memory']['retrieved_turns']])
            self.assertEqual(p['memory']['pinned_user_statements'],small.payloads[0]['memory']['pinned_user_statements'])
        selection=small.payloads[1]['routing_state']['previous']
        self.s.quarantine_invocation(first['session'],selection,'Invalid selection')
        with self.assertRaises(PageFault):self.s.load_result(first['session'],out['result']['invocation_id'])
        with self.assertRaises(PageFault):cm.history(self.s,first['session'],out['result']['invocation_id'])

    def test_missing_unavailable_context_does_not_escalate(self):
        small=Fake(fault=lambda r,p:r.update(escalation_reason='missing_context',answer='Which project do you mean?'))
        out=self.route(providers=small)
        self.assertEqual(len(small.payloads),1);self.assertIsNone(out['routing_followup'])
        self.assertEqual(out['result']['accepted_output']['answer'],'Which project do you mean?')

    def test_paging_progress_uses_original_budget_instead_of_a_two_round_cutoff(self):
        p={'routing_state':{'page_passes':3,'requested_pages':['a','b','c']}}
        proposal={'intent':'conversation','page_requests':['d'],'escalation_reason':'reasoning_capacity'}
        self.assertEqual(routing.followup(proposal,p)['kind'],'page_in')
        proposal['page_requests']=['a']
        self.assertEqual(routing.followup(proposal,p)['kind'],'blocked')

    def test_too_small_next_context_pauses_without_dropping_pins_or_charging_a_call(self):
        first=self.ask('Remember Cedar CED724. Never change the kernel. No network.')
        one=self.route(parent=first['result']['invocation_id'],providers=Fake(fault=escalate),one_step=True)
        sid=one['session'];spent=usage(self.s,sid)['used'];medium=Fake('medium');medium.budget=Budget(2048,512,128)
        with self.assertRaises(ContextBudgetExceeded):
            self.route(parent=first['result']['invocation_id'],providers={'medium':medium},
                conversation_session=sid,routing_continuation=one['result']['invocation_id'])
        self.assertEqual(usage(self.s,sid)['used'],spent);self.assertEqual(medium.payloads,[])
        self.assertIn('CED724',canonical(cm.history(self.s,sid,first['result']['invocation_id'])))

    def test_blocked_escalation_resumes_with_explicit_model_without_replaying(self):
        small,medium=Fake(fault=escalate),Fake('medium')
        req={'event':'start','prompt':'Explain this problem.','models':MODELS,'tier':'low','armed':False}
        providers=lambda stage:{'small':small,'medium':medium}
        paused=flow.handle(self.s,self.meta,self.ws,self.storage,req,providers)
        self.assertEqual(paused['state'],'PAUSED')
        sid=paused['routing_step']['session'];spent=usage(self.s,sid)['used']
        def step(out,event,tier):
            return flow.handle(self.s,self.meta,self.ws,self.storage,dict(req,event=event,tier=tier,flow_id=out['flow_id'],cursor=out['cursor']),providers)
        ready=step(paused,'resume','mid');done=step(ready,'step','mid')
        self.assertEqual(done['state'],'DONE');self.assertEqual(done['conversation']['session'],sid)
        self.assertEqual(len(small.payloads),1);self.assertEqual(len(medium.payloads),1)
        self.assertEqual(usage(self.s,sid)['used']['calls'],spent['calls']+1)

    def test_explicit_tier_stays_fixed_and_duplicate_models_are_not_retried(self):
        small=Fake(fault=escalate)
        out=self.ask(provider=small)
        self.assertEqual(out['routing_followup']['kind'],'blocked')
        self.assertFalse(out['result']['admission']['admitted']);self.assertEqual(len(small.payloads),1)
        models={'low':'small','mid':'small','high':'large'};large=Fake('large',escalate)
        out=chat.answer(self.s,self.meta,self.storage,{'prompt':'A hard question.','models':models,'tier':'auto'},
                        {'small':Fake(fault=escalate),'large':large})
        self.assertFalse(out['result']['admission']['admitted'])
        self.assertEqual(len(large.payloads),1)
        self.assertEqual(out['result']['token_usage']['totals']['calls'],2)

    def test_invalid_model_request_is_repaired_without_escalation(self):
        for fault in (lambda r,p:r.update(escalation_reason='use arbitrary remote model'),
                      lambda r,p:r.pop('escalation_reason')):
            small=Fake(fault=fault)
            out=self.route(providers=small)
            self.assertFalse(out['result']['admission']['admitted'])
            self.assertEqual(len(small.payloads),3)
            self.assertEqual(usage(self.s,out['session'])['used']['calls'],3)

    def test_receipt_cannot_change_request_parent_or_contract(self):
        one=self.route(providers=Fake(fault=escalate),one_step=True)
        sid=one['session'];previous=one['result']['invocation_id']
        with self.assertRaisesRegex(InvalidUpdate,'original request'):
            self.route('New task',{'medium':Fake('medium')},conversation_session=sid,routing_continuation=previous)
        payload=json.loads(self.s.db.execute('SELECT d.payload_json FROM invocations i JOIN deliveries d ON d.id=i.delivery_id WHERE i.id=?',(previous,)).fetchone()[0])
        payload['routing_state']['page_passes']=0;payload['route']['tier']='high'
        with self.assertRaises(InvalidUpdate):routing.check(self.s,sid,payload)

    def test_kernel_budget_rejects_escalation_without_a_new_budget(self):
        small,medium=Fake(fault=escalate),Fake('medium')
        with patch.dict(chat.CONSTRAINTS,review_calls=1):
            one=self.route(providers=small,one_step=True)
            sid=one['session'];spent=usage(self.s,sid)['used']
            self.s.close();self.s=InvocationLedger(self.storage/'state.sqlite')
            with self.assertRaises(BudgetStop):
                self.route(providers={'medium':medium},conversation_session=sid,routing_continuation=one['result']['invocation_id'])
            self.assertEqual(usage(self.s,sid)['used'],spent)
            self.assertEqual(len(medium.payloads),0)

    def test_resource_pause_resumes_next_model_without_repeating_small_call(self):
        small,medium=Fake(fault=escalate),Fake('medium');providers=lambda stage:{'small':small,'medium':medium}
        req={'event':'start','prompt':'Explain a reasoning problem.','models':MODELS,'tier':'auto','armed':False}
        one=flow.handle(self.s,self.meta,self.ws,self.storage,req,providers)
        self.assertEqual(one['state'],'CLASSIFYING');sid=one['routing_step']['session']
        contract=chat.contract(self.s,sid);spent=usage(self.s,sid)['used']
        def step(out,event='step'):
            return flow.handle(self.s,self.meta,self.ws,self.storage,dict(req,event=event,flow_id=out['flow_id'],cursor=out['cursor']),providers)
        with patch('conversation.answer',side_effect=ResourceWait({'admitted':False,'reason':'fixture memory pressure'})):
            paused=step(one)
        self.assertEqual(paused['state'],'PAUSED');self.assertEqual(usage(self.s,sid)['used'],spent)
        self.s.close();self.s=InvocationLedger(self.storage/'state.sqlite')
        with patch('monitoring.resource_state',return_value={'admitted':True}):ready=step(paused,'resume')
        done=step(ready)
        self.assertEqual(done['state'],'DONE');self.assertEqual(done['conversation']['session'],sid)
        self.assertEqual(len(small.payloads),1);self.assertEqual(len(medium.payloads),1)
        self.assertEqual(chat.contract(self.s,sid),contract)
        self.assertEqual(usage(self.s,sid)['used']['calls'],spent['calls']+1)

    def test_workspace_handoff_precedes_source_based_escalation(self):
        small=Fake(fault=lambda r,p:r.update(intent='workspace',escalation_reason='reasoning_capacity'))
        out=self.route('Review the architecture.',small)
        self.assertEqual(out['route']['tier'],'low');self.assertIsNone(out['routing_followup'])
        self.assertNotIn('active',self.meta);self.assertEqual(len(small.payloads),1)


if __name__=='__main__':unittest.main()
