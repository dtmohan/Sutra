"""Regression tests for receipt-backed catalog exclusions."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate
from invocation_gateway import InvocationLedger
import repository_tools as repo
from vscode_bridge import handle


class CatalogExclusionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.store = self.root / 'store'

    def source(self, path, data=b'x = 1\n'):
        target = self.workspace / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def review(self, paths, automatic=False):
        result = handle(self.store, self.workspace, {
            'op': 'auto_new' if automatic else 'review_new',
            'goal': 'Review the scoped files',
            'roots': paths,
        })
        ledger = InvocationLedger(self.store / 'state.sqlite')
        self.addCleanup(ledger.close)
        return result, ledger, result['session']

    def execute(self, ledger, sid, kind, **kwargs):
        return repo.execute(ledger, sid, self.workspace, self.store, kind,
                            resource_check=False, **kwargs)

    def test_manual_catalog_commits_size_exclusion_and_denies_without_spending(self):
        self.source('src/a.py')
        self.source('src/large.py', b'#' * 2400000)
        self.source('unscoped.py', b'#' * 2400000)
        result, ledger, sid = self.review(['src'])
        self.assertEqual(result['architecture']['catalog'], ['src/a.py'])
        self.assertEqual(result['architecture']['excluded'], {'src/large.py': 'too_large'})
        receipt = repo.records(ledger, sid)[-1]
        self.assertEqual(receipt['result']['excluded'], {'src/large.py': 'too_large'})
        before = repo.usage(ledger, sid)['used']
        for kind in ('read', 'static_map', 'api_index'):
            with self.subTest(kind=kind), self.assertRaisesRegex(InvalidUpdate, 'too_large'):
                self.execute(ledger, sid, kind, target='src/large.py')
        with self.assertRaisesRegex(InvalidUpdate, 'outside committed catalog'):
            self.execute(ledger, sid, 'read', target='unscoped.py')
        self.assertEqual(repo.usage(ledger, sid)['used'], before)

    def test_explicit_oversized_file_creates_an_empty_diagnostic_catalog(self):
        self.source('large.py', b'#' * (repo.MAX_FILE + 1))
        result, ledger, sid = self.review(['large.py'])
        self.assertEqual(result['architecture']['catalog_files'], 0)
        self.assertEqual(repo.catalog_snapshot(ledger, sid), {
            'files': [], 'excluded': {'large.py': 'too_large'},
        })
        with self.assertRaisesRegex(InvalidUpdate, 'too_large'):
            self.execute(ledger, sid, 'read', target='large.py')
        self.assertEqual(repo.usage(ledger, sid)['used'], 1)

    def test_automatic_catalog_keeps_large_files_for_page_allocation(self):
        self.source('large.py', b'#' * (repo.MAX_FILE + 1))
        self.source('explicit.png', b'unsupported bytes')
        _, ledger, sid = self.review(['large.py', 'explicit.png'], automatic=True)
        self.assertEqual(repo.catalog_snapshot(ledger, sid), {
            'files': ['large.py'], 'excluded': {'explicit.png': 'unsupported_extension'},
        })

    @unittest.skipIf(os.name == 'nt', 'Symlink creation can require Windows developer mode')
    def test_link_exclusions_use_local_paths_without_following_link_targets(self):
        self.source('src/a.py')
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'secret.py').write_text('SECRET = 1\n')
        (self.workspace / 'src' / 'escape').symlink_to(outside, target_is_directory=True)
        (self.workspace / 'src' / 'cross.py').symlink_to(outside / 'secret.py')
        os.link(outside / 'secret.py', self.workspace / 'src' / 'hard.py')
        _, ledger, sid = self.review(['src/a.py', 'src/cross.py', 'src/hard.py',
                                      'src/escape/wanted.py'])
        snapshot = repo.catalog_snapshot(ledger, sid)
        self.assertEqual(snapshot['files'], ['src/a.py'])
        self.assertEqual(snapshot['excluded'], {
            'src/cross.py': 'symlink', 'src/hard.py': 'hard_link',
            'src/escape/wanted.py': 'symlink',
        })
        self.assertNotIn('secret.py', str(snapshot))
        with self.assertRaisesRegex(InvalidUpdate, 'symlink'):
            self.execute(ledger, sid, 'read', target='src/escape/wanted.py')
        with self.assertRaisesRegex(InvalidUpdate, 'outside committed catalog'):
            self.execute(ledger, sid, 'read', target='src/escape/secret.py')

    def test_binary_detection_stays_in_governed_reads(self):
        self.source('bin.py', b'\x00\xff')
        result, ledger, sid = self.review(['bin.py'])
        self.assertEqual(result['architecture']['catalog'], ['bin.py'])
        read = self.execute(ledger, sid, 'read', target='bin.py')
        self.assertEqual(read['result']['status'], 'failed')
        self.assertEqual(read['result']['excerpts'], [])
        self.assertIn('Binary file rejected', read['result']['error'])
        self.source('bin.py')
        self.assertEqual(self.execute(ledger, sid, 'read', target='bin.py')['result']['status'], 'ok')

    def test_legacy_catalog_receipt_without_exclusion_field_loads(self):
        self.source('a.py')
        settle = repo.finish

        def legacy_finish(ledger, operation, digest, result, elapsed):
            result.pop('excluded')
            return settle(ledger, operation, digest, result, elapsed)

        with patch('repository_tools.finish', side_effect=legacy_finish):
            result, ledger, sid = self.review(['a.py'])
        self.assertEqual(result['architecture']['excluded'], {})
        self.assertEqual(repo.catalog_snapshot(ledger, sid), {'files': ['a.py'], 'excluded': {}})
