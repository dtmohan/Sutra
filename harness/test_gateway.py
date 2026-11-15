import ast
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from budget_preflight import host_seed
from checked_protocol import SYSTEM, SCHEMA, worker_schema
from context_manager import InvalidUpdate
from dag_runtime import run_paged
from invocation_gateway import InvocationGateway, InvocationLedger, consume_dispatch
from legacy_model import EXTRACTOR, PATCH_SCHEMA, source_units
from local_model import LocalModel
from paging import Budget, PageFault
from protocol import SELECT_SYSTEM, SELECT_SCHEMA, schema_for
from runtime import ingest
from test_governance import ScriptModel, proposal
from validate_dag import build_new, new_definitions


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.db = self.root/'state.sqlite'
        s = InvocationLedger(self.db); s.create('active'); s.close()

    def tearDown(self): self.tmp.cleanup()

    def parse(self, value='false', source=0, message='No network access.', script=None):
        model = ScriptModel(script or (lambda n,p: {'operations':[{'key':'network_allowed','value':value,'source_unit':source}]}))
        s = InvocationLedger(self.db)
        try: result = ingest(s, 'active', message, model)
        finally: s.close()
        return result, model

    def fixture(self):
        definition = next(d for d in new_definitions() if d['id']=='N06-permitted-branch')
        with patch('experiment.ingest',host_seed): case = build_new(definition,self.root/'case',None)
        self.db = case['db']; return case

    def prepared(self, role='selector', model=None):
        case = self.fixture(); s = InvocationLedger(self.db)
        run = s.begin_run('active',case['request'],case['required_facts'],case['policy_request'])
        wanted = ['tests/test_policy.py'] + (case['seed_pages'] if role=='worker' else [])
        system = SYSTEM if role=='worker' else SELECT_SYSTEM
        schema = worker_schema if role=='worker' else lambda p:schema_for(p,SELECT_SCHEMA)
        payload,rid,_ = s.assemble('active',case['request'],wanted,required_facts=case['required_facts'],
            system=system,schema=SCHEMA if role=='worker' else SELECT_SCHEMA,policy_request=case['policy_request'],final=role=='worker')
        s.close()
        return InvocationGateway(self.db,run,model or ScriptModel(lambda n,p:proposal())),payload,rid,system,schema(payload)

    def test_bootstrap_parser_receipt_binds_zero_to_first_contract(self):
        result,_ = self.parse(); self.assertTrue(result['committed'])
        s = InvocationLedger(self.db); self.assertTrue(s.verify_invocations('active'))
        r = json.loads(s.db.execute('SELECT record_json FROM invocation_receipts').fetchone()[0])
        self.assertEqual(r['epoch'],0); self.assertNotEqual(r['contract_hash'],r['resulting_contract_hash'])
        self.assertFalse(s.snapshot('active')['constraints']['network_allowed']); s.close()

    def test_inverted_permission_is_never_committed_and_repairs_are_bounded(self):
        result,model = self.parse('true'); self.assertFalse(result['committed']); self.assertEqual(len(model.calls),2)
        s = InvocationLedger(self.db); self.assertEqual(s.snapshot('active')['revision'],0)
        self.assertTrue(s.verify_invocations('active')); s.close()

    def test_permission_mention_inside_other_sentence_is_not_authority(self):
        result,_ = self.parse('true',message='Do not assume network access is allowed.')
        self.assertFalse(result['committed'])

    def test_parser_repair_does_not_replay_previous_proposal(self):
        def script(n,p): return {'operations':[{'key':'network_allowed','value':'true' if n==1 else 'false','source_unit':0}]}
        result,model = self.parse(script=script); self.assertTrue(result['committed'])
        self.assertEqual(len(model.calls),2); self.assertNotIn('true',model.calls[1]['validation_error'])
        self.assertNotIn('operations',model.calls[1])

    def test_parser_rejects_bad_source_and_unexpected_fields(self):
        for script in (lambda n,p:{'operations':[{'key':'network_allowed','value':'false','source_unit':99}]},
                       lambda n,p:{'operations':[],'complete':True}):
            result,_ = self.parse(script=script); self.assertFalse(result['committed'])

    def test_parser_stale_response_does_not_overwrite_concurrent_amendment(self):
        def script(n,p):
            s=InvocationLedger(self.db);s.apply_user_patch('active','other','No new dependencies.',
                [{'key':'dependencies_allowed','value':'false','quote':'No new dependencies.'}],0);s.current_contract('active');s.close()
            return {'operations':[{'key':'network_allowed','value':'false','source_unit':0}]}
        result,model=self.parse(script=script);self.assertFalse(result['committed']);self.assertEqual(len(model.calls),1)
        s=InvocationLedger(self.db);self.assertNotIn('network_allowed',s.snapshot('active')['constraints']);s.close()

    def test_parser_commit_and_receipt_roll_back_together(self):
        original=InvocationLedger._audit
        def fail(s,event,payload):
            if event=='INVOCATION_RECEIPT':raise RuntimeError('simulated disk failure')
            return original(s,event,payload)
        with patch.object(InvocationLedger,'_audit',fail): result,_=self.parse()
        self.assertFalse(result['committed']);s=InvocationLedger(self.db)
        self.assertEqual(s.snapshot('active')['revision'],0)
        self.assertFalse(s.verify_invocations('active'));self.assertTrue(s.verify_invocations('active',complete=False))
        iid=s.db.execute('SELECT id FROM invocations').fetchone()[0]
        with self.assertRaises(PageFault):s.load_result('active',iid)
        s.close()

    def test_selector_valid_receipt_and_explicit_quarantine(self):
        model=ScriptModel(lambda n,p:{'read_pages':['src/policy.py'],'browse':[]})
        gate,payload,rid,system,schema=self.prepared(model=model)
        r=gate.invoke('selector','select',system,payload,schema,rid);self.assertTrue(r['admission']['admitted'])
        s=InvocationLedger(self.db);self.assertEqual(s.load_result('active',r['invocation_id']),r['accepted_output'])
        s.quarantine_invocation('active',r['invocation_id'],'revoked')
        with self.assertRaises(PageFault):s.load_result('active',r['invocation_id'])
        self.assertTrue(s.verify_invocations('active'));s.close()

    def test_model_retag_during_call_withholds_output_and_keeps_usage(self):
        from model_identity import unknown
        class LiveFixture(LocalModel):
            def call(self,cid,system,p,fmt):
                consume_dispatch(cid,self.model,system,p,fmt)
                return {'read_pages':['src/policy.py'],'browse':[]},{'response':{'prompt_eval_count':90,'eval_count':30}}
        model=LiveFixture(self.root/'calls',model='fixture')
        gate,payload,rid,system,schema=self.prepared(model=model)
        a=dict(unknown('fixture'),manifest_digest='before');b=dict(a,manifest_digest='after')
        with patch('model_identity.observe',side_effect=[a,b]),patch('monitoring.require_resources',return_value={'profile':'injected-test-provider'}):
            result=gate.invoke('selector','retag',system,payload,schema,rid)
        self.assertFalse(result['admission']['admitted'])
        self.assertIn('changed during inference',result['blocked'])
        self.assertEqual(result['record']['response']['prompt_eval_count'],90)
        self.assertEqual(result['token_usage']['totals']['input_tokens'],90)

    def test_selector_rejects_unknown_pages_and_bad_cursors(self):
        for answer in ({'read_pages':['../../secret'],'browse':[]},
                       {'read_pages':[],'browse':[{'group':'implementation','offset':999}]},
                       {'read_pages':[],'browse':[]}):
            # Reuse one prepared context for separate committed calls.
            if not hasattr(self,'selector_context'): self.selector_context=self.prepared()
            gate,payload,rid,system,schema=self.selector_context
            gate.provider=ScriptModel(lambda n,p:copy.deepcopy(answer))
            s=InvocationLedger(self.db);rid=s.prepare_delivery('active',payload,1000);s.close()
            r=gate.invoke('selector',str(answer),system,payload,schema,rid)
            self.assertIsNone(r['accepted_output']);s=InvocationLedger(self.db)
            with self.assertRaises(PageFault):s.load_result('active',r['invocation_id'])
            s.close()

    def test_worker_receipt_and_existing_assertion_receipts_both_present(self):
        c=self.fixture();model=ScriptModel(lambda n,p:proposal())
        result=run_paged(self.db,'active',c['request'],c['required_facts'],model,'worker',
            initial_resident=c['seed_pages'],policy_request=c['policy_request'])
        self.assertEqual(result['protocol']['state'],'ALLOW');self.assertEqual(len(result['receipt_ids']),3)
        s=InvocationLedger(self.db);self.assertEqual(s.db.execute('SELECT COUNT(*) FROM invocation_receipts').fetchone()[0],1)
        self.assertTrue(s.verify_invocations('active'));s.close()

    def test_worker_malformed_output_is_bounded_and_quarantined(self):
        c=self.fixture();model=ScriptModel(lambda n,p:{'unexpected':True})
        r=run_paged(self.db,'active',c['request'],c['required_facts'],model,'bad',
            initial_resident=c['seed_pages'],policy_request=c['policy_request'])
        self.assertIsNone(r['released_answer']);self.assertLessEqual(len(model.calls),3)
        s=InvocationLedger(self.db);self.assertTrue(s.verify_invocations('active'));s.close()

    def test_role_schema_mismatch_and_unknown_role_never_dispatch(self):
        gate,payload,rid,system,schema=self.prepared()
        for role,fmt in [('evaluator',schema),('selector',PATCH_SCHEMA)]:
            with self.assertRaises(InvalidUpdate):gate.invoke(role,'wrong',system,payload,fmt,rid)
        self.assertEqual(gate.provider.calls,[])

    def test_direct_transport_and_legacy_transport_are_blocked(self):
        m=LocalModel(self.root/'calls')
        with patch('http.client.HTTPConnection') as connection:
            with self.assertRaises(InvalidUpdate):m.call('bypass','system',{}, {})
            connection.assert_not_called()
        from legacy_model import LocalModel as Legacy
        with self.assertRaises(RuntimeError):Legacy(self.root/'legacy')

    def test_duplicate_call_and_shared_budget_prevent_additional_dispatch(self):
        gate,payload,rid,system,schema=self.prepared(model=ScriptModel(lambda n,p:{'read_pages':['src/policy.py'],'browse':[]}))
        gate.invoke('selector','unique',system,payload,schema,rid)
        with self.assertRaises(sqlite3.IntegrityError):gate.invoke('selector','unique',system,payload,schema,rid)
        self.assertEqual(len(gate.provider.calls),1)
        s=InvocationLedger(self.db);s.event(gate.run_id,'FIN',{'reason':'done'});s.close()
        with self.assertRaises(InvalidUpdate):gate.invoke('selector','new',system,payload,schema,rid)
        self.assertEqual(len(gate.provider.calls),1)

    def test_permit_is_single_use_and_bound_to_request(self):
        class Provider:
            budget=Budget();model='guard-test'
            def call(self,cid,system,payload,schema):
                consume_dispatch(cid,self.model,system,payload,schema)
                with self_test.assertRaises(InvalidUpdate):consume_dispatch(cid,self.model,system,payload,schema)
                return {'read_pages':['src/policy.py'],'browse':[]},{'call_id':cid}
        self_test=self
        gate,payload,rid,system,schema=self.prepared(model=Provider())
        self.assertTrue(gate.invoke('selector','permit',system,payload,schema,rid)['admission']['admitted'])
        with self.assertRaises(InvalidUpdate):consume_dispatch('permit','guard-test',system,payload,schema)

    def test_wrong_session_cannot_read_invocation_result(self):
        self.parse();s=InvocationLedger(self.db);s.create('other')
        iid=s.db.execute('SELECT id FROM invocations').fetchone()[0]
        with self.assertRaises(PageFault):s.load_result('other',iid)
        s.close()

    def test_receipts_immutable_and_rejected_rows_not_reusable(self):
        self.parse('true');s=InvocationLedger(self.db)
        for table in ('invocations','invocation_receipts'):
            with self.assertRaises(sqlite3.IntegrityError):s.db.execute('DELETE FROM '+table)
            s.db.rollback()
        for iid, in s.db.execute('SELECT id FROM invocations'):
            with self.assertRaises(PageFault):s.load_result('active',iid)
        s.close()

    def test_production_sources_have_one_provider_call_site(self):
        root=Path(__file__).parent;sites=[]
        for p in root.glob('*.py'):
            if p.name.startswith(('test_','validate_','audit_')):continue
            for node in ast.walk(ast.parse(p.read_text())):
                if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='call':
                    sites.append(p.name)
        self.assertEqual(sites,['invocation_gateway.py'])

    def lineage(self):
        c=self.fixture()
        def run(prefix,parents=()):
            return run_paged(self.db,'active',c['request'],c['required_facts'],ScriptModel(lambda n,p:proposal()),prefix,
                initial_resident=c['seed_pages'],policy_request=c['policy_request'],checkpoint_ids=parents)
        parent=run('parent');child=run('child',[parent['checkpoint_id']])
        return parent,child

    def test_checkpoint_quarantine_blocks_both_call_result_views(self):
        parent,child=self.lineage();s=InvocationLedger(self.db)
        self.assertEqual(s.load_result('active',child['invocation_id']),proposal())
        s.quarantine('active',parent['checkpoint_id'],'revoked checkpoint')
        for r in (parent,child):
            with self.assertRaises(PageFault):s.load_result('active',r['invocation_id'])
        s.close()

    def test_call_quarantine_blocks_checkpoint_and_descendants(self):
        parent,child=self.lineage();s=InvocationLedger(self.db)
        s.quarantine_invocation('active',parent['invocation_id'],'revoked invocation')
        for r in (parent,child):
            with self.assertRaises(PageFault):s.load_checkpoint('active',r['checkpoint_id'])
            with self.assertRaises(PageFault):s.load_result('active',r['invocation_id'])
        self.assertEqual(s.read_page('active','rule-0').authority,'user');s.close()

    def test_worker_call_result_waits_for_checkpoint_commit(self):
        gate,payload,rid,system,schema=self.prepared('worker')
        r=gate.invoke('worker','pending-checkpoint',system,payload,schema,rid)
        self.assertTrue(r['admission']['admitted']);s=InvocationLedger(self.db)
        with self.assertRaises(PageFault):s.load_result('active',r['invocation_id'])
        s.close()


if __name__=='__main__':unittest.main()
