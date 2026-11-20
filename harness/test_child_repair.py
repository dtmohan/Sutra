"""Failed children stay quarantined; one separately admitted branch can finish."""
import copy
import json
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate, canonical
from invocation_gateway import InvocationLedger
import orchestration as machine
import orchestration_flow as flow
import review_budget
import test_orchestration as fixtures
Child = fixtures.Child
from test_conversation import MODELS
from paging import Budget


class RejectedPacketChild(Child):
    """A provider rejection has no final usage, so the whole call stays charged."""
    model_interface = 1
    budget = Budget(65536, 24000, 256)

    def call(self, cid, system, packet, schema):
        from invocation_gateway import consume_dispatch
        from local_model import StructuredOutputRejected
        from model_packet import build
        consume_dispatch(cid, self.model, system, packet, schema, packet=build(system, packet, schema))
        self.payloads.append(copy.deepcopy(packet))
        error = StructuredOutputRejected('Return one JSON object matching the supplied schema.')
        error.call_record = {'call_id': cid, 'provider': 'strata', 'ok': False,
                             'response': {'done': False, 'eval_count': 80}}
        raise error


class ChildRepairTests(unittest.TestCase):
    setUp = fixtures.OrchestrationTests.setUp
    tearDown = fixtures.OrchestrationTests.tearDown
    step = fixtures.OrchestrationTests.step
    until = fixtures.OrchestrationTests.until
    children = fixtures.OrchestrationTests.children
    provider = fixtures.OrchestrationTests.provider

    def reject(self, out):
        original = Child.call
        def bad(worker, *args):
            proposal, record = original(worker, *args)
            proposal['constraints'] = []
            return proposal, record
        with patch.object(Child, 'call', bad):
            return self.step(out)

    def test_replacement_on_another_model_preserves_sibling_context_and_exact_remaining_share(self):
        out = self.step(self.children())
        completed, rejected = [row['id'] for row in out['children']]
        completed_receipt = machine.events(self.s, completed)[-1]['data']['invocation_id']
        stopped = self.reject(out)
        sid = self.meta['active']; parent = machine.node(self.s, rejected)['parent_id']
        contract = self.s.pinned(sid); old = copy.deepcopy(machine.node(self.s, rejected))
        old_events = machine.events(self.s, rejected); old_spent = machine.spent(self.s, rejected)
        before = review_budget.usage(self.s, sid); parent_before = machine.spent(self.s, parent)
        self.assertEqual(stopped['state'], 'PAUSED')
        self.assertEqual(stopped['stop']['status'], 'child_review_rejected')
        self.assertTrue(stopped['stop']['errors'])
        self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite'); self.tier = 'high'
        with self.assertRaisesRegex(InvalidUpdate, 'user-armed'):
            self.step(stopped, event='resume', armed=False)
        resumed = self.step(stopped, event='resume')
        replacement = resumed['children'][1]['id']; new = machine.node(self.s, replacement)
        self.assertNotEqual(replacement, rejected)
        self.assertEqual(resumed['children'][0]['id'], completed)
        self.assertEqual(new['limits'], {k: machine.effective_limits(self.s, old)[k] - old_spent[k] for k in old['limits']})
        for key in ('context', 'context_hash', 'permissions', 'files', 'binding'):
            self.assertEqual(new[key], old[key])
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertEqual(machine.spent(self.s, parent), parent_before)
        done = self.until('DONE', resumed)
        self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertEqual(self.s.pinned(sid), contract)
        self.assertEqual(machine.events(self.s, rejected), old_events)
        self.assertEqual(machine.spent(self.s, rejected), old_spent)
        self.assertEqual(machine.state(self.s, rejected), 'QUARANTINED')
        joined = [event for event in machine.events(self.s, parent) if event['kind'] == 'JOIN'][-1]['data']['invocations']
        self.assertIn(completed_receipt, joined)
        self.assertNotIn(stopped['stop']['invocation_id'], joined)
        worker = [provider for stage, provider in self.models if stage == 'subagent'][-1]
        self.assertEqual(worker.model, MODELS['high'])
        self.assertIn('CED724', canonical(worker.payloads[0]['memory']))
        self.assertGreater(review_budget.usage(self.s, sid)['used']['calls'], before['used']['calls'])
        self.assertTrue(self.s.verify_invocations(sid)); self.assertTrue(self.s.audit_valid())

    def test_second_rejection_stays_saved_without_endless_replacements_or_refunds(self):
        stopped = self.reject(self.children())
        resumed = self.step(stopped, event='resume')
        replacement = resumed['children'][0]['id']
        stopped_again = self.reject(resumed)
        self.assertEqual(stopped_again['state'], 'PAUSED')
        self.assertFalse(stopped_again['stop']['retryable'])
        before = review_budget.usage(self.s, self.meta['active'])
        count = self.s.db.execute('SELECT COUNT(*) FROM orchestration_nodes').fetchone()[0]
        still = self.step(stopped_again, event='resume')
        self.assertEqual(still['state'], 'PAUSED')
        self.assertEqual(machine.state(self.s, replacement), 'QUARANTINED')
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM orchestration_nodes').fetchone()[0], count)
        self.assertEqual(review_budget.usage(self.s, self.meta['active']), before)

    def test_replacement_commit_recovers_without_duplicate_allocation(self):
        stopped = self.reject(self.children())
        original = flow.move
        def interrupt(s, fid, target, kind, **data):
            if kind == 'CHILD_REPAIR_ALLOCATED':
                raise RuntimeError('Interrupted after replacement commit')
            return original(s, fid, target, kind, **data)
        before = review_budget.usage(self.s, self.meta['active'])
        with patch.object(flow, 'move', interrupt), self.assertRaisesRegex(RuntimeError, 'Interrupted'):
            self.step(stopped, event='resume')
        old = stopped['stop']['branch_id']; parent = machine.node(self.s, old)['parent_id']
        replacement = machine.fork_children(self.s, parent)[0]
        self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite')
        resumed = self.step(stopped, event='resume')
        self.assertEqual(resumed['children'][0]['id'], replacement)
        self.assertEqual(review_budget.usage(self.s, self.meta['active']), before)
        self.assertEqual(len([e for e in machine.events(self.s, parent) if e['kind'] == 'CHILD_REPLACED']), 1)
        self.assertEqual(self.until('DONE', resumed)['state'], 'DONE')

    def test_auto_chooses_an_alternate_configured_model_once(self):
        self.tier = 'high'
        stopped = self.reject(self.children())
        self.assertEqual(stopped['stop']['model'], MODELS['high'])
        resumed = self.step(stopped, event='resume', tier='auto')
        replacement = machine.node(self.s, resumed['children'][0]['id'])
        self.assertEqual(replacement['retry_route']['model'], MODELS['mid'])
        self.tier = 'mid'
        reviewed = self.step(resumed, tier='auto')
        self.assertEqual(reviewed['child']['route']['model'], MODELS['mid'])
        self.assertEqual(machine.state(self.s, replacement['id']), 'DONE')

    def test_replacement_does_not_override_a_later_governed_resource_fallback(self):
        self.tier = 'high'
        stopped = self.reject(self.children())
        resumed = self.step(stopped, event='resume', tier='auto')
        flow.move(self.s, resumed['flow_id'], 'CHILDREN', 'RESOURCE_MODEL_FALLBACK',
                  resource_fallback={'models': dict(MODELS), 'tier': 'low'})
        resumed = flow.response(self.s, resumed['flow_id'])
        self.tier = 'low'
        reviewed = self.step(resumed, tier='auto')
        self.assertEqual(reviewed['child']['route']['model'], MODELS['low'])
        self.assertEqual(reviewed['child']['route']['mode'], 'resource-low')

    def test_accepted_slices_survive_a_rejected_later_slice_as_unverified_context(self):
        fixtures.OrchestrationTests.sources_for_slices(self)
        initial = self.children(); old = initial['children'][0]['id']; original = Child.call
        def checkpoint(worker, *args):
            proposal, record = original(worker, *args); proposal['slice_status'] = 'continue'; return proposal, record
        with patch.object(Child, 'call', checkpoint):
            partial = self.step(initial)
        checkpoint_id = partial['child']['result']['invocation_id']
        stopped = self.reject(partial)
        resumed = self.step(stopped, event='resume'); replacement = resumed['children'][0]['id']
        reviewed = self.step(resumed)
        packet = self.models[-1][1].payloads[0]
        self.assertEqual(packet['recovery_checkpoint'][0]['invocation_id'], checkpoint_id)
        self.assertEqual(packet['child_checkpoint'], [])
        self.assertEqual(machine.state(self.s, old), 'QUARANTINED')
        self.assertEqual(machine.state(self.s, replacement), 'DONE')
        self.assertEqual(self.until('DONE', reviewed)['state'], 'DONE')

    def test_two_provider_rejections_then_budget_pause_amend_and_resume_same_child(self):
        fixtures.OrchestrationTests.sources_for_slices(self)
        self.tier = 'high'
        out = self.children()
        child, sibling = [row['id'] for row in out['children']]
        original_call = Child.call
        def checkpoint(worker, *args):
            result, record = original_call(worker, *args)
            result['slice_status'] = 'continue'
            return result, record
        with patch.object(Child, 'call', checkpoint):
            out = self.step(out)
        cursor = machine.slice_cursor(self.s, child)
        notes = machine.slice_history(self.s, child, cursor)
        saved_child = machine.node(self.s, child)
        execution = saved_child['parent_id']
        sibling_limits = machine.effective_limits(self.s, machine.node(self.s, sibling))
        failed = RejectedPacketChild(MODELS['high'])
        provider = self.provider
        self.provider = lambda stage: failed if stage == 'subagent' else provider(stage)
        with patch.dict('os.environ', SUTRA_WORKSPACE_ALLOCATION='{"enabled":false}'):
            stopped = self.step(out)
        self.provider = provider
        self.assertEqual(len(failed.payloads), 2)
        self.assertEqual(stopped['state'], 'PAUSED')
        self.assertEqual(stopped['stop']['status'], 'task_budget')
        self.assertEqual(stopped['stop']['frame']['limiting_dimension'], 'tokens')
        checkpoint = stopped['stop']['child_repair']
        self.assertEqual(checkpoint['remaining_attempts'], 1)
        self.assertEqual(checkpoint['child_slice'], cursor)
        run = self.s.run(checkpoint['run_id'])
        self.assertEqual(run['events'][-1]['kind'], 'FIN')
        self.assertEqual(sum(e['kind'] == 'DISPATCH' for e in run['events']), 2)
        failed_rows = list(self.s.db.execute('SELECT i.id,i.record_json,r.record_json FROM invocations i '
            'JOIN invocation_receipts r ON r.invocation_id=i.id WHERE i.run_id=?', (run['id'],)))
        for _, raw, receipt_raw in failed_rows:
            env, receipt = json.loads(raw), json.loads(receipt_raw)
            self.assertEqual(receipt['status'], 'quarantined')
            self.assertNotIn('failure_kind', receipt)
            self.assertEqual(receipt['accounting_settlement']['basis'], 'reserved_unmeasured')
            self.assertEqual(receipt['accounting_settlement']['used_tokens'],
                             env['charge'] + env['budget_profile']['response_reserve'])
        self.assertIsNone(self.s.db.execute('SELECT c.id FROM orchestration_charges c LEFT JOIN '
            'orchestration_settlements r ON r.charge_id=c.id WHERE r.charge_id IS NULL').fetchone())
        sid = self.meta['active']
        spent = review_budget.usage(self.s, sid)
        preview = self.step(stopped, event='allocation_preview', armed=False)
        approved = self.step(preview, event='allocation_grant', armed=False, confirmed=True,
                             proposal=preview['allocation_proposal'])
        self.assertEqual(review_budget.usage(self.s, sid)['used'], spent['used'])
        self.assertEqual(approved['execution_id'], execution)
        self.assertEqual([row['id'] for row in approved['children']], [child, sibling])
        self.assertEqual(machine.node(self.s, child), saved_child)
        self.assertEqual(machine.effective_limits(self.s, machine.node(self.s, sibling)), sibling_limits)
        self.assertEqual(machine.slice_history(self.s, child, cursor), notes)
        self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite')
        self.tier = 'mid'
        resumed = self.step(approved, event='resume')
        reviewed = self.step(resumed)
        worker = self.models[-1][1]
        self.assertEqual(worker.model, MODELS['mid'])
        self.assertEqual(len(worker.payloads), 1)
        self.assertEqual(worker.payloads[0]['child_slice'], cursor)
        self.assertEqual(worker.payloads[0]['child_checkpoint'][0]['invocation_id'], notes[0]['invocation_id'])
        self.assertEqual(worker.payloads[0]['repair'], checkpoint['repair'])
        self.assertIn('CED724', canonical(worker.payloads[0]['memory']))
        self.assertEqual(worker.payloads[0]['branch']['id'], child)
        for iid, raw, receipt_raw in failed_rows:
            self.assertEqual(self.s.db.execute('SELECT record_json FROM invocations WHERE id=?', (iid,)).fetchone()[0], raw)
            self.assertEqual(self.s.db.execute('SELECT record_json FROM invocation_receipts WHERE invocation_id=?', (iid,)).fetchone()[0], receipt_raw)
        new_run, = self.s.db.execute('SELECT run_id FROM invocations WHERE id=?',
                                    (reviewed['child']['result']['invocation_id'],)).fetchone()
        self.assertEqual(self.s.run(new_run)['config']['max_calls'], 1)
        self.assertEqual(machine.state(self.s, child), 'DONE')
        self.assertEqual(self.until('DONE', reviewed)['state'], 'DONE')
        self.assertTrue(self.s.verify_invocations(sid))
        self.assertTrue(self.s.audit_valid())

    def test_workspace_reserve_amends_a_settled_child_without_replacing_execution(self):
        self.tier = 'high'
        out = self.children()
        child, sibling = [row['id'] for row in out['children']]
        execution = out['execution_id']
        original = {nid: machine.node(self.s, nid) for nid in (child, sibling, execution)}
        failed = RejectedPacketChild(MODELS['high'])
        provider = self.provider
        self.provider = lambda stage: failed if stage == 'subagent' else provider(stage)
        with patch.dict('os.environ', SUTRA_WORKSPACE_ALLOCATION='{}'):
            amended = self.step(out)
        self.provider = provider
        self.assertEqual(len(failed.payloads), 2)
        self.assertEqual(amended['state'], 'CHILDREN')
        self.assertEqual(amended['execution_id'], execution)
        self.assertEqual(amended['allocation_amendment']['authority'], 'host_workspace_policy')
        sid = self.meta['active']
        self.assertEqual(review_budget.usage(self.s, sid)['allocation_version'], 1)
        self.assertEqual(machine.state(self.s, execution), 'WAITING_CHILDREN')
        self.assertEqual(machine.state(self.s, child), 'READY')
        self.assertEqual(machine.effective_limits(self.s, original[sibling]), original[sibling]['limits'])
        for nid, node in original.items():
            self.assertEqual(machine.node(self.s, nid), node)
        failed_tokens = machine.spent(self.s, child)['tokens']
        self.assertGreater(failed_tokens, 50000)
        self.tier = 'mid'
        reviewed = self.step(amended)
        self.assertEqual(machine.state(self.s, child), 'DONE')
        self.assertGreater(machine.spent(self.s, child)['tokens'], failed_tokens)
        self.assertEqual(self.until('DONE', reviewed)['state'], 'DONE')
        self.assertTrue(self.s.verify_invocations(sid))

    def test_saved_budget_pause_without_checkpoint_recovers_through_normal_resume(self):
        self.tier = 'high'
        out = self.children()
        child = out['children'][0]['id']
        failed = RejectedPacketChild(MODELS['high'])
        provider = self.provider
        self.provider = lambda stage: failed if stage == 'subagent' else provider(stage)
        with patch.dict('os.environ', SUTRA_WORKSPACE_ALLOCATION='{"enabled":false}'), \
                patch('subagent_runtime._pause_repair', return_value=None):
            stopped = self.step(out)
        self.provider = provider
        self.assertEqual(len(failed.payloads), 2)
        self.assertNotIn('child_repair', stopped['stop'])
        # Match a saved stop written before resume_phase and repair checkpoints.
        stop = {k: v for k, v in stopped['stop'].items() if k != 'resume_phase'}
        flow.move(self.s, stopped['flow_id'], 'PAUSED', 'WORKSPACE_RESERVE_STOPPED', workflow_stop=stop)
        stopped = flow.response(self.s, stopped['flow_id'])
        run_id, = self.s.db.execute("SELECT run_id FROM invocations WHERE "
            "json_extract(record_json,'$.orchestration.node_id')=? ORDER BY rowid DESC LIMIT 1", (child,)).fetchone()
        self.assertEqual(self.s.run(run_id)['events'][-1]['kind'], 'ATTEMPT')
        charged = review_budget.usage(self.s, self.meta['active'])['used']
        preview = self.step(stopped, event='allocation_preview', armed=False)
        approved = self.step(preview, event='allocation_grant', armed=False, confirmed=True,
                             proposal=preview['allocation_proposal'])
        self.assertEqual(review_budget.usage(self.s, self.meta['active'])['used'], charged)
        self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite')
        self.tier = 'mid'
        resumed = self.step(approved, event='resume')
        reviewed = self.step(resumed)
        self.assertEqual(self.s.run(run_id)['events'][-1]['kind'], 'FIN')
        self.assertEqual(machine.state(self.s, child), 'DONE')
        self.assertEqual(len(self.models[-1][1].payloads), 1)
        self.assertIn('repair', self.models[-1][1].payloads[0])
        self.assertEqual(self.until('DONE', reviewed)['state'], 'DONE')
        self.assertTrue(self.s.verify_invocations(self.meta['active']))

    def test_resource_stop_after_resumed_rejection_keeps_only_the_unspent_repair_slot(self):
        from invocation_gateway import InvocationGateway
        from local_model import OutputLimitExceeded, StructuredOutputRejected
        from monitoring import ResourceWait
        import subagent_runtime as child_runtime
        self.tier = 'high'
        out = self.children()
        child = out['children'][0]['id']
        limited = RejectedPacketChild(MODELS['high'])
        limited.budget = Budget(65536, 40000, 256)
        limited.context_metadata = {'thinking': {'values': [False, True]}}
        reject = limited.call
        def output_limit(*args):
            try:
                return reject(*args)
            except StructuredOutputRejected as exc:
                error = OutputLimitExceeded('The generation exhausted its output allowance.')
                error.call_record = exc.call_record
                raise error
        limited.call = output_limit
        provider = self.provider
        self.provider = lambda stage: limited if stage == 'subagent' else provider(stage)
        with patch.dict('os.environ', SUTRA_WORKSPACE_ALLOCATION='{"enabled":false}'):
            stopped = self.step(out)
        self.provider = provider
        carried = stopped['stop']['child_repair']
        self.assertEqual(carried['remaining_attempts'], 2)
        self.assertTrue(carried['output_repaired'])
        preview = self.step(stopped, event='allocation_preview', armed=False)
        approved = self.step(preview, event='allocation_grant', armed=False, confirmed=True,
                             proposal=preview['allocation_proposal'])
        resumed = self.step(approved, event='resume')
        rejected = RejectedPacketChild(MODELS['mid'])
        rejected.budget = Budget(16384, 2048, 256)
        invoke = InvocationGateway.invoke
        def busy(gateway, *args):
            if rejected.payloads:
                raise ResourceWait({'reason': 'Provider is busy.', 'runtime': {'kind': 'busy'}})
            return invoke(gateway, *args)
        with patch.object(InvocationGateway, 'invoke', busy), self.assertRaises(ResourceWait):
            child_runtime.run(self.s, self.meta, self.ws, self.root, child, MODELS, 'mid', rejected)
        self.assertEqual(len(rejected.payloads), 1)
        recovered = child_runtime._resume_repair(self.s, child, machine.slice_cursor(self.s, child))
        self.assertNotEqual(recovered['run_id'], carried['run_id'])
        self.assertEqual(recovered['remaining_attempts'], 1)
        self.assertTrue(recovered['output_repaired'])
        latest = self.s.run(recovered['run_id'])
        self.assertEqual(latest['config']['max_calls'], 2)
        self.assertEqual(sum(e['kind'] == 'DISPATCH' for e in latest['events']), 1)
        self.assertEqual(latest['events'][-1]['kind'], 'FIN')
        self.tier = 'mid'
        reviewed = self.step(resumed)
        self.assertEqual(len(self.models[-1][1].payloads), 1)
        self.assertEqual(machine.state(self.s, child), 'DONE')
        self.assertEqual(self.until('DONE', reviewed)['state'], 'DONE')
        self.assertTrue(self.s.verify_invocations(self.meta['active']))


if __name__ == '__main__':
    unittest.main()
