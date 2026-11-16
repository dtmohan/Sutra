import json
import os
import unittest
from unittest.mock import patch

from invocation_gateway import InvocationLedger
from karma_adapter import KarmaAdapter
import orchestration as machine
import orchestration_flow as flow
from review_budget import usage
import test_orchestration as fixtures
import workspace_allocation as allocation


class WorkspaceAllocationTests(unittest.TestCase):
    setUp = fixtures.OrchestrationTests.setUp
    tearDown = fixtures.OrchestrationTests.tearDown
    provider = fixtures.OrchestrationTests.provider
    step = fixtures.OrchestrationTests.step
    until = fixtures.OrchestrationTests.until

    def pause(self, out):
        budget = usage(self.s, self.meta['active'])
        next_bytes = budget['limits']['source_bytes'] - budget['used']['source_bytes'] + 1
        frame = KarmaAdapter().allocations(budget['used'], budget['limits'], {'source_bytes': next_bytes})
        self.assertNotEqual(frame['decision'], 'ALLOW')
        flow.move(self.s, out['flow_id'], 'PAUSED', 'CONTEXT_STOPPED',
                  workflow_stop={'status': 'task_budget', 'stage': 'SYNTHESIS', 'frame': frame})
        return flow.response(self.s, out['flow_id'])

    def test_measured_grant_retains_use_constraints_and_workspace_authority(self):
        out = self.pause(self.until('SYNTHESIS'))
        sid = self.meta['active']
        before = usage(self.s, sid)
        contract = self.s.pinned(sid)
        cap = before['limits']['source_bytes'] + 4096
        old = machine.node(self.s, out['execution_id'])
        with patch.dict(os.environ, {allocation.ENVIRONMENT_KEY: json.dumps({'source_bytes': cap})}):
            result = allocation.try_amend(self.s, out['flow_id'], self.meta)
            self.assertEqual(result['status'], 'amended')
            self.assertEqual(result['dimension'], 'source_bytes')
            self.assertEqual(result['amendment']['authority'], 'host_workspace_policy')
            self.assertIsNotNone(result['amendment']['receipt'])
            self.assertEqual(machine.state(self.s, old['id']), 'DONE')
            after = usage(self.s, sid)
            self.assertEqual(before['used'], after['used'])
            self.assertEqual(after['allocation_version'], 1)
            self.assertEqual(after['limits'], dict(before['limits'], source_bytes=cap))
            self.assertEqual(self.s.pinned(sid), contract)
        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        saved = flow.response(self.s, out['flow_id'])
        self.assertEqual(saved['conversation_id'], out['conversation_id'])
        new = machine.node(self.s, saved['execution_id'])
        self.assertEqual(new['previous'], old['id'])
        self.assertEqual(new['context'], old['context'])
        self.assertEqual(new['binding'], old['binding'])
        self.assertEqual(new['permissions'], old['permissions'])
        self.assertEqual(usage(self.s, sid), after)

    def test_workspace_refusal_and_non_budget_pause_do_not_grant(self):
        out = self.pause(self.until('SYNTHESIS'))
        before = usage(self.s, self.meta['active'])
        with patch.dict(os.environ, {allocation.ENVIRONMENT_KEY: json.dumps({
                'source_bytes': before['limits']['source_bytes']})}):
            self.assertEqual(allocation.try_amend(self.s, out['flow_id'], self.meta)['reason'], 'workspace_limit')
        with patch.dict(os.environ, {allocation.ENVIRONMENT_KEY: '{"enabled":false}'}):
            self.assertEqual(allocation.try_amend(self.s, out['flow_id'], self.meta)['reason'], 'policy_disabled')
        flow.move(self.s, out['flow_id'], 'PAUSED', 'RESOURCE_STOPPED',
                  workflow_stop={'status': 'resource_wait', 'resume_phase': 'SYNTHESIS'})
        self.assertIsNone(allocation.try_amend(self.s, out['flow_id'], self.meta))
        self.assertEqual(usage(self.s, self.meta['active']), before)

    def test_one_amendment_per_stop_and_no_repeat_without_progress(self):
        out = self.pause(self.until('SYNTHESIS'))
        fid = out['flow_id']
        with patch.dict(os.environ, {allocation.ENVIRONMENT_KEY: '{}'}):
            self.assertEqual(allocation.try_amend(self.s, fid, self.meta)['status'], 'amended')
            self.assertEqual(allocation.try_amend(self.s, fid, self.meta)['reason'], 'already_amended')
            saved = flow.response(self.s, fid)
            resumed = self.step(saved, event='resume')
            self.assertEqual(resumed['state'], 'SYNTHESIS')
            self.pause(resumed)
            self.assertEqual(allocation.try_amend(self.s, fid, self.meta)['reason'], 'no_progress')
        self.assertEqual(usage(self.s, self.meta['active'])['allocation_version'], 1)

    def test_context_growth_requires_a_proved_task_allocation_shortfall(self):
        out = self.until('SYNTHESIS')
        budget = usage(self.s, self.meta['active'])
        details = {'available_tokens': 10000, 'effective_context_tokens': 8192,
                   'attempt_slots_reserved': 2}
        measured = {'dimension': 'tokens', 'minimum': 2000, 'slots': 2,
                    'required_context': 6000, 'model_context': 8192}
        for invalid in ({'dimension': 'tokens', 'minimum': 2000},
                        dict(measured, required_context=9000, minimum=8000),
                        dict(measured, model_context=12000), dict(measured, minimum=1999)):
            stop = {'status': 'context_budget', 'allocation': details, 'allocation_need': invalid}
            flow.move(self.s, out['flow_id'], 'PAUSED', 'CONTEXT_STOPPED', workflow_stop=stop)
            self.assertIsNone(allocation.try_amend(self.s, out['flow_id'], self.meta))
            self.assertEqual(usage(self.s, self.meta['active']), budget)
        stop = {'status': 'context_budget', 'allocation': details, 'allocation_need': measured}
        flow.move(self.s, out['flow_id'], 'PAUSED', 'CONTEXT_STOPPED', workflow_stop=stop)
        with patch.dict(os.environ, {allocation.ENVIRONMENT_KEY: '{}'}):
            self.assertEqual(allocation.try_amend(self.s, out['flow_id'], self.meta)['dimension'], 'tokens')

    def test_bad_policy_values_fail_closed(self):
        for value in ('{', '{"actions":7}', '{"source_bytes":0}', '{"calls":true}',
                      '{"enabled":"false"}', '{"tokens":1000001}'):
            with self.subTest(value=value), patch.dict(os.environ, {allocation.ENVIRONMENT_KEY: value}):
                self.assertIsNone(allocation.policy())


if __name__ == '__main__':
    unittest.main()
