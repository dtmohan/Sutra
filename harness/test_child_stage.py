"""Branch evidence can join without the final writer's presentation contract."""
import copy
import json
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate
import chat_runtime
import orchestration as machine
import subagent_runtime as child
import test_orchestration as fixtures


class ChildStageTests(unittest.TestCase):
    setUp = fixtures.OrchestrationTests.setUp
    tearDown = fixtures.OrchestrationTests.tearDown
    step = fixtures.OrchestrationTests.step
    until = fixtures.OrchestrationTests.until
    children = fixtures.OrchestrationTests.children
    provider = fixtures.OrchestrationTests.provider

    def architecture(self):
        self.prompt = 'Explain the project architecture and tools logic with source-backed diagrams. ' + self.prompt
        return self.children()

    def test_findings_complete_or_omitted_status_join_then_parent_writes_report(self):
        out = self.architecture()
        original = fixtures.Child.call

        def findings(worker, *args):
            proposal, record = original(worker, *args)
            proposal['answer'] = 'The supplied source defines the partition functions and their return values.'
            proposal['remaining'] = ['Trace the cross-partition entry path with additional source evidence.']
            proposal['slice_status'] = 'complete'
            return proposal, record

        with patch.object(fixtures.Child, 'call', findings):
            first = self.step(out)
        result = first['child']['result']
        self.assertTrue(result['admission']['admitted'], result)
        self.assertNotIn('report', result['accepted_output'])
        self.assertEqual(machine.state(self.s, first['children'][0]['id']), 'DONE')
        # The next branch omits slice_status, as the captured valid UI reply did.
        second = self.step(first)
        self.assertTrue(second['child']['result']['admission']['admitted'])
        self.assertNotIn('slice_status', second['child']['result']['accepted_output'])
        done = self.until('DONE', second)
        final = done['data']['result']
        self.assertTrue(final['admission']['admitted'], final)
        self.assertTrue(final['accepted_output']['report']['components'])
        self.assertTrue(final['accepted_output']['report']['flow'])
        parent = next(model for stage, model in self.models if stage == 'synthesis')
        self.assertIn('partition functions', parent.payloads[0]['child_results'][0]['answer_unverified'])
        self.assertTrue(self.s.verify_invocations(self.meta['active']))

    def test_continuation_still_needs_a_next_check_and_new_source(self):
        payload = {'contract': {'task_id': 'task', 'constraints': {}},
                   'branch': {'id': 'branch'},
                   'excerpts': {'entry.py': 'def start():\n    return run()'},
                   'child_checkpoint': [{'citations': [{'path': 'entry.py', 'quote': 'def start():'}]}]}
        proposal = {'task_id': 'task', 'branch_id': 'branch', 'constraints': [],
                    'answer': 'The entry delegates to run.', 'slice_status': 'continue',
                    'remaining': ['Inspect the next source operation.'],
                    'citations': [{'path': 'entry.py', 'quote': '    return run()'}]}
        with patch.object(child, 'request_check'):
            self.assertTrue(child.validate(None, 'task', payload, proposal)['admission']['admitted'])
            for change in ({'remaining': []},
                           {'citations': payload['child_checkpoint'][0]['citations']}):
                with self.subTest(change=change), self.assertRaisesRegex(InvalidUpdate, 'specific next step'):
                    child.validate(None, 'task', payload, dict(proposal, **change))

    def test_findings_can_exceed_parent_intro_length_within_existing_packet_allocation(self):
        out = self.architecture()
        original = fixtures.Child.call
        detail = ('The supplied partition functions return the values written in their source bodies. '
                  'These excerpts establish static definitions, while their callers and runtime ordering '
                  'need additional evidence. ') * 8
        self.assertGreater(len(detail), 1000)

        def findings(worker, *args):
            proposal, record = original(worker, *args)
            proposal['answer'] = detail
            return proposal, record

        with patch.object(fixtures.Child, 'call', findings):
            reviewed = self.step(out)
        result = reviewed['child']['result']
        self.assertTrue(result['admission']['admitted'], result)
        self.assertEqual(result['accepted_output']['answer'], detail)
        envelope = json.loads(self.s.db.execute('SELECT record_json FROM invocations WHERE id=?',
            (result['invocation_id'],)).fetchone()[0])
        self.assertEqual(envelope['schema']['properties']['answer']['maxLength'], 12000)
        budget = envelope['budget_profile']
        self.assertLessEqual(envelope['charge'] + budget['response_reserve'], budget['context_limit'])
        self.assertEqual(budget['response_reserve'], 2048)
        self.assertTrue(result['accepted_output']['citations'])

    def test_forged_child_citation_is_withheld(self):
        out = self.architecture()
        original = fixtures.Child.call

        def forged(worker, *args):
            proposal, record = original(worker, *args)
            proposal['citations'] = [{'path': 'outside.py', 'quote': 'fabricated'}]
            return proposal, record

        with patch.object(fixtures.Child, 'call', forged):
            stopped = self.step(out)
        self.assertEqual(stopped['state'], 'PAUSED')
        self.assertEqual(stopped['stop']['status'], 'child_review_rejected')
        self.assertEqual(machine.state(self.s, out['children'][0]['id']), 'QUARANTINED')

    def test_changed_child_permission_echo_is_withheld(self):
        out = self.architecture()
        original = fixtures.Child.call
        pinned = self.s.pinned(self.meta['active'])

        def forged(worker, *args):
            proposal, record = original(worker, *args)
            for row in proposal['constraints']:
                if row['key'] == 'edits_allowed':
                    row['value'] = 'true'
            return proposal, record

        with patch.object(fixtures.Child, 'call', forged):
            stopped = self.step(out)
        self.assertEqual(stopped['stop']['status'], 'child_review_rejected')
        self.assertEqual(self.s.pinned(self.meta['active']), pinned)
        self.assertFalse(pinned['constraints']['edits_allowed'])

    def test_final_parent_still_withholds_an_architecture_answer_without_report(self):
        out = self.architecture()
        provider = self.provider

        def without_report(stage):
            model = provider(stage)
            if stage == 'synthesis':
                model.fault = lambda result, packet: result.pop('report', None)
            return model

        self.provider = without_report
        done = self.until('DONE', out)
        self.assertFalse(done['data']['result']['admission']['admitted'])
        self.assertIsNone(done['data']['result']['accepted_output'])
        self.assertTrue(any('missing fields' in error for error in done['data']['result']['admission']['errors']))

    def test_historical_report_bearing_child_receipts_still_verify_and_join(self):
        out = self.architecture()
        current_schema = child.schema

        def historical_schema(payload):
            schema = current_schema(payload)
            report = chat_runtime.schema(payload)['properties']['report']
            schema['properties']['report'] = copy.deepcopy(report)
            return schema

        with patch.object(child, 'schema', historical_schema):
            saved = self.step(out)
        self.assertIn('report', saved['child']['result']['accepted_output'])
        self.assertTrue(self.s.verify_invocations(self.meta['active']))
        done = self.until('DONE', saved)
        self.assertTrue(done['data']['result']['admission']['admitted'])
        parent = next(model for stage, model in self.models if stage == 'synthesis')
        self.assertIn('Main components', parent.payloads[0]['child_results'][0]['answer_unverified'])


if __name__ == '__main__':
    unittest.main()
