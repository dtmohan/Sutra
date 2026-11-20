"""Publication boundary unit tests with a minimal immutable accepted candidate.

The fixture supplies acceptance; these tests exercise the later shared guard,
not invocation admission or source authenticity.
"""
import copy
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate, canonical
import report_review


class CandidateStore:
    def __init__(self, marked=True, requesting=True):
        self.db = sqlite3.connect(':memory:')
        self.draft = {'report': {}, 'remaining': ['Recall context: ' + 'c' * 64] if requesting else []}
        self.source = {'request': 'Explain the architecture.'}
        if marked:
            self.source.update(retained_context_version=1, retained_context={
                'root': {'kind': 'retained_memory_root', 'ref': 'a' * 64,
                         'links': [{'ref': 'b' * 64}]},
                'pages': [{'kind': 'index', 'ref': 'b' * 64, 'links': [{'ref': 'c' * 64}]}],
            })
        self.db.executescript('''
            CREATE TABLE invocations(id TEXT,session TEXT,record_json TEXT,delivery_id TEXT);
            CREATE TABLE deliveries(id TEXT,payload_json TEXT);
            CREATE TABLE page_audit(seq INTEGER,event TEXT,payload_json TEXT);
            CREATE TABLE spending(calls INTEGER,units INTEGER);
            INSERT INTO spending VALUES(8,105329);
        ''')
        envelope = {'id': 'candidate', 'role': 'chat_reviewer', 'delivery_id': 'delivery', 'model': 'writer'}
        self.db.execute('INSERT INTO invocations VALUES(?,?,?,?)',
                        ('candidate', 'task', canonical(envelope), 'delivery'))
        self.db.execute('INSERT INTO deliveries VALUES(?,?)', ('delivery', canonical(self.source)))
        for table in ('invocations', 'deliveries', 'spending'):
            for operation in ('UPDATE', 'DELETE'):
                self.db.execute(f'''CREATE TRIGGER immutable_{table}_{operation}
                    BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable fixture'); END''')
        self.db.commit()

    def load_result(self, session, invocation_id):
        if session != 'task' or invocation_id != 'candidate':
            raise InvalidUpdate('Foreign candidate fixture')
        return copy.deepcopy(self.draft)


class ContextPublicationTests(unittest.TestCase):
    def test_manual_candidate_run_and_publication_reject_pending_recall_before_any_new_admission(self):
        store = CandidateStore()
        self.addCleanup(store.db.close)
        before = '\n'.join(store.db.iterdump())
        changes = store.db.total_changes
        with patch('api_runtime.provider_for', side_effect=AssertionError('Unexpected provider access')), \
             patch('review_context.context_budget', side_effect=AssertionError('Unexpected budget change')), \
             patch('invocation_gateway.InvocationGateway.invoke', side_effect=AssertionError('Unexpected inference')):
            operations = (
                lambda: report_review.candidate(store, 'task', 'candidate'),
                lambda: report_review.run(store, 'task', Path('/unused-fixture'), 'candidate'),
                lambda: report_review.published(store, 'task', 'candidate', 'reviewer'),
            )
            for operation in operations:
                with self.subTest(operation=operation), self.assertRaisesRegex(InvalidUpdate, 'waiting for retained context'):
                    operation()
                self.assertEqual(store.db.total_changes, changes)
                self.assertEqual('\n'.join(store.db.iterdump()), before)
                self.assertEqual(store.db.execute('SELECT calls,units FROM spending').fetchone(), (8, 105329))

    def test_unmarked_historical_remaining_text_does_not_acquire_new_control_meaning(self):
        store = CandidateStore(marked=False)
        self.addCleanup(store.db.close)
        store.draft['remaining'] = ['Recall context: an old prose recommendation, without a page address.']
        draft, envelope, source = report_review.candidate(store, 'task', 'candidate')
        self.assertEqual(draft, store.draft)
        self.assertEqual(envelope['role'], 'chat_reviewer')
        self.assertNotIn('retained_context_version', source)

    def test_marked_draft_without_pending_recall_remains_a_candidate(self):
        store = CandidateStore(requesting=False)
        self.addCleanup(store.db.close)
        draft, _, source = report_review.candidate(store, 'task', 'candidate')
        self.assertEqual(draft['remaining'], [])
        self.assertEqual(source['retained_context_version'], 1)


if __name__ == '__main__':
    unittest.main()
