import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from context_manager import InvalidUpdate,ContextBudgetExceeded,canonical
from invocation_gateway import InvocationLedger
from test_chat import Fake as Review
from test_conversation import Fake as Conversation,MODELS
from paging import PageFault
import orchestration as m
import orchestration_flow as flow
import review_budget

class Child(Review):
    def call(self,cid,system,p,schema):
        result,record=super().call(cid,system,p,schema)
        result['branch_id']=p['branch']['id'];return result,record

class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name);self.ws=self.base/'repo';self.ws.mkdir();self.root=self.base/'store';self.root.mkdir()
        for i in range(4):(self.ws/f'part{i}.py').write_text(f'def part{i}():\n    return {i}\n')
        self.meta={'workspace':str(self.ws)};self.s=InvocationLedger(self.root/'state.sqlite');self.models=[];self.tier='low'
        self.prompt='Review the project. Remember project Cedar, ticket CED724. Keep this read only. No network or new dependencies.'
    def tearDown(self):self.s.close();self.tmp.cleanup()
    def provider(self,stage):
        model=MODELS[self.tier]
        if stage=='conversation':
            p=Conversation(model,lambda r,p:r.update(intent='workspace',facts=['Remember project Cedar, ticket CED724.'],workspace_request={'operation':getattr(self,'operation','read'),'paths':[]}))
        else:p=Child(model) if stage=='subagent' else Review(model)
        self.models.append((stage,p));return p
    def step(self,out=None,**kw):
        request={'models':MODELS,'tier':self.tier,'armed':True}
        if out:request.update(event='step',flow_id=out['flow_id'],cursor=out['cursor'],binding=out.get('binding') or out.get('bound_task'))
        else:request.update(event='start',prompt=self.prompt)
        request.update(kw)
        return flow.handle(self.s,self.meta,self.ws,self.root,request,self.provider)
    def until(self,phase,out=None):
        for _ in range(40):
            out=self.step(out)
            if out['state']==phase:return out
        self.fail('Flow never reached '+phase+': '+str(out))
    def children(self):return self.until('CHILDREN')
    def test_repaired_handoff_still_requires_host_arming_and_retains_limits(self):
        original=self.provider
        def provider(stage):
            if stage!='conversation':return original(stage)
            def reply(r,p):
                if 'repair' not in p:r['answer']='This conversation has no file-access tools.'
                else:r.update(intent='workspace',answer='Handing off in this chat.',workspace_request={'operation':'read','paths':[]})
            model=Conversation(MODELS[self.tier],reply);self.models.append((stage,model));return model
        self.provider=provider
        out=self.step(armed=False)
        self.assertEqual(out['state'],'NEED_ARM')
        self.assertNotIn('active',self.meta)
        self.assertEqual(len(self.models[0][1].payloads),2)
        with self.assertRaisesRegex(InvalidUpdate,'user-armed'):
            self.step(out,armed=False)
        out=self.until('CHILDREN',out)
        sid=self.meta['active'];contract=self.s.pinned(sid)
        self.assertFalse(contract['constraints']['edits_allowed'])
        self.assertFalse(contract['constraints']['network_allowed'])
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');self.tier='high'
        out=self.until('DONE',out)
        self.assertTrue(out['data']['result']['admission']['admitted'])
        self.assertEqual(contract,self.s.pinned(sid))

    def test_auto_small_first_then_fresh_source_worker_switch_keeps_contract(self):
        self.tier='auto';self.prompt='Review the architecture. Remember Cedar, CED724. Keep this read only; no network or new dependencies.'
        def provider(stage):
            if stage=='conversation':
                p=Conversation('small',lambda r,p:r.update(intent='workspace',escalation_reason='reasoning_capacity'))
            else:
                p=Child('large') if stage=='subagent' else Review('large')
                p.budget=__import__('paging').Budget(32768,4096,256)
            self.models.append((stage,p));return p
        self.provider=provider
        first=self.step(armed=False)
        self.assertEqual(first['conversation']['route']['tier'],'low')
        self.assertEqual(len(self.models),1);self.assertNotIn('active',self.meta)
        with self.assertRaisesRegex(InvalidUpdate,'user-armed'):self.step(first,armed=False)
        children=self.until('CHILDREN',first);sid=self.meta['active'];contract=self.s.pinned(sid)
        used=review_budget.usage(self.s,sid)['used']
        done=self.until('DONE',children)
        self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertEqual(done['data']['route']['tier'],'high')
        self.assertEqual(self.s.pinned(sid),contract)
        self.assertGreater(review_budget.usage(self.s,sid)['used']['calls'],used['calls'])
        for stage,p in self.models:
            if stage not in ('subagent','synthesis'):continue
            packet=p.payloads[0]
            self.assertTrue(packet['excerpts']);self.assertTrue(packet.get('snapshot_receipts'))
            self.assertEqual(packet['contract'],contract)
            self.assertFalse(packet['contract']['constraints']['network_allowed'])
            self.assertFalse(packet['contract']['constraints']['edits_allowed'])
            self.assertIn('CED724',canonical(packet['memory']))

    def test_fresh_models_keep_context_permissions_budget_and_join(self):
        out=self.children();self.assertGreaterEqual(len(out['children']),2)
        contract=self.s.pinned(self.meta['active']);sid=self.meta['active']
        parent=m.node(self.s,out['children'][0]['id'])['parent_id'];before=m.spent(self.s,parent)
        out=self.step(out);after=m.spent(self.s,parent);self.assertGreater(after['calls'],before['calls'])
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');self.tier='high'
        out=self.until('DONE',out);self.assertTrue(out['data']['result']['admission']['admitted'],out['data']['result']['admission'])
        self.assertEqual(contract,self.s.pinned(sid));self.assertGreater(m.spent(self.s,parent)['calls'],after['calls'])
        children=[p for stage,p in self.models if stage=='subagent'];self.assertGreaterEqual(len({p.model for p in children}),2)
        for p in children:
            packet=p.payloads[0];self.assertEqual(packet['contract'],contract)
            self.assertIn('CED724',canonical(packet['memory']));self.assertEqual(packet['memory']['recent_turns'][-1]['user'],self.prompt)
            self.assertFalse(packet['branch']['capabilities']['edit']);self.assertFalse(packet['branch']['capabilities']['network'])
        main=[p for stage,p in self.models if stage=='synthesis'][0];self.assertEqual(len(main.payloads[0]['child_results']),len(children))
        for result in main.payloads[0]['child_results']:
            self.assertIn('Static calls',result['answer_unverified'])
            self.assertTrue(result['evidence_coverage']['excerpt_files'])
        self.assertEqual(main.payloads[0]['workspace_map']['source_files'],4)
        self.assertTrue(self.s.verify_invocations(sid));self.assertTrue(self.s.audit_valid())
    def test_replayed_ui_event_and_unarmed_step_are_rejected(self):
        out=self.step()
        with self.assertRaises(InvalidUpdate):self.step(out,armed=False)
        self.step(out)
        with self.assertRaisesRegex(InvalidUpdate,'stale'):self.step(out)
    def test_child_cannot_escape_permissions_or_role(self):
        out=self.children();n=m.node(self.s,out['children'][0]['id'])
        for kind,paths,role in [('edit',n['files'],None),('run_tests',n['files'],None),('model',n['files'],'coder'),('repository',['outside.py'],None)]:
            with self.subTest(kind=kind),self.assertRaises(InvalidUpdate):m.scope_check(n,kind,paths,role)
        wider=copy.deepcopy(n['permissions']);wider['network_allowed']=True
        with self.assertRaises(InvalidUpdate):m.subset(wider,n['permissions'])
    def test_budget_reservations_prevent_sibling_overspend_and_no_refund_of_calls(self):
        out=self.children();cid=out['children'][0]['id'];n=m.node(self.s,cid);parent=n['parent_id']
        frames=m.gates(self.s,parent,{'tokens':m.node(self.s,parent)['limits']['tokens']})
        self.assertNotEqual(frames[0]['decision'],'ALLOW')
        with self.assertRaises(m.OrchestrationStop):m.allowed(m.gates(self.s,cid,{'calls':n['limits']['calls']*3}))
        out=self.step(out);used=m.spent(self.s,parent)
        self.step(out,event='cancel');self.assertEqual(m.spent(self.s,parent),used)
        self.assertEqual(m.state(self.s,parent),'CANCELLED')
        task=m.node(self.s,parent)['parent_id']
        self.assertTrue(all(v==0 for v in m.gates(self.s,task,{})[0]['reserved_for_other_children'].values()))
    def test_source_change_after_child_completion_blocks_join(self):
        out=self.children();out=self.step(out);first=m.node(self.s,out['children'][0]['id'])
        (self.ws/first['files'][0]).write_text('def changed(): return 99\n')
        with self.assertRaises((InvalidUpdate,PageFault)):
            self.until('DONE',out)
    def test_contract_change_invalidates_children(self):
        out=self.children();cid=out['children'][0]['id'];sid=self.meta['active']
        from vscode_bridge import save_contract
        snap=self.s.snapshot(sid);save_contract(self.s,sid,{'goal':'Changed task','constraints':snap['constraints']},self.ws)
        with self.assertRaises(InvalidUpdate):m.checked(self.s,cid)
    def test_kernel_compound_fin_precedes_other_decisions(self):
        from karma_adapter import KarmaAdapter
        a=KarmaAdapter()
        self.assertEqual(a.allocations({'calls':0},{'calls':10},{'calls':1})['decision'],'ALLOW')
        self.assertEqual(a.allocations({'calls':0},{'calls':10},{'calls':15})['decision'],'BRIDGE')
        self.assertEqual(a.allocations({'calls':0},{'calls':10},{'calls':30})['decision'],'FIN')
        self.assertEqual(a.allocations({'calls':10},{'calls':10},{'calls':0})['decision'],'COMPOUND_FIN')

    def test_cancelled_call_recovery_keeps_allocation_and_context(self):
        out=self.children();cid=out['children'][0]['id'];parent=m.node(self.s,cid)['parent_id']
        from lifecycle import reconcile
        with patch('subagent_runtime.InvocationGateway.invoke',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.step(out)
        used=m.spent(self.s,parent)
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');reconcile(self.s,self.ws)
        self.assertEqual(m.spent(self.s,parent),used)
        self.tier='high';res=self.until('DONE',out)
        self.assertTrue(res['data']['result']['admission']['admitted'])
        self.assertIn('CED724',canonical([p.payloads for stage,p in self.models if stage=='subagent']))

    def test_transport_interruption_reconciles_child_and_charges_failed_call(self):
        out=self.children();cid=out['children'][0]['id'];parent=m.node(self.s,cid)['parent_id']
        before=m.spent(self.s,parent)
        with patch.object(Child,'call',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.step(out)
        self.assertEqual(m.state(self.s,cid),'RUNNING')
        from lifecycle import reconcile
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');reconcile(self.s,self.ws)
        self.assertEqual(m.state(self.s,cid),'READY')
        charged=m.spent(self.s,parent);self.assertEqual(charged['calls'],before['calls']+1)
        self.assertGreaterEqual(charged['seconds'],before['seconds']+150)
        self.tier='high';res=self.until('DONE',out);self.assertTrue(res['data']['result']['admission']['admitted'])
        self.assertGreater(m.spent(self.s,parent)['calls'],charged['calls'])

    def test_committed_fork_and_join_recover_without_duplicate_children(self):
        out=self.until('BRANCHING');n,d=flow.info(self.s,out['flow_id'])
        parent,children,_=flow.plan(self.s,self.meta,self.ws,n['prompt'],d['conversation_id'],d['root_node'])
        out=self.step(out);self.assertEqual([i['id'] for i in out['children']],[c['id'] for c in children])
        while any(i['state']!='DONE' for i in out['children']):out=self.step(out)
        ids=m.join(self.s,parent['id']);out=self.step(out);self.assertEqual(out['state'],'SYNTHESIS')
        res=self.step(out);self.assertTrue(res['data']['result']['admission']['admitted'])
        self.assertEqual(len([e for e in m.events(self.s,parent['id']) if e['kind']=='JOIN']),1)

    def test_forged_child_context_and_foreign_completion_are_rejected(self):
        out=self.children();cid=out['children'][0]['id'];other=out['children'][1]['id'];out=self.step(out)
        p=[p for stage,p in self.models if stage=='subagent'][0].payloads[0]
        import subagent_runtime
        for mutation in ('goal','memory','scope'):
            bad=copy.deepcopy(p)
            if mutation=='goal':bad['contract']['goal']='Different goal'
            elif mutation=='memory':bad['memory']['pinned_user_statements']=[]
            else:bad['source_paths']=['outside.py']
            with self.subTest(mutation=mutation),self.assertRaises(InvalidUpdate):subagent_runtime.request_check(self.s,self.meta['active'],bad)
        iid=m.events(self.s,cid)[-1]['data']['invocation_id']
        with self.assertRaises(InvalidUpdate):m.complete(self.s,other,iid)
        self.s.quarantine_invocation(self.meta['active'],iid,'test quarantine')
        with self.assertRaises((InvalidUpdate,PageFault)):self.until('DONE',out)

    def test_explicit_permission_grant_changes_scope_once_without_refunding_budget(self):
        self.operation='edit';out=self.until('NEED_PERMISSION');sid=self.meta['active'];old=review_budget.usage(self.s,sid)['used']
        self.assertFalse(self.s.snapshot(sid)['constraints']['edits_allowed'])
        with self.assertRaises(InvalidUpdate):self.step(out)
        out=self.step(out,event='grant',grant={'operation':'edit','paths':['part0.py'],'test_paths':[]})
        self.assertEqual(out['state'],'WORKSPACE');contract=self.s.pinned(sid)
        self.assertTrue(contract['constraints']['edits_allowed']);self.assertEqual(contract['constraints']['allowed_paths'],['part0.py'])
        now=review_budget.usage(self.s,sid)['used'];self.assertTrue(all(now[k]>=v for k,v in old.items()))
        self.tier='high';out=self.step(out);self.assertEqual(out['state'],'CODING');self.assertEqual(self.s.pinned(sid),contract)

    def test_unchanged_canonical_kernel(self):
        import hashlib
        self.assertEqual(hashlib.sha256((Path(__file__).parent/'stp_canonical/kernel.py').read_bytes()).hexdigest(),'9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac')

    def test_nonempty_calibration_binding_inherited_and_revocation_stops_children(self):
        import calibration
        from test_calibration import profile
        from vscode_bridge import handle
        handle(self.root,self.ws,{'op':'auto_new','goal':self.prompt,'roots':['.']})
        self.meta=json.loads((self.root/'workspace.json').read_text());sid=self.meta['active']
        ref=calibration.register(self.s,profile());bound=calibration.bind(self.s,sid,[ref],calibration.EMPTY_HASH)
        out=self.children();cid=out['children'][0]['id'];self.assertEqual(m.node(self.s,cid)['binding']['calibration_hash'],bound['hash'])
        out=self.step(out);worker=[p for stage,p in self.models if stage=='subagent'][0]
        self.assertEqual(worker.payloads[0]['contract']['calibration_binding'],bound)
        calibration.revoke(self.s,ref,'test revocation')
        before=len(self.models)
        blocked=self.step(out)
        self.assertFalse(blocked['recovery']['restart_allowed'])
        self.assertEqual(len(self.models),before)

    def test_oversized_fork_is_atomic_and_records_no_children(self):
        out=self.until('BRANCHING');_,data=flow.info(self.s,out['flow_id']);n=m.checked(self.s,data['root_node'])
        proposal={'objective':'Too large','files':['part0.py'],'roles':['subagent'],'permissions':n['permissions'],
                  'limits':{k:v*3 for k,v in n['limits'].items()}}
        before=self.s.db.execute('SELECT COUNT(*) FROM orchestration_nodes').fetchone()[0]
        with self.assertRaises(m.OrchestrationStop):m.fork(self.s,n['id'],[proposal],{}, {'request':self.prompt})
        self.assertEqual(m.state(self.s,n['id']),'READY');self.assertEqual(before,self.s.db.execute('SELECT COUNT(*) FROM orchestration_nodes').fetchone()[0])

    def test_explicit_read_only_review_uses_same_state_machine_without_classifier(self):
        from vscode_bridge import handle
        handle(self.root,self.ws,{'op':'auto_new','goal':self.prompt,'roots':['.']})
        self.meta=json.loads((self.root/'workspace.json').read_text());sid=self.meta['active']
        out=self.step(event='start_review',binding=flow.identity(self.s,sid))
        self.assertEqual(out['state'],'NEED_ARM');self.assertFalse(self.models)
        result=self.until('DONE',out);self.assertTrue(result['data']['result']['admission']['admitted'])
        self.assertFalse(any(stage=='conversation' for stage,p in self.models))

    def test_paused_mapping_explicit_resume_retains_binding_and_spent_budget(self):
        out=self.until('MAPPING');sid=self.meta['active'];contract=self.s.pinned(sid)
        with patch('auto_review.step',return_value={'auto':{'status':'resource_wait'}}):out=self.step(out)
        self.assertEqual(out['state'],'PAUSED');used=review_budget.usage(self.s,sid)['used']
        with self.assertRaisesRegex(InvalidUpdate,'Choose Resume'):self.step(out)
        with self.assertRaisesRegex(InvalidUpdate,'user-armed'):self.step(out,event='resume',armed=False)
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');self.tier='high'
        out=self.step(out,event='resume');self.assertEqual(out['state'],'MAPPING')
        self.assertEqual(used,review_budget.usage(self.s,sid)['used']);self.assertEqual(contract,self.s.pinned(sid))
        self.assertTrue(self.until('DONE',out)['data']['result']['admission']['admitted'])

    def test_context_shortage_pauses_and_resumes_synthesis_without_reclassification(self):
        out=self.until('SYNTHESIS');sid=self.meta['active'];contract=self.s.pinned(sid)
        used=review_budget.usage(self.s,sid)['used'];conversation_id=out['conversation_id']
        with patch('chat_runtime.answer',side_effect=ContextBudgetExceeded('Review allocation is too small.')):
            out=self.step(out)
        self.assertEqual(out['state'],'PAUSED')
        self.assertEqual(out['stop']['status'],'context_budget')
        self.assertEqual(out['stop']['stage'],'SYNTHESIS')
        self.assertEqual(review_budget.usage(self.s,sid)['used'],used)
        with self.assertRaisesRegex(InvalidUpdate,'Choose Resume'):self.step(out)
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');self.tier='high'
        pending=flow.handle(self.s,self.meta,self.ws,self.root,{'event':'pending'})
        self.assertEqual(pending['stop'],out['stop'])
        resumed=self.step(pending,event='resume')
        self.assertEqual(resumed['state'],'SYNTHESIS');self.assertIsNone(resumed['stop'])
        self.assertEqual(resumed['conversation_id'],conversation_id)
        self.assertEqual(review_budget.usage(self.s,sid)['used'],used)
        with patch('auto_review.step',side_effect=AssertionError('unnecessary remapping')):
            done=self.step(resumed)
        self.assertEqual(done['state'],'DONE');self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertEqual(self.s.pinned(sid),contract)

    def test_resource_wait_during_classification_needs_no_workspace_arm_and_rechecks_on_resume(self):
        from monitoring import ResourceWait
        blocked={'admitted':False,'available':True,'reason':'thermal pressure','thermal_label':'serious',
                 'decision':'BRIDGE','sample':{'memory_pressure_raw':1},'reclaimable_estimate_gib':20}
        with patch('conversation.answer',side_effect=ResourceWait(blocked)):
            out=self.step(armed=False)
        self.assertEqual(out['state'],'PAUSED');self.assertNotIn('active',self.meta)
        self.assertEqual(out['stop']['measurement'],blocked)
        self.assertEqual(out['stop']['resume_phase'],'CLASSIFYING')
        self.assertFalse(out['stop']['automatic_retry']);self.assertFalse(any(p.payloads for _,p in self.models))
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite')
        with patch('monitoring.resource_state',return_value=blocked):
            still=self.step(out,event='resume',armed=False)
        self.assertEqual(still['state'],'PAUSED');self.assertFalse(any(p.payloads for _,p in self.models))
        with patch('monitoring.resource_state',return_value={'admitted':True}):
            ready=self.step(still,event='resume',armed=False)
        self.assertEqual(ready['state'],'CLASSIFYING')
        classified=self.step(ready,armed=False)
        self.assertEqual(classified['state'],'NEED_ARM');self.assertNotIn('active',self.meta)

    def test_child_resource_pause_keeps_completed_sibling_and_model_switch_context(self):
        from monitoring import ResourceWait
        out=self.children();out=self.step(out);sid=self.meta['active']
        first=out['children'][0]['id'];parent=m.node(self.s,first)['parent_id']
        self.assertEqual(m.state(self.s,first),'DONE')
        used=m.spent(self.s,parent);contract=self.s.pinned(sid)
        blocked={'admitted':False,'available':True,'reason':'memory pressure','thermal_label':'nominal',
                 'decision':'FIN','sample':{'memory_pressure_raw':4},'reclaimable_estimate_gib':3}
        with patch('subagent_runtime.runtime.build_partition',side_effect=ResourceWait(blocked)):
            stopped=self.step(out)
        self.assertEqual(stopped['state'],'PAUSED');self.assertEqual(stopped['stop']['stage'],'CHILDREN')
        self.assertEqual(m.spent(self.s,parent),used)
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');self.tier='high'
        with self.assertRaisesRegex(InvalidUpdate,'user-armed'):self.step(stopped,event='resume',armed=False)
        with patch('monitoring.resource_state',return_value=blocked):
            still=self.step(stopped,event='resume')
        self.assertEqual(still['state'],'PAUSED');self.assertEqual(m.spent(self.s,parent),used)
        with patch('monitoring.resource_state',return_value={'admitted':True}):ready=self.step(still,event='resume')
        self.assertEqual(ready['state'],'CHILDREN')
        done=self.until('DONE',ready)
        self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertEqual(contract,self.s.pinned(sid))
        self.assertEqual(len([e for e in m.events(self.s,first) if e['kind']=='RESULT']),1)

    def test_resource_wait_in_mapping_and_synthesis_keeps_correct_resume_stage(self):
        from monitoring import ResourceWait
        blocked={'admitted':False,'available':False,'reason':'Hardware sample is stale or future-dated','decision':None}
        out=self.until('MAPPING');sid=self.meta['active'];used=review_budget.usage(self.s,sid)['used']
        # Exercise the real auto-review handler's typed exception path.
        with patch('static_runtime.build',side_effect=ResourceWait(blocked)):
            out=self.step(out)
        self.assertEqual(out['state'],'PAUSED');self.assertEqual(out['stop']['stage'],'MAPPING')
        after=review_budget.usage(self.s,sid)['used']
        self.assertEqual(after['calls'],used['calls']);self.assertEqual(after['tokens'],used['tokens'])
        self.assertGreaterEqual(after['operations'],used['operations'])
        with patch('monitoring.resource_state',return_value={'admitted':True}):out=self.step(out,event='resume')
        out=self.until('SYNTHESIS',out);used=review_budget.usage(self.s,sid)['used']
        with patch('chat_runtime.answer',side_effect=ResourceWait(blocked)):out=self.step(out)
        self.assertEqual(out['state'],'PAUSED');self.assertEqual(out['stop']['stage'],'SYNTHESIS')
        self.assertEqual(review_budget.usage(self.s,sid)['used'],used)
        with patch('monitoring.resource_state',return_value={'admitted':True}):out=self.step(out,event='resume')
        self.assertEqual(out['state'],'SYNTHESIS')
        self.assertTrue(self.step(out)['data']['result']['admission']['admitted'])

    def test_memory_release_then_model_switch_preserves_child_context_grants_and_spend(self):
        import os
        import runtime_memory as rm
        from monitoring import ResourceWait
        out=self.children();out=self.step(out);sid=self.meta['active']
        first=out['children'][0]['id'];parent=m.node(self.s,first)['parent_id']
        contract=self.s.pinned(sid);used=m.spent(self.s,parent)
        row={'name':'large','digest':'identity','size':10*rm.GIB,'context_length':65536}
        blocked={'available':False,'admitted':False,'reason':'memory pressure','decision':'BRIDGE'}
        healthy={'available':True,'admitted':True,'decision':'ALLOW','sample':{'wall_time':100}}
        with patch.dict(os.environ,{'SUTRA_RUNTIME_DIR':str(self.root/'runtime'),'SUTRA_MEMORY_OWNERSHIP':'exclusive'}),patch('monitoring.resource_state',return_value=blocked),patch.object(rm,'stable_resources',return_value=healthy),patch.object(rm,'running',side_effect=[[row],[],[]]),patch.object(rm,'api') as api:
            rm.write_state({'active':None,'owned':{'name':'large','digest':'identity','completed_at':1}})
            with patch('subagent_runtime.runtime.build_partition',side_effect=ResourceWait(blocked)):paused=self.step(out)
            self.assertEqual(paused['state'],'PAUSED');self.assertTrue(paused['stop']['recovery']['admitted'])
            self.assertEqual(paused['stop']['recovery']['action'],'released_owned_model')
            self.assertEqual(m.spent(self.s,parent),used)
            self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');self.tier='high'
            with self.assertRaisesRegex(InvalidUpdate,'user-armed'):self.step(paused,event='resume',armed=False)
            with patch('monitoring.require_resources',return_value=healthy):resumed=self.step(paused,event='resume')
            self.assertEqual(m.spent(self.s,parent),used)
            api.assert_called_once()
        done=self.until('DONE',resumed)
        self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertEqual(contract,self.s.pinned(sid))
        self.assertEqual(len([e for e in m.events(self.s,first) if e['kind']=='RESULT']),1)
        self.assertGreater(m.spent(self.s,parent)['calls'],used['calls'])
        children=[p for stage,p in self.models if stage=='subagent']
        self.assertGreaterEqual(len({p.model for p in children}),2)
        self.assertIn('CED724',canonical(children[-1].payloads))
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM page_audit WHERE event='MEMORY_RELEASE_PLANNED'").fetchone()[0],1)

    def test_mapping_failure_details_survive_reload_and_do_not_dispatch_children(self):
        out=self.until('MAPPING');sid=self.meta['active'];used=review_budget.usage(self.s,sid)['used']
        failure={'auto':{'status':'needs_attention','mapped_files':2,'source_files':9,
                        'failures':{f'part{i}.py':'Static graph work-unit allocation exceeded' for i in range(7)}}}
        with patch('auto_review.step',return_value=failure):out=self.step(out)
        self.assertEqual(out['state'],'PAUSED');self.assertEqual(out['stop']['failure_count'],7)
        self.assertEqual(len(out['stop']['failures']),5);self.assertEqual(out['stop']['mapped_files'],2)
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite')
        saved=flow.handle(self.s,self.meta,self.ws,self.root,{'event':'pending'})
        self.assertEqual(saved['stop'],out['stop']);self.assertFalse(saved['children'])
        self.assertEqual(review_budget.usage(self.s,sid)['used'],used)
        self.tier='high';resumed=self.step(saved,event='resume');self.assertIsNone(resumed['stop'])

    def test_calibrated_allocation_pause_then_model_switch_preserves_parent(self):
        import runtime_memory as rm
        from test_memory_profiles import profile
        from monitoring import ResourceWait
        out=self.children();out=self.step(out);sid=self.meta['active']
        first=out['children'][0]['id'];parent=m.node(self.s,first)['parent_id']
        spent=m.spent(self.s,parent);contract=self.s.pinned(sid)
        hardware={'sample':{'free_bytes':20*rm.GIB,'inactive_bytes':0}}
        plan=rm.estimate('large',{},8*rm.GIB,[],32768,hardware,profile())
        self.assertEqual(plan['decision'],'BRIDGE')
        def stopped(*args,**kwargs):rm.denied('Calibrated memory allocation does not fit',plan=plan)
        with patch('subagent_runtime.runtime.build_partition',side_effect=stopped):paused=self.step(out)
        self.assertEqual(paused['state'],'PAUSED');self.assertEqual(m.spent(self.s,parent),spent)
        self.assertEqual(paused['stop']['measurement']['calibration']['profile_sha256'],plan['calibration']['profile_sha256'])
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');self.tier='high'
        with self.assertRaisesRegex(InvalidUpdate,'user-armed'):self.step(paused,event='resume',armed=False)
        with patch('monitoring.resource_state',return_value={'admitted':True}):ready=self.step(paused,event='resume')
        self.assertEqual(m.spent(self.s,parent),spent)
        done=self.until('DONE',ready)
        self.assertTrue(done['data']['result']['admission']['admitted']);self.assertEqual(self.s.pinned(sid),contract)
        self.assertEqual(len([e for e in m.events(self.s,first) if e['kind']=='RESULT']),1)
        self.assertGreater(m.spent(self.s,parent)['calls'],spent['calls'])
        children=[p for stage,p in self.models if stage=='subagent']
        self.assertGreaterEqual(len({p.model for p in children}),2)
        self.assertIn('CED724',canonical(children[-1].payloads))

    def test_committed_child_receipt_recovers_without_duplicate_inference(self):
        out=self.children();cid=out['children'][0]['id']
        with patch('subagent_runtime.machine.complete',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.step(out)
        self.assertEqual(m.state(self.s,cid),'READY');used=m.spent(self.s,cid)
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite')
        with patch('subagent_runtime.InvocationGateway.invoke',side_effect=AssertionError('duplicate inference')):
            recovered=self.step(out)
        self.assertTrue(recovered['child']['recovered']);self.assertEqual(m.state(self.s,cid),'DONE');self.assertEqual(used,m.spent(self.s,cid))

    def test_pending_discovery_blocks_second_workflow_and_cancel_keeps_history(self):
        out=self.children();sid=self.meta['active'];used=review_budget.usage(self.s,sid)['used']
        found=flow.handle(self.s,self.meta,self.ws,self.root,{'event':'pending'})
        self.assertEqual(found['flow_id'],out['flow_id'])
        deferred=self.step()
        self.assertEqual(deferred['state'],'DONE')
        self.assertEqual(deferred['deferred_flow_id'],out['flow_id'])
        self.assertEqual(used,review_budget.usage(self.s,sid)['used'])
        with self.assertRaisesRegex(InvalidUpdate,'saved workflow'):
            self.step(event='start_existing',conversation_id=deferred['conversation_id'])
        inspected=self.step(out,event='inspect');self.assertEqual(inspected['prompt'],self.prompt)
        self.step(out,event='cancel');self.assertEqual(used,review_budget.usage(self.s,sid)['used'])
        self.assertIsNone(flow.handle(self.s,self.meta,self.ws,self.root,{'event':'pending'})['flow_id'])

    def test_greeting_beside_saved_review_preserves_context_budget_and_permissions(self):
        out=self.children();out=self.step(out);sid=self.meta['active']
        before=flow.response(self.s,out['flow_id']);contract=self.s.pinned(sid)
        used=review_budget.usage(self.s,sid)['used'];original=self.provider
        greeting=Conversation(MODELS['low'])
        self.provider=lambda stage:greeting if stage=='conversation' else original(stage)
        with patch('static_analysis.snapshot',side_effect=AssertionError('Greeting read source')):
            chat=self.step(prompt='hello there',armed=False)
        self.assertEqual(chat['state'],'DONE');self.assertNotIn('deferred_flow_id',chat)
        self.assertTrue(chat['conversation']['result']['admission']['admitted'])
        self.assertEqual(len(greeting.payloads),1)
        self.assertEqual(flow.response(self.s,out['flow_id']),before)
        self.assertEqual(self.s.pinned(sid),contract)
        self.assertEqual(review_budget.usage(self.s,sid)['used'],used)
        self.assertEqual(flow.handle(self.s,self.meta,self.ws,self.root,{'event':'pending'})['flow_id'],out['flow_id'])
        self.provider=original;self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');self.tier='high'
        done=self.until('DONE',out)
        self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertEqual(self.s.pinned(sid),contract)
        self.assertGreater(review_budget.usage(self.s,sid)['used']['calls'],used['calls'])
        self.assertIn('CED724',canonical(self.models[-1][1].payloads[0]['memory']))

    def test_new_classification_resource_stop_does_not_pause_saved_same_prompt(self):
        from monitoring import ResourceWait
        out=self.children();before=flow.response(self.s,out['flow_id'])
        with patch('conversation.answer',side_effect=ResourceWait({'admitted':False,'reason':'memory pressure'})):
            paused=self.step(armed=False)
        self.assertNotEqual(paused['flow_id'],out['flow_id'])
        self.assertEqual(paused['state'],'PAUSED')
        self.assertEqual(paused['stop']['resume_phase'],'CLASSIFYING')
        self.assertEqual(flow.response(self.s,out['flow_id']),before)

    def test_document_review_does_not_reuse_saved_architecture_task(self):
        saved=self.children()
        sid=self.meta['active']
        before=flow.response(self.s,saved['flow_id'])
        contract=self.s.pinned(sid)
        permissions=m.permissions(self.s,sid)
        usage=review_budget.usage(self.s,sid)
        prompt="Can you review this resume and comment? I am applying for post-silicon validation roles. The file is /Users/example/Downloads/Candidate Resume (1).docx"
        self.tier='high'
        with patch.object(self,'provider',side_effect=AssertionError('Document request dispatched a model')), \
                patch('repository_tools.execute',side_effect=AssertionError('Document request read the repository')):
            for event in ('start','start_existing','start_review'):
                with self.subTest(event=event):
                    result=self.step(event=event,prompt=prompt,armed=False)
                    self.assertEqual(result['state'],'DONE')
                    self.assertIsNone(result['conversation_id'])
                    self.assertIn('DOCX',result['request_notice'])
                    self.assertIn('paste',result['request_notice'].lower())
                    self.assertEqual(m.events(self.s,result['flow_id'])[-1]['kind'],'DOCUMENT_INPUT_REQUIRED')
        self.assertEqual(self.meta['active'],sid)
        self.assertEqual(flow.response(self.s,saved['flow_id']),before)
        self.assertEqual(self.s.pinned(sid),contract)
        self.assertEqual(m.permissions(self.s,sid),permissions)
        self.assertEqual(review_budget.usage(self.s,sid),usage)
        self.assertEqual(self.step(event='pending')['flow_id'],saved['flow_id'])

    def test_replacing_review_keeps_edit_intent_and_requires_permission(self):
        out=self.children();sid=self.meta['active'];self.operation='edit'
        edit_prompt='Remember project Cedar, ticket CED724. Edit part0.py'
        deferred=self.step(prompt=edit_prompt,armed=False)
        self.assertEqual(deferred['deferred_flow_id'],out['flow_id'])
        self.assertFalse(self.s.snapshot(sid)['constraints']['edits_allowed'])
        self.step(out,event='cancel')
        start=self.step(event='start_existing',prompt=edit_prompt,conversation_id=deferred['conversation_id'])
        self.assertEqual(start['state'],'NEED_ARM')
        with self.assertRaisesRegex(InvalidUpdate,'user-armed'):self.step(start,armed=False)
        needed=self.step(start)
        self.assertEqual(needed['state'],'NEED_PERMISSION')
        self.assertEqual(needed['permission']['operation'],'edit')
        self.assertFalse(self.s.snapshot(sid)['constraints']['edits_allowed'])

    def test_settlement_is_idempotent_and_illegal_transition_is_rejected(self):
        out=self.children();cid=out['children'][0]['id'];self.step(out);used=m.spent(self.s,cid)
        row=self.s.db.execute('SELECT id FROM orchestration_charges WHERE node_id=? LIMIT 1',(cid,)).fetchone()
        with self.s.db:m.settle(self.s,{'node_id':cid,'charge_id':row[0]},'repeat',99999)
        self.assertEqual(used,m.spent(self.s,cid))
        with self.s.db,self.assertRaisesRegex(InvalidUpdate,'forbidden'):m.transition(self.s,cid,'CLASSIFYING','BAD')

    def test_rejected_child_is_quarantined_and_cannot_join(self):
        out=self.children();cid=out['children'][0]['id'];original=Child.call
        def corrupt(child,*args):
            proposal,record=original(child,*args);proposal['constraints']=[];return proposal,record
        with patch.object(Child,'call',corrupt):out=self.step(out)
        self.assertEqual(m.state(self.s,cid),'QUARANTINED')
        self.assertEqual(m.spent(self.s,cid)['calls'],3)
        with self.assertRaisesRegex(InvalidUpdate,'quarantined'):self.step(out)

    def test_corrupt_transition_chain_is_detected_before_resume(self):
        out=self.children();fid=out['flow_id']
        # Simulate corruption outside the append-only API, not an allowed update.
        self.s.db.execute('DROP TRIGGER immutable_orchestration_events_UPDATE')
        self.s.db.execute("UPDATE orchestration_events SET digest='tampered' WHERE node_id=? AND seq=1",(fid,));self.s.db.commit()
        with self.assertRaisesRegex(InvalidUpdate,'corrupted'):self.step(out,event='inspect')

    def test_child_larger_than_page_allocation_pages_fresh_sources_across_model_switch(self):
        from vscode_bridge import handle
        import repository_tools as repo
        for i in range(6):
            text=f'def part{i}():\n    return {i}\n'+('# padding\n'*2200)
            (self.ws/f'part{i}.py').write_text(text)
        handle(self.root,self.ws,{'op':'auto_new','goal':self.prompt,'roots':['.'],'review_page_kib':32})
        self.meta=json.loads((self.root/'workspace.json').read_text())
        out=self.children();sid=self.meta['active'];contract=self.s.pinned(sid)
        parent=m.node(self.s,out['children'][0]['id'])['parent_id']
        before=m.spent(self.s,parent)
        out=self.step(out);after=m.spent(self.s,parent)
        self.assertGreaterEqual(after['operations']-before['operations'],2)
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');self.tier='high'
        result=self.until('DONE',out)
        self.assertTrue(result['data']['result']['admission']['admitted'])
        self.assertEqual(contract,self.s.pinned(sid))
        for stage,model in self.models:
            if stage!='subagent':continue
            packet=model.payloads[0];self.assertGreaterEqual(len(packet['snapshot_receipts']),2)
            files=set()
            for rid in packet['snapshot_receipts']:
                row=repo.record_by_id(self.s,sid,rid)
                self.assertLessEqual(row['result']['read_limit'],32*1024)
                self.assertLessEqual(sum(len(t.encode()) for t in row['result']['sources'].values()),32*1024)
                self.assertEqual(row['operation']['orchestration']['node_id'],packet['branch']['id'])
                files.update(row['result']['sources'])
            self.assertEqual(files,set(m.node(self.s,packet['branch']['id'])['files']))
        self.assertTrue(self.s.verify_invocations(sid))

    def test_policy_upgrade_offers_explicit_restart_without_refunding_or_reusing_children(self):
        out=self.children();out=self.step(out);sid=self.meta['active'];old_flow=out['flow_id']
        contract=self.s.pinned(sid);used=review_budget.usage(self.s,sid)['used'];calls=len(self.models)
        import conversation_memory as cm
        with patch('governance.policy_hash',return_value='f'*64), patch.object(cm,'LEGACY_POLICIES',cm.LEGACY_POLICIES|{self.s.logic_hash}):
            self.s.close();self.s=InvocationLedger(self.root/'state.sqlite')
            blocked=self.step(out)
            self.assertEqual(blocked['recovery']['kind'],'policy_changed')
            self.assertTrue(blocked['recovery']['restart_allowed'])
            self.assertEqual(len(self.models),calls)
            before=flow.response(self.s,old_flow)
            greeting=flow.handle(self.s,self.meta,self.ws,self.root,
                {'event':'start','prompt':'hello there','models':MODELS,'tier':'low','armed':False},
                lambda stage:Conversation())
            self.assertEqual(greeting['state'],'DONE')
            self.assertTrue(greeting['conversation']['result']['admission']['admitted'])
            self.assertEqual(flow.response(self.s,old_flow),before)
            self.assertEqual(review_budget.usage(self.s,sid)['used'],used)
            self.assertEqual(self.s.pinned(sid),contract)
            with self.assertRaisesRegex(InvalidUpdate,'user-armed'):self.step(blocked,event='restart_review',armed=False)
            restarted=self.step(blocked,event='restart_review')
            self.assertNotEqual(restarted['flow_id'],old_flow)
            self.assertEqual(m.state(self.s,old_flow),'CANCELLED')
            self.assertEqual(flow.info(self.s,restarted['flow_id'])[0]['prompt'],self.prompt)
            self.assertEqual(restarted['conversation_id'],out['conversation_id'])
            self.assertEqual(review_budget.usage(self.s,sid)['used'],used)
            self.assertEqual(self.s.pinned(sid),contract)
            self.tier='high';done=self.until('DONE',restarted)
            self.assertTrue(done['data']['result']['admission']['admitted'])
            self.assertEqual(self.s.pinned(sid),contract)
            self.assertGreater(review_budget.usage(self.s,sid)['used']['calls'],used['calls'])
            packet=[p for stage,p in self.models if stage=='synthesis'][-1].payloads[0]
            self.assertIn('CED724',canonical(packet['memory']))

    def test_changed_contract_does_not_allow_policy_only_restart(self):
        from vscode_bridge import save_contract
        out=self.children();sid=self.meta['active'];constraints=self.s.snapshot(sid)['constraints']
        save_contract(self.s,sid,{'goal':'Different user goal','constraints':constraints},self.ws)
        blocked=self.step(out)
        self.assertEqual(blocked['recovery']['kind'],'binding_changed')
        self.assertFalse(blocked['recovery']['restart_allowed'])
        with self.assertRaisesRegex(InvalidUpdate,'policy-only'):
            self.step(blocked,event='restart_review',binding=flow.identity(self.s,sid))

    def test_exact_full_page_allows_a_following_empty_source_file(self):
        import static_analysis
        text='def source(): return 1\n'
        (self.ws/'full.py').write_text(text);(self.ws/'empty.py').write_text('')
        page=static_analysis.snapshot(self.ws,['full.py','empty.py'],max_total=len(text.encode()))
        self.assertEqual(set(page['sources']),{'full.py','empty.py'})

    def test_compiler_config_is_not_a_separate_review_partition(self):
        from test_support import ENV,HAS_TYPESCRIPT
        if not HAS_TYPESCRIPT:self.skipTest('Node/TypeScript required')
        for path in self.ws.glob('*.py'):path.unlink()
        (self.ws/'entry.ts').write_text('export function main(){return 1;}\n')
        (self.ws/'tsconfig.json').write_text('{"compilerOptions":{"strict":true}}')
        with patch.dict('os.environ',ENV):out=self.until('SYNTHESIS')
        self.assertFalse(out['children'])

    def test_ranked_child_excerpts_keep_scope_after_canonical_storage(self):
        for i,name in enumerate(('main.py','a.py','b.py','c.py')):
            (self.ws/f'part{i}.py').rename(self.ws/name)
        out=self.children();out=self.step(out)
        worker=[p for stage,p in self.models if stage=='subagent'][0]
        packet=worker.payloads[0]
        self.assertNotEqual(list(packet['excerpts']),sorted(packet['excerpts']))
        self.assertEqual(packet['source_paths'],sorted(packet['excerpts']))
        from subagent_runtime import request_check
        for paths in (packet['source_paths']+packet['source_paths'][:1],['outside.py']):
            bad=copy.deepcopy(packet);bad['source_paths']=paths
            with self.assertRaisesRegex(InvalidUpdate,'scope changed'):request_check(self.s,self.meta['active'],bad)
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');self.tier='high'
        self.assertTrue(self.until('DONE',out)['data']['result']['admission']['admitted'])
