"""Whole-task continuation decisions use the existing immutable audit chain."""
import copy
import json
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate
from invocation_gateway import InvocationLedger
import orchestration as machine
import orchestration_flow as flow
import review_budget
import task_continuation as continuation
import test_orchestration as fixture
import test_task_continuation as recovery_fixture


class ContinuationReceiptTests(unittest.TestCase):
    setUp = fixture.OrchestrationTests.setUp
    tearDown = fixture.OrchestrationTests.tearDown
    provider = fixture.OrchestrationTests.provider
    step = fixture.OrchestrationTests.step
    until = fixture.OrchestrationTests.until
    paused = recovery_fixture.ContinuationRecoveryTests.paused

    def request(self, out, event=None, request_id='c' * 32):
        descriptor = out['continuation']
        return {'models': fixture.MODELS, 'tier': self.tier, 'armed': True,
                'event': event or descriptor['next_event'], 'flow_id': out['flow_id'],
                'cursor': out['cursor'], 'binding': out.get('binding'),
                'task_runner': {'version': 1, 'request_id': request_id, 'request_digest': 'e' * 64},
                'continuation_receipt': copy.deepcopy(descriptor['receipt'])}

    def run_request(self, request):
        return flow.handle(self.s, self.meta, self.ws, self.root, request, self.provider)

    def permit(self, out, request_id='c' * 32):
        return continuation.attach(self.s, self.root,
            {'event': 'resume', 'task_runner': {'version': 1, 'request_id': request_id, 'request_digest': 'e' * 64}}, out)

    def decision_count(self):
        return self.s.db.execute("SELECT COUNT(*) FROM page_audit WHERE event='RUNNER_CONTINUATION_DECISION'").fetchone()[0]

    def test_initial_explicit_request_records_kernel_decision_without_extra_flow_transition(self):
        out = self.until('CHILDREN')
        before = len(machine.events(self.s, out['flow_id']))
        spending = review_budget.usage(self.s, self.meta['active'])
        permitted = self.permit(out)
        self.assertEqual(permitted['cursor'], out['cursor'])
        self.assertEqual(len(machine.events(self.s, out['flow_id'])), before)
        self.assertEqual(review_budget.usage(self.s, self.meta['active']), spending)
        self.assertEqual(permitted['continuation']['kernel']['decision'], 'ALLOW')
        self.assertEqual(permitted['continuation']['runner_request_id'], 'c' * 32)
        row = self.s.db.execute('SELECT payload_json FROM page_audit WHERE seq=?',
                               (permitted['continuation']['receipt']['seq'],)).fetchone()
        record = json.loads(row[0])
        self.assertGreater(record['at'], 0)
        self.assertEqual(record['flow_receipt'], out['cursor'])
        self.assertEqual(record['kernel'], permitted['continuation']['kernel'])
        self.assertTrue(self.s.audit_valid())

    def test_latest_receipt_is_required_and_old_model_step_cannot_replay(self):
        out = self.until('CHILDREN')
        permitted = self.permit(out)
        request = self.request(permitted)
        result = self.run_request(request)
        self.assertNotEqual(result['cursor'], permitted['cursor'])
        calls = len(self.models)
        for wrong in (request, dict(self.request(result), continuation_receipt=None)):
            with self.assertRaisesRegex(InvalidUpdate, 'receipt is missing, stale or replayed'):
                self.run_request(wrong)
        self.assertEqual(len(self.models), calls)
        self.assertTrue(self.s.audit_valid())

    def test_cross_runner_flow_binding_or_intent_cannot_borrow_permit(self):
        permitted = self.permit(self.until('CHILDREN'))
        request = self.request(permitted)
        wrongs = [dict(request, task_runner={'version': 1, 'request_id': 'd' * 32, 'request_digest': 'e' * 64}),
                  dict(request, task_runner={'version': 1, 'request_id': 'c' * 32, 'request_digest': 'f' * 64}),
                  dict(request, event='resume'), dict(request, binding={'forged': True}),
                  dict(request, cursor='0' * 64)]
        calls = len(self.models)
        for wrong in wrongs:
            with self.subTest(request=wrong), self.assertRaises(InvalidUpdate):
                self.run_request(wrong)
        self.assertEqual(len(self.models), calls)

    def test_wait_probes_reuse_receipt_but_fresh_readiness_creates_resume_permit(self):
        out = self.permit(self.paused())
        before = self.decision_count()
        spending = review_budget.usage(self.s, self.meta['active'])
        blocked = recovery_fixture.ResourceWait({'admitted': False, 'available': True,
            'decision': 'BRIDGE', 'reason': 'Memory pressure remains elevated.'})
        for _ in range(3):
            with patch('monitoring.require_resources', side_effect=blocked):
                observed = self.run_request(self.request(out))
            self.assertEqual(observed['continuation']['receipt'], out['continuation']['receipt'])
            self.assertEqual(self.decision_count(), before)
            out = observed
        healthy = {'admitted': True, 'available': True, 'decision': 'ALLOW', 'sample': {'wall_time': 1}}
        with patch('monitoring.require_resources', return_value=healthy):
            ready = self.run_request(self.request(out))
        self.assertEqual(ready['continuation']['kernel']['decision'], 'ALLOW')
        self.assertEqual(ready['continuation']['next_event'], 'resume')
        self.assertNotEqual(ready['continuation']['receipt'], out['continuation']['receipt'])
        record = continuation._latest(self.s, 'c' * 32)[0]
        self.assertEqual(record['readiness_hash'], continuation.sha(continuation.canonical(record['readiness'])))
        with self.assertRaisesRegex(InvalidUpdate, 'stale or replayed'):
            self.run_request(self.request(out))
        self.assertEqual(review_budget.usage(self.s, self.meta['active']), spending)
        self.assertTrue(self.s.audit_valid())

    def test_reload_model_switch_and_fresh_resume_recheck_preserve_contract_and_spending(self):
        out = self.permit(self.paused())
        sid = self.meta['active']; contract = self.s.pinned(sid)
        spending = review_budget.usage(self.s, sid)
        healthy = {'admitted': True, 'available': True, 'decision': 'ALLOW'}
        with patch('monitoring.require_resources', return_value=healthy):
            ready = self.run_request(self.request(out))
        request = self.request(ready, event='resume')
        request['continuation_fingerprint'] = ready['continuation']['fingerprint']
        self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite'); self.tier = 'high'
        request['tier'] = 'high'
        blocked = recovery_fixture.ResourceWait({'admitted': False, 'available': False, 'reason': 'Fresh observation failed'})
        with patch('monitoring.require_resources', side_effect=blocked):
            waiting = self.run_request(request)
        self.assertEqual(waiting['state'], 'PAUSED')
        self.assertEqual(waiting['continuation']['kernel']['decision'], 'BRIDGE')
        self.assertEqual(self.s.pinned(sid), contract)
        self.assertEqual(review_budget.usage(self.s, sid), spending)
        self.assertTrue(self.s.verify_invocations(sid))

    def test_stop_invalidates_old_permit_before_any_new_model_work(self):
        out = self.permit(self.until('CHILDREN'))
        request = self.request(out)
        self.step(out, event='cancel')
        calls = len(self.models)
        with self.assertRaisesRegex(InvalidUpdate, 'checkpoint or task binding changed'):
            self.run_request(request)
        self.assertEqual(len(self.models), calls)

    def test_probe_discovered_unknown_outcome_records_fin_reason_and_measurement(self):
        out = self.permit(self.paused())
        with patch.object(continuation, '_pending_call', return_value='unsettled-invocation'):
            result = self.run_request(self.request(out))
        self.assertEqual(result['cursor'], out['cursor'])
        self.assertEqual(result['continuation']['kernel']['decision'], 'FIN')
        record = continuation._latest(self.s, 'c' * 32)[0]
        self.assertEqual(record['reason'], result['continuation']['reason'])
        self.assertEqual(record['readiness']['observation']['blocker'], 'unknown_outcome')
        self.assertTrue(self.s.audit_valid())

    def test_final_fin_permit_only_authorizes_readonly_summary(self):
        out = self.permit(self.until('DONE'))
        calls = len(self.models); before = self.decision_count()
        self.assertEqual(out['continuation']['kernel']['decision'], 'FIN')
        result = self.run_request(self.request(out, event='runner_summary'))
        self.assertIn('runner_usage', result)
        self.assertEqual(result['continuation']['receipt'], out['continuation']['receipt'])
        self.assertEqual(self.decision_count(), before)
        with self.assertRaisesRegex(InvalidUpdate, 'intent was not admitted'):
            self.run_request(self.request(result, event='step'))
        self.assertEqual(len(self.models), calls)

    def test_permission_pause_and_readonly_summary_bind_same_ungranted_task(self):
        self.operation = 'edit'
        out = self.until('NEED_PERMISSION')
        sid = self.meta['active']; contract = self.s.pinned(sid)
        spending = review_budget.usage(self.s, sid)
        calls = len(self.models)
        original = (self.ws / 'part0.py').read_bytes()
        out = self.permit(out)
        self.assertEqual(out['continuation']['binding'], out['bound_task'])
        self.assertEqual(out['continuation']['kernel']['decision'], 'BRIDGE')
        request = self.request(out, event='runner_summary')
        request['binding'] = out['bound_task']
        result = self.run_request(request)
        self.assertEqual(result['state'], 'NEED_PERMISSION')
        self.assertEqual(result['cursor'], out['cursor'])
        self.assertEqual(result['bound_task'], out['bound_task'])
        self.assertEqual(result['continuation'], out['continuation'])
        self.assertEqual(self.s.pinned(sid), contract)
        self.assertEqual(review_budget.usage(self.s, sid), spending)
        self.assertEqual((self.ws / 'part0.py').read_bytes(), original)
        self.assertEqual(len(self.models), calls)
        with self.assertRaisesRegex(InvalidUpdate, 'intent was not admitted'):
            self.run_request(dict(request, event='step'))

    def test_malformed_or_omitted_runner_identity_cannot_use_a_receipt(self):
        out = self.permit(self.until('CHILDREN'))
        request = self.request(out)
        malformed = dict(request, task_runner={'version': True, 'request_id': 'c' * 32, 'request_digest': 'e' * 64})
        missing = dict(request); missing.pop('task_runner')
        for value in (malformed, missing):
            with self.assertRaises(InvalidUpdate):
                self.run_request(value)

    def stop_request(self, out=None, request_id='c' * 32):
        return {'event': 'runner_stop', 'op': 'orchestrate',
                'task_runner': {'version': 1, 'request_id': request_id, 'request_digest': 'e' * 64},
                'runner_stop': {'version': 1, 'request_digest': 'e' * 64, 'reason': 'User pressed Stop'},
                **({'flow_id': out['flow_id']} if out else {})}

    def test_runner_stop_audits_fin_and_revokes_uid_without_cancelling_saved_task(self):
        out = self.permit(self.until('CHILDREN'))
        request = self.request(out)
        sid = self.meta['active']; contract = self.s.pinned(sid)
        spending = review_budget.usage(self.s, sid); children = copy.deepcopy(out['children'])
        calls = len(self.models)
        stopped = self.run_request(self.stop_request(out))
        self.assertTrue(stopped['stopped'])
        self.assertEqual(stopped['kernel']['decision'], 'FIN')
        self.assertNotEqual(stopped['receipt'], out['continuation']['receipt'])
        current = flow.response(self.s, out['flow_id'])
        self.assertEqual(current['state'], out['state'])
        self.assertEqual(current['cursor'], out['cursor'])
        self.assertEqual(current['children'], children)
        self.assertEqual(self.s.pinned(sid), contract)
        self.assertEqual(review_budget.usage(self.s, sid), spending)
        self.assertEqual(len(self.models), calls)
        for wrong in (request, dict(request, continuation_receipt=stopped['receipt'])):
            with self.assertRaisesRegex(InvalidUpdate, 'permits are revoked'):
                self.run_request(wrong)
        self.assertEqual(self.run_request(self.stop_request(out)), stopped)
        self.assertTrue(self.s.audit_valid())

    def test_stop_before_first_response_survives_reload_and_blocks_initial_dispatch(self):
        stopped = self.run_request(self.stop_request())
        self.assertEqual(stopped['kernel']['decision'], 'FIN')
        self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite')
        with self.assertRaisesRegex(InvalidUpdate, 'permits are revoked'):
            self.step(task_runner={'version': 1, 'request_id': 'c' * 32, 'request_digest': 'e' * 64})
        self.assertEqual(self.models, [])
        self.assertTrue(self.s.audit_valid())

    def test_stop_is_metadata_only_when_authorization_or_storage_prevents_work(self):
        out = self.permit(self.until('CHILDREN'))
        stop = self.stop_request(out); stop['armed'] = False
        with patch('storage_policy.require_space', side_effect=AssertionError('Stop cannot acquire capacity')), \
                patch('lifecycle.reconcile', side_effect=AssertionError('Stop cannot reconcile work')):
            stopped = self.run_request(stop)
        self.assertTrue(stopped['stopped'])
        self.assertTrue(self.s.audit_valid())


if __name__ == '__main__':
    unittest.main()
