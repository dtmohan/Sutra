import json
import sqlite3
import unittest
from unittest.mock import patch

from context_manager import ContextBudgetExceeded, InvalidUpdate, canonical
from invocation_gateway import InvocationLedger
import orchestration as m
import orchestration_flow as flow
import review_budget
import source_paging
import test_orchestration as fixtures


class SessionTests(unittest.TestCase):
    setUp = fixtures.OrchestrationTests.setUp
    tearDown = fixtures.OrchestrationTests.tearDown
    step = fixtures.OrchestrationTests.step
    until = fixtures.OrchestrationTests.until
    provider = fixtures.OrchestrationTests.provider

    def test_retry_reload_switch_preserve_execution_memory_and_spending(self):
        out = self.until('SYNTHESIS'); sid = self.meta['active']
        nid = out['execution_id']; node = m.node(self.s, nid)
        contract = self.s.pinned(sid); spent = review_budget.usage(self.s, sid)['used']
        with patch('chat_runtime.answer', side_effect=ContextBudgetExceeded('Pinned context does not fit')):
            paused = self.step(out)
        self.assertEqual(paused['execution_id'], nid)
        self.assertEqual(review_budget.usage(self.s, sid)['used'], spent)
        self.s.close(); self.s = InvocationLedger(self.root/'state.sqlite')
        self.tier = 'high'
        resumed = self.step(paused, event='resume')
        self.assertEqual(resumed['execution_id'], nid)
        self.assertEqual(m.node(self.s, nid), node)
        done = self.step(resumed)
        self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertEqual(m.state(self.s, nid), 'DONE')
        self.assertEqual(self.s.pinned(sid), contract)
        self.assertIn('CED724', canonical(self.models[-1][1].payloads[-1]['memory']))
        self.assertGreater(review_budget.usage(self.s, sid)['used']['calls'], spent['calls'])

    def test_new_request_uses_remaining_parent_capacity_and_keeps_history(self):
        done = self.until('DONE'); sid = self.meta['active']
        previous = m.node(self.s, done['execution_id']); spent = review_budget.usage(self.s, sid)
        self.prompt += ' Explain the entry point next.'
        new = self.until('WORKSPACE')
        node = m.node(self.s, new['execution_id'])
        self.assertNotEqual(node['id'], previous['id'])
        self.assertEqual(node['parent_id'], previous['parent_id'])
        self.assertEqual(review_budget.usage(self.s, sid), spent)
        for k in spent['limits']:
            self.assertAlmostEqual(node['limits'][k], spent['limits'][k]-spent['used'][k])
        self.assertEqual(node['permissions'], previous['permissions'])
        self.assertEqual(m.state(self.s, previous['id']), 'DONE')
        self.assertTrue(self.s.audit_valid())

    def test_unrelated_request_cannot_refill_paused_execution(self):
        out=self.until('SYNTHESIS'); sid=self.meta['active']
        with patch('chat_runtime.answer', side_effect=ContextBudgetExceeded('too large')):
            paused=self.step(out)
        before=review_budget.usage(self.s,sid)
        other=self.step(prompt=self.prompt+' Try the architecture review again with another model.')
        self.assertEqual(other['deferred_flow_id'],paused['flow_id'])
        self.assertEqual(review_budget.usage(self.s,sid),before)
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM orchestration_nodes WHERE json_extract(record_json,'$.kind')='execution'").fetchone()[0],1)

    def test_execution_is_immutable_and_cannot_widen_permissions(self):
        out=self.until('WORKSPACE'); n=m.node(self.s,out['execution_id'])
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.db.execute('UPDATE orchestration_nodes SET record_json=? WHERE id=?',('{}',n['id']))
        self.s.db.rollback()
        for kind in ('edit','run_tests'):
            with self.assertRaises(InvalidUpdate):m.scope_check(n,kind,['part0.py'])
        with self.assertRaises(m.OrchestrationStop):
            with m.scope(n['id']):m.admit(self.s,n['session'],'model','overspend',{'tokens':n['limits']['tokens']+1})

    def test_exhausted_parent_pauses_before_new_session_and_keeps_recovery_binding(self):
        out=self.step()
        with patch('execution_sessions.open_session',side_effect=m.OrchestrationStop([
                {'decision':'FIN','limiting_dimension':'source_bytes'}])):
            stopped=self.step(out)
        self.assertEqual(stopped['state'],'PAUSED')
        self.assertEqual(stopped['stop']['status'],'task_budget')
        self.assertIsNone(stopped['execution_id'])
        self.assertIsNotNone(stopped['binding'])
        self.assertEqual(m.node(self.s,flow.info(self.s,stopped['flow_id'])[1]['root_node'])['kind'],'task')
        proposal=self.step(stopped,event='allocation_preview')
        self.assertIn('allocation_proposal',proposal)

    def test_interrupted_binding_cannot_leave_an_unowned_execution_reservation(self):
        out=self.step();original=m.transition
        def fail(s,nid,target,kind,*args,**kw):
            if kind=='TASK_BOUND':raise RuntimeError('interrupted binding')
            return original(s,nid,target,kind,*args,**kw)
        with patch.object(m,'transition',side_effect=fail),self.assertRaises(RuntimeError):
            self.step(out)
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM orchestration_nodes WHERE json_extract(record_json,'$.kind')='execution'").fetchone()[0],0)
        current=flow.response(self.s,out['flow_id'])
        bound=self.step(current)
        self.assertEqual(bound['state'],'WORKSPACE')
        self.assertIsNotNone(bound['execution_id'])


class PagingTests(unittest.TestCase):
    setUp = fixtures.OrchestrationTests.setUp
    tearDown = fixtures.OrchestrationTests.tearDown
    step = fixtures.OrchestrationTests.step
    until = fixtures.OrchestrationTests.until
    provider = fixtures.OrchestrationTests.provider

    def test_saved_source_allowance_selects_smaller_fresh_page_no_refund(self):
        from vscode_bridge import handle
        from test_chat import Fake
        import auto_review
        import chat_runtime
        # A real small ceiling reaches the same state as the saved 64 MiB task:
        # full reread no longer fits, while a useful complete source page does.
        for i in range(4):
            (self.ws/f'part{i}.py').write_text(f'def part{i}():\n    return {i}\n'+'#padding\n'*60)
        handle(self.root,self.ws,{'op':'auto_new','goal':'Review the architecture. Remember Cedar CED724.',
                                  'roots':['.'],'review_source_kib':5})
        self.meta=json.loads((self.root/'workspace.json').read_text());sid=self.meta['active']
        auto_review.step(self.s,self.meta,self.ws,self.root,{'explain':False},provider=Fake())
        from static_runtime import build
        build(self.s,self.meta,self.ws,self.root,['part0.py','part1.py'],real=False)
        before=review_budget.usage(self.s,sid);contract=self.s.pinned(sid)
        model=Fake(fixtures.MODELS['low'])
        out=chat_runtime.answer(self.s,self.meta,self.ws,self.root,
              {'prompt':'Explain the architecture.','models':fixtures.MODELS,'tier':'low'},provider=model)
        self.assertTrue(out['result']['admission']['admitted'])
        self.assertGreater(out['coverage']['deferred_files'],0)
        self.assertLess(out['coverage']['fresh_selected_files'],4)
        after=review_budget.usage(self.s,sid)
        self.assertEqual(before['limits'],after['limits']);self.assertEqual(after['allocation_version'],0)
        self.assertGreater(after['used']['source_bytes'],before['used']['source_bytes'])
        self.assertLess(after['used']['source_bytes'],after['limits']['source_bytes'])
        self.assertEqual(self.s.pinned(sid),contract)
        self.assertTrue(self.s.db.execute("SELECT 1 FROM page_audit WHERE event='SOURCE_PAGE_PLAN'").fetchone())

    def test_no_useful_page_means_stop_without_dispatch_or_increase(self):
        out=self.until('WORKSPACE');sid=self.meta['active'];nid=out['execution_id']
        before=review_budget.usage(self.s,sid);calls=len(self.models)
        frames=m.gates(self.s,nid,{})
        for f in frames:f['limits']['source_bytes']=1
        with m.scope(nid),patch.object(m,'gates',return_value=frames):
            with self.assertRaises(InvalidUpdate):
                source_paging.select(self.s,sid,self.ws,['part0.py'],'test')
        self.assertEqual(review_budget.usage(self.s,sid),before)
        self.assertEqual(len(self.models),calls)

    def test_modified_source_is_not_reused_by_page_planning(self):
        out=self.until('WORKSPACE');sid=self.meta['active'];nid=out['execution_id']
        from static_runtime import build_partition,load_partition
        with m.scope(nid):
            chosen,_=source_paging.select(self.s,sid,self.ws,['part0.py'],'test')
            (self.ws/'part0.py').write_text('def changed(): return 724\n')
            receipts=build_partition(self.s,self.meta,self.ws,self.root,chosen,real=False)
            data=load_partition(self.s,sid,self.ws,receipts,nid)
        self.assertIn('changed',data['sources']['part0.py'])
        self.assertNotIn('return 0',data['sources']['part0.py'])


if __name__=='__main__':unittest.main()
