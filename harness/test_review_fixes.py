"""Regressions for the independently reported 0.20.2 findings."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import host_actions
from context_manager import InvalidUpdate, sha
from kernel_boundary import KernelBoundary
from karma_adapter import KarmaAdapter
from smriti_adapter import SmritiAdapter
from paging import PagedContext
import repository_tools as repo
import test_architecture as fixtures


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.text = 'VALUE = 1\n'
        (self.root / 'a.py').write_text(self.text)
        self.constraints = {'edits_allowed': True, 'allowed_paths': ['a.py'],
                            'dependencies_allowed': False}
        self.payload = {'pages': [{'path': 'a.py', 'text': self.text}],
                        'source_versions': {'a.py': sha(self.text)}}

    def tearDown(self):
        self.tmp.cleanup()

    def propose(self, module, **kw):
        action = {'target': 'a.py', 'old': self.text,
                  'new': 'import ' + module + '\n' + self.text}
        return host_actions.proposed_edit(self.root, self.constraints, action, self.payload, **kw)

    def test_compiled_stdlib_imports_are_allowed_without_installation(self):
        for name in ('math', 'zlib', 'select', 'mmap', 'resource', 'termios'):
            with self.subTest(name=name):
                self.propose(name)
        self.assertEqual((self.root / 'a.py').read_text(), self.text)

    def test_python39_discovery_uses_extension_suffixes_without_site_packages(self):
        stdlib = self.root / 'stdlib'
        native = stdlib / 'lib-dynload'
        native.mkdir(parents=True)
        (stdlib / 'json').mkdir()
        (stdlib / 'json/__init__.py').write_text('raise Exception("must not import")')
        (stdlib / 'pure.py').touch()
        (native / ('compiled' + host_actions.EXTENSION_SUFFIXES[0])).touch()
        (stdlib / 'site-packages').mkdir()
        (stdlib / 'site-packages/thirdparty.py').touch()
        existing = getattr(sys, 'stdlib_module_names', None)
        try:
            if existing is not None:
                del sys.stdlib_module_names
            with patch('host_actions.sysconfig.get_paths', return_value={'stdlib': str(stdlib)}), \
                 patch('host_actions.sysconfig.get_config_var', return_value=str(native)):
                names = host_actions.stdlib_modules()
            self.assertTrue({'json', 'pure', 'compiled'} <= names)
            self.assertFalse({'thirdparty', 'site-packages'} & names)
        finally:
            if existing is not None:
                sys.stdlib_module_names = existing

    def test_readable_local_import_does_not_grant_edit_access(self):
        (self.root / 'helper.py').write_text('raise Exception("must not import")')
        self.propose('helper', read_scope=['helper.py'])
        self.assertEqual(self.constraints['allowed_paths'], ['a.py'])
        with self.assertRaises(InvalidUpdate):
            self.propose('helper', read_scope=['a.py'])
        with self.assertRaises(InvalidUpdate):
            self.propose('uninstalled_third_party', read_scope=['.'])
        with self.assertRaises(InvalidUpdate):
            host_actions.proposed_edit(self.root, self.constraints,
                                      {'target': 'helper.py', 'old': 'raise', 'new': 'pass #'}, self.payload,
                                      read_scope=['.'])


class ReadCursorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ArchitectureTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def read(self, **kw):
        return self.fixture.tool('read', target='entry.py', **kw)

    def test_unicode_crlf_and_long_lines_are_reconstructed_exactly(self):
        for text in ('é' * 950 + 'TAIL\r\nNEXT\r\n', 'x' * 1300, 'short\nlast'):
            with self.subTest(text_length=len(text)):
                # A separate task allocation for each test document.
                self.fixture.tearDown()
                self.fixture.setUp()
                (self.fixture.ws / 'entry.py').write_bytes(text.encode())
                cursor = None
                chunks = []
                for _ in range(4):
                    r = self.read(line=cursor['line'] if cursor else 1, cursor=cursor)['result']
                    self.assertEqual(r['status'], 'ok')
                    chunks.append(r['excerpts'][0]['text'])
                    self.assertLessEqual(len(chunks[-1].encode()), 1200)
                    cursor = r['next_cursor']
                    self.assertEqual(r['truncated'], cursor is not None)
                    if cursor is None:
                        break
                self.assertIsNone(cursor)
                self.assertEqual(''.join(chunks), text)

    def test_source_changes_and_cursor_replay_are_rejected(self):
        path = self.fixture.ws / 'entry.py'
        path.write_text('x' * 1600 + '\n')
        first = self.read(request_id='first')
        cursor = first['result']['next_cursor']
        self.assertEqual(cursor['line'], 1)
        self.assertEqual(repo.feedback(self.fixture.s, self.fixture.sid)['next_cursor'], cursor)
        with self.assertRaisesRegex(InvalidUpdate, 'changed continuation'):
            self.read(request_id='first', cursor=cursor)
        path.write_text('y' * 1600 + '\n')
        changed = self.read(cursor=cursor)['result']
        self.assertEqual(changed['status'], 'failed')
        self.assertIn('Source changed', changed['error'])
        self.assertFalse(changed['excerpts'])

    def test_governed_read_continues_after_model_switch(self):
        text = '#' + 'x' * 1600 + '\nTAIL = 7\n'
        (self.fixture.ws / 'entry.py').write_text(text)
        first_model = fixtures.Model()
        first = self.fixture.step(first_model)
        cursor = first['tool_receipt']['result']['next_cursor']
        used = repo.usage(self.fixture.s, self.fixture.sid)['used']

        def continue_read(proposal):
            proposal['tool'].update(cursor=cursor, line=cursor['line'])
            for finding in proposal['findings']:
                finding['quote'] = finding['quote'][:240]

        next_model = fixtures.Model(fault=continue_read)
        next_model.model = 'fixture-B'
        second = self.fixture.step(next_model)
        self.assertTrue(second['result']['admission']['admitted'])
        self.assertEqual(first_model.payloads[0]['contract'], next_model.payloads[0]['contract'])
        self.assertEqual(repo.usage(self.fixture.s, self.fixture.sid)['used'], used + 1)
        chunks = [row['tool_receipt']['result']['excerpts'][0]['text'] for row in (first, second)]
        self.assertEqual(''.join(chunks), text)
        self.assertTrue(self.fixture.s.verify_invocations(self.fixture.sid))

    def test_invalid_positions_and_non_read_cursors_are_rejected(self):
        for column in (-1, True, 0.5):
            with self.assertRaises(InvalidUpdate):
                self.read(cursor={'line': 1, 'column': column, 'sha256': 'a' * 64})
        with self.assertRaises(InvalidUpdate):
            self.fixture.tool('list', cursor={'line': 1, 'column': 0, 'sha256': 'a' * 64})


class AuditVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.sqlite'
        self.s = PagedContext(self.path)
        with self.s.db:
            self.s._audit('FIRST', {'value': 1})
        self.assertTrue(self.s.audit_valid())

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def test_unchanged_view_is_reused_but_append_is_revalidated(self):
        with patch('paging.sha', wraps=sha) as digest:
            self.assertTrue(self.s.audit_valid())
            self.assertEqual(digest.call_count, 0)
            with self.s.db:
                self.s._audit('NEXT', {'value': 2})
            digest.reset_mock()
            self.assertTrue(self.s.audit_valid())
            self.assertEqual(digest.call_count, 2)

    def test_same_connection_tampering_invalidates_verified_view(self):
        self.s.db.execute('DROP TRIGGER immutable_page_audit_UPDATE')
        with self.s.db:
            self.s.db.execute("UPDATE page_audit SET payload_json='{}' WHERE seq=1")
        self.assertFalse(self.s.audit_valid())

    def test_other_connection_tampering_and_reopen_are_rechecked(self):
        with sqlite3.connect(self.path) as other:
            other.execute('DROP TRIGGER immutable_page_audit_UPDATE')
            other.execute("UPDATE page_audit SET payload_json='{}' WHERE seq=1")
        self.assertFalse(self.s.audit_valid())
        self.s.close()
        self.s = PagedContext(self.path)
        self.assertFalse(self.s.audit_valid())

    def test_rollback_and_database_replacement_do_not_reuse_a_stamp(self):
        self.s._audit('ROLLED_BACK', {})
        self.assertTrue(self.s.audit_valid())
        self.s.db.rollback()
        with patch('paging.sha', wraps=sha) as digest:
            self.assertTrue(self.s.audit_valid())
            self.assertEqual(digest.call_count, 1)
        replacement = Path(self.tmp.name) / 'replacement.sqlite'
        other = PagedContext(replacement)
        other.close()
        os.replace(replacement, self.path)
        self.assertFalse(self.s.audit_valid())


class AdapterThresholdTests(unittest.TestCase):
    def test_fits_and_exhaustion_mean_the_same_for_nondefault_thresholds(self):
        for k in (.05, .15, .3, .9):
            for sigma_clause in (.5, 1., 2.):
                boundary = KernelBoundary(k, sigma_clause)
                karma, smriti = KarmaAdapter(boundary), SmritiAdapter(boundary)
                for cost in (0, 50, 90, 91, 180, 181):
                    with self.subTest(k=k, sigma_clause=sigma_clause, cost=cost):
                        a = karma.allocations({'tokens': 10}, {'tokens': 100}, {'tokens': cost})
                        b = karma.storage(10, 100, cost)
                        c = smriti.context(cost, 10, 100)
                        self.assertEqual(a['decision'] == 'ALLOW', cost <= 90)
                        self.assertEqual(b['decision'] == 'ALLOW', cost <= 90)
                        self.assertEqual(c['decision'] == 'ALLOW', cost <= 90)
                        self.assertEqual(a['k'], k)
                self.assertEqual(karma.counted_slots(100, 100)['decision'], 'COMPOUND_FIN')
                self.assertEqual(karma.allocations({'tokens': 100}, {'tokens': 100})['decision'], 'COMPOUND_FIN')
                self.assertTrue(smriti.freshness(10, 10)['expired'])


if __name__ == '__main__':
    unittest.main()
