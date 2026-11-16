import copy
import unittest
from unittest.mock import patch

from context_manager import canonical
from invocation_gateway import InvocationLedger
from monitoring import ResourceWait
from test_conversation import MODELS
import test_orchestration as fixture
import orchestration as machine
import orchestration_flow as flow
import resource_routing as routing
import review_budget


class ResourceRoutingTests(unittest.TestCase):
    setUp=fixture.OrchestrationTests.setUp
    tearDown=fixture.OrchestrationTests.tearDown
    provider=fixture.OrchestrationTests.provider
    step=fixture.OrchestrationTests.step
    until=fixture.OrchestrationTests.until
    children=fixture.OrchestrationTests.children

    def tags(self):
        return {'models':[{'name':MODELS[t],'size':(i+1)*10**9,'digest':str(i+1)*64}
                          for i,t in enumerate(('low','mid','high'))]}

    def stop(self,tier='mid',**extra):
        return ResourceWait({'decision':'BRIDGE','admitted':False,'reason':'Capacity fixture',
            'runtime':dict({'kind':'model_capacity','stage':'configure','inference_started':False,
                            'plan':{'model':MODELS[tier],'decision':'BRIDGE'}},**extra)})

    def fail_stage(self,out,target='chat_runtime.answer',tier='mid',**extra):
        with patch(target,side_effect=self.stop(tier,**extra)),patch('runtime_memory.api',return_value=self.tags()):
            return self.step(out,tier='auto')

    def test_fallback_reopen_repages_original_context_without_refund_or_permission_change(self):
        out=self.until('SYNTHESIS');sid=self.meta['active'];pin=self.s.pinned(sid)
        before=review_budget.usage(self.s,sid)['used'];children=copy.deepcopy(out['children'])
        fallback=self.fail_stage(out)
        self.assertEqual(fallback['state'],'SYNTHESIS')
        self.assertEqual(fallback['resource_fallback']['tier'],'low')
        self.assertEqual(fallback['resource_fallback']['bounded_switch']['decision'],'ALLOW')
        self.assertEqual(review_budget.usage(self.s,sid)['used'],before)
        self.assertEqual(fallback['children'],children);self.assertEqual(self.s.pinned(sid),pin)
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite')
        done=self.step(fallback,tier='auto')
        self.assertEqual(done['state'],'DONE');self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertEqual(done['data']['route']['reason'],'governed resource fallback')
        p=self.models[-1][1].payloads[-1]
        self.assertEqual(p['request'],self.prompt);self.assertIn('CED724',canonical(p))
        self.assertEqual(p['contract'],pin);self.assertEqual(self.s.pinned(sid),pin)
        self.assertEqual(done['children'],children)
        after=review_budget.usage(self.s,sid)['used'];self.assertEqual(after['calls'],before['calls']+1)
        self.assertGreater(after['tokens'],before['tokens']);self.assertTrue(self.s.verify_invocations(sid))

    def test_child_fallback_keeps_completed_sibling_across_model_switch(self):
        out=self.children();out=self.step(out);first=out['children'][0]['id']
        self.assertEqual(machine.state(self.s,first),'DONE');spent=machine.spent(self.s,first)
        out=self.fail_stage(out,'subagent_runtime.run',tier='high')
        self.assertEqual(out['resource_fallback']['tier'],'mid')
        self.tier='mid';out=self.step(out,tier='auto')
        self.assertEqual(machine.spent(self.s,first),spent)
        self.assertTrue(all(c['state']=='DONE' for c in out['children']))
        self.assertIn('CED724',canonical(self.models[-1][1].payloads))
        self.assertEqual({p.model for role,p in self.models if role=='subagent'},{MODELS['low'],MODELS['mid']})

    def test_exhausted_candidates_pause_and_do_not_reset_switch_bound_on_resume(self):
        out=self.until('SYNTHESIS')
        out=self.fail_stage(out,tier='high');self.assertEqual(out['resource_fallback']['tier'],'mid')
        out=self.fail_stage(out,tier='mid');self.assertEqual(out['resource_fallback']['tier'],'low')
        out=self.fail_stage(out,tier='low');self.assertEqual(out['state'],'PAUSED')
        with patch('monitoring.resource_state',return_value={'admitted':True}):out=self.step(out,event='resume',tier='auto')
        out=self.fail_stage(out,tier='high');self.assertEqual(out['state'],'PAUSED')
        self.assertEqual(len([e for e in machine.events(self.s,out['flow_id']) if e['kind']=='RESOURCE_MODEL_FALLBACK']),2)

    def test_explicit_selection_pressure_busy_and_post_inference_errors_never_fallback(self):
        out=self.until('SYNTHESIS');_,data=flow.info(self.s,out['flow_id'])
        req={'models':MODELS,'tier':'auto','armed':True}
        cases=[dict(req,tier='high'),dict(req,armed=False)]
        with patch('runtime_memory.api',return_value=self.tags()) as api:
            for r in cases:self.assertIsNone(routing.propose(self.s,out['flow_id'],data,r,self.stop().state,'SYNTHESIS'))
            for extra in ({'inference_started':True},{'kind':'busy'},{'stage':'provider'},{'kind':'identity_changed'}):
                self.assertIsNone(routing.propose(self.s,out['flow_id'],data,req,self.stop(**extra).state,'SYNTHESIS'))
            self.assertIsNone(routing.propose(self.s,out['flow_id'],data,req,{'decision':'BRIDGE','reason':'OS pressure'},'SYNTHESIS'))
            api.assert_not_called()

    def test_no_candidate_or_budget_cannot_authorize_a_switch(self):
        out=self.until('SYNTHESIS');_,data=flow.info(self.s,out['flow_id']);req={'models':MODELS,'tier':'auto','armed':True}
        for tags in ({'models':[]},{'models':[{**r,'size':10**9} for r in self.tags()['models']]},
                     {'models':[{**r,'digest':'1'*64} for r in self.tags()['models']]}):
            with patch('runtime_memory.api',return_value=tags):
                self.assertIsNone(routing.propose(self.s,out['flow_id'],data,req,self.stop().state,'SYNTHESIS'))
        with patch('orchestration.gates',return_value=[{'decision':'FIN'}]),patch('runtime_memory.api') as api:
            self.assertIsNone(routing.propose(self.s,out['flow_id'],data,req,self.stop().state,'SYNTHESIS'));api.assert_not_called()
        with patch('runtime_memory.api',return_value=self.tags()),patch('model_identity.weights',return_value='same-weights'):
            self.assertIsNone(routing.propose(self.s,out['flow_id'],data,req,self.stop().state,'SYNTHESIS'))

    def test_configuration_changes_and_explicit_tiers_do_not_reuse_auto_choice(self):
        saved={'resource_fallback':{'models':MODELS,'tier':'low'}}
        self.assertEqual(routing.effective(saved,MODELS,'auto'),'resource-low')
        self.assertEqual(routing.effective(saved,MODELS,'high'),'high')
        self.assertEqual(routing.effective(saved,dict(MODELS,low='different'),'auto'),'auto')

    def test_coding_fallback_preserves_scoped_grant_and_requires_action_confirmation(self):
        from test_native_runtime import NativeModel
        self.operation='edit';out=self.until('NEED_PERMISSION')
        out=self.step(out,event='grant',grant={'operation':'edit','paths':['part0.py'],'test_paths':[]})
        out=self.until('CODING',out);sid=self.meta['active'];pin=self.s.pinned(sid)
        before=review_budget.usage(self.s,sid)['used'];source=(self.ws/'part0.py').read_text()
        out=self.fail_stage(out,'native_runtime.turn')
        self.assertEqual(out['resource_fallback']['tier'],'low')
        self.assertEqual(review_budget.usage(self.s,sid)['used'],before)
        coder=NativeModel(MODELS['low'],'edit',lambda r:r['action'].update(target='part0.py',old='return 0',new='return 1'))
        self.provider=lambda stage:coder
        out=self.step(out,tier='auto')
        self.assertEqual(out['state'],'AWAITING_ACTION')
        self.assertTrue(out['data']['result']['admission']['admitted'])
        self.assertEqual(self.s.pinned(sid),pin)
        self.assertEqual(pin['constraints']['allowed_paths'],['part0.py'])
        for key in ('tests_allowed','network_allowed','dependencies_allowed'):
            self.assertFalse(pin['constraints'][key])
        self.assertEqual((self.ws/'part0.py').read_text(),source)
        self.assertIn('CED724',canonical(coder.calls[0]))
        self.assertEqual(review_budget.usage(self.s,sid)['used']['calls'],before['calls']+1)


if __name__=='__main__':unittest.main()
