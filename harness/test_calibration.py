"""Synthetic protocol tests. These do not establish any empirical calibration."""
import copy,json,math,sqlite3,time,unittest
from pathlib import Path
from unittest.mock import patch
import calibration as c
from context_manager import InvalidUpdate,Conflict,ContextBudgetExceeded,sha,canonical
from invocation_gateway import InvocationLedger
from kernel_boundary import KernelBoundary
from smriti_adapter import SmritiAdapter
from karma_adapter import KarmaAdapter
from paging import Budget,PageFault
import test_vscode_bridge as fixtures
FakeModel=fixtures.FakeModel
from vscode_bridge import handle,state

def profile(adapter='karma',version=1):
    now=time.time()
    return dict(schema_version=1,profile_id=adapter+'-synthetic',version=version,adapter=adapter,
        domain='fixture-review',metric='declared_deviation',unit='fixture_points',
        measurement_method='synthetic-v1',producers=['fixture-A','fixture-B'],
        normalization=dict(method='linear_deviation',offset=0,scale=100,input_min=0,input_max=100,
                           sigma_definition='Synthetic independent accumulation signal; already normalized.'),
        kernel=dict(k=.15,sigma_clause=1.),valid_from=now-10,valid_until=now+3600,max_sample_age_seconds=60,
        evidence=dict(kind='synthetic',dataset_sha256=sha('fixture data'),report_sha256=sha('fixture report'),
                      calibration_samples=10,validation_samples=5,procedure='Manually chosen synthetic boundary fixtures.',
                      objective='Exercise protocol transitions only.',evaluator='unittest'),
        limitations='No empirical validity. ALLOW cannot authorize tools or verify semantic correctness.')

def sample(p,value=10,sigma=0):
    return dict({key:p[key] for key in ('domain','metric','unit','measurement_method')},producer='fixture-A',
                value=value,sigma=sigma,observed_at=time.time(),source='synthetic',evidence_sha256=sha('fixture observation'))

class CalibrationTests(unittest.TestCase):
    setUp=fixtures.VSCodeBridgeTests.setUp
    tearDown=fixtures.VSCodeBridgeTests.tearDown
    review=fixtures.VSCodeBridgeTests.review
    def select(self,adapter='karma',version=1):
        p=profile(adapter,version);r=c.register(self.s,p)
        b=c.bind(self.s,self.session,[r],c.current(self.s,self.session)['hash'])
        return p,r,b

    def test_registration_is_immutable_idempotent_and_sequential(self):
        p,r,b=self.select();self.assertEqual(c.register(self.s,p),r)
        changed=copy.deepcopy(p);changed['kernel']['k']=.2
        with self.assertRaises(Conflict):c.register(self.s,changed)
        changed['version']=3
        with self.assertRaises(InvalidUpdate):c.register(self.s,changed)
        changed['version']=2;r2=c.register(self.s,changed)
        self.assertNotEqual(r['hash'],r2['hash']);self.assertEqual(c.current(self.s,self.session),b)
        self.assertEqual(c.load(self.s,r)['kernel']['k'],.15)

    def test_schema_rejects_nonfinite_and_ambiguous_parameters(self):
        base=profile()
        mutations=[('version',True),('schema_version',2),('valid_until',math.inf),('max_sample_age_seconds',0),
                   ('producers',[]),('adapter','other'),('headroom_coefficient',.3)]
        for key,value in mutations:
            p=copy.deepcopy(base);p[key]=value
            with self.subTest(key=key),self.assertRaises(InvalidUpdate):c.register(self.s,p)
        for section,key,value in [('kernel','k',True),('kernel','k',math.nan),('kernel','k',0),
                                 ('kernel','k',1e308),('kernel','sigma_clause',0),('kernel','variance_coupling',.3),
                                 ('normalization','scale',0),('normalization','input_min',-1),
                                 ('evidence','dataset_sha256','not-a-hash'),('evidence','validation_samples',0)]:
            p=copy.deepcopy(base);p[section][key]=value
            with self.subTest(section=section,key=key,value=value),self.assertRaises(InvalidUpdate):c.register(self.s,p)

    def test_no_implicit_profile_and_no_cross_adapter_use(self):
        p=profile();r=c.register(self.s,p)
        with self.assertRaises(InvalidUpdate):KarmaAdapter().uncertain(self.s,self.session,r,sample(p))
        c.bind(self.s,self.session,[r],c.EMPTY_HASH)
        with self.assertRaises(InvalidUpdate):SmritiAdapter().uncertain(self.s,self.session,r,sample(p))
        bad=dict(r,hash='0'*64)
        with self.assertRaises(InvalidUpdate):c.load(self.s,bad)

    def test_both_adapters_have_identical_kernel_choices_and_priority(self):
        for adapter in (SmritiAdapter(),KarmaAdapter()):
            p,r,b=self.select(adapter.name)
            for value,sigma,expected in [(0,0,'ALLOW'),(15,0,'ALLOW'),(20,0,'BRIDGE'),(30,0,'BRIDGE'),
                                         (31,0,'FIN'),(0,1,'COMPOUND_FIN'),(100,-1,'COMPOUND_FIN')]:
                rec=adapter.uncertain(self.s,self.session,r,sample(p,value,sigma))
                self.assertEqual(rec['decision'],expected);self.assertEqual(rec['profile'],r)
                self.assertEqual(rec['binding'],b);self.assertFalse(rec['grants_tool_permission'])
                self.assertFalse(rec['semantic_correctness_verified']);self.assertEqual(rec['evidence_kind'],'synthetic')
                self.assertEqual(rec['decision'],KernelBoundary().evaluate(value/100,sigma).decision.value)
        self.assertTrue(c.verify_receipts(self.s,self.session))

    def test_units_producer_method_domain_range_and_sample_age_fail_closed(self):
        p,r,b=self.select()
        changes={'unit':'other','domain':'other','metric':'other','measurement_method':'other','producer':'unknown',
                 'value':101,'sigma':math.nan,'observed_at':time.time()-100,'source':'tool-proof','evidence_sha256':'bad'}
        for key,value in changes.items():
            obs=sample(p);obs[key]=value
            with self.subTest(key=key),self.assertRaises(InvalidUpdate):KarmaAdapter().uncertain(self.s,self.session,r,obs)
        for key,value in [('observed_at',time.time()+10),('value',-1),('value',True),('sigma',math.inf)]:
            obs=sample(p);obs[key]=value
            with self.assertRaises(InvalidUpdate):KarmaAdapter().uncertain(self.s,self.session,r,obs)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM calibration_receipts').fetchone()[0],0)

    def test_expiry_future_validity_revocation_and_unselection(self):
        p,r,b=self.select()
        for moment in (p['valid_from']-1,p['valid_until']):
            with patch('calibration.time.time',return_value=moment),self.assertRaises(InvalidUpdate):c.assert_binding(self.s,self.session,b)
        c.revoke(self.s,r,'Fixture no longer valid')
        with self.assertRaises(InvalidUpdate):c.assert_binding(self.s,self.session,b)
        self.assertIn('revoked',c.overview(self.s,self.session)['profiles'][0]['status'])
        empty=c.bind(self.s,self.session,[],b['hash']);self.assertEqual(empty['profiles'],[])
        self.assertNotEqual(empty['hash'],c.EMPTY_HASH)
        self.assertEqual(c.assert_binding(self.s,self.session,empty),empty)
        m=FakeModel();self.review(m)
        self.assertEqual(m.payloads[0]['contract']['calibration_binding'],empty)

    def test_duplicate_coverage_and_stale_selection_rejected(self):
        p,r,b=self.select();p['version']=2;r2=c.register(self.s,p)
        with self.assertRaises(InvalidUpdate):c.bind(self.s,self.session,[r,r2],b['hash'])
        with self.assertRaises(Conflict):c.bind(self.s,self.session,[],c.EMPTY_HASH)

    def test_fresh_model_switch_preserves_contract_selection_and_handoff(self):
        p,r,b=self.select();a=FakeModel();a.model='fixture-A';first=self.review(a)
        second=FakeModel();second.model='fixture-B';last=self.review(second,'tests/test_retry.py#1')
        self.assertTrue(first['result']['admission']['admitted']);self.assertTrue(last['result']['admission']['admitted'])
        self.assertEqual(a.payloads[0]['contract'],second.payloads[0]['contract'])
        self.assertEqual(second.payloads[0]['contract']['calibration_binding'],b)
        self.assertFalse(second.payloads[0]['contract']['constraints']['network_allowed'])
        self.assertEqual(second.payloads[0]['derived_from'],[first['result']['invocation_id']])
        self.assertNotIn('messages',second.payloads[0]);self.assertNotIn('history',second.payloads[0])
        for raw, in self.s.db.execute('SELECT record_json FROM invocation_receipts'):
            self.assertEqual(json.loads(raw)['calibration_binding'],b)
        self.assertTrue(self.s.verify_invocations(self.session))

    def test_selecting_new_version_withholds_old_result_without_resetting_usage(self):
        p,r,b=self.select();first=self.review();iid=first['result']['invocation_id']
        before=self.s.current_contract(self.session);used=self.s.db.execute('SELECT COUNT(*) FROM invocations').fetchone()[0]
        p['version']=2;p['kernel']['k']=.2;r2=c.register(self.s,p)
        self.assertIsNotNone(self.s.load_result(self.session,iid))
        c.bind(self.s,self.session,[r2],b['hash'])
        with self.assertRaises(PageFault):self.s.load_result(self.session,iid)
        st=state(self.s,self.meta,self.workspace);self.assertIsNone(st['checkpoint'])
        self.assertEqual(self.s.current_contract(self.session),before)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM invocations').fetchone()[0],used)
        next_model=FakeModel();self.review(next_model)
        self.assertEqual(next_model.payloads[0]['derived_from'],[])
        self.assertEqual(next_model.payloads[0]['contract']['calibration_binding']['profiles'],[r2])

    def test_mid_call_profile_change_quarantines_output(self):
        p,r,b=self.select()
        def change(answer):c.bind(self.s,self.session,[],b['hash'])
        try:self.review(FakeModel(change))
        except InvalidUpdate:pass  # The original run cannot reserve a repair under a new binding.
        rows=self.s.db.execute('SELECT record_json FROM invocation_receipts').fetchall();self.assertEqual(len(rows),1)
        receipt=json.loads(rows[0][0]);self.assertEqual(receipt['status'],'quarantined')
        self.assertIn('selection changed',' '.join(receipt['admission']['errors']))

    def test_mid_call_revocation_quarantines_and_blocks_further_dispatch(self):
        p,r,b=self.select();model=FakeModel(lambda answer:c.revoke(self.s,r,'Changed measurement regime'))
        try:self.review(model)
        except InvalidUpdate:pass
        receipt=json.loads(self.s.db.execute('SELECT record_json FROM invocation_receipts').fetchone()[0])
        self.assertEqual(receipt['status'],'quarantined');self.assertEqual(len(model.payloads),1)
        future=FakeModel()
        with self.assertRaises(InvalidUpdate):self.review(future)
        self.assertEqual(future.payloads,[])

    def test_expired_profile_blocks_before_provider(self):
        p,r,b=self.select();m=FakeModel()
        with patch('calibration.time.time',return_value=p['valid_until']),self.assertRaises(InvalidUpdate):self.review(m)
        self.assertFalse(m.payloads)

    def test_permissive_uncertain_profile_cannot_authorize_tools_or_completion(self):
        p=profile();p['kernel']['k']=100;r=c.register(self.s,p);c.bind(self.s,self.session,[r],c.EMPTY_HASH)
        self.assertEqual(KarmaAdapter().uncertain(self.s,self.session,r,sample(p,100))['decision'],'ALLOW')
        model=FakeModel(lambda a:a.update(complete=True));result=self.review(model)
        self.assertFalse(result['result']['admission']['admitted'])
        self.assertFalse(self.s.snapshot(self.session)['constraints']['network_allowed'])
        self.assertEqual(KarmaAdapter().counted_slots(3,3)['decision'],'COMPOUND_FIN')
        with self.assertRaises(ContextBudgetExceeded):Budget(10,5).check('oversized',{}, {})

    def test_restart_and_readonly_dashboard_preserve_integrity(self):
        p,r,b=self.select();KarmaAdapter().uncertain(self.s,self.session,r,sample(p))
        other=InvocationLedger(self.storage/'state.sqlite')
        try:
            before=other.db.total_changes;view=c.overview(other,self.session)
            self.assertEqual(other.db.total_changes,before);self.assertEqual(view['binding'],b)
            self.assertTrue(view['receipts_valid']);self.assertEqual(view['last_evaluation']['decision'],'ALLOW')
        finally:other.close()
        for table in ('calibration_profiles','calibration_bindings','calibration_receipts'):
            with self.assertRaises(sqlite3.DatabaseError):self.s.db.execute('DELETE FROM '+table)
            self.s.db.rollback()
        c.revoke(self.s,r,'fixture');self.assertTrue(c.verify_receipts(self.s,self.session))
        with self.assertRaises(sqlite3.DatabaseError):self.s.db.execute('DELETE FROM calibration_revocations')
        self.s.db.rollback()

    def test_bridge_import_selection_and_evaluation(self):
        p=profile('smriti');r=handle(self.storage,self.workspace,{'op':'calibration_register','profile':p})['registered']
        result=handle(self.storage,self.workspace,{'op':'calibration_bind','profiles':[r],'expected_hash':c.EMPTY_HASH})
        rec=handle(self.storage,self.workspace,{'op':'calibration_evaluate','profile_ref':r,'sample':sample(p)})['evaluation']
        self.assertEqual(rec['binding'],result['binding']);self.assertEqual(rec['adapter'],'smriti')
        self.assertEqual(handle(self.storage,self.workspace,{'op':'state'})['calibration']['binding'],result['binding'])

class BoundRoleTests(unittest.TestCase):
    def exercise(self,case_type,method):
        case=case_type(method);case.setUp()
        try:
            sid=getattr(case,'sid',getattr(case,'session',None));r=c.register(case.s,profile())
            binding=c.bind(case.s,sid,[r],c.EMPTY_HASH)
            getattr(case,method)()
            self.assertEqual(c.current(case.s,sid),binding)
            rows=case.s.db.execute('SELECT record_json FROM invocation_receipts').fetchall()
            self.assertTrue(rows)
            for raw, in rows:self.assertEqual(json.loads(raw)['calibration_binding'],binding)
        finally:case.tearDown()

    def test_auto_review_handoff_and_allocation_with_selected_profile(self):
        import test_auto_review as f
        self.exercise(f.AutomaticTests,'test_discovers_over_128_and_preserves_progress_across_models_and_restart')

    def test_static_explanation_model_switch_with_selected_profile(self):
        import test_static_analysis as f
        self.exercise(f.StaticProtocolTests,'test_fresh_call_and_switch_preserve_contract')

    def test_api_mapper_and_reviewer_with_selected_profile(self):
        import test_api_map as f
        self.exercise(f.APIMapTests,'test_valid_map_recursion_unknown_and_model_handoff')

    @unittest.skipIf(__import__('os').name=='nt','Existing action adapter is read-only on Windows')
    def test_coder_edit_and_handoff_with_selected_profile(self):
        import test_host_actions as f
        self.exercise(f.ActionTests,'test_source_receipt_preserves_progress_when_model_changes')

    def test_selection_does_not_refund_automatic_review_allocations(self):
        import test_auto_review as f
        import review_budget as budget
        case=f.AutomaticTests();case.setUp()
        try:
            p=profile();r=c.register(case.s,p);b=c.bind(case.s,case.sid,[r],c.EMPTY_HASH)
            case.step();before=budget.decision(case.s,case.sid)
            p['version']=2;r2=c.register(case.s,p);c.bind(case.s,case.sid,[r2],b['hash'])
            self.assertEqual(budget.decision(case.s,case.sid)['used'],before['used'])
            self.assertEqual(budget.decision(case.s,case.sid)['limits'],before['limits'])
            after=case.step();self.assertEqual(after['auto']['budget']['used']['calls'],before['used']['calls']+1)
        finally:case.tearDown()

    def test_worker_checkpoint_invalidated_after_reselection(self):
        import test_gateway as f
        from dag_runtime import run_paged
        case=f.GatewayTests();case.setUp()
        try:
            data=case.fixture();s=InvocationLedger(case.db)
            try:r=c.register(s,profile('smriti'));b=c.bind(s,'active',[r],c.EMPTY_HASH)
            finally:s.close()
            result=run_paged(case.db,'active',data['request'],data['required_facts'],f.ScriptModel(lambda n,p:f.proposal()),
                'worker',initial_resident=data['seed_pages'],policy_request=data['policy_request'])
            self.assertIsNotNone(result['released_answer'])
            s=InvocationLedger(case.db)
            try:
                cid=s.db.execute('SELECT id FROM checkpoints WHERE session=?',('active',)).fetchone()[0]
                self.assertEqual(s.load_checkpoint('active',cid)['calibration_binding'],b)
                c.bind(s,'active',[],b['hash'])
                with self.assertRaises(PageFault):s.load_checkpoint('active',cid)
            finally:s.close()
        finally:case.tearDown()

class AdapterTests(unittest.TestCase):
    def test_smriti_exact_capacity_and_ttl_boundaries(self):
        a=SmritiAdapter()
        self.assertEqual(a.context(700,300,1000)['decision'],'ALLOW')
        self.assertEqual(a.context(701,300,1000)['decision'],'BRIDGE')
        self.assertFalse(a.freshness(9.99,10)['expired']);self.assertTrue(a.freshness(10,10)['expired'])
        self.assertEqual(a.freshness(5,10)['decision'],'ALLOW');self.assertEqual(a.freshness(6,10)['decision'],'BRIDGE')

    def test_karma_allocations_retain_previous_normalization(self):
        a=KarmaAdapter()
        for used in (0,2,9,10,12):
            for cost in (0,1,2,11):
                v=a.allocations({'calls':used},{'calls':10},{'calls':cost})
                self.assertEqual(v['decision'],KernelBoundary().evaluate(.15*cost/max(10-used,1e-9),used/10).decision.value)
                s=a.storage(used,10,cost)
                self.assertEqual(s['decision'],KernelBoundary().evaluate(.15*cost/max(10-used,1),used/10).decision.value)

    def test_only_shared_boundary_constructs_canonical_kernel(self):
        root=Path(__file__).parent
        for name in ('smriti_adapter.py','karma_adapter.py','calibration.py'):
            self.assertNotIn('stp_canonical.kernel',(root/name).read_text())
        from governance import POLICY_FILES
        self.assertTrue({'smriti_adapter.py','karma_adapter.py','kernel_boundary.py','calibration.py'}<=set(POLICY_FILES))

if __name__=='__main__':unittest.main()
