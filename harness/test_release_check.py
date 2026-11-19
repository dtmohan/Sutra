"""Release gates reject a damaged artifact, rather than merely checking its survivors."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('sutra_release_check', ROOT / 'scripts/check-release.py')
RELEASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RELEASE)


class ReleaseCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = {'version': '1.2.3', 'contributes': {'commands': []}}
        self.files = {}
        for name, body in {
                'package.json': json.dumps(self.manifest),
                'harness/stp_canonical/kernel.py': (ROOT / 'harness/stp_canonical/kernel.py').read_text(),
                'harness/engine_control.py': 'owned worker entrypoint\n',
                'harness/engine_codec.py': 'owned codec\n',
                'vendor/sutra_engine/LICENSE': 'third-party notice\n',
                'THIRD_PARTY_NOTICES.md': 'notices\n'}.items():
            file = self.root / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(body)
            self.files['extension/' + name] = file.read_bytes()

    def archive(self, omit=None, extra=None, duplicate=None):
        archive = self.root / 'test.vsix'
        with zipfile.ZipFile(archive, 'w') as bundle:
            for name in RELEASE.ARCHIVE_METADATA:
                bundle.writestr(name, 'metadata')
            for name, body in self.files.items():
                if name != omit:
                    bundle.writestr(name, body)
            if extra:
                bundle.writestr(extra, 'unreviewed file')
            if duplicate:
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', UserWarning)
                    bundle.writestr(duplicate, self.files[duplicate])
        return archive

    def validate(self, archive):
        with patch.object(RELEASE, 'runtime_files', return_value={
                self.root / name[len('extension/'):] for name in self.files}):
            RELEASE.check_package(self.root, archive, self.manifest)

    def test_complete_artifact_passes(self):
        self.validate(self.archive())

    def test_missing_worker_codec_or_notice_fails_even_when_surviving_bytes_match(self):
        for name in ('extension/harness/engine_control.py', 'extension/harness/engine_codec.py',
                     'extension/vendor/sutra_engine/LICENSE', 'extension/THIRD_PARTY_NOTICES.md'):
            with self.subTest(name=name), self.assertRaisesRegex(AssertionError, 'Missing runtime/package files'):
                self.validate(self.archive(omit=name))

    def test_duplicate_and_unexpected_entries_fail(self):
        with self.assertRaisesRegex(AssertionError, 'Duplicate'):
            self.validate(self.archive(duplicate='extension/harness/engine_control.py'))
        with self.assertRaisesRegex(AssertionError, 'Unexpected'):
            self.validate(self.archive(extra='extension/test/accidentally-shipped.py'))

    def test_real_runtime_discovery_includes_owned_processes_codec_and_license(self):
        names = {file.relative_to(ROOT).as_posix() for file in RELEASE.runtime_files(ROOT)}
        self.assertTrue({'harness/engine_control.py', 'harness/engine_service.py', 'harness/engine_codec.py',
                         'requirements-engine.txt', 'vendor/sutra_engine/LICENSE'} <= names)

    def metadata_fixture(self, changelog):
        for name, body in {'README.md': '# Sutra 1.2.3 — Current\n',
                           'README.txt': 'Sutra 1.2.3 — Current\n', 'CHANGELOG.md': changelog,
                           'media/panel.html': 'Local models · 1.2.3</span>'}.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        for name in ('index.js', 'native-vscode.js', 'visual-vscode.js', 'selection-vscode.js', 'compaction-vscode.js'):
            path = self.root / 'test' / name
            path.parent.mkdir(exist_ok=True)
            path.write_text('// no hardcoded release contract\n')

    def test_current_changelog_is_first_under_single_document_heading(self):
        self.metadata_fixture('# Changelog\n\n## 1.2.3\n\n- Current change.\n\n## 1.2.2\n\n- Earlier.\n')
        self.assertEqual(RELEASE.check_metadata(self.root), self.manifest)
        for text in ('## 1.2.3\n\n- Current.\n\n# Changelog\n',
                     '# Changelog\n\n## 1.2.2\n\n- Old.\n\n## 1.2.3\n\n- Current.\n',
                     '# Changelog\n\n## 1.2.3\n\n## 1.2.2\n\n- Old.\n',
                     '# Changelog\n\n## 1.2.3\n\n- Current.\n\n# Changelog\n'):
            with self.subTest(text=text):
                self.metadata_fixture(text)
                with self.assertRaises(AssertionError):
                    RELEASE.check_metadata(self.root)


if __name__ == '__main__':
    unittest.main()
