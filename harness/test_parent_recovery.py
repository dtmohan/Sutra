"""Parent boundaries preserve accepted evidence and retry spending.

Deterministic fixtures only: no inference or user ledger access. These tests do
not establish tokenizer accuracy, RAM safety or real report quality.
"""
import json
import unittest
from unittest.mock import patch
import claim_pages
import chat_runtime
import orchestration as machine
import orchestration_flow as flow
import review_budget
import test_orchestration as fixture
from budget_accounting import used as settled_tokens
from context_manager import InvalidUpdate, canonical
from invocation_gateway import InvocationLedger
from lifecycle import Interrupted, reconcile
from paging import Budget
from test_architecture_followup import Reader
from test_chat import Fake


class ParentModel(Fake):
    def __init__(self, model, *, interrupt=False, fail_draft=False):
        super().__init__(model)
        self.budget = Budget(32768, 4096, 256)
        self.interrupt, self.fail_draft = interrupt, fail_draft

    def call(self, cid, system, packet, schema):
        value, record = super().call(cid, system, packet, schema)
        if packet.get('review_protocol') == 2 and self.interrupt:
            raise Interrupted('Bridge operation deadline reached')
        if 'report' in value and self.fail_draft and 'repair' not in packet:
            value['complete'] = True
        record['response'] = {'done': True, 'prompt_eval_count': 400, 'eval_count': 80}
        return value, record


class ParentRecoveryTests(unittest.TestCase):
    tearDown = fixture.OrchestrationTests.tearDown
    step = fixture.OrchestrationTests.step
    until = fixture.OrchestrationTests.until

    def setUp(self):
        fixture.OrchestrationTests.setUp(self)
        self.prompt = 'Explain the architecture and tools logic. ' + self.prompt
        self.interrupt = self.fail_draft = False
        self.planner_calls = []

    def provider(self, stage):
        if stage != 'synthesis':
            return fixture.OrchestrationTests.provider(self, stage)
        model = ParentModel(fixture.MODELS[self.tier], interrupt=self.interrupt, fail_draft=self.fail_draft)
        model.reader_provider = Reader(model.model, [], self.planner_calls, Budget(32768, 512, 256))
        self.models.append((stage, model))
        return model

    def rows(self, sid):
        return self.s.db.execute('SELECT i.id,i.record_json,r.record_json FROM invocations i '
            'LEFT JOIN invocation_receipts r ON r.invocation_id=i.id WHERE i.session=? ORDER BY i.rowid', (sid,)).fetchall()

    def checkpoint(self):
        saved = self.step(self.until('SYNTHESIS'))
        self.assertEqual(saved['state'], 'SYNTHESIS'); self.assertIsNone(saved['stop'])
        self.assertNotIn('data', saved)
        point = flow.info(self.s, saved['flow_id'])[1]['parent_checkpoint']
        self.assertEqual(point['version'], 1)
        self.assertEqual(machine.events(self.s, saved['flow_id'])[-1]['kind'], 'PARENT_REVIEW_CHECKPOINT')
        return saved, point

    def test_draft_repair_returns_usage_before_claims_then_reload_switch_without_replay(self):
        self.fail_draft = True
        saved, point = self.checkpoint(); sid = self.meta['active']; rows = self.rows(sid)
        parent = [(json.loads(e), json.loads(r)) for _, e, r in rows if json.loads(e)['role'] == 'chat_reviewer']
        self.assertEqual([r['status'] for _, r in parent], ['quarantined', 'accepted'])
        self.assertEqual(parent[-1][0]['id'], point['candidate_id'])
        self.assertFalse(any(json.loads(e)['role'] == 'report_reviewer' for _, e, _ in rows))
        self.assertEqual(len(self.planner_calls), 1)
        usage = saved['claim_review_usage']
        self.assertEqual({c['invocation_id'] for c in usage['calls']}, {i for i, _, _ in rows})
        self.assertEqual(len([c for c in usage['calls'] if c['stage'] == 'review_reader']), 1)
        self.assertEqual(len([c for c in usage['calls'] if c['stage'] == 'chat_reviewer']), 2)
        self.assertEqual(usage['total_calls'], len(rows))
        contract = self.s.pinned(sid); execution = machine.node(self.s, saved['execution_id'])
        before = review_budget.usage(self.s, sid)
        self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite')
        self.meta = json.loads((self.root / 'workspace.json').read_text()); self.tier = 'high'; self.fail_draft = False
        with patch('chat_runtime.answer', side_effect=AssertionError('draft replay')), \
                patch('review_followup.expand', side_effect=AssertionError('planner replay')):
            done = self.until('DONE', saved)
        self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertEqual(done['data']['result']['invocation_id'], point['candidate_id'])
        self.assertEqual(self.rows(sid)[:len(rows)], rows); self.assertEqual(self.s.pinned(sid), contract)
        self.assertEqual(machine.node(self.s, saved['execution_id']), execution)
        self.assertFalse(execution['permissions']['network_allowed']); self.assertFalse(execution['permissions']['write_paths'])
        self.assertIn('CED724', canonical(next(model for stage, model in self.models if stage == 'synthesis').payloads[0]['memory']))
        after = review_budget.usage(self.s, sid)
        self.assertEqual(after['limits'], before['limits'])
        for key in ('operations', 'source_bytes'):
            self.assertEqual(after['used'][key], before['used'][key])
        self.assertGreater(after['used']['tokens'], before['used']['tokens']); self.assertTrue(self.s.verify_invocations(sid))

    def test_deadline_normal_pause_returns_unknown_usage_and_one_claim_attempt_left(self):
        saved, point = self.checkpoint(); sid = self.meta['active']; contract = self.s.pinned(sid)
        original = self.rows(sid); before = review_budget.usage(self.s, sid); self.interrupt = True
        paused = self.step(saved)
        self.assertEqual(paused['state'], 'PAUSED'); self.assertEqual(paused['stop']['status'], 'interrupted')
        self.assertEqual(paused['stop']['resume_phase'], 'SYNTHESIS'); self.assertFalse(paused['stop']['automatic_retry'])
        self.assertEqual(self.rows(sid)[:len(original)], original)
        [(failed_id, raw, receipt_raw)] = self.rows(sid)[len(original):]
        envelope, receipt = json.loads(raw), json.loads(receipt_raw)
        self.assertEqual(receipt['status'], 'quarantined')
        self.assertEqual(receipt['admission']['errors'], ['Prior bridge ended without a model receipt; result withheld'])
        charged = settled_tokens(envelope, receipt)
        self.assertEqual(charged, envelope['charge'] + envelope['budget_profile']['response_reserve'])
        settled = review_budget.usage(self.s, sid)
        self.assertEqual(settled['used']['tokens'] - before['used']['tokens'], charged)
        self.assertEqual(paused['claim_review_usage']['total_calls'], len(self.rows(sid)))
        unknown = next(c for c in paused['claim_review_usage']['calls'] if c['invocation_id'] == failed_id)
        self.assertIsNone(unknown['input_tokens']); self.assertIsNone(unknown['output_tokens'])
        plan = claim_pages.find(self.s, sid, point['candidate_id'])
        self.assertEqual(len(claim_pages.attempts(self.s, sid, plan['id'], 0)), 1); self.assertEqual(plan['attempts_per_page'], 2)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM invocations i LEFT JOIN invocation_receipts r '
            'ON r.invocation_id=i.id WHERE r.id IS NULL').fetchone()[0], 0)
        with self.assertRaisesRegex(InvalidUpdate, 'Choose Resume'): self.step(paused)
        with self.assertRaisesRegex(InvalidUpdate, 'user-armed'): self.step(paused, event='resume', armed=False)
        self.assertEqual(review_budget.usage(self.s, sid), settled)
        self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite')
        self.meta = json.loads((self.root / 'workspace.json').read_text()); self.tier = 'high'; self.interrupt = False
        resumed = self.step(paused, event='resume'); self.assertEqual(review_budget.usage(self.s, sid), settled)
        with patch('chat_runtime.answer', side_effect=AssertionError('accepted draft replay')): done = self.until('DONE', resumed)
        self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertEqual(done['data']['result']['invocation_id'], point['candidate_id'])
        self.assertEqual(len(claim_pages.attempts(self.s, sid, plan['id'], 0)), 2)
        self.assertEqual(self.rows(sid)[len(original)], (failed_id, raw, receipt_raw))
        final = review_budget.usage(self.s, sid)
        self.assertEqual(final['used']['calls'], settled['used']['calls'] + 1)
        self.assertGreater(final['used']['tokens'], settled['used']['tokens'])
        self.assertEqual(final['limits'], before['limits']); self.assertEqual(self.s.pinned(sid), contract)
        self.assertTrue(self.s.verify_invocations(sid)); self.assertTrue(self.s.audit_valid())

    def test_failed_writer_pause_reload_switch_keeps_original_three_attempt_limit(self):
        self.failed_writer_pause_reload_switch()

    def test_pre_marker_writer_repair_does_not_acquire_static_source_version(self):
        self.failed_writer_pause_reload_switch(legacy_claim_sources=True)

    def failed_writer_pause_reload_switch(self, *, legacy_claim_sources=False):
        from invocation_gateway import InvocationGateway
        from monitoring import ResourceWait
        ready = self.until('SYNTHESIS'); sid = self.meta['active']
        contract = self.s.pinned(sid); original_call = ParentModel.call
        original_invoke = InvocationGateway.invoke; dispatches = 0
        original_fit = chat_runtime.fit_packet
        def initial_fit(store, session, packet, *args, **kwargs):
            if legacy_claim_sources:
                packet.pop('claim_source_version', None)
            return original_fit(store, session, packet, *args, **kwargs)
        def reject(model, *args):
            value, record = original_call(model, *args)
            if 'report' in value:
                value['complete'] = True
            return value, record
        def pause(gateway, role, *args):
            nonlocal dispatches
            if role == 'chat_reviewer':
                dispatches += 1
                if dispatches == 2:
                    raise ResourceWait({'admitted': False, 'available': False, 'reason': 'fixture counter unavailable',
                                        'blocker': 'measurement_unavailable'})
            return original_invoke(gateway, role, *args)
        with patch.object(ParentModel, 'call', reject), patch.object(InvocationGateway, 'invoke', pause), \
                patch.object(chat_runtime, 'fit_packet', side_effect=initial_fit):
            stopped = self.step(ready)
        self.assertEqual(stopped['state'], 'PAUSED'); self.assertEqual(stopped['stop']['status'], 'resource_wait')
        old = self.rows(sid); before = review_budget.usage(self.s, sid)
        parents = [(i, json.loads(e), r) for i, e, r in old if json.loads(e)['role'] == 'chat_reviewer']
        self.assertEqual(len(parents), 1)
        _, first, _ = parents[0]
        first_packet = json.loads(self.s.db.execute('SELECT payload_json FROM deliveries WHERE id=?', (first['delivery_id'],)).fetchone()[0])
        self.assertEqual(first_packet['parent_repair']['attempt_limit'], 3)
        self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite'); self.tier = 'high'
        resumed = self.step(stopped, event='resume')
        self.assertEqual(review_budget.usage(self.s, sid), before)
        with patch.object(ParentModel, 'call', reject):
            done = self.until('DONE', resumed)
        self.assertFalse(done['data']['result']['admission']['admitted'])
        after = self.rows(sid); self.assertEqual(after[:len(old)], old)
        parents = [(json.loads(e), json.loads(r)) for _, e, r in after if json.loads(e)['role'] == 'chat_reviewer']
        self.assertEqual(len(parents), 3)
        self.assertEqual([self.s.run(env['run_id'])['config']['max_calls'] for env, _ in parents], [3, 2, 2])
        for env, receipt in parents:
            packet = json.loads(self.s.db.execute('SELECT payload_json FROM deliveries WHERE id=?', (env['delivery_id'],)).fetchone()[0])
            self.assertEqual(packet['_request_nonce'], first_packet['_request_nonce'])
            self.assertEqual(packet['parent_repair'], first_packet['parent_repair'])
            if legacy_claim_sources:
                self.assertNotIn('claim_source_version', packet)
            else:
                self.assertEqual(packet['claim_source_version'], 2)
            self.assertEqual(packet['contract'], contract); self.assertIn('CED724', canonical(packet['memory']))
            self.assertEqual(receipt['status'], 'quarantined')
        self.assertEqual(parents[-1][0]['model'], fixture.MODELS['high'])
        final = review_budget.usage(self.s, sid)
        self.assertEqual(final['used']['calls'], before['used']['calls'] + 2)
        self.assertGreater(final['used']['tokens'], before['used']['tokens']); self.assertEqual(final['limits'], before['limits'])
        self.step(done); self.assertEqual(self.rows(sid), after)
        self.assertTrue(self.s.verify_invocations(sid)); self.assertTrue(self.s.audit_valid())

    def test_checkpoint_cannot_change_request_or_authority_before_review_dispatch(self):
        saved, point = self.checkpoint(); sid = self.meta['active']
        data = flow.info(self.s, saved['flow_id'])[1]
        request = {'prompt': self.prompt, 'models': fixture.MODELS, 'tier': 'high',
                   'conversation_id': saved['conversation_id'], 'child_results': data['child_results'],
                   'orchestration_parent': saved['execution_id'], '_parent_step': True}
        before = review_budget.usage(self.s, sid); rows = self.rows(sid)
        model = ParentModel('other-reviewer')
        with machine.scope(saved['execution_id']):
            with self.assertRaisesRegex(InvalidUpdate, 'checkpoint differs'):
                chat_runtime.resume_parent_review(self.s, self.meta, self.ws, self.root, request,
                    dict(point, candidate_hash='0' * 64), model)
            with self.assertRaisesRegex(InvalidUpdate, 'another workflow or request'):
                chat_runtime.resume_parent_review(self.s, self.meta, self.ws, self.root,
                    dict(request, prompt='A different request.'), point, model)
        self.assertFalse(model.payloads)
        self.assertEqual(self.rows(sid), rows); self.assertEqual(review_budget.usage(self.s, sid), before)

    def test_older_saved_synthesis_recovers_accepted_candidate_and_interrupted_claim(self):
        ready = self.until('SYNTHESIS'); sid = self.meta['active']; data = flow.info(self.s, ready['flow_id'])[1]
        request = {'prompt': self.prompt, 'models': fixture.MODELS, 'tier': self.tier,
            'conversation_id': ready['conversation_id'], 'child_results': data['child_results'],
            'orchestration_parent': ready['execution_id']}
        # Pre-boundary saved051 shape, under the same validator: accepted draft,
        # committed plan, interrupted pending claim, flow still SYNTHESIS.
        writer = self.provider('synthesis'); reviewer = ParentModel('interrupted-reviewer', interrupt=True)
        with machine.scope(ready['execution_id']), self.assertRaises(Interrupted):
            chat_runtime.answer(self.s, self.meta, self.ws, self.root, request, writer, reviewer)
        reconcile(self.s, self.ws)
        rows = self.rows(sid); before = review_budget.usage(self.s, sid)
        candidate = next(i for i, e, r in reversed(rows) if json.loads(e)['role'] == 'chat_reviewer')
        self.assertNotIn('parent_checkpoint', flow.info(self.s, ready['flow_id'])[1])
        self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite'); self.tier = 'high'
        with patch('chat_runtime.answer', side_effect=AssertionError('old draft replay')):
            recovered = self.step(ready)
            self.assertEqual(recovered['state'], 'SYNTHESIS')
            self.assertEqual(machine.events(self.s, recovered['flow_id'])[-1]['kind'], 'PARENT_CANDIDATE_RECOVERED')
            self.assertEqual(review_budget.usage(self.s, sid), before)
            done = self.until('DONE', recovered)
        self.assertEqual(done['data']['result']['invocation_id'], candidate)
        self.assertTrue(done['data']['result']['admission']['admitted']); self.assertEqual(self.rows(sid)[:len(rows)], rows)
        self.assertEqual(review_budget.usage(self.s, sid)['used']['calls'], before['used']['calls'] + 1)
        self.assertEqual(recovered['claim_review_usage']['total_calls'], len(rows)); self.assertTrue(self.s.verify_invocations(sid))


if __name__ == '__main__': unittest.main()
