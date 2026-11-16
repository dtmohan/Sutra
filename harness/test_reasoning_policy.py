import copy
import json
import unittest
from unittest.mock import patch

import conversation as chat
import conversation_memory as cm
import orchestration_flow as flow
import reasoning_policy as policy
from context_manager import InvalidUpdate, ContextBudgetExceeded, canonical
from conversation_routing import RoutingBlocked
from invocation_gateway import InvocationLedger, InvocationGateway
from monitoring import ResourceWait
from paging import Budget, PageFault
from review_budget import usage, BudgetStop
import test_conversation as fixture
from test_conversation import Fake, MODELS
from test_conversation_routing import escalate
import test_conversation_memory as memory_fixture


def model(name='small', values=(False, True), fault=escalate):
    result = Fake(name, fault)
    result.context_metadata = dict(result.context_metadata,
                                   thinking={'values': list(values), 'default': values[-1] if values else None})
    return result


class ControlsTests(unittest.TestCase):
    def test_only_documented_distinct_controls_form_the_ladder(self):
        for values, expected in [([False, True], [False, True]), ([True], [False, True]),
                ([False], [False]), ([], [None]), (['unknown'], [None]),
                ([False, 'xhigh', 'low', 'medium'], [False, 'low', 'medium', 'xhigh']),
                (['medium', 'low'], [False, 'low', 'medium']), ([True, 'low'], [None]),
                ([0, 1], [None])]:
            self.assertEqual(policy.ladder(policy.capabilities({'thinking': {'values': values}})), expected)

    def test_static_overrides_never_gain_an_automatic_reasoning_retry(self):
        cap = policy.capabilities({'thinking': {'values': [False, 'low', 'medium']}})
        for preference, expected in [('off', False), ('on', 'low'), ('model-default', None)]:
            p = policy.make(cap, preference)
            self.assertEqual(p['requested'], expected)
            self.assertIsNone(policy.next_control(p))
        with self.assertRaises(InvalidUpdate): policy.make(cap, 'unlimited')
        for requested in (0, 1, [], {}):
            p = policy.make(cap, 'adaptive'); p['requested'] = requested
            with self.assertRaises(InvalidUpdate): policy.validate(p)


class ReasoningTests(unittest.TestCase):
    setUp = fixture.ConversationTests.setUp
    tearDown = fixture.ConversationTests.tearDown
    ask = fixture.ConversationTests.ask

    def route(self, providers, **extra):
        with patch.dict('os.environ', SUTRA_THINKING='adaptive'):
            return chat.answer(self.s, self.meta, self.storage,
                               dict(prompt='Recall the earlier detail.', models=MODELS, tier='auto', **extra), providers)

    def test_simple_answer_uses_one_minimal_call_and_no_workspace_permission(self):
        small = model(fault=None)
        out = self.route(small)
        self.assertTrue(out['result']['admission']['admitted'])
        self.assertEqual([p['reasoning']['requested'] for p in small.payloads], [False])
        self.assertEqual(usage(self.s, out['session'])['used']['calls'], 1)
        self.assertNotIn('active', self.meta)

    def test_page_then_reason_then_switch_preserves_memory_contract_and_budget(self):
        first, parent = memory_fixture.MemoryTests.fill(self)
        target = []
        def page_first(r, p):
            escalate(r, p)
            if not p['requested_memory_pages']:
                target.append(p['memory']['toc'][-1]['id'])
                r['page_requests'] = [target[0]]
        small, medium = model(fault=page_first), model('medium', [False, 'low'], fault=None)
        spent = usage(self.s, first['session'])['used']
        out = self.route({'small': small, 'medium': medium}, parent=parent)
        self.assertTrue(out['result']['admission']['admitted'], out['result'])
        self.assertEqual([p['reasoning']['requested'] for p in small.payloads], [False, False, True])
        self.assertEqual([p['reasoning']['requested'] for p in medium.payloads], [False])
        self.assertEqual(usage(self.s, first['session'])['used']['calls'], spent['calls'] + 4)
        baseline = small.payloads[0]
        for p in small.payloads[1:] + medium.payloads:
            for key in ('request', 'contract', 'context_binding', 'workspace_context'):
                self.assertEqual(p[key], baseline[key])
            self.assertEqual(p['memory']['pinned_user_statements'], baseline['memory']['pinned_user_statements'])
            self.assertEqual(p['memory']['recent_turns'][-1], baseline['memory']['recent_turns'][-1])
            self.assertIn(target[0], [r['id'] for r in p['memory']['retrieved_turns']])
            self.assertFalse(p['contract']['constraints']['edits_allowed'])
            self.assertFalse(p['contract']['constraints']['network_allowed'])
        self.assertEqual(len(cm.history(self.s, out['session'], out['result']['invocation_id'])), 8)
        self.s.quarantine_invocation(out['session'], small.payloads[2]['routing_state']['previous'], 'fixture invalid ancestor')
        with self.assertRaises(PageFault): cm.history(self.s, out['session'], out['result']['invocation_id'])

    def test_named_effort_steps_precede_model_switch(self):
        small = model(values=[False, 'low', 'medium', 'xhigh'])
        medium = model('medium', [True], fault=None)
        out = self.route({'small': small, 'medium': medium})
        self.assertEqual([p['reasoning']['requested'] for p in small.payloads], [False, 'low', 'medium', 'xhigh'])
        self.assertEqual(out['route']['model'], 'medium')
        self.assertEqual(usage(self.s, out['session'])['used']['calls'], 5)

    def test_context_discovered_after_reasoning_keeps_the_higher_effort(self):
        first, parent = memory_fixture.MemoryTests.fill(self)
        def respond(r, p):
            if p['reasoning']['requested'] is False:
                escalate(r, p)
            elif not p['requested_memory_pages']:
                r['page_requests'] = [p['memory']['toc'][-1]['id']]
        small = model(fault=respond)
        out = self.route(small, parent=parent)
        self.assertTrue(out['result']['admission']['admitted'])
        self.assertEqual([p['reasoning']['requested'] for p in small.payloads], [False, True, True])
        self.assertEqual(out['route']['model'], 'small')

    def test_saved_tier_cannot_silently_replace_model_during_reasoning_retry(self):
        small = model()
        one = self.route(small, one_step=True)
        sid = one['session']; spent = usage(self.s, sid)['used']
        with patch.dict('os.environ', SUTRA_THINKING='adaptive'), self.assertRaises(RoutingBlocked):
            chat.answer(self.s, self.meta, self.storage,
                        dict(prompt='Recall the earlier detail.', models=dict(MODELS, low='medium'), tier='auto',
                             conversation_session=sid, routing_continuation=one['result']['invocation_id']),
                        {'medium': model('medium', fault=None)})
        self.assertEqual(usage(self.s, sid)['used'], spent)

    def test_single_effort_model_does_not_repeat_equivalent_settings(self):
        small, medium = model(values=[False]), model('medium', fault=None)
        out = self.route({'small': small, 'medium': medium})
        self.assertEqual(len(small.payloads), 1)
        self.assertEqual(out['route']['model'], 'medium')

    def test_sparse_boolean_metadata_can_enable_advertised_thinking_before_switch(self):
        small, medium = model(values=[True]), model('medium', fault=None)
        out = self.route({'small': small, 'medium': medium})
        self.assertEqual([p['reasoning']['requested'] for p in small.payloads], [False, True])
        self.assertEqual(out['route']['model'], 'medium')

    def test_explicit_model_can_reason_but_does_not_switch(self):
        small = model()
        with patch.dict('os.environ', SUTRA_THINKING='adaptive'):
            out = self.ask(provider=small)
        self.assertEqual([p['reasoning']['requested'] for p in small.payloads], [False, True])
        self.assertFalse(out['result']['admission']['admitted'])
        self.assertEqual(out['route']['tier'], 'low')

    def test_unsupported_control_or_transport_mismatch_cannot_dispatch(self):
        small = model(fault=None)
        out = self.route(small)
        p = small.payloads[0]
        bad = copy.deepcopy(p); bad['reasoning']['requested'] = 'xhigh'
        with self.assertRaises(InvalidUpdate): chat.request_check(self.s, out['session'], bad)
        small.thinking = True
        with self.assertRaisesRegex(InvalidUpdate, 'Provider reasoning'):
            InvocationGateway(self.s.path, 'unused', small).invoke('conversation', 'unused', chat.SYSTEM, p, chat.schema(p), 'unused')
        small.thinking = False
        small.context_metadata['thinking']['values'] = [True]
        with self.assertRaisesRegex(InvalidUpdate, 'capabilities changed'):
            policy.check_provider(small, p)

    def test_budget_and_context_failure_do_not_charge_or_drop_constraints(self):
        for failure in ('budget', 'context'):
            small = model()
            with patch.dict(chat.CONSTRAINTS, review_calls=1 if failure == 'budget' else 40):
                one = self.route(small, one_step=True)
                sid = one['session']; spent = usage(self.s, sid)['used']
                if failure == 'context': small.budget = Budget(2048, 512, 128)
                with self.assertRaises(BudgetStop if failure == 'budget' else ContextBudgetExceeded):
                    self.route(small, conversation_session=sid, routing_continuation=one['result']['invocation_id'])
                self.assertEqual(usage(self.s, sid)['used'], spent)
                self.assertEqual(len(small.payloads), 1)

    def test_resume_does_not_reset_effort_when_settings_or_metadata_change(self):
        small = model()
        one = self.route(small, one_step=True)
        sid = one['session']; spent = usage(self.s, sid)['used']
        req = dict(prompt='Recall the earlier detail.', models=MODELS, tier='auto',
                   conversation_session=sid, routing_continuation=one['result']['invocation_id'])
        with patch.dict('os.environ', SUTRA_THINKING='off'), self.assertRaises(RoutingBlocked):
            chat.answer(self.s, self.meta, self.storage, req, small)
        small.context_metadata['thinking']['values'] = [True]
        with self.assertRaises(RoutingBlocked):
            self.route(small, conversation_session=sid, routing_continuation=one['result']['invocation_id'])
        self.assertEqual(usage(self.s, sid)['used'], spent)

    def test_resource_pause_reload_reasoning_then_model_switch(self):
        small, medium = model(), model('medium', fault=None)
        providers = lambda stage: {'small': small, 'medium': medium}
        req = dict(event='start', prompt='Explain this problem.', models=MODELS, tier='auto', armed=False)
        def step(out, event='step'):
            return flow.handle(self.s, self.meta, self.ws, self.storage,
                               dict(req, event=event, flow_id=out['flow_id'], cursor=out['cursor']), providers)
        with patch.dict('os.environ', SUTRA_THINKING='adaptive'):
            one = flow.handle(self.s, self.meta, self.ws, self.storage, req, providers)
            self.assertEqual(one['routing_step']['routing_followup']['kind'], 'reasoning')
            sid = one['routing_step']['session']; spent = usage(self.s, sid)['used']
            contract = chat.contract(self.s, sid)
            with patch('conversation.answer', side_effect=ResourceWait({'admitted': False, 'reason': 'fixture pressure'})):
                paused = step(one)
            self.assertEqual(paused['state'], 'PAUSED')
            self.assertEqual(usage(self.s, sid)['used'], spent)
            self.s.close(); self.s = InvocationLedger(self.storage/'state.sqlite')
            with patch('monitoring.resource_state', return_value={'admitted': True}): ready = step(paused, 'resume')
            second = step(ready)
            self.assertEqual(second['routing_step']['routing_followup']['kind'], 'escalate')
            done = step(second)
            self.assertEqual(done['state'], 'DONE')
            self.assertEqual([p['reasoning']['requested'] for p in small.payloads], [False, True])
            self.assertEqual(len(medium.payloads), 1)
            self.assertEqual(chat.contract(self.s, sid), contract)
            self.assertEqual(usage(self.s, sid)['used']['calls'], spent['calls'] + 2)


if __name__ == '__main__':
    unittest.main()
