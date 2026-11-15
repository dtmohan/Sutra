from test_support import requires_typescript
import copy,json,os,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from context_manager import InvalidUpdate
from paging import Budget,PageFault
from invocation_gateway import InvocationLedger,consume_dispatch
from invocation_roles import validate_shape
from review_role import constraint_rows
from vscode_bridge import handle
from api_extract import extract
import api_facts as facts
import api_roles as roles
import api_runtime as runtime

TEXTS={'entry.py':'from worker import leaf as helper\ndef root():\n    return helper()\ndef recur(n):\n    return recur(n-1) if n else 0\ndef dynamic(callback):\n    return callback()\n',
       'worker.py':'def leaf():\n    return 1\ndef alternate():\n    return 2\n'}
class Model:
 model='fixture-mapper';budget=Budget()
 def __init__(self,data,fault=None):self.data=data;self.fault=fault;self.payloads=[]
 def call(self,cid,system,payload,schema):
  consume_dispatch(cid,self.model,system,payload,schema);self.payloads.append(copy.deepcopy(payload))
  p={'task_id':payload['contract']['task_id'],'constraints':constraint_rows(payload['contract']),'complete':False}
  inventory={c['id']:c for c in facts.inventory(self.data)}
  if 'claims' in schema['properties']:
   p.update(summary='API syntax map',claims={c['id']:{'source':c['caller'],'relation':'calls','target':inventory[c['id']]['target'] or '', 'evidence':[c['evidence']],'note':'Static lexical call.'} for c in payload['packet']['sites']})
  else:
   p['observations']={c['id']:{'source':c['caller'],'target':inventory[c['id']]['target'] or '', 'evidence':[c['evidence']],'reason':inventory[c['id']]['reason']} for c in payload['packet']['sites']}
  if self.fault:self.fault(p,payload)
  return p,{'ok':True,'answer':p,'call_id':cid,'fault_injection':bool(self.fault)}

class APIMapTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.ws=self.root/'project';self.ws.mkdir();self.storage=self.root/'store'
  for name,body in TEXTS.items():(self.ws/name).write_text(body)
  initial=handle(self.storage,self.ws,{'op':'review_new','goal':'Review API calls and recursion without executing project code.','roots':['.'],'max_tools':12})
  self.meta=json.loads((self.storage/'workspace.json').read_text());self.sid=self.meta['active'];self.s=InvocationLedger(self.storage/'state.sqlite');self.contract=initial['contract']
  self.data={name:extract(name,body) for name,body in TEXTS.items()}
 def tearDown(self):self.s.close();self.tmp.cleanup()
 def build(self,fault=None):return runtime.build(self.s,self.meta,self.ws,self.storage,{'files':list(TEXTS),'offset':0},Model(self.data,fault))
 def review(self,fault=None):
  m=Model(self.data,fault);m.model='fixture-small-reviewer';return runtime.verify(self.s,self.meta,self.ws,self.storage,{},m)
 def corrupt_self(self,p,payload):
  key=next(c['id'] for c in payload['packet']['sites'] if c['expression']=='helper');p['claims'][key]['target']=p['claims'][key]['source']
 def test_valid_map_recursion_unknown_and_model_handoff(self):
  total_supported=0;total_unknown=0;reports=[];offset=0
  while offset is not None:
   a=runtime.build(self.s,self.meta,self.ws,self.storage,{'files':list(TEXTS),'offset':offset},Model(self.data));self.assertTrue(a['result']['admission']['admitted']);self.assertIsNone(a['api']['report'])
   self.meta['model']='fixture-other';b=self.review();self.assertEqual(b['api']['status'],'reviewed')
   total_supported+=len(b['api']['decision']['verified_edges']);total_unknown+=len(b['api']['decision']['unresolved']);reports.append(b['api']['report']);offset=b['api']['position']['next_offset']
  self.assertEqual((total_supported,total_unknown),(2,1));self.assertIn('entry.py::recur',''.join(reports));self.assertIn('Unresolved:',''.join(reports));self.assertEqual(self.s.current_contract(self.sid),self.contract);self.assertTrue(self.s.verify_invocations(self.sid))
 def test_unsupported_self_loop_is_quarantined_even_when_reviewer_agrees(self):
  self.build(self.corrupt_self)
  def rubber_stamp(p,_):
   candidate=self.s.load_result(self.sid,self.meta['api_candidate'])
   for key,item in p['observations'].items():item.update(source=candidate['claims'][key]['source'],target=candidate['claims'][key]['target'])
  result=self.review(rubber_stamp);self.assertEqual(result['api']['status'],'quarantined');self.assertIsNone(result['api']['report']);self.assertTrue(result['api']['decision']['errors'])
 def test_review_records_unknown_separation_for_unidentified_fixture(self):
  self.build();result=self.review();sep=result['api']['independence']
  self.assertEqual(sep['weight_relation'],'unknown');self.assertIsNone(sep['same_model'])
  self.assertIn('Weight separation unknown',result['api']['report'])
 def test_strict_reviewer_rejects_unknown_before_dispatch(self):
  self.build();m=Model(self.data);m.model='other-name'
  before=self.s.db.execute('select count(*) from invocations').fetchone()[0]
  with self.assertRaisesRegex(InvalidUpdate,'not established'):
   runtime.verify(self.s,self.meta,self.ws,self.storage,{'require_distinct_reviewer':True},m)
  self.assertEqual(m.payloads,[])
  self.assertEqual(self.s.db.execute('select count(*) from invocations').fetchone()[0],before)
 def test_independent_reviewer_catches_corruption(self):
  self.build(self.corrupt_self);result=self.review();self.assertEqual(result['api']['status'],'quarantined');self.assertIn('contradicted',str(result['api']['reviewer_comparison']))
 def test_wrong_target_and_reversed_caller_rejected(self):
  for kind in ('target','source'):
   def corrupt(p,payload):
    key=next(c['id'] for c in payload['packet']['sites'] if c['expression']=='helper');item=p['claims'][key];item[kind]=item['source'] if kind=='target' else item['target']
   self.build(corrupt);self.assertEqual(self.review()['api']['status'],'quarantined')
 def test_legitimate_recursion_wrongly_rejected_by_reviewer_is_withheld(self):
  runtime.build(self.s,self.meta,self.ws,self.storage,{'files':list(TEXTS),'offset':1},Model(self.data))
  def reject(p,payload):
   key=next(c['id'] for c in payload['packet']['sites'] if c['expression']=='recur');p['observations'][key]['target']=''
  r=self.review(reject);self.assertEqual(r['api']['status'],'quarantined');self.assertTrue(any('disagreement' in e for e in r['api']['decision']['errors']))
 def test_omitted_site_or_constraints_exhausts_bounded_repair(self):
  for fn in (lambda p,_:p['claims'].pop(next(iter(p['claims']))),lambda p,_:p['constraints'].pop()):
   m=Model(self.data,fn);r=runtime.build(self.s,self.meta,self.ws,self.storage,{'files':list(TEXTS)},m);self.assertFalse(r['result']['admission']['admitted']);self.assertEqual(len(m.payloads),3)
 def test_fabricated_target_and_citation_rejected(self):
  for key,value in (('target','invented-node'),('evidence',['invented-evidence'])):
   def fault(p,_):next(iter(p['claims'].values()))[key]=value
   self.assertFalse(self.build(fault)['result']['admission']['admitted'])
 def test_reviewer_cannot_omit_a_verdict_or_its_callsite(self):
  self.build()
  for action in ('omit','citation'):
   def fault(p,payload):
    key=next(iter(p['observations']))
    if action=='omit':p['observations'].pop(key)
    else:p['observations'][key]['evidence']=[]
   self.assertFalse(self.review(fault)['result']['admission']['admitted'])
 def test_source_change_blocks_review_and_invalidates_published_map(self):
  self.build();self.review();(self.ws/'worker.py').write_text('def leaf():\n    return 99\n')
  self.assertEqual(runtime.state(self.s,self.meta,self.ws)['status'],'withheld')
  with self.assertRaises((InvalidUpdate,PageFault)):self.review()
 def test_candidate_quarantine_invalidates_review(self):
  self.build();self.review();self.s.quarantine_invocation(self.sid,self.meta['api_candidate'],'test')
  self.assertEqual(runtime.state(self.s,self.meta,self.ws)['status'],'withheld')
 def test_reviewer_payload_is_bound_to_candidate(self):
  self.build();m=Model(self.data);runtime.verify(self.s,self.meta,self.ws,self.storage,{},m);p=m.payloads[0];self.assertNotIn('candidate',p);p['readable_sites'][0]['containing_function']='forged'
  with self.assertRaises(InvalidUpdate):roles.request_check(self.s,self.sid,'api_reviewer',p)
 def test_review_ledger_is_append_only(self):
  self.build();self.review()
  with self.assertRaises(sqlite3.IntegrityError):self.s.db.execute("UPDATE api_reviews SET record_json='{}'")
  self.s.db.rollback();self.assertEqual(len(runtime.records(self.s,self.sid)),1)
 def test_read_scope_and_tool_budget_apply_to_indexing(self):
  with self.assertRaises(InvalidUpdate):runtime.build(self.s,self.meta,self.ws,self.storage,{'files':['../private.py']},Model(self.data))
  import repository_tools as repo
  self.build();used=repo.usage(self.s,self.sid)['used'];self.build();self.assertEqual(repo.usage(self.s,self.sid)['used'],used)
  self.assertTrue(all(r['operation']['kernel']['decision']=='ALLOW' for r in repo.records(self.s,self.sid)))
 def test_deterministic_source_parser_does_not_execute_code(self):
  p=self.ws/'marker';data=extract('bad.py','raise RuntimeError("never execute")\ndef f():\n    return f()\n');self.assertFalse(p.exists());self.assertEqual(facts.inventory({'bad.py':data})[-1]['target'],data['calls'][-1]['caller'])

class ExtractorTests(unittest.TestCase):
 def facts(self,text):return facts.inventory({'code.py':extract('code.py',text)})
 def test_same_file_and_mutual_recursion(self):
  rows=self.facts('def a():\n    return b()\ndef b():\n    return a()\n');self.assertEqual(rows[0]['target'],rows[1]['caller']);self.assertEqual(rows[1]['target'],rows[0]['caller'])
 def test_shadowing_and_rebinding_are_unknown(self):
  for text in ('def f(): pass\ndef g(f): return f()','def f(): pass\nf = other\nf()','from lib import f\ndef g(f): return f()'):
   self.assertIsNone(self.facts(text)[0]['target'])
 def test_dynamic_methods_decorators_and_classes_are_unknown(self):
  for text in ('def g(x): return x.run()','@decorator\ndef f(): return f()','class C:\n def f(self): return f()'):
   self.assertIsNone(self.facts(text)[-1]['target'])
 def test_import_alias_and_missing_definition(self):
  d={'a.py':extract('a.py','import b as other\ndef root(): return other.leaf()'),'b.py':extract('b.py','def leaf(): return 1')};self.assertIsNotNone(facts.inventory(d)[0]['target']);self.assertIsNone(facts.inventory({'a.py':d['a.py']})[0]['target'])
 def test_relative_import(self):
  d={'pkg/a.py':extract('pkg/a.py','from .b import leaf as call\ndef root(): return call()'),'pkg/b.py':extract('pkg/b.py','def leaf(): return 1')};self.assertIsNotNone(facts.inventory(d)[0]['target'])
 def test_duplicate_definition_is_unknown(self):
  self.assertIsNone(self.facts('def f(): return 1\ndef f(): return 2\ndef g(): return f()')[0]['target'])
 @requires_typescript
 def test_js_typescript_alias_recursion_and_type_only(self):
  from test_support import ENV as env
  with patch.dict(os.environ,env):
   d={'a.ts':extract('a.ts','import {leaf as helper} from "./b";\nexport function root(){return helper();}\nexport function recur(n:number):number{return n?recur(n-1):0;}'), 'b.ts':extract('b.ts','export function leaf(){return 1;}')}
   rows=facts.inventory(d);self.assertTrue(all(r['target'] for r in rows));self.assertEqual(rows[1]['target'],rows[1]['caller'])
   d['a.ts']=extract('a.ts','import type {leaf} from "./b";\nexport function root(){return leaf();}');self.assertIsNone(facts.inventory(d)[0]['target'])
   d['a.ts']=extract('a.ts','import {leaf} from "./b";\nexport function root(){return leaf();}');d['b.ts']=extract('b.ts','function leaf(){return 1;}');self.assertIsNone(facts.inventory(d)[0]['target'])
 def test_js_runtime_missing_is_explicit(self):
  with patch.dict(os.environ,{},clear=True):
   with self.assertRaisesRegex(InvalidUpdate,'parser runtime'):extract('a.js','function f(){return f();}')
