"""A proven duplicate child syntax repair checkpoints its unused paid ceiling."""
import copy
import json
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate, canonical, sha
from invocation_gateway import InvocationLedger, consume_dispatch
from karma_adapter import KarmaAdapter
from local_model import StructuredOutputRejected
from model_packet import build
from paging import Budget
import orchestration as machine
import review_budget
import subagent_runtime as child_runtime
import test_orchestration as fixtures
from test_conversation import MODELS


class SyntaxChild:
    model_interface = 1

    def __init__(self, model, *, success=False, continue_slice=False, errors=None):
        self.model = model
        self.budget = Budget(16384, 2048, 256)
        self.payloads = []
        self.success, self.continue_slice = success, continue_slice
        self.errors = errors or [
            'Strata rejected the structured reply. Cause: invalid JSON: '
            'Expecting value: line 1 column 1 (char 0). Return one JSON object matching the supplied schema.']

    def call(self, cid, system, payload, schema):
        wire = build(system, payload, schema)
        consume_dispatch(cid, self.model, system, payload, schema, packet=wire)
        self.payloads.append(copy.deepcopy(payload))
        response = {'done': True, 'done_reason': 'stop', 'prompt_eval_count': 400, 'eval_count': 80}
        if not self.success:
            error = StructuredOutputRejected(self.errors[min(len(self.payloads) - 1, len(self.errors) - 1)])
            error.call_record = {'call_id': cid, 'provider': 'strata', 'ok': False, 'response': response}
            raise error
        properties = wire['schema']['properties']
        choices = properties['citations']['items'].get('enum', [])
        result = {'answer': 'The supplied source defines these partition functions and return paths.',
                  'citations': [choices[0]], 'remaining': []}
        if 'summary' in properties:
            result['summary'] = 'Current source defines the partition.'
        if 'slice_status' in properties:
            result['slice_status'] = 'continue' if self.continue_slice else 'complete'
        if self.continue_slice:
            result['remaining'] = ['Inspect the next source slice to trace the remaining return path.']
        return result, {'call_id': cid, 'provider': 'strata', 'ok': True,
                        'answer': copy.deepcopy(result), 'response': response}


class ChildGovernorTests(unittest.TestCase):
    setUp = fixtures.OrchestrationTests.setUp
    tearDown = fixtures.OrchestrationTests.tearDown
    step = fixtures.OrchestrationTests.step
    until = fixtures.OrchestrationTests.until
    children = fixtures.OrchestrationTests.children
    sources_for_slices = fixtures.OrchestrationTests.sources_for_slices

    def provider(self, stage):
        if stage != 'subagent':
            return fixtures.OrchestrationTests.provider(self, stage)
        worker = SyntaxChild(MODELS[self.tier], success=getattr(self, 'successful', False),
                             continue_slice=getattr(self, 'continuing', False),
                             errors=getattr(self, 'syntax_errors', None))
        self.models.append((stage, worker))
        return worker

    def workers(self):
        return [worker for stage, worker in self.models if stage == 'subagent']

    def receipts(self, nid):
        return self.s.db.execute("SELECT i.id,r.id,r.record_json FROM invocations i "
            "JOIN invocation_receipts r ON r.invocation_id=i.id "
            "WHERE json_extract(i.record_json,'$.orchestration.node_id')=? ORDER BY i.rowid", (nid,)).fetchall()

    def test_third_identical_syntax_call_saves_attempt_without_quarantine_or_pending_dispatch(self):
        ready = self.children()
        nid = ready['children'][0]['id']
        contract = self.s.pinned(self.meta['active'])
        before = review_budget.usage(self.s, self.meta['active'])
        stopped = self.step(ready)
        self.assertEqual(stopped['state'], 'PAUSED')
        self.assertEqual(stopped['stop']['status'], 'child_repair_unchanged_request')
        self.assertEqual(len(self.workers()[-1].payloads), 2)
        self.assertNotIn('repair', self.workers()[-1].payloads[0])
        self.assertIn('failed JSON parsing', canonical(self.workers()[-1].payloads[1]['repair']))
        self.assertEqual(machine.state(self.s, nid), 'READY')
        pause = machine.events(self.s, nid)[-1]
        self.assertEqual(pause['kind'], 'CHILD_REPAIR_PAUSED')
        self.assertEqual(pause['data']['remaining_attempts'], 1)
        self.assertEqual(pause['kernel']['decision'], 'BRIDGE')
        self.assertFalse(pause['kernel']['grants_permissions'])
        self.assertEqual(len(self.receipts(nid)), 2)
        pending = self.s.db.execute('SELECT COUNT(*) FROM invocations i LEFT JOIN invocation_receipts r '
                                   'ON r.invocation_id=i.id WHERE r.id IS NULL').fetchone()[0]
        self.assertEqual(pending, 0)
        self.assertEqual(self.s.run(pause['data']['run_id'])['events'][-1]['kind'], 'FIN')
        after = review_budget.usage(self.s, self.meta['active'])
        self.assertEqual(after['used']['calls'], before['used']['calls'] + 2)
        self.assertEqual(after['used']['tokens'], before['used']['tokens'] + 960)
        self.assertEqual(self.s.pinned(self.meta['active']), contract)
        self.assertTrue(self.s.verify_invocations(self.meta['active']))

    def test_reload_same_child_no_change_resume_cannot_spend_saved_attempt(self):
        ready = self.children(); nid = ready['children'][0]['id']
        stopped = self.step(ready)
        receipts = self.receipts(nid)
        used = review_budget.usage(self.s, self.meta['active'])
        inherited = copy.deepcopy(machine.node(self.s, nid))
        self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite')
        resumed = self.step(stopped, event='resume')
        paused_again = self.step(resumed)
        self.assertEqual(paused_again['state'], 'PAUSED')
        self.assertEqual(paused_again['children'][0]['id'], nid)
        self.assertEqual(self.workers()[-1].payloads, [])
        self.assertEqual(self.receipts(nid), receipts)
        self.assertEqual(review_budget.usage(self.s, self.meta['active']), used)
        for key in ('context', 'context_hash', 'permissions', 'files', 'binding', 'limits'):
            self.assertEqual(machine.node(self.s, nid)[key], inherited[key])

    def test_model_switch_uses_only_original_remaining_attempt_and_keeps_saved_slice(self):
        self.sources_for_slices()
        ready = self.children(); nid = ready['children'][0]['id']
        self.successful, self.continuing = True, True
        sliced = self.step(ready)
        self.assertEqual(sliced['child']['slice_status'], 'checkpoint')
        prefix = self.receipts(nid)
        self.successful = self.continuing = False
        stopped = self.step(sliced)
        self.assertEqual(stopped['state'], 'PAUSED')
        self.assertEqual(self.receipts(nid)[:len(prefix)], prefix)
        spent = review_budget.usage(self.s, self.meta['active'])
        node = copy.deepcopy(machine.node(self.s, nid))
        self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite')
        self.tier, self.successful = 'high', True
        resumed = self.step(stopped, event='resume')
        completed = self.step(resumed)
        self.assertEqual(completed['children'][0]['id'], nid)
        self.assertEqual(machine.state(self.s, nid), 'DONE')
        worker = self.workers()[-1]
        self.assertEqual(len(worker.payloads), 1)
        self.assertTrue(worker.payloads[0]['child_checkpoint'])
        self.assertIn('CED724', canonical(worker.payloads[0]['memory']))
        self.assertEqual(worker.model, MODELS['high'])
        self.assertEqual(self.receipts(nid)[:len(prefix)], prefix)
        after = review_budget.usage(self.s, self.meta['active'])
        self.assertEqual(after['used']['calls'], spent['used']['calls'] + 1)
        self.assertEqual(after['used']['tokens'], spent['used']['tokens'] + 480)
        for key in ('context', 'context_hash', 'permissions', 'files', 'binding', 'limits'):
            self.assertEqual(machine.node(self.s, nid)[key], node[key])
        self.assertTrue(self.s.verify_invocations(self.meta['active']))

    def test_changed_parser_location_is_a_different_repair_opportunity(self):
        self.syntax_errors = [
            'Strata rejected the structured reply. Cause: invalid JSON: Expecting value: '
            'line 1 column 1 (char 0). Return one JSON object.',
            'Strata rejected the structured reply. Cause: invalid JSON: Expecting value: '
            'line 1 column 8 (char 7). Return one JSON object.']
        stopped = self.step(self.children())
        self.assertEqual(len(self.workers()[-1].payloads), 3)
        self.assertEqual(stopped['stop']['status'], 'child_review_rejected')

    def test_missing_profile_retains_historical_three_attempt_behavior(self):
        with patch.object(child_runtime, 'CHILD_REPAIR_CONTROL_VERSION', 0):
            stopped = self.step(self.children())
        worker = self.workers()[-1]
        self.assertEqual(len(worker.payloads), 3)
        self.assertNotIn('child_repair_control_version', worker.payloads[0])
        self.assertEqual(stopped['stop']['status'], 'child_review_rejected')
        self.assertEqual(worker.payloads[1]['repair'], worker.payloads[2]['repair'])

    def test_unclassified_backend_failure_is_not_guessed_to_be_repeated_json(self):
        self.syntax_errors = ['Structured output was rejected without a parser classification.']
        stopped = self.step(self.children())
        self.assertEqual(len(self.workers()[-1].payloads), 3)
        self.assertEqual(stopped['stop']['status'], 'child_review_rejected')

    def test_financial_reservation_change_alone_does_not_change_generation_fingerprint(self):
        stopped = self.step(self.children()); nid = stopped['children'][0]['id']
        iid = self.receipts(nid)[-1][0]
        env, _, packet = machine._child_receipt(self.s, machine.node(self.s, nid), iid)
        budget = dict(env['budget_profile'], reservation_limit=100000)
        same = dict(budget, reservation_limit=900000)
        identity = env['model_identity']; thinking = packet['reasoning']['requested']
        first = child_runtime._repair_fingerprint(env['model'], identity, packet, env['schema'], budget, thinking)
        self.assertEqual(first, child_runtime._repair_fingerprint(
            env['model'], identity, packet, env['schema'], same, thinking))
        changed = dict(same, response_reserve=same['response_reserve'] + 1024)
        self.assertNotEqual(first, child_runtime._repair_fingerprint(
            env['model'], identity, packet, env['schema'], changed, thinking))

    def test_higher_effective_task_limit_alone_does_not_authorize_duplicate_inference(self):
        # Exercise a changed allocation projection, not a forged grant receipt.
        # Grant authorization has separate existing tests; this guard must not
        # treat extra financial room as different model input.
        import allocation_recovery
        stopped = self.step(self.children()); nid = stopped['children'][0]['id']
        receipts = self.receipts(nid)
        before = review_budget.usage(self.s, self.meta['active'])
        effective = allocation_recovery.effective

        def increased(*args, **kwargs):
            limits, version, head = effective(*args, **kwargs)
            return dict(limits, tokens=limits['tokens'] + 50000), version + 1, head

        with patch.object(allocation_recovery, 'effective', side_effect=increased):
            raised = review_budget.usage(self.s, self.meta['active'])
            self.assertEqual(raised['limits']['tokens'], before['limits']['tokens'] + 50000)
            resumed = self.step(stopped, event='resume')
            again = self.step(resumed)
            self.assertEqual(again['stop']['status'], 'child_repair_unchanged_request')
            self.assertEqual(self.workers()[-1].payloads, [])
            self.assertEqual(review_budget.usage(self.s, self.meta['active'])['used'], before['used'])
        self.assertEqual(self.receipts(nid), receipts)

    def test_adapter_rejects_forged_attempt_accounting_and_failure_class(self):
        observation = {'profile': 'child-repair-governor-v1', 'version': 1,
            'status': 'unchanged_failed_request', 'fingerprint': 'a' * 64,
            'failure_class': 'invalid_json', 'attempts_used': 2, 'remaining_attempts': 1}
        self.assertEqual(KarmaAdapter().child_repair(observation)['decision'], 'BRIDGE')
        for changes in ({'attempts_used': True}, {'remaining_attempts': 2},
                        {'failure_class': 'contradicted'}, {'fingerprint': 'bad'}):
            with self.assertRaises(ValueError): KarmaAdapter().child_repair(dict(observation, **changes))


if __name__ == '__main__':
    unittest.main()
