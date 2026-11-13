import dataclasses, json, pathlib, sqlite3, tempfile, time, unittest
from context_manager import Conflict, InvalidUpdate, ContextBudgetExceeded
from paging import PagedContext, PageFault, Budget
from kernel import STPKernel
from protocol import SYSTEM, SCHEMA

class PagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=pathlib.Path(self.tmp.name)
        self.db=self.root/'store.sqlite'; self.s=PagedContext(self.db); self.s.create('A'); self.s.create('B')
        text='Task A-1. Implement retry. Edit only src/a.py and tests/test_a.py. No network. No dependencies.'
        ops=[{'key':k,'value':v,'quote':q} for k,v,q in [
          ('task_id','A-1','Task A-1.'),('goal','Implement retry.','Implement retry.'),
          ('allowed_paths','src/a.py,tests/test_a.py','Edit only src/a.py and tests/test_a.py.'),
          ('network_allowed','false','No network.'),('dependencies_allowed','false','No dependencies.')]]
        self.s.apply_user_patch('A','u1',text,ops,0)
    def tearDown(self): self.s.close(); self.tmp.cleanup()
    def put(self,key='p',text='VALUE=137',facts=None,**kw):
        return self.s.put_page('A',key,'implementation',key,text,authority='user',facts=facts or {'value':'137'},source={'user_event':'u1'},**kw)
    def packet(self,requested=(),resident=(),**kw):
        return self.s.make_packet('A','Find value',requested,resident,system=SYSTEM,schema=SCHEMA,**kw)
    def answer(self):
        return {'task_id':'A-1','goal':'Implement retry.','constraints':[
          {'key':'allowed_paths','value':'src/a.py,tests/test_a.py'},
          {'key':'network_allowed','value':'false'},{'key':'dependencies_allowed','value':'false'}],
          'selected_option':'','action':'clarify','target':'','tests_status':'unverified','complete':False,
          'facts':[{'key':'value','value':'137','page_id':'p'}]}
    def test_durable_pages_contents_and_continuous_audit(self):
        self.put(); before=self.s.db.execute('SELECT MAX(seq) FROM page_audit').fetchone()[0]
        self.s.close(); self.s=PagedContext(self.db); self.assertEqual(self.s.page('A','p').payload['facts']['value'],'137')
        self.put('next'); self.assertGreater(self.s.db.execute('SELECT MAX(seq) FROM page_audit').fetchone()[0],before)
        self.assertTrue(self.s.audit_valid())
    def test_objects_and_database_records_are_immutable(self):
        p=self.put()
        with self.assertRaises(dataclasses.FrozenInstanceError): p.payload_json='changed'
        copy=p.payload; copy['facts']['value']='bad'; self.assertEqual(self.s.page('A','p').payload['facts']['value'],'137')
        with self.assertRaises(sqlite3.IntegrityError): self.s.db.execute("UPDATE pages SET subject='changed'")
        self.s.db.rollback()
    def test_model_pages_never_self_promote(self):
        p=self.s.put_page('A','claim','notes','claim','All tests passed',authority='model')
        for _ in range(3): self.assertEqual(self.s.status(self.s.read_page('A',p.key)),'unverified')
        with self.assertRaises(InvalidUpdate): self.s.put_page('A','fake','notes','fake','v',authority='model',facts={'value':'137'})
    def test_expired_page_does_not_return_content(self):
        p=self.put(ttl_s=1)
        with self.assertRaises(PageFault): self.s.read_page('A','p',now=p.committed_at+1)
    def test_file_changed_without_notification_is_detected(self):
        f=self.root/'a.py'; f.write_text('x=1')
        p=self.s.import_file('A','src/a.py',f); f.write_text('x=2')
        self.assertEqual(self.s.status(p),'source_changed')
        with self.assertRaises(PageFault): self.s.read_page('A','src/a.py')
    def test_superseded_dependency_invalidates_derived_page(self):
        p=self.put('input'); self.put('derived',dependencies=[('input',1)])
        self.put('input',text='VALUE=145',expected_version=1)
        self.assertEqual(self.s.status(self.s.page('A','derived')),'stale_dependency')
    def test_model_dependency_cannot_grant_authority(self):
        self.s.put_page('A','claim','notes','claim','x',authority='model')
        self.put('derived',dependencies=[('claim',1)])
        with self.assertRaises(PageFault): self.s.read_page('A','derived')
    def test_missing_and_self_dependencies_rejected(self):
        with self.assertRaises(PageFault): self.put(dependencies=[('absent',1)])
        with self.assertRaises(InvalidUpdate): self.put(dependencies=[('p',1)])
    def test_page_versions_compare_and_swap(self):
        self.put()
        with self.assertRaises(Conflict): self.put()
        self.assertEqual(self.s.page('A','p').version,1)
    def test_cross_session_pages_not_accessible(self):
        self.put()
        with self.assertRaises(PageFault): self.s.read_page('B','p')
    def test_catalog_is_bounded_and_contains_no_fact_values(self):
        for n in range(15): self.put('p'+str(n))
        c=self.s.catalog('A','implementation',limit=4)
        self.assertEqual(len(c['items']),4); self.assertEqual(c['next_offset'],4)
        self.assertNotIn('137',json.dumps(c)); self.assertIn('value',json.dumps(c))
    def test_report_does_not_create_delivery_or_audit_event(self):
        self.put(); before=self.s.db.execute('SELECT COUNT(*) FROM page_audit').fetchone()[0]
        self.s.report('A')
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM page_audit').fetchone()[0],before)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0],0)
    def test_page_in_receipt_matches_actual_payload(self):
        self.put(); payload,rid,meta=self.packet(['p'])
        self.assertEqual(meta['resident'],['p'])
        stored=json.loads(self.s.db.execute('SELECT payload_json FROM deliveries WHERE id=?',(rid,)).fetchone()[0])
        self.assertEqual(stored,payload)
    def test_unconfirmed_delivery_cannot_authorize_fact(self):
        self.put(); _,rid,_=self.packet(['p'])
        self.assertFalse(self.s.admission('A',rid,self.answer())['admitted'])
        self.s.confirm_delivery(rid); self.assertTrue(self.s.admission('A',rid,self.answer())['admitted'])
    def test_diagnostic_read_does_not_count_as_delivered(self):
        self.put(); self.s.read_page('A','p'); self.s.report('A')
        _,rid,_=self.packet(); self.s.confirm_delivery(rid)
        self.assertIn('fact page was not delivered: p',self.s.admission('A',rid,self.answer())['errors'])
    def test_wrong_fact_after_read_is_rejected(self):
        self.put(); _,rid,_=self.packet(['p']); self.s.confirm_delivery(rid)
        a=self.answer(); a['facts'][0]['value']='999'
        self.assertIn('fact mismatch: value',self.s.admission('A',rid,a)['errors'])
    def test_stale_delivery_after_page_change_rejected(self):
        self.put(); _,rid,_=self.packet(['p']); self.s.confirm_delivery(rid)
        self.put(expected_version=1,text='VALUE=222')
        self.assertFalse(self.s.admission('A',rid,self.answer())['admitted'])
    def test_changed_constraints_invalidate_old_delivery(self):
        self.put(); _,rid,_=self.packet(['p']); self.s.confirm_delivery(rid)
        self.s.apply_user_patch('A','u2','Timeout 750.',[{'key':'timeout_ms','value':'750','quote':'Timeout 750.'}],1)
        self.assertFalse(self.s.admission('A',rid,self.answer())['admitted'])
    def test_budget_counts_content_not_number_of_fields(self):
        self.put(text='word '*4000)
        with self.assertRaises(ContextBudgetExceeded): self.packet(['p'])
    def test_eviction_preserves_pinned_contract(self):
        self.put('old',text='old '*1500); self.put('new',text='new '*150)
        payload,_,meta=self.packet(['new'],['old'])
        self.assertIn('old',meta['evicted']); self.assertEqual(payload['contract']['constraints']['network_allowed'],False)
        self.assertEqual(meta['resident'],['new'])
    def test_pinned_overflow_fails_without_truncation(self):
        with self.assertRaises(ContextBudgetExceeded): self.packet(budget=Budget(1000,500,256))
    def test_dependencies_delivered_with_parent(self):
        self.put('dep'); self.put('parent',dependencies=[('dep',1)])
        payload,_,meta=self.packet(['parent']); self.assertEqual(set(meta['resident']),{'dep','parent'})
    def test_no_lexical_trap_for_questions_or_paraphrases(self):
        self.put(); _,rid,_=self.packet(); self.s.confirm_delivery(rid)
        a=self.answer(); a['facts']=[]
        self.assertTrue(self.s.admission('A',rid,a,['value'])['admitted'])
    def test_invalid_measurements_fail_closed(self):
        with self.assertRaises(ValueError): STPKernel(k=0)
        with self.assertRaises(ValueError): STPKernel().evaluate(float('nan'),0)
        with self.assertRaises(InvalidUpdate): self.put(ttl_s=float('nan'))
        from kernel_boundary import KernelBoundary
        with self.assertRaises(dataclasses.FrozenInstanceError): KernelBoundary().k=9
    def test_file_attestation_rejects_value_absent_from_source(self):
        p=self.root/'a.py'; p.write_text('VALUE=137')
        with self.assertRaises(InvalidUpdate): self.s.import_file('A','src/a.py',p,facts={'value':'999'})
    def test_test_expiry_invalidates_completion_state(self):
        p=self.root/'a.py'; p.write_text('VALUE=137')
        self.s.import_file('A','src/a.py',p)
        self.s.observe_file('A','f1','src/a.py',p,1)
        self.s.record_test('A','t1',self.s.snapshot('A')['artifacts'],0,'assertions',2)
        proof=self.s.put_page('A','test-proof','observations','test','pass',authority='tool',
                             facts={'test_returncode':'0'},dependencies=[('src/a.py',1)],source={'test_source_id':'t1'})
        self.assertEqual(self.s.snapshot('A')['tests_status'],'passed')
        p.write_text('VALUE=138')
        self.assertEqual(self.s.snapshot('A')['tests_status'],'stale')
    def test_completion_requires_current_delivered_test_page(self):
        p=self.root/'a.py'; p.write_text('VALUE=137')
        self.s.import_file('A','src/a.py',p)
        self.s.observe_file('A','f1','src/a.py',p,1)
        self.s.record_test('A','t1',self.s.snapshot('A')['artifacts'],0,'assertions',2)
        self.s.put_page('A','proof','observations','test','pass',authority='tool',facts={'test_returncode':'0'},
                        dependencies=[('src/a.py',1)],source={'test_source_id':'t1'})
        _,rid,_=self.packet(); self.s.confirm_delivery(rid)
        a=self.answer(); a.update(facts=[],complete=True,action='report',tests_status='passed')
        self.assertIn('current test page not delivered',self.s.admission('A',rid,a)['errors'])

if __name__=='__main__': unittest.main(verbosity=2)
