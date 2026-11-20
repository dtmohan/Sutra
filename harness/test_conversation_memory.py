import copy
import unittest
from unittest.mock import patch
import conversation as chat
import conversation_memory as cm
from context_manager import ContextBudgetExceeded, InvalidUpdate, canonical
from invocation_gateway import InvocationLedger
from model_context import allocation
from paging import Budget, PageFault
import test_conversation as fixture
from test_conversation import Fake


class MemoryTests(unittest.TestCase):
    setUp = fixture.ConversationTests.setUp
    tearDown = fixture.ConversationTests.tearDown
    ask = fixture.ConversationTests.ask

    def fill(self):
        first = self.ask('Project Orchid label is AZ-7786.', provider=Fake(fault=lambda r,p:r.update(facts=['Project Orchid label is AZ-7786.'])))
        parent = first['result']['invocation_id']
        for i in range(6):
            m = Fake(fault=lambda r,p:r.update(answer='A bounded response. ' * 200, summary='Current unrelated topic.'))
            parent = self.ask('Topic '+str(i)+' '+('filler ' * 120), parent, m)['result']['invocation_id']
        return first, parent

    def test_recent_verbatim_pins_survive_reload_model_switch_and_summary_omission(self):
        first = self.ask('Never change the STP kernel.\nThe launch code is Étoile-42.', provider=Fake(fault=lambda r,p:r.update(facts=['The launch code is Étoile-42.'], summary='Short note.')))
        parent = first['result']['invocation_id']
        second = self.ask('Actually use Étoile-43; keep the kernel unchanged.', parent)
        self.s.close(); self.s = InvocationLedger(self.storage/'state.sqlite')
        m = Fake('large'); m.budget = Budget(32768, 4096, 256)
        out = self.ask('What changed?', second['result']['invocation_id'], m, 'high')
        self.assertTrue(out['result']['admission']['admitted'])
        memory = m.payloads[0]['memory']
        self.assertIn('Never change the STP kernel.', canonical(memory['pinned_user_statements']))
        self.assertEqual(memory['recent_turns'][-1]['user'], 'Actually use Étoile-43; keep the kernel unchanged.')
        self.assertIn('The launch code is Étoile-42.', [q['quote'] for q in memory['pinned_user_statements']])
        self.assertEqual(memory['stored_turns'], 2)
        self.assertEqual(m.payloads[0]['contract']['constraints']['edits_allowed'], False)

    def test_older_facts_are_pinned_and_pages_retrieved_without_full_transcript(self):
        first, parent = self.fill()
        m = Fake(); out = self.ask('Which Orchid label did I specify?', parent, m)
        self.assertTrue(out['result']['admission']['admitted'])
        memory = m.payloads[0]['memory']
        self.assertTrue(memory['omitted_turns'] > 0)
        self.assertTrue(any(r['id'] == first['result']['invocation_id'] for r in memory['retrieved_turns']))
        self.assertIn('AZ-7786', canonical(memory['pinned_user_statements']))
        self.assertLess(len(memory['recent_turns']) + len(memory['retrieved_turns']), memory['stored_turns'])
        self.assertTrue(self.s.verify_invocations(first['session']))

    def test_workspace_memory_allocation_keeps_pins_latest_and_requested_pages(self):
        first, parent = self.fill(); sid = first['session']
        cm.materialize(self.s, sid, cm.history(self.s, sid, parent))
        schema = lambda p: {'type':'object'}
        budget = Budget(65536,8192,256)
        full = cm.working_set(self.s,sid,parent,'Topic',budget,{},'',schema)
        limited = cm.working_set(self.s,sid,parent,'Topic',budget,{},'',schema,extra_bytes=1500)
        self.assertEqual(limited['pinned_user_statements'],full['pinned_user_statements'])
        self.assertEqual(limited['recent_turns'][-1],full['recent_turns'][-1])
        self.assertLess(len(canonical(limited)),len(canonical(full)))
        self.assertEqual(limited['stored_turns'],full['stored_turns'])
        target = first['result']['invocation_id']
        requested = cm.working_set(self.s,sid,parent,'Topic',budget,{'requested_memory_pages':[target]},'',schema,extra_bytes=0)
        self.assertIn(target,[row['id'] for row in requested['retrieved_turns']])

    def test_model_can_page_in_from_toc_before_publication(self):
        first, parent = self.fill()
        target = None
        def page(r, p):
            nonlocal target
            if not p.get('requested_memory_pages'):target=p['memory']['toc'][-1]['id']
            r['page_requests'] = [] if p.get('requested_memory_pages') else [target]
        m = Fake(fault=page)
        out = self.ask('Tell me about that earlier topic.', parent, m)
        self.assertTrue(out['result']['admission']['admitted'], out['result'])
        self.assertEqual(len(m.payloads), 2)
        self.assertIn(target, [p['id'] for p in m.payloads[0]['memory']['toc']])
        self.assertIn(target, [p['id'] for p in m.payloads[1]['memory']['retrieved_turns']])
        self.assertTrue(self.s.verify_invocations(first['session']))
        selection = m.payloads[1]['routing_state']['previous']
        self.s.quarantine_invocation(first['session'], selection, 'Invalid selection')
        with self.assertRaises(PageFault): self.s.load_result(first['session'], out['result']['invocation_id'])

    def test_capacity_comes_from_model_metadata_and_runtime_allocation(self):
        b, info = allocation({'model_info': {'general.architecture':'qwen', 'qwen.context_length':131072}, 'parameters':'temperature 0\nnum_ctx 32768\n'})
        self.assertEqual(b.context_limit, 32768)
        self.assertEqual(info['model_context_tokens'], 131072)
        self.assertEqual(cm.summary_limit(b), 2048)
        small, _ = allocation({'model_info': {'llama.context_length':8192}})
        self.assertEqual(cm.summary_limit(small), 512)
        self.assertEqual(allocation({})[1]['source'], 'metadata_missing_conservative_fallback')
        self.assertEqual(allocation({'model_info': {'x.context_length':262144}})[0].context_limit, 65536)
        with self.assertRaises(InvalidUpdate): allocation({'model_info': {'x.context_length':1024}})

    def test_longer_working_note_is_accepted_within_current_output_allocation(self):
        m = Fake(fault=lambda r,p:r.update(summary='s'*1500))
        self.assertTrue(self.ask(provider=m)['result']['admission']['admitted'])
        small = Fake(fault=lambda r,p:r.update(summary='s'*1500)); small.budget=Budget(16384, 2048, 256)
        self.assertFalse(self.ask(provider=small)['result']['admission']['admitted'])

    def test_pinned_overflow_stops_before_inference_instead_of_dropping_constraints(self):
        first=self.ask('Never remove this constraint: '+('z'*1700))
        small=Fake(); small.budget=Budget(8192,1024,256)
        with self.assertRaises(ContextBudgetExceeded): self.ask('Continue', first['result']['invocation_id'], small)
        self.assertEqual(small.payloads, [])

    def test_empty_pin_history_reports_packet_shortage_instead_of_claiming_pins(self):
        first = self.ask('Hello')
        small = Fake(); small.budget = Budget(4096, 512, 256)
        before = self.s.db.execute('SELECT COUNT(*) FROM invocations').fetchone()[0]
        with self.assertRaises(cm.ConversationContextExceeded) as stopped:
            self.ask('Review this workspace.', first['result']['invocation_id'], small)
        self.assertEqual(stopped.exception.budget['pinned_statements'], 0)
        self.assertEqual(stopped.exception.budget['stage'], 'request_format')
        self.assertNotIn('pinned', str(stopped.exception).lower())
        self.assertGreater(stopped.exception.budget['required_context'], small.budget.context_limit)
        self.assertEqual(small.payloads, [])
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM invocations').fetchone()[0], before)

    def test_true_pin_overflow_remains_mandatory_and_has_exact_budget_observation(self):
        first = self.ask('Never remove this constraint: ' + ('z' * 1700))
        sid = first['session']; parent = first['result']['invocation_id']
        original = chat.contract(self.s, sid)
        with self.assertRaises(cm.ConversationContextExceeded) as stopped:
            cm.working_set(self.s, sid, parent, 'Continue', Budget(1024, 128, 0), {}, '', lambda _: {})
        self.assertEqual(stopped.exception.budget['stage'], 'pinned_statements')
        self.assertEqual(stopped.exception.budget['pinned_statements'], 1)
        self.assertEqual(stopped.exception.budget['required_context'],
                         stopped.exception.budget['required_input'] + 128)
        self.assertEqual(chat.contract(self.s, sid), original)

    def test_real_memory_capped_conversation_refits_once_before_inference(self):
        first = self.ask('Hello')
        model = Fake(); model.context_metadata = dict(model.context_metadata, _sutra_runtime_cap=4096)
        def refit(provider, root, observation):
            self.assertEqual(provider.payloads, [])
            self.assertEqual(observation['pinned_statements'], 0)
            provider.budget = Budget(24576, 512, 256)
            return {'effective_context_tokens': 24576, 'source': 'measured_test_preflight'}
        with patch('api_runtime.provider_for', return_value=model), \
                patch('runtime_memory.refit_required_context', side_effect=refit) as plan, \
                patch('monitoring.require_resources', return_value={'admitted': True}):
            out = chat.answer(self.s, self.meta, self.storage, {'prompt': 'Hello again',
                'parent': first['result']['invocation_id'], 'tier': 'low', 'models': fixture.MODELS})
        self.assertTrue(out['result']['admission']['admitted'])
        plan.assert_called_once()
        self.assertEqual(len(model.payloads), 1)
        self.assertEqual(model.payloads[0]['memory_allocation']['response_reserve'], 512)
        self.assertEqual(model.payloads[0]['memory_allocation']['context_limit'], 24576)
        self.assertEqual(model.payloads[0]['contract']['constraints']['network_allowed'], False)

    def test_fact_invention_and_out_of_ancestry_page_requests_rejected(self):
        for fault in (lambda r,p:r.update(facts=['UNSUPPORTED USER FACT']), lambda r,p:r.update(page_requests=['0'*32])):
            out=self.ask(provider=Fake(fault=fault))
            self.assertFalse(out['result']['admission']['admitted'])

    def test_legacy_policy_data_restored_without_restoring_old_action_authority(self):
        current=self.s.logic_hash;self.s.logic_hash=next(iter(cm.LEGACY_POLICIES))
        with patch('governance.policy_hash', return_value=self.s.logic_hash):
            first=self.ask('Remember exact legacy fact L-232.')
        self.s.logic_hash=current
        with self.assertRaises(PageFault): self.s.load_result(first['session'], first['result']['invocation_id'])
        m=Fake();out=self.ask('What was the fact?', first['result']['invocation_id'], m)
        self.assertTrue(out['result']['admission']['admitted'])
        self.assertTrue(m.payloads[0]['memory']['legacy_data_restored'])
        self.assertIn('L-232',canonical(m.payloads[0]['memory']))
        self.s.quarantine_invocation(first['session'], first['result']['invocation_id'], 'Rejected ancestor')
        with self.assertRaises(PageFault): self.ask(parent=out['result']['invocation_id'])

    def test_released_020_and_021_conversation_data_restores_without_execution_authority(self):
        current=self.s.logic_hash
        for version,previous in cm.RELEASE_POLICIES.items():
            with self.subTest(version=version):
                self.s.logic_hash=previous
                with patch('governance.policy_hash',return_value=previous):
                    first=self.ask('Remember the same-session request '+version)
                self.s.logic_hash=current
                with self.assertRaises(PageFault):
                    self.s.load_result(first['session'],first['result']['invocation_id'])
                m=Fake();out=self.ask('Continue the conversation',first['result']['invocation_id'],m)
                self.assertTrue(out['result']['admission']['admitted'])
                self.assertTrue(m.payloads[0]['memory']['legacy_data_restored'])
                self.assertIn(version,canonical(m.payloads[0]['memory']))
                self.assertEqual(out['session'],first['session'])

    def test_unknown_policy_is_not_implicitly_trusted_as_legacy(self):
        current=self.s.logic_hash;self.s.logic_hash='f'*64
        with patch('governance.policy_hash', return_value=self.s.logic_hash):
            first=self.ask()
        self.s.logic_hash=current
        with self.assertRaises(PageFault): self.ask(parent=first['result']['invocation_id'])

    def test_0502_data_restore_keeps_contract_and_spending_without_old_execution_authority(self):
        import review_budget
        previous=cm.RELEASE_POLICIES['0.50.2']
        self.assertEqual(previous,'3e145efb75250ece2e2a0360d253d9bba8326ce6d26b289a4747b71fffab23c6')
        current=self.s.logic_hash;self.s.logic_hash=previous
        with patch('governance.policy_hash',return_value=previous):
            first=self.ask('Never edit files. Remember retained request Q-502.')
        sid=first['session'];iid=first['result']['invocation_id']
        contract=self.s.current_contract(sid)
        spent=review_budget.usage(self.s,sid)['used']
        receipt=self.s.db.execute('SELECT record_json FROM invocation_receipts WHERE invocation_id=?',(iid,)).fetchone()[0]
        self.s.logic_hash=current
        with self.assertRaises(PageFault):self.s.load_result(sid,iid)
        stronger=Fake('large')
        out=self.ask('Continue that request',iid,stronger,'high')
        self.assertEqual(out['session'],sid)
        self.assertEqual(self.s.current_contract(sid),contract)
        self.assertEqual(self.s.db.execute('SELECT record_json FROM invocation_receipts WHERE invocation_id=?',(iid,)).fetchone()[0],receipt)
        memory=stronger.payloads[0]['memory']
        self.assertTrue(memory['legacy_data_restored'])
        self.assertIn('Q-502',canonical(memory))
        self.assertFalse(stronger.payloads[0]['contract']['constraints']['edits_allowed'])
        after=review_budget.usage(self.s,sid)['used']
        self.assertTrue(all(after[key]>=value for key,value in spent.items()))
        with self.assertRaises(PageFault):self.s.load_result(sid,iid)
