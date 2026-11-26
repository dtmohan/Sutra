"""Section checkpoints through the real gateway with a deterministic provider."""
import copy
import json
import unittest
from unittest.mock import patch

from invocation_gateway import InvocationLedger
from context_manager import InvalidUpdate
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


class ProseProvider:
    def __init__(self, model, observed):
        self.model = model
        self.budget = Budget(65536, 8192, 256)
        self.context_metadata = {'model_info': {'fixture.context_length': 65536}}
        self.observed = observed

    def call(self, cid, system, packet, schema):
        role = packet['section_role']
        view = json.loads(section_synthesis.messages(role, packet)[1]['content'])
        self.observed.append((role, packet['section_index'], packet.get('claim_index'), view))
        if role == 'section_writer':
            def paragraph(index, *sentences, **fields):
                eid = packet['section_sources'][index][0]
                return dict(fields, sentences=[{'text': text, 'sources': [eid]} for text in sentences])
            overview_id = packet['section_sources'][0][0]
            value = {'sections': {
                '0': [
                    paragraph(0, 'The module names a function “part0.”',
                              f'Its body returns a literal integer when the function is called [{overview_id}].'),
                    paragraph(0, 'A plausible sentence accompanies an unsupported statement here.',
                              'The function also records telemetry in a remote database.'),
                ],
                '1': [paragraph(1, 'Calling the inspected function returns its literal integer.'),
                      paragraph(1, 'An incomplete paragraph with missing final punctuation')],
                '2': [
                    paragraph(2, 'The implementation returns a literal integer.',
                              'Add a unit test for that exact return value.', kind='recommendation'),
                    paragraph(2, 'Replace the function with an unrelated queue.', kind='recommendation'),
                ],
            }}
            value['sections']['0'][0]['sentences'][1]['sources'].append(overview_id)
        else:
            verdicts = {i: ('insufficient' if 'remote database' in row['text'] else 'supported')
                        for i, row in view['sentences'].items()}
            value = {'verdicts': verdicts}
            if view['kind'] == 'recommendation':
                value['recommendation'] = 'supported' if len(verdicts) > 1 else 'insufficient'
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

    def test_proactive_prose_uses_one_draft_and_only_publishes_complete_supported_paragraphs(self):
        self.ws = self.ws.resolve()
        self.root = self.root.resolve()
        self.meta['workspace'] = str(self.ws)
        self.prompt = 'Explain the architecture and request flow. ' + self.prompt
        ready = self.until('SYNTHESIS')
        self.provider = lambda _: None
        sid = self.meta['active']
        budget = review_budget.usage(self.s, sid)
        before = self.rows(sid)
        owner = machine.node(self.s, ready['execution_id'])
        observed = []
        make_plan = section_synthesis.make_prose_plan

        def legacy_plan(ledger, session, source, model, **_):
            from chat_runtime import conversation_pages
            source['memory'] = conversation_pages(ledger, source, Budget(**source['memory_budget']))
            return make_plan(ledger, session, source, model)

        with patch('remote_profiles.is_remote', return_value=True), \
                patch('chat_runtime.prose_review_supported', return_value=True), \
                patch('section_synthesis.make_prose_plan', side_effect=legacy_plan), \
                patch('remote_context.require_section_reasoning'), \
                patch('provider_resources.require_resources', return_value={}), \
                patch('api_runtime.provider_for', side_effect=lambda _, model, **__: ProseProvider(model, observed)), \
                patch('review_discovery.advance', return_value={'complete': True, 'followups': {}}):
            planned = self.step(ready)
            fid = planned['flow_id']
            _, saved = flow.info(self.s, fid)
            self.assertEqual(saved['section_plan']['version'], section_synthesis.PROSE_VERSION)
            self.assertEqual(saved['section_source']['request'], self.prompt)
            self.assertEqual(review_budget.usage(self.s, sid)['used']['calls'], budget['used']['calls'])
            self.assertEqual(self.rows(sid), before)
            altered = copy.deepcopy(saved['section_source'])
            altered['_request_nonce'] = '0' * 32
            with self.assertRaisesRegex(InvalidUpdate, 'source binding changed'):
                section_synthesis.validate_plan(self.s, sid, saved['section_plan'], source=altered)
            draft = self.step(planned)
            self.assertFalse(draft['report_sections'])
            one_review = self.step(draft)
            self.assertFalse(one_review['report_sections'])
            settled = review_budget.usage(self.s, sid)
            self.s.close()
            self.s = InvocationLedger(self.root / 'state.sqlite')
            self.assertEqual(review_budget.usage(self.s, sid), settled)
            self.assertEqual(self.step(one_review, event='inspect')['report_sections'], [])
            done = self.until('DONE', one_review)
        self.assertEqual(done['flow_id'], fid)
        self.assertEqual(done['execution_id'], ready['execution_id'])
        rows = done['report_sections']
        self.assertEqual(len(rows), 3)
        overview = rows[0]['markdown']
        self.assertIn('The module names a function “part0.” Its body returns a literal integer', overview)
        self.assertNotRegex(overview, r'e[0-9a-f]{12}')
        self.assertEqual(overview.count('file:///'), 1, 'Repeated source links appear only once per paragraph')
        self.assertNotIn('\n- ', overview)
        markdown = done['data']['report_markdown']
        for hidden in ('plausible sentence', 'remote database', 'unrelated queue', 'An incomplete paragraph'):
            self.assertNotIn(hidden, markdown)
        self.assertIn('**Next step.** The implementation returns a literal integer. Add a unit test', markdown)
        self.assertIn('Withheld 3 of the draft paragraphs', done['data']['report_completion_markdown'])
        self.assertEqual([(role, section, paragraph) for role, section, paragraph, _ in observed], [
            ('section_writer', 0, None),
            ('section_reviewer', 0, 0), ('section_reviewer', 0, 1),
            ('section_reviewer', 1, 0),
            ('section_reviewer', 2, 0), ('section_reviewer', 2, 1),
        ])
        self.assertEqual(observed[0][-1]['request'], self.prompt)
        for _, _, _, review in observed[1:]:
            for row in review['sentences'].values():
                self.assertEqual(len(row['sources']), len(set(row['sources'])))
            cited = {eid for row in review['sentences'].values() for eid in row['sources']}
            self.assertEqual(set(review['evidence']), cited)
        self.assertEqual(self.rows(sid)[:len(before)], before)
        self.assertEqual(machine.node(self.s, done['execution_id'])['permissions'], owner['permissions'])
        self.assertTrue(self.s.verify_invocations(sid))
        self.assertTrue(self.s.audit_valid())

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
