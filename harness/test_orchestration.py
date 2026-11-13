import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from context_manager import InvalidUpdate,canonical
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
        self.assertEqual(m.state(self.s,parent),'READY');self.assertTrue(all(v==0 for v in m.gates(self.s,parent,{})[0]['reserved_for_other_children'].values()))
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
        parent,children,_=flow.plan(self.s,self.meta,self.ws,n['prompt'],d['conversation_id'])
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
        with self.assertRaises((InvalidUpdate,PageFault)):self.step(out)

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
