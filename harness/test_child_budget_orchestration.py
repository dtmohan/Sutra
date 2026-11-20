"""Child financial recovery reaches workspace policy before another dispatch."""
import copy
import json
import os
import unittest
from unittest.mock import patch

from context_manager import ContextBudgetExceeded, canonical
from invocation_gateway import InvocationLedger
from paging import Budget
import orchestration as machine
import orchestration_flow as flow
from review_context import ReviewContextExceeded
import review_budget
import test_orchestration as fixture
from test_model_packet import WireModel
import subagent_runtime as child_runtime
import test_child_source_handoff as handoff_fixture
import workspace_allocation


class CheckpointChild(WireModel):
    counts = (400, 80)
    continuing = True

    def call(self, *args):
        answer, record = super().call(*args)
        answer['slice_status'] = 'continue' if self.continuing else 'complete'
        return answer, record


class ChildBudgetOrchestrationTests(unittest.TestCase):
    tearDown = fixture.OrchestrationTests.tearDown
    step = fixture.OrchestrationTests.step
    until = fixture.OrchestrationTests.until

    def setUp(self):
        fixture.OrchestrationTests.setUp(self)
        fixture.OrchestrationTests.sources_for_slices(self)
        self.continuing = True

    def provider(self, stage):
        if stage != 'subagent':
            return fixture.OrchestrationTests.provider(self, stage)
        model = CheckpointChild(fixture.MODELS[self.tier])
        model.continuing = self.continuing
        self.models.append((stage, model))
        return model

    def records(self):
        return self.s.db.execute(
            'SELECT i.id,i.record_json,r.record_json FROM invocations i '
            'LEFT JOIN invocation_receipts r ON r.invocation_id=i.id ORDER BY i.rowid').fetchall()

    def checkpoint(self):
        fork = machine.fork

        def bounded(store, parent, proposals, *args, **kwargs):
            proposals = copy.deepcopy(proposals)
            # A real accepted fixture call consumes 480 tokens, leaving the
            # same financial remainder reported by the user's saved review.
            proposals[0]['limits']['tokens'] = 16152
            return fork(store, parent, proposals, *args, **kwargs)

        with patch.object(machine, 'fork', side_effect=bounded):
            out = self.until('CHILDREN')
        child, sibling = [row['id'] for row in out['children']]
        reviewed = self.step(out)
        self.assertEqual(reviewed['child']['slice_status'], 'checkpoint')
        self.assertEqual(machine.spent(self.s, child)['tokens'], 480)
        remaining = min(frame['limits']['tokens'] - frame['used']['tokens']
                        for frame in machine.gates(self.s, child, {}))
        self.assertEqual(remaining, 15672)
        return reviewed, child, sibling

    def shortage(self, child, *, physical=False):
        sizing = {'branch_id': child, 'available_tokens': 15672,
                  'effective_context_tokens': 65536, 'model_context_tokens': 262144,
                  'review_context_allocation': 15672, 'attempt_slots_reserved': 1}
        if not physical:
            sizing['task_allocation_need'] = {'dimension': 'tokens', 'minimum': 731,
                'required_context': 16403, 'model_context': 65536, 'slots': 1}
        failure = ReviewContextExceeded(sizing, Budget(15672, 2048, 256),
                                        ContextBudgetExceeded('Measured child packet cannot fit'))
        if physical:
            failure.details.update(failure_basis='physical_context',
                                   physical_context={'required_tokens': 70000, 'context_tokens': 65536})
        return failure

    def test_measured_child_shortfall_uses_workspace_reserve_in_place_then_switches_model(self):
        out, child, sibling = self.checkpoint()
        sid, execution = self.meta['active'], out['execution_id']
        ids = (child, sibling, execution)
        nodes = {ident: machine.node(self.s, ident) for ident in ids}
        limits = {ident: machine.effective_limits(self.s, nodes[ident]) for ident in ids}
        spent = {ident: machine.spent(self.s, ident) for ident in ids}
        cursor = machine.slice_cursor(self.s, child)
        history = machine.slice_history(self.s, child, cursor)
        rows, contract, before = self.records(), self.s.pinned(sid), review_budget.usage(self.s, sid)

        with patch.dict(os.environ, {workspace_allocation.ENVIRONMENT_KEY: '{}'}), \
                patch('subagent_runtime.run', side_effect=self.shortage(child)):
            recovered = self.step(out)

        self.assertEqual(recovered['state'], 'CHILDREN')
        self.assertEqual(recovered['execution_id'], execution)
        self.assertEqual([row['id'] for row in recovered['children']], [child, sibling])
        self.assertEqual(recovered['allocation_amendment']['additions'], {'tokens': 731})
        self.assertEqual(recovered['allocation_amendment']['authority'], 'host_workspace_policy')
        for ident in ids:
            self.assertEqual(machine.node(self.s, ident), nodes[ident])
            expected = dict(limits[ident])
            if ident != sibling:
                expected['tokens'] += 731
            self.assertEqual(machine.effective_limits(self.s, nodes[ident]), expected)
            self.assertEqual(machine.spent(self.s, ident), spent[ident])
        self.assertEqual(machine.slice_history(self.s, child, cursor), history)
        self.assertEqual(self.records(), rows)
        after = review_budget.usage(self.s, sid)
        self.assertEqual(after['used'], before['used'])
        self.assertEqual(after['limits'], dict(before['limits'], tokens=before['limits']['tokens'] + 731))
        self.assertEqual(after['allocation_version'], before['allocation_version'] + 1)
        self.assertEqual(self.s.pinned(sid), contract)
        self.assertTrue(all(frame['decision'] == 'ALLOW'
                            for frame in machine.gates(self.s, child, {'tokens': 16403})))

        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        self.tier, self.continuing = 'high', False
        saved = flow.response(self.s, recovered['flow_id'])
        self.assertEqual(review_budget.usage(self.s, sid), after)
        with patch('repository_tools.execute', side_effect=AssertionError('Unchanged sources must reuse their receipts')):
            continued = self.step(saved)
        self.assertTrue(continued['child']['result']['admission']['admitted'])
        model = self.models[-1][1]
        self.assertEqual(model.model, fixture.MODELS['high'])
        self.assertEqual(model.payloads[0]['child_slice'], cursor)
        self.assertEqual(model.payloads[0]['contract'], contract)
        self.assertIn('CED724', canonical(model.payloads[0]['memory']))
        self.assertEqual(self.records()[:len(rows)], rows)
        final = review_budget.usage(self.s, sid)
        self.assertEqual(final['used']['calls'], after['used']['calls'] + 1)
        self.assertEqual(final['used']['tokens'], after['used']['tokens'] + 480)
        for dimension in ('source_bytes', 'operations'):
            self.assertEqual(final['used'][dimension], after['used'][dimension])
        self.assertEqual(final['limits'], after['limits'])
        self.assertEqual(machine.node(self.s, sibling), nodes[sibling])
        self.assertTrue(self.s.verify_invocations(sid))

    def test_disabled_or_exhausted_workspace_policy_keeps_a_financial_pause(self):
        out, child, sibling = self.checkpoint()
        sid = self.meta['active']
        before, rows = review_budget.usage(self.s, sid), self.records()
        checkpoint = machine.slice_cursor(self.s, child)
        policies = [({'enabled': False}, 'policy_disabled'),
                    ({'tokens': before['limits']['tokens']}, 'workspace_limit')]
        for index, (policy, reason) in enumerate(policies):
            with self.subTest(reason=reason), patch.dict(os.environ, {
                    workspace_allocation.ENVIRONMENT_KEY: json.dumps(policy)}):
                if index:
                    out = self.step(out, event='resume')
                    self.assertEqual(out['state'], 'CHILDREN')
                with patch('subagent_runtime.run', side_effect=self.shortage(child)):
                    out = self.step(out)
                self.assertEqual(out['state'], 'PAUSED')
                stop = out['stop']
                self.assertEqual(stop['status'], 'context_budget')
                self.assertEqual(stop['allocation_need']['minimum'], 731)
                self.assertEqual(stop['automatic_allocation']['reason'], reason)
                self.assertEqual(stop['child_context']['branch_id'], child)
                self.assertEqual(review_budget.usage(self.s, sid), before)
                self.assertEqual(self.records(), rows)
                self.assertEqual(machine.slice_cursor(self.s, child), checkpoint)
                self.assertEqual([row['id'] for row in out['children']], [child, sibling])

    def test_physical_child_limit_does_not_invent_an_allocation_grant(self):
        out, child, _ = self.checkpoint()
        sid = self.meta['active']
        before, rows = review_budget.usage(self.s, sid), self.records()
        with patch.dict(os.environ, {workspace_allocation.ENVIRONMENT_KEY: '{}'}), \
                patch('subagent_runtime.run', side_effect=self.shortage(child, physical=True)):
            paused = self.step(out)
        self.assertEqual(paused['state'], 'PAUSED')
        self.assertEqual(paused['stop']['child_context']['failure_basis'], 'physical_context')
        self.assertNotIn('allocation_need', paused['stop'])
        self.assertNotIn('allocation_amendment', paused)
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertEqual(self.records(), rows)


class ClosingCallRecoveryTests(unittest.TestCase):
    setUp = fixture.OrchestrationTests.setUp
    tearDown = fixture.OrchestrationTests.tearDown
    provider = fixture.OrchestrationTests.provider
    step = fixture.OrchestrationTests.step
    until = fixture.OrchestrationTests.until
    source = handoff_fixture.ChildSourceHandoffTests.source

    def test_prior_call_grants_do_not_block_one_final_handoff_within_workspace_policy(self):
        fork = machine.fork

        def bounded(store, parent, proposals, *args, **kwargs):
            proposals = copy.deepcopy(proposals)
            proposals[0]['limits']['calls'] = 4 / 3
            return fork(store, parent, proposals, *args, **kwargs)

        original = fixture.Child.call
        with patch.dict(os.environ, {workspace_allocation.ENVIRONMENT_KEY: '{}'}), \
                patch.object(machine, 'fork', side_effect=bounded):
            out, branch, path, target = self.source(target_first=False)
        remaining = f'Inspect {target}.'

        def historical(worker, cid, system, packet, schema):
            result, record = original(worker, cid, system, packet, schema)
            prior = {(item['path'], item['quote']) for note in packet.get('child_checkpoint', [])
                     for item in note['citations']}
            fresh = next(item for item in schema['properties']['citations']['items']['enum']
                         if (item['path'], item['quote']) not in prior)
            result.update(slice_status='continue', remaining=[remaining], citations=[copy.deepcopy(fresh)])
            return result, record

        accepted = 0
        with patch.dict(os.environ, {workspace_allocation.ENVIRONMENT_KEY: '{}'}), \
                patch.object(child_runtime, 'CHILD_EVIDENCE_VERSION', 3), \
                patch.object(fixture.Child, 'call', historical):
            for _ in range(10):
                out = self.step(out)
                self.assertEqual(out['state'], 'CHILDREN', out)
                if out.get('allocation_amendment'):
                    self.assertEqual(out['allocation_amendment']['additions'], {'calls': 2})
                else:
                    self.assertTrue(out['child']['result']['admission']['admitted'])
                    accepted += 1
                if accepted == 4:
                    break
        self.assertEqual(accepted, 4)
        sid, execution = self.meta['active'], out['execution_id']
        prior_grants = self.s.db.execute('SELECT record_json FROM allocation_amendments ORDER BY rowid').fetchall()
        self.assertEqual(len(prior_grants), 2)
        self.assertEqual(machine.spent(self.s, branch)['calls'], 4)
        self.assertAlmostEqual(machine.effective_limits(self.s, machine.node(self.s, branch))['calls'], 5 + 1 / 3)

        seen = []

        def finish(worker, cid, system, packet, schema):
            result, record = original(worker, cid, system, packet, schema)
            seen.append(copy.deepcopy(packet))
            if packet.get('child_source_handoff'):
                self.assertEqual(schema['properties']['slice_status']['enum'], ['complete'])
                result.update(slice_status='complete', remaining=[])
            else:
                self.assertIn(f'def {target}()', packet['excerpts'][path])
                self.assertIn('    return value', packet['excerpts'][path])
                result.update(slice_status='continue', remaining=[remaining])
            return result, record

        with patch.dict(os.environ, {workspace_allocation.ENVIRONMENT_KEY: '{}'}), \
                patch.object(fixture.Child, 'call', finish):
            delivered = self.step(out)
            self.assertEqual(delivered['state'], 'CHILDREN')
            self.assertFalse(seen[-1].get('child_source_handoff'))
            self.assertEqual(machine.spent(self.s, branch)['calls'], 5)
            cursor = machine.slice_cursor(self.s, branch)
            saved = machine.slice_history(self.s, branch, cursor)
            before = review_budget.usage(self.s, sid)
            permissions = copy.deepcopy(machine.node(self.s, branch)['permissions'])
            records = self.s.db.execute('SELECT record_json FROM invocation_receipts ORDER BY rowid').fetchall()

            # The handoff reaches the same fractional call boundary as the
            # saved user's 16 -> 17 -> 18 sequence. The outer flow amends the
            # settled child automatically rather than returning a user pause.
            resumed = self.step(delivered)
            self.assertEqual(resumed['state'], 'CHILDREN')
            self.assertIsNone(resumed['stop'])
            amendment = resumed['allocation_amendment']
            self.assertEqual(amendment['authority'], 'host_workspace_policy')
            self.assertEqual(amendment['additions'], {'calls': 2})
            self.assertEqual(review_budget.usage(self.s, sid)['used'], before['used'])
            self.assertEqual(machine.slice_history(self.s, branch, cursor), saved)
            self.assertEqual(self.s.db.execute('SELECT record_json FROM invocation_receipts ORDER BY rowid').fetchall(), records)
            self.assertEqual(machine.node(self.s, branch)['permissions'], permissions)
            self.assertEqual(resumed['execution_id'], execution)
            self.assertEqual(len(seen), 1, 'A refused closing call must not execute')
            finished = self.step(resumed)

        self.assertEqual(machine.state(self.s, branch), 'DONE')
        self.assertEqual(len(seen), 2)
        self.assertTrue(seen[-1].get('child_source_handoff'))
        self.assertIn('partial_handoff', finished['child'])
        self.assertEqual(machine.spent(self.s, branch)['calls'], 6)
        all_grants = self.s.db.execute('SELECT record_json FROM allocation_amendments ORDER BY rowid').fetchall()
        self.assertEqual(all_grants[:len(prior_grants)], prior_grants)
        self.assertEqual(len(all_grants), 3)
        after = review_budget.usage(self.s, sid)
        self.assertEqual(after['used']['calls'], before['used']['calls'] + 1)
        self.assertEqual(after['limits']['calls'], before['limits']['calls'] + 2)
        for dimension in ('operations', 'source_bytes'):
            self.assertEqual(after['used'][dimension], before['used'][dimension])
        joined = self.until('SYNTHESIS', finished)
        self.assertEqual(joined['state'], 'SYNTHESIS')
        self.assertTrue(any(event['kind'] == 'JOIN' for event in machine.events(self.s, execution)))
        self.assertTrue(self.s.verify_invocations(sid))


if __name__ == '__main__':
    unittest.main()
