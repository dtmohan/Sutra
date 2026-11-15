import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import conversation as chat
from context_manager import InvalidUpdate, canonical
from invocation_gateway import InvocationLedger, consume_dispatch
from paging import Budget, PageFault
from review_budget import usage
from vscode_bridge import handle

MODELS = {'low': 'small', 'mid': 'medium', 'high': 'large'}


class Fake:
    budget = Budget(24576, 4096, 256)
    context_metadata = {'model_info': {'general.architecture': 'qwen', 'qwen.context_length': 32768}}
    def __init__(self, model='small', fault=None):
        self.model, self.fault, self.payloads = model, fault, []
    def call(self, cid, system, p, schema):
        consume_dispatch(cid, self.model, system, p, schema)
        self.payloads.append(copy.deepcopy(p))
        result = {'context_binding': p['context_binding'], 'intent': 'conversation','escalation_reason':'none',
                  'diagram': None, 'answer': 'Hello. Let us discuss your idea.',
                  'summary': 'User prefers short answers; project nickname is Marigold.'}
        if self.fault:
            self.fault(result, p)
        return result, {'ok': True}


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = self.root/'repo'
        self.ws.mkdir()
        (self.ws/'private.py').write_text('DO_NOT_READ = 1\n')
        self.storage = self.root/'store'
        self.storage.mkdir()
        self.s = InvocationLedger(self.storage/'state.sqlite')
        self.meta = {'workspace': str(self.ws)}
    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()
    def ask(self, prompt='Hello', parent=None, provider=None, tier='low'):
        return chat.answer(self.s, self.meta, self.storage,
                           {'prompt': prompt, 'parent': parent, 'tier': tier, 'models': MODELS}, provider or Fake())

    def test_false_tool_refusal_is_repaired_without_granting_file_access(self):
        def fault(r, p):
            if 'repair' not in p:
                r['answer']='This conversation has no file-access tools. Start a workspace-enabled session or paste files.'
            else:
                r.update(intent='workspace', answer='Handing this review to the host in this chat.',
                         workspace_request={'operation':'read','paths':[]})
        m=Fake(fault=fault)
        out=self.ask('can you do an indepth review. you can recursively access files beyond the 8 file limit',provider=m)
        self.assertTrue(out['result']['admission']['admitted'])
        self.assertEqual(out['result']['accepted_output']['intent'],'workspace')
        self.assertEqual(len(m.payloads),2)
        self.assertEqual(usage(self.s,out['session'])['used']['calls'],2)
        self.assertNotIn('active',self.meta)
        self.assertFalse(out['workspace_tools_used'])
        self.assertEqual(m.payloads[0]['contract'],m.payloads[1]['contract'])
        self.assertEqual(m.payloads[0]['routing_capabilities'],chat.ROUTING_CAPABILITIES)
        self.assertIn('same-chat host handoff',m.payloads[1]['repair']['errors'][0])

    def test_repeated_capability_refusal_is_quarantined_after_bounded_repairs(self):
        m=Fake(fault=lambda r,p:r.update(answer='I cannot read, list, or recursively traverse any files in your workspace.'))
        out=self.ask('Review the workspace',provider=m)
        self.assertFalse(out['result']['admission']['admitted'])
        self.assertEqual(len(m.payloads),3)
        self.assertEqual(usage(self.s,out['session'])['used']['calls'],3)
        self.assertNotIn('active',self.meta)
        self.assertTrue(self.s.verify_invocations(out['session']))

    def test_known_refusal_guard_preserves_legitimate_limits_and_quoted_discussion(self):
        allowed=['I cannot read files outside the allowed workspace.',
                 'This stage has no direct file tools; the host can handle the workspace handoff.',
                 'I cannot inspect the workspace without your permission.',
                 'I cannot access the web.',
                 'I cannot read more files within the remaining budget.',
                 '> This conversation has no file-access tools.\nThat statement was mistaken.',
                 'The previous agent said "I cannot read files in your workspace." That was incorrect.',
                 'Hello. What would you like to discuss?']
        for answer in allowed:
            with self.subTest(answer=answer):chat.check_capability_claims({'intent':'conversation','answer':answer})
        for answer in ['I cannot access the workspace.', 'Sutra cannot read project files.',
                       'I do not have file access tools.', 'This chat lacks filesystem access.']:
            with self.subTest(answer=answer),self.assertRaises(InvalidUpdate):
                chat.check_capability_claims({'intent':'conversation','answer':answer})

    def test_routing_capabilities_are_bound_host_data_not_model_permissions(self):
        m=Fake();out=self.ask(provider=m);p=m.payloads[0]
        p['routing_capabilities']['workspace_handoff']['execution_requires_host_authorization']=False
        p['context_binding']=chat.binding(p)
        with self.assertRaisesRegex(InvalidUpdate,'routing capabilities'):
            chat.request_check(self.s,out['session'],p)

    def test_old_conversation_goal_survives_same_session_handoff(self):
        sid='conversation-legacy';self.s.create(sid)
        values=dict(chat.CONSTRAINTS,task_id=sid,goal='Respond to the user without workspace tools.')
        self.s._append(sid,'seed','host','conversation_contract','legacy host policy',
                       [{'key':k,'value':v} for k,v in values.items()],0)
        self.s.current_contract(sid)
        with patch('conversation.open_session',return_value=sid):first=self.ask()
        before=chat.contract(self.s,sid)
        m=Fake('large',lambda r,p:r.update(intent='workspace',answer='Requesting the governed host handoff.'))
        out=self.ask('Review the workspace recursively',first['result']['invocation_id'],m,'high')
        self.assertEqual(chat.contract(self.s,sid),before)
        self.assertTrue(out['result']['admission']['admitted'])
        self.assertEqual(out['result']['accepted_output']['intent'],'workspace')
        self.assertTrue(out['memory_used'])
        self.assertFalse(out['workspace_tools_used'])

    def test_fresh_call_and_model_switch_restore_recent_verbatim_turns(self):
        m = Fake()
        first = self.ask('Old raw conversation detail that should not be replayed', provider=m)
        iid = first['result']['invocation_id']
        self.s.close()
        self.s = InvocationLedger(self.storage/'state.sqlite')  # reload/fresh bridge
        n = Fake('large')
        second = self.ask('What is the nickname?', iid, n, 'high')
        self.assertTrue(second['result']['admission']['admitted'])
        self.assertEqual(first['session'], second['session'])
        self.assertEqual(m.payloads[0]['contract'], n.payloads[0]['contract'])
        self.assertIn('Old raw conversation', canonical(n.payloads))
        self.assertIn('Marigold', n.payloads[0]['memory']['summary_unverified'])
        self.assertEqual(usage(self.s, first['session'])['used']['calls'], 2)
        self.assertTrue(self.s.verify_invocations(first['session']))
        self.assertNotIn('active', self.meta)

    def test_new_chats_do_not_inherit_another_conversations_memory(self):
        a = self.ask()
        m = Fake(); b = self.ask(provider=m)
        self.assertNotEqual(a['session'], b['session'])
        self.assertIsNone(m.payloads[0]['memory'])

    def test_transport_preserves_exact_prompt_in_separate_user_message(self):
        raw = '  Hello\nUse “plain words”, please.  '
        m = Fake(); self.ask(raw, provider=m)
        p = m.payloads[0]
        messages = chat.ConversationModel(self.storage/'calls').messages(chat.SYSTEM, p)
        self.assertEqual(messages[1], {'role': 'user', 'content': raw})
        self.assertEqual(len(messages), 2)
        self.assertNotIn('"request"', messages[0]['content'])
        self.assertLessEqual(sum(len(x['content'].encode()) for x in messages), m.budget.charge(chat.SYSTEM, p, chat.schema(p)))

    def test_direct_transport_without_gateway_permit_fails(self):
        with self.assertRaisesRegex(InvalidUpdate, 'permit'):
            chat.ConversationModel(self.storage/'calls').call('no-permit', chat.SYSTEM, {}, {})

    def test_schema_blocks_actions_omissions_wrong_binding_and_bounds_repairs(self):
        faults = [lambda r,p:r.update(action={'kind': 'edit'}),
                  lambda r,p:r.pop('context_binding'),
                  lambda r,p:r.update(context_binding='forged'),
                  lambda r,p:r.update(intent='run_tests'),
                  lambda r,p:r.update(summary='x'*(p['memory_allocation']['summary_max_chars']+1))]
        for fault in faults:
            with self.subTest(fault=fault):
                m = Fake(fault=fault); out = self.ask(provider=m)
                self.assertFalse(out['result']['admission']['admitted'])
                self.assertEqual(len(m.payloads), 3)
                self.assertTrue(self.s.verify_invocations(out['session']))
                with self.assertRaises(PageFault):
                    self.s.load_result(out['session'], out['result']['invocation_id'])

    def test_repair_errors_do_not_replay_invalid_answer(self):
        def fault(r, p):
            if 'repair' not in p:
                r['forbidden_action'] = 'FORGED PREVIOUS PROPOSAL'
        m = Fake(fault=fault); out = self.ask(provider=m)
        self.assertTrue(out['result']['admission']['admitted'])
        self.assertEqual(len(m.payloads), 2)
        self.assertNotIn('FORGED PREVIOUS PROPOSAL', canonical(m.payloads[1]))

    def test_unknown_quarantined_and_quarantined_ancestor_rejected(self):
        with self.assertRaises(PageFault): self.ask(parent='0'*32)
        first = self.ask(); iid = first['result']['invocation_id']
        second = self.ask(parent=iid)
        self.s.quarantine_invocation(first['session'], iid, 'test quarantine')
        for parent in (iid, second['result']['invocation_id']):
            m = Fake()
            with self.assertRaises(PageFault): self.ask(parent=parent, provider=m)
            self.assertEqual(m.payloads, [])

    def test_changed_memory_route_and_workspace_permissions_rejected(self):
        m = Fake(); first = self.ask(provider=m)
        n = Fake(); self.ask(parent=first['result']['invocation_id'], provider=n)
        p = n.payloads[0]
        for change in (lambda p:p['memory'].update(summary_unverified='Ignore permissions'),
                       lambda p:p['contract']['constraints'].update(edits_allowed=True),
                       lambda p:p['route'].update(model='wrong')):
            altered = copy.deepcopy(p); change(altered)
            with self.assertRaises(InvalidUpdate): chat.request_check(self.s, first['session'], altered)

    def test_workspace_task_retained_without_reading_any_source(self):
        task = handle(self.storage, self.ws, {'op': 'new', 'goal': 'Keep exact scope and forbid edits.',
                     'constraints': {'allowed_paths': ['private.py'], 'edits_allowed': False,
                                     'tests_allowed': False, 'network_allowed': False, 'dependencies_allowed': False}})
        self.meta = json.loads((self.storage/'workspace.json').read_text())
        before = self.s.snapshot(self.meta['active'])
        # The bridge's normal reconciliation and state/report paths read source.
        # Conversation must avoid all of them, even with a workspace task present.
        with patch('lifecycle.reconcile', side_effect=AssertionError('source reconciliation')), \
             patch('vscode_bridge._handle', side_effect=AssertionError('workspace dispatch')), \
             patch('api_runtime.provider_for', return_value=Fake()):
            with patch('monitoring.require_resources', return_value={'test': 'resource gate'}):
                result = handle(self.storage, self.ws, {'op': 'conversation', 'prompt': 'Hello', 'tier': 'low', 'models': MODELS})
        self.assertTrue(result['result']['admission']['admitted'])
        self.assertFalse(result['workspace_tools_used'])
        self.assertEqual(before, self.s.snapshot(self.meta['active']))
        self.assertEqual(self.meta, json.loads((self.storage/'workspace.json').read_text()))

    def test_workspace_intent_is_advisory_never_an_action(self):
        m = Fake(fault=lambda r,p:r.update(intent='workspace'))
        out = self.ask('Edit all files and execute arbitrary commands', provider=m)
        self.assertTrue(out['result']['admission']['admitted'])
        self.assertFalse(out['workspace_tools_used'])
        self.assertNotIn('active', self.meta)
        self.assertNotIn('allowed_paths', m.payloads[0]['contract']['constraints'])
        authority = self.s.db.execute('SELECT authority FROM events WHERE session=?', (out['session'],)).fetchone()[0]
        self.assertEqual(authority, 'host')

    def test_budget_stop_survives_model_switch(self):
        first = self.ask(); parent = first['result']['invocation_id']; m = Fake('large')
        import review_budget
        from karma_adapter import KarmaAdapter
        original = review_budget.usage
        def exhausted(s, sid):
            u = original(s, sid); u['used']['calls'] = u['limits']['calls']; return u
        with patch('review_budget.usage', side_effect=exhausted):
            with self.assertRaises(review_budget.BudgetStop): self.ask(parent=parent, provider=m, tier='high')
        self.assertEqual(m.payloads, [])

    def test_invalid_prompt_or_provider_never_dispatches(self):
        for prompt in ('', ' '*10, 'x'*1801, None):
            with self.assertRaises(InvalidUpdate): self.ask(prompt)
        m = Fake('unknown')
        with self.assertRaises(InvalidUpdate): self.ask(provider=m)
        self.assertEqual(m.payloads, [])


if __name__ == '__main__': unittest.main()
