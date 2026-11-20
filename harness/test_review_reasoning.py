import copy
import json
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate
from local_model import OutputLimitExceeded
from model_activity import thinking_control
from reasoning_policy import check_provider
from review_reasoning import bind, repair
from review_budget import usage
from paging import Budget, PageFault
import chat_runtime as chat
import test_chat as fixture
from test_chat import Fake


class Exhausted(Fake):
    context_metadata = {'thinking': {'values': [False, True], 'default': True}}

    def __init__(self, failures=1, error=OutputLimitExceeded, change=None):
        super().__init__()
        self.failures, self.error, self.change = failures, error, change

    def call(self, cid, system, p, schema):
        answer, record = super().call(cid, system, p, schema)
        if len(self.payloads) <= self.failures:
            if self.change: self.change()
            exc = self.error('Incomplete generation: output allowance exhausted')
            exc.call_record = {'call_id': cid, 'ok': False,
                               'response': {'done': True, 'done_reason': 'length',
                                            'prompt_eval_count': 400,
                                            'eval_count': self.budget.response_reserve}}
            raise exc
        return answer, record


class StrataTimedOut(Exhausted):
    def call(self, cid, system, p, schema):
        try:
            return super().call(cid, system, p, schema)
        except TimeoutError as exc:
            exc.call_record = {'call_id': cid, 'ok': False, 'provider': 'strata'}
            raise


class ControlsTests(unittest.TestCase):
    def test_claim_evaluation_uses_supported_effort_with_answer_headroom(self):
        from review_reasoning import check
        for reserve, preference, expected in ((8192, 'adaptive', True), (2048, 'adaptive', False),
                                              (8192, 'off', False), (8192, 'model-default', None)):
            model = Exhausted()
            model.budget = Budget(32384, reserve, 256)
            p = {'review_protocol': 2, 'claims': {'overview': {}}, 'source_evidence': {'e1': {}}}
            bind(model, p, preference=preference, evaluating_claims=True)
            self.assertIs(model.thinking, expected)
            check(p)
            check_provider(model, p)
        p['reasoning']['step'] = 1
        p['reasoning']['preference'] = 'adaptive'
        p['reasoning']['requested'] = True
        p['claim_response_reserve'] = True
        with self.assertRaises(InvalidUpdate): check(p)

    def test_supported_adaptive_defaults_and_user_overrides(self):
        for values, expected in [([True], False), (['low', 'medium'], False),
                                  ([], None), (['unfamiliar'], None)]:
            info = {'thinking': {'values': values}}
            self.assertIs(thinking_control(info, 'adaptive'), expected)
        self.assertTrue(thinking_control({'thinking': {'values': [True]}}, 'on'))
        self.assertIsNone(thinking_control({}, 'model-default'))

    def test_receipt_plan_binds_transport_and_fresh_capabilities(self):
        model = Exhausted(); p = {}
        with patch.dict('os.environ', SUTRA_THINKING='adaptive'): bind(model, p)
        check_provider(model, p)
        model.thinking = True
        with self.assertRaises(InvalidUpdate): check_provider(model, p)
        model.thinking = False
        model.context_metadata = {'thinking': {'values': ['low', 'high']}}
        with self.assertRaises(InvalidUpdate): check_provider(model, p)

    def test_bounded_evidence_selection_keeps_reasoning_capacity_for_answer(self):
        model = Exhausted()
        with patch.dict('os.environ', SUTRA_THINKING='model-default'):
            lookup = {}
            bind(model, lookup, bounded_selection=True)
            self.assertIs(model.thinking, False)
            self.assertEqual(lookup['reasoning']['preference'], 'adaptive')
            review = {}
            bind(model, review)
            self.assertIsNone(model.thinking)
            self.assertEqual(review['reasoning']['preference'], 'model-default')
        model.context_metadata = {}
        with patch.dict('os.environ', SUTRA_THINKING='off'):
            bind(model, {}, bounded_selection=True)
            self.assertIs(model.thinking, False)

    def test_repair_cannot_create_attempts_or_override_explicit_controls(self):
        model = Exhausted(); result = {'failure_kind': 'output_limit'}
        for preference in ('off', 'on', 'model-default'):
            p = {}
            with patch.dict('os.environ', SUTRA_THINKING=preference): bind(model, p)
            self.assertIsNone(repair(result, p, False, True))
        with patch.dict('os.environ', SUTRA_THINKING='adaptive'): bind(model, p)
        self.assertIsNone(repair(result, p, False, False))
        self.assertIsNone(repair(result, p, True, True))
        self.assertIsNone(repair({'blocked': 'output_limit'}, p, False, True))
        self.assertTrue(all(len(e) <= 140 for e in repair(result, p, False, True)['errors']))


class ReviewRecoveryTests(unittest.TestCase):
    setUp = fixture.ChatTests.setUp
    tearDown = fixture.ChatTests.tearDown
    ask = fixture.ChatTests.ask

    def architecture(self, model, reviewer):
        return chat.answer(self.s, self.meta, self.ws, self.storage,
            {'prompt': 'Explain the architecture.', 'models': fixture.MODELS, 'tier': 'low'},
            provider=model, review_provider=reviewer)

    def test_adaptive_claim_output_limit_reuses_one_attempt_without_losing_evidence_or_spending(self):
        reviewer = Exhausted()
        reviewer.model = fixture.MODELS['mid']
        reviewer.budget = Budget(32384, 8192, 256)
        before = usage(self.s, self.sid)
        with patch.dict('os.environ', SUTRA_THINKING='adaptive'):
            out = self.architecture(Fake(), reviewer)
        self.assertTrue(out['result']['admission']['admitted'], out)
        first, retry = reviewer.payloads
        self.assertIs(first['reasoning']['requested'], True)
        self.assertIs(retry['reasoning']['requested'], False)
        for field in ('contract', 'claims', 'source_evidence', 'candidate_id', 'review_plan', 'review_page'):
            self.assertEqual(first[field], retry[field])
        calls = [c for c in out['result']['token_usage']['calls'] if c['stage'] == 'report_reviewer']
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]['status'], 'quarantined')
        self.assertEqual(calls[0]['output_tokens'], 8192)
        self.assertGreater(usage(self.s, self.sid)['used']['tokens'], before['used']['tokens'])
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_adaptive_report_writer_retries_at_lowest_effort_and_charges_truncation(self):
        writer = Exhausted()
        writer.budget = Budget(32384, 8192, 256)
        writer.context_metadata = {'thinking': {'values': [False, 'low', 'medium'], 'default': 'medium'}}
        reviewer = Exhausted(failures=0)
        reviewer.model = fixture.MODELS['mid']
        before = usage(self.s, self.sid)

        with patch.dict('os.environ', SUTRA_THINKING='adaptive'):
            out = self.architecture(writer, reviewer)

        self.assertTrue(out['result']['admission']['admitted'], out)
        self.assertTrue(out['report_markdown'])
        first, retry = writer.payloads
        self.assertEqual(set(first['excerpts']), {'entry.py', 'leaf.py'})
        self.assertEqual(
            [(p['reasoning']['preference'], p['reasoning']['step'], p['reasoning']['requested'])
             for p in (first, retry)],
            [('adaptive', 1, 'low'), ('adaptive', 0, False)])
        self.assertEqual(
            {k: v for k, v in first.items() if k != 'reasoning'},
            {k: v for k, v in retry.items() if k not in ('reasoning', 'repair')})
        self.assertTrue(reviewer.payloads)
        self.assertTrue(all(p['reasoning']['requested'] is False for p in reviewer.payloads))
        calls = out['result']['token_usage']['calls']
        generation = [c for c in calls if c['stage'] == 'chat_reviewer']
        self.assertEqual(len(generation), 2)
        failed = next(c for c in generation if c['status'] == 'quarantined')
        self.assertEqual(failed['output_tokens'], writer.budget.response_reserve)
        self.assertEqual(usage(self.s, self.sid)['used']['calls'] - before['used']['calls'], len(calls))
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_report_writer_keeps_low_effort_with_tight_output_or_explicit_off(self):
        for preference, reserve in (('adaptive', 2048), ('off', 8192)):
            with self.subTest(preference=preference, reserve=reserve):
                writer = Exhausted(failures=0)
                writer.budget = Budget(32384, reserve, 256)
                with patch.dict('os.environ', SUTRA_THINKING=preference):
                    out = self.architecture(writer, Fake())
                self.assertTrue(out['result']['admission']['admitted'], out)
                self.assertEqual(len(writer.payloads), 1)
                plan = writer.payloads[0]['reasoning']
                self.assertEqual(plan['preference'], preference)
                self.assertIs(plan['requested'], False)

    def test_adaptive_strata_synthesis_timeout_retries_off_and_retains_spent_budget(self):
        writer = StrataTimedOut(error=TimeoutError)
        writer.budget = Budget(32384, 8192, 256)
        writer.context_metadata = {'thinking': {'values': ['low', 'high'], 'default': 'high'}}
        before = usage(self.s, self.sid)['used']['tokens']
        with patch.dict('os.environ', SUTRA_THINKING='adaptive'):
            out = self.architecture(writer, Fake())
        self.assertTrue(out['result']['admission']['admitted'], out)
        first, retry = writer.payloads
        self.assertEqual(first['reasoning']['requested'], 'low')
        self.assertIs(retry['reasoning']['requested'], False)
        self.assertEqual(first['contract'], retry['contract'])
        self.assertEqual(first['excerpts'], retry['excerpts'])
        calls = out['result']['token_usage']['calls']
        failed = next(c for c in calls if c['status'] == 'quarantined')
        self.assertIsNone(failed['output_tokens'])
        reserved = 0
        for call in calls:
            raw, = self.s.db.execute('SELECT record_json FROM invocations WHERE id=?', (call['invocation_id'],)).fetchone()
            env = json.loads(raw)
            reserved += env['charge'] + env['budget_profile']['response_reserve']
        self.assertEqual(usage(self.s, self.sid)['used']['tokens'] - before, reserved)
        self.assertTrue(any(row['kind'] == 'BRIDGE' and row['data']['reason'] == 'bounded_timeout_repair'
                            for row in self.s.run(failed['run_id'])['events']))
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_recovery_then_model_switch_retains_pins_and_charges_failure(self):
        before = usage(self.s, self.sid)
        model = Exhausted()
        with patch.dict('os.environ', SUTRA_THINKING='adaptive'):
            out = self.ask(provider=model)
        self.assertTrue(out['result']['admission']['admitted'], out)
        self.assertEqual(len(model.payloads), 2)
        first, second = model.payloads
        self.assertEqual({k: v for k, v in second.items() if k != 'repair'}, first)
        self.assertIs(first['reasoning']['requested'], False)
        failed = self.s.db.execute("SELECT invocation_id FROM invocation_receipts WHERE json_extract(record_json,'$.status')='quarantined'").fetchone()[0]
        with self.assertRaises((InvalidUpdate, PageFault)): self.s.load_result(self.sid, failed)
        calls = out['result']['token_usage']['calls']
        self.assertEqual(len(calls), 2)
        self.assertEqual(next(c for c in calls if c['status']=='quarantined')['output_tokens'], model.budget.response_reserve)
        after = usage(self.s, self.sid)
        self.assertEqual(after['used']['calls'], before['used']['calls'] + 2)
        charge = self.s.db.execute('SELECT SUM(json_extract(record_json,\'$.charge\') + json_extract(record_json,\'$.budget_profile.response_reserve\')) FROM invocations WHERE run_id=?',
                                  (calls[0]['run_id'],)).fetchone()[0]
        self.assertEqual(after['used']['tokens'] - before['used']['tokens'], charge)
        other = Fake('fixture-large')
        switched = self.ask('Continue the source explanation.', tier='high', provider=other)
        self.assertTrue(switched['result']['admission']['admitted'])
        p = other.payloads[0]
        self.assertEqual(p['contract'], first['contract'])
        self.assertEqual(p['constraint_echo'], first['constraint_echo'])
        self.assertEqual(p['handoff']['invocation_id'], out['result']['invocation_id'])
        self.assertGreater(usage(self.s, self.sid)['used']['tokens'], after['used']['tokens'])
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_two_exhaustions_stop_without_publishing_or_third_call(self):
        model = Exhausted(failures=3)
        with patch.dict('os.environ', SUTRA_THINKING='adaptive'): out = self.ask(provider=model)
        self.assertFalse(out['result']['admission']['admitted'])
        self.assertEqual(len(model.payloads), 2)
        self.assertFalse(self.meta.get('chat', {}).get('checkpoint'))
        self.assertEqual(out['result']['failure_kind'], 'output_limit')

    def test_transport_errors_and_explicit_thinking_do_not_retry(self):
        for model, preference in [(Exhausted(error=RuntimeError), 'adaptive'), (Exhausted(), 'on')]:
            with patch.dict('os.environ', SUTRA_THINKING=preference): out = self.ask(provider=model)
            self.assertFalse(out['result']['admission']['admitted'])
            self.assertEqual(len(model.payloads), 1)

    def test_changed_source_after_exhaustion_stops_recovery(self):
        model = Exhausted(change=lambda: (self.ws/'entry.py').write_text('def changed(): return 2\n'))
        with patch.dict('os.environ', SUTRA_THINKING='adaptive'): out = self.ask(provider=model)
        self.assertFalse(out['result']['admission']['admitted'])
        self.assertEqual(len(model.payloads), 1)

    def test_final_allocated_attempt_cannot_spawn_recovery(self):
        fit = chat.fit_packet
        def one(*args): return fit(*args)[0], 1
        model = Exhausted()
        model.budget = Budget(32384, 8192, 256)
        with patch('chat_runtime.fit_packet', side_effect=one), patch.dict('os.environ', SUTRA_THINKING='adaptive'):
            out = self.architecture(model, Fake())
        self.assertFalse(out['result']['admission']['admitted'])
        self.assertEqual(len(model.payloads), 1)
        self.assertIs(model.payloads[0]['reasoning']['requested'], False)

    def test_independent_report_check_can_recover_without_publishing_partial_claims(self):
        reviewer = Exhausted()
        with patch.dict('os.environ', SUTRA_THINKING='adaptive'):
            out = chat.answer(self.s, self.meta, self.ws, self.storage,
                {'prompt': 'Explain the architecture.', 'models': fixture.MODELS, 'tier': 'low'},
                provider=Fake(), review_provider=reviewer)
        self.assertTrue(out['result']['admission']['admitted'], out)
        self.assertEqual(len(reviewer.payloads), 2)
        self.assertEqual(reviewer.payloads[0]['contract'], reviewer.payloads[1]['contract'])
        self.assertTrue(out['report_markdown'])
        self.assertTrue(self.s.verify_invocations(self.sid))


import test_orchestration as orchestration_fixture
import orchestration as machine


class BranchRecoveryTests(unittest.TestCase):
    setUp = orchestration_fixture.OrchestrationTests.setUp
    tearDown = orchestration_fixture.OrchestrationTests.tearDown
    provider = orchestration_fixture.OrchestrationTests.provider
    step = orchestration_fixture.OrchestrationTests.step
    until = orchestration_fixture.OrchestrationTests.until
    children = orchestration_fixture.OrchestrationTests.children

    def test_child_recovery_then_model_switch_preserves_parent_memory_and_budget(self):
        out = self.children()
        cid = out['children'][0]['id']
        parent = machine.node(self.s, cid)['parent_id']
        before = machine.spent(self.s, parent)
        original = self.provider
        class Branch(Exhausted):
            def call(self, cid, system, p, schema):
                result, record = super().call(cid, system, p, schema)
                result['branch_id'] = p['branch']['id']
                return result, record
        child = Branch(); child.model = orchestration_fixture.MODELS[self.tier]
        self.provider = lambda stage: child if stage == 'subagent' else original(stage)
        with patch.dict('os.environ', SUTRA_THINKING='adaptive'): out = self.step(out)
        self.assertEqual(machine.state(self.s, cid), 'DONE')
        self.assertEqual(len(child.payloads), 2)
        first, second = child.payloads
        self.assertEqual(first, {k: v for k, v in second.items() if k != 'repair'})
        self.assertIn('CED724', json.dumps(first))
        spent = machine.spent(self.s, parent)
        self.assertGreater(spent['tokens'], before['tokens'])
        self.provider = original; self.tier = 'high'
        final = self.until('DONE', out)
        self.assertTrue(final['data']['result']['admission']['admitted'])
        self.assertGreater(machine.spent(self.s, parent)['tokens'], spent['tokens'])
