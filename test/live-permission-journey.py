"""Run the native permission UI in an isolated VS Code profile; no inference."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--node', default=os.environ.get('KARMA_NODE') or shutil.which('node'))
parser.add_argument('--code', default='/Applications/Visual Studio Code.app/Contents/MacOS/Code')
args = parser.parse_args()
if not args.output.is_absolute() or not args.node or not os.environ.get('KARMA_PLAYWRIGHT'):
    parser.error('Use an absolute output, --node and KARMA_PLAYWRIGHT')
out = args.output
out.mkdir(parents=True, exist_ok=False)
fixture = out / 'extension'
fixture.mkdir()
package = json.loads((ROOT / 'package.json').read_text())
package.update(main='journey.js', activationEvents=['onStartupFinished'])
(fixture / 'package.json').write_text(json.dumps(package))
shutil.copyfile(ROOT / 'test/live-permission-journey.js', fixture / 'journey.js')
shutil.copytree(ROOT / 'media', fixture / 'media')
workspace = out / 'workspace'
workspace.mkdir()
(workspace / 'entry.py').write_text('from leaf import leaf\ndef start():\n    return leaf()\n')
(workspace / 'leaf.py').write_text('def leaf():\n    return 1\n')
profile = out / 'profile'
(profile / 'User').mkdir(parents=True)
(profile / 'User/settings.json').write_text(json.dumps({
    'telemetry.telemetryLevel': 'off', 'update.mode': 'none', 'extensions.autoUpdate': False,
    'workbench.startupEditor': 'none', 'window.restoreWindows': 'none', 'window.dialogStyle': 'custom',
    'chat.defaultModel': 'karma-auto', 'chat.editor.localAgent.enabled': True,
    'chat.defaultToCopilotHarness': False, 'chat.editor.preferCopilotHarness': False,
}))
with socket.socket() as probe:
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
result = out / 'result.json'
env = dict(os.environ, SUTRA_PERMISSION_ROOT=str(ROOT), SUTRA_PERMISSION_RESULT=str(result),
           SUTRA_PERMISSION_PORT=str(port), SUTRA_PERMISSION_AUTORUN='1')
command = [args.code, '--user-data-dir', str(profile), '--extensions-dir', str(out / 'extensions'),
           '--extensionDevelopmentPath=' + str(fixture),
           '--disable-workspace-trust', '--skip-welcome', '--skip-release-notes',
           '--remote-debugging-port=' + str(port), '--remote-allow-origins=*', '--new-window', str(workspace)]
processes = []
with (out / 'vscode.log').open('w') as host_log, (out / 'ui.log').open('w') as ui_log:
    try:
        host = subprocess.Popen(command, env=env, stdout=host_log, stderr=subprocess.STDOUT)
        driver = subprocess.Popen([args.node, str(ROOT / 'test/live-permission-journey-ui.cjs')],
                                  env=env, stdout=ui_log, stderr=subprocess.STDOUT)
        processes = [host, driver]
        previous, deadline = None, time.monotonic() + 210
        while time.monotonic() < deadline:
            report = json.loads(result.read_text()) if result.exists() else {}
            phase = report.get('phase')
            if phase != previous:
                print(json.dumps({'phase': phase, 'output': str(out)}), flush=True)
                previous = phase
            if phase in ('complete', 'failed'):
                if phase == 'failed':
                    raise RuntimeError(report.get('error'))
                driver.wait(timeout=15)
                assert driver.returncode == 0
                print(json.dumps({'passed': report['passed'], 'scope': report['scope'],
                                  'checks': report['checks']}), flush=True)
                break
            if driver.poll() is not None:
                raise RuntimeError('UI driver exited early: ' + (out / 'ui.log').read_text())
            if host.poll() is not None:
                raise RuntimeError('VS Code exited early: ' + (out / 'vscode.log').read_text()[-5000:])
            time.sleep(.25)
        else:
            raise RuntimeError('Permission journey timed out; inspect ' + str(out))
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
