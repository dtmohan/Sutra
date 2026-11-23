"""Authenticated local client for the Sutra-owned engine worker."""
import ast
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import sys
import time
import tempfile
import re

from context_manager import InvalidUpdate
from lifecycle import timeout
from platform_support import lock_file

VERSION = 2
FRAME_LIMIT = 2 * 1024 * 1024
RUNTIME_FILES = ('engine_client.py', 'engine_service.py', 'engine_process.py',
                 'engine_codec.py', 'engine_profiles.py', 'provider_startup.py')


class EngineServiceError(RuntimeError):
    pass


def directory():
    value = os.environ.get('SUTRA_RUNTIME_DIR')
    if not value or not Path(value).is_absolute():
        raise InvalidUpdate('Sutra engine management requires an absolute runtime directory')
    path = Path(value) / 'engine'
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise InvalidUpdate('Invalid Sutra engine runtime directory')
    return path


def socket_path(root):
    # macOS AF_UNIX paths are limited to roughly 100 bytes. VS Code's storage
    # path is longer, so only the authenticated socket uses a private short path.
    uid = os.getuid()
    key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:20]
    parent = Path(tempfile.mkdtemp(prefix='sutra-eng-' + str(uid) + '-' + key + '-', dir='/tmp'))
    return parent / 'worker.sock'


def validate_socket(root, endpoint, legacy=False):
    path = Path(endpoint)
    key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:20]
    parent = path.parent
    current = re.fullmatch('sutra-eng-' + str(os.getuid()) + '-' + key + '-[a-z0-9_]{8}', parent.name) is not None
    previous = legacy and parent.name == 'sutra-engine-' + str(os.getuid()) + '-' + key
    if path.name != 'worker.sock' or parent.parent != Path('/tmp') or not (current or previous):
        raise EngineServiceError('The engine socket does not belong to this runtime directory')
    info = parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise InvalidUpdate('The Sutra engine socket directory is not private')
    return path


def runtime_hash():
    root = Path(__file__).parent
    digest = hashlib.sha256()
    # Persistent workers cache imports, including admission adapters. Bind their
    # complete executable harness and policy data so an upgrade cannot reuse
    # yesterday's resource checks behind a matching transport identity.
    pending = [root / name for name in RUNTIME_FILES + ('provider_startup.py',)]
    files = set()
    while pending:
        file = pending.pop()
        if file in files or not file.is_file():
            continue
        files.add(file)
        tree = ast.parse(file.read_text())
        for node in ast.walk(tree):
            names = ([item.name for item in node.names] if isinstance(node, ast.Import) else
                     [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
            for name in names:
                dependency = root / (name.replace('.', '/') + '.py')
                if dependency.is_file() and dependency not in files:
                    pending.append(dependency)
    files.update(root / name for name in ('canonical-kernel.json', 'memory-policy.json', 'temperature-provenance.json'))
    files.update((root / 'memory_profiles').glob('*.json'))
    for file in sorted(files):
        if not file.is_file():
            continue
        digest.update(file.relative_to(root).as_posix().encode())
        digest.update(file.read_bytes())
    for name in ('telemetry', 'telemetry-build.json'):
        file = root.parent / 'bin' / name
        if file.exists():
            digest.update(('bin/' + name).encode())
            digest.update(file.read_bytes())
    vendor = root.parent / 'vendor' / 'sutra_engine'
    for file in sorted(vendor.rglob('*.py')) if vendor.exists() else []:
        digest.update(file.relative_to(root.parent).as_posix().encode())
        digest.update(file.read_bytes())
    requirements = root.parent / 'requirements-engine.txt'
    if requirements.exists():
        digest.update(requirements.read_bytes())
    return digest.hexdigest()


def metadata(root, allow_legacy=False):
    path = root / 'worker.json'
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        raw = stream.read(8193)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1 or len(raw) > 8192:
        raise EngineServiceError('Invalid private engine worker record')
    value = json.loads(raw)
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value.get('version') not in ((1, VERSION) if allow_legacy else (VERSION,)) or
            not isinstance(value.get('token'), str) or re.fullmatch(r'[a-f0-9]{64}', value['token']) is None or
            type(value.get('pid')) is not int or value['pid'] <= 0):
        raise EngineServiceError('Invalid engine worker identity')
    validate_socket(root, value.get('socket', ''), legacy=allow_legacy)
    return value


def signed(value, secret):
    body = dict(value)
    raw = json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
    body['mac'] = hmac.new(secret.encode(), b'sutra-engine-v2:' + raw, hashlib.sha256).hexdigest()
    return body


def verified(value, secret):
    if not isinstance(value, dict):
        raise EngineServiceError('Invalid engine authenticated frame')
    body = {key: item for key, item in value.items() if key != 'mac'}
    try:
        expected = signed(body, secret)['mac']
    except (ValueError, TypeError, OverflowError):
        raise EngineServiceError('Invalid canonical engine frame') from None
    if not isinstance(value.get('mac'), str) or not hmac.compare_digest(value['mac'], expected):
        raise EngineServiceError('Engine frame authentication failed')
    return body


def progress_frame(value):
    if (not isinstance(value, dict) or set(value) - {'read', 'total', 'milliseconds', 'tokens_per_second'} or
            any(type(value.get(key)) is not int or not 0 <= value[key] <= 262144 for key in ('read', 'total')) or
            value['read'] > value['total']):
        raise EngineServiceError('Invalid bounded engine progress frame')
    for key in ('milliseconds', 'tokens_per_second'):
        if key in value and (type(value[key]) not in (int, float) or not math.isfinite(value[key]) or not 0 <= value[key] <= 1e12):
            raise EngineServiceError('Invalid engine progress measurement')
    return value


def observed_usage(value):
    if (not isinstance(value, dict) or any(type(value.get(key)) is not int or not 0 <= value[key] <= 262144
                                         for key in ('prompt_tokens', 'completion_tokens')) or
            value.get('finish_reason') not in ('cancelled', 'error', 'stop', 'length')):
        raise EngineServiceError('Invalid engine usage observation')
    if 'elapsed_s' in value and (type(value['elapsed_s']) not in (int, float) or not math.isfinite(value['elapsed_s']) or value['elapsed_s'] < 0):
        raise EngineServiceError('Invalid engine usage duration')
    return value


def read_line(connection, pending, until, limit=FRAME_LIMIT):
    """Read one frame against an absolute deadline, including a dribbling peer."""
    while True:
        position = pending.find(b'\n')
        if position >= 0:
            if position >= limit:
                raise EngineServiceError('Engine response exceeds the frame allocation')
            raw = bytes(pending[:position + 1])
            del pending[:position + 1]
            return raw
        if len(pending) >= limit:
            raise EngineServiceError('Engine response exceeds the frame allocation')
        remaining = until - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Engine exchange exceeded its absolute deadline')
        connection.settimeout(remaining)
        block = connection.recv(min(65536, limit - len(pending)))
        if not block:
            raise EngineServiceError('Engine worker closed an incomplete request')
        pending.extend(block)


def _failure(result, operation=None):
    reason = str(result.get('error') or 'The engine worker did not confirm the operation')[:1000]
    if result.get('status') == 'timeout' and operation == 'generate':
        error = TimeoutError(reason)
        error.engine_usage = observed_usage(result['usage']) if result.get('usage') is not None else None
        raise error
    if result.get('status') in ('busy', 'paused', 'timeout', 'uncertain'):
        from monitoring import ResourceWait
        status = result.get('status')
        blocker = {'busy': 'runtime_busy', 'timeout': 'runtime_timeout', 'uncertain': 'runtime_uncertain'}.get(status)
        blocker = blocker or ('model_admission' if result.get('decision') else 'measurement_unavailable')
        raise ResourceWait({'profile': 'sutra-engine-v2', 'provider': 'sutra_engine',
                            'available': False, 'admitted': False, 'decision': result.get('decision'),
                            'reason': reason, 'status': status, 'blocker': blocker, 'observed_at': time.time(),
                            'blocking_measurement': result.get('blocking_measurement')})
    raise EngineServiceError(reason)


def _exchange(record, request, *, seconds=150, on_thinking=None, on_progress=None):
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    duration = timeout(seconds)
    until = time.monotonic() + duration
    connection.settimeout(duration)
    try:
        connection.connect(record['socket'])
        nonce = secrets.token_hex(16)
        raw = json.dumps(signed({'version': VERSION, 'nonce': nonce, 'sent_at': int(time.time()), 'request': request}, record['token']),
                         separators=(',', ':'), ensure_ascii=False).encode() + b'\n'
        if len(raw) > FRAME_LIMIT:
            raise EngineServiceError('Engine request exceeds the transport allocation')
        connection.sendall(raw)
        pending = bytearray()
        total = 0
        sequence = 0
        while True:
            frame = read_line(connection, pending, until)
            total += len(frame)
            if not frame.endswith(b'\n') or len(frame) > FRAME_LIMIT or total > 16 * FRAME_LIMIT:
                raise EngineServiceError('Engine response exceeds the transport allocation')
            event = verified(json.loads(frame), record['token'])
            if event.get('nonce') != nonce or type(event.get('seq')) is not int or event['seq'] != sequence:
                raise EngineServiceError('Engine response nonce or sequence does not match the request')
            sequence += 1
            kind = event.get('event')
            if kind == 'thinking':
                text = event.get('text')
                if not isinstance(text, str):
                    raise EngineServiceError('Invalid engine reasoning frame')
                if on_thinking:
                    on_thinking(text)
            elif kind == 'progress':
                data = progress_frame(event.get('data'))
                if on_progress:
                    on_progress(data)
            elif kind == 'result':
                result = event.get('data')
                if not isinstance(result, dict):
                    raise EngineServiceError('Invalid engine result')
                if result.get('ok') is not True:
                    _failure(result, request.get('op'))
                return result
            else:
                raise EngineServiceError('Unknown engine worker frame')
    finally:
        connection.close()


def _retire_legacy(record):
    """Only retire an authenticated idle v1 worker; never dispatch a legacy model call."""
    def operation(kind):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(timeout(2))
        try:
            connection.connect(record['socket'])
            connection.sendall(json.dumps({'op': kind, 'version': 1, 'token': record['token']}).encode() + b'\n')
            raw = read_line(connection, bytearray(), time.monotonic() + 2, 16384)
            if len(raw) > 16384 or not raw.endswith(b'\n'):
                raise EngineServiceError('The previous worker did not confirm bounded retirement')
            value = json.loads(raw)
            result = value.get('data') if isinstance(value, dict) and value.get('event') == 'result' else None
            if not isinstance(result, dict) or result.get('ok') is not True:
                raise EngineServiceError('The previous worker did not confirm retirement')
            return result
        finally:
            connection.close()
    if operation('status').get('busy'):
        _failure({'status': 'busy', 'error': 'The previous Sutra worker is busy; wait before upgrading its protocol'})
    operation('shutdown')


def _wait_for_retirement(root, until):
    while True:
        if not (root / 'worker.json').exists():
            try:
                with lock_file(root / 'owner.lock'):
                    return
            except InvalidUpdate:
                pass
        remaining = until - time.monotonic()
        if remaining <= 0:
            _failure({'status': 'uncertain',
                      'error': 'The previous engine worker has not released ownership; no replacement was started'})
        time.sleep(min(.05, remaining))


def ensure_service():
    startup_until = time.monotonic() + 20
    root = directory()
    from engine_profiles import registry_path
    registry = str(registry_path())
    executable = os.environ.get('SUTRA_ENGINE_PYTHON')
    with lock_file(root / 'startup.lock'):
        try:
            record = metadata(root, allow_legacy=True)
            if record.get('version') == 1:
                _retire_legacy(record)
                _wait_for_retirement(root, min(startup_until, time.monotonic() + 5))
                try:
                    Path(record['socket']).parent.rmdir()
                except OSError:
                    pass
                record, live = None, None
            else:
                live = _exchange(record, {'op': 'ping'}, seconds=2)
        except (OSError, ValueError, EngineServiceError):
            record, live = None, None
            # A ping timeout does not establish that an owner died. Never fork a
            # competing worker while another process still holds the OS lease.
            try:
                with lock_file(root / 'owner.lock'):
                    pass
            except InvalidUpdate:
                _failure({'status': 'uncertain', 'error': 'The existing Sutra worker is unresponsive but still owns this runtime. Wait or stop that worker from its original Sutra window; no competing process was started.'})
        if live is not None:
            if (live.get('runtime_hash') == runtime_hash() and record.get('registry') == registry
                    and record.get('python') == executable):
                return record
            if live.get('busy'):
                _failure({'status': 'busy', 'error': 'The previous engine worker is still busy; wait before upgrading it'})
            _exchange(record, {'op': 'shutdown'}, seconds=5)
            _wait_for_retirement(root, min(startup_until, time.monotonic() + 5))
        if not executable or not Path(executable).is_absolute() or not Path(executable).is_file():
            raise EngineServiceError('Set up Sutra engine dependencies in Model settings before loading a model')
        from engine_process import sanitized_environment
        environment = sanitized_environment(worker=True)
        environment['PYTHONNOUSERSITE'] = '1'
        log_path = root / 'worker.log'
        if log_path.exists() and log_path.stat().st_size > 1024 * 1024:
            log_path.replace(root / 'worker.previous.log')
        with log_path.open('ab') as log:
            process = subprocess.Popen([executable, '-B', str(Path(__file__).with_name('engine_service.py')),
                                        '--directory', str(root), '--registry', registry],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       env=environment, start_new_session=True)
        until = startup_until
        while time.monotonic() < until:
            if process.poll() is not None:
                raise EngineServiceError('Sutra engine worker exited during startup; inspect ' + str(log_path))
            try:
                record = metadata(root)
                live = _exchange(record, {'op': 'ping'}, seconds=min(1, max(.01, until - time.monotonic())))
                if (live.get('runtime_hash') == runtime_hash() and record.get('registry') == registry
                        and record.get('python') == executable and record.get('pid') == process.pid):
                    return record
            except (OSError, ValueError, EngineServiceError):
                pass
            time.sleep(.1)
        # This handle refers only to the child created above, never a PID read
        # from an ownership file. No unknown engine process is terminated.
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        raise EngineServiceError('Sutra engine worker did not confirm startup')


def activate(profile):
    from engine_profiles import require_approval
    require_approval(profile)
    ref = profile.get('ref') or profile['name']
    result = _exchange(ensure_service(), {'op': 'load', 'ref': ref,
                                         'profile_hash': profile['profile_hash']}, seconds=180)
    if result.get('profile_hash') != profile['profile_hash']:
        raise EngineServiceError('Loaded engine profile differs from the admitted profile')
    return result


def generate(profile, messages, max_tokens, thinking, schema, on_thinking=None,
             on_progress=None, context_tokens=None, expected_instance_id=None):
    from engine_profiles import require_approval
    require_approval(profile)
    result = _exchange(ensure_service(), {'op': 'generate', 'ref': profile.get('ref') or profile['name'],
        'profile_hash': profile['profile_hash'], 'messages': messages, 'max_tokens': max_tokens,
        'thinking': thinking, 'schema': schema, 'context_tokens': context_tokens,
        'expected_instance_id': expected_instance_id}, seconds=175,
        on_thinking=on_thinking, on_progress=on_progress)
    if expected_instance_id is not None and result.get('instance_id') != expected_instance_id:
        raise EngineServiceError('The engine instance changed during inference')
    if result.get('profile_hash') != profile['profile_hash']:
        raise EngineServiceError('The generated engine profile differs from the admitted profile')
    return result


def status():
    try:
        return _exchange(metadata(directory()), {'op': 'status'}, seconds=2)
    except FileNotFoundError:
        return {'ok': True, 'running': False, 'busy': False, 'profile': None}
    except (OSError, ValueError, EngineServiceError) as error:
        return {'ok': False, 'running': None, 'busy': False, 'profile': None,
                'phase': 'uncertain', 'status': 'uncertain',
                'error': 'The existing Sutra worker could not confirm its state: ' + str(error)[:500]}


def unload():
    try:
        return _exchange(metadata(directory()), {'op': 'unload'}, seconds=10)
    except FileNotFoundError:
        return {'ok': True, 'running': False, 'profile': None}


def shutdown():
    try:
        return _exchange(metadata(directory()), {'op': 'shutdown'}, seconds=10)
    except FileNotFoundError:
        return {'ok': True, 'running': False}
