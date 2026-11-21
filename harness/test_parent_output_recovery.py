"""Output-limit recovery retains the paid writer's exact input evidence."""
import copy
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import chat_runtime
from context_manager import InvalidUpdate, canonical
from model_packet import DERIVED_REVIEW_SUMMARY_VERSION, host_fields
import orchestration
from paging import Budget, PageFault
from remote_context import PARENT_REASONING_IDENTITY, wire_request
import report_review


class ParentOutputRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.original = {
            'contract': {'task_id': 'task', 'goal': 'Review the project architecture', 'constraints': {}},
            'request': 'Explain the architecture.',
            'constraint_echo': [],
            'excerpts': {'harness/one.py': 'def sample():\n    return True\n'},
            'route': {'record': {'choice': {'model': 'sutra:remote/benchmark'}}},
            'report_schema_version': 2,
            'snapshot_owner': 'old',
            'evidence_discovery_version': 4,
            'child_results': [{'invocation_id': 'c' * 32}],
            'parent_repair': {'nonce': 'a' * 32, 'attempt_limit': 3, 'owner': 'old'},
        }
        window = dict(PARENT_REASONING_IDENTITY, version=1, provider='sutra_remote',
                      context_tokens=32768, reference='sutra:remote/benchmark',
                      model='sutra-benchmark', port=18092, profile_hash='3' * 64,
                      defaults_sha256='4' * 64)
        self.budget = Budget(32768, 8192, 256, context_profile=2,
                             reservation_limit=53976, context_window=window)
        self.original['memory_budget'] = vars(self.budget)
        self.invocation_id = '1' * 32
        self.receipt = '2' * 64
        self.failure = {'id': self.invocation_id, 'budget_profile': vars(self.budget)}

    def recovered(self):
        remaining = self.budget.with_limits(reservation_limit=90000)
        return chat_runtime._parent_output_packet(self.original, self.invocation_id, self.receipt, remaining)

    def test_recovery_schema_is_compact_without_rewriting_legacy_schema(self):
        old = chat_runtime.schema(self.original)
        new = chat_runtime.schema(self.recovered())
        self.assertNotIn('maxItems', old['properties']['report']['properties']['flow'])
        self.assertEqual(new['properties']['report']['properties']['flow']['maxItems'], 5)
        self.assertEqual(new['properties']['report']['properties']['components']['maxItems'], 4)
        self.assertEqual(old['properties']['citations']['items'], new['properties']['citations']['items'])
        self.assertEqual(old, chat_runtime.schema(self.original))

    def test_new_admission_cannot_change_context_or_replace_failed_receipt(self):
        packet = self.recovered()
        original = copy.deepcopy(self.original)
        with patch('chat_runtime._parent_output_failure', return_value=(self.failure, original, self.receipt)):
            self.assertEqual(chat_runtime._check_parent_draft_recovery(None, 'session', packet), original)
            for change in (
                {'snapshot_owner': 'unrelated-branch'},
                {'child_results': []},
                {'excerpts': {'new.py': 'An unapproved replacement'}},
                {'parent_draft_recovery': dict(packet['parent_draft_recovery'], failed_receipt='0' * 64)},
                {'memory_budget': dict(packet['memory_budget'], response_reserve=4096)},
            ):
                with self.subTest(change=change), self.assertRaisesRegex(InvalidUpdate, 'saved evidence or allocation'):
                    chat_runtime._check_parent_draft_recovery(None, 'session', dict(packet, **change))
        self.assertEqual(self.original, original)

    def test_shape_repair_changes_only_summary_contract_and_feedback(self):
        initial = self.recovered()
        report = {
            'overview': 'A source-backed overview. ' * 14, 'strengths': [], 'risks': [], 'next_steps': [],
            'components': [{'name': 'Sample', 'responsibility': 'Produces a flag.', 'paths': ['harness/one.py']}],
            'flow': [{'description': 'The sample function returns the constant True.', 'paths': ['harness/one.py']}],
        }
        proposal = {'answer': 'The report describes the sample.', 'summary': 'a' * 320, 'complete': False,
                    'task_id': 'task', 'constraints': [], 'remaining': [], 'report': report,
                    'citations': [{'path': 'harness/one.py', 'quote': 'def sample():'}]}
        receipt = {'admission': {'errors': ['response.summary: string length 320 outside 1..240 characters; '
                                             'select a shorter exact quote when quoting source']},
                   'proposal': proposal}
        env = {'model': 'sutra:remote/benchmark', 'schema': chat_runtime.schema(initial),
               'budget_profile': initial['memory_budget'], 'orchestration': {'node_id': 'successor'}}
        source = dict(self.failure, model=env['model'])
        repair_id = '3' * 32
        repair_rec = '4' * 64
        with patch('orchestration.node', return_value={'id': 'successor', 'previous': 'old'}), \
                patch('parent_synthesis.linked_to', return_value=True), \
                patch('chat_runtime._parent_output_receipt', return_value=(env, initial, receipt, repair_rec)):
            repair = chat_runtime._parent_shape_repair(None, 'sid', repair_id, source, self.original, self.receipt)
            self.assertEqual(repair, {'version': 1, 'invocation_id': repair_id, 'receipt': repair_rec})
            retry = chat_runtime._parent_output_packet(self.original, self.invocation_id, self.receipt,
                                                       self.budget, repair=repair, derive_summary=True)
            old_format, new_format = chat_runtime.schema(initial), chat_runtime.schema(retry)
            wire, _, _ = wire_request(self.budget.context_window, chat_runtime.SYSTEM, retry,
                                      new_format, self.budget.response_reserve)
            wire_fields = wire['response_format']['json_schema']['schema']['properties']
            self.assertNotIn('summary', wire_fields)
            self.assertEqual(wire_fields['report'], old_format['properties']['report'])
            self.assertEqual(len(host_fields('chat_reviewer', initial, old_format,
                                            DERIVED_REVIEW_SUMMARY_VERSION, proposal)['summary']), 320)
            self.assertEqual(len(host_fields('chat_reviewer', retry, new_format,
                                            DERIVED_REVIEW_SUMMARY_VERSION, proposal)['summary']),
                             new_format['properties']['summary']['maxLength'])
            self.assertNotIn(proposal['summary'], canonical(wire))
            receipt['proposal']['complete'] = True
            with self.assertRaisesRegex(InvalidUpdate, 'response.complete'):
                chat_runtime._parent_shape_repair(None, 'sid', repair_id, source, self.original, self.receipt)

    def test_original_quota_includes_successor_and_unsettled_dispatches(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.executescript('''
            CREATE TABLE invocations(id TEXT, session TEXT, delivery_id TEXT, record_json TEXT);
            CREATE TABLE deliveries(id TEXT, payload_json TEXT);
            CREATE TABLE invocation_receipts(invocation_id TEXT, record_json TEXT);
        ''')
        store = SimpleNamespace(db=db)
        saved = self.original['parent_repair']

        def dispatch(iid, owner, status):
            db.execute('INSERT INTO invocations VALUES (?, ?, ?, ?)',
                       (iid, 'sid', iid, canonical({'role': 'chat_reviewer', 'orchestration': {'node_id': owner}})))
            db.execute('INSERT INTO deliveries VALUES (?, ?)', (iid, canonical(self.original)))
            if status:
                db.execute('INSERT INTO invocation_receipts VALUES (?, ?)', (iid, canonical({'status': status})))

        def owner(_, nid):
            return {'id': nid, 'previous': 'old' if nid == 'successor' else None}

        with patch('orchestration.node', side_effect=owner), \
                patch('parent_synthesis.linked_to', side_effect=lambda _, n, prev: n.get('previous') == prev):
            dispatch('1', 'old', 'quarantined')
            chat_runtime._parent_output_attempts(store, 'sid', saved)
            dispatch('2', 'successor', None)
            with self.assertRaisesRegex(InvalidUpdate, 'already in progress'):
                chat_runtime._parent_output_attempts(store, 'sid', saved)
            db.execute('INSERT INTO invocation_receipts VALUES (?, ?)', ('2', canonical({'status': 'quarantined'})))
            chat_runtime._parent_output_attempts(store, 'sid', saved)
            dispatch('3', 'successor', 'quarantined')
            with self.assertRaisesRegex(InvalidUpdate, 'allocation is exhausted'):
                chat_runtime._parent_output_attempts(store, 'sid', saved)

    def test_quarantined_summary_candidate_requires_exact_committed_flow(self):
        marker = {'version': 1, 'candidate_id': 'draft', 'failed_invocation': 'failed'}
        scope = {'flow_id': 'new-flow', 'active_root': 'writer', 'current': {'session': 'sid'}}
        event = {'kind': 'PARENT_REPORT_CONTINUATION',
                 'data': {'parent_summary_revalidation': marker, 'review_policy_upgrade': scope}}
        draft = {'report': {'overview': 'Validated from the original receipt.'}}
        store = SimpleNamespace(_review_upgrade=scope)

        def quarantined(*_):
            raise PageFault('Invocation result quarantined')

        store.load_result = quarantined
        with patch('orchestration.events', return_value=[event]), \
                patch('review_upgrade.recorded', return_value=scope) as recorded, \
                patch('chat_runtime._parent_summary_candidate', return_value=(marker, draft, {}, {})) as rebuild:
            with orchestration.scope('unrelated'):
                with self.assertRaisesRegex(PageFault, 'quarantined'):
                    report_review.candidate(store, 'sid', 'draft')
            with orchestration.scope('writer'):
                with self.assertRaisesRegex(PageFault, 'quarantined'):
                    report_review.candidate(store, 'sid', 'unrelated')
                self.assertEqual(report_review.candidate(store, 'sid', 'draft'), (draft, {}, {}))
                rebuild.assert_called_once_with(store, 'sid', 'failed', 'draft')
                rebuild.return_value = (dict(marker, draft_hash='different'), draft, {}, {})
                with self.assertRaisesRegex(InvalidUpdate, 'committed descriptor'):
                    report_review.candidate(store, 'sid', 'draft')
                recorded.return_value = None
                with self.assertRaisesRegex(InvalidUpdate, 'changed its flow binding'):
                    report_review.candidate(store, 'sid', 'draft')
