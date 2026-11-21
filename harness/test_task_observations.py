import copy
import json
import unittest
from unittest.mock import patch

import conversation
import chat_runtime
import task_observations as obs
from context_manager import canonical, sha, ContextBudgetExceeded
from invocation_gateway import InvocationLedger
from review_budget import usage
import test_chat
from test_chat import Fake as Worker, MODELS
from test_conversation import Fake as Router


COMMITMENT = {'understanding': 'Explain the source without edits or network.',
              'criteria': [{'id': 'c1', 'deliverable': 'Explain the entry flow',
                            'done_when': 'Identify the entry and leaf behavior using source references.'},
                           {'id': 'c2', 'deliverable': 'Give an example',
                            'done_when': 'Include one input and expected result.'}], 'assumptions': []}


def assessment():
    return {'judgement': 'partial', 'delivered': 'Explained entry and leaf.',
            'remaining': ['One worked example.'],
            'items': [{'criterion_id': 'c1', 'status': 'met', 'evidence': 'entry.py and leaf.py'},
                      {'criterion_id': 'c2', 'status': 'unmet', 'evidence': 'No example was delivered.'}]}


class ObservationTests(unittest.TestCase):
    setUp = test_chat.ChatTests.setUp
    tearDown = test_chat.ChatTests.tearDown

    def route(self, value=COMMITMENT, prompt='Explain the entry flow and give an example. No edits or network.'):
        def answer(r, p):
            r.update(intent='workspace', answer='I will inspect the source.', summary='Source explanation requested.')
            if value is not None:
                r[obs.FIELD] = copy.deepcopy(value)
        out = conversation.answer(self.s, self.meta, self.storage,
            {'prompt': prompt, 'models': MODELS, 'tier': 'low'}, provider=Router(MODELS['low'], answer))
        self.assertTrue(out['result']['admission']['admitted'])
        return out, prompt

    def work(self, routed, prompt, tier='high', value=None):
        provider = Worker(MODELS[tier], lambda r,p:r.update({obs.FIELD: assessment() if value is None else value}))
        from paging import Budget
        provider.budget = Budget(32768 if tier == 'high' else 16384, 2048, 256)
        out = chat_runtime.answer(self.s,self.meta,self.ws,self.storage,
            {'prompt':prompt,'models':MODELS,'tier':tier,'conversation_id':routed['result']['invocation_id']},provider=provider)
        return out, provider

    def test_precommit_and_assessment_across_reload_and_model_switch(self):
        original_contract = self.s.current_contract(self.sid)
        routed, prompt = self.route()
        iid = routed['result']['invocation_id']
        commitment = obs.commitment(self.s, iid)
        self.assertEqual(commitment['value'], COMMITMENT)
        self.assertEqual(commitment['request'], prompt)
        self.assertEqual(commitment['version'], 1)
        self.assertNotIn(obs.FIELD, routed['result']['accepted_output'])
        self.s.close(); self.s = InvocationLedger(self.storage/'state.sqlite')
        before = usage(self.s, self.sid)
        first, large = self.work(routed, prompt)
        self.assertTrue(first['result']['admission']['admitted'])
        self.assertEqual(large.payloads[0]['conversation_handoff']['task_commitment'], obs.context(commitment))
        self.assertEqual(large.payloads[0]['request'], prompt)
        middle = usage(self.s, self.sid)
        second, small = self.work(routed, prompt, 'low')
        after = usage(self.s, self.sid)
        self.assertEqual(small.payloads[0]['conversation_handoff']['task_commitment'], obs.context(commitment))
        self.assertEqual(self.s.current_contract(self.sid), original_contract)
        self.assertEqual(before['limits'], after['limits'])
        self.assertEqual(middle['used']['calls'], before['used']['calls'] + 1)
        self.assertEqual(after['used']['calls'], middle['used']['calls'] + 1)
        self.assertGreater(after['used']['tokens'], middle['used']['tokens'])
        self.assertEqual(second['result']['task_observation']['commitment_id'], iid)
        self.assertEqual(second['result']['task_observation']['value'], assessment())
        self.assertFalse(second['result']['accepted_output']['complete'])
        self.assertTrue(self.s.verify_invocations(self.sid))
        self.assertTrue(self.s.verify_invocations(routed['session']))
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM host_actions').fetchone()[0], 0)

    def test_context_too_small_preserves_commitment_without_dispatch_or_refund(self):
        from paging import Budget
        routed,prompt=self.route()
        self.work(routed,prompt)
        iid=routed['result']['invocation_id'];saved=obs.commitment(self.s,iid)
        before=usage(self.s,self.sid);contract=self.s.current_contract(self.sid)
        small=Worker(MODELS['low']);small.budget=Budget(4096,1024,256)
        with self.assertRaises(ContextBudgetExceeded):
            chat_runtime.answer(self.s,self.meta,self.ws,self.storage,
                {'prompt':prompt,'models':MODELS,'tier':'low','conversation_id':iid},provider=small)
        self.assertEqual(small.payloads,[])
        self.assertEqual(obs.commitment(self.s,iid),saved)
        self.assertEqual(self.s.current_contract(self.sid),contract)
        after=usage(self.s,self.sid)
        self.assertEqual(after['limits'],before['limits'])
        for name in ('calls','tokens'):
            self.assertEqual(after['used'][name],before['used'][name])

    def test_absent_or_malformed_commitment_never_repairs_or_changes_route(self):
        for value in [None, {}, 'bad', {'understanding':'x'*9000}, {'permissions':{'edits_allowed':True}}]:
            with self.subTest(value=str(value)[:30]):
                routed, _ = self.route(value)
                self.assertEqual(len(routed['result']['token_usage']['calls']), 1)
                self.assertEqual(routed['result']['accepted_output']['intent'], 'workspace')
                self.assertEqual(routed['result']['task_observation']['status'], 'missing' if value is None else 'malformed')
                self.assertTrue(self.s.verify_invocations(routed['session']))

    def test_inconsistent_completion_is_recorded_without_admission_or_budget_effect(self):
        routed,prompt=self.route();value=assessment()
        value.update(judgement='complete');value['items']=[value['items'][0],value['items'][0]]
        out,provider=self.work(routed,prompt,value=value)
        self.assertTrue(out['result']['admission']['admitted'])
        self.assertEqual(len(provider.payloads),1)
        self.assertFalse(out['result']['accepted_output']['complete'])
        self.assertEqual(len(out['result']['task_observation']['issues']),3)

    def test_worker_omission_is_optional_before_decoding_and_diagnostic_at_admission(self):
        routed,prompt=self.route();provider=Worker(MODELS['high'])
        out=chat_runtime.answer(self.s,self.meta,self.ws,self.storage,
            {'prompt':prompt,'models':MODELS,'tier':'high','conversation_id':routed['result']['invocation_id']},provider=provider)
        from model_packet import build
        schema = chat_runtime.schema(provider.payloads[0])
        packet = build(chat_runtime.SYSTEM, provider.payloads[0], schema)
        self.assertNotIn(obs.FIELD, schema['required'])
        self.assertNotIn(obs.FIELD, packet['schema']['required'])
        self.assertEqual(packet['schema']['properties'][obs.FIELD], {})
        self.assertTrue(out['result']['admission']['admitted'])
        self.assertEqual(out['result']['task_observation']['status'],'missing')
        self.assertEqual(len(provider.payloads),1)

    def test_malformed_assessment_does_not_withhold_valid_work(self):
        routed,prompt=self.route();out,provider=self.work(routed,prompt,value={'judgement':'complete','permissions':{'network_allowed':True}})
        self.assertTrue(out['result']['admission']['admitted'])
        self.assertEqual(len(provider.payloads),1)
        self.assertEqual(out['result']['task_observation']['status'],'malformed')
        self.assertFalse(self.s.snapshot(self.sid)['constraints']['network_allowed'])
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_decoder_observation_shape_cannot_block_commitments_or_assessments(self):
        from model_packet import build
        routed, prompt = self.route({'permissions': {'network_allowed': True}})
        env = json.loads(self.s.db.execute('SELECT record_json FROM invocations WHERE id=?',
            (routed['result']['invocation_id'],)).fetchone()[0])
        payload = json.loads(self.s.db.execute('SELECT payload_json FROM deliveries WHERE id=?',
            (env['delivery_id'],)).fetchone()[0])
        wire = build(conversation.SYSTEM, payload, env['schema'])['schema']
        self.assertNotIn(obs.FIELD, wire['required'])
        self.assertEqual(wire['properties'][obs.FIELD], {})
        self.assertEqual(routed['result']['task_observation']['status'], 'malformed')
        for value in (None, {}, 'bad', 1.5, {'permissions': {'network_allowed': True}}):
            with self.subTest(value=value):
                good, request = self.route()
                provider = Worker(MODELS['high'], lambda r,p:r.update({obs.FIELD: value}))
                out = chat_runtime.answer(self.s, self.meta, self.ws, self.storage,
                    {'prompt': request, 'models': MODELS, 'tier': 'high',
                     'conversation_id': good['result']['invocation_id']}, provider=provider)
                packet = build(chat_runtime.SYSTEM, provider.payloads[0],
                               chat_runtime.schema(provider.payloads[0]))
                self.assertEqual(packet['schema']['properties'][obs.FIELD], {})
                self.assertEqual(out['result']['task_observation']['status'], 'malformed')
                self.assertTrue(out['result']['admission']['admitted'])
                self.assertEqual(len(provider.payloads), 1)
                self.assertFalse(self.s.snapshot(self.sid)['constraints']['network_allowed'])

    def test_observation_cannot_hide_authoritative_constraint_failure(self):
        routed,prompt=self.route()
        def bad(r,p):
            r.update(task_observation=assessment(),constraints=[])
        out=chat_runtime.answer(self.s,self.meta,self.ws,self.storage,
            {'prompt':prompt,'models':MODELS,'tier':'high','conversation_id':routed['result']['invocation_id']},provider=Worker(MODELS['high'],bad))
        self.assertFalse(out['result']['admission']['admitted'])
        self.assertIsNone(out['result']['accepted_output'])
        self.assertEqual(out['result']['task_observation']['status'],'recorded')
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_original_response_reconstructs_including_untrusted_observation(self):
        routed,_=self.route();iid=routed['result']['invocation_id'];rec=obs.receipt(self.s,iid)
        raw=dict(rec['proposal']);raw.pop('context_binding');raw.update(rec['observation_fields'])
        self.assertEqual(sha(canonical(raw)),rec['model_response_hash'])
        self.assertNotIn(obs.FIELD,rec['proposal'])
        self.assertTrue(self.s.verify_invocations(routed['session']))

    def test_greetings_and_direct_work_do_not_manufacture_a_precommitment(self):
        out=conversation.answer(self.s,self.meta,self.storage,{'prompt':'Hello','models':MODELS,'tier':'low'},provider=Router(MODELS['low']))
        self.assertNotIn('task_observation',out['result'])
        direct=chat_runtime.answer(self.s,self.meta,self.ws,self.storage,
            {'prompt':'Explain start.','models':MODELS,'tier':'low'},provider=Worker())
        self.assertNotIn('task_observation',direct['result'])

    def test_revision_keeps_previous_commitment_and_original_request(self):
        routed,prompt=self.route();iid=routed['result']['invocation_id']
        env=json.loads(self.s.db.execute('SELECT record_json FROM invocations WHERE id=?',(iid,)).fetchone()[0])
        p=json.loads(self.s.db.execute('SELECT payload_json FROM deliveries WHERE id=?',(env['delivery_id'],)).fetchone()[0])
        p['routing_state']['previous']=iid
        changed=copy.deepcopy(COMMITMENT);changed['understanding']='New interpretation, same original ask.'
        current=obs.capture(self.s,dict(env,id='f'*32),p,{'intent':'workspace'},{obs.FIELD:changed})
        self.assertEqual(current['version'],2);self.assertEqual(current['previous_version'],iid)
        self.assertEqual(current['request'],prompt)
        self.assertEqual(obs.commitment(self.s,iid)['value'],COMMITMENT)

    def test_withheld_publication_cannot_be_reported_as_delivery(self):
        routed,prompt=self.route();out,_=self.work(routed,prompt)
        summary=obs.summary(self.s,routed['result']['invocation_id'],out['result']['invocation_id'],'withheld')
        self.assertEqual(summary['delivery_status'],'withheld')
        self.assertEqual(summary['assessment']['value'],assessment())
        self.assertEqual(summary['mode'],'observe_only')

import test_orchestration
import orchestration
import orchestration_flow


class ObservationFlowTests(unittest.TestCase):
    setUp = test_orchestration.OrchestrationTests.setUp
    tearDown = test_orchestration.OrchestrationTests.tearDown
    step = test_orchestration.OrchestrationTests.step
    until = test_orchestration.OrchestrationTests.until

    def provider(self, stage):
        model = test_orchestration.OrchestrationTests.provider(self, stage)
        original = model.fault
        def annotate(r, p):
            if original: original(r, p)
            r[obs.FIELD] = copy.deepcopy(COMMITMENT) if stage=='conversation' else assessment()
        model.fault = annotate
        return model

    def test_pause_resume_children_switch_and_final_assessment_keep_commitment(self):
        out=self.until('CHILDREN');iid=out['conversation_id'];c=out['task_observations']['commitment']
        sid=self.meta['active'];contract=self.s.pinned(sid)
        parent=orchestration.node(self.s,out['children'][0]['id'])['parent_id']
        used=orchestration.spent(self.s,parent)
        from monitoring import ResourceWait
        with patch('subagent_runtime.run',side_effect=ResourceWait({'admitted':False,'decision':'BRIDGE'})),patch('runtime_memory.recover',return_value=None):
            paused=self.step(out)
        self.assertEqual(paused['state'],'PAUSED')
        self.assertEqual(paused['task_observations']['commitment'],c)
        self.assertEqual(orchestration.spent(self.s,parent),used)
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');self.tier='high'
        with patch('monitoring.require_resources',return_value={'admitted':True}),patch('runtime_memory.recover',return_value=None):
            resumed=self.step(paused,event='resume')
        self.assertEqual(orchestration.spent(self.s,parent),used)
        done=self.until('DONE',resumed)
        self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertEqual(done['task_observations']['commitment'],c)
        self.assertEqual(done['task_observations']['assessment']['commitment_id'],iid)
        self.assertEqual(done['task_observations']['delivery_status'],'admitted')
        self.assertEqual(self.s.pinned(sid),contract)
        for stage,model in self.models:
            if stage in ('subagent','synthesis'):
                for p in model.payloads:
                    if 'claims' in p:
                        # The separate claim reviewer receives fresh evidence,
                        # not the writer's conversational commitment as proof.
                        self.assertNotIn('conversation_handoff',p)
                        continue
                    self.assertEqual(p['conversation_handoff']['task_commitment'],obs.context(c))
                    self.assertEqual(p['request'],self.prompt)
                    self.assertEqual(p['contract']['constraints'],contract['constraints'])
                    if stage == 'subagent':
                        from model_packet import build
                        import subagent_runtime
                        packet = build(subagent_runtime.SYSTEM, p, subagent_runtime.schema(p))
                        body = json.loads(packet['messages'][1]['content'])
                        self.assertNotIn('task_commitment', body)
                        self.assertNotIn(obs.FIELD, packet['schema']['properties'])
                        self.assertNotIn('assess its criteria', packet['messages'][0]['content'])
        self.assertGreater(orchestration.spent(self.s,parent)['calls'],used['calls'])
        self.assertTrue(self.s.verify_invocations(sid))


import test_host_actions
from paging import Budget
from vscode_bridge import dispatch


class ObservationActionTests(unittest.TestCase):
    setUp = test_host_actions.ActionTests.setUp
    tearDown = test_host_actions.ActionTests.tearDown

    def test_self_judgement_cannot_execute_or_widen_an_edit(self):
        prompt='Continue this bounded task.'
        router=Router('small',lambda r,p:r.update(intent='workspace',task_observation=copy.deepcopy(COMMITMENT)))
        route=conversation.answer(self.s,self.meta,self.storage,{'prompt':prompt,'models':{'low':'small','mid':'large','high':'large'},'tier':'low'},provider=router)
        def claim(r):
            value=assessment();value.update(judgement='complete',remaining=[])
            for item in value['items']:item['status']='met'
            r[obs.FIELD]=value
        coder=test_host_actions.Coder(claim);coder.budget=Budget(32768,4096,256)
        request={'op':'work','mode':'edit','model':coder.model,'prompt':prompt,
                 'pages':['src/retry.py#1','tests/test_retry.py#1'],'conversation_id':route['result']['invocation_id']}
        out=dispatch(self.s,self.meta,self.workspace,self.storage,request,coder)
        self.assertTrue(out['result']['admission']['admitted'])
        self.assertEqual(out['result']['task_observation']['value']['judgement'],'complete')
        self.assertFalse(out['result']['accepted_output']['complete'])
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM host_actions').fetchone()[0],0)
        self.assertEqual((self.workspace/'src/retry.py').read_text(),test_host_actions.SOURCE)
        def outside(r):claim(r);r['action']['target']='outside.py'
        bad=test_host_actions.Coder(outside);bad.budget=coder.budget
        refused=dispatch(self.s,self.meta,self.workspace,self.storage,request,bad)
        self.assertFalse(refused['result']['admission']['admitted'])
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM host_actions').fetchone()[0],0)
        self.assertTrue(self.s.verify_invocations(self.session))
