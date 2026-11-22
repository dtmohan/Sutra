"""Exercise writer output exhaustion through the real gateway and task workflow."""

import copy
import json
import unittest
from unittest.mock import patch

import chat_runtime
import orchestration as machine
import review_budget
import test_orchestration as fixture
import test_parent_recovery as parent
import test_review_slice_recovery as slices
from budget_accounting import used as settled_tokens
from context_manager import InvalidUpdate, canonical
from local_model import OutputLimitExceeded
from paging import Budget


INCOMPLETE_TEXT = '{"answer":"UNVERIFIED_PARTIAL_WRITE'


class OutputModel(parent.ParentModel):
    def __init__(self, model, test):
        super().__init__(model)
        self.budget = Budget(32768, 8192, 256)
        self.context_metadata = {'thinking': {'values': list(test.controls)}}
        self.test = test
        self.review_fault = test.review_fault

    def call(self, cid, system, packet, schema):
        value, record = super().call(cid, system, packet, schema)
        if 'report' not in schema['properties']:
            return value, record
        state = self.test
        state.writer_calls.append((self.model, copy.deepcopy(packet), copy.deepcopy(schema)))
        if len(state.writer_calls) > state.failed_writer_calls:
            if state.invalid_compact:
                value['complete'] = True
            return value, record
        error = OutputLimitExceeded('Fixture stopped at its generation cap')
        response = {'done': True, 'done_reason': 'length',
                    'message': {'content': INCOMPLETE_TEXT}}
        if state.measured:
            response.update(prompt_eval_count=400, eval_count=self.budget.response_reserve)
        error.call_record = {
            'call_id': cid, 'ok': False, 'response': response,
            'transport': {'version': 1, 'phase': 'stream', 'headers_received': True,
                          'frames_received': 3, 'content_bytes': len(INCOMPLETE_TEXT),
                          'reasoning_bytes': 90},
        }
        raise error


class ParentOutputPolicyTests(unittest.TestCase):
    tearDown = fixture.OrchestrationTests.tearDown
    step = fixture.OrchestrationTests.step
    until = fixture.OrchestrationTests.until
    rows = parent.ParentRecoveryTests.rows
    reload = slices.ReviewSliceTests.reload
    role_rows = slices.ReviewSliceTests.role_rows

    def setUp(self):
        parent.ParentRecoveryTests.setUp(self)
        self.writer_calls = []
        self.controls = [False, True]
        self.failed_writer_calls = 1
        self.measured = True
        self.invalid_compact = False
        self.review_fault = None

    def provider(self, stage):
        if stage != 'synthesis':
            return fixture.OrchestrationTests.provider(self, stage)
        model = OutputModel(fixture.MODELS[self.tier], self)
        self.models.append((stage, model))
        return model

    def first_stop(self):
        ready = self.until('SYNTHESIS')
        self.assertEqual([child['state'] for child in ready['children']], ['DONE', 'DONE'])
        sid = self.meta['active']
        before = review_budget.usage(self.s, sid)
        pending = self.step(ready)
        self.assertEqual(pending['state'], 'SYNTHESIS')
        self.assertNotIn('data', pending)
        self.assertEqual(machine.events(self.s, pending['flow_id'])[-1]['kind'], 'PARENT_GENERATION_CHECKPOINT')
        [(iid, env, saved)] = self.role_rows(sid, 'chat_reviewer')
        self.assertEqual(saved['failure_kind'], 'output_limit')
        self.assertEqual(saved['status'], 'quarantined')
        self.assertIsNone(saved['proposal'])
        self.assertEqual(saved['token_usage']['output_tokens'],
                         env['budget_profile']['response_reserve'] if self.measured else None)
        after = review_budget.usage(self.s, sid)
        self.assertEqual(after['used']['calls'] - before['used']['calls'], 1)
        self.assertEqual(after['used']['tokens'] - before['used']['tokens'], settled_tokens(env, saved))
        self.assertEqual(after['limits'], before['limits'])
        self.assertEqual(self.role_rows(sid, 'report_reviewer'), [])
        return pending, sid, iid

    def assert_compact(self, *, first_control, compact_control):
        (first_model, first, first_schema), (second_model, second, second_schema) = self.writer_calls
        self.assertEqual(first['reasoning']['requested'], first_control)
        self.assertEqual(second['reasoning']['requested'], compact_control)
        self.assertEqual(first['parent_output_policy_version'], 1)
        self.assertNotIn('parent_output_compact', first)
        self.assertEqual(second['parent_output_compact'],
                         self.role_rows(self.meta['active'], 'chat_reviewer')[0][0])
        for same in ('request', 'contract', 'constraint_echo', '_request_nonce', 'parent_repair',
                     'snapshot_receipts', 'snapshot_owner'):
            self.assertEqual(first[same], second[same], same)
        self.assertIn('CED724', canonical(second['memory']))
        self.assertEqual([row['invocation_id'] for row in first['child_results']],
                         [row['invocation_id'] for row in second['child_results']])
        old = first_schema['properties']['report']['properties']
        new = second_schema['properties']['report']['properties']
        self.assertLess(new['overview']['maxLength'], old['overview']['maxLength'])
        self.assertTrue(new['components']['maxItems'] >= new['components']['minItems'])
        self.assertTrue(new['flow']['maxItems'] >= new['flow']['minItems'])
        return first_model, second_model

    def test_mandatory_or_unknown_reasoning_recovers_after_reload_and_model_switch(self):
        self.controls = []
        with patch.dict('os.environ', SUTRA_THINKING='adaptive'):
            pending, sid, failed_id = self.first_stop()
            prior = self.rows(sid)
            contract = self.s.pinned(sid)
            permissions = machine.node(self.s, pending['execution_id'])['permissions']
            charged = review_budget.usage(self.s, sid)
            self.reload()
            self.tier = 'high'
            self.assertEqual(review_budget.usage(self.s, sid), charged)
            draft = self.step(pending)
            self.assertEqual(draft['state'], 'SYNTHESIS')
            self.assertNotIn('data', draft)
            self.assertEqual(self.role_rows(sid, 'report_reviewer'), [])
            done = self.until('DONE', draft)
        self.assertEqual(self.assert_compact(first_control=None, compact_control=None),
                         (fixture.MODELS['low'], fixture.MODELS['high']))
        self.assertEqual(self.rows(sid)[:len(prior)], prior)
        self.assertEqual(self.s.pinned(sid), contract)
        self.assertEqual(machine.node(self.s, done['execution_id'])['permissions'], permissions)
        self.assertFalse(permissions['network_allowed'])
        self.assertFalse(permissions['write_paths'])
        self.assertTrue(done['data']['result']['admission']['admitted'],
                        done['data']['result']['admission']['errors'])
        self.assertTrue(done['data']['report_markdown'])
        self.assertTrue(self.role_rows(sid, 'report_reviewer'))
        self.assertNotEqual(done['data']['result']['invocation_id'], failed_id)
        self.assertNotIn(INCOMPLETE_TEXT, canonical(done['data']))
        after = review_budget.usage(self.s, sid)
        self.assertEqual(after['limits'], charged['limits'])
        self.assertGreater(after['used']['tokens'], charged['used']['tokens'])
        for counter in ('operations', 'source_bytes'):
            self.assertEqual(after['used'][counter], charged['used'][counter])
        self.assertTrue(self.s.verify_invocations(sid))

    def test_optional_adaptive_thinking_lowers_for_compact_writer_only(self):
        with patch.dict('os.environ', SUTRA_THINKING='adaptive'):
            pending, sid, _ = self.first_stop()
            done = self.until('DONE', pending)
        self.assert_compact(first_control=True, compact_control=False)
        self.assertTrue(done['data']['result']['admission']['admitted'],
                        done['data']['result']['admission']['errors'])
        self.assertTrue(self.s.verify_invocations(sid))

    def test_persistent_limit_keeps_explicit_on_and_reports_no_unverified_result(self):
        self.failed_writer_calls = 3
        self.measured = False
        with patch.dict('os.environ', SUTRA_THINKING='on'):
            pending, sid, _ = self.first_stop()
            done = self.until('DONE', pending)
        self.assert_compact(first_control=True, compact_control=True)
        data = done['data']
        self.assertFalse(data['result']['admission']['admitted'])
        self.assertIsNone(data['result']['accepted_output'])
        self.assertIsNone(data['report_markdown'])
        self.assertNotIn(INCOMPLETE_TEXT, canonical(data))
        self.assertEqual(self.role_rows(sid, 'report_reviewer'), [])
        stop = data['output_stop']
        self.assertEqual(stop['retry_reason'], 'compact_attempt_used')
        self.assertEqual(stop['compact_failure'], 'output_limit')
        self.assertIsNone(stop['output_tokens'])
        self.assertIsNone(stop['reasoning_tokens'])
        self.assertEqual(stop['attempt_count'], 2)
        self.assertEqual(stop['evidence']['child_reports'], 2)
        self.assertEqual(stop['evidence']['mapped_files'], 4)
        self.assertGreater(stop['evidence']['source_files_sampled'], 0)
        rows = self.role_rows(sid, 'chat_reviewer')
        self.assertEqual([receipt['status'] for _, _, receipt in rows], ['quarantined', 'quarantined'])
        self.assertEqual(stop['output_reserve'], rows[-1][1]['budget_profile']['response_reserve'])
        charged = review_budget.usage(self.s, sid)
        self.step(done)
        self.assertEqual(review_budget.usage(self.s, sid), charged)
        self.assertTrue(self.s.verify_invocations(sid))

    def test_no_original_retry_capacity_explains_measured_limit(self):
        ready = self.until('SYNTHESIS')
        sid = self.meta['active']
        original = chat_runtime.fit_packet

        def one_attempt(*args, **kwargs):
            schema, _ = original(*args, **kwargs)
            return schema, 1

        with patch('chat_runtime.fit_packet', side_effect=one_attempt):
            done = self.step(ready)
        self.assertEqual(done['state'], 'DONE')
        self.assertFalse(done['data']['result']['admission']['admitted'])
        stop = done['data']['output_stop']
        [(iid, env, saved)] = self.role_rows(sid, 'chat_reviewer')
        self.assertEqual(stop['retry_reason'], 'attempt_limit')
        self.assertEqual(stop['reason'], 'output_limit')
        self.assertEqual(stop['attempt_count'], 1)
        self.assertEqual(stop['attempt_limit'], 1)
        self.assertEqual(stop['model'], env['model'])
        self.assertEqual(stop['output_tokens'], saved['token_usage']['output_tokens'])
        self.assertEqual(stop['answer_bytes'], len(INCOMPLETE_TEXT))
        self.assertEqual(stop['reasoning_bytes'], 90)
        self.assertEqual(saved['token_usage']['output_tokens'], env['budget_profile']['response_reserve'])
        self.assertEqual(saved['transport_failure']['content_bytes'], len(INCOMPLETE_TEXT))
        self.assertIsNone(saved['proposal'])
        self.assertEqual(done['data']['result']['invocation_id'], iid)
        self.assertTrue(self.s.verify_invocations(sid))

    def test_invalid_compact_output_does_not_reach_claim_review_or_retry_again(self):
        self.invalid_compact = True
        pending, sid, _ = self.first_stop()
        done = self.until('DONE', pending)
        data = done['data']
        self.assertFalse(data['result']['admission']['admitted'])
        self.assertEqual(data['output_stop']['retry_reason'], 'compact_attempt_used')
        self.assertEqual(data['output_stop']['compact_failure'], 'report_rejected')
        self.assertIsNone(data['result']['accepted_output'])
        self.assertIsNone(data['report_markdown'])
        self.assertEqual(self.role_rows(sid, 'report_reviewer'), [])
        self.assertEqual([receipt['status'] for _, _, receipt in self.role_rows(sid, 'chat_reviewer')],
                         ['quarantined', 'quarantined'])
        self.assertTrue(self.s.verify_invocations(sid))

    def test_claim_reviewer_can_omit_a_compact_writers_conclusion(self):
        def contradict_overview(result, packet):
            claim = result['claims'].get('overview')
            if claim and claim['evidence_ids']:
                claim.update(verdict='contradicted', reason='The delivered evidence contradicts this conclusion.')

        self.review_fault = contradict_overview
        pending, sid, _ = self.first_stop()
        done = self.until('DONE', pending)
        data = done['data']
        self.assertTrue(data['result']['admission']['admitted'], data['result']['admission']['errors'])
        self.assertEqual(data['report_review']['status'], 'reviewed_with_omissions')
        self.assertIn({'claim': 'overview', 'verdict': 'contradicted',
                       'reason': 'The delivered evidence contradicts this conclusion.'},
                      data['report_review']['omitted'])
        self.assertNotIn('The entry function calls the leaf implementation.', data['report_markdown'])
        self.assertTrue(self.s.verify_invocations(sid))

    def test_reload_after_paid_compact_failure_recovers_terminal_stop_without_dispatch(self):
        self.failed_writer_calls = 3
        pending, sid, _ = self.first_stop()
        with patch('chat_runtime._finish_answer', side_effect=RuntimeError('Fixture exited after receipt')):
            with self.assertRaisesRegex(RuntimeError, 'Fixture exited after receipt'):
                self.step(pending)
        original = self.rows(sid)
        charged = review_budget.usage(self.s, sid)
        self.reload()
        done = self.step(pending)
        self.assertEqual(done['state'], 'DONE')
        self.assertEqual(done['data']['output_stop']['retry_reason'], 'compact_attempt_used')
        self.assertEqual(done['data']['output_stop']['compact_failure'], 'output_limit')
        self.assertEqual(self.rows(sid), original)
        self.assertEqual(review_budget.usage(self.s, sid), charged)
        self.assertEqual(len(self.writer_calls), 2)
        self.assertTrue(self.s.verify_invocations(sid))

    def test_permission_and_source_freshness_still_gate_compact_dispatch(self):
        pending, sid, _ = self.first_stop()
        charged = review_budget.usage(self.s, sid)
        with self.assertRaises(InvalidUpdate):
            self.step(pending, armed=False)
        self.assertEqual(review_budget.usage(self.s, sid), charged)
        (self.ws / 'part0.py').write_text('def changed_after_checkpoint(): return 2\n')
        stopped = self.step(pending)
        self.assertEqual(stopped['state'], 'PAUSED')
        self.assertEqual(stopped['stop']['status'], 'provenance_changed')
        self.assertEqual(len(self.writer_calls), 1)
        self.assertEqual(len(self.role_rows(sid, 'chat_reviewer')), 1)
        self.assertTrue(self.s.verify_invocations(sid))


if __name__ == '__main__':
    unittest.main()
