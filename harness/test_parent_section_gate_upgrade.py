"""Keep a paid section draft across the exact pre-dispatch gate fix."""
import copy
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate
from invocation_gateway import InvocationLedger
from monitoring import ResourceWait
from paging import PageFault
import orchestration as machine
import orchestration_flow as flow
import review_budget
import review_upgrade
import test_section_synthesis as fixture


class ParentSectionGateUpgradeTests(unittest.TestCase):
    tearDown = fixture.SectionSynthesisTests.tearDown
    provider = fixture.SectionSynthesisTests.provider
    rows = fixture.SectionSynthesisTests.rows
    step = fixture.SectionSynthesisTests.step
    until = fixture.SectionSynthesisTests.until
    closed = fixture.SectionSynthesisTests.closed
    qualified_transport = fixture.SectionSynthesisTests.qualified_transport

    def setUp(self):
        fixture.SectionSynthesisTests.setUp(self)
        reasoning = patch('remote_context.require_section_reasoning')
        reasoning.start()
        self.addCleanup(reasoning.stop)

    def stage(self, **override):
        original = self.closed()
        self.s.close()
        with patch('governance.policy_hash', return_value=review_upgrade.SECTION_GATE_PREDECESSOR), \
                patch('review_upgrade.release_pair', return_value=True), self.qualified_transport():
            self.s = InvocationLedger(self.root / 'state.sqlite')
            notice = flow.response(self.s, original['flow_id'])
            continued = self.step(notice, event='continue_parent_report', confirmed=True)
            fid = continued['flow_id']
            _, data = flow.info(self.s, fid)
            model = data['section_plan']['model']
            observed = []
            self.provider = lambda _: fixture.SectionProvider(model, observed)
            draft = self.step(continued)
            self.assertFalse(draft['report_sections'])
            self.assertEqual([event[1] for event in observed], ['section_writer'])
            state = {'model': model, 'profile': 'physical-context-v1', 'provider': 'sutra_remote',
                     'available': False, 'admitted': False, 'blocker': 'measurement_unavailable',
                     'runtime': {'model': model, 'kind': 'remote_observation',
                                 'stage': 'remote_section_reasoning', 'inference_started': False}}
            state['runtime'].update(override)
            with patch('section_synthesis.step', side_effect=ResourceWait(state)):
                stopped = self.step(draft)
            self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        return original, stopped, observed

    def test_uses_the_same_flow_execution_and_paid_writer_after_upgrade(self):
        original, stopped, observed = self.stage()
        fid, sid, owner = stopped['flow_id'], self.meta['active'], stopped['execution_id']
        budget = review_budget.usage(self.s, sid)
        rows = self.rows(sid)
        permissions = machine.node(self.s, owner)['permissions']
        with patch('review_upgrade.release_pair', return_value=True), self.qualified_transport():
            notice = flow.response(self.s, fid)
            self.assertEqual(notice['recovery']['kind'], 'compatible_review_upgrade')
            with self.assertRaises(InvalidUpdate):
                self.step(notice, event='restart_review', confirmed=False)
            upgraded = self.step(notice, event='restart_review', confirmed=True)
            stamp = review_upgrade.recorded(self.s, fid)
            [writer] = stamp['section_gate_results']
            self.assertEqual(stamp['section_gate_stop_receipt'], machine.events(self.s, fid)[-2]['receipt'])
            with self.assertRaises(PageFault):
                self.s.load_result(sid, writer)
            self.assertEqual(upgraded['execution_id'], owner)
            self.assertEqual(self.rows(sid), rows)
            self.assertEqual(review_budget.usage(self.s, sid), budget)
            self.assertEqual(machine.state(self.s, original['execution_id']), 'DONE')
            self.assertEqual(machine.node(self.s, owner)['permissions'], permissions)
            with patch('provider_resources.recover', return_value=None), patch('provider_resources.require_resources'):
                resumed = self.step(upgraded, event='resume')
            for _ in range(6):
                if resumed['report_sections']:
                    break
                resumed = self.step(resumed)
            self.assertEqual(len(resumed['report_sections']), 1)
            self.assertEqual([event[1] for event in observed], ['section_writer', 'section_reviewer', 'section_reviewer'])
            self.assertEqual(review_budget.usage(self.s, sid)['used']['calls'], budget['used']['calls'] + 2)
            self.assertEqual(review_upgrade.recorded(self.s, fid), stamp)
            _, data = flow.info(self.s, fid)
            tampered = copy.deepcopy(data)
            tampered['review_policy_upgrade']['section_gate_results'][writer] = 'f' * 64
            self.assertIsNone(review_upgrade.recorded(self.s, fid, tampered))
            self.assertTrue(self.s.verify_invocations(sid))
            self.assertTrue(self.s.audit_valid())

    def test_rejects_a_stop_after_generation_started(self):
        _, stopped, _ = self.stage(inference_started=True)
        with patch('review_upgrade.release_pair', return_value=True), self.qualified_transport():
            _, data = flow.info(self.s, stopped['flow_id'])
            self.assertIsNone(review_upgrade.offer(self.s, stopped['flow_id'], data))


if __name__ == '__main__':
    unittest.main()
