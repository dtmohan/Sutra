import copy,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from vscode_bridge import handle
from invocation_gateway import InvocationLedger
from context_manager import InvalidUpdate
from test_static_analysis import Fake
from kernel_adapter import HarnessKernelAdapter
import repository_tools as repo
import auto_review as auto
import review_budget as budget
import static_role

class AutomaticTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.ws=self.root/'repo';self.ws.mkdir();self.storage=self.root/'state'
  for i in range(140):(self.ws/f'file{i:03}.py').write_text('def leaf(): return 1\ndef start(): return leaf()\n#'+('padding '*90)+'\n')
  self.initial=handle(self.storage,self.ws,{'op':'auto_new','goal':'Review whole repository architecture.','roots':['.'],'review_page_kib':1})
  self.meta=json.loads((self.storage/'workspace.json').read_text());self.s=InvocationLedger(self.storage/'state.sqlite');self.sid=self.meta['active']
 def tearDown(self):self.s.close();self.tmp.cleanup()
 def step(self,model=None,**request):return auto.step(self.s,self.meta,self.ws,self.storage,request,provider=model or Fake())
 def test_discovers_over_128_and_preserves_progress_across_models_and_restart(self):
  self.assertEqual(self.initial['auto']['source_files'],140);first=Fake();second=Fake();second.model='other-model'
  a=self.step(first);b=self.step(second);self.assertTrue(a['result']['admission']['admitted']);self.assertTrue(b['result']['admission']['admitted']);self.assertEqual(first.payload['contract'],second.payload['contract']);self.assertEqual(b['auto']['mapped_files'],2)
  p=second.payload['review_progress']['progress'];self.assertEqual(p['prior_model'],first.model);self.assertEqual(p['mapped_before_this_page'],1);self.assertEqual(p['remaining_after_this_page'],138)
  (self.storage/'workspace.json').write_text(json.dumps(self.meta));restored=handle(self.storage,self.ws,{'op':'state'});self.assertEqual(restored['auto']['mapped_files'],2);self.assertEqual(restored['contract'],self.initial['contract'])
 def test_new_file_discovered_and_same_size_mtime_change_requeues_page(self):
  self.step(explain=False);page=self.meta['auto']['pages'][0];target=self.ws/page['primary_files'][0];st=target.stat();target.write_text(target.read_text().replace('return 1','return 2'));os.utime(target,ns=(st.st_atime_ns,st.st_mtime_ns))
  self.assertEqual(auto.status(self.s,self.meta,self.ws)['mapped_files'],0)
  (self.ws/'new.py').write_text('def new(): return 1\n');self.assertEqual(auto.status(self.s,self.meta,self.ws)['source_files'],141);out=self.step(explain=False);self.assertEqual(out['auto']['source_files'],141);self.assertTrue(out['auto']['stale_pages'])
 def test_large_python_file_completes_fresh_governed_mapping(self):
  (self.ws/'large.py').write_text('def leaf(): return 1\n'+'leaf()\n'*300)
  self.s.close();handle(self.storage,self.ws,{'op':'auto_new','goal':'Review large Python file.','roots':['large.py'],'review_page_kib':16})
  self.meta=json.loads((self.storage/'workspace.json').read_text());self.s=InvocationLedger(self.storage/'state.sqlite');self.sid=self.meta['active']
  contract=self.s.current_contract(self.sid);self.meta['auto'].update(error='OLD RESOURCE_WAIT',stop_frame={'decision':'FIN'})
  out=self.step(explain=False);self.assertEqual(out['auto']['status'],'structural_pass_complete');self.assertEqual(out['auto']['mapped_files'],1)
  self.assertEqual(out['auto']['last_page']['calls'],300);self.assertFalse(out['auto']['failures']);self.assertIsNone(out['auto']['error']);self.assertIsNone(out['auto']['stop_frame'])
  self.assertEqual(self.s.current_contract(self.sid),contract);self.assertEqual(budget.usage(self.s,self.sid)['used']['calls'],0)
  (self.ws/'large.py').write_text('def leaf(): return 2\n'+'leaf()\n'*300)
  self.assertEqual(auto.status(self.s,self.meta,self.ws)['mapped_files'],0)
  again=self.step(explain=False);self.assertEqual(again['auto']['status'],'structural_pass_complete')
  self.assertNotEqual(out['auto']['last_page']['receipt'],again['auto']['last_page']['receipt'])
 def test_corrupted_progress_receipt_rejected(self):
  m=Fake();self.step(m);p=copy.deepcopy(m.payload);p['review_progress']['progress']['mapped_before_this_page']=99
  with self.assertRaises(InvalidUpdate):static_role.request_check(self.s,self.sid,p)
 def test_repaired_source_reenters_failed_file_queue(self):
  p=self.ws/'file000.py';p.write_text('def broken(:\n')
  first=self.step(explain=False);self.assertIn('file000.py',first['auto']['failures'])
  p.write_text('def repaired(): return 1\n');second=self.step(explain=False)
  self.assertNotIn('file000.py',second['auto']['failures']);self.assertGreater(second['auto']['mapped_files'],0)
 def test_bytes_replace_eight_file_limit(self):
  for p in self.ws.glob('*.py'):p.write_text('x=1\n')
  r=self.step(explain=False);self.assertEqual(r['auto']['mapped_files'],140);self.assertEqual(r['auto']['status'],'structural_pass_complete');self.assertFalse(r['auto']['semantic_review_complete'])
 def test_legacy_failures_requeue_after_upgrade_without_refunding_or_changing_contract(self):
  self.step(explain=False);contract=self.s.current_contract(self.sid);used=budget.usage(self.s,self.sid)['used']
  a=self.meta['auto'];a.pop('failure_environment',None)
  auto.fail_page(a,self.ws,'file001.py','API index bound exceeded: old parser',1024)
  out=self.step(explain=False)
  self.assertNotIn('file001.py',out['auto']['failures']);self.assertEqual(out['auto']['mapped_files'],2)
  self.assertEqual(self.s.current_contract(self.sid),contract)
  now=budget.usage(self.s,self.sid)['used'];self.assertTrue(all(now[k]>=v for k,v in used.items()))
  events=self.s.db.execute("SELECT payload_json FROM page_audit WHERE event='AUTO_FAILURES_REQUEUED'").fetchall()
  self.assertEqual(len(events),1);self.assertEqual(json.loads(events[0][0])['paths'],['file001.py'])
  self.assertEqual(a['failure_environment']['policy_hash'],self.s.logic_hash)
 def test_same_failure_environment_retains_errors_and_tool_change_requeues_once(self):
  a=self.meta['auto'];a['failure_environment']=auto.failure_environment(self.s,self.sid)
  auto.fail_page(a,self.ws,'file000.py','fixture failure',1024)
  auto.requeue_stale_failures(self.s,self.meta);self.assertIn('file000.py',a['failures'])
  with patch.dict(os.environ,{'KARMA_NODE':'/different/trusted/node'}):
   auto.requeue_stale_failures(self.s,self.meta);self.assertFalse(a['failures'])
   auto.fail_page(a,self.ws,'file000.py','still failed',1024)
   auto.requeue_stale_failures(self.s,self.meta);self.assertIn('file000.py',a['failures'])
  self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM page_audit WHERE event='AUTO_FAILURES_REQUEUED'").fetchone()[0],1)
 def test_resource_wait_does_not_mark_a_source_file_as_permanently_failed(self):
  with patch('auto_review.runtime.build',side_effect=InvalidUpdate('RESOURCE_WAIT: pressure')):
   out=self.step(explain=False)
  self.assertEqual(out['auto']['status'],'resource_wait');self.assertFalse(out['auto']['failures'])
  self.assertEqual(self.step(explain=False)['auto']['mapped_files'],1)
 def test_allocations_use_kernel_and_preserve_compound_priority(self):
  for cost in ({'tokens':1},{'tokens':300000},{'tokens':600000}):
   frame=budget.decision(self.s,self.sid,cost);self.assertEqual(frame['decision'],HarnessKernelAdapter().evaluate(frame['omega'],frame['sigma']).decision.value)
  self.assertEqual(budget.decision(self.s,self.sid,{'tokens':300000})['decision'],'BRIDGE');self.assertEqual(budget.decision(self.s,self.sid,{'tokens':600000})['decision'],'FIN')
 def test_one_call_allocation_stops_without_forgetting_queue(self):
  self.s.close();self.initial=handle(self.storage,self.ws,{'op':'auto_new','goal':'Review all source.','roots':['.'],'review_page_kib':1,'review_calls':1});self.meta=json.loads((self.storage/'workspace.json').read_text());self.s=InvocationLedger(self.storage/'state.sqlite');self.sid=self.meta['active']
  first=Fake();self.step(first);again=Fake();r=self.step(again);self.assertEqual(again.calls,0);self.assertEqual(r['auto']['status'],'budget_stopped');self.assertEqual(r['auto']['stop_frame']['decision'],'COMPOUND_FIN');self.assertEqual(r['auto']['mapped_files'],1);self.assertEqual(r['auto']['pending_files'],139)
  with patch('repository_tools.require_resources',side_effect=InvalidUpdate('RESOURCE_WAIT: pressure')) as health:
   stopped=auto.step(self.s,self.meta,self.ws,self.storage,{},provider=None)
   self.assertEqual(stopped['auto']['stop_frame']['decision'],'COMPOUND_FIN');health.assert_not_called()
 def test_repair_calls_also_consume_global_allocation(self):
  self.s.close();handle(self.storage,self.ws,{'op':'auto_new','goal':'Review source.','roots':['.'],'review_page_kib':1,'review_calls':2});self.meta=json.loads((self.storage/'workspace.json').read_text());self.s=InvocationLedger(self.storage/'state.sqlite');self.sid=self.meta['active']
  m=Fake(lambda p:p['constraints'].pop());r=self.step(m);self.assertEqual(m.calls,2);self.assertEqual(r['auto']['status'],'budget_stopped');self.assertEqual(r['auto']['budget']['used']['calls'],2);self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM invocations WHERE session=?',(self.sid,)).fetchone()[0],2)
 def test_interrupted_dispatch_reservation_not_reset(self):
  m=Fake();self.step(m);frame=budget.decision(self.s,self.sid);self.assertEqual(frame['used']['calls'],1);self.assertGreater(frame['used']['tokens'],0)
  with patch.object(self.s,'snapshot',return_value={'constraints':dict(self.initial['contract']['contract']['constraints'],review_calls=1)}):self.assertEqual(budget.decision(self.s,self.sid)['decision'],'COMPOUND_FIN')

if __name__=='__main__':unittest.main()
