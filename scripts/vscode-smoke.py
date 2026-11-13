"""Test an extracted VSIX in a temporary VS Code profile, without installing it."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

source = Path(__file__).resolve().parent.parent
parser = argparse.ArgumentParser()
parser.add_argument('--package-dir', type=Path, required=True)
parser.add_argument('--code', default=os.environ.get('SUTRA_VSCODE') or shutil.which('code'))
args = parser.parse_args()
if not args.code:
    parser.error('Set SUTRA_VSCODE to the VS Code command executable')
out = args.package_dir.resolve()
version = json.loads((source / 'package.json').read_text())['version']
test = Path(tempfile.mkdtemp(prefix='sutra-vsix-'))
with zipfile.ZipFile(out / ('Sutra-' + version + '.vsix')) as archive:
    archive.extractall(test / 'package')
runtime = test / 'package/extension'
if (runtime / 'bin/telemetry').exists():
    (runtime / 'bin/telemetry').chmod(0o755)
project = test / 'project'
project.mkdir()
(project / 'a.ts').write_text('import {leaf} from "./b";\nexport function start(){return leaf();}\n')
(project / 'b.ts').write_text('export function leaf(){return 1;}\n')
profile = test / 'profile'
(profile / 'User').mkdir(parents=True)
(profile / 'User/settings.json').write_text(json.dumps({
    'telemetry.telemetryLevel': 'off', 'update.mode': 'none',
    'extensions.autoUpdate': False, 'window.restoreWindows': 'none',
    'workbench.startupEditor': 'none'}))
result_path = test / 'result.json'
env = dict(os.environ, KARMA_SMOKE_PROFILE=str(profile), KARMA_SMOKE_OUTPUT=str(result_path),
           KARMA_SMOKE_DRIVER=str(source / 'test'), KARMA_SMOKE_PYTHON=sys.executable)
command = [args.code, '--user-data-dir', str(profile), '--extensions-dir', str(test / 'extensions'),
           '--extensionDevelopmentPath=' + str(runtime), '--extensionTestsPath=' + str(source / 'test'),
           '--disable-workspace-trust', '--skip-welcome', '--skip-release-notes', '--new-window', str(project)]
with (out / 'Packaged-VSCode.log').open('w') as log:
    process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 150
    while not result_path.exists() and time.monotonic() < deadline:
        time.sleep(0.5)
    if not result_path.exists():
        raise RuntimeError('Packaged VS Code test timed out; inspect ' + str(test))
result = json.loads(result_path.read_text())
shutil.copyfile(result_path, out / 'Packaged-VSCode.json')
(out / 'Packaged-VSCode-paths.json').write_text(json.dumps({'profile': str(profile), 'runtime': str(runtime)}))
print(json.dumps({'passed': result['passed'], 'checks': result['checks'], 'error': result.get('error')}))
raise SystemExit(0 if result['passed'] else 1)
