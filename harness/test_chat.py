import copy,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from context_manager import InvalidUpdate,canonical
from paging import Budget,PageFault
from invocation_gateway import InvocationLedger,consume_dispatch
from vscode_bridge import handle
from karma_adapter import KarmaAdapter
import chat_runtime as chat
import auto_review as auto
import calibration
import review_budget
from test_calibration import profile

MODELS={'low':'fixture-small','mid':'fixture-mid','high':'fixture-large'}
class Fake:
    budget=Budget(16384,2048,256)
    def __init__(self,model='fixture-small',fault=None):self.model=model;self.fault=fault;self.payloads=[]
    def call(self,cid,system,p,schema):
        consume_dispatch(cid,self.model,system,p,schema);self.payloads.append(copy.deepcopy(p))
        if 'claims' in schema['properties']:
            judged={key:{'verdict':'supported','reason':'Supported by the delivered fixture source.',
                         'evidence':[{'path':claim['paths'][0],'quote':p['excerpts'][claim['paths'][0]].splitlines()[0]}]}
                    for key,claim in p['claims'].items()} if p.get('review_protocol') != 2 else {}
            if p.get('review_protocol') == 2:
                judged = {}
                for key, claim in p['claims'].items():
                    ids = [eid for eid, entry in p['source_evidence'].items() if entry['path'] in claim['paths']]
                    judged[key] = {'verdict': 'supported' if ids else 'insufficient_evidence',
                                   'reason': 'Fixture source judgment.', 'evidence_ids': ids[:1]}
            result={'task_id':p['contract']['task_id'],'constraints':copy.deepcopy(p['constraint_echo']),'claims':judged,'complete':False}
            if getattr(self,'review_fault',None):self.review_fault(result,p)
            return result,{'call_id':cid,'ok':True,'answer':result}
        name=next(iter(p['excerpts']))
        result={'task_id':p['contract']['task_id'],'constraints':copy.deepcopy(p['constraint_echo']),
            'answer':'The entry function calls the leaf implementation. Static calls do not prove runtime execution.',
            'summary':'Inspected the entry and leaf source.','citations':[{'path':name,'quote':p['excerpts'][name].splitlines()[0]}],
            'remaining':['Review runtime behavior.'],'complete':False}
        if 'report' in schema['properties']:
            result['report']={'overview':result['answer'],
                'components':[{'name':'Entry','responsibility':'Starts the request.','paths':[name]}],
                'flow':[{'description':'Calls the leaf implementation to finish the request.','paths':[name]}],
                'strengths':[], 'risks':[], 'next_steps':['Exercise error recovery with an injected failure.']}
        if self.fault:self.fault(result,p)
        return result,{'call_id':cid,'ok':True,'answer':result}

class ChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.ws=self.root/'repo';self.ws.mkdir();self.storage=self.root/'store'
        (self.ws/'entry.py').write_text('from leaf import run\ndef start(): return run()\n')
        (self.ws/'leaf.py').write_text('def run(): return 1\n')
        initial=handle(self.storage,self.ws,{'op':'auto_new','goal':'Review source while preserving every constraint.','roots':['.']})
        self.contract=initial['contract'];self.meta=json.loads((self.storage/'workspace.json').read_text());self.sid=self.meta['active'];self.s=InvocationLedger(self.storage/'state.sqlite')
        auto.step(self.s,self.meta,self.ws,self.storage,{'explain':False},provider=Fake())
    def tearDown(self):self.s.close();self.tmp.cleanup()
    def ask(self,prompt='Explain start.',tier='auto',provider=None):
        return chat.answer(self.s,self.meta,self.ws,self.storage,{'prompt':prompt,'models':MODELS,'tier':tier},provider=provider or Fake())

    def test_no_call_sites_allows_source_explanation_with_no_invented_relationships(self):
        (self.ws/'entry.py').write_text('def add(a, b): return a + b\n')
        auto.step(self.s,self.meta,self.ws,self.storage,{'explain':False},provider=Fake())
        m=Fake();out=self.ask('Explain add.',provider=m)
        self.assertTrue(out['result']['admission']['admitted'])
        packet=m.payloads[0];self.assertEqual(packet['sites'],[]);self.assertEqual(packet['facts']['calls'],[])
        self.assertTrue(any(n['name']=='add' for n in packet['facts']['nodes']))
        self.assertEqual(packet['contract']['constraints'],self.contract['contract']['constraints'])
        bad=copy.deepcopy(packet);bad['facts']['calls']=[{'source':'add','target':'add'}]
        with self.assertRaises(InvalidUpdate):chat.request_check(self.s,self.sid,bad)
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_empty_site_selection_cannot_hide_existing_calls(self):
        m=Fake();self.ask(provider=m);packet=copy.deepcopy(m.payloads[0]);packet['sites']=[];packet['facts']={'nodes':[],'calls':[]}
        with self.assertRaises(InvalidUpdate):chat.request_check(self.s,self.sid,packet)

    def test_auto_routing_and_explicit_tiers(self):
        signals={'source_files':500,'selected_files':2,'unresolved_calls':1}
        for prompt,tier in [('Explain start.','low'),('Compare dependencies across files.','mid'),('Review the architecture.','high')]:
            choice=KarmaAdapter().route(prompt,MODELS,'auto',signals);self.assertEqual(choice['tier'],tier)
            self.assertFalse(choice['semantic_complexity_verified'])
        for tier in MODELS:self.assertEqual(KarmaAdapter().route('Audit architecture.',MODELS,tier,signals)['model'],MODELS[tier])

    def test_whole_workspace_inventory_includes_separate_source_pages(self):
        # Force two parser work units without changing the task's byte allocation.
        original=__import__('static_runtime').build
        def split(s,meta,workspace,root,files,real=True):
            if len(files)>1:raise InvalidUpdate('Static graph work-unit allocation exceeded')
            return original(s,meta,workspace,root,files,real=real)
        with patch('static_runtime.build',side_effect=split):
            model=Fake();out=self.ask(provider=model)
        p=model.payloads[0]
        self.assertEqual(len(p['snapshot_receipts']),2)
        self.assertEqual(p['workspace_map']['files'],['entry.py','leaf.py'])
        self.assertEqual(out['coverage']['evidence']['fresh_snapshot_files'],2)
        self.assertFalse(out['coverage']['evidence']['exhaustive_semantic_review'])
        self.assertEqual(p['review_capabilities']['source_field'],'excerpts')
        self.assertFalse(p['contract']['implementation_present'])
        self.assertTrue(p['excerpts'])
        for key in ['workspace_map','evidence_coverage','review_capabilities']:
            bad=copy.deepcopy(p);bad[key]={}
            with self.assertRaises(InvalidUpdate):chat.request_check(self.s,self.sid,bad)
        bad=copy.deepcopy(p);bad['snapshot_receipts']=p['snapshot_receipts'][:1]
        with self.assertRaises(InvalidUpdate):chat.request_check(self.s,self.sid,bad)

    def test_architecture_selection_uses_entrypoint_and_original_goal(self):
        texts={'aaa.py':'unrelated = 1\n','extension.js':'function activate(){ return 1; }\n'}
        self.assertEqual(next(iter(chat.excerpts(texts,'Architecture overview. Retry the original request.',30))),'extension.js')

    def test_review_context_expands_with_model_capability_without_resetting_budget(self):
        import review_context
        from types import SimpleNamespace
        from unittest.mock import patch
        provider=SimpleNamespace(context_metadata={'model_info':{'general.architecture':'qwen','qwen.context_length':131072},'parameters':'num_ctx 65536'})
        before=review_budget.usage(self.s,self.sid)
        with patch('review_budget.usage',return_value={'limits':{'tokens':90000},'used':{'tokens':30000}}):
            result=review_context.context_budget(provider,self.s,self.sid)
        self.assertEqual(provider.budget.context_limit,30000)
        self.assertEqual(provider.budget.response_reserve,8192)
        self.assertLessEqual(provider.budget.response_reserve,8192)
        self.assertEqual(result['repair_slots_reserved'],1)
        self.assertEqual(review_budget.usage(self.s,self.sid),before)

    def test_structural_inventory_discloses_omissions_and_keeps_imports_unresolved(self):
        import review_context
        data={'sources':{'entry.py':'import external\ndef entry(): return 1\n','leaf.py':'def leaf(): return 2\n'},
              'graph':{'nodes':[],'calls':[],'diagnostics':[]}}
        full=review_context.overview(data,4096)
        self.assertEqual(full['modules'][0]['imports'],['external'])
        self.assertNotIn('edges',full)
        small=review_context.overview(data,512)
        self.assertGreater(small['omitted_module_details'],0)

    def test_optional_repairs_yield_to_preserved_conversation_and_reviewer(self):
        import conversation
        import review_context
        from test_conversation import Fake as Conversational
        prompt='Explain the architecture. Remember exact review requirements: '+('preserve public API and keep the review read only; '*20)
        router=Conversational('fixture-small',lambda r,p:r.update(intent='workspace',
            workspace_request={'operation':'read','paths':[]},facts=[]))
        route=conversation.answer(self.s,self.meta,self.storage,
            {'prompt':prompt,'models':MODELS,'tier':'low'},provider=router)
        self.assertTrue(route['result']['admission']['admitted'],route['result']['admission'])
        iid=route['result']['invocation_id']
        model=Fake();model.budget=Budget(65536,4096,256)
        result=chat.answer(self.s,self.meta,self.ws,self.storage,
            {'prompt':prompt,'models':MODELS,'tier':'low','conversation_id':iid},provider=model)
        self.assertTrue(result['result']['admission']['admitted'])
        p=copy.deepcopy(model.payloads[0]);before=review_budget.usage(self.s,self.sid)
        data=chat.runtime.load_partition(self.s,self.sid,self.ws,p['snapshot_receipts'],p['snapshot_owner'])
        model.context_metadata={'model_info':{'qwen.context_length':65536}}
        # Like a long-lived task, only 41,403 conservative units remain.
        with patch('review_budget.usage',return_value={'limits':{'tokens':250000},'used':{'tokens':208597}}):
            fmt,attempts=chat.fit_packet(self.s,self.sid,p,data,model,True)
        self.assertLess(attempts,3)
        self.assertGreater(model.budget.context_limit,10350)
        self.assertLessEqual(model.budget.context_limit* (attempts+1),41403)
        self.assertIn(prompt,canonical(p['memory']))
        self.assertEqual(p['memory']['recent_turns'][-1]['user'],prompt)
        self.assertEqual(p['contract'],self.s.pinned(self.sid))
        self.assertLessEqual(model.budget.check(chat.SYSTEM,p,fmt)+model.budget.response_reserve,model.budget.context_limit)
        self.assertEqual(review_budget.usage(self.s,self.sid),before)
        with patch('review_budget.usage',return_value={'limits':{'tokens':250000},'used':{'tokens':244000}}):
            with self.assertRaises(review_context.ReviewContextExceeded) as stopped:
                chat.fit_packet(self.s,self.sid,p,data,model,True)
        self.assertEqual(stopped.exception.details['available_tokens'],6000)
        need = stopped.exception.details['task_allocation_need']
        self.assertEqual(need['dimension'], 'tokens')
        self.assertLessEqual(need['required_context'], need['model_context'])
        self.assertEqual(need['minimum'], need['slots'] * need['required_context'] - 6000)
        self.assertIn('65,536',str(stopped.exception))
        self.assertEqual(review_budget.usage(self.s,self.sid),before)

    def test_two_fresh_models_preserve_task_binding_handoff_and_spent_budget(self):
        ref=calibration.register(self.s,profile());binding=calibration.bind(self.s,self.sid,[ref],calibration.EMPTY_HASH)
        small=Fake();one=self.ask(provider=small);before=review_budget.usage(self.s,self.sid)
        large=Fake('fixture-large');two=self.ask('Review architecture.',provider=large)
        self.assertIn('\n\n## Main components\n\n',two['report_markdown'])
        self.assertIn('report',two['result']['accepted_output'])
        self.assertTrue(one['result']['admission']['admitted']);self.assertTrue(two['result']['admission']['admitted'])
        self.assertEqual(small.payloads[0]['contract'],large.payloads[0]['contract'])
        self.assertEqual(large.payloads[0]['contract']['calibration_binding'],binding)
        self.assertEqual(large.payloads[0]['handoff']['invocation_id'],one['result']['invocation_id'])
        self.assertFalse({'messages','history','conversation'}&set(large.payloads[0]))
        self.assertNotEqual(small.payloads[0]['snapshot_receipt'],large.payloads[0]['snapshot_receipt'])
        self.assertEqual(self.s.current_contract(self.sid),self.contract)
        self.assertEqual(review_budget.usage(self.s,self.sid)['used']['calls'],before['used']['calls']+2)
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_forged_citation_is_quarantined_with_bounded_repairs(self):
        m=Fake(fault=lambda r,p:r['citations'][0].update(quote='Invented source'))
        out=self.ask(provider=m);self.assertFalse(out['result']['admission']['admitted']);self.assertEqual(len(m.payloads),3)
        with self.assertRaises(PageFault):self.s.load_result(self.sid,out['result']['invocation_id'])

    def test_citation_choices_are_exact_bounded_source_and_wrong_file_still_rejected(self):
        m=Fake();self.ask(provider=m);p=m.payloads[0]
        choices=[c['quote'] for c in chat.schema(p)['properties']['citations']['items']['enum']]
        self.assertTrue(choices)
        for citation in chat.schema(p)['properties']['citations']['items']['enum']:
            self.assertIn(citation['quote'],p['excerpts'][citation['path']])
        for quote in choices:
            self.assertTrue(any(quote in text for text in p['excerpts'].values()))
            self.assertLessEqual(len(quote),160)
        def wrong_file(r,p):
            r['citations']=[{'path':'leaf.py','quote':'from leaf import run'}]
        out=self.ask(provider=Fake(fault=wrong_file));self.assertFalse(out['result']['admission']['admitted'])

    def test_citation_choices_keep_indentation_unicode_and_long_lines_exact(self):
        p={'excerpts':{'a.py':'\n    def café():\r\n        return "✓"\r\n'+'x'*240,'b.py':'   '}}
        for quote in chat.citation_quotes(p):
            self.assertTrue(any(quote in text for text in p['excerpts'].values()))
            self.assertLessEqual(len(quote),160)
        self.assertIn('    def café():',chat.citation_quotes(p))

    def test_omitted_or_changed_constraints_and_false_completion_rejected(self):
        for fault in (lambda r,p:r['constraints'].pop(),lambda r,p:r.update(complete=True),lambda r,p:r.update(task_id='other')):
            with self.subTest(fault=fault):self.assertFalse(self.ask(provider=Fake(fault=fault))['result']['admission']['admitted'])

    def test_source_changed_during_response_cannot_be_released(self):
        def mutate(r,p):(self.ws/'leaf.py').write_text('def run(): return 2\n')
        out=self.ask(provider=Fake(fault=mutate));self.assertFalse(out['result']['admission']['admitted'])

    def test_profile_change_withholds_chat_handoff(self):
        r=calibration.register(self.s,profile());b=calibration.bind(self.s,self.sid,[r],calibration.EMPTY_HASH)
        first=self.ask();calibration.bind(self.s,self.sid,[],b['hash']);m=Fake();self.ask(provider=m)
        self.assertIsNone(m.payloads[0]['handoff'])
        with self.assertRaises(PageFault):self.s.load_result(self.sid,first['result']['invocation_id'])

    def test_wrong_provider_and_remote_tiers_fail_before_inference(self):
        wrong=Fake('unconfigured')
        with self.assertRaises(InvalidUpdate):self.ask(provider=wrong)
        self.assertFalse(wrong.payloads)
        with self.assertRaises(InvalidUpdate):KarmaAdapter().route('Explain.',dict(MODELS,high='model:cloud'),'auto',{'source_files':1,'selected_files':1,'unresolved_calls':0})

    def test_routing_receipt_and_source_packet_tampering_rejected(self):
        m=Fake();self.ask(provider=m);p=m.payloads[0];bad=copy.deepcopy(p);bad['route']['record']['choice']['model']='attacker'
        with self.assertRaises(InvalidUpdate):chat.request_check(self.s,self.sid,bad)
        bad=copy.deepcopy(p);bad['excerpts']['entry.py']='forged'
        with self.assertRaises(InvalidUpdate):chat.request_check(self.s,self.sid,bad)

    def test_stop_budget_cannot_escalate_to_another_tier(self):
        self.s.close();self.tmp.cleanup();self.setUp()
        # A separate explicitly allocated task, rather than mutating its ledger.
        handle(self.storage,self.ws,{'op':'auto_new','goal':'One call only.','roots':['.'],'review_calls':1})
        self.meta=json.loads((self.storage/'workspace.json').read_text());self.sid=self.meta['active']
        auto.step(self.s,self.meta,self.ws,self.storage,{'explain':False},provider=Fake())
        self.ask();other=Fake('fixture-large')
        with self.assertRaises(InvalidUpdate):self.ask('Audit architecture.',provider=other)
        self.assertEqual(other.payloads,[]);self.assertEqual(review_budget.usage(self.s,self.sid)['used']['calls'],1)

    def test_restored_state_shows_only_current_chat_answer(self):
        self.ask();(self.storage/'workspace.json').write_text(json.dumps(self.meta))
        restored=handle(self.storage,self.ws,{'op':'state'});self.assertIsNotNone(restored['chat']['answer'])
        (self.ws/'leaf.py').write_text('def run(): return 2\n')
        restored=handle(self.storage,self.ws,{'op':'state'});self.assertIsNone(restored['chat']['answer'])

    def test_thermal_zero_is_nominal_and_celsius_is_not_fabricated(self):
        h=KarmaAdapter().hardware({'wall_time':100,'thermal_state':0,'memory_pressure_raw':1,'free_bytes':8*1024**3,'inactive_bytes':0},101)
        self.assertEqual(h['thermal_label'],'nominal');self.assertIsNone(h['temperature_celsius'])
        self.assertEqual(h['thermal_source'],'NSProcessInfo.thermalState');self.assertIn('not degrees',h['temperature_reason'])

if __name__=='__main__':unittest.main()
