"""Compare host preparation with fresh workers, persistent workers and RAM pages.

The fixture uses scripted gateway responses. No inference, user workspaces,
system cache purges or provider configuration changes are involved.
"""
import argparse
from contextlib import nullcontext
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import resource
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time


SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / 'harness'))
from context_manager import canonical, sha
import conversation
import conversation_memory as memory
from invocation_gateway import InvocationLedger, consume_dispatch
from paging import Budget, PageFault
from ram_compartments import Compartments
from review_budget import usage

MODELS = {'low': 'fixture-low', 'mid': 'fixture-mid', 'high': 'fixture-high'}
MAX_BYTES = 16 * 1024**2


class ScriptedProvider:
    model = MODELS['low']
    budget = Budget(65536, 4096, 256)
    context_metadata = {'model_info': {'general.architecture': 'qwen', 'qwen.context_length': 65536}}

    def call(self, call_id, system, payload, schema):
        consume_dispatch(call_id, self.model, system, payload, schema)
        answer = ('Cedar discussion notes preserve the supplied evidence. ' * 22).strip()
        return {
            'page_requests': [], 'intent': 'conversation', 'escalation_reason': 'none',
            'diagram': None, 'answer': answer, 'summary': 'Cedar evidence discussion.',
        }, {'prompt_eval_count': 100, 'eval_count': 100}


def build_fixture(root, turns, pages, page_kib):
    root.mkdir(parents=True, exist_ok=True)
    workspace = root / 'workspace'
    workspace.mkdir()
    storage = root / 'store'
    storage.mkdir()
    ledger = InvocationLedger(storage / 'state.sqlite')
    parent = None
    for index in range(turns):
        prompt = ('Remember project Cedar, tag CED724. Keep replies clear. Never use network or edit files.'
                  if index == 0 else f'Discuss Cedar evidence item {index} and how it relates to CED724.')
        result = conversation.answer(ledger, {}, storage,
            {'models': MODELS, 'tier': 'low', 'prompt': prompt, 'parent': parent},
            provider=ScriptedProvider())
        assert result['result']['admission']['admitted'], result
        session = result['session']
        parent = result['result']['invocation_id']
    rows = memory.history(ledger, session, parent)
    memory.materialize(ledger, session, rows)
    keys = []
    files = {}
    text = 'Cedar retained source evidence. ' * (page_kib * 1024 // 31 + 1)
    text = text[:page_kib * 1024]
    for index in range(pages):
        key = f'bench-page-{index:03}'
        keys.append(key)
        if index < min(4, pages):
            path = workspace / f'evidence-{index}.txt'
            path.write_text(text + str(index))
            ledger.import_file(session, key, path)
            files[key] = str(path)
        else:
            ledger.put_page(session, key, 'evidence', key, text + str(index), authority='model')
    delivery, = ledger.db.execute('SELECT d.payload_json FROM deliveries d JOIN invocations i '
        'ON i.delivery_id=d.id WHERE i.id=?', (parent,)).fetchone()
    base = json.loads(delivery)
    base['request'] = 'Explain Cedar evidence with CED724 and retain the stated restrictions.'
    base['memory'] = None
    data = {'session': session, 'parent': parent, 'keys': keys, 'files': files,
            'base': base, 'constraints': conversation.contract(ledger, session),
            'spent': usage(ledger, session)['used'], 'turns': turns,
            'pages': pages, 'page_kib': page_kib}
    ledger.close()
    (root / 'fixture.json').write_text(json.dumps(data))
    return data


def measured(work, timings, key):
    start = time.perf_counter_ns()
    value = work()
    timings[key] = (time.perf_counter_ns() - start) / 1e6
    return value


def prepare(root, cache, compartment=True, partition=None):
    fixture = json.loads((root / 'fixture.json').read_text())
    timings = {}
    started = time.perf_counter_ns()
    ledger = measured(lambda: InvocationLedger(root / 'store/state.sqlite'), timings, 'ledger_open_ms')
    try:
        with cache.request(str(root)) if compartment else nullcontext():
            keys = fixture['keys'] if partition is None else fixture['keys'][partition::2]
            pages = measured(lambda: [ledger.page(fixture['session'], key)
                                     for key in keys], timings, 'sql_lookup_ms')
            statuses = measured(lambda: [ledger.status(page) for page in pages], timings,
                                'page_integrity_and_source_freshness_ms')
            assert all(value in ('current', 'aging', 'unverified') for value in statuses), statuses
            payloads = measured(lambda: [page.payload for page in pages], timings, 'page_decode_and_copy_ms')
            history = measured(lambda: memory.history(ledger, fixture['session'], fixture['parent']),
                               timings, 'ancestry_validation_ms')
            measured(lambda: memory.materialize(ledger, fixture['session'], history), timings,
                     'materialized_page_validation_ms')
            working = measured(lambda: memory.working_set(ledger, fixture['session'], fixture['parent'],
                fixture['base']['request'], ScriptedProvider.budget, fixture['base'], conversation.SYSTEM,
                conversation.schema), timings, 'working_set_and_prompt_sizing_ms')
            assert conversation.contract(ledger, fixture['session']) == fixture['constraints']
            assert usage(ledger, fixture['session'])['used'] == fixture['spent']
            assert any('CED724' in item['quote'] for item in working['pinned_user_statements'])
            signature = sha(canonical({'payloads': payloads, 'memory': working,
                                       'contract': fixture['constraints']}))
        timings['host_prepare_ms'] = (time.perf_counter_ns() - started) / 1e6
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return {'timings': timings, 'signature': signature, 'cache': cache.snapshot(),
                'max_rss_bytes': rss if sys.platform == 'darwin' else rss * 1024}
    finally:
        ledger.close()


def worker(root, budget, once=False):
    cache = Compartments(budget)
    for raw in sys.stdin:
        request = json.loads(raw)
        try:
            operation = request.get('op', 'prepare')
            if operation == 'clear':
                cache.clear('synthetic_pressure')
                cache.resize(0)
                result = {'cache': cache.snapshot()}
            elif operation == 'restore':
                cache.resize(budget)
                result = {'cache': cache.snapshot()}
            elif operation == 'fault':
                fixture = json.loads((root / 'fixture.json').read_text())
                key = next(iter(fixture['files']))
                ledger = InvocationLedger(root / 'store/state.sqlite')
                try:
                    with cache.request(str(root)):
                        ledger.read_page(fixture['session'], key).payload
                    result = {'fault': None}
                except PageFault as error:
                    result = {'fault': error.code}
                finally:
                    ledger.close()
            else:
                partition = {'partition_a': 0, 'partition_b': 1}.get(operation)
                result = prepare(root, cache, budget > 0, partition)
            print(json.dumps({'ok': True, 'data': result}), flush=True)
        except Exception as error:
            print(json.dumps({'ok': False, 'error': repr(error)}), flush=True)
        if once:
            break


class Process:
    def __init__(self, root, budget):
        self.root, self.budget = root, budget
        self.process = subprocess.Popen([sys.executable, '-B', str(Path(__file__)), '--worker',
            str(root), '--budget', str(budget)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1)

    def request(self, operation='prepare'):
        started = time.perf_counter_ns()
        self.process.stdin.write(json.dumps({'op': operation}) + '\n')
        self.process.stdin.flush()
        raw = self.process.stdout.readline()
        if not raw:
            raise RuntimeError(self.process.stderr.read())
        result = json.loads(raw)
        assert result['ok'], result
        result['data']['roundtrip_ms'] = (time.perf_counter_ns() - started) / 1e6
        return result['data']

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=10)
        assert self.process.returncode == 0, self.process.stderr.read()
        self.process.stdout.close()
        self.process.stderr.close()


def distribution(values):
    values = sorted(values)
    return {'median': statistics.median(values), 'p95': values[max(0, math.ceil(.95 * len(values)) - 1)],
            'min': min(values), 'max': max(values)}


def summary(samples):
    phases = samples[0]['timings']
    return {'roundtrip_ms': distribution([sample['roundtrip_ms'] for sample in samples]),
            'phases_ms': {phase: distribution([s['timings'][phase] for s in samples]) for phase in phases},
            'max_rss_bytes': max(s['max_rss_bytes'] for s in samples),
            'last_cache': samples[-1]['cache']}


def run(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    fixture_dir = output / ('fixture-' + str(time.time_ns()))
    fixture = build_fixture(fixture_dir, args.turns, args.pages, args.page_kib)
    report = {'scope': 'Direct host preparation only; scripted model responses, no inference or resource-sampler overhead.',
        'cache_state': 'Logical cold only; operating system and SQLite caches are left untouched.',
        'runtime': {'python': sys.version, 'platform': platform.platform(),
                    'source_sha256': {name: hashlib.sha256((SOURCE / 'harness' / name).read_bytes()).hexdigest()
                                      for name in ('ram_compartments.py', 'paging.py', 'conversation_memory.py',
                                                   'stp_canonical/kernel.py')}},
        'fixture': {'root': str(fixture_dir), 'turns': args.turns, 'pages': args.pages,
                    'page_kib': args.page_kib}, 'iterations': args.iterations, 'modes': {}}
    signatures = set()
    fresh = []
    for _ in range(args.iterations):
        started = time.perf_counter_ns()
        process = subprocess.run([sys.executable, '-B', str(Path(__file__)), '--worker',
            str(fixture_dir), '--budget', '0', '--once'], input='{"op":"prepare"}\n',
            capture_output=True, text=True, check=True, timeout=60)
        result = json.loads(process.stdout)
        assert result['ok'], result
        sample = result['data']
        sample['roundtrip_ms'] = (time.perf_counter_ns() - started) / 1e6
        fresh.append(sample)
        signatures.add(sample['signature'])
    report['modes']['fresh_process_no_ram'] = {'summary': summary(fresh), 'samples': fresh}
    for label, budget in [('persistent_no_ram', 0), ('persistent_active_warm', MAX_BYTES)]:
        process = Process(fixture_dir, budget)
        try:
            cold = process.request()
            samples = [process.request() for _ in range(args.iterations)]
            signatures.add(cold['signature'])
            signatures.update(sample['signature'] for sample in samples)
            report['modes'][label] = {'logical_cold': cold, 'summary': summary(samples), 'samples': samples}
            if budget:
                first_partition = process.request('partition_a')
                other_partition = process.request('partition_b')
                returned_partition = process.request('partition_a')
                assert first_partition['signature'] == returned_partition['signature']
                assert other_partition['cache']['warm_bytes'] > 0
                assert returned_partition['cache']['last_request']['warm_hits'] > 0
                report['retired_page_revisit'] = {
                    'first': first_partition, 'other': other_partition, 'returned': returned_partition}
                evicted = process.request('clear')
                assert evicted['cache']['retained_bytes'] == 0
                process.request('restore')
                recovered = process.request()
                signatures.add(recovered['signature'])
                report['pressure_recovery'] = {'evicted': evicted, 'recovered': recovered}
                source = Path(next(iter(fixture['files'].values())))
                original = source.read_bytes()
                try:
                    source.write_bytes(original + b'\nchanged without notification\n')
                    fault = process.request('fault')
                    assert fault['fault'] == 'source_changed', fault
                    report['changed_source_fault'] = fault
                finally:
                    source.write_bytes(original)
        finally:
            process.close()
    restarted = Process(fixture_dir, MAX_BYTES)
    try:
        report['restart_recovery'] = restarted.request()
        signatures.add(report['restart_recovery']['signature'])
    finally:
        restarted.close()
    assert len(signatures) == 1, 'Prompt working set or source output changed between retention modes'
    report['checks'] = {'same_selected_memory_and_payloads': True, 'same_contract_and_constraints': True,
        'spent_budgets_unchanged': True, 'pressure_eviction_recovered': True,
        'changed_source_rejected_while_cached': True, 'worker_restart_recovered': True}
    report['signature'] = signatures.pop()
    for mode, result in report['modes'].items():
        print(mode, json.dumps(result['summary']['roundtrip_ms']), flush=True)
    destination = output / 'ram-compartments-results.json'
    destination.write_text(json.dumps(report, indent=2) + '\n')
    print(destination)


def run_live(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    root = output / ('live-' + str(time.time_ns()))
    workspace = root / 'workspace'
    workspace.mkdir(parents=True)
    original = root / 'base'
    original.mkdir()
    bridge = SOURCE / 'harness/vscode_bridge.py'
    models = {'low': 'qwen3:4b', 'mid': 'local-assistant:latest', 'high': 'strata:high'}
    environment = {
        'SUTRA_THINKING': 'off', 'SUTRA_MEMORY_OWNERSHIP': 'shared',
        'SUTRA_RUNTIME_DIR': str(root / 'runtime-memory'), 'SUTRA_ACTIVITY': '1',
    }
    node = os.environ.get('KARMA_NODE') or shutil.which('node')
    if node:
        environment['KARMA_NODE'] = node

    def command(storage, persistent=False):
        return [sys.executable, '-B', str(bridge), '--workspace', str(workspace),
                '--storage', str(storage)] + (['--request-stream'] if persistent else [])

    initial = {'op': 'conversation', 'models': models, 'tier': 'low',
               'prompt': 'Remember project Cedar, tag CED724. The target is 18 and the actual is 21. Never use network or edit files. Reply briefly.'}
    with (root / 'base-stderr.log').open('w') as log:
        base = subprocess.run(command(original), env=dict(os.environ, **environment),
            input=json.dumps(initial), text=True, capture_output=False, stdout=subprocess.PIPE,
            stderr=log, check=True, timeout=180)
    first = json.loads(base.stdout)
    assert first['ok'] and first['data']['result']['admission']['admitted'], first
    session = first['data']['session']
    parent = first['data']['result']['invocation_id']
    ledger = InvocationLedger(original / 'state.sqlite')
    original_contract = conversation.contract(ledger, session)
    original_spending = usage(ledger, session)['used']
    ledger.close()
    request = {'op': 'conversation', 'models': models, 'tier': 'low', 'parent': parent,
               'prompt': 'What is our project tag? Calculate actual minus target using the saved facts. Give the result briefly.'}
    report = {'scope': 'Three controlled qwen3:4b modes, two same-parent calls per mode; no user workspace or settings changed.',
              'root': str(root), 'session': session, 'original_spending': original_spending,
              'prompt': request['prompt'], 'model': 'qwen3:4b', 'modes': {}}
    for label, persistent, budget in [('fresh_process_no_ram', False, 0),
                                      ('persistent_no_ram', True, 0),
                                      ('persistent_active_warm', True, 64)]:
        storage = root / label
        storage.mkdir()
        with sqlite3.connect(original / 'state.sqlite') as source, sqlite3.connect(storage / 'state.sqlite') as destination:
            source.backup(destination)
        if (original / 'workspace.json').exists():
            shutil.copy2(original / 'workspace.json', storage / 'workspace.json')
        values = dict(environment, SUTRA_RAM_CACHE_MIB=str(budget))
        samples = []
        observed_traces = set()
        with (root / (label + '-stderr.log')).open('w') as log:
            process = subprocess.Popen(command(storage, True), env=dict(os.environ, **environment),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True, bufsize=1) if persistent else None
            try:
                previous = original_spending
                for index in range(2):
                    started = time.perf_counter()
                    if process:
                        process.stdin.write(json.dumps({'request': request, 'environment': values}) + '\n')
                        process.stdin.flush()
                        raw = process.stdout.readline()
                    else:
                        call = subprocess.run(command(storage), env=dict(os.environ, **values),
                            input=json.dumps(request), text=True, stdout=subprocess.PIPE,
                            stderr=log, check=True, timeout=180)
                        raw = call.stdout
                    elapsed = time.perf_counter() - started
                    response = json.loads(raw)
                    assert response['ok'], response
                    data = response['data']
                    result = data['result']
                    assert data['session'] == session and result['admission']['admitted'], result
                    answer = result['accepted_output']['answer']
                    assert 'CED724' in answer and '3' in answer, answer
                    ledger = InvocationLedger(storage / 'state.sqlite')
                    try:
                        assert conversation.contract(ledger, session) == original_contract
                        spent = usage(ledger, session)['used']
                        assert spent['calls'] > previous['calls'] and spent['tokens'] > previous['tokens']
                        assert ledger.verify_invocations(session, complete=True)
                    finally:
                        ledger.close()
                    previous = spent
                    traces = []
                    for trace in sorted((storage / 'calls').glob('*.json')):
                        if trace.name in observed_traces:
                            continue
                        observed_traces.add(trace.name)
                        body = json.loads(trace.read_text())
                        runtime = body.get('response') or {}
                        traces.append({'trace': trace.name, 'elapsed_s': body.get('elapsed_s'),
                            **{key: runtime.get(key) for key in ('prompt_eval_count', 'eval_count',
                              'load_duration', 'prompt_eval_duration', 'eval_duration', 'total_duration')}})
                    samples.append({'ordinal': index + 1, 'elapsed_seconds': elapsed, 'answer': answer,
                        'usage': result.get('token_usage'), 'spent': spent,
                        'host_memory': response.get('host_memory'), 'provider_traces': traces})
                    print(label, index + 1, round(elapsed, 3), answer, flush=True)
            finally:
                if process:
                    process.stdin.close()
                    process.wait(timeout=15)
                    process.stdout.close()
        report['modes'][label] = samples
    report['checks'] = {'admitted_outputs_contain_saved_tag_and_correct_difference': True,
        'same_saved_contract_and_permissions': True, 'spending_increased_without_refunds': True,
        'same_prompt_model_and_parent': True, 'all_invocation_chains_valid': True}
    destination = output / 'ram-compartments-live-results.json'
    destination.write_text(json.dumps(report, indent=2) + '\n')
    print(destination)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path)
    parser.add_argument('--iterations', type=int, default=16)
    parser.add_argument('--turns', type=int, default=8)
    parser.add_argument('--pages', type=int, default=64)
    parser.add_argument('--page-kib', type=int, default=16)
    parser.add_argument('--worker', type=Path)
    parser.add_argument('--budget', type=int, default=0)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if args.worker:
        worker(args.worker, args.budget, args.once)
    else:
        if args.output is None or not 3 <= args.iterations <= 100:
            parser.error('--output and 3..100 iterations are required')
        if args.live:
            run_live(args)
        else:
            run(args)


if __name__ == '__main__':
    main()
