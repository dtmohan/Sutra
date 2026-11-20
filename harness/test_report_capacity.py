"""Versioned narrative capacity keeps source choices and legacy replay strict."""
import copy
import hashlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import claim_pages as pages
from context_manager import InvalidUpdate, canonical, sha
from invocation_roles import validate_shape
from model_identity import compare, unknown
import report_review as review
import review_report as report


class ReportCapacityTests(unittest.TestCase):
    def setUp(self):
        self.source = {
            'contract': {'task_id': 'task-1', 'goal': 'Architecture review', 'constraints': {}},
            'workspace': '/fixture', 'constraint_echo': [],
            'excerpts': {f'part{i}.py': f'def part{i}():\n    return {i}\n' for i in range(8)},
        }
        self.claim_packet = {
            'review_protocol': 2, 'contract': self.source['contract'], 'constraint_echo': [],
            'claims': {'c': {'text': 'The module returns a value.', 'paths': ['part0.py']},
                       'empty': {'text': 'Needs more evidence.', 'paths': ['missing.py']}},
            'source_evidence': {'e0': {'path': 'part0.py', 'text': 'one'},
                                'e1': {'path': 'part0.py', 'text': 'two'},
                                'e2': {'path': 'part0.py', 'text': 'three'},
                                'foreign': {'path': 'part1.py', 'text': 'other'}},
        }
        self.large_report = {
            'overview': 'The delivered modules each return a local value.',
            'components': [{'name': f'Part {i}', 'responsibility': 'Returns a local value.',
                            'paths': list(self.source['excerpts'])} for i in range(8)],
            'flow': [{'description': f'The caller executes part {i} and receives its local value.',
                      'paths': ['part0.py']} for i in range(19)],
            'strengths': [{'observation': f'Part {i} has a local return.',
                           'implication': 'Its visible operation can be inspected directly.',
                           'paths': ['part0.py']} for i in range(7)],
            'risks': [{'observation': f'Part {i} has no visible error example.',
                       'implication': 'Failure behavior needs further evidence.',
                       'paths': ['part0.py']} for i in range(8)],
            'next_steps': [f'Inspect the caller of part {i} in a subsequent source page.' for i in range(9)],
        }

    def test_absent_version_reconstructs_exact_historical_schemas(self):
        values = [
            (report.schema(self.source), '0c9d1fa68c686a802a00f9703acd92a5dd94cee604022032afa9897318cab1fd'),
            (report.schema(), '8ebe6d9dc6766cb8de834d372d88d80c4639f6f0bd708962c4eca6364f99ad4e'),
            (review.schema(self.claim_packet), 'bdb9c4dbb399624c5d33906896500ae081aa4a0030a2442ec34b4b839a722c30'),
        ]
        for schema, digest in values:
            self.assertEqual(hashlib.sha256(canonical(schema).encode()).hexdigest(), digest)
        with self.assertRaises(InvalidUpdate):
            validate_shape(self.large_report, report.schema(self.source))
        legacy = dict(self.claim_packet, review_protocol=1, excerpts=self.source['excerpts'])
        self.assertEqual(review.schema(legacy)['properties']['claims']['properties']['c']
                         ['properties']['evidence']['maxItems'], 2)

    def test_new_report_retains_all_narrative_entries_without_section_caps(self):
        p = dict(self.source, report_schema_version=2, request='Explain the architecture.',
                 workspace_map={'files': list(self.source['excerpts'])})
        fmt = report.schema(p)
        validate_shape(self.large_report, fmt)
        report.validate(p, {'report': self.large_report})
        for section in ('components', 'flow', 'strengths', 'risks', 'next_steps'):
            self.assertNotIn('maxItems', fmt['properties'][section])
        rendered = report.markdown({'report': self.large_report})
        self.assertIn('Part 7', rendered)
        self.assertIn('19. The caller executes part 18', rendered)
        self.assertIn(self.large_report['next_steps'][-1], rendered)
        claims = review.claims(self.large_report, self.source['excerpts'])
        self.assertEqual(len(claims), 1 + sum(len(self.large_report[s]) for s in
                         ('components', 'flow', 'strengths', 'risks', 'next_steps')))
        self.assertIn('next_steps/8', claims)

    def test_new_paths_use_actual_delivered_choices_and_keep_text_source_checks(self):
        p = dict(self.source, report_schema_version=2, request='Architecture review',
                 workspace_map={'files': list(self.source['excerpts']) + ['unread.py']})
        fmt = report.schema(p)
        paths = fmt['properties']['components']['items']['properties']['paths']
        self.assertEqual(paths['maxItems'], len(self.source['excerpts']))
        self.assertEqual(paths['items']['enum'], sorted(self.source['excerpts']))
        for change in ('unread', 'excess_paths', 'long_text', 'code_fence'):
            value = copy.deepcopy(self.large_report)
            if change == 'unread': value['components'][0]['paths'] = ['unread.py']
            elif change == 'excess_paths': value['components'][0]['paths'].append('part0.py')
            elif change == 'long_text': value['components'][0]['responsibility'] = 'x' * 501
            else: value['next_steps'][0] = 'Inspect the caller around ``` before deciding further.'
            with self.subTest(change=change), self.assertRaises(InvalidUpdate):
                validate_shape(value, fmt)
                report.validate(p, {'report': value})

    def test_diagram_limits_presentation_only_and_announces_first_of_total(self):
        proposal = {'report': copy.deepcopy(self.large_report)}
        before = copy.deepcopy(proposal)
        diagram = report.flow_diagram(proposal)
        self.assertEqual(len(diagram['nodes']), 16)
        self.assertEqual(len(diagram['edges']), 15)
        self.assertIn('First 16 of 19 report steps shown.', diagram['note'])
        self.assertEqual(proposal, before)
        self.assertIn('19. The caller executes part 18', report.markdown(proposal))

    def test_new_evidence_cardinality_uses_applicable_supplied_choices(self):
        p = dict(self.claim_packet, evidence_choice_version=2)
        fmt = review.schema(p)
        c = fmt['properties']['claims']['properties']['c']['properties']['evidence_ids']
        self.assertEqual(c['maxItems'], 3)
        self.assertEqual(c['items']['enum'], ['e0', 'e1', 'e2'])
        value = {'task_id': 'task-1', 'constraints': [], 'complete': False,
                 'claims': {'c': {'verdict': 'supported', 'reason': 'The supplied blocks show the return.',
                                  'evidence_ids': ['e0', 'e1', 'e2']},
                            'empty': {'verdict': 'insufficient_evidence', 'reason': 'No relevant source supplied.',
                                      'evidence_ids': []}}}
        validate_shape(value, fmt)
        with patch.object(review, 'request_check', return_value={}):
            self.assertTrue(review.validate(None, None, p, value)['admission']['admitted'])
            duplicate = copy.deepcopy(value)
            duplicate['claims']['c']['evidence_ids'] = ['e0', 'e0']
            with self.assertRaisesRegex(InvalidUpdate, 'Duplicate evidence IDs'):
                review.validate(None, None, p, duplicate)
            missing = copy.deepcopy(value)
            missing['claims']['c']['evidence_ids'] = []
            with self.assertRaisesRegex(InvalidUpdate, 'requires source evidence'):
                review.validate(None, None, p, missing)
        with self.assertRaises(InvalidUpdate):
            validate_shape(value, review.schema(self.claim_packet))
        for ids in (['foreign'], ['unknown'], ['e0', 'e1', 'e2', 'e0']):
            invalid = copy.deepcopy(value); invalid['claims']['c']['evidence_ids'] = ids
            with self.subTest(ids=ids), self.assertRaises(InvalidUpdate): validate_shape(invalid, fmt)
        empty = fmt['properties']['claims']['properties']['empty']['properties']['evidence_ids']
        self.assertEqual(empty['maxItems'], 0)

    def test_claim_flag_is_derived_and_cannot_be_enabled_on_legacy_candidate(self):
        draft = {'report': self.large_report}
        cid = 'c' * 32
        all_claims = review.claims(self.large_report, self.source['excerpts'])
        evidence = pages.inventory(cid, self.source)
        plan = {'id': 'plan-1', 'version': pages.VERSION, 'candidate_id': cid,
                'candidate_hash': sha(canonical(draft)), 'source_hash': pages.source_hash(self.source),
                'contract': self.source['contract'], 'strict': False, 'attempts_per_page': 2,
                'pages': [{'claims': list(all_claims), 'evidence': list(evidence)}]}
        separation = compare(unknown('writer'), unknown('reviewer'))
        store = SimpleNamespace(validation_contract=lambda sid: self.source['contract'])
        for version in (None, 2):
            source = dict(self.source)
            if version: source['report_schema_version'] = version
            p = pages.packet(source, draft, cid, plan, 0, all_claims, evidence, separation, False)
            self.assertEqual(p.get('evidence_choice_version'), version)
            if version is None: self.assertNotIn('evidence_choice_version', p)
            env = {'model': 'writer'}
            with patch.object(review, 'candidate', return_value=(draft, env, source)), \
                 patch.object(pages, 'find', return_value=plan):
                self.assertEqual(review.request_check(store, 'sid', p), draft)
                changed = copy.deepcopy(p)
                if version: changed.pop('evidence_choice_version')
                else: changed['evidence_choice_version'] = 2
                with self.assertRaisesRegex(InvalidUpdate, 'binding changed'):
                    review.request_check(store, 'sid', changed)


class ClaimPageStepTests(unittest.TestCase):
    def setUp(self):
        from test_chat import ChatTests
        ChatTests.setUp(self)

    def tearDown(self):
        from test_chat import ChatTests
        ChatTests.tearDown(self)

    def test_one_page_steps_reload_switch_model_and_publish_only_when_complete(self):
        import review_budget
        from invocation_gateway import InvocationLedger
        from paging import Budget
        from test_chat import ChatTests, Fake
        with patch.object(review, 'run', side_effect=InvalidUpdate('Defer claims')):
            draft = ChatTests.ask(self, 'Explain the architecture.', tier='low', provider=Fake())
        cid = draft['result']['invocation_id']
        pinned = self.s.pinned(self.sid)
        before = review_budget.usage(self.s, self.sid)
        previous = 0
        first_receipt = None
        for turn in range(100):
            model = Fake('reviewer-' + str(turn))
            model.budget = Budget(6000, 128, 256)
            result = review.run(self.s, self.sid, self.storage, cid, provider=model, max_pages=1)
            progress = result['review_pages']
            self.assertEqual(progress['completed'], previous + 1)
            self.assertEqual(progress['reused'], previous)
            self.assertEqual(len(model.payloads), 1)
            self.assertEqual(result['token_usage']['total_calls'], 1)
            self.assertEqual(model.payloads[0]['candidate_id'], cid)
            if first_receipt is None:
                first_receipt = self.s.db.execute('SELECT record_json FROM invocation_receipts WHERE invocation_id=?',
                                                (result['invocation_id'],)).fetchone()[0]
                first_id = result['invocation_id']
            else:
                self.assertEqual(self.s.db.execute('SELECT record_json FROM invocation_receipts WHERE invocation_id=?',
                                                  (first_id,)).fetchone()[0], first_receipt)
            previous = progress['completed']
            if 'claim_review_pending' not in result:
                break
            pending = result['claim_review_pending']
            self.assertEqual(pending['candidate_id'], cid)
            self.assertEqual(pending['plan_id'], progress['plan_id'])
            self.assertEqual(pending['review_page'], previous + 1)
            with self.assertRaisesRegex(InvalidUpdate, 'incomplete'):
                review.published(self.s, self.sid, cid, result['invocation_id'])
            self.s.close()
            self.s = InvocationLedger(self.storage / 'state.sqlite')
        else:
            self.fail('Claim-page steps did not complete')
        self.assertGreater(previous, 1)
        self.assertEqual(progress['completed'], progress['total'])
        published, metadata = review.published(self.s, self.sid, cid, result['invocation_id'])
        self.assertTrue(published['report'])
        self.assertEqual(metadata['review_page_count'], previous)
        self.assertEqual(self.s.pinned(self.sid), pinned)
        after = review_budget.usage(self.s, self.sid)
        self.assertEqual(after['limits'], before['limits'])
        self.assertEqual(after['used']['calls'], before['used']['calls'] + previous)
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_invalid_page_step_fails_before_candidate_or_provider_access(self):
        for value in (0, -1, True, 1.5, '1'):
            with self.subTest(value=value), self.assertRaisesRegex(InvalidUpdate, 'positive integer'):
                review.run(None, None, None, None, max_pages=value)


if __name__ == '__main__': unittest.main()
