"""Claim transport timeouts retain the draft, plan, charges and retry ceiling.

The real LocalModel 150-second check runs against a simulated clock and HTTP
transport. No inference, runtime activation or user ledger is involved.
"""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import claim_pages
import chat_runtime
import local_model
import orchestration as machine
import orchestration_flow as flow
import report_review
import review_budget
import test_orchestration as fixture
from budget_accounting import used as settled_tokens
from context_manager import InvalidUpdate, canonical
from invocation_gateway import InvocationLedger
from model_packet import build
from paging import Budget
from test_model_packet import WireModel


class TimedReviewer(WireModel):
    context_metadata = {'thinking': {'values': [False, True], 'default': True}}

    def __init__(self, model, root, timeout=True):
        super().__init__(model)
        self.budget = Budget(65536, 8192, 256)
        self.log_dir = Path(root) / 'simulated-model'
        self.port = 19400
        self.health = None
        self.timeout = timeout
        self.http_timeouts = []

    def call(self, cid, system, packet, schema):
        if packet.get('review_protocol') != 2 or not self.timeout:
            return super().call(cid, system, packet, schema)
        self.payloads.append(copy.deepcopy(packet))
        connection = SimpleNamespace(sock=None, connect=lambda: None, request=lambda *a, **kw: None,
            getresponse=lambda: SimpleNamespace(status=200), close=lambda: None)
        def http(*args, **kwargs):
            self.http_timeouts.append(kwargs['timeout'])
            return connection
        ticks = iter((0, 151, 151))
        clock = SimpleNamespace(monotonic=lambda: next(ticks))
        with patch('local_model.http.client.HTTPConnection', side_effect=http), \
                patch.object(local_model, 'time', clock), \
                patch('invocation_gateway.time', clock), \
                patch('storage_policy.write_trace'), patch('logit_sensor.Sensor', side_effect=RuntimeError('disabled fixture sensor')):
            return local_model.LocalModel.call(self, cid, system, packet, schema)


class ClaimTimeoutTests(unittest.TestCase):
    tearDown = fixture.OrchestrationTests.tearDown
    step = fixture.OrchestrationTests.step
    until = fixture.OrchestrationTests.until

    def setUp(self):
        fixture.OrchestrationTests.setUp(self)
        self.prompt = 'Explain the architecture and request flow. ' + self.prompt
        self.timeout = True
        self.reviewers = []

    def provider(self, stage):
        if stage != 'synthesis':
            return fixture.OrchestrationTests.provider(self, stage)
        model = TimedReviewer(fixture.MODELS[self.tier], self.root, self.timeout)
        self.reviewers.append(model); self.models.append((stage, model))
        return model

    def rows(self, sid):
        return self.s.db.execute('SELECT i.record_json,d.payload_json,r.record_json FROM invocations i '
            'JOIN deliveries d ON d.id=i.delivery_id JOIN invocation_receipts r ON r.invocation_id=i.id '
            'WHERE i.session=? ORDER BY i.rowid', (sid,)).fetchall()

    def claims(self, sid):
        return [(json.loads(e), json.loads(p), json.loads(r)) for e, p, r in self.rows(sid)
                if json.loads(e)['role'] == 'report_reviewer']

    def draft(self):
        ready = self.until('SYNTHESIS')
        saved = self.step(ready)
        self.assertEqual(saved['state'], 'SYNTHESIS')
        return saved

    def test_real_150_cap_unknown_charge_then_fresh_bridge_off_retry_after_reload_and_switch(self):
        with patch.dict(os.environ, SUTRA_THINKING='adaptive'):
            saved = self.draft(); sid = self.meta['active']; contract = self.s.pinned(sid)
            before = review_budget.usage(self.s, sid); rows = self.rows(sid)
            pending = self.step(saved)
            self.assertEqual(pending['state'], 'SYNTHESIS'); self.assertIsNone(pending['stop'])
            self.assertEqual(machine.events(self.s, pending['flow_id'])[-1]['kind'], 'CLAIM_REVIEW_CHECKPOINT')
            retry = flow.info(self.s, pending['flow_id'])[1]['claim_review_retry']
            self.assertEqual(retry['retry_mode'], 'automatic_off'); self.assertTrue(retry['automatic_retry'])
            [(env, packet, receipt)] = self.claims(sid)
            self.assertEqual(self.reviewers[-1].http_timeouts, [150])
            self.assertEqual(receipt['failure_kind'], 'timeout'); self.assertEqual(receipt['status'], 'quarantined')
            self.assertEqual(receipt['admission']['errors'], ['TimeoutError: Model transport deadline reached'])
            self.assertIsNone(receipt['token_usage']['input_tokens']); self.assertIsNone(receipt['token_usage']['output_tokens'])
            full = env['charge'] + env['budget_profile']['response_reserve']
            self.assertEqual(settled_tokens(env, receipt), full)
            after_timeout = review_budget.usage(self.s, sid)
            self.assertEqual(after_timeout['used']['tokens'] - before['used']['tokens'], full)
            self.assertEqual(self.rows(sid)[:len(rows)], rows)
            self.assertIs(packet['reasoning']['requested'], True)
            self.assertEqual(packet['reasoning']['preference'], 'adaptive')
            plan = claim_pages.find(self.s, sid, retry['candidate_id'])
            self.assertEqual(plan['attempts_per_page'], 2)
            self.assertEqual(len(claim_pages.attempts(self.s, sid, plan['id'], 0)), 1)
            old_rows = self.rows(sid)
            self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite')
            self.meta = json.loads((self.root / 'workspace.json').read_text()); self.tier = 'high'; self.timeout = False
            with patch('chat_runtime.answer', side_effect=AssertionError('draft replay')), \
                    patch('review_followup.expand', side_effect=AssertionError('source planner replay')):
                done = self.until('DONE', pending)
            self.assertTrue(done['data']['result']['admission']['admitted'])
            self.assertEqual(done['data']['result']['invocation_id'], retry['candidate_id'])
            self.assertEqual(self.rows(sid)[:len(old_rows)], old_rows)
            self.assertEqual(len(self.claims(sid)), 2)
            new, new_packet, new_receipt = self.claims(sid)[1]
            self.assertEqual(new['model'], fixture.MODELS['high']); self.assertEqual(new_receipt['status'], 'accepted')
            self.assertEqual(self.s.run(new['run_id'])['config']['max_calls'], 1)
            self.assertIs(new_packet['reasoning']['requested'], False)
            self.assertEqual(os.environ['SUTRA_THINKING'], 'adaptive')
            for key in ('candidate_id', 'review_plan', 'review_page', 'claims', 'source_evidence', 'contract', 'constraint_echo'):
                self.assertEqual(new_packet[key], packet[key], key)
            final = review_budget.usage(self.s, sid)
            self.assertEqual(final['limits'], before['limits']); self.assertEqual(final['used']['calls'], after_timeout['used']['calls'] + 1)
            self.assertEqual(self.s.pinned(sid), contract)
            self.assertFalse(contract['constraints']['edits_allowed']); self.assertFalse(contract['constraints']['network_allowed'])
            self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM invocations i LEFT JOIN invocation_receipts r '
                'ON r.invocation_id=i.id WHERE r.id IS NULL').fetchone()[0], 0)
            self.assertTrue(self.s.verify_invocations(sid)); self.assertTrue(self.s.audit_valid())

    def test_timeout_retry_exhaustion_cannot_dispatch_a_third_claim(self):
        with patch.dict(os.environ, SUTRA_THINKING='adaptive'):
            pending = self.step(self.draft()); sid = self.meta['active']
            retry = flow.info(self.s, pending['flow_id'])[1]['claim_review_retry']
            old = self.rows(sid); before = review_budget.usage(self.s, sid)
            done = self.step(pending)
            self.assertEqual(done['state'], 'DONE'); self.assertFalse(done['data']['result']['admission']['admitted'])
            self.assertEqual(len(self.claims(sid)), 2); self.assertEqual(self.rows(sid)[:len(old)], old)
            self.assertEqual(review_budget.usage(self.s, sid)['used']['calls'], before['used']['calls'] + 1)
            after = self.rows(sid)
            with self.assertRaisesRegex(InvalidUpdate, 'repair exhausted'):
                report_review.run(self.s, sid, self.root, retry['candidate_id'], self.provider('synthesis'), retry=retry)
            self.assertEqual(self.rows(sid), after)
            self.step(done); self.assertEqual(self.rows(sid), after)
            self.assertTrue(self.s.verify_invocations(sid))

    def test_explicit_on_pauses_without_changing_controls_or_charging_until_off_selected(self):
        with patch.dict(os.environ, SUTRA_THINKING='on'):
            saved = self.draft(); stopped = self.step(saved); sid = self.meta['active']
            self.assertEqual(stopped['state'], 'PAUSED'); self.assertEqual(stopped['stop']['retry_mode'], 'explicit_off')
            self.assertIn('Select Thinking Off', stopped['stop']['error'])
            rows = self.rows(sid); spent = review_budget.usage(self.s, sid)
            again = self.step(self.step(stopped, event='resume'))
            self.assertEqual(again['state'], 'PAUSED'); self.assertEqual(self.rows(sid), rows)
            self.assertEqual(review_budget.usage(self.s, sid), spent); self.assertEqual(os.environ['SUTRA_THINKING'], 'on')
        self.timeout = False
        with patch.dict(os.environ, SUTRA_THINKING='off'):
            done = self.until('DONE', self.step(again, event='resume'))
            self.assertTrue(done['data']['result']['admission']['admitted'])
            self.assertIs(self.claims(sid)[1][1]['reasoning']['requested'], False)
            self.assertEqual(os.environ['SUTRA_THINKING'], 'off')

    def test_new_explicit_on_between_automatic_bridges_requires_saved_pause(self):
        with patch.dict(os.environ, SUTRA_THINKING='adaptive'):
            pending = self.step(self.draft()); sid = self.meta['active']
        rows = self.rows(sid); spent = review_budget.usage(self.s, sid)
        with patch.dict(os.environ, SUTRA_THINKING='on'):
            stopped = self.step(pending)
            self.assertEqual(stopped['state'], 'PAUSED'); self.assertTrue(stopped['stop']['control_wait'])
            self.assertFalse(stopped['stop']['automatic_retry']); self.assertEqual(self.rows(sid), rows)
            self.assertEqual(review_budget.usage(self.s, sid), spent)
        self.timeout = False
        with patch.dict(os.environ, SUTRA_THINKING='adaptive'):
            done = self.until('DONE', self.step(stopped, event='resume'))
        self.assertTrue(done['data']['result']['admission']['admitted']); self.assertEqual(len(self.claims(sid)), 2)

    def test_settled_timeout_recovers_off_after_interruption_before_flow_checkpoint(self):
        from lifecycle import Interrupted
        saved = self.draft(); sid = self.meta['active']; original_move = flow.move
        def interrupt(store, fid, target, kind, **data):
            if kind == 'CLAIM_REVIEW_CHECKPOINT':
                raise Interrupted('Fixture interrupted after timeout receipt before checkpoint')
            return original_move(store, fid, target, kind, **data)
        with patch.dict(os.environ, SUTRA_THINKING='adaptive'), patch.object(flow, 'move', side_effect=interrupt):
            stopped = self.step(saved)
        self.assertEqual(stopped['state'], 'PAUSED'); self.assertEqual(stopped['stop']['status'], 'interrupted')
        self.assertIsNone(flow.info(self.s, stopped['flow_id'])[1].get('claim_review_retry'))
        rows = self.rows(sid); spent = review_budget.usage(self.s, sid)
        self.assertEqual(len(self.claims(sid)), 1)
        self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite'); self.tier = 'high'; self.timeout = False
        with patch.dict(os.environ, SUTRA_THINKING='adaptive'), \
                patch('chat_runtime.answer', side_effect=AssertionError('accepted draft replay')):
            resumed = self.step(stopped, event='resume')
            self.assertEqual(review_budget.usage(self.s, sid), spent)
            done = self.until('DONE', resumed)
        self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertEqual(self.rows(sid)[:len(rows)], rows)
        self.assertEqual(len(self.claims(sid)), 2)
        self.assertIs(self.claims(sid)[1][1]['reasoning']['requested'], False)
        self.assertEqual(review_budget.usage(self.s, sid)['used']['calls'], spent['used']['calls'] + 1)
        self.assertTrue(self.s.verify_invocations(sid))

    def test_one_fit_attempt_still_saves_second_committed_plan_slot(self):
        from context_manager import ContextBudgetExceeded
        from model_identity import compare, unknown
        class TooThin(Budget):
            def check(self, *args, **kwargs):
                raise ContextBudgetExceeded('Simulated two-share packet cannot fit')
        with patch.dict(os.environ, SUTRA_THINKING='adaptive'):
            saved = self.draft(); sid = self.meta['active']
            candidate = flow.info(self.s, saved['flow_id'])[1]['parent_checkpoint']['candidate_id']
            model = TimedReviewer('fixture-timeout-reviewer', self.root)
            slots = []
            def allocation(provider, ledger, session, *, slots=2, **kwargs):
                provider.budget = (TooThin(65536, 8192, 256) if slots == 2 else Budget(65536, 8192, 256))
                return {'attempt_slots_reserved': slots}
            def record(*args, **kwargs):
                slots.append(kwargs.get('slots', 2)); return allocation(*args, **kwargs)
            separation = compare(unknown(fixture.MODELS['low']), unknown(model.model))
            with machine.scope(saved['execution_id']), \
                    patch('report_review.choose', return_value=(model.model, separation)), \
                    patch('api_runtime.provider_for', return_value=model), \
                    patch('review_context.context_budget', side_effect=record):
                stopped = report_review.run(self.s, sid, self.root, candidate, max_pages=1)
            self.assertFalse(stopped['admission']['admitted'])
            retry = stopped['claim_review_stop']
            self.assertTrue(retry['automatic_retry'])
            [(env, _, receipt)] = self.claims(sid)
            self.assertEqual(self.s.run(env['run_id'])['config']['max_calls'], 1)
            self.assertEqual(settled_tokens(env, receipt), env['charge'] + env['budget_profile']['response_reserve'])
            plan = claim_pages.find(self.s, sid, candidate)
            self.assertEqual(plan['attempts_per_page'], 2); self.assertIn(1, slots)
            rows = self.rows(sid); before = review_budget.usage(self.s, sid)
            other = TimedReviewer('fixture-high-reviewer', self.root, timeout=False)
            other.thinking = True
            with machine.scope(saved['execution_id']):
                final = report_review.run(self.s, sid, self.root, candidate, other, retry=retry, max_pages=1)
            self.assertTrue(final['admission']['admitted'])
            self.assertEqual(self.rows(sid)[:len(rows)], rows)
            self.assertEqual(len(self.claims(sid)), 2)
            self.assertIs(other.payloads[0]['reasoning']['requested'], False)
            self.assertIs(other.thinking, True)  # Temporary Off never persists on the supplied provider.
            self.assertEqual(os.environ['SUTRA_THINKING'], 'adaptive')
            self.assertEqual(review_budget.usage(self.s, sid)['used']['calls'], before['used']['calls'] + 1)

    def test_absent_opt_in_retains_old_timeout_outcome_schema_and_wire(self):
        fit = chat_runtime.fit_packet
        def historical(ledger, sid, packet, *args, **kwargs):
            packet.pop('claim_timeout_recovery_version', None)
            return fit(ledger, sid, packet, *args, **kwargs)
        with patch.dict(os.environ, SUTRA_THINKING='adaptive'), patch.object(chat_runtime, 'fit_packet', historical):
            saved = self.draft()
        sid = self.meta['active']
        with patch.dict(os.environ, SUTRA_THINKING='adaptive'):
            done = self.step(saved)
        self.assertEqual(done['state'], 'DONE'); self.assertFalse(done['data']['result']['admission']['admitted'])
        self.assertIsNone(flow.info(self.s, done['flow_id'])[1].get('claim_review_retry'))
        [(env, packet, _)] = self.claims(sid)
        self.assertNotIn('claim_timeout_recovery_version', packet)
        opted = dict(packet, claim_timeout_recovery_version=1)
        self.assertEqual(report_review.schema(opted), report_review.schema(packet))
        self.assertEqual(build(report_review.SYSTEM, opted, report_review.schema(opted)),
                         build(report_review.SYSTEM, packet, report_review.schema(packet)))
        budget = Budget(**env['budget_profile'])
        self.assertEqual(budget.charge(report_review.SYSTEM, opted, report_review.schema(opted)),
                         budget.charge(report_review.SYSTEM, packet, report_review.schema(packet)))
        with self.assertRaisesRegex(InvalidUpdate, 'binding changed'):
            report_review.request_check(self.s, sid, opted)
        self.assertTrue(self.s.verify_invocations(sid))

    def test_timeout_retry_flags_and_source_cannot_bypass_original_receipt(self):
        with patch.dict(os.environ, SUTRA_THINKING='adaptive'):
            pending = self.step(self.draft()); sid = self.meta['active']
        retry = flow.info(self.s, pending['flow_id'])[1]['claim_review_retry']
        before = review_budget.usage(self.s, sid); rows = self.rows(sid)
        self.timeout = False; model = self.provider('synthesis')
        with machine.scope(pending['execution_id']), self.assertRaisesRegex(InvalidUpdate, 'committed failure'):
            report_review.run(self.s, sid, self.root, retry['candidate_id'], model,
                              retry=dict(retry, retry_mode='explicit_off', automatic_retry=False))
        self.assertFalse(model.payloads); self.assertEqual(self.rows(sid), rows)
        self.assertEqual(review_budget.usage(self.s, sid), before)
        (self.ws / 'part0.py').write_text('def changed(): return 999\n')
        stopped = self.step(pending)
        self.assertEqual(stopped['state'], 'PAUSED'); self.assertEqual(stopped['stop']['status'], 'provenance_changed')
        self.assertEqual(self.rows(sid), rows); self.assertEqual(review_budget.usage(self.s, sid), before)


if __name__ == '__main__': unittest.main()
