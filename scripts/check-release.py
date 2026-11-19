"""Offline source/package gate; no inference, installs or publishing."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import zipfile
import sys
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from package import runtime_files, verify_integrity


KERNEL_SHA256 = '9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac'
ARCHIVE_METADATA = {'extension.vsixmanifest', '[Content_Types].xml'}


def check_metadata(root):
    manifest = json.loads((root / 'package.json').read_text())
    version = manifest['version']
    assert (root / 'README.txt').read_text().startswith('Sutra ' + version + ' '), 'README.txt version'
    assert (root / 'README.md').read_text().startswith('# Sutra ' + version + ' '), 'README.md version'
    changelog = (root / 'CHANGELOG.md').read_text()
    first_line = next((line for line in changelog.splitlines() if line.strip()), '')
    assert first_line == '# Changelog', 'CHANGELOG.md must start with # Changelog'
    headings = list(re.finditer(r'^## (\d+\.\d+\.\d+)\s*$', changelog, re.MULTILINE))
    assert headings and headings[0].group(1) == version, 'First changelog release must match package.json'
    end = headings[1].start() if len(headings) > 1 else len(changelog)
    assert re.search(r'^- \S', changelog[headings[0].end():end], re.MULTILINE), 'Current changelog entry is empty'
    assert changelog.splitlines().count('# Changelog') == 1, 'Duplicate Changelog heading'
    assert 'Local models · ' + version + '</span>' in (root / 'media/panel.html').read_text(), 'Panel version'
    sources = '\n'.join(p.read_text() for p in root.glob('*.js'))
    for command in manifest['contributes']['commands']:
        assert command['command'] in sources, command
    for name in ('index.js', 'native-vscode.js', 'visual-vscode.js', 'selection-vscode.js', 'compaction-vscode.js'):
        text = (root / 'test' / name).read_text()
        assert "assert.equal(api.version,'" not in text, name
    return manifest


def check_package(root, archive_path, manifest):
    expected = {'extension/' + p.relative_to(root).as_posix() for p in runtime_files(root)}
    expected.update(ARCHIVE_METADATA)
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.testzip() is None, 'Corrupt VSIX entry'
        names = archive.namelist()
        duplicates = sorted(name for name, count in Counter(names).items() if count != 1)
        assert not duplicates, 'Duplicate VSIX entries: ' + ', '.join(duplicates)
        missing, unexpected = sorted(expected - set(names)), sorted(set(names) - expected)
        assert not missing, 'Missing runtime/package files: ' + ', '.join(missing)
        assert not unexpected, 'Unexpected VSIX files: ' + ', '.join(unexpected)
        assert json.loads(archive.read('extension/package.json')) == manifest, 'Packaged manifest differs'
        for name in names:
            if name.startswith('extension/'):
                assert archive.read(name) == (root / name[len('extension/'):]).read_bytes(), name
        assert hashlib.sha256(archive.read('extension/harness/stp_canonical/kernel.py')).hexdigest() == KERNEL_SHA256


def check_release(root, package_dir=None):
    verify_integrity(root)
    provenance = json.loads((root / 'bin/telemetry-build.json').read_text())
    for name, digest in provenance['sha256'].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest, name
    assert hashlib.sha256((root / 'harness/stp_canonical/kernel.py').read_bytes()).hexdigest() == KERNEL_SHA256
    manifest = check_metadata(root)
    if package_dir is not None:
        check_package(root, package_dir / ('Sutra-' + manifest['version'] + '.vsix'), manifest)
    return {'passed': True, 'version': manifest['version'], 'packaged': package_dir is not None,
            'kernel_sha256': KERNEL_SHA256}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package-dir', type=Path)
    args = parser.parse_args()
    print(json.dumps(check_release(ROOT, args.package_dir)))


if __name__ == '__main__':
    main()
