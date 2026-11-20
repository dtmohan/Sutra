"""Long review calls yield between paid attempts without refilling a retry limit.

Deterministic gateway/central-flow fixtures; no inference or user store access.
"""
import json
import unittest
from unittest.mock import patch

import claim_pages
import orchestration as machine
import orchestration_flow as flow
import review_budget
import test_orchestration as fixture
import test_chat as direct
from invocation_gateway import InvocationLedger
from invocation_timing import BridgeStepRequired
from local_model import OutputLimitExceeded
from paging import Budget
from test_architecture_followup import Reader
import test_parent_recovery as parent
from test_parent_recovery import ParentModel


class LongReviewModel(ParentModel):
    transport_deadline_cap = 360


class ReviewSliceTests(unittest.TestCase):
    setUp = parent.ParentRecoveryTests.setUp
    tearDown = fixture.OrchestrationTests.tearDown
    step = fixture.OrchestrationTests.step
    until = fixture.OrchestrationTests.until
    rows = parent.ParentRecoveryTests.rows

    def provider(self, stage):
        if stage != 'synthesis':
            return fixture.OrchestrationTests.provider(self, stage)
        model = LongReviewModel(fixture.MODELS[self.tier], fail_draft=self.fail_draft)
        model.reader_provider = Reader(model.model, [], self.planner_calls, Budget(32768, 512, 256))
        self.models.append((stage, model))
        return model

    def role_rows(self, sid, role):
        return [(iid, json.loads(env), json.loads(receipt))
                for iid, env, receipt in self.rows(sid) if json.loads(env)['role'] == role]

    def reload(self):
        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        self.meta = json.loads((self.root / 'workspace.json').read_text())

    def test_writer_yields_after_one_rejection_and_keeps_paid_attempts_after_model_switch(self):
        self.fail_draft = True
        saved = self.step(self.until('SYNTHESIS'))
        sid = self.meta['active']; original = self.rows(sid)
        self.assertEqual(saved['state'], 'SYNTHESIS'); self.assertIsNone(saved['stop'])
        self.assertEqual(machine.events(self.s, saved['flow_id'])[-1]['kind'], 'PARENT_GENERATION_CHECKPOINT')
        [(first, env, receipt)] = self.role_rows(sid, 'chat_reviewer')
        self.assertEqual(receipt['status'], 'quarantined')
        self.assertEqual(env['transport_timing']['seconds'], 360)
        packet = json.loads(self.s.db.execute('SELECT payload_json FROM deliveries WHERE id=?', (env['delivery_id'],)).fetchone()[0])
        initial = packet['parent_repair']; contract = self.s.pinned(sid)
        self.assertGreater(initial['attempt_limit'], 1)
        spending = review_budget.usage(self.s, sid)
        self.reload(); self.tier = 'high'; self.fail_draft = False
        accepted = self.step(saved)
        self.assertEqual(accepted['state'], 'SYNTHESIS')
        self.assertEqual(self.rows(sid)[:len(original)], original)
        self.assertEqual(len(self.role_rows(sid, 'chat_reviewer')), 2)
        new_env = self.role_rows(sid, 'chat_reviewer')[-1][1]
        new_packet = json.loads(self.s.db.execute('SELECT payload_json FROM deliveries WHERE id=?', (new_env['delivery_id'],)).fetchone()[0])
        self.assertEqual(new_packet['parent_repair'], initial)
        self.assertEqual(new_packet['_request_nonce'], packet['_request_nonce'])
        self.assertIn('repair', new_packet)
        self.assertEqual(self.s.pinned(sid), contract)
        self.assertEqual(review_budget.usage(self.s, sid)['limits'], spending['limits'])
        done = self.until('DONE', accepted)
        self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertTrue(self.s.verify_invocations(sid)); self.assertTrue(self.s.audit_valid())

    def test_three_rejected_writer_slices_do_not_create_a_fourth_attempt(self):
        original_call = ParentModel.call
        def reject(model, *args):
            result, record = original_call(model, *args)
            if 'report' in result: result['complete'] = True
            return result, record
        ready = self.until('SYNTHESIS'); sid = self.meta['active']
        with patch.object(ParentModel, 'call', reject):
            first = self.step(ready)
            self.reload(); self.tier = 'high'
            second = self.step(first)
            self.assertEqual(first['state'], 'SYNTHESIS'); self.assertEqual(second['state'], 'SYNTHESIS')
            third = self.step(second)
        self.assertEqual(third['state'], 'DONE')
        self.assertFalse(third['data']['result']['admission']['admitted'])
        self.assertEqual(len(self.role_rows(sid, 'chat_reviewer')), 3)
        before = self.rows(sid)
        self.assertEqual(self.step(third)['state'], 'DONE')
        self.assertEqual(self.rows(sid), before)

    def test_claim_repair_uses_a_fresh_step_and_retains_candidate_after_reload(self):
        saved = self.step(self.until('SYNTHESIS')); sid = self.meta['active']
        point = flow.info(self.s, saved['flow_id'])[1]['parent_checkpoint']
        contract = self.s.pinned(sid); original_call = ParentModel.call
        def reject_first(model, cid, system, packet, schema):
            result, record = original_call(model, cid, system, packet, schema)
            if packet.get('review_protocol') == 2 and 'repair' not in packet:
                result['complete'] = True
            return result, record
        with patch.object(ParentModel, 'call', reject_first): pending = self.step(saved)
        self.assertEqual(pending['state'], 'SYNTHESIS'); self.assertIsNone(pending['stop'])
        self.assertEqual(flow.info(self.s, saved['flow_id'])[1]['parent_checkpoint'], point)
        [(iid, env, receipt)] = self.role_rows(sid, 'report_reviewer')
        self.assertEqual(receipt['status'], 'quarantined'); self.assertEqual(env['transport_timing']['seconds'], 360)
        original = self.rows(sid); spending = review_budget.usage(self.s, sid)
        plan = claim_pages.find(self.s, sid, point['candidate_id'])
        self.assertEqual(len(claim_pages.attempts(self.s, sid, plan['id'], 0)), 1)
        self.reload(); self.tier = 'low'
        done = self.until('DONE', pending)
        self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertEqual(done['data']['result']['invocation_id'], point['candidate_id'])
        self.assertEqual(self.rows(sid)[:len(original)], original)
        self.assertEqual(len(claim_pages.attempts(self.s, sid, plan['id'], 0)), 2)
        self.assertEqual(review_budget.usage(self.s, sid)['limits'], spending['limits'])
        self.assertEqual(self.s.pinned(sid), contract)

    def test_insufficient_bridge_headroom_defers_without_a_writer_dispatch(self):
        ready = self.until('SYNTHESIS'); sid = self.meta['active']
        def defer(value):
            if value and value['seconds'] == 360: raise BridgeStepRequired(360, 200)
        with patch('invocation_timing.require_headroom', side_effect=defer): pending = self.step(ready)
        self.assertEqual(pending['state'], 'SYNTHESIS'); self.assertIsNone(pending['stop'])
        self.assertEqual(machine.events(self.s, pending['flow_id'])[-1]['kind'], 'MODEL_SLICE_DEFERRED')
        self.assertEqual(self.role_rows(sid, 'chat_reviewer'), [])
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM invocations i LEFT JOIN invocation_receipts r ON r.invocation_id=i.id WHERE r.id IS NULL').fetchone()[0], 0)
        done = self.until('DONE', pending)
        self.assertTrue(done['data']['result']['admission']['admitted'])

    def test_new_explicit_on_overrides_saved_adaptive_output_repair_after_boundary(self):
        original_provider = self.provider
        def positive_provider(stage):
            model = original_provider(stage)
            if stage == 'synthesis':
                model.context_metadata = {'thinking': {'values': [False, True], 'default': True}}
            return model
        self.provider = positive_provider
        original_call = ParentModel.call
        failed = False
        def limit_first_claim(model, cid, system, packet, schema):
            nonlocal failed
            result, record = original_call(model, cid, system, packet, schema)
            if packet.get('review_protocol') == 2 and not failed:
                failed = True
                self.assertEqual(packet['reasoning']['preference'], 'adaptive')
                self.assertEqual(packet['reasoning']['step'], 1)
                self.assertIs(packet['reasoning']['requested'], True)
                error = OutputLimitExceeded('Fixture exhausted positive Adaptive output.')
                error.call_record = record
                raise error
            return result, record
        with patch.dict('os.environ', SUTRA_THINKING='adaptive'), \
                patch.object(ParentModel, 'call', limit_first_claim):
            saved = self.step(self.until('SYNTHESIS'))
            point = flow.info(self.s, saved['flow_id'])[1]['parent_checkpoint']
            pending = self.step(saved)
        sid = self.meta['active']; before = self.rows(sid)
        self.assertEqual(pending['state'], 'SYNTHESIS'); self.assertIsNone(pending['stop'])
        self.assertNotIn('data', pending)  # The rejected claim page cannot publish the draft.
        [(iid, env, receipt)] = self.role_rows(sid, 'report_reviewer')
        self.assertEqual(receipt['status'], 'quarantined'); self.assertEqual(receipt['failure_kind'], 'output_limit')
        plan = claim_pages.find(self.s, sid, point['candidate_id'])
        self.assertEqual(len(claim_pages.attempts(self.s, sid, plan['id'], 0)), 1)
        spending = review_budget.usage(self.s, sid)
        self.reload(); self.tier = 'high'
        with patch.dict('os.environ', SUTRA_THINKING='on'):
            done = self.until('DONE', pending)
        self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertEqual(done['data']['result']['invocation_id'], point['candidate_id'])
        self.assertEqual(self.rows(sid)[:len(before)], before)
        self.assertEqual(len(claim_pages.attempts(self.s, sid, plan['id'], 0)), 2)
        new_env = self.role_rows(sid, 'report_reviewer')[-1][1]
        new_packet = json.loads(self.s.db.execute('SELECT payload_json FROM deliveries WHERE id=?',
                                                 (new_env['delivery_id'],)).fetchone()[0])
        self.assertEqual(new_packet['reasoning']['preference'], 'on')
        self.assertIs(new_packet['reasoning']['requested'], True)
        self.assertIn('repair', new_packet)
        self.assertEqual(review_budget.usage(self.s, sid)['limits'], spending['limits'])
        self.assertTrue(self.s.verify_invocations(sid))


class DirectTimingTests(unittest.TestCase):
    setUp = direct.ChatTests.setUp
    tearDown = direct.ChatTests.tearDown

    def test_direct_answer_keeps_legacy_timing_without_central_slice_controller(self):
        model = LongReviewModel(direct.MODELS['high'])
        result = direct.ChatTests.ask(self, 'Explain the architecture and request flow.', 'high', model)
        self.assertTrue(result['result']['admission']['admitted'])
        rows = self.s.db.execute('SELECT i.record_json,d.payload_json FROM invocations i '
            'JOIN deliveries d ON d.id=i.delivery_id WHERE i.session=?', (self.sid,)).fetchall()
        self.assertTrue(rows)
        for envelope, packet in rows:
            self.assertNotIn('transport_timing', json.loads(envelope))
            self.assertNotIn('review_transport_version', json.loads(packet))


if __name__ == '__main__': unittest.main()
