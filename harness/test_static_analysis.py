import copy,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from context_manager import InvalidUpdate
from paging import Budget,PageFault
from invocation_gateway import InvocationLedger,consume_dispatch
from vscode_bridge import handle
from review_role import constraint_rows
import repository_tools as repo
import static_analysis as analysis
import static_runtime as runtime
import static_role as role

from test_support import ENV,requires_typescript
CASES={
 'direct':({'a.ts':'function leaf(){return 1;}\nexport function start(){return leaf();}'},1,0),
 'import_alias':({'a.ts':'import {leaf as run} from "./b";\nexport function start(){return run();}','b.ts':'export function leaf(){return 1;}'},1,0),
 'default_import':({'a.ts':'import run from "./b";\nexport function start(){return run();}','b.ts':'export default function leaf(){return 1;}'},1,0),
 'reexport':({'a.ts':'import {run} from "./b";\nexport function start(){return run();}','b.ts':'export {leaf as run} from "./c";','c.ts':'export function leaf(){return 1;}'},1,0),
 'const_alias':({'a.ts':'function leaf(){return 1;}\nconst run=leaf;\nexport function start(){return run();}'},1,0),
 'arrow':({'a.ts':'const leaf=()=>1;\nexport const start=()=>leaf();'},1,0),
 'typed_method':({'a.ts':'class Service { run(){return 1;} }\nexport function start(s:Service){return s.run();}'},1,0),
 'recursion':({'a.ts':'export function recur(n:number):number{return n?recur(n-1):0;}'},1,0),
 'mutual_recursion':({'a.ts':'function a(n:number):number{return n?b(n-1):0;}\nfunction b(n:number):number{return n?a(n-1):0;}'},2,0),
 'shadowed_parameter':({'a.ts':'function run(){return 1;}\nexport function start(run:()=>number){return run();}'},0,1),
 'external_method':({'a.ts':'export function start(x:string){return x.trim();}'},0,1),
 'mutable_alias':({'a.ts':'function leaf(){return 1;}\nlet run=leaf;\nexport function start(){return run();}'},0,1),
 'conditional_alias':({'a.ts':'function a(){return 1;} function b(){return 2;}\nexport function start(flag:boolean){const chosen=flag?a:b;return chosen();}'},0,1),
 'private_import':({'a.ts':'import {leaf} from "./b";\nexport function start(){return leaf();}','b.ts':'function leaf(){return 1;} export {};'},0,1),
 'missing_import':({'a.ts':'import {leaf} from "./missing";\nexport function start(){return leaf();}'},0,1),
 'invalid_argument_type':({'a.ts':'function leaf(x:number){return x;}\nexport function start(){return leaf("wrong");}'},1,0),
}

class StaticCompilerTests(unittest.TestCase):
 @requires_typescript
 def test_labelled_relationships(self):
  with patch.dict(os.environ,ENV):
   for label,(texts,edges,unknown) in CASES.items():
    with self.subTest(label=label):
     data=analysis.analyze(texts);self.assertEqual(sum(c['target'] is not None for c in data['calls']),edges);self.assertEqual(sum(c['target'] is None for c in data['calls']),unknown)
     if label=='recursion':self.assertEqual(data['calls'][0]['source'],data['calls'][0]['target'])
     if label=='direct':self.assertNotEqual(data['calls'][0]['source'],data['calls'][0]['target'])
 @requires_typescript
 def test_no_import_loading_or_execution(self):
  with patch.dict(os.environ,ENV):
   data=analysis.analyze({'a.ts':'/// <reference path="../../secret.ts" />\nimport {run} from "outside-package";\nthrow Error("never execute");\nrun();'})
   self.assertTrue(data['diagnostics']);self.assertTrue(all(c['target'] is None for c in data['calls']))
   self.assertEqual({n['path'] for n in data['nodes']},{'a.ts'})
 @requires_typescript
 def test_explicit_alias_config_and_unrelated_diagnostic(self):
  texts={'app/main.ts':'import {leaf} from "@lib/leaf"; import x from "absent"; export function run(){return leaf();}', 'shared/leaf.ts':'export function leaf(){return 1;}', 'tsconfig.json':'{"compilerOptions":{"baseUrl":".","paths":{"@lib/*":["shared/*"]}}}'}
  with patch.dict(os.environ,ENV):
   g=analysis.analyze(texts);self.assertIsNotNone(g['calls'][0]['target']);self.assertTrue(g['diagnostics']);self.assertEqual(g['profiles'],['typescript-program-v2'])
   texts['tsconfig.json']='{"compilerOptions":{"baseUrl":"../outside"}}'
   with self.assertRaises(InvalidUpdate):analysis.analyze(texts)
 @requires_typescript
 def test_type_only_import_cannot_be_a_call_edge(self):
  with patch.dict(os.environ,ENV):
   g=analysis.analyze({'a.ts':'import type {leaf} from "./b"; export function f(){return leaf();}','b.ts':'export function leaf(){return 1;}'})
   self.assertIsNone(g['calls'][0]['target'])
 @requires_typescript
 def test_nested_calls_have_distinct_identities(self):
  with patch.dict(os.environ,ENV):
   for files in ({'a.ts':'function leaf(){return "x";} export const x=leaf().trim();'}, {'a.py':'def leaf(): return "x"\nx=leaf().strip()\n'}):
    g=analysis.analyze(files);self.assertEqual(len(g['calls']),2);self.assertEqual(len({c['id'] for c in g['calls']}),2)
    self.assertEqual(len(analysis.compact(g,[c['id'] for c in g['calls']])['calls']),2)
 @requires_typescript
 def test_many_diagnostics_do_not_drop_source_mapping(self):
  with patch.dict(os.environ,ENV):
   g=analysis.analyze({'a.ts':'\n'.join(f'const x{i}=missing{i};' for i in range(300))+'\nfunction leaf(){return 1;} export const answer=leaf();'})
   self.assertGreater(g['diagnostic_count'],256);self.assertEqual(len(g['diagnostics']),256);self.assertTrue(g['diagnostics_truncated']);self.assertTrue(g['calls'][0]['target'])
 @requires_typescript
 def test_larger_source_allocation_supports_larger_graph_work_unit(self):
  with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,ENV):
   p=Path(tmp);(p/'many.ts').write_text('function leaf(){return 1;}\n'+'leaf();\n'*1200)
   with self.assertRaises(InvalidUpdate):analysis.snapshot(p,['many.ts'])
   data=analysis.snapshot(p,['many.ts'],max_total=65536);self.assertEqual(len(data['graph']['calls']),1200);self.assertEqual(data['graph']['item_budget'],8192)
 @requires_typescript
 def test_repeat_analysis_is_structurally_identical(self):
  with patch.dict(os.environ,ENV):
   a=analysis.analyze(CASES['reexport'][0]);b=analysis.analyze(CASES['reexport'][0]);a.pop('analysis_ms');b.pop('analysis_ms');self.assertEqual(a,b);self.assertFalse(a['cache_used'])
 def test_python_retains_conservative_profile(self):
  data=analysis.analyze({'a.py':'def recurse(n): return recurse(n-1) if n else 0\ndef f(callback): return callback()\n'})
  self.assertEqual([c['target'] is None for c in data['calls']],[False,True]);self.assertEqual(data['calls'][0]['source'],data['calls'][0]['target'])

class Fake:
 model='test-static-explainer';budget=Budget()
 def __init__(self,fault=None):self.fault=fault;self.calls=0;self.payload=None
 def call(self,cid,system,payload,schema):
  consume_dispatch(cid,self.model,system,payload,schema);self.calls+=1;self.payload=copy.deepcopy(payload)
  p={'task_id':payload['contract']['task_id'],'constraints':constraint_rows(payload['contract']),'complete':False,'summary':'Static relationships with unresolved callbacks.','unresolved_sites':[c['id'] for c in payload['facts']['calls'] if c['target'] is None]}
  if self.fault:self.fault(p)
  return p,{'ok':True,'answer':p,'fault_injection':bool(self.fault)}

class StaticProtocolTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.ws=self.root/'project';self.ws.mkdir();self.storage=self.root/'state'
  (self.ws/'a.py').write_text('from b import leaf\ndef start(): return leaf()\ndef callback(f): return f()\n');(self.ws/'b.py').write_text('def leaf(): return 1\n')
  self.contract=handle(self.storage,self.ws,{'op':'review_new','goal':'Explain current static relationships.','roots':['a.py','b.py'],'max_tools':12})['contract']
  self.meta=json.loads((self.storage/'workspace.json').read_text());self.s=InvocationLedger(self.storage/'state.sqlite');self.sid=self.meta['active']
 def tearDown(self):self.s.close();self.tmp.cleanup()
 def build(self):return runtime.build(self.s,self.meta,self.ws,self.storage,['a.py','b.py'],real=False)
 def explain(self,r,model=None,mode='compact'):
  return runtime.explain(self.s,self.meta,self.ws,self.storage,r['id'],'fake',mode=mode,provider=model or Fake())
 def test_every_ask_reindexes_and_consumes_budget(self):
  a=self.build();b=self.build();self.assertNotEqual(a['id'],b['id']);self.assertEqual(repo.usage(self.s,self.sid)['used'],3);self.assertFalse(a['result']['graph']['cache_used'])
 def test_changed_dependency_rejects_old_map_even_if_entry_is_unchanged(self):
  a=self.build();result=self.explain(a);before=(self.ws/'b.py').stat();(self.ws/'b.py').write_text('def leaf(): return 2\n');os.utime(self.ws/'b.py',ns=(before.st_atime_ns,before.st_mtime_ns))
  with self.assertRaises(InvalidUpdate):analysis.load(self.s,self.sid,self.ws,a['id'])
  with self.assertRaises((InvalidUpdate,PageFault)):self.s.load_result(self.sid,result['invocation_id'])
  b=self.build();self.assertNotEqual(a['result']['versions'],b['result']['versions'])
 def test_fresh_call_and_switch_preserve_contract(self):
  a=self.build();p=Fake();q=Fake();q.model='other-model';x=self.explain(a,p);y=self.explain(a,q)
  self.assertTrue(x['admission']['admitted'] and y['admission']['admitted']);self.assertEqual(self.contract,self.s.current_contract(self.sid));self.assertNotEqual(p.payload['_request_nonce'],q.payload['_request_nonce']);self.assertTrue(self.s.verify_invocations(self.sid))
 def test_missing_constraint_bounded_repair(self):
  a=self.build();m=Fake(lambda p:p['constraints'].pop());r=self.explain(a,m);self.assertFalse(r['admission']['admitted']);self.assertEqual(m.calls,3)
 def test_incorrect_unresolved_decision_quarantined(self):
  a=self.build();m=Fake(lambda p:p.update(unresolved_sites=[]));r=self.explain(a,m);self.assertFalse(r['admission']['admitted']);self.assertEqual(m.calls,3)
 def test_unsupported_self_loop_or_reversed_caller_in_packet_rejected(self):
  a=self.build();m=Fake();self.explain(a,m)
  for key in ('source','target'):
   p=copy.deepcopy(m.payload);c=next(c for c in p['facts']['calls'] if c['target']);c[key]=c['source'] if key=='target' else c['target']
   with self.assertRaises(InvalidUpdate):role.request_check(self.s,self.sid,p)
 def test_source_change_during_model_response_is_quarantined(self):
  a=self.build();m=Fake(lambda p:(self.ws/'b.py').write_text('def leaf(): return 9\n'))
  result=self.explain(a,m);self.assertFalse(result['admission']['admitted']);self.assertEqual(m.calls,1)
  self.assertEqual(self.s.run(result['run_id'])['events'][-1]['kind'],'FIN')
  row=self.s.db.execute('select record_json from invocation_receipts').fetchone();self.assertEqual(json.loads(row[0])['status'],'quarantined')
 def test_scope_and_duplicate_files_rejected(self):
  for files in (['../private.py'],['a.py','a.py'],['unknown.py']):
   with self.assertRaises(InvalidUpdate):runtime.build(self.s,self.meta,self.ws,self.storage,files,real=False)
 def test_full_source_and_compact_are_bound_to_same_snapshot(self):
  a=self.build();p=Fake();self.explain(a,p,'full_source');self.assertEqual(p.payload['source_text'],a['result']['sources']);p.payload['source_text']['b.py']='forged'
  with self.assertRaises(InvalidUpdate):role.request_check(self.s,self.sid,p.payload)
 def test_snapshot_receipt_is_immutable(self):
  import sqlite3
  self.build()
  with self.assertRaises(sqlite3.IntegrityError):self.s.db.execute("update repository_receipts set record_json='{}'")
  self.s.db.rollback()
 def test_ui_dispatch_rebuild_handoff_and_changed_source_progress_reset(self):
  p=Fake();q=Fake();q.model='switched-model'
  a=runtime.dispatch(self.s,self.meta,self.ws,self.storage,{'op':'static_explain','files':['a.py','b.py'],'model':p.model},p)
  rebuilt=runtime.dispatch(self.s,self.meta,self.ws,self.storage,{'op':'static_build'},p)
  self.assertEqual(rebuilt['static']['last_accepted']['invocation_id'],a['result']['invocation_id'])
  b=runtime.dispatch(self.s,self.meta,self.ws,self.storage,{'op':'static_explain','model':q.model,'offset':1},q)
  self.assertTrue(b['result']['admission']['admitted']);self.assertEqual(q.payload['handoff']['invocation_id'],a['result']['invocation_id']);self.assertEqual(p.payload['contract'],q.payload['contract']);self.assertNotEqual(a['snapshot_receipt'],b['snapshot_receipt']);self.assertEqual(len(b['static']['covered_sites']),2)
  (self.ws/'b.py').write_text('def leaf(): return 3\n')
  self.assertEqual(runtime.state(self.s,self.meta,self.ws)['status'],'withheld')
  c=runtime.dispatch(self.s,self.meta,self.ws,self.storage,{'op':'static_explain','model':q.model,'offset':1},q)
  self.assertNotIn('handoff',q.payload);self.assertTrue(c['static']['source_changed']);self.assertEqual(len(c['static']['covered_sites']),1)
 def test_failed_new_explanation_does_not_publish_previous_commentary(self):
  runtime.dispatch(self.s,self.meta,self.ws,self.storage,{'op':'static_explain','files':['a.py','b.py']},Fake())
  c=runtime.dispatch(self.s,self.meta,self.ws,self.storage,{'op':'static_explain'},Fake(lambda p:p['constraints'].pop()))
  self.assertEqual(c['static']['explanation_status'],'quarantined');self.assertIsNone(c['static']['invocation_id']);self.assertNotIn('commentary',c['static'])

if __name__=='__main__':unittest.main()
