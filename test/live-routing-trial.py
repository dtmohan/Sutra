"""Opt-in replay of the reported two-turn routing failure, using real Ollama.

Fresh bridge processes, isolated synthetic source, actual host permissions and
resource gates. --review also runs the source workflow with a model switch.
--harness permits a frozen baseline with identical prompts and Adaptive setting.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

HERE = Path(__file__).resolve().parents[1]
CAPABILITY = 'can you access the codebase?'
ARCHITECTURE = ('i want you to explain to me the aarchitecture using diagrams in a way you would '
                'to a new intern joining the team who will take care of the project going forward')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--harness', type=Path, default=HERE)
    parser.add_argument('--review', action='store_true')
    parser.add_argument('--worker', default='local-assistant:latest')
    args = parser.parse_args()
    code, output = args.harness.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    ws, state = output/'workspace', output/'state'
    ws.mkdir(); state.mkdir()
    source = ('def normalize(value):\n    return max(0, value)\n\n'
              'def calculate(value):\n    return normalize(value) * 2\n\n'
              'def start(value):\n    return calculate(value)\n')
    (ws/'app.py').write_text(source)
    env = dict(os.environ, SUTRA_THINKING='adaptive', SUTRA_ACTIVITY='0', SUTRA_MEMORY_OWNERSHIP='shared',
               KARMA_TYPESCRIPT=str(code/'vendor/typescript/typescript.js'),
               SUTRA_RUNTIME_DIR=str(Path.home()/'Library/Application Support/Code/User/globalStorage/deepak-local.karma-local-harness/runtime-memory'))
    monitor = subprocess.Popen([str(HERE/'bin/telemetry')], stdout=subprocess.PIPE, text=True)
    def collect():
        for line in monitor.stdout:
            try:
                json.loads(line)
                tmp = state/'telemetry.tmp'; tmp.write_text(line); tmp.replace(state/'telemetry.json')
            except (ValueError, OSError):
                pass
    threading.Thread(target=collect, daemon=True).start()
    report = {'passed': False, 'harness': str(code), 'rows': [], 'checks': {}, 'reasoning_preference': 'adaptive'}
    models = {'low': 'qwen3:4b', 'mid': args.worker, 'high': args.worker}

    def save():
        (output/'results.json').write_text(json.dumps(report, indent=2)+'\n')

    def send(label, **req):
        started = time.monotonic()
        result = subprocess.run([sys.executable, '-B', str(code/'harness/vscode_bridge.py'),
                                 '--workspace', str(ws), '--storage', str(state)],
                                input=json.dumps(req), capture_output=True, text=True, env=env, timeout=480)
        data = json.loads(result.stdout) if result.stdout.strip() else {'ok': False, 'error': result.stderr}
        report['rows'].append({'label': label, 'seconds': round(time.monotonic()-started, 3), 'response': data})
        save()
        print(json.dumps({'label': label, 'seconds': report['rows'][-1]['seconds'], 'ok': data.get('ok'),
                          'state': data.get('data', {}).get('state')}), flush=True)
        assert result.returncode == 0 and data.get('ok'), data
        return data['data']

    def counts():
        with sqlite3.connect(state/'state.sqlite') as db:
            return {name: db.execute('SELECT count(*) FROM '+name).fetchone()[0]
                    for name in ('repository_ops', 'host_actions', 'invocations')}

    try:
        for _ in range(50):
            if (state/'telemetry.json').exists(): break
            time.sleep(.1)
        send('create-read-only-task', op='auto_new', goal='Review this workspace and explain its architecture.', roots=['.'])
        before = counts()
        first = send('capability', op='orchestrate', event='start', prompt=CAPABILITY, models=models, tier='auto', armed=False)
        second = send('architecture', op='orchestrate', event='start', prompt=ARCHITECTURE, models=models, tier='auto',
                      armed=False, parent=first['conversation']['result']['invocation_id'])
        report['checks'].update(capability_conversation=first['state']=='DONE', architecture_handoff=second['state']=='NEED_ARM',
                               no_source_reads_during_routing=counts()['repository_ops']==before['repository_ops'],
                               no_actions_during_routing=counts()['host_actions']==before['host_actions'],
                               context_carried=second['conversation']['memory_used'])
        for turn in (first, second):
            chat = turn['conversation']
            assert chat['result']['admission']['admitted'], chat
            assert chat['reasoning']['preference']=='adaptive' and chat['reasoning']['requested'] is False
            assert len(chat['result']['token_usage']['calls']) == 1
        if args.review:
            assert all(report['checks'].values()), report['checks']
            flow = second
            # Low router -> larger workspace reviewer; every step is a fresh bridge.
            for i in range(20):
                if flow['state'] in ('DONE', 'PAUSED'): break
                flow = send('review-'+str(i), op='orchestrate', event='step', flow_id=flow['flow_id'],
                            cursor=flow['cursor'], binding=flow.get('binding'), armed=True, models=models, tier='high')
            report['review'] = flow
            report['checks']['review_completed'] = flow['state']=='DONE' and bool(flow.get('data', {}).get('report_markdown'))
            report['checks']['source_unchanged'] = (ws/'app.py').read_text()==source
            report['checks']['source_was_read'] = counts()['repository_ops']>before['repository_ops']
            report['checks']['no_edits_or_tests'] = counts()['host_actions']==before['host_actions']
            if flow.get('data', {}).get('report_markdown'):
                (output/'Architecture.md').write_text(flow['data']['report_markdown'])
            # Reopen actual ledger: budgets never shrink/reset; model switch leaves
            # the immutable workspace contract and conversation parent in place.
            sys.path.insert(0, str(code/'harness'))
            from invocation_gateway import InvocationLedger
            from review_budget import usage
            ledger = InvocationLedger(state/'state.sqlite')
            try:
                for sid, in ledger.db.execute('SELECT DISTINCT session FROM invocations').fetchall():
                    assert ledger.verify_invocations(sid, complete=False)
                sid = flow['binding']['session']
                contract = ledger.pinned(sid)
                assert not any(contract['constraints'][k] for k in ('edits_allowed','tests_allowed','network_allowed','dependencies_allowed'))
                report['workspace_budget'] = usage(ledger, sid)
                report['checks']['constraints_preserved'] = True
                report['checks']['receipts_verified'] = True
            finally:
                ledger.close()
        report['passed'] = all(report['checks'].values())
        report['kernel_sha256'] = hashlib.sha256((code/'harness/stp_canonical/kernel.py').read_bytes()).hexdigest()
    except BaseException as exc:
        report['error'] = str(exc)
        raise
    finally:
        save(); monitor.terminate(); monitor.wait(timeout=5)
    if not report['passed']: raise SystemExit('Routing assertions failed; raw results retained.')


if __name__ == '__main__':
    main()
