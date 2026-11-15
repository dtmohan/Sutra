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
        target = first['result']['invocation_id']
        def page(r, p):
            r['page_requests'] = [] if p.get('requested_memory_pages') else [target]
        m = Fake(fault=page)
        out = self.ask('Tell me about that earlier topic.', parent, m)
        self.assertTrue(out['result']['admission']['admitted'], out['result'])
        self.assertEqual(len(m.payloads), 2)
        self.assertIn(target, [p['id'] for p in m.payloads[0]['memory']['toc']])
        self.assertIn(target, [p['id'] for p in m.payloads[1]['memory']['retrieved_turns']])
        self.assertTrue(self.s.verify_invocations(first['session']))
        selection = m.payloads[1]['memory_selection']
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
