"""Opt-in governed local review with the output allocation from a failed call.

Uses six real Sutra modules in a separate task ledger; never modifies user state.
This is a sampled review, not a reproduction of the user's full saved task.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'harness'))
from invocation_gateway import InvocationLedger
from vscode_bridge import handle
from api_runtime import provider_for
from paging import Budget
from review_budget import usage
from review_context import context_budget
import auto_review
import chat_runtime


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--task-tokens', type=int, default=250000)
    parser.add_argument('--auto-allocation', action='store_true')
    args = parser.parse_args()
    out = args.output.resolve(); out.mkdir(parents=True, exist_ok=False)
    state = out/'state'; state.mkdir()
    os.environ.update(SUTRA_THINKING='adaptive', SUTRA_MEMORY_OWNERSHIP='shared',
                      SUTRA_RUNTIME_DIR=str(out/'runtime'), SUTRA_ACTIVITY='1',
                      KARMA_TYPESCRIPT=str(ROOT/'vendor/typescript/typescript.js'))
    monitor = subprocess.Popen([str(ROOT/'bin/telemetry')], stdout=subprocess.PIPE, text=True)
    def collect():
        for line in monitor.stdout:
            try:
                json.loads(line); tmp = state/'telemetry.tmp'; tmp.write_text(line); tmp.replace(state/'telemetry.json')
            except (ValueError, OSError): pass
    threading.Thread(target=collect, daemon=True).start()
    report = {'passed': False, 'model': 'qwen3:4b', 'context_limit': 20701, 'output_reserve': 4096,
              'task_tokens': args.task_tokens, 'auto_allocation': args.auto_allocation,
              'scope': 'six actual Sutra modules; isolated new task, not original conversation replay'}
    ledger = None
    try:
        for _ in range(50):
            if (state/'telemetry.json').exists(): break
            time.sleep(.1)
        roots = ['extension.js', 'harness/chat_runtime.py', 'harness/invocation_gateway.py',
                 'harness/orchestration_flow.py', 'harness/runtime_memory.py', 'harness/stp_canonical/kernel.py']
        start = handle(state, ROOT, {'op': 'auto_new', 'goal': 'Review this workspace and explain its architecture. Keep the STP kernel unchanged. No edits, tests or network.', 'roots': roots, 'review_tokens': args.task_tokens})
        meta = json.loads((state/'workspace.json').read_text())
        ledger = InvocationLedger(state/'state.sqlite')
        for _ in range(12):
            mapped = auto_review.step(ledger, meta, ROOT, state, {'explain': False})
            print('mapping', mapped['auto']['status'], mapped['auto']['mapped_files'], flush=True)
            if not mapped['auto']['pending_files']: break
        assert mapped['auto']['pending_files'] == 0, mapped['auto']
        writer = provider_for(state, 'qwen3:4b', review=True)
        reviewer = provider_for(state, 'qwen3:4b', review=True)
        writer.budget = Budget(20701, 4096, 256)
        writer.keep_alive = reviewer.keep_alive = 0
        # The independent checker gets its normal context share, not the draft's
        # deliberately restricted regression allocation. Task admission still applies.
        report['reviewer_allocation'] = context_budget(reviewer, ledger, meta['active'], slots=2)
        before = usage(ledger, meta['active'])
        started = time.monotonic()
        result = chat_runtime.answer(ledger, meta, ROOT, state,
                    {'prompt': 'Give me a high-level overview first, then we can dig into each block later.',
                     'models': dict.fromkeys(['low', 'mid', 'high'], 'qwen3:4b'), 'tier': 'low'},
                    provider=None if args.auto_allocation else writer, review_provider=None if args.auto_allocation else reviewer)
        report.update(seconds=time.monotonic()-started, result=result,
                      before=before, after=usage(ledger, meta['active']))
        assert result['result']['admission']['admitted'], result['result']['admission']
        assert result['report_markdown'], 'No published report'
        rows = ledger.db.execute('SELECT d.payload_json FROM invocations i JOIN deliveries d ON d.id=i.delivery_id WHERE i.session=?', (meta['active'],)).fetchall()
        packets = [json.loads(r[0]) for r in rows]
        assert all(p['reasoning']['requested'] is False for p in packets)
        assert all(p['contract'] == packets[0]['contract'] for p in packets)
        assert all(p['constraint_echo'] == packets[0]['constraint_echo'] for p in packets)
        assert ledger.verify_invocations(meta['active'])
        report['passed'] = True
        (out/'report.md').write_text(result['report_markdown']+'\n')
        print('PASS', round(report['seconds'], 2), 'seconds', flush=True)
    finally:
        (out/'results.json').write_text(json.dumps(report, indent=2)+'\n')
        if ledger: ledger.close()
        monitor.terminate(); monitor.wait(timeout=5)


if __name__ == '__main__': main()
