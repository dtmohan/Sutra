"""Publication, freshness and accounting across independently governed review calls."""
import copy
import json
import unittest
from unittest.mock import patch
import test_chat
from test_chat import Fake, MODELS
from test_model_identity import identity
from context_manager import InvalidUpdate
from paging import PageFault
import chat_runtime as chat
import review_budget
import report_review
from vscode_bridge import handle


class ReportReviewTests(unittest.TestCase):
    setUp=test_chat.ChatTests.setUp
    tearDown=test_chat.ChatTests.tearDown
    ask=test_chat.ChatTests.ask

    def review(self, writer=None, reviewer=None, **request):
        return chat.answer(self.s,self.meta,self.ws,self.storage,
            dict(prompt=request.pop('prompt','Explain the architecture.'),models=MODELS,tier='low',**request),
            provider=writer or Fake(),review_provider=reviewer or Fake('reviewer'))

    def test_distinct_review_has_its_own_receipt_and_charged_call(self):
        writer=Fake();writer.model_identity=identity(writer.model,'w','wm','qwen')
        reviewer=Fake('reviewer');reviewer.model_identity=identity(reviewer.model,'r','rm','qwen')
        before=review_budget.usage(self.s,self.sid)
        out=self.review(writer,reviewer,require_distinct_reviewer=True)
        self.assertTrue(out['result']['admission']['admitted'],out)
        self.assertEqual(out['report_review']['independence']['weight_relation'],'different')
        rid=out['report_review']['reviewer_id']
        env=json.loads(self.s.db.execute('SELECT record_json FROM invocations WHERE id=?',(rid,)).fetchone()[0])
        self.assertEqual(env['role'],'report_reviewer');self.assertEqual(env['model_identity'],reviewer.model_identity)
        self.assertEqual(writer.payloads[0]['contract'],reviewer.payloads[0]['contract'])
        self.assertEqual(review_budget.usage(self.s,self.sid)['used']['calls'],before['used']['calls']+2)
        self.assertFalse(out['report_review']['semantic_correctness_verified'])
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_unsupported_flow_is_absent_after_publish_reload_and_model_switch(self):
        claim='Invented recursive calls guarantee infinite parallel speed.'
        writer=Fake(fault=lambda r,p:r['report']['flow'][0].update(description=claim))
        reviewer=Fake('reviewer')
        def reject(r,p):r['claims']['flow/0'].update(verdict='insufficient_evidence',reason='No source supports this relationship.',evidence_ids=[])
        reviewer.review_fault=reject
        out=self.review(writer,reviewer)
        self.assertTrue(out['result']['admission']['admitted'],out)
        self.assertEqual(out['report_review']['status'],'reviewed_with_omissions')
        self.assertNotIn(claim,out['report_markdown']);self.assertEqual(out['result']['accepted_output']['report']['flow'],[])
        (self.storage/'workspace.json').write_text(json.dumps(self.meta))
        restored=handle(self.storage,self.ws,{'op':'state'})
        self.assertNotIn(claim,json.dumps(restored['chat']['answer']))
        other=Fake('fixture-large');next_out=self.ask('Continue with a source explanation.',tier='high',provider=other)
        self.assertTrue(next_out['result']['admission']['admitted'])
        self.assertEqual(other.payloads[0]['handoff']['reviewer_id'],out['report_review']['reviewer_id'])
        self.assertNotIn(claim,json.dumps(other.payloads[0]['handoff']))
        self.assertEqual(other.payloads[0]['contract'],writer.payloads[0]['contract'])

    def test_legitimate_recursive_claim_can_be_retained_with_source_quote(self):
        source='def recur(n): return recur(n-1) if n else 0\n'
        (self.ws/'leaf.py').write_text(source)
        import auto_review
        auto_review.step(self.s,self.meta,self.ws,self.storage,{'explain':False},provider=Fake())
        writer=Fake(fault=lambda r,p:r['report']['flow'][0].update(description='recur calls itself while n is nonzero.',paths=['leaf.py']))
        out=self.review(writer)
        self.assertTrue(out['result']['admission']['admitted'],out)
        self.assertIn('recur calls itself',out['report_markdown'])

    def test_missing_claims_constraints_and_invented_quotes_are_withheld(self):
        faults=[lambda r,p:r['claims'].pop('overview'),lambda r,p:r['constraints'].pop(),
                lambda r,p:r['claims']['overview'].update(evidence_ids=['invented-source'])]
        for index,fault in enumerate(faults):
            with self.subTest(fault=fault):
                reviewer=Fake('reviewer');reviewer.review_fault=fault
                old=copy.deepcopy(self.meta.get('chat'))
                out=self.review(reviewer=reviewer,prompt='Explain the architecture. Case '+str(index))
                self.assertFalse(out['result']['admission']['admitted'])
                self.assertIsNone(out['report_markdown']);self.assertEqual(len(reviewer.payloads),2)
                self.assertEqual(old,self.meta.get('chat'))

    def test_changed_source_during_review_cannot_publish(self):
        reviewer=Fake('reviewer')
        reviewer.review_fault=lambda r,p:(self.ws/'leaf.py').write_text('def run(): return 42\n')
        out=self.review(reviewer=reviewer)
        self.assertFalse(out['result']['admission']['admitted']);self.assertIsNone(out['report_markdown'])

    def test_initial_review_uses_remaining_budget_instead_of_reserving_an_unaffordable_retry(self):
        from model_identity import compare,unknown
        from paging import Budget
        with patch('report_review.run',side_effect=InvalidUpdate('defer checker')):
            out=self.review()
        candidate_id=out['result']['invocation_id']
        first=report_review.run(self.s,self.sid,self.storage,candidate_id,provider=Fake('reviewer'))
        previous=json.loads(self.s.db.execute('SELECT payload_json FROM deliveries WHERE id=(SELECT delivery_id FROM invocations WHERE id=?)',
                                             (first['invocation_id'],)).fetchone()[0])
        with patch('report_review.run',side_effect=InvalidUpdate('defer checker')):
            candidate_id=self.review()['result']['invocation_id']
        needed=Budget().charge(report_review.SYSTEM,dict(previous,repair={'errors':['x'*160]*2}),report_review.schema(previous))+4096+300
        current=review_budget.usage(self.s,self.sid);limited=copy.deepcopy(current)
        limited['limits']['tokens']=limited['used']['tokens']+needed
        reviewer=Fake('reviewer');reviewer.context_metadata={'model_info':{'qwen.context_length':65536}}
        separation=compare(unknown('fixture-small'),unknown('reviewer'))
        separation.update(selection='test',requested_model='auto')
        with patch('report_review.choose',return_value=('reviewer',separation)), \
             patch('api_runtime.provider_for',return_value=reviewer), \
             patch('review_budget.usage',return_value=limited):
            result=report_review.run(self.s,self.sid,self.storage,candidate_id)
        self.assertTrue(result['admission']['admitted'],result['admission'])
        self.assertEqual(len(reviewer.payloads),1)
        config=json.loads(self.s.db.execute('SELECT config_json FROM protocol_runs WHERE id=(SELECT run_id FROM invocations WHERE id=?)',
                                           (result['invocation_id'],)).fetchone()[0])
        self.assertEqual(config['max_calls'],1);self.assertEqual(config['max_repairs'],0)
        self.assertEqual(reviewer.payloads[0]['constraint_echo'],previous['constraint_echo'])
        self.assertEqual(sorted(reviewer.payloads[0]['source_evidence'].values(),key=lambda x:x['path']),sorted(previous['source_evidence'].values(),key=lambda x:x['path']))

    def test_strict_unknown_identity_spends_no_reviewer_call(self):
        reviewer=Fake('reviewer');before=review_budget.usage(self.s,self.sid)['used']['calls']
        out=self.review(reviewer=reviewer,require_distinct_reviewer=True)
        self.assertFalse(out['result']['admission']['admitted']);self.assertEqual(reviewer.payloads,[])
        self.assertEqual(review_budget.usage(self.s,self.sid)['used']['calls'],before+1)

    def test_supplied_local_reviewer_binds_observed_identity(self):
        from local_model import LocalModel
        class LocalFixture(LocalModel):
            model_interface = None
            call=Fake.call
        reviewer=LocalFixture(self.storage/'calls',model='live-fixture')
        reviewer.payloads=[];reviewer.fault=None;reviewer.budget=Fake.budget
        observed=identity(reviewer.model,'weights','manifest')
        with patch('model_identity.observe',return_value=observed),patch('monitoring.require_resources',return_value={'profile':'injected-test-provider'}), patch('vscode_bridge.ollama',return_value={}):
            out=self.review(reviewer=reviewer)
        self.assertTrue(out['result']['admission']['admitted'],out)
        self.assertEqual(out['report_review']['independence']['reviewer_identity'],observed)
        self.assertEqual(reviewer.dispatch_identity,observed)

    def test_supplied_local_reviewer_does_not_replace_a_pinned_identity(self):
        from local_model import LocalModel
        reviewer=LocalModel(self.storage/'calls',model='live-fixture');reviewer.expected_identity=identity(reviewer.model,'old')
        with patch('model_identity.observe',return_value=identity(reviewer.model,'new')):
            out=self.review(reviewer=reviewer)
        self.assertFalse(out['result']['admission']['admitted'])
        self.assertEqual(reviewer.expected_identity['weights_sha256'],'old')
        self.assertFalse(list((self.storage/'calls').glob('*.json')))

    def test_reviewer_dispatch_checks_identity_again(self):
        reviewer=Fake('reviewer');reviewer.model_identity=identity('reviewer','before')
        original=report_review.schema
        def retag(p):
            reviewer.model_identity=identity('reviewer','after');return original(p)
        with patch.object(report_review,'schema',side_effect=retag):out=self.review(reviewer=reviewer)
        self.assertFalse(out['result']['admission']['admitted']);self.assertEqual(reviewer.payloads,[])

    def test_exhausted_budget_withholds_report_instead_of_skipping_review(self):
        handle(self.storage,self.ws,{'op':'auto_new','goal':'Architecture review.','roots':['.'],'review_calls':1})
        self.meta=json.loads((self.storage/'workspace.json').read_text());self.sid=self.meta['active']
        import auto_review
        auto_review.step(self.s,self.meta,self.ws,self.storage,{'explain':False},provider=Fake())
        reviewer=Fake('reviewer');out=self.review(reviewer=reviewer)
        self.assertFalse(out['result']['admission']['admitted']);self.assertIsNone(out['report_markdown'])
        self.assertEqual(reviewer.payloads,[])
        self.assertEqual(review_budget.usage(self.s,self.sid)['used']['calls'],1)
