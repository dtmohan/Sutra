#!/usr/bin/env python3
"""Compare prompt cache modes on a private, disposable llama.cpp server.

Run on the inference host with an explicit llama-server and GGUF. This
experiment uses Sutra's wire format and count/stream readers, without a
workspace, remote registry, live server, or Sutra task ledger.
For short answer comparisons, use a chat template that honors the thinking
control. The report records reasoning bytes and output-limit stops separately.
GPU offload requires Apple Silicon unified memory. Discrete GPU allocation
on an RTX needs a separate GPU memory admission check.
"""
import argparse
from contextlib import closing, contextmanager
from hashlib import sha256
import http.client
import json
import math
import os
from pathlib import Path
import platform
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'harness'))

from context_manager import InvalidUpdate, canonical, sha
from karma_adapter import KarmaAdapter
from lifecycle import Interrupted, deadline
from local_model import StructuredOutputRejected
from monitoring import require_resources
from remote_context import CONTEXT_SLACK, _rpc, wire_request
from remote_control import headers, request_json
from remote_model import _json_answer, _read_stream
from token_usage import count


GIB = 1024 ** 3
EXPECTED = {'color': 'amber-circuit', 'value': 724}
SCHEMA = {'type': 'object', 'properties': {'color': {'type': 'string'}, 'value': {'type': 'integer'}},
          'required': ['color', 'value'], 'additionalProperties': False}
SCOPES = ('5af1dc68e3284da99b3571a6c49f2b90', '91d6bb73a42c4dfc90f2be7f8120d568')
TRIALS = (('seed_task', 0, 'Read the target record and return its color and value.'),
          ('same_task_followup', 0, 'Find the target row and return its color and value.'),
          ('same_task_exact_repeat', 0, 'Find the target row and return its color and value.'),
          ('same_task_early_change', 0, 'Find the target row and return its color and value.'),
          ('alternate_task', 1, 'Find the target row and return its color and value.'))


class BenchmarkError(Exception):
    pass


def digest(path):
    value = sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(8 * 1024 ** 2), b''):
            value.update(block)
    return value.hexdigest()


def document(rows):
    lines = [f'Record {index:04d} | class ordinary | color blue-metal | value {1200 + index}'
             for index in range(rows)]
    lines.insert(3 * rows // 4, 'Record TARGET | class selected | color amber-circuit | value 724')
    return '\n'.join(lines)


def fresh_admission(root, args):
    hardware = require_resources(root)
    sample = hardware['sample']
    available = (sample['available_bytes'] if sample.get('profile') == 'portable-memory-v1'
                 else sample['free_bytes'] + sample['inactive_bytes'])
    estimated = round(args.startup_memory_gib * GIB)
    reserve = round(args.system_reserve_gib * GIB)
    frame = KarmaAdapter().memory_allocation(estimated, available, reserve)
    if frame['decision'] != 'ALLOW':
        raise BenchmarkError(f'Startup memory admission returned {frame["decision"]}. '
                             f'{available / GIB:.2f} GiB available with '
                             f'{(estimated + reserve) / GIB:.2f} GiB required including reserve.')
    return {'available_bytes': available, 'estimated_startup_bytes': estimated,
            'system_reserve_bytes': reserve, 'decision': frame['decision']}


def bind_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def ready(process, profile, timeout):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if process.poll() is not None:
            raise BenchmarkError(f'The dedicated llama-server exited during startup with code {process.returncode}.')
        try:
            models = request_json(profile, '/v1/models', timeout_seconds=1)
            props = request_json(profile, '/props', timeout_seconds=1)
        except (InvalidUpdate, ValueError, OSError, http.client.HTTPException):
            time.sleep(.2)
            continue
        models = models.get('data') if isinstance(models, dict) else None
        aliases = [entry.get('id') for entry in models if isinstance(entry, dict)] if isinstance(models, list) else []
        settings = props.get('default_generation_settings') if isinstance(props, dict) else None
        template = props.get('chat_template') if isinstance(props, dict) else None
        if (aliases != [profile['model']] or not isinstance(props, dict) or props.get('total_slots') != 1
                or not isinstance(settings, dict) or settings.get('n_ctx') != profile['context_tokens']
                or not isinstance(template, str)
                or not 0 < len(template.encode()) <= 60000):
            raise BenchmarkError('The private endpoint did not attest the expected alias, context, single slot and chat template.')
        return {'template_sha256': sha(template), 'defaults_sha256': sha(canonical(settings))}
    raise BenchmarkError('The dedicated llama-server did not become ready before the startup deadline.')


def stop(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


@contextmanager
def server(args, alias):
    with tempfile.TemporaryDirectory(prefix='sutra-prefix-benchmark-') as scratch:
        root = Path(scratch)
        admission = fresh_admission(root, args)
        key = root / 'key'
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as file:
            file.write(secrets.token_hex(24) + '\n')
        profile = {'model': alias, 'port': bind_port(), 'context_tokens': args.n_ctx, 'api_key_file': str(key)}
        argv = [str(args.binary), '--model', str(args.gguf), '--alias', alias,
                '--host', '127.0.0.1', '--port', str(profile['port']), '--api-key-file', str(key),
                '--ctx-size', str(args.n_ctx), '--parallel', '1', '--cache-ram', '0', '--cache-reuse', '0',
                '--no-cache-idle-slots', '--no-context-shift', '--no-webui', '--no-agent', '--offline',
                '--jinja', '--flash-attn', 'on', '--cache-type-k', 'f16', '--cache-type-v', 'f16',
                '--n-gpu-layers', str(args.gpu_layers), '--threads', str(args.threads),
                '--reasoning-format', 'deepseek', '--reasoning', 'off']
        if args.template:
            argv.extend(['--chat-template-file', str(args.template)])
        if not args.gpu_layers:
            argv.extend(['--device', 'none'])
        elif args.device:
            argv.extend(['--device', args.device])
        # A caller's llama.cpp environment must not change the private worker.
        environment = {key: value for key, value in os.environ.items() if not key.startswith('LLAMA_')}
        started = time.monotonic()
        process = subprocess.Popen(argv, cwd=root, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=os.name != 'nt')
        try:
            identity = ready(process, profile, args.startup_timeout)
            yield profile, {**identity, 'startup_seconds': time.monotonic() - started, 'admission': admission}
        finally:
            stop(process)


def number(value):
    if type(value) in (int, float) and 0 <= value < 2 ** 53 and math.isfinite(value):
        return value
    return None


def run_call(args, profile, source, trial, cache):
    name, scope, instruction = trial
    system = (f'{SCOPES[scope]}\nThe first line is a scope marker. '
              'Read the synthetic registry. Use only the TARGET record to answer. '
              'Return exactly one JSON object with its color and numeric value.')
    header = 'Backup registry' if name == 'same_task_early_change' else 'Primary registry'
    payload = {'document': header + '\n' + source, 'request': instruction}
    body, _, _ = wire_request(profile, system, payload, SCHEMA, args.max_output)
    body['cache_prompt'] = cache
    body['chat_template_kwargs'] = {'enable_thinking': False}
    body['reasoning_budget_tokens'] = 0
    count_started = time.monotonic()
    exact = _rpc(profile, body)
    count_seconds = time.monotonic() - count_started
    if exact + args.max_output + CONTEXT_SLACK > args.n_ctx:
        raise BenchmarkError(f'The fixture needs {exact} prompt tokens before output and framing reserve. Reduce --rows.')

    request = canonical(body)
    transport = {'frames_received': 0, 'content_bytes': 0, 'reasoning_bytes': 0}
    with deadline(args.call_timeout):
        started = time.monotonic()
        with closing(http.client.HTTPConnection('127.0.0.1', profile['port'], timeout=args.call_timeout)) as connection:
            connection.request('POST', '/v1/chat/completions', request, headers(profile))
            transport_socket = connection.sock
            with connection.getresponse() as response:
                if response.status != 200:
                    raise BenchmarkError(f'The private generation endpoint returned HTTP {response.status}.')
                measured = _read_stream(response, transport_socket, started, 65536, None, transport)
        wall = time.monotonic() - started

    answer_text = measured['message']['content']
    try:
        answer = _json_answer(answer_text)
    except StructuredOutputRejected:
        answer = None
    timing = measured['timings'] or {}
    cached = count(timing.get('cache_n'))
    prefill = count(timing.get('prompt_n'))
    input_tokens = measured['prompt_eval_count']
    return {'trial': name, 'wire_sha256': sha(request), 'message_sha256': sha(canonical(body['messages'])),
            'count_seconds': count_seconds, 'exact_prompt_tokens': exact, 'prompt_tokens': input_tokens,
            'prompt_count_matches': exact == input_tokens, 'completion_tokens': measured['eval_count'],
            'cached_tokens': cached, 'prefill_tokens': prefill,
            'prefill_plus_cache_matches_prompt': cached + prefill == exact if cached is not None and prefill is not None else None,
            'prefill_ms': number(timing.get('prompt_ms')), 'decode_tokens': count(timing.get('predicted_n')),
            'decode_ms': number(timing.get('predicted_ms')), 'wall_seconds': wall,
            'first_answer_seconds': measured['first_answer_chunk_s'], 'finish': measured['done_reason'],
            'reasoning_bytes': transport['reasoning_bytes'], 'response_bytes': transport['content_bytes'],
            'structured_facts_match': measured['done_reason'] == 'stop' and answer == EXPECTED,
            'structured_answer_sha256': sha(canonical(answer)) if answer is not None else None,
            'answer_text_sha256': sha(answer_text) if answer_text else None}


def compare(arms):
    by_mode = {arm['cache_prompt']: {row['trial']: row for row in arm['calls']} for arm in arms}
    cold, warm = by_mode[False], by_mode[True]
    rows = {}
    for name in cold:
        left, right = cold[name], warm[name]
        rows[name] = {'same_message': left['message_sha256'] == right['message_sha256'],
                      'same_full_prompt_count': left['exact_prompt_tokens'] == right['exact_prompt_tokens'],
                      'both_runtime_counts_match': left['prompt_count_matches'] and right['prompt_count_matches'],
                      'both_structured_facts_match': left['structured_facts_match'] and right['structured_facts_match'],
                      'same_structured_answer': left['structured_answer_sha256'] is not None
                      and left['structured_answer_sha256'] == right['structured_answer_sha256'],
                      'same_answer_text': left['answer_text_sha256'] is not None
                      and left['answer_text_sha256'] == right['answer_text_sha256']}
    return rows


def experiment(args):
    with args.gguf.open('rb') as source:
        if source.read(4) != b'GGUF':
            raise BenchmarkError('The selected weights file is not GGUF.')
    source = document(args.rows)
    identity = {'executable_sha256': digest(args.binary), 'gguf_sha256': digest(args.gguf),
                'template_file_sha256': digest(args.template) if args.template else None}
    report = {'version': 1, 'benchmark': 'sutra-isolated-prefix-reuse', **identity,
              'context_tokens': args.n_ctx, 'output_reserve': args.max_output, 'rows': args.rows,
              'device': args.device, 'gpu_layers': args.gpu_layers, 'reasoning_requested': 'off', 'seed': 42,
              'document_sha256': sha(source), 'status': 'complete', 'trials': []}
    alias = 'sutra-prefix-' + secrets.token_hex(8)
    for index in range(args.repeats):
        entry = {'index': index + 1, 'arms': []}
        report['trials'].append(entry)
        for cache in ((False, True) if index % 2 == 0 else (True, False)):
            print(f'Prefix benchmark trial {index + 1} with cache {"on" if cache else "off"}.', file=sys.stderr, flush=True)
            arm = {'cache_prompt': cache, 'calls': []}
            entry['arms'].append(arm)
            try:
                with server(args, alias) as (profile, runtime):
                    arm['runtime'] = runtime
                    for trial in TRIALS:
                        observation = run_call(args, profile, source, trial, cache)
                        arm['calls'].append(observation)
                        print(f'  {trial[0]}: {observation["finish"]}, {observation["cached_tokens"]} cached tokens.',
                              file=sys.stderr, flush=True)
            except (Interrupted, KeyboardInterrupt):
                report['status'] = 'interrupted'
                return report
            except (BenchmarkError, InvalidUpdate, OSError, ValueError, RuntimeError, http.client.HTTPException) as error:
                arm['error'] = {'kind': type(error).__name__}
                if isinstance(error, BenchmarkError):
                    arm['error']['message'] = str(error)
                report['status'] = 'failed'
                return report
        arms = entry['arms']
        if not all(arms[0]['runtime'][key] == arms[1]['runtime'][key] for key in ('template_sha256', 'defaults_sha256')):
            entry['error'] = 'The live template or server defaults changed between arms.'
            report['status'] = 'failed'
            return report
        entry['comparison'] = compare(arms)
    return report


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True, type=Path)
    parser.add_argument('--gguf', required=True, type=Path)
    parser.add_argument('--template', type=Path)
    parser.add_argument('--n-ctx', type=int, default=8192)
    parser.add_argument('--gpu-layers', type=int, default=99,
                        help='Offload on Apple Silicon only. Choose 0 for CPU.')
    parser.add_argument('--device')
    parser.add_argument('--threads', type=int, default=8)
    parser.add_argument('--startup-memory-gib', type=float, required=True)
    parser.add_argument('--system-reserve-gib', type=float, default=4)
    parser.add_argument('--startup-timeout', type=int, default=90)
    parser.add_argument('--call-timeout', type=int, default=150)
    parser.add_argument('--max-output', type=int, default=1536)
    parser.add_argument('--rows', type=int, default=280)
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    for key, lower, upper in (('n_ctx', 4096, 65536), ('gpu_layers', 0, 999), ('threads', 1, 64),
                              ('startup_memory_gib', .25, 1024), ('system_reserve_gib', 1, 1024),
                              ('startup_timeout', 1, 600), ('call_timeout', 1, 150), ('max_output', 16, 4096),
                              ('rows', 1, 2048), ('repeats', 1, 10)):
        if not lower <= getattr(args, key) <= upper:
            parser.error(f'--{key.replace("_", "-")} must be {lower}..{upper}')
    if args.gpu_layers and (sys.platform != 'darwin' or platform.machine() != 'arm64'):
        parser.error('GPU offload requires Apple Silicon unified memory. Discrete GPUs need a separate memory gate.')
    if not args.gpu_layers and args.device not in (None, 'none'):
        parser.error('CPU runs require --device none or no device selection')
    for key in ('binary', 'gguf', 'template'):
        path = getattr(args, key)
        if path is not None:
            if not path.is_file():
                parser.error(f'--{key} must identify an existing file')
            setattr(args, key, path.resolve())
    if args.output and args.output.exists():
        parser.error('--output must be a new file')
    return args


def main(argv=None):
    args = arguments(argv)
    def interrupt(_signum, _frame):
        raise Interrupted('Benchmark interrupted')
    previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        report = experiment(args)
        result = json.dumps(report, indent=2, allow_nan=False) + '\n'
        if args.output:
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as file:
                file.write(result)
        else:
            print(result, end='')
        if report['status'] != 'complete':
            print('The private benchmark stopped. The report retains completed measurements.', file=sys.stderr)
        return int(report['status'] != 'complete')
    except BenchmarkError as error:
        print(str(error), file=sys.stderr)
    except (Interrupted, KeyboardInterrupt):
        print('The benchmark stopped and cleaned up its private server.', file=sys.stderr)
    except (InvalidUpdate, OSError, ValueError, RuntimeError, http.client.HTTPException) as error:
        print(f'The benchmark could not complete ({type(error).__name__}).', file=sys.stderr)
    finally:
        signal.signal(signal.SIGTERM, previous)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
