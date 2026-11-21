"""Child headroom and pre-dispatch stops retain the same accounted task."""
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from invocation_gateway import InvocationLedger
from invocation_timing import BridgeStepRequired
from monitoring import ResourceWait
from paging import Budget
import orchestration as machine
import orchestration_flow as flow
import review_budget
import subagent_runtime as child
import test_orchestration as fixtures


class ChildOutputAllocationTests(unittest.TestCase):
    def test_slices_retain_capability_and_financially_bounded_output(self):
        for context, expected in ((8192, 2730), (16384, 5461), (32768, 8192)):
            provider = SimpleNamespace(model='fixture', budget=Budget(), context_metadata={
                'model_info': {'fixture.context_length': context}, '_sutra_provider': 'sutra_remote'})
            usage = {'limits': {'tokens': 500000}, 'used': {'tokens': 12000}}
            with patch('review_budget.usage', return_value=copy.deepcopy(usage)), \
                    patch('orchestration.task', return_value={'id': None}):
                allocation = child._context_budget(None, 'saved-task', provider, True, 2)
            self.assertEqual(provider.budget.response_reserve, expected)
            self.assertLess(provider.budget.response_reserve, provider.budget.context_limit)
            self.assertEqual(allocation['available_tokens'], 488000)
            self.assertEqual(usage['used']['tokens'], 12000)


class ChildAdmissionRecoveryTests(unittest.TestCase):
    setUp = fixtures.OrchestrationTests.setUp
    tearDown = fixtures.OrchestrationTests.tearDown
    step = fixtures.OrchestrationTests.step
    until = fixtures.OrchestrationTests.until
    children = fixtures.OrchestrationTests.children
    provider = fixtures.OrchestrationTests.provider

    def test_failed_remote_sample_is_hash_chained_before_pause_then_switch_resume(self):
        out = self.step(self.children())
        sid = self.meta['active']
        sibling, pending = [row['id'] for row in out['children']]
        self.assertEqual(machine.state(self.s, sibling), 'DONE')
        pin = copy.deepcopy(self.s.pinned(sid))
        before = copy.deepcopy(review_budget.usage(self.s, sid))
        permissions = copy.deepcopy(machine.permissions(self.s, sid))
        worker = fixtures.Child(fixtures.MODELS['low'])
        worker.context_metadata = {'model_info': {'fixture.context_length': 32768}}
        original = self.provider
        self.provider = lambda stage: None if stage == 'subagent' else original(stage)
        blocked = {'profile': 'remote-nvidia-v1', 'decision': 'BRIDGE', 'available': False,
                   'admitted': False, 'blocker': 'measurement_unavailable',
                   'reason': 'Fresh SSH hardware inspection timed out.',
                   'runtime': {'kind': 'measurement_unavailable', 'inference_started': False}}
        with patch('api_runtime.provider_for', return_value=worker), \
                patch('monitoring.require_resources', return_value={'admitted': True}), \
                patch('provider_resources.require_resources', side_effect=ResourceWait(blocked)):
            stopped = self.step(out)
        self.assertEqual(stopped['state'], 'PAUSED')
        self.assertEqual(stopped['stop']['measurement'], blocked)
        self.assertEqual(stopped['stop']['blocker'], 'measurement_unavailable')
        self.assertEqual(worker.payloads, [])
        self.assertEqual(review_budget.usage(self.s, sid)['used']['calls'], before['used']['calls'])
        run = self.s.db.execute('SELECT id FROM protocol_runs ORDER BY rowid DESC LIMIT 1').fetchone()[0]
        self.assertEqual([event['kind'] for event in self.s.run(run)['events']],
                         ['START', 'ATTEMPT', 'RESOURCE_WAIT'])
        events = [json.loads(row[0]) for row in self.s.db.execute(
            "SELECT payload_json FROM page_audit WHERE event='RESOURCE_GATE'")]
        self.assertTrue(any(row.get('run_id') == run and row.get('measurement') == blocked for row in events))
        after = copy.deepcopy(review_budget.usage(self.s, sid))
        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        self.provider = original
        self.tier = 'high'
        with patch('provider_resources.require_resources', return_value={'admitted': True}):
            resumed = self.step(stopped, event='resume')
        self.assertEqual(resumed['state'], 'CHILDREN')
        self.assertEqual(review_budget.usage(self.s, sid), after)
        done = self.until('DONE', resumed)
        self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertEqual(self.s.pinned(sid), pin)
        self.assertEqual(machine.permissions(self.s, sid), permissions)
        self.assertEqual(len([e for e in machine.events(self.s, sibling) if e['kind'] == 'RESULT']), 1)
        self.assertTrue(self.s.audit_valid())
        self.assertTrue(self.s.verify_protocol(sid))
        self.assertTrue(self.s.verify_invocations(sid))

    def test_bridge_headroom_defers_same_child_without_dispatch_or_budget_reset(self):
        out = self.children()
        sid = self.meta['active']
        before = copy.deepcopy(review_budget.usage(self.s, sid))
        pin = copy.deepcopy(self.s.pinned(sid))
        with patch.object(child.InvocationGateway, 'invoke', side_effect=BridgeStepRequired(360, 300)):
            deferred = self.step(out)
        self.assertEqual(deferred['state'], 'CHILDREN')
        self.assertEqual(review_budget.usage(self.s, sid)['used']['calls'], before['used']['calls'])
        self.assertEqual(self.s.pinned(sid), pin)
        after = copy.deepcopy(review_budget.usage(self.s, sid))
        child_id = out['children'][0]['id']
        self.assertEqual(machine.events(self.s, child_id)[-1]['kind'], 'CHILD_REPAIR_PAUSED')
        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        self.tier = 'high'
        self.assertEqual(review_budget.usage(self.s, sid), after)
        completed = self.until('DONE', deferred)
        self.assertTrue(completed['data']['result']['admission']['admitted'])
        self.assertEqual(self.s.pinned(sid), pin)
        self.assertTrue(self.s.verify_invocations(sid))
