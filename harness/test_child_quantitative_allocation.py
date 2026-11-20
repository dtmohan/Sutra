"""Settled amendments retain checkpoints and prior charges; no real inference."""
import copy,json,math,os,unittest
from unittest.mock import patch
import allocation_recovery as allocation
import orchestration as machine
import orchestration_flow as flow
import review_budget
import subagent_runtime
import test_orchestration as fixture
import workspace_allocation
from context_manager import InvalidUpdate,canonical
from invocation_gateway import InvocationLedger
from karma_adapter import KarmaAdapter
from test_model_packet import WireModel

class Child(WireModel):
    counts=(4000,749)
    continuing=True
    def call(self,*args):
        answer,record=super().call(*args)
        answer['slice_status']='continue' if self.continuing else 'complete'
        return answer,record

class QuantitativeChildTests(unittest.TestCase):
    tearDown=fixture.OrchestrationTests.tearDown
    step=fixture.OrchestrationTests.step
    until=fixture.OrchestrationTests.until
    def setUp(self):
        fixture.OrchestrationTests.setUp(self);self.continuing=True
        self.prompt='Explain the architecture and request flow. '+self.prompt
        for i in range(4):
            (self.ws/f'part{i}.py').write_text(f'def part{i}():\n    value = {i}\n'+''.join(
                f'    value += {line}  # source segment {line:04}\n' for line in range(1000))+'    return value\n')
    def provider(self,stage):
        if stage!='subagent':return fixture.OrchestrationTests.provider(self,stage)
        model=Child(fixture.MODELS[self.tier]);model.continuing=self.continuing
        self.models.append((stage,model));return model
    def records(self):
        return self.s.db.execute('SELECT i.id,i.record_json,r.record_json FROM invocations i LEFT JOIN invocation_receipts r ON r.invocation_id=i.id ORDER BY i.rowid').fetchall()
    def four_checkpoints(self):
        fork=machine.fork
        def bounded(s,nid,proposals,*args,**kwargs):
            proposals=copy.deepcopy(proposals)
            proposals[0]['limits'].update(seconds=372.931764888,tokens=52914.33333333333)
            return fork(s,nid,proposals,*args,**kwargs)
        with patch.object(machine,'fork',side_effect=bounded):out=self.until('CHILDREN')
        child,sibling=[r['id'] for r in out['children']];finalize=InvocationLedger.finalize;count=0
        def observed(s,envelope,payload,proposal,*args,**kwargs):
            nonlocal count
            if envelope['role']=='subagent' and envelope['orchestration']['node_id']==child:
                count+=1
                held=json.loads(s.db.execute('SELECT cost_json FROM orchestration_charges WHERE id=?',
                    (envelope['orchestration']['charge_id'],)).fetchone()[0])['seconds']
                target=276.02256995799985*count/4
                elapsed=target-(machine.spent(s,child)['seconds']-held)
                args=list(args)
                if len(args)>1:args[1]=elapsed
                else:kwargs['elapsed_seconds']=elapsed
            return finalize(s,envelope,payload,proposal,*args,**kwargs)
        with patch.object(InvocationLedger,'finalize',observed):
            for _ in range(4):
                out=self.step(out);self.assertEqual(out['child']['slice_status'],'checkpoint')
        self.assertEqual(count,4);self.assertEqual(machine.spent(self.s,child)['tokens'],18996)
        self.assertAlmostEqual(machine.spent(self.s,child)['seconds'],276.02256995799985,places=10)
        self.assertEqual(machine.slice_cursor(self.s,child)['ordinal'],5)
        with patch.dict(os.environ,{workspace_allocation.ENVIRONMENT_KEY:'{"enabled":false}'}):stopped=self.step(out)
        self.assertEqual(stopped['state'],'PAUSED')
        self.assertEqual(stopped['stop']['frame']['limiting_dimension'],'seconds')
        self.assertEqual(stopped['stop']['child_repair']['remaining_attempts'],3)
        return stopped,child,sibling
    def approve(self,stopped,additions=None):
        proposal=self.step(stopped,event='allocation_preview',armed=False,
            **({'additions':additions} if additions is not None else {}))
        return self.step(proposal,event='allocation_grant',armed=False,confirmed=True,
            proposal=proposal['allocation_proposal']),proposal['allocation_proposal']
    def test_four_checkpoint_seconds_grants55_in_place_reload_switch_retains_all_work(self):
        stopped,child,sibling=self.four_checkpoints();sid=self.meta['active'];execution=stopped['execution_id']
        ids=(child,sibling,execution);nodes={n:machine.node(self.s,n) for n in ids}
        limits={n:machine.effective_limits(self.s,nodes[n]) for n in ids};spent={n:machine.spent(self.s,n) for n in ids}
        cursor=machine.slice_cursor(self.s,child);notes=machine.slice_history(self.s,child,cursor)
        rows=self.records();contract=self.s.pinned(sid);before=review_budget.usage(self.s,sid)
        with self.assertRaisesRegex(InvalidUpdate,'measured child quantitative'):self.approve(stopped,{'tokens':55})
        granted,proposal=self.approve(stopped)
        self.assertEqual(proposal['additions'],{'seconds':55});self.assertEqual(proposal['child_allocation']['format'],2)
        self.assertEqual(granted['execution_id'],execution)
        self.assertEqual(granted['stop']['child_allocation_granted'],{'seconds':55})
        self.assertEqual([r['id'] for r in granted['children']],[child,sibling])
        for n in ids:
            expected=dict(limits[n])
            if n!=sibling:expected['seconds']+=55
            self.assertEqual(machine.effective_limits(self.s,nodes[n]),expected)
            self.assertEqual(machine.node(self.s,n),nodes[n]);self.assertEqual(machine.spent(self.s,n),spent[n])
        self.assertEqual(self.records(),rows);self.assertEqual(machine.slice_history(self.s,child,cursor),notes)
        after=review_budget.usage(self.s,sid)
        self.assertEqual(after['used'],before['used']);self.assertEqual(after['limits'],dict(before['limits'],seconds=before['limits']['seconds']+55))
        self.assertEqual(after['allocation_version'],1)
        self.assertTrue(all(f['decision']=='ALLOW' for f in machine.gates(self.s,child,stopped['stop']['frame']['reserved_next'])))
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');self.tier='high';self.continuing=False
        self.assertEqual(review_budget.usage(self.s,sid),after);resumed=self.step(granted,event='resume')
        with patch('repository_tools.execute',side_effect=AssertionError('source replay')):continued=self.step(resumed)
        self.assertTrue(continued['child']['result']['admission']['admitted']);packet=self.models[-1][1].payloads[0]
        self.assertEqual(packet['child_slice'],cursor);self.assertEqual(packet['contract'],contract)
        self.assertIn('CED724',canonical(packet['memory']));self.assertEqual(self.records()[:len(rows)],rows)
        self.assertEqual(machine.node(self.s,sibling),nodes[sibling])
        self.assertFalse(contract['constraints']['network_allowed']);self.assertFalse(contract['constraints']['edits_allowed'])
        self.assertTrue(self.s.verify_invocations(sid));self.assertTrue(self.s.audit_valid())
    def test_automatic_seconds_checks_ceiling_and_no_refund(self):
        stopped,child,sibling=self.four_checkpoints();sid=self.meta['active'];before=review_budget.usage(self.s,sid);rows=self.records()
        with patch.dict(os.environ,{workspace_allocation.ENVIRONMENT_KEY:json.dumps({'seconds':before['limits']['seconds']+54})}):
            self.assertEqual(workspace_allocation.try_amend(self.s,stopped['flow_id'],self.meta)['reason'],'workspace_limit')
        self.assertEqual(review_budget.usage(self.s,sid),before)
        with patch.dict(os.environ,{workspace_allocation.ENVIRONMENT_KEY:json.dumps({'seconds':before['limits']['seconds']+55})}):
            result=workspace_allocation.try_amend(self.s,stopped['flow_id'],self.meta)
        self.assertEqual(result['status'],'amended');self.assertEqual(result['dimension'],'seconds')
        self.assertEqual(result['amendment']['additions'],{'seconds':55})
        self.assertEqual(result['amendment']['authority'],'host_workspace_policy')
        self.assertEqual(review_budget.usage(self.s,sid)['used'],before['used']);self.assertEqual(self.records(),rows)
        self.assertEqual(machine.state(self.s,child),'READY');self.assertEqual(machine.state(self.s,sibling),'READY')
        self.assertTrue(self.s.audit_valid())
    def test_kernel_frames_calls_operations_source_bytes_and_legacy_tokens(self):
        out=self.until('CHILDREN');child,sibling=[r['id'] for r in out['children']]
        execution=out['execution_id'];sid=self.meta['active'];contract=self.s.pinned(sid)
        rows=self.records();siblings=machine.effective_limits(self.s,machine.node(self.s,sibling))
        for dimension in ('calls','operations','source_bytes','tokens'):
            with self.subTest(dimension=dimension):
                limits={nid:machine.effective_limits(self.s,machine.node(self.s,nid)) for nid in (child,execution)}
                before=review_budget.usage(self.s,sid);spent={nid:machine.spent(self.s,nid) for nid in limits}
                cost={dimension:math.floor(limits[child][dimension]-spent[child][dimension])+1}
                frame=next(f for f in machine.gates(self.s,child,cost) if f['decision']!='ALLOW')
                self.assertEqual(frame['limiting_dimension'],dimension)
                flow.move(self.s,out['flow_id'],'PAUSED','TEST_MEASURED_STOP',workflow_stop={
                    'status':'task_budget','stage':'CHILDREN','resume_phase':'CHILDREN',
                    'frame':frame,'child_repair':{'branch_id':child}})
                stopped=flow.response(self.s,out['flow_id']);out,proposal=self.approve(stopped)
                amount=proposal['additions'][dimension]
                for nid in limits:
                    self.assertEqual(machine.effective_limits(self.s,machine.node(self.s,nid)),
                        dict(limits[nid],**{dimension:limits[nid][dimension]+amount}))
                    self.assertEqual(machine.spent(self.s,nid),spent[nid])
                self.assertTrue(all(f['decision']=='ALLOW' for f in machine.gates(self.s,child,cost)))
                self.assertEqual(review_budget.usage(self.s,sid)['used'],before['used'])
                self.assertEqual(machine.effective_limits(self.s,machine.node(self.s,sibling)),siblings)
                events=machine.events(self.s,child)
                if dimension=='tokens':
                    self.assertNotIn('format',proposal['child_allocation'])
                    self.assertEqual(set(proposal['child_allocation']),{'branch','execution','tokens','minimum_context','kernel'})
                    self.assertEqual(events[-1]['kind'],'CHILD_ALLOCATION_INCREASED')
                    self.assertEqual(set(events[-1]['data']),{'tokens','allocation_receipt'})
                else:
                    self.assertEqual(proposal['child_allocation']['format'],2)
                    self.assertEqual(events[-1]['kind'],'CHILD_QUANTITATIVE_ALLOCATION_INCREASED')
        self.assertEqual(self.records(),rows);self.assertEqual(self.s.pinned(sid),contract)
        self.assertTrue(self.s.audit_valid())
    def test_real_pending_sibling_denies_manual_and_workspace_amendment(self):
        stopped,child,sibling=self.four_checkpoints();before=review_budget.usage(self.s,self.meta['active'])
        worker=Child(fixture.MODELS[self.tier])
        with patch.object(worker,'call',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                subagent_runtime.run(self.s,self.meta,self.ws,self.root,sibling,fixture.MODELS,self.tier,worker)
        self.assertEqual(machine.state(self.s,sibling),'RUNNING')
        self.assertIsNotNone(self.s.db.execute('SELECT c.id FROM orchestration_charges c LEFT JOIN '
            'orchestration_settlements r ON r.charge_id=c.id WHERE c.node_id=? AND r.charge_id IS NULL',
            (sibling,)).fetchone())
        with self.assertRaisesRegex(InvalidUpdate,'active task'):self.approve(stopped)
        self.assertEqual(workspace_allocation.try_amend(self.s,stopped['flow_id'],self.meta)['reason'],'pending_execution')
        self.assertEqual(review_budget.usage(self.s,self.meta['active'])['allocation_version'],before['allocation_version'])
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM allocation_amendments').fetchone()[0],0)
    def test_zero_cost_exhausted_operation_dimension_can_resume_same_child(self):
        fork=machine.fork
        def bounded(s,nid,proposals,*args,**kwargs):
            proposals=copy.deepcopy(proposals);proposals[0]['limits']['operations']=1
            return fork(s,nid,proposals,*args,**kwargs)
        with patch.object(machine,'fork',side_effect=bounded):out=self.until('CHILDREN')
        child=out['children'][0]['id']
        with patch.dict(os.environ,{workspace_allocation.ENVIRONMENT_KEY:'{"enabled":false}'}):stopped=self.step(out)
        self.assertEqual(stopped['state'],'PAUSED');frame=stopped['stop']['frame']
        self.assertEqual(frame['limiting_dimension'],'operations')
        self.assertEqual(frame['reserved_next'].get('operations',0),0)
        granted,proposal=self.approve(stopped)
        self.assertEqual(proposal['additions'],{'operations':1})
        self.assertEqual(machine.spent(self.s,child)['operations'],1)
        resumed=self.step(granted,event='resume');self.continuing=False
        continued=self.step(resumed)
        self.assertTrue(continued['child']['result']['admission']['admitted'])

if __name__=='__main__':unittest.main()
