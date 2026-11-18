"""Opt-in saved conversation across real Ollama/Strata calls and native reloads.

Run low-first, cycle the native runtime separately, run high, then low-last.
Each phase opens the same durable task in a new process. This script never
downloads, loads, unloads, stops or reconfigures a model runtime.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'harness'))

from context_manager import canonical, sha
from conversation import answer, chat_axis, session_for
from invocation_gateway import InvocationLedger
from lifecycle import deadline
from review_budget import usage


PHASES = ('low-first', 'high', 'low-last')
MODELS = {'low': 'qwen3:4b', 'mid': 'qwen3:4b', 'high': 'strata:high'}
PROMPTS = {
    'low-first': (
        'Remember project Cedar-724, ticket CED724. The target is 18 and the actual is 21. '
        'Never change the STP kernel. No network, edits, tests, or new dependencies. '
        'Keep this task conversation only. Reply with one sentence confirming the facts.'
    ),
    'high': (
        'Using the previous turn, name the project and ticket, calculate actual minus target, '
        'and repeat what must stay unchanged and which operations are prohibited. '
        'Answer in one short paragraph without performing any actions.'
    ),
    'low-last': (
        'Recall our saved project and ticket, the difference between actual and target, '
        'and all work restrictions. Give one short paragraph without performing actions.'
    ),
}


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def kernel_hash():
    manifest = json.loads((ROOT / 'harness/canonical-kernel.json').read_text())
    digest = hashlib.sha256((ROOT / 'harness/stp_canonical/kernel.py').read_bytes()).hexdigest()
    assert digest == manifest['sha256'], 'Canonical STP kernel differs from its supplied manifest'
    return digest


def checkpoint(ledger, session):
    invocations = [list(row) for row in ledger.db.execute(
        'SELECT id,digest FROM invocations WHERE session=? ORDER BY rowid', (session,))]
    receipts = [list(row) for row in ledger.db.execute(
        'SELECT seq,invocation_id,id,record_json FROM invocation_receipts WHERE session=? ORDER BY seq',
        (session,))]
    return {
        'origin': ledger.origin(session), 'snapshot': ledger.snapshot(session),
        'budget': usage(ledger, session), 'invocations': invocations,
        'receipt_fingerprints': [row[:3] + [sha(row[3])] for row in receipts],
    }


def check_saved(ledger, fixture):
    current = checkpoint(ledger, fixture['session'])
    previous = fixture['checkpoint']
    assert current == previous, 'Saved context, permissions, receipts or spending changed between phases'
    assert ledger.verify_invocations(fixture['session']), 'Invocation receipts failed verification'
    assert ledger.audit_valid(), 'Audit evidence failed verification'
    return current


def check_no_actions(ledger):
    for table in ('host_actions', 'repository_ops'):
        if ledger.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            assert ledger.db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 0, table


@contextmanager
def telemetry(storage):
    executable = ROOT / 'bin/telemetry'
    process = subprocess.Popen([str(executable)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    ready = threading.Event()

    def collect():
        for line in process.stdout:
            try:
                value = json.loads(line)
                write_json(storage / 'telemetry.json', value)
                ready.set()
            except (ValueError, OSError):
                pass

    thread = threading.Thread(target=collect, daemon=True)
    thread.start()
    try:
        for _ in range(50):
            if ready.is_set():
                break
            if process.poll() is not None:
                raise RuntimeError('Native telemetry stopped before providing a resource sample')
            time.sleep(.1)
        assert ready.is_set(), 'No fresh telemetry sample arrived in this process'
        yield
    finally:
        process.terminate()
        process.wait(timeout=5)
        thread.join(timeout=1)
        process.stdout.close()


def check_recall(text):
    lower = text.lower()
    assert 'Cedar-724' in text and 'CED724' in text, text
    assert re.search(r'\b3\b', text), text
    for word in ('kernel', 'network', 'edit', 'test', 'dependenc'):
        assert word in lower, ('Missing recalled restriction: ' + word, text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--phase', choices=(*PHASES, 'verify'), required=True)
    parser.add_argument('--thinking', choices=('off', 'adaptive'), default='off')
    args = parser.parse_args()
    output = args.output.resolve()
    fixture_path = output / 'fixture.json'
    report_path = output / 'results.json'

    if args.phase == 'low-first' and not fixture_path.exists():
        output.mkdir(parents=True, exist_ok=False)
        (output / 'workspace').mkdir()
        (output / 'state').mkdir()
        fixture = {
            'version': 1, 'models': MODELS, 'thinking': args.thinking,
            'kernel_sha256': kernel_hash(), 'completed': [], 'attempts': [],
            'session': None, 'parent': None,
            'axis': chat_axis(output / 'workspace', 'native-model-management-continuity'),
        }
        write_json(fixture_path, fixture)
    else:
        fixture = json.loads(fixture_path.read_text())
        assert fixture['kernel_sha256'] == kernel_hash(), 'Canonical kernel changed across phases'
        assert fixture['models'] == MODELS and fixture['thinking'] == args.thinking

    os.environ.update(
        SUTRA_THINKING=args.thinking, SUTRA_MEMORY_OWNERSHIP='shared',
        SUTRA_RUNTIME_DIR=str(output / 'runtime-memory'),
    )
    storage = output / 'state'
    ledger = InvocationLedger(storage / 'state.sqlite')
    report = {
        'passed': False, 'phase': args.phase, 'scope': 'Real gateway calls; external native runtime cycles between phases',
        'models': MODELS, 'kernel_sha256': fixture['kernel_sha256'],
        'runtime_actions_from_this_script': [], 'workspace_actions_from_this_script': [],
    }
    try:
        before = check_saved(ledger, fixture) if fixture['session'] else None
        if args.phase == 'verify':
            assert fixture['completed'] == list(PHASES), fixture['completed']
            check_no_actions(ledger)
            report.update(passed=True, checks={
                'durable_context_permissions_and_spending': True,
                'receipt_chains': True, 'canonical_kernel_unchanged': True,
                'low_high_low_recall': True, 'fresh_process_per_phase': True,
            }, checkpoint=before, attempts=fixture['attempts'])
            return

        assert fixture['completed'] == list(PHASES[:PHASES.index(args.phase)]), 'Run phases in the documented order'
        tier = 'high' if args.phase == 'high' else 'low'
        request = {'prompt': PROMPTS[args.phase], 'models': MODELS, 'tier': tier,
                   'parent': fixture['parent'], 'one_step': True}
        if not fixture['session']:
            fixture['session'] = session_for(ledger, request, axis=fixture['axis'])
            fixture['checkpoint'] = checkpoint(ledger, fixture['session'])
            write_json(fixture_path, fixture)
        request['conversation_session'] = fixture['session']
        started = time.monotonic()
        with telemetry(storage), deadline(450):
            result = answer(ledger, {'workspace': str(output / 'workspace')}, storage,
                            request, axis=fixture['axis'])
        elapsed = round(time.monotonic() - started, 3)
        attempt = {'phase': args.phase, 'seconds': elapsed, 'response': result}
        fixture['attempts'].append(attempt)
        report['attempt'] = attempt
        assert fixture['session'] in (None, result['session']), 'Model switch created a new task'
        fixture['session'] = result['session']
        receipt = result['result']
        assert receipt['admission']['admitted'], receipt['admission']
        assert result['route']['model'] == MODELS[tier], result['route']
        envelope = json.loads(ledger.db.execute(
            'SELECT record_json FROM invocations WHERE id=?', (receipt['invocation_id'],)).fetchone()[0])
        assert envelope['model'] == MODELS[tier]
        attempt['model_identity'] = envelope.get('model_identity')
        text = receipt['accepted_output']['answer']
        current = checkpoint(ledger, fixture['session'])
        permissions = current['snapshot']['constraints']
        assert all(permissions[name] is False for name in (
            'network_allowed', 'edits_allowed', 'tests_allowed', 'dependencies_allowed'))
        assert permissions['conversation_only'] is True
        if before:
            assert current['origin'] == before['origin']
            assert current['snapshot'] == before['snapshot']
            assert current['budget']['limits'] == before['budget']['limits']
            assert current['budget']['base_limits'] == before['budget']['base_limits']
            assert current['budget']['allocation_version'] == before['budget']['allocation_version']
            old_used, new_used = before['budget']['used'], current['budget']['used']
            assert all(new_used[key] >= old_used[key] for key in old_used)
            assert new_used['calls'] > old_used['calls'] and new_used['tokens'] > old_used['tokens']
            assert current['invocations'][:len(before['invocations'])] == before['invocations']
            assert current['receipt_fingerprints'][:len(before['receipt_fingerprints'])] == before['receipt_fingerprints']
        if fixture['parent']:
            assert result['memory_used'], 'Saved memory was not supplied after the model switch'
            raw = ledger.db.execute(
                'SELECT d.payload_json FROM deliveries d JOIN invocations i ON i.delivery_id=d.id WHERE i.id=?',
                (receipt['invocation_id'],)).fetchone()[0]
            packet = json.loads(raw)
            pins = json.dumps(packet['memory']['pinned_user_statements'])
            assert all(value in pins for value in ('Cedar-724', 'CED724', 'STP kernel', 'No network'))
            check_recall(text)
        assert ledger.verify_invocations(fixture['session'])
        assert ledger.audit_valid()
        check_no_actions(ledger)
        fixture['parent'] = receipt['invocation_id']
        fixture['completed'].append(args.phase)
        fixture['checkpoint'] = current
        report.update(passed=True, answer=text, checkpoint=current,
                      task_contract_hash=sha(canonical(current['snapshot'])))
    except BaseException as error:
        report['error'] = str(error)
        if fixture.get('session'):
            # Failed or withheld attempts remain charged. Re-running a phase can
            # never resurrect an earlier spending checkpoint.
            fixture['checkpoint'] = checkpoint(ledger, fixture['session'])
        raise
    finally:
        write_json(fixture_path, fixture)
        write_json(output / ('phase-' + args.phase + '.json'), report)
        write_json(report_path, report)
        ledger.close()
        print(json.dumps({key: report.get(key) for key in ('phase', 'passed', 'error')}), flush=True)


if __name__ == '__main__':
    main()
