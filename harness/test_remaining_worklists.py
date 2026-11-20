"""Unverified follow-ups use response/note capacity, not a fixed item count."""
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate
from invocation_gateway import InvocationLedger
from invocation_roles import validate_shape
from local_model import OutputLimitExceeded
from paging import Budget
import architecture_role
import chat_runtime
import coding_role
import orchestration as machine
import review_budget
import review_role
import test_architecture as architecture_fixture
import test_chat as chat_fixture
import test_host_actions as coding_fixture
import test_orchestration as child_fixture


def followups(count):
    return [f'Inspect source behavior for follow-up {index + 1}.' for index in range(count)]


class WorklistSchemaTests(unittest.TestCase):
    def test_informational_count_has_no_schema_cap_but_authority_fields_stay_bounded(self):
        payload = {'contract': {'task_id': 'fixture', 'goal': 'Explain the supplied code.',
                               'constraints': {'allowed_paths': ['entry.py']}},
                   'constraint_echo': [{'key': 'allowed_paths', 'value': 'entry.py'}],
                   'request': 'Explain source.', 'mode': 'review',
                   'pages': [{'id': 'entry-page'}], 'excerpts': {'entry.py': 'VALUE = 1\n'},
                   'evidence': [{'id': 'entry-page', 'path': 'entry.py', 'text': 'VALUE = 1\n'}],
                   'repository': {'budget': {'decision': 'ALLOW'}}, 'phase': 'explore'}
        for builder in (chat_runtime.schema, review_role.review_schema,
                        architecture_role.schema, coding_role.coding_schema):
            with self.subTest(role=builder.__module__):
                fields = builder(payload)['properties']
                self.assertNotIn('maxItems', fields['remaining'])
                for count in (5, 10):
                    validate_shape(followups(count), fields['remaining'])
                self.assertEqual(fields['constraints']['minItems'], 1)
                self.assertEqual(fields['constraints']['maxItems'], 1)
                self.assertEqual(fields['complete']['enum'], [False])
        self.assertEqual(chat_runtime.schema(payload)['properties']['citations']['maxItems'], 4)


class ChatWorklistTests(unittest.TestCase):
    setUp = chat_fixture.ChatTests.setUp
    tearDown = chat_fixture.ChatTests.tearDown
    ask = chat_fixture.ChatTests.ask

    def test_five_and_ten_followups_survive_receipts_reload_and_model_switch(self):
        contract = self.s.pinned(self.sid)
        for count, tier in ((5, 'low'), (10, 'high')):
            expected = followups(count)
            model = chat_fixture.Fake(chat_fixture.MODELS[tier],
                                      lambda result, _: result.update(remaining=expected))
            before = review_budget.usage(self.s, self.sid)
            result = self.ask(tier=tier, provider=model)['result']
            self.assertTrue(result['admission']['admitted'], result)
            self.assertEqual(result['accepted_output']['remaining'], expected)
            self.assertEqual(len(model.payloads), 1, 'Useful notes must not trigger schema repair')
            after = review_budget.usage(self.s, self.sid)
            self.assertEqual(after['limits'], before['limits'])
            self.assertEqual(after['used']['calls'], before['used']['calls'] + 1)
            self.s.close()
            self.s = InvocationLedger(self.storage / 'state.sqlite')
            self.assertEqual(self.s.load_result(self.sid, result['invocation_id'])['remaining'], expected)
            self.assertEqual(self.s.pinned(self.sid), contract)
            self.assertTrue(self.s.verify_invocations(self.sid))

    def test_individual_note_length_is_still_enforced_without_truncation(self):
        model = chat_fixture.Fake(fault=lambda result, _: result.update(remaining=['x' * 161]))
        result = self.ask(provider=model)['result']
        self.assertFalse(result['admission']['admitted'])
        self.assertTrue(any('response.remaining[]' in error for error in result['admission']['errors']))
        self.assertEqual(self.s.db.execute(
            "SELECT COUNT(*) FROM invocation_receipts WHERE json_extract(record_json,'$.status')='accepted' AND invocation_id=?",
            (result['invocation_id'],)).fetchone()[0], 0)

    def test_model_output_limit_still_withholds_an_incomplete_response(self):
        class Limited(chat_fixture.Fake):
            budget = Budget(32768, 512, 256)

            def call(self, call_id, *args):
                super().call(call_id, *args)
                error = OutputLimitExceeded('Fixture output budget reached before complete JSON')
                error.call_record = {'call_id': call_id, 'ok': False, 'response': {
                    'done': True, 'done_reason': 'length', 'prompt_eval_count': 100,
                    'eval_count': self.budget.response_reserve}}
                raise error

        model = Limited(fault=lambda result, _: result.update(remaining=followups(10)))
        before = review_budget.usage(self.s, self.sid)
        result = self.ask(provider=model)['result']
        self.assertFalse(result['admission']['admitted'])
        self.assertEqual(result['failure_kind'], 'output_limit')
        after = review_budget.usage(self.s, self.sid)
        self.assertEqual(after['limits'], before['limits'])
        self.assertGreater(after['used']['calls'], before['used']['calls'])
        self.assertTrue(self.s.verify_invocations(self.sid))


class ChildWorklistTests(unittest.TestCase):
    setUp = child_fixture.OrchestrationTests.setUp
    tearDown = child_fixture.OrchestrationTests.tearDown
    step = child_fixture.OrchestrationTests.step
    until = child_fixture.OrchestrationTests.until
    children = child_fixture.OrchestrationTests.children
    provider = child_fixture.OrchestrationTests.provider

    def test_valid_continue_checkpoint_keeps_all_followups_and_resumes_on_another_model(self):
        # Keep real unread body lines for the continuation under full-span
        # paging instead of asking the model to revisit an exhausted tiny file.
        for index in range(4):
            body = ''.join(f'    value += {line}\n' for line in range(180))
            (self.ws / f'part{index}.py').write_text(
                f'def part{index}():\n    value = {index}\n' + body + '    return value\n')
        flow = self.children()
        child = flow['children'][0]['id']
        contract = self.s.pinned(self.meta['active'])
        original = child_fixture.Child.call
        expected = followups(5)

        def checkpoint(model, *args):
            proposal, record = original(model, *args)
            proposal.update(slice_status='continue', remaining=expected)
            return proposal, record

        with patch.object(child_fixture.Child, 'call', checkpoint):
            saved = self.step(flow)
        result = saved['child']['result']
        self.assertTrue(result['admission']['admitted'], result)
        delivered = self.models[-1][1].payloads[0]
        self.assertTrue(any(span['next_line'] <= len((self.ws / path).read_text().splitlines())
                            for path, span in delivered['excerpt_spans'].items()))
        self.assertEqual(saved['child']['slice_status'], 'checkpoint')
        self.assertEqual(machine.state(self.s, child), 'READY')
        history = machine.slice_history(self.s, child, machine.slice_cursor(self.s, child))
        self.assertEqual(history[-1]['output']['remaining'], expected)
        charged = review_budget.usage(self.s, self.meta['active'])
        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        self.tier = 'high'
        completed = self.until('DONE', saved)
        self.assertTrue(completed['data']['result']['admission']['admitted'])
        self.assertEqual(self.s.pinned(self.meta['active']), contract)
        self.assertEqual(review_budget.usage(self.s, self.meta['active'])['limits'], charged['limits'])
        self.assertGreater(review_budget.usage(self.s, self.meta['active'])['used']['calls'], charged['used']['calls'])
        self.assertEqual(self.s.load_result(self.meta['active'], result['invocation_id'])['remaining'], expected)
        self.assertTrue(self.s.verify_invocations(self.meta['active']))


class ArchitectureWorklistTests(unittest.TestCase):
    setUp = architecture_fixture.ArchitectureTests.setUp
    tearDown = architecture_fixture.ArchitectureTests.tearDown
    step = architecture_fixture.ArchitectureTests.step

    def test_architecture_report_preserves_ten_short_followups(self):
        self.step(architecture_fixture.Model())
        self.step(architecture_fixture.Model('read', 'broker.py'))
        expected = followups(10)
        model = architecture_fixture.Model('report', '', fault=lambda result: result.update(remaining=expected))
        result = self.step(model)['result']
        self.assertTrue(result['admission']['admitted'], result)
        self.assertEqual(self.s.load_result(self.sid, result['invocation_id'])['remaining'], expected)

    def test_aggregate_architecture_note_allocation_is_still_enforced(self):
        model = architecture_fixture.Model(fault=lambda result: result.update(remaining=['x' * 160] * 20))
        result = self.step(model)['result']
        self.assertFalse(result['admission']['admitted'])
        self.assertTrue(any('1600 bytes' in error for error in result['admission']['errors']))


class CodingWorklistTests(unittest.TestCase):
    setUp = coding_fixture.ActionTests.setUp
    tearDown = coding_fixture.ActionTests.tearDown
    propose = coding_fixture.ActionTests.propose

    def test_review_action_keeps_all_followups_without_granting_an_operation(self):
        expected = followups(10)
        model = coding_fixture.Coder(fault=lambda result: result.update(remaining=expected))
        result = self.propose(mode='review', model=model)['result']
        self.assertTrue(result['admission']['admitted'], result)
        self.assertEqual(self.s.load_result(self.session, result['invocation_id'])['remaining'], expected)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM host_actions').fetchone()[0], 0)

    def test_aggregate_coder_note_allocation_is_still_enforced(self):
        model = coding_fixture.Coder(fault=lambda result: result.update(remaining=['x' * 160] * 20))
        result = self.propose(mode='review', model=model)['result']
        self.assertFalse(result['admission']['admitted'])
        self.assertTrue(any('oversized' in error for error in result['admission']['errors']))


if __name__ == '__main__':
    unittest.main()
