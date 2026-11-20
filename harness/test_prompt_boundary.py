import copy
import io
import json
import unittest
from unittest.mock import MagicMock, patch

import conversation as chat
import conversation_memory as cm
from context_manager import InvalidUpdate, canonical, sha
from decoder_schema import for_decoder, profile
from invocation_roles import validate_shape
from review_budget import usage
import test_conversation as fixture
from test_conversation import Fake, MODELS
import test_conversation_memory as memory_fixture
from test_reasoning_policy import model


class BoundaryTests(unittest.TestCase):
    setUp = fixture.ConversationTests.setUp
    tearDown = fixture.ConversationTests.tearDown
    ask = fixture.ConversationTests.ask

    def test_host_binds_raw_response_and_keeps_metadata_out_of_prompt(self):
        provider = Fake()
        out = self.ask('Remember Cedar. Never change the kernel.', provider=provider)
        p = provider.payloads[0]
        raw = json.loads(self.s.db.execute('SELECT record_json FROM invocation_receipts WHERE invocation_id=?',
                                         (out['result']['invocation_id'],)).fetchone()[0])
        self.assertEqual(raw['host_fields'], {'context_binding': p['context_binding']})
        original = dict(raw['proposal']); original.pop('context_binding')
        self.assertEqual(raw['model_response_hash'], sha(canonical(original)))
        self.assertTrue(self.s.verify_invocations(out['session']))
        wire = chat.ConversationModel(self.storage/'calls').messages(chat.SYSTEM, p)
        self.assertEqual(wire[-1]['content'], p['request'])
        for value in (p['context_binding'], p['_request_nonce'], p['contract']['contract_hash']):
            self.assertNotIn(value, canonical(wire))
        self.assertNotIn('Echo context_binding', canonical(wire))
        self.assertIn('RESPONSE_FORMAT', wire[0]['content'])
        self.assertNotIn('"minLength"', wire[0]['content'])
        self.assertNotIn('"additionalProperties"', wire[0]['content'])
        self.assertIn('Omit unused optional fields entirely', wire[0]['content'])

    def test_format_guidance_does_not_supply_project_facts_or_diagram_devices(self):
        for request in ('Review the project architecture and tools logic with a few block diagrams.',
                        'Draw a conceptual flow Antenna -> Decoder -> Speaker.'):
            with self.subTest(request=request):
                provider = Fake()
                self.ask(request, provider=provider)
                payload = provider.payloads[0]
                wire = chat.ConversationModel(self.storage/'calls').messages(chat.SYSTEM, payload)
                self.assertEqual(wire[-1]['content'], request)
                instructions = wire[0]['content']
                self.assertIn('original user request remains authoritative', instructions)
                self.assertIn('format examples are not project facts or requested work', instructions)
                self.assertIn('Each node has id, label, kind', instructions)
                self.assertIn('each edge has source, target, label', instructions)
                for unrelated in ('Sensor', 'Display', 'app.py', 'tools.py', 'config.py'):
                    self.assertNotIn(unrelated, instructions)

    def test_decoder_copy_is_version_scoped_and_host_limits_remain_strict(self):
        provider = Fake(); self.ask(provider=provider); p = provider.payloads[0]
        original = chat.model_schema(p, chat.schema(p)); before = copy.deepcopy(original)
        info = {'details': {'runner': 'mlx'}}
        compat = profile(info, '0.40.16')
        adapted = for_decoder(original, compat)
        self.assertEqual(original, before)
        self.assertNotIn('maxLength', adapted['properties']['answer'])
        self.assertEqual(adapted['required'], original['required'])
        self.assertEqual(adapted['properties']['intent'], original['properties']['intent'])
        self.assertFalse(adapted['additionalProperties'])
        good = {'intent': 'conversation', 'page_requests': [], 'escalation_reason': 'none',
                'answer': 'alpha "beta"\nsecond', 'summary': 'Short note.', 'diagram': None}
        validate_shape(good, original)
        for change in ({'answer': ''}, {'answer': 'x'*6001}, {'context_binding': p['context_binding']},
                       {'action': 'edit'}, {'page_requests': ['unknown']}):
            with self.subTest(change=change), self.assertRaises(Exception):
                chat.bind_output(p, dict(good, **change))
        for other in (profile(info, None), profile(info, '0.40.17'), profile({'details': {'runner': 'ggml'}}, '0.40.16')):
            self.assertEqual(for_decoder(original, other), original)

    def test_actual_transport_uses_wire_adapter_but_gateway_retains_host_schema(self):
        fake = Fake(); self.ask(provider=fake); p = fake.payloads[0]
        provider = chat.ConversationModel(self.storage/'calls', budget=fake.budget)
        provider.thinking = p['reasoning']['requested']; provider.context_metadata = fake.context_metadata
        provider.decoder_compatibility = profile({'details': {'runner': 'mlx'}}, '0.40.16')
        answer = {'intent': 'conversation', 'page_requests': [], 'escalation_reason': 'none',
                  'answer': 'alpha "beta"\nsecond', 'summary': 'Note.', 'diagram': None}
        response = io.BytesIO((json.dumps({'message': {'content': json.dumps(answer)},
                            'done': True, 'done_reason': 'stop', 'prompt_eval_count': 100, 'eval_count': 30})+'\n').encode())
        response.status = 200
        conn = MagicMock(); conn.getresponse.return_value = response
        with patch('invocation_gateway.consume_dispatch'), patch('http.client.HTTPConnection', return_value=conn):
            result, record = provider.call('wire', chat.SYSTEM, p, chat.schema(p))
        sent = json.loads(conn.request.call_args.args[2])
        self.assertNotIn('context_binding', sent['format']['properties'])
        self.assertNotIn('maxLength', sent['format']['properties']['answer'])
        self.assertEqual(chat.schema(p)['properties']['answer']['maxLength'], 6000)
        self.assertEqual(result, answer)
        self.assertEqual(record['decoder_compatibility']['policy'], 'mlx-string-bounds-v1')
        trace = json.loads((self.storage/'calls'/'wire.json').read_text())
        self.assertEqual(trace['decoder_compatibility'], provider.decoder_compatibility)
        self.assertNotIn('request', trace)
        self.assertNotIn('answer', trace)

    def test_missing_actionable_page_request_repairs_then_pages_before_publishing(self):
        first, parent = memory_fixture.MemoryTests.fill(self)
        before = usage(self.s, first['session'])['used']['calls']
        target = []
        def respond(r, p):
            if p['requested_memory_pages']:
                r.update(answer='Now using the requested evidence.')
            elif p.get('repair'):
                target.append(p['memory']['toc'][-1]['id'])
                r.update(page_requests=[target[0]], escalation_reason='missing_context')
            else:
                r.update(answer='I request the page in this prose only.', escalation_reason='missing_context')
        provider = Fake(fault=respond)
        out = chat.answer(self.s, self.meta, self.storage,
                          dict(prompt='Explain that earlier topic.', parent=parent, models=MODELS, tier='auto'), provider)
        self.assertTrue(out['result']['admission']['admitted'])
        self.assertEqual(len(provider.payloads), 3)
        self.assertIn('top-level page_requests', provider.payloads[1]['repair']['errors'][0])
        self.assertIn(target[0], [r['id'] for r in provider.payloads[2]['memory']['retrieved_turns']])
        self.assertEqual(usage(self.s, first['session'])['used']['calls'], before + 3)
        self.assertNotIn('this prose only', canonical(cm.history(self.s, first['session'], out['result']['invocation_id'])))

    def test_precise_missing_evidence_question_finishes_without_retry_or_escalation(self):
        first, parent = memory_fixture.MemoryTests.fill(self)
        def respond(r, p):
            r.update(escalation_reason='missing_context', context_question='Which unpublished report do you mean?',
                     answer='Which unpublished report do you mean?')
        provider = Fake(fault=respond)
        out = self.ask('What does the unpublished report conclude?', parent, provider)
        self.assertTrue(out['result']['admission']['admitted'])
        self.assertEqual(len(provider.payloads), 1)
        self.assertIsNone(out['routing_followup'])

    def test_truncated_answer_repair_is_bounded_and_keeps_original_permissions(self):
        for answer in ('   ', '```json', '```python\ndef broken():'):
            provider = Fake(fault=lambda r, p: r.update(answer=answer))
            out = self.ask(provider=provider)
            self.assertFalse(out['result']['admission']['admitted'])
            self.assertEqual(len(provider.payloads), 3)
            self.assertEqual(usage(self.s, out['session'])['used']['calls'], 3)
            self.assertTrue(all(not p['contract']['constraints']['edits_allowed'] for p in provider.payloads))

    def test_task_selected_effort_still_obeys_user_override_and_gateway_binding(self):
        for prompt, expected in [('Hello there', False), ('can you access the codebase?', False),
                                 ('i want you to explain to me the aarchitecture using diagrams in a way you would to a new intern joining the team who will take care of the project going forward', False),
                                 ('Maximise value under a budget of 7.', 'low'),
                                 ('What is JSON?', False), ('Reply with a fenced JSON object.', 'low'),
                                 ('Trace this implementation: ```python\npass\n```', 'low')]:
            provider = model(values=[False, 'low', 'medium'], fault=None)
            with patch.dict('os.environ', SUTRA_THINKING='adaptive'):
                out = self.ask(prompt, provider=provider)
            self.assertEqual(out['reasoning']['requested'], expected)
            self.assertEqual(len(provider.payloads), 1)
        provider = model(values=[False, 'low'], fault=None)
        with patch.dict('os.environ', SUTRA_THINKING='off'):
            out = self.ask('Maximise value under a budget of 7.', provider=provider)
        self.assertIs(out['reasoning']['requested'], False)
        altered = copy.deepcopy(provider.payloads[0]); altered['reasoning']['step'] = 1
        with self.assertRaises(InvalidUpdate): chat.request_check(self.s, out['session'], altered)

    def test_optional_fields_are_omitted_and_contradictory_handoff_is_repaired(self):
        def respond(r, p):
            r['workspace_request'] = {'operation': 'edit', 'paths': []}
            if p.get('repair'):
                r['intent'] = 'workspace'
        provider = Fake(fault=respond)
        out = self.ask('Explain the current project architecture.', provider=provider)
        self.assertEqual(len(provider.payloads), 2)
        self.assertIn('intent=workspace', provider.payloads[-1]['repair']['errors'][0])
        self.assertTrue(out['result']['admission']['admitted'])
        proposal = out['result']['accepted_output']
        self.assertEqual(proposal['intent'], 'workspace')
        self.assertNotIn('context_question', proposal)
        self.assertNotIn('facts', proposal)
        self.assertFalse(out['workspace_tools_used'])
        self.assertNotIn('active', self.meta)
        self.assertEqual(usage(self.s, out['session'])['used']['calls'], 2)

    def test_unused_read_hint_does_not_trigger_repairs_or_file_access(self):
        provider = Fake(fault=lambda r, p: r.update(workspace_request={'operation': 'read', 'paths': []}))
        out = self.ask('Explain the supplied code: def twice(x): return x * 2', provider=provider)
        self.assertTrue(out['result']['admission']['admitted'])
        self.assertEqual(out['result']['accepted_output']['intent'], 'conversation')
        self.assertEqual(len(provider.payloads), 1)
        self.assertIsNone(out['routing_followup'])
        self.assertFalse(out['workspace_tools_used'])
        self.assertNotIn('active', self.meta)

    def test_capability_then_workspace_handoff_survives_reload_and_model_switch(self):
        from invocation_gateway import InvocationLedger
        first_provider = Fake()
        first = self.ask('can you access the codebase?', provider=first_provider)
        contract = chat.contract(self.s, first['session'])
        before = usage(self.s, first['session'])
        self.s.close()
        self.s = InvocationLedger(self.storage/'state.sqlite')
        next_provider = Fake('large', lambda r, p: r.update(intent='workspace',
                             answer='I will inspect the source and explain the architecture.'))
        second = self.ask('Explain the architecture to a new intern.',
                          first['result']['invocation_id'], next_provider, 'high')
        packet = next_provider.payloads[0]
        self.assertIn('can you access the codebase?', canonical(packet['memory']))
        self.assertEqual(chat.contract(self.s, second['session']), contract)
        self.assertEqual(second['session'], first['session'])
        self.assertEqual(usage(self.s, second['session'])['limits'], before['limits'])
        self.assertEqual(usage(self.s, second['session'])['used']['calls'], before['used']['calls']+1)
        self.assertFalse(second['workspace_tools_used'])
        self.assertNotIn('active', self.meta)
        self.assertFalse(packet['contract']['constraints']['edits_allowed'])
        self.assertTrue(self.s.verify_invocations(second['session']))


if __name__ == '__main__':
    unittest.main()
