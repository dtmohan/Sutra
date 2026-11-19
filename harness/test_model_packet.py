import copy
import json
import unittest
from unittest.mock import patch

from context_manager import canonical, InvalidUpdate
from paging import Budget
from model_packet import build, descriptor, host_fields, bind_output
from budget_accounting import settlement, used, reserved
from invocation_gateway import consume_dispatch, _permit, request_hash
from review_budget import usage
import chat_runtime as chat
import test_chat as fixture


class WireModel(fixture.Fake):
    """A deterministic transport stub obeying the real model-facing interface."""
    model_interface = 1
    counts = (400, 80)
    completed = True
    def call(self, cid, system, p, schema):
        packet = build(system, p, schema)
        self.packet = packet
        with patch('test_chat.consume_dispatch', side_effect=lambda *args: consume_dispatch(*args, packet=packet)):
            result, record = super().call(cid, system, p, schema)
        for k in host_fields(packet['role'], p, schema, packet['version'], result):
            result.pop(k, None)
        record['response'] = {'done': self.completed, 'prompt_eval_count': self.counts[0], 'eval_count': self.counts[1]}
        if getattr(self, 'after', None): self.after(result, p)
        return result, record


class PacketTests(unittest.TestCase):
    setUp = fixture.ChatTests.setUp
    tearDown = fixture.ChatTests.tearDown
    ask = fixture.ChatTests.ask

    def ask_strata(self, provider):
        return chat.answer(self.s, self.meta, self.ws, self.storage,
                           {'prompt': 'Explain start.', 'models': dict(fixture.MODELS, high=provider.model),
                            'tier': 'high'}, provider=provider)

    def test_strata_citation_reference_binds_the_exact_indented_quote(self):
        from strata_model import MODEL
        from model_packet import DERIVED_REVIEW_SUMMARY_VERSION

        (self.ws / 'entry.py').write_text('from leaf import run\ndef start():\n    return run()\n')
        chat.auto.step(self.s, self.meta, self.ws, self.storage, {'explain': False}, provider=fixture.Fake())
        provider = WireModel(MODEL)
        expected = {'path': 'entry.py', 'quote': '    return run()'}

        def reference(reply, payload):
            choices = json.loads(provider.packet['messages'][1]['content'])['citation_choices']
            ref = next(item['id'] for item in choices if item['path'] == expected['path']
                       and item['line_in_excerpt'] == 2)
            reply['citations'] = [ref]

        provider.after = reference
        result = self.ask_strata(provider)['result']
        self.assertTrue(result['admission']['admitted'], result)
        self.assertEqual(result['accepted_output']['citations'], [expected])
        self.assertEqual(provider.packet['version'], DERIVED_REVIEW_SUMMARY_VERSION)
        self.assertEqual(provider.packet['schema']['properties']['citations']['items']['type'], 'integer')
        self.assertNotIn('summary', provider.packet['schema']['properties'])
        self.assertEqual(result['accepted_output']['summary'], result['accepted_output']['answer'][:320])
        self.assertIn('return integer IDs from citation_choices', provider.packet['messages'][0]['content'])
        self.assertIn('array must contain 1 to 4 IDs', provider.packet['messages'][0]['content'])
        self.assertTrue(self.s.verify_invocations(self.sid))
        self.assertEqual(self.s.load_result(self.sid, result['invocation_id'])['citations'], [expected])

    def test_strata_citation_reference_withholds_unknown_or_model_written_quotes(self):
        from strata_model import MODEL
        provider = WireModel(MODEL)
        for supplied in ([0], [999], [True], [{'path': 'entry.py', 'quote': 'return run()'}]):
            provider.after = lambda reply, payload: reply.update(citations=supplied)
            result = self.ask_strata(provider)['result']
            self.assertFalse(result['admission']['admitted'], supplied)
            self.assertTrue(any('response.citations[]' in error for error in result['admission']['errors']))
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_named_native_profile_uses_the_same_citation_reference_gate(self):
        provider = WireModel('strata:profile/low-native')
        def reference(reply, payload):
            choices = json.loads(provider.packet['messages'][1]['content'])['citation_choices']
            reply['citations'] = [choices[0]['id']]
        provider.after = reference
        result = self.ask_strata(provider)['result']
        self.assertTrue(result['admission']['admitted'], result)
        self.assertEqual(provider.packet['schema']['properties']['citations']['items']['type'], 'integer')
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_sutra_owned_engine_uses_checked_citation_ids(self):
        provider = WireModel('sutra:engine/owned')
        def reference(reply, payload):
            choices = json.loads(provider.packet['messages'][1]['content'])['citation_choices']
            reply['citations'] = [choices[0]['id']]
        provider.after = reference
        result = self.ask_strata(provider)['result']
        self.assertTrue(result['admission']['admitted'], result)
        self.assertEqual(provider.packet['schema']['properties']['citations']['items']['type'], 'integer')
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_v1_strata_receipt_still_verifies_with_verbatim_citation_objects(self):
        from strata_model import MODEL
        provider = WireModel(MODEL)
        with patch('model_packet.citation_references', return_value=False):
            result = self.ask_strata(provider)['result']
        self.assertTrue(result['admission']['admitted'], result)
        self.assertEqual(provider.packet['version'], 1)
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_strata_child_checkpoint_and_report_share_current_citation_references(self):
        import subagent_runtime as child
        from strata_model import MODEL
        payload = {'contract': self.s.pinned(self.sid), 'request': 'Explain this architecture.',
                   'branch': {'id': 'branch-1', 'objective': 'Explain the entry point.'},
                   'route': {'model': MODEL}, 'child_slice': {'ordinal': 2}, 'child_checkpoint': [],
                   'constraint_echo': [], 'excerpts': {'entry.py': 'def start():\n    return run()'}}
        schema = child.schema(payload)
        wire = build(child.SYSTEM, payload, schema)
        data = json.loads(wire['messages'][1]['content'])
        self.assertEqual(data['citation_choices'][1], {'id': 2, 'path': 'entry.py', 'line_in_excerpt': 2})
        self.assertEqual(wire['schema']['properties']['citations']['items']['enum'], [1, 2])
        self.assertIn('Include report whenever slice_status is complete', wire['messages'][0]['content'])
        self.assertIn('check within this branch', wire['messages'][0]['content'])
        self.assertNotIn('report', wire['schema']['required'])

    def test_only_task_semantics_cross_boundary_with_source_verbatim(self):
        m = WireModel(); result = self.ask(provider=m)
        self.assertTrue(result['result']['admission']['admitted'], result)
        full = m.payloads[0]; packet = m.packet
        body = json.loads(packet['messages'][1]['content'])
        self.assertEqual(body['request'], full['request'])
        self.assertEqual(body['excerpts'], full['excerpts'])
        self.assertEqual(body['task']['goal'], full['contract']['goal'])
        for k in ('network_allowed','new_dependencies_allowed','allowed_paths','edits_allowed'):
            if k in full['contract']['constraints']:
                self.assertEqual(body['task']['requirements'][k], full['contract']['constraints'][k])
        for k in ('contract', 'route', 'constraint_echo', 'memory_budget', 'reasoning', '_request_nonce', 'snapshot_receipts'):
            self.assertNotIn(k, body)
        for k in ('task_id','constraints','complete'):
            self.assertNotIn(k, packet['schema']['properties'])
        p = copy.deepcopy(full); p['excerpts']['entry.py'] = 'contract_hash = "user source must remain intact"'
        self.assertEqual(json.loads(build(chat.SYSTEM,p,chat.schema(p))['messages'][1]['content'])['excerpts'],p['excerpts'])
        r = json.loads(self.s.db.execute('SELECT record_json FROM invocation_receipts WHERE invocation_id=?',(result['result']['invocation_id'],)).fetchone()[0])
        self.assertEqual(r['host_fields_authority'], 'host_binding_not_model_acknowledgement')
        self.assertEqual(r['host_fields']['constraints'], full['constraint_echo'])
        self.assertNotEqual(r['model_response_hash'], r['response_hash'])

    def test_host_metadata_does_not_consume_context_but_source_does(self):
        m = WireModel(); self.ask(provider=m); p = m.payloads[0]
        fmt = chat.schema(p); d = descriptor(build(chat.SYSTEM,p,fmt),m.budget.template_reserve)
        self.assertEqual(m.budget.charge(chat.SYSTEM,p,fmt),d['prompt_utf8_bytes']+d['schema_allowance']+d['template_allowance'])
        q=copy.deepcopy(p);q['_request_nonce']='x'*50000;q['reasoning']={'history':'x'*50000}
        self.assertEqual(m.budget.charge(chat.SYSTEM,p,fmt),m.budget.charge(chat.SYSTEM,q,fmt))
        q['excerpts']['entry.py']+='\n'+'x'*1000
        self.assertGreater(m.budget.charge(chat.SYSTEM,q,fmt),m.budget.charge(chat.SYSTEM,p,fmt))

    def test_actual_http_uses_committed_projection_and_original_strict_shape(self):
        import io
        from unittest.mock import MagicMock
        from local_model import LocalModel
        from context_manager import sha
        m=WireModel();result=self.ask(provider=m);p=m.payloads[0];fmt=chat.schema(p)
        raw=dict(result['result']['accepted_output'])
        for k in host_fields('chat_reviewer',p,fmt):raw.pop(k)
        wire=build(chat.SYSTEM,p,fmt)
        chunks={'done':True,'prompt_eval_count':300,'eval_count':70,'message':{'content':json.dumps(raw)}}
        response=io.BytesIO((json.dumps(chunks)+'\n').encode());response.status=200
        connection=MagicMock();connection.getresponse.return_value=response
        model=LocalModel(self.storage/'calls',model=m.model,budget=m.budget,thinking=p['reasoning']['requested'])
        token=_permit.set({'used':False,'hash':request_hash('http',m.model,chat.SYSTEM,p,fmt),'packet_hash':sha(canonical(wire))})
        try:
            with patch('http.client.HTTPConnection',return_value=connection):
                answer,record=model.call('http',chat.SYSTEM,p,fmt)
        finally:_permit.reset(token)
        sent=json.loads(connection.request.call_args.args[2])
        self.assertEqual(sent['messages'],wire['messages']);self.assertEqual(sent['format'],wire['schema'])
        self.assertEqual(answer,raw)
        self.assertEqual(record['conservative_input_charge'],descriptor(wire,m.budget.template_reserve)['input_reservation'])

    def test_forged_host_fields_rejected_instead_of_overwritten(self):
        for key,value in [('task_id','forged'),('constraints',[]),('complete',True)]:
            m=WireModel();m.after=lambda r,p:r.update({key:value})
            out=self.ask(provider=m)
            self.assertFalse(out['result']['admission']['admitted'],key)
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_switch_keeps_contract_and_settles_each_completed_call(self):
        before=usage(self.s,self.sid)['used']
        small=WireModel(); a=self.ask(provider=small)
        large=WireModel('fixture-large'); b=self.ask(tier='high',provider=large)
        self.assertTrue(a['result']['admission']['admitted']);self.assertTrue(b['result']['admission']['admitted'])
        self.assertEqual(small.payloads[0]['contract'],large.payloads[0]['contract'])
        after=usage(self.s,self.sid)['used']
        self.assertEqual(after['calls']-before['calls'],2)
        self.assertEqual(after['tokens']-before['tokens'],960)
        self.assertEqual(large.packet['messages'][0]['content'],small.packet['messages'][0]['content'])
        from invocation_gateway import InvocationLedger
        reopened=InvocationLedger(self.storage/'state.sqlite')
        try:self.assertEqual(usage(reopened,self.sid)['used'],after)
        finally:reopened.close()
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_unmeasured_and_historical_calls_keep_reservation(self):
        for model in (fixture.Fake(),WireModel()):
            if isinstance(model,WireModel):model.completed=False
            before=usage(self.s,self.sid)['used']['tokens'];out=self.ask(provider=model)
            env=json.loads(self.s.db.execute('SELECT record_json FROM invocations WHERE id=?',(out['result']['invocation_id'],)).fetchone()[0])
            self.assertEqual(usage(self.s,self.sid)['used']['tokens']-before,reserved(env))

    def test_changed_source_still_quarantines_completed_measured_work(self):
        m=WireModel();m.after=lambda r,p:(self.ws/'entry.py').write_text('changed = True\n')
        before=usage(self.s,self.sid)['used']['tokens'];out=self.ask(provider=m)
        self.assertFalse(out['result']['admission']['admitted'])
        self.assertGreater(usage(self.s,self.sid)['used']['tokens'],before)
        self.assertEqual(self.s.snapshot(self.sid)['constraints'],m.payloads[0]['contract']['constraints'])


class AccountingTests(unittest.TestCase):
    def test_settlement_never_forgives_used_tokens_or_counts_text_as_measurement(self):
        env={'charge':1000,'budget_profile':{'response_reserve':500},'model_interface':{'version':1}}
        for response,expected,basis in [({},1500,'reserved_unmeasured'),({'done':True,'prompt_eval_count':900,'eval_count':200},1100,'runtime_measured'),({'done':True,'done_reason':'length','prompt_eval_count':900,'eval_count':900},1800,'runtime_measured'),({'done':False,'prompt_eval_count':900,'eval_count':200},1500,'reserved_unmeasured'),({'done':True,'prompt_eval_count':True,'eval_count':200},1500,'reserved_unmeasured')]:
            record={'response':response};s=settlement(env,record)
            from token_usage import measured
            receipt={'accounting_settlement':s,'token_usage':measured(record)}
            self.assertEqual(used(env,receipt),expected);self.assertEqual(s['basis'],basis)
            bad=copy.deepcopy(receipt);bad['accounting_settlement']['used_tokens']=0
            with self.assertRaises(InvalidUpdate):used(env,bad)
        self.assertEqual(used(env,{}),1500)
        old=dict(env);old.pop('model_interface')
        self.assertEqual(used(old,receipt),1500)
        self.assertIsNone(settlement(old,record))

    def test_dispatch_permit_binds_projected_packet(self):
        packet={'messages':[],'schema':{}};args=('call','model','system',{},{});token=_permit.set({'used':False,'hash':request_hash(*args),'packet_hash':'different'})
        try:
            with self.assertRaises(InvalidUpdate):consume_dispatch(*args,packet=packet)
            self.assertFalse(_permit.get()['used'])
        finally:_permit.reset(token)

    def test_optional_graph_pages_disclose_omissions_without_dangling_references(self):
        from model_packet import facts_view
        facts={'nodes':[{'id':'a','name':'entry'},{'id':'b','name':'leaf'}],
               'calls':[{'id':'c','source':'a','target':'b','text':'call '+('x'*500)}],
               'diagnostics':[{'message':'x'*500}], 'diagnostic_count':30}
        original=copy.deepcopy(facts)
        view=facts_view(facts,512)
        self.assertLessEqual(len(canonical(view).encode()),512)
        self.assertEqual(view['omitted_sample_calls'],1);self.assertEqual(view['diagnostic_count'],30)
        self.assertEqual(facts,original)
        larger=facts_view(facts,1200)
        for call in larger['calls']:
            self.assertIn(call['source'],{n['id'] for n in larger['nodes']})
            self.assertIn(call['target'],{n['id'] for n in larger['nodes']})

    def test_memory_projection_preserves_recent_answers_pins_and_retrieval_addresses(self):
        from model_packet import memory_view
        m={'invocation_id':'private-binding','summary_unverified':'note','pinned_user_statements':[{'source':'receipt','quote':'Do not edit.','authority':'user_statement'}],'recent_turns':[{'id':'t1','user':'device A','assistant_unverified':'device B','hash':'private-hash'}],'retrieved_turns':[{'id':'t0','user':'old exact statement','assistant_unverified':'old answer'}],'toc':[{'id':'t2','subject':'older','keywords':['older']}]}
        view=memory_view(m)
        self.assertEqual(view['recent_turns'][0]['assistant_unverified'],'device B')
        self.assertEqual(view['retrieved_turns'][0]['user'],'old exact statement')
        self.assertEqual(view['pinned_user_statements'][0]['quote'],'Do not edit.')
        self.assertEqual(view['toc'][0],m['toc'][0]);self.assertNotIn('invocation_id',view)
        self.assertNotIn('hash',view['recent_turns'][0])

if __name__=='__main__':unittest.main()

from test_conversation import Fake as ConversationFake
import test_orchestration as orchestration_fixture
import orchestration as machine
from invocation_gateway import InvocationLedger

class WireConversation(ConversationFake):
    model_interface=1
    def call(self,cid,system,p,schema):
        self.packet=build(system,p,schema)
        with patch('test_conversation.consume_dispatch',side_effect=lambda *args:consume_dispatch(*args,packet=self.packet)):
            answer,record=super().call(cid,system,p,schema)
        record['response']={'done':True,'prompt_eval_count':500,'eval_count':90}
        return answer,record

class BranchAccountingTests(unittest.TestCase):
    setUp=orchestration_fixture.OrchestrationTests.setUp
    tearDown=orchestration_fixture.OrchestrationTests.tearDown
    step=orchestration_fixture.OrchestrationTests.step
    until=orchestration_fixture.OrchestrationTests.until
    children=orchestration_fixture.OrchestrationTests.children
    def provider(self,stage):
        name=orchestration_fixture.MODELS[self.tier]
        if stage=='conversation':
            p=WireConversation(name,lambda r,p:r.update(intent='workspace',facts=['Remember project Cedar, ticket CED724.'],workspace_request={'operation':'read','scope':'workspace','paths':[]}))
        else:p=WireModel(name)
        self.models.append((stage,p));return p

    def test_child_settlements_agree_with_parent_after_reload_and_model_switch(self):
        out=self.children();cid=out['children'][0]['id'];n=machine.node(self.s,cid);sid=self.meta['active']
        before=usage(self.s,sid)['used']['tokens'];contract=self.s.pinned(sid)
        with self.assertRaises(machine.OrchestrationStop):machine.allowed(machine.gates(self.s,cid,{'tokens':n['limits']['tokens']+1}))
        out=self.step(out)
        self.assertEqual(machine.spent(self.s,cid)['tokens'],480)
        self.assertEqual(usage(self.s,sid)['used']['tokens']-before,480)
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');self.tier='high'
        out=self.until('DONE',out)
        self.assertTrue(out['data']['result']['admission']['admitted'],out)
        self.assertEqual(contract,self.s.pinned(sid))
        children=[p for stage,p in self.models if stage=='subagent']
        self.assertGreaterEqual(len({p.model for p in children}),2)
        for p in children:
            body=json.loads(p.packet['messages'][1]['content'])
            self.assertIn('CED724',canonical(body['memory']))
            self.assertIn('No network',canonical(body['memory']))
            self.assertEqual(body['memory']['recent_turns'][-1]['user'],self.prompt)
            self.assertFalse(body['task']['requirements']['edits_allowed'])
            self.assertFalse(body['task']['requirements']['network_allowed'])
        self.assertEqual(machine.spent(self.s,n['parent_id'])['tokens'],usage(self.s,sid)['used']['tokens'])
        self.assertEqual(machine.spent(self.s,n['parent_id'])['tokens'],480*(len(children)+1))
        self.assertTrue(self.s.verify_invocations(sid))

    def test_interruption_retains_full_reservation_and_recovers_without_refund(self):
        out=self.children();cid=out['children'][0]['id'];sid=self.meta['active'];contract=self.s.pinned(sid)
        original=WireModel.call
        def interrupted(model,cid,system,p,schema):
            consume_dispatch(cid,model.model,system,p,schema,packet=build(system,p,schema))
            raise KeyboardInterrupt()
        with patch.object(WireModel,'call',interrupted),self.assertRaises(KeyboardInterrupt):self.step(out)
        held=usage(self.s,sid)['used'];self.assertEqual(held['calls'],1)
        env=json.loads(self.s.db.execute('SELECT record_json FROM invocations WHERE session=? ORDER BY rowid DESC',(sid,)).fetchone()[0])
        self.assertEqual(held['tokens'],reserved(env))
        from lifecycle import reconcile
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');reconcile(self.s,self.ws)
        self.assertEqual(usage(self.s,sid)['used'],held)
        self.tier='high';out=self.until('DONE',out)
        self.assertTrue(out['data']['result']['admission']['admitted'],out)
        self.assertEqual(contract,self.s.pinned(sid))
        self.assertGreaterEqual(usage(self.s,sid)['used']['tokens'],held['tokens']+480)
        self.assertTrue(self.s.verify_invocations(sid))
