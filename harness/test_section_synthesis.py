"""Section checkpoints through the real gateway with a deterministic provider."""
import json
import unittest
from unittest.mock import patch

from invocation_gateway import InvocationLedger
import orchestration as machine
import orchestration_flow as flow
from paging import Budget
import review_budget
import section_synthesis
import test_parent_section_recovery as adoption


class SectionProvider:
    def __init__(self, model, observed):
        self.model = model
        self.budget = Budget(32768, 2048, 256)
        self.observed = observed

    def call(self, cid, system, packet, schema):
        role, index, attempt = (packet[k] for k in ('section_role', 'section_index', 'attempt'))
        claim_index = packet.get('claim_index')
        view = json.loads(section_synthesis.messages(role, packet)[1]['content'])
        self.observed.append((index, role, attempt, self.model, claim_index, view))
        if role == 'section_writer':
            eid, other, *_ = packet['evidence']
            value = {'claims': [
                {'text': 'The checked source declares a function.', 'sources': [eid]},
                {'text': 'This unsupported claim must never appear.', 'sources': [other]},
                {'text': 'This truncated claim ends in the middle of', 'sources': [eid]},
            ]}
            if index == section_synthesis.ASSESSMENT_INDEX:
                for row in value['claims']:
                    row['kind'] = 'finding'
                value['claims'][0].update(kind='recommendation',
                                          text='Add a unit test for the function declared in the checked source.')
        elif index == 1 and attempt == 0 and claim_index == 0:
            value = {'verdict': 'guessed'}
        else:
            value = {'verdict': 'supported' if claim_index == 0 else 'insufficient'}
        return value, {'call_id': cid, 'ok': True,
                       'response': {'done': True, 'prompt_eval_count': 400, 'eval_count': 80}}


class SectionSynthesisTests(unittest.TestCase):
    setUp = adoption.ParentSectionRecoveryTests.setUp
    tearDown = adoption.ParentSectionRecoveryTests.tearDown
    provider = adoption.ParentSectionRecoveryTests.provider
    rows = adoption.ParentSectionRecoveryTests.rows
    step = adoption.ParentSectionRecoveryTests.step
    until = adoption.ParentSectionRecoveryTests.until
    closed = adoption.ParentSectionRecoveryTests.closed
    qualified_transport = adoption.ParentSectionRecoveryTests.qualified_transport

    def test_publishes_only_reviewed_sections_and_resumes_the_failed_stage(self):
        done = self.closed()
        sid = self.meta['active']
        historical_rows = self.rows(sid)
        original_budget = review_budget.usage(self.s, sid)
        with patch('review_upgrade.release_pair', return_value=True), self.qualified_transport(), \
                patch('remote_context.require_section_reasoning'):
            origin = flow.response(self.s, done['flow_id'])
            continued = self.step(origin, event='continue_parent_report', confirmed=True)
            fid = continued['flow_id']
            _, data = flow.info(self.s, fid)
            observed = []
            prototype = SectionProvider(data['section_plan']['model'], observed)
            self.provider = lambda _: SectionProvider(prototype.model, observed)
            ready = self.step(continued)
            self.assertFalse(ready['report_sections'])
            one_claim = self.step(ready)
            self.assertFalse(one_claim['report_sections'])
            published = self.step(one_claim)
            [first] = published['report_sections']
            self.assertIn('checked source', first['markdown'])
            self.assertNotIn('unsupported claim', first['markdown'])
            self.assertNotIn('truncated claim', first['markdown'])
            self.assertEqual(len(first['receipt_id']), 64)
            second_draft = self.step(published)
            stopped = self.step(second_draft)
            self.assertEqual(stopped['state'], 'PAUSED')
            self.assertEqual(stopped['report_sections'], [first])
            self.assertEqual(stopped['stop']['status'], 'report_section_stopped')
            charged = review_budget.usage(self.s, sid)
            self.assertEqual(charged['used']['calls'] - original_budget['used']['calls'], 5)
            self.s.close()
            self.s = InvocationLedger(self.root / 'state.sqlite')
            self.tier = 'high'
            self.assertEqual(review_budget.usage(self.s, sid), charged)
            self.assertEqual(self.step(stopped, event='inspect')['report_sections'], [first])
            final = self.until('DONE', self.step(stopped, event='resume'))
        self.assertEqual(final['flow_id'], fid)
        self.assertEqual(len(final['report_sections']), 3)
        self.assertEqual(final['report_sections'][0], first)
        self.assertNotIn('unsupported claim', final['data']['report_markdown'])
        self.assertIn('3 of 3', final['data']['report_completion_markdown'])
        self.assertEqual(self.rows(sid)[:len(historical_rows)], historical_rows)
        self.assertEqual([(i, role, attempt, claim) for i, role, attempt, _, claim, _ in observed], [
            (0, 'section_writer', 0, None), (0, 'section_reviewer', 0, 0), (0, 'section_reviewer', 0, 1),
            (1, 'section_writer', 0, None), (1, 'section_reviewer', 0, 0), (1, 'section_reviewer', 1, 0),
            (1, 'section_reviewer', 0, 1), (2, 'section_writer', 0, None),
            (2, 'section_reviewer', 0, 0), (2, 'section_reviewer', 0, 1),
        ])
        self.assertIn('**Next step** Add a unit test', final['data']['report_markdown'])
        self.assertNotIn('Actionable next steps are still outstanding.', final['data']['report_completion_markdown'])
        original_evidence = observed[0][-1]['evidence']
        self.assertGreaterEqual(len(original_evidence), 2)
        self.assertNotIn('review_only', observed[0][-1]['requirements'])
        self.assertNotIn('output_format', observed[0][-1]['requirements'])
        for review in (observed[1][-1], observed[2][-1]):
            self.assertEqual(set(review), {'claim', 'evidence'})
            [cited] = review['claim']['sources']
            self.assertEqual(review['evidence'], {cited: original_evidence[cited]})
        self.assertNotEqual(set(observed[1][-1]['evidence']), set(observed[2][-1]['evidence']))
        after = review_budget.usage(self.s, sid)
        self.assertEqual(after['limits'], original_budget['limits'])
        for counter in ('operations', 'source_bytes'):
            self.assertEqual(after['used'][counter], original_budget['used'][counter])
        execution = machine.node(self.s, final['execution_id'])
        self.assertEqual(execution['permissions'], machine.node(self.s, final['previous_execution_id'])['permissions'])
        iid = final['data']['result']['invocation_id']
        self.assertEqual(next(json.loads(raw)['role'] for key, raw, _ in self.rows(sid) if key == iid),
                         'section_reviewer')
        self.assertTrue(self.s.verify_invocations(sid))
        self.assertTrue(self.s.audit_valid())

    def test_changed_source_pauses_before_another_paid_stage(self):
        done = self.closed()
        sid = self.meta['active']
        with patch('review_upgrade.release_pair', return_value=True), self.qualified_transport(), \
                patch('remote_context.require_section_reasoning'):
            origin = flow.response(self.s, done['flow_id'])
            continued = self.step(origin, event='continue_parent_report', confirmed=True)
            _, data = flow.info(self.s, continued['flow_id'])
            observed = []
            self.provider = lambda _: SectionProvider(data['section_plan']['model'], observed)
            published = self.step(self.step(self.step(continued)))
            source = self.ws / 'part0.py'
            source.write_text(source.read_text() + '\n# A different snapshot\n')
            spent = review_budget.usage(self.s, sid)
            paused = self.step(published)
            self.assertEqual(paused['state'], 'PAUSED')
            self.assertEqual(paused['stop']['status'], 'provenance_changed')
            self.assertEqual(paused['report_sections'], published['report_sections'])
            self.assertEqual(review_budget.usage(self.s, sid), spent)
            self.assertEqual(len(observed), 3)

    def test_partial_report_does_not_publish_an_exhausted_stage_receipt(self):
        done = self.closed()
        sid = self.meta['active']
        with patch('review_upgrade.release_pair', return_value=True), self.qualified_transport(), \
                patch('remote_context.require_section_reasoning'):
            origin = flow.response(self.s, done['flow_id'])
            current = self.step(origin, event='continue_parent_report', confirmed=True)
            _, data = flow.info(self.s, current['flow_id'])
            observed = []

            class LastReviewWithheld(SectionProvider):
                def call(self, cid, system, packet, schema):
                    value, record = super().call(cid, system, packet, schema)
                    if packet['section_index'] == 2 and packet['section_role'] == 'section_reviewer':
                        if packet['claim_index'] == 0:
                            value = {'verdict': 'insufficient'}
                        else:
                            value = {}
                    return value, record

            self.provider = lambda _: LastReviewWithheld(data['section_plan']['model'], observed)
            for _ in range(20):
                current = self.step(current, event='resume' if current['state'] == 'PAUSED' else 'step')
                if current['state'] == 'DONE':
                    break
            self.assertEqual(current['state'], 'DONE')
            self.assertEqual(len(current['report_sections']), 2)
            self.assertIn('2 of 3', current['data']['report_completion_markdown'])
            self.assertIn('Actionable next steps are still outstanding.', current['data']['report_completion_markdown'])
            iid = current['data']['result']['invocation_id']
            self.assertEqual(next(json.loads(row)['status'] for key, _, row in self.rows(sid) if key == iid),
                             'accepted')
            attempts = [(stage[4], stage[2]) for stage in observed if stage[:2] == (2, 'section_reviewer')]
            self.assertEqual(attempts, [(0, 0), (1, 0), (1, 1)])


if __name__ == '__main__':
    unittest.main()
