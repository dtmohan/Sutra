"""Measure numeric token diagnostics through the live Sutra gateway.

Uses synthetic conversations and temporary ledgers. The report contains timings,
counts and deterministic grades. It omits answer and reasoning text.
"""
from argparse import ArgumentParser
from contextlib import closing
from hashlib import sha256
from itertools import combinations
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from tempfile import TemporaryDirectory
import time
from unittest.mock import patch


LEDGER = (
    'Ada (group I): cost 4, value 7. Bram (I): cost 5, value 12. '
    'Cy (II): cost 3, value 6. Dia (II): cost 4, value 9. Eon (III): cost 2, value 3. '
    'Choose exactly two items from distinct groups, with at least one group II item. '
    'Total cost must be at most 9. Maximize total value.'
)
CORRECTION = (
    'Based on this document, apply this correction: Bram now costs 6 and Dia costs 5. '
    'Keep all other values, the budget and eligibility rules. '
    'Solve the corrected ledger. Put only a JSON object in the answer field with keys '
    'items (names in alphabetical order), cost and value.'
)
ROUTE = (
    'Directed edges (minutes, battery): S→A(2,3), S→B(3,1), A→B(1,1), A→M(2,3), A→T(1,1), '
    'B→M(2,2), B→C(1,2), C→M(1,3), M→T(1,4), M→D(2,1), D→T(1,1), S→X(1,1), X→M(1,1). '
    'X→M is closed. The route must visit M and use at most 8 battery. Break time ties by lower battery.'
)
ROUTE_ASK = ('Read this complete graph puzzle and solve it now using the supplied edges. '
             'Find the fastest S→T route. Put only a JSON object in the answer field '
             'with keys path (array), minutes and battery.')
ROUTE_LONG = (
    'Ignore these unrelated inventory IDs. They impose no routing constraints.\n'
    + ' '.join(sha256(str(i).encode()).hexdigest()[:16] for i in range(65)) + '\n' + ROUTE
)
ROUTE_EXPECTED = {'path': ['S', 'B', 'M', 'T'], 'minutes': 6, 'battery': 7}


def ledger_expected():
    rows = [('Ada', 1, 4, 7), ('Bram', 1, 6, 12), ('Cy', 2, 3, 6),
            ('Dia', 2, 5, 9), ('Eon', 3, 2, 3)]
    eligible = [(a[3] + b[3], a[2] + b[2], [a[0], b[0]]) for a, b in combinations(rows, 2)
                if a[1] != b[1] and 2 in (a[1], b[1]) and a[2] + b[2] <= 9]
    value, cost, items = max(eligible)
    return {'items': items, 'cost': cost, 'value': value}


def grade(answer, expected):
    if answer is None or expected is None:
        return None
    text = answer.strip()
    fenced = re.fullmatch(r'```(?:json)?\s*(.*?)\s*```', text, re.DOTALL | re.IGNORECASE)
    try:
        actual = json.loads(fenced[1] if fenced else text)
    except ValueError:
        objects = re.findall(r'```json\s*(.*?)\s*```', text, re.DOTALL | re.IGNORECASE)
        try:
            actual = json.loads(objects[0]) if len(objects) == 1 else None
        except ValueError:
            actual = None
    matches = {key: isinstance(actual, dict) and actual.get(key) == value for key, value in expected.items()}
    return {'json_object': isinstance(actual, dict), 'fields': matches,
            'exact': isinstance(actual, dict) and actual == expected}


class Probe:
    """Record transport numbers before the compact trace discards some timings."""

    def __init__(self, sensor):
        self.observe = sensor.observe
        self.finish = sensor.finish
        self.calls = []

    def observe_frame(self, sensor, chunk):
        probe = sensor.__dict__.setdefault('timing_probe', {'stream_positions': 0})
        entries = chunk.get('logprobs')
        if isinstance(entries, list):
            probe['stream_positions'] += len(entries)
        message = chunk.get('message') or {}
        now = time.monotonic()
        if message.get('thinking'):
            probe.setdefault('first_thinking', now)
        if message.get('content'):
            probe.setdefault('first_answer', now)
        if chunk.get('done'):
            probe['done'] = now
        self.observe(sensor, chunk)

    def finish_call(self, sensor, directory, call_id, model, record, identity=None):
        row = self.finish(sensor, directory, call_id, model, record, identity)
        response = record.get('response') or {}
        request = record['request']
        probe = sensor.__dict__.get('timing_probe', {})
        intervals = {a + '_to_' + b: round(probe[b] - probe[a], 4)
                     for a, b in [('first_thinking', 'first_answer'), ('first_answer', 'done')]
                     if a in probe and b in probe and probe[b] >= probe[a]}
        self.calls.append({
            'call_id': call_id, 'model': model, 'think': request.get('think'),
            'logprobs_requested': request.get('logprobs', False),
            'top_logprobs_requested': request.get('top_logprobs'),
            'elapsed_s': round(record['elapsed_s'], 4), 'transport_ok': record['ok'],
            'done_reason': response.get('done_reason'),
            'input_tokens': response.get('prompt_eval_count'), 'output_tokens': response.get('eval_count'),
            'backend_seconds': {k: round(response[k] / 1e9, 4) for k in
                                ('load_duration', 'prompt_eval_duration', 'eval_duration', 'total_duration')
                                if type(response.get(k)) in (float, int)},
            'stream_seconds': intervals, 'stream_positions': probe.get('stream_positions', 0),
            'sensor': {k: row[k] for k in ('available', 'tokens', 'malformed', 'truncated', 'attribution',
                                         'counts', 'answer', 'thinking', 'tripped', 'thresholds') if k in row}
                      if row else None,
            'sensor_has_flagged_text': any('token' in flag or 'runner_up' in flag
                                           for flag in (row or {}).get('flags', [])),
        })
        return row


def audit_calls(store, calls):
    from budget_accounting import used
    for call in calls:
        with closing(sqlite3.connect(store / 'state.sqlite')) as db:
            envelope, receipt, evidence_digest, retained_from = db.execute(
                'SELECT i.record_json,r.record_json,'
                'json_extract(d.payload_json,\'$.document_evidence.digest\'),'
                'json_extract(d.payload_json,\'$.document_evidence.retained_from\') FROM invocations i '
                'JOIN invocation_receipts r ON r.invocation_id=i.id JOIN deliveries d ON d.id=i.delivery_id WHERE i.call_id=?',
                (call['call_id'],),
            ).fetchone()
        envelope, receipt = json.loads(envelope), json.loads(receipt)
        call['stage'] = envelope['role']
        call['receipt_status'] = receipt['status']
        call['settled_units'] = used(envelope, receipt)
        call['evidence_digest'] = evidence_digest
        call['retained_document'] = retained_from is not None
        measured = (call['input_tokens'], call['output_tokens'])
        usage = receipt['token_usage']
        checks = {
            'receipt_matches_ollama': measured == (usage['input_tokens'], usage['output_tokens']),
            'measured_usage_settled': call['settled_units'] == sum(measured) if None not in measured else None,
            'numbers_only': not call['sensor_has_flagged_text'],
        }
        sensor = call['sensor']
        if sensor and sensor['available']:
            counts = sensor['counts']
            captured = sum(counts[k] for k in ('answer', 'thinking', 'unattributed'))
            valid = sensor['tokens'] - sensor['malformed']
            checks.update(
                observer_matches_wire=sensor['tokens'] == call['stream_positions'],
                stream_partition=captured <= valid and (sensor['truncated'] or captured == valid),
                answer_values=0 <= counts['value'] <= counts['answer'] and sensor['answer']['n'] == counts['value'],
                thinking_values=0 <= sensor['thinking']['n'] <= counts['thinking'],
                ambiguity_bounds=all(0 <= stage.get('low_margin', 0) <= stage.get('with_alternatives', 0) <= stage['n']
                                     for stage in (sensor['answer'], sensor['thinking'])),
                alert_matches_answer=bool(sensor['answer'].get('low_margin')) == sensor['tripped'],
                top_k_wire_matches=call['top_logprobs_requested'] == sensor['thresholds']['top_logprobs'],
            )
            if call['output_tokens'] is not None:
                call['capture_minus_output_tokens'] = captured - call['output_tokens']
        elif not call['logprobs_requested']:
            checks['disabled_sensor_has_no_positions'] = sensor is None and call['stream_positions'] == 0
        call['checks'] = checks


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--model', default='qwen3:4b')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.resolve().parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        parser.error('choose a new output path')
    sys.path.insert(0, str(args.runtime.resolve() / 'harness'))
    import model_context
    from governance import policy_hash
    from invocation_gateway import InvocationLedger
    from logit_sensor import Sensor, read
    from monitoring import require_resources
    from paging import Budget
    from review_budget import usage
    from vscode_bridge import handle, ollama

    probe = Probe(Sensor)
    version = ollama('/api/version').get('version')
    if ollama('/api/ps').get('models'):
        parser.error('Ollama already has a resident model. Retry when the local runtime is idle.')
    package = json.loads((args.runtime / 'package.json').read_text())
    report = {'complete': False, 'sutra': package['version'], 'policy_hash': policy_hash(),
              'ollama': version, 'model': args.model, 'sequences': []}
    original_allocation = model_context.allocation

    def narrow_output(metadata):
        budget, capacity = original_allocation(metadata)
        return Budget(budget.context_limit, 48, budget.template_reserve), capacity

    def save():
        args.output.write_text(json.dumps(report, indent=2) + '\n')

    with TemporaryDirectory(prefix='sutra-token-metrics-') as temp:
        root = Path(temp)
        options = dict(SUTRA_MEMORY_OWNERSHIP='shared', SUTRA_RUNTIME_DIR=str(root / 'memory'),
                       SUTRA_ACTIVITY='0')
        models = dict.fromkeys(('low', 'mid', 'high'), args.model)

        def observe(sensor, chunk):
            return probe.observe_frame(sensor, chunk)

        def finish(sensor, *values, **kwargs):
            return probe.finish_call(sensor, *values, **kwargs)

        def sequence(name, thinking, document, turns, *, sensor=True, clipped=False):
            workspace = root / name / 'workspace'
            workspace.mkdir(parents=True)
            store = workspace.parent / 'store'
            store.mkdir()
            parent = None
            session = None
            chat = sha256(name.encode()).hexdigest()
            record = {'case': name, 'thinking': thinking, 'numbers_sensor': sensor,
                      'limited_output_control': clipped, 'turns': []}
            report['sequences'].append(record)
            for index, (prompt, expected) in enumerate(turns, 1):
                admission = require_resources(store)
                (store / 'telemetry.json').write_text(json.dumps(admission['sample']))
                probe.calls = []
                with patch.dict(os.environ, dict(options, SUTRA_THINKING=thinking,
                                                 SUTRA_LOGIT_SENSOR='1' if sensor else '')), \
                        patch.object(model_context, 'allocation', narrow_output if clipped else original_allocation):
                    def event(kind, flow=None, **fields):
                        identity = {'flow_id': flow['flow_id'], 'cursor': flow['cursor']} if flow else {}
                        return handle(store, workspace, dict(op='orchestrate', event=kind, chat_key=chat,
                                      armed=False, models=models, tier='low', **identity, **fields))
                    flow = event('start', prompt=prompt + ('\n\nDocument text:' if index == 1 else ''), parent=parent)
                    if index == 1:
                        assert flow['stop']['status'] == 'evidence_bridge'
                        flow = event('supply_evidence', flow, evidence_text='Document text:\n' + document)
                    for _ in range(3):
                        if flow['state'] != 'CLASSIFYING':
                            break
                        flow = event('step', flow)
                    outcome = flow.get('conversation')
                    if not outcome:
                        raise RuntimeError(f"{name} stopped at {flow['state']} before completing the turn")
                sid = outcome['session']
                result = outcome['result']
                admitted = result['admission']['admitted']
                answer = (result.get('accepted_output') or {}).get('answer')
                audit_calls(store, probe.calls)
                record['turns'].append({'turn': index, 'request_characters': len(prompt), 'admitted': admitted,
                                        'intent': (result.get('accepted_output') or {}).get('intent'),
                                        'same_session': session is None or sid == session,
                                        'grade': grade(answer, expected), 'calls': probe.calls})
                parent = result['invocation_id'] if admitted else None
                session = sid
                save()
                if not parent:
                    break
            calls = [call for turn in record['turns'] for call in turn['calls']]
            ledger = InvocationLedger(store / 'state.sqlite')
            try:
                spent = usage(ledger, session)['used']
                record['ledger_checks'] = {
                    'invocations_verify': ledger.verify_invocations(session),
                    'budget_matches_calls': spent['calls'] == len(calls),
                    'budget_matches_settlements': spent['tokens'] == sum(c['settled_units'] for c in calls),
                    'sensor_rows_match': len(read(store)) == (len(calls) if sensor else 0),
                    'same_evidence': len({c['evidence_digest'] for c in calls}) == 1
                                     and calls[0]['evidence_digest'] is not None,
                    'no_source_operations': spent['operations'] == 0 and spent['source_bytes'] == 0,
                }
            finally:
                ledger.close()
            print(json.dumps({'case': name, 'turns': len(record['turns']), 'calls': len(calls),
                              'grade': record['turns'][-1]['grade']}), flush=True)
            save()

        with patch.object(Sensor, 'observe', observe), patch.object(Sensor, 'finish', finish):
            for thinking in ('off', 'on'):
                sequence('ledger-' + thinking, thinking, LEDGER,
                         [('Confirm that you can apply the ledger rules on the next turn.', None),
                          (CORRECTION, ledger_expected())])
                sequence('route-' + thinking, thinking, ROUTE, [(ROUTE_ASK, ROUTE_EXPECTED)])
                sequence('long-route-' + thinking, thinking, ROUTE_LONG, [(ROUTE_ASK, ROUTE_EXPECTED)])
            sequence('sensor-off-control', 'off', ROUTE, [(ROUTE_ASK, ROUTE_EXPECTED)], sensor=False)
            sequence('output-limit-control', 'on', ROUTE, [(ROUTE_ASK, None)], clipped=True)
    report['complete'] = True
    report['failed_checks'] = [
        f"{sequence['case']}: {name}" for sequence in report['sequences']
        for checks in [sequence['ledger_checks']] + [
            call['checks'] for turn in sequence['turns'] for call in turn['calls']
        ] for name, passed in checks.items() if passed is False
    ]
    save()
    if report['failed_checks']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
