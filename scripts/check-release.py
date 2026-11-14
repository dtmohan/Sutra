"""Offline source/package gate; no inference, installs or publishing."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from package import verify_integrity


root = Path(__file__).resolve().parent.parent
parser = argparse.ArgumentParser()
parser.add_argument('--package-dir', type=Path)
args = parser.parse_args()
manifest = json.loads((root / 'package.json').read_text())
version = manifest['version']
verify_integrity(root)
provenance = json.loads((root / 'bin/telemetry-build.json').read_text())
for name, digest in provenance['sha256'].items():
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest, name
kernel = '9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac'
assert hashlib.sha256((root / 'harness/stp_canonical/kernel.py').read_bytes()).hexdigest() == kernel
assert (root / 'README.txt').read_text().startswith('Sutra ' + version)
assert '## ' + version in (root / 'CHANGELOG.md').read_text()
assert 'Local models · ' + version + '</span>' in (root / 'media/panel.html').read_text()
sources = '\n'.join(p.read_text() for p in root.glob('*.js'))
for command in manifest['contributes']['commands']:
    assert command['command'] in sources, command
for name in ('index.js', 'native-vscode.js', 'visual-vscode.js', 'selection-vscode.js', 'compaction-vscode.js'):
    text = (root / 'test' / name).read_text()
    assert "assert.equal(api.version,'" not in text, name
if args.package_dir:
    with zipfile.ZipFile(args.package_dir / ('Sutra-' + version + '.vsix')) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
        assert json.loads(archive.read('extension/package.json')) == manifest
        assert 'extension/THIRD_PARTY_NOTICES.md' in names
        assert not any(n.startswith(('extension/test/', 'extension/docs/', 'extension/scripts/')) for n in names)
        for name in names:
            if not name.startswith('extension/'):
                continue
            assert archive.read(name) == (root / name[len('extension/'):]).read_bytes(), name
        assert hashlib.sha256(archive.read('extension/harness/stp_canonical/kernel.py')).hexdigest() == kernel
print(json.dumps({'passed': True, 'version': version, 'packaged': bool(args.package_dir), 'kernel_sha256': kernel}))
