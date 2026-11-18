"""Compare Auto with fixed Low on one approved DOCX through the real bridge."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

from document_stage_fixture import NAME, REQUEST, grade, write_document


SOURCE = Path(__file__).resolve().parents[1]


def run_condition(runtime, root, tier, models, document):
    workspace, storage = root/'workspace', root/'storage'
    workspace.mkdir(parents=True)
    storage.mkdir()
    (workspace/NAME).write_bytes(document)
    env = dict(os.environ, SUTRA_THINKING='adaptive', SUTRA_RUNTIME_DIR=str(root/'runtime'),
               SUTRA_MEMORY_OWNERSHIP='shared')
    trace = []
    telemetry = subprocess.Popen([str(runtime/'bin/telemetry')], stdout=subprocess.PIPE, text=True)

    def sample():
        for line in telemetry.stdout:
            try:
                json.loads(line)
                temporary = storage/'telemetry.tmp'
                temporary.write_text(line)
                temporary.replace(storage/'telemetry.json')
            except (ValueError, OSError):
                continue

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()

    def bridge(event, previous=None, **fields):
        request = {'op': 'orchestrate', 'event': event, 'chat_key': 'a'*64,
                   'tier': tier, 'models': models, **fields}
        if previous is not None:
            request.update(flow_id=previous['flow_id'], cursor=previous['cursor'])
        begin = time.monotonic()
        worker = subprocess.run([sys.executable, '-B', str(runtime/'harness/vscode_bridge.py'),
                                 '--workspace', str(workspace), '--storage', str(storage)],
                                input=json.dumps(request), text=True, capture_output=True,
                                timeout=500, env=env, check=False)
        data = json.loads(worker.stdout)
        if worker.returncode or not data['ok']:
            raise RuntimeError(data.get('error') or worker.stderr)
        response = data['data']
        item = {'event': event, 'state': response['state'], 'seconds': round(time.monotonic()-begin, 3)}
        for key in ('progress','document_stage_progress','stop'):
            if response.get(key):
                item[key] = response[key]
        trace.append(item)
        print(tier, json.dumps(item), flush=True)
        return response

    try:
        for _ in range(80):
            if (storage/'telemetry.json').is_file():
                break
            time.sleep(.1)
        if not (storage/'telemetry.json').is_file():
            raise RuntimeError('No fresh telemetry samples')
        flow = bridge('start', prompt=REQUEST)
        flow = bridge('locate_document', flow)
        flow = bridge('approve_document', flow, confirmed=True, document_path=NAME)
        for _ in range(24):
            if flow['state'] != 'CLASSIFYING':
                break
            flow = bridge('step', flow)
    finally:
        telemetry.terminate()
        telemetry.wait(timeout=5)
        sampler.join(timeout=5)
        telemetry.stdout.close()

    from invocation_gateway import InvocationLedger
    from review_budget import usage
    from orchestration_flow import info
    with closing(InvocationLedger(storage/'state.sqlite')) as ledger:
        _, data = info(ledger, flow['flow_id'])
        sid = data['conversation_session']
        verified = ledger.verify_invocations(sid)
        budget = usage(ledger, sid)
    calls, extracts = [], []
    with closing(sqlite3.connect(storage/'state.sqlite')) as db:
        for record, receipt, payload in db.execute("""
            SELECT i.record_json, r.record_json, d.payload_json FROM invocations i
            JOIN invocation_receipts r ON r.invocation_id=i.id
            JOIN deliveries d ON d.id=i.delivery_id ORDER BY i.rowid
        """):
            inv, proof, packet = map(json.loads, (record, receipt, payload))
            call = {'role': inv['role'], 'model': inv['model'], 'result': proof['status'],
                    'tokens': proof['token_usage'], 'seconds': round(proof['elapsed_seconds'], 3),
                    'output_reserve': inv['budget_profile']['response_reserve']}
            if 'section' in packet:
                call['section'] = packet['section']['id']
                if proof['status'] == 'accepted':
                    extracts.append({'section': call['section'], 'notes': proof['proposal']['notes']})
            if 'document_stage_flow' in packet and inv['role'] == 'conversation':
                call['role'] = 'document_synthesis'
            if proof['status'] != 'accepted':
                call['errors'] = proof['admission']['errors']
            calls.append(call)
    final = (flow.get('conversation') or {}).get('result') or {}
    answer = (final.get('accepted_output') or {}).get('answer')
    assert (workspace/NAME).read_bytes() == document and verified
    assert budget['used']['operations'] == 1 and budget['used']['source_bytes'] == len(document)
    return {'tier': tier, 'state': flow['state'], 'steps': trace, 'plan': data.get('document_stage_plan'),
            'answer': answer, 'grade': grade(answer), 'calls': calls, 'extracts': extracts,
            'review_budget': budget, 'verified_receipts': verified,
            'total_wall_seconds': round(sum(item['seconds'] for item in trace), 3)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=SOURCE)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--low', default='qwen3:4b')
    parser.add_argument('--strong', default='local-assistant:latest')
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    sys.path.insert(0, str(runtime/'harness'))
    models = {'low': args.low, 'mid': args.strong, 'high': args.strong}
    report = {'models': models, 'request': REQUEST, 'conditions': []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='sutra-document-routing-') as folder:
        root = Path(folder).resolve()
        fixture = root/NAME
        text = write_document(fixture)
        data = fixture.read_bytes()
        report['document'] = {'name': NAME, 'sha256': hashlib.sha256(data).hexdigest(),
                              'file_bytes': len(data), 'extracted_utf8_bytes': len(text.encode('utf-8'))}
        for tier in ('auto', 'low'):
            result = run_condition(runtime, root/tier, tier, models, data)
            report['conditions'].append(result)
            args.output.write_text(json.dumps(report, indent=2) + '\n')
            print(tier, json.dumps({'state': result['state'], 'grade': result['grade'],
                                    'total_wall_seconds': result['total_wall_seconds']}), flush=True)
        first, second = report['conditions']
        report['same_extracted_evidence'] = first['extracts'] == second['extracts']
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    return 0 if all(condition['answer'] for condition in report['conditions']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
