import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate
import document_source
import repository_tools


class DocumentSourceTests(unittest.TestCase):
    name = 'Veena_Vinodkumar_Post_Silicon_Resume_ATS (1).docx'
    raw = b'PK\x03\x04\x00fixture'

    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.workspace = Path(workspace.name)

    def write(self, relative):
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.raw)
        return path

    def test_bare_filename_resolves_metadata_without_reading(self):
        target = 'attachments/' + self.name
        self.write(target)
        self.write('node_modules/' + self.name)
        self.write('.git/' + self.name)
        with patch('document_source.read_bytes', side_effect=AssertionError('Document opened during lookup')):
            self.assertEqual(
                document_source.locate_document(self.workspace, self.name),
                {'status': 'found', 'paths': [target], 'sizes': {target: len(self.raw)}},
            )

    def test_ambiguous_basename_and_exact_path(self):
        first, second = ('drafts/' + self.name, 'final/' + self.name)
        for path in (second, first):
            self.write(path)
        result = document_source.locate_document(self.workspace, self.name)
        self.assertEqual(result['status'], 'ambiguous')
        self.assertEqual(result['paths'], [first, second])
        found = document_source.locate_document(self.workspace, second)
        self.assertEqual(found['status'], 'found')
        self.assertEqual(found['paths'], [second])

    def test_missing_unsupported_and_unsafe_targets(self):
        self.assertEqual(document_source.locate_document(self.workspace, self.name)['status'], 'missing')
        for target in ('/tmp/cv.docx', '../cv.docx', 'node_modules/cv.docx', 'cv.pdf', 'cv.doc'):
            with self.subTest(target=target):
                self.assertEqual(document_source.locate_document(self.workspace, target)['status'], 'blocked')
                with self.assertRaises(InvalidUpdate):
                    document_source.read_document(self.workspace, target)

    def test_absolute_workspace_path_is_exact_and_returns_a_relative_target(self):
        workspace = self.workspace.resolve()
        targets = (self.name, 'attachments/' + self.name)
        for target in targets:
            self.write(target)
        with patch('document_source.MAX_SCAN_ENTRIES', 0), \
                patch('document_source.read_bytes', side_effect=AssertionError('Document opened during lookup')):
            for target in targets:
                with self.subTest(target=target):
                    self.assertEqual(
                        document_source.locate_document(workspace, str(workspace / target)),
                        {'status': 'found', 'paths': [target], 'sizes': {target: len(self.raw)}},
                    )
            self.assertEqual(document_source.locate_document(
                workspace, str(workspace / 'missing' / self.name))['status'], 'missing')
        with self.assertRaises(InvalidUpdate):
            document_source.read_document(workspace, str(workspace / self.name))

    def test_absolute_lookup_blocks_outside_paths_and_noncanonical_spellings(self):
        workspace = self.workspace.resolve()
        outside = workspace.with_name(workspace.name + '-outside')
        for target in (str(outside / self.name), f'{workspace}/../{workspace.name}/{self.name}',
                       f'{workspace}/./{self.name}', f'{workspace}//{self.name}'):
            with self.subTest(target=target), \
                    patch('document_source._directory', side_effect=AssertionError('Invalid target was probed')):
                self.assertEqual(document_source.locate_document(workspace, target)['status'], 'blocked')

    def test_scan_limit_requires_explicit_relative_target(self):
        target = 'documents/' + self.name
        self.write(target)
        with patch('document_source.MAX_SCAN_ENTRIES', 0):
            self.assertEqual(document_source.locate_document(self.workspace, self.name)['status'], 'scan_limit')
            self.assertEqual(document_source.locate_document(self.workspace, target)['status'], 'found')

    def test_scoped_binary_read_keeps_source_validation_and_byte_limit(self):
        self.write(self.name)
        self.assertEqual(document_source.read_document(self.workspace, self.name), self.raw)
        with self.assertRaisesRegex(InvalidUpdate, 'needs'):
            document_source.read_document(self.workspace, self.name, limit=len(self.raw) - 1)
        with self.assertRaisesRegex(InvalidUpdate, 'Binary'):
            repository_tools.read_bytes(self.workspace, self.name)

    def test_symlinks_and_hard_links_are_excluded_and_cannot_replace_an_approved_path(self):
        target = 'documents/' + self.name
        original = self.write(target)
        self.write('real/' + self.name)
        self.assertEqual(document_source.locate_document(self.workspace, target)['status'], 'found')
        original.unlink()
        try:
            original.symlink_to(self.workspace / 'real' / self.name)
            (self.workspace / 'redirect').symlink_to(self.workspace / 'real', target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f'Symlinks unavailable: {exc}')
        for path in (target, 'redirect/' + self.name):
            self.assertEqual(document_source.locate_document(self.workspace, path)['status'], 'blocked')
            self.assertEqual(document_source.locate_document(
                self.workspace, str(self.workspace.resolve() / path))['status'], 'blocked')
            with self.assertRaises(InvalidUpdate):
                document_source.read_document(self.workspace, path)
        original.unlink()
        os.link(self.workspace / 'real' / self.name, original)
        self.assertEqual(document_source.locate_document(self.workspace, self.name)['status'], 'blocked')
        self.assertEqual(document_source.locate_document(
            self.workspace, str(self.workspace.resolve() / target))['status'], 'blocked')
        with self.assertRaisesRegex(InvalidUpdate, 'hard links|unlinked'):
            document_source.read_document(self.workspace, target)


if __name__ == '__main__':
    unittest.main()
