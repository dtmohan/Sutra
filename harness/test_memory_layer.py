import copy
import json
import unittest
from unittest.mock import patch

from context_manager import ContextBudgetExceeded, canonical
from invocation_gateway import InvocationLedger
from paging import PagedContext, Budget, PageFault
import memory_layer as memory
import conversation_memory as cm
import orchestration as orchestration
import review_budget
import test_paging
import test_conversation
import test_conversation_memory
import test_orchestration


class PageLifecycleTests(unittest.TestCase):
    setUp = test_paging.PagingTests.setUp
    tearDown = test_paging.PagingTests.tearDown
    put = test_paging.PagingTests.put
    packet = test_paging.PagingTests.packet

    def deliver(self, required=(), resident=(), **kw):
        payload, rid, meta = self.packet(required, resident, **kw)
        self.s.confirm_delivery(rid)
        return payload, rid, meta

    def test_eviction_refault_reload_preserves_exact_data_and_contract(self):
        old = self.put('old', text='old '*1500)
        self.put('new', text='new '*150)
        original = self.s.pinned('A')
        self.deliver(['old'], budget=Budget(20000, 1024))
        payload, _, meta = self.deliver(['new'], ['old'])
        self.assertEqual(meta['evicted'], ['old'])
        self.assertEqual(payload['contract'], original)
        self.s.close(); self.s = PagedContext(self.db)
        self.assertEqual(self.s.read_page('A', 'old').payload_json, old.payload_json)
        self.deliver(['old'], budget=Budget(20000, 1024))
        stats = memory.snapshot(self.s.db)['counters']
        self.assertEqual(stats, dict(deliveries=3, page_ins=3, evictions=2, refaults=1))
        self.assertTrue(self.s.audit_valid())

    def test_reads_selection_validation_and_monitor_do_not_change_residency(self):
        self.put()
        self.deliver(['p'])
        before = self.s.db.total_changes
        for _ in range(4):
            self.s.report('A'); self.s.read_page('A','p'); memory.snapshot(self.s.db)
        self.assertEqual(self.s.db.total_changes, before)
        self.packet()  # Preparing a packet is not successful delivery.
        self.assertEqual(memory.snapshot(self.s.db)['counters']['deliveries'], 1)

    def test_duplicate_confirmation_rolls_back_diagnostics_atomically(self):
        self.put(); _, rid, _ = self.deliver(['p'])
        before = memory.snapshot(self.s.db)
        with self.assertRaises(Exception): self.s.confirm_delivery(rid)
        self.assertEqual(memory.snapshot(self.s.db), before)

    def test_interrupted_working_set_update_rolls_back_confirmation(self):
        self.put(); _, rid, _ = self.packet(['p'])
        before = memory.snapshot(self.s.db)
        with patch('memory_layer.event', side_effect=RuntimeError('Interrupted index write')):
            with self.assertRaises(RuntimeError): self.s.confirm_delivery(rid)
        self.assertFalse(self.s.db.execute('SELECT 1 FROM delivery_success WHERE id=?',(rid,)).fetchone())
        self.assertEqual(memory.snapshot(self.s.db),before)
        self.s.close();self.s=PagedContext(self.db)
        self.s.confirm_delivery(rid)
        self.assertEqual(memory.snapshot(self.s.db)['counters']['deliveries'],1)
        self.assertTrue(self.s.audit_valid())

    def test_source_excerpts_track_exact_text_and_span_without_becoming_cache(self):
        def deliver(text, start):
            packet={'contract':self.s.pinned('A'),'pages':[],
                    'excerpts':{'app.py':text} if text else {},
                    'excerpt_locations':{'app.py':{'start_line':start}}}
            rid=self.s.prepare_delivery('A',packet,1000)
            self.s.confirm_delivery(rid)
        deliver('return 1',1);deliver('',1);deliver('return 1',1)
        self.assertEqual(memory.snapshot(self.s.db)['counters']['refaults'],1)
        deliver('return 2',1);deliver('return 2',2)
        self.assertEqual(memory.snapshot(self.s.db)['counters']['refaults'],1)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM pages').fetchone()[0],0)

    def test_damaged_disposable_index_does_not_block_delivery_or_repair_evidence(self):
        self.put(); self.deliver(['p']); before=self.s.read_page('A','p')
        with self.s.db:
            self.s.db.execute("UPDATE memory_working_sets SET refs_json='invalid'")
            self.s.db.execute("UPDATE memory_events SET record_json='[]'")
        self.assertEqual(memory.snapshot(self.s.db)['diagnostic_issues'],1)
        self.deliver(['p'])
        last=memory.snapshot(self.s.db)['events'][0]
        self.assertTrue(last['diagnostic_history_gap'])
        self.assertEqual(self.s.read_page('A','p'),before)
        self.assertTrue(self.s.audit_valid())

    def test_versions_sessions_and_pinned_dependencies_do_not_alias(self):
        self.put('dep'); self.put('required', dependencies=[('dep',1)])
        self.put('optional')
        pages = {key:self.s.page('A',key) for key in ('dep','required','optional')}
        self.assertEqual(memory.eviction_closure(pages, {'required'}), {'optional'})
        with self.assertRaises(ContextBudgetExceeded):
            memory.eviction_closure({k:v for k,v in pages.items() if k!='optional'}, {'required'})
        self.deliver(['dep']); self.deliver()
        self.put('dep', text='changed', expected_version=1)
        self.deliver(['dep'])
        self.assertEqual(memory.snapshot(self.s.db)['counters']['refaults'], 0)
        with self.assertRaises(PageFault) as missing: self.s.read_page('B','dep')
        self.assertEqual(missing.exception.code, 'missing')
        with self.assertRaises(PageFault) as stale: self.s.read_page('A','required')
        self.assertEqual(stale.exception.code, 'stale_dependency')

    def test_fresh_source_fault_then_new_version_and_dependency(self):
        path = self.root/'source.py'; path.write_text('x=1')
        self.s.import_file('A','source',path)
        self.put('derived', dependencies=[('source',1)])
        path.write_text('x=2')
        with self.assertRaises(PageFault) as caught: self.s.read_page('A','source')
        self.assertEqual(caught.exception.details()['recovery'], 'refresh_source_and_dependents')
        memory.observe_fault(self.s,'A',caught.exception)
        self.s.import_file('A','source',path,expected_version=1)
        with self.assertRaises(PageFault): self.s.read_page('A','derived')
        self.put('derived',dependencies=[('source',2)],expected_version=1)
        self.assertEqual(self.s.read_page('A','derived').version,2)
        self.assertEqual(self.s.page('A','source',1).payload['text'],'x=1')

    def test_bounded_diagnostics_never_prune_pages_or_immutable_audit(self):
        self.put(); saved = self.s.page('A','p')
        with patch.dict(memory.LIMITS, memory_events=2, working_sets=2):
            for _ in range(5): self.deliver(['p'])
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM memory_events').fetchone()[0],2)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM memory_working_sets').fetchone()[0],2)
        self.assertEqual(self.s.page('A','p'),saved)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM delivery_success').fetchone()[0],5)
        self.assertTrue(self.s.audit_valid())


class ConversationLifecycleTests(unittest.TestCase):
    setUp = test_conversation.ConversationTests.setUp
    tearDown = test_conversation.ConversationTests.tearDown
    ask = test_conversation.ConversationTests.ask
    fill = test_conversation_memory.MemoryTests.fill

    def test_verified_receipt_rehydration_retains_pins_and_spent_budget(self):
        first = self.ask('Remember Project Orchid. No network. Keep the STP kernel unchanged.')
        sid, parent = first['session'], first['result']['invocation_id']
        before = review_budget.usage(self.s,sid)
        # The current turn has a committed receipt but no materialized page yet.
        with self.assertRaises(PageFault): self.s.read_page(sid,'conversation:'+parent)
        self.s.close();self.s = InvocationLedger(self.storage/'state.sqlite')
        cm.materialize(self.s,sid,cm.history(self.s,sid,parent))
        self.assertEqual(review_budget.usage(self.s,sid),before)
        page = self.s.read_page(sid,'conversation:'+parent)
        self.assertEqual(page.authority,'model')
        self.assertEqual(json.loads(page.payload['text'])['user'],first['result']['accepted_output'].get('request', 'Remember Project Orchid. No network. Keep the STP kernel unchanged.'))
        model = test_conversation.Fake('large')
        out = self.ask('What must remain unchanged?',parent,model,tier='high')
        self.assertTrue(out['result']['admission']['admitted'],out['result']['admission'])
        self.assertIn('Keep the STP kernel unchanged.',canonical(model.payloads[0]['memory']['pinned_user_statements']))
        after=review_budget.usage(self.s,sid)
        self.assertEqual(after['limits'],before['limits'])
        self.assertEqual(after['used']['calls'],before['used']['calls']+1)
        self.assertTrue(any(e['kind']=='restored' for e in memory.snapshot(self.s.db)['events']))

    def test_evicted_turn_can_be_requested_after_reload_switch_and_capacity_stop(self):
        first,parent=self.fill();sid=first['session']
        before=review_budget.usage(self.s,sid);contract=self.s.origin(sid)
        tiny=test_conversation.Fake('large');tiny.budget=Budget(4096,2048)
        with self.assertRaises(ContextBudgetExceeded): self.ask('Recall Orchid',parent,tiny,tier='high')
        self.assertFalse(tiny.payloads)
        self.assertEqual(review_budget.usage(self.s,sid),before)
        self.s.close();self.s=InvocationLedger(self.storage/'state.sqlite')
        target=first['result']['invocation_id']
        model=test_conversation.Fake('large',lambda r,p:r.update(page_requests=[target] if not p.get('requested_memory_pages') else []))
        out=self.ask('Recall an earlier topic',parent,model,tier='high')
        self.assertTrue(out['result']['admission']['admitted'],out['result']['admission'])
        self.assertEqual(self.s.origin(sid),contract)
        self.assertIn(target,[p['id'] for p in model.payloads[-1]['memory']['retrieved_turns']])
        self.assertEqual(review_budget.usage(self.s,sid)['used']['calls'],before['used']['calls']+2)

    def test_conversation_forks_use_parent_receipt_not_other_fork(self):
        first=self.ask('Remember seed');sid=first['session'];parent=first['result']['invocation_id']
        a=self.ask('branch A',parent)
        b=self.ask('branch B',parent)
        def delivery(iid):return self.s.db.execute('SELECT delivery_id FROM invocations WHERE id=?',(iid,)).fetchone()[0]
        for child in (a,b):
            previous=self.s.db.execute('SELECT previous_id FROM memory_working_sets WHERE delivery_id=?',(delivery(child['result']['invocation_id']),)).fetchone()[0]
            self.assertEqual(previous,delivery(parent))
        self.assertNotIn('branch A',canonical(cm.history(self.s,sid,b['result']['invocation_id'])))

    def test_quarantined_original_and_corrupt_page_never_rehydrate(self):
        first=self.ask();sid=first['session'];parent=first['result']['invocation_id']
        rows=cm.history(self.s,sid,parent);cm.materialize(self.s,sid,rows)
        with patch.object(self.s,'status',return_value='corrupt'):
            with self.assertRaises(PageFault) as caught: cm.materialize(self.s,sid,rows)
        self.assertEqual(caught.exception.code,'corrupt')
        self.s.quarantine_invocation(sid,parent,'Invalid original')
        with self.assertRaises(PageFault):cm.history(self.s,sid,parent)


class WorkflowMemoryTests(unittest.TestCase):
    setUp=test_orchestration.OrchestrationTests.setUp
    tearDown=test_orchestration.OrchestrationTests.tearDown
    provider=test_orchestration.OrchestrationTests.provider
    step=test_orchestration.OrchestrationTests.step
    until=test_orchestration.OrchestrationTests.until
    children=test_orchestration.OrchestrationTests.children

    def test_child_fault_reload_recovery_switch_preserves_sibling_and_budget(self):
        out=self.children();out=self.step(out);sid=self.meta['active']
        first=out['children'][0]['id'];parent=orchestration.node(self.s,first)['parent_id']
        spent=orchestration.spent(self.s,parent);contract=self.s.pinned(sid)
        fault=PageFault('Dependency source changed',code='source_changed',key='source')
        with patch('subagent_runtime.runtime.build_partition',side_effect=fault):stopped=self.step(out)
        self.assertEqual(stopped['state'],'PAUSED')
        self.assertEqual(stopped['stop']['memory_fault']['code'],'source_changed')
        self.assertFalse(stopped['stop']['automatic_retry'])
        self.assertEqual(orchestration.spent(self.s,parent),spent)
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');self.tier='high'
        from context_manager import InvalidUpdate
        with self.assertRaisesRegex(InvalidUpdate,'user-armed'):self.step(stopped,event='resume',armed=False)
        ready=self.step(stopped,event='resume')
        self.assertEqual(orchestration.spent(self.s,parent),spent)
        done=self.until('DONE',ready)
        self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertEqual(contract,self.s.pinned(sid))
        self.assertEqual(len([e for e in orchestration.events(self.s,first) if e['kind']=='RESULT']),1)

    def test_classification_fault_pauses_without_grant_or_inference(self):
        with patch('conversation.answer',side_effect=PageFault('Receipt corrupt',code='corrupt')):
            out=self.step(armed=False)
        self.assertEqual(out['state'],'PAUSED')
        self.assertEqual(out['stop']['memory_fault']['recovery'],'quarantine_and_inspect')
        self.assertNotIn('active',self.meta)
        self.assertFalse(any(p.payloads for _,p in self.models))
        self.assertEqual(self.step(out,event='resume',armed=False)['state'],'CLASSIFYING')
