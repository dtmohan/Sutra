"""Authenticated local client for the Sutra-owned engine worker."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import sys
import time

from context_manager import InvalidUpdate
from lifecycle import timeout
from platform_support import lock_file

VERSION = 1
FRAME_LIMIT = 2 * 1024 * 1024


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
    parent = Path('/tmp') / ('sutra-engine-' + str(uid) + '-' + key)
    parent.mkdir(mode=0o700, exist_ok=True)
    info = parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077:
        raise InvalidUpdate('The Sutra engine socket directory is not private')
    return parent / 'worker.sock'


def runtime_hash():
    root = Path(__file__).parent
    digest = hashlib.sha256()
    # Persistent workers cache imports, including admission adapters. Bind their
    # complete executable harness and policy data so an upgrade cannot reuse
    # yesterday's resource checks behind a matching transport identity.
    files = sorted(file for file in root.rglob('*')
                   if file.is_file() and file.suffix in ('.py', '.js', '.json')
                   and not file.name.startswith('test_') and '__pycache__' not in file.parts)
    for file in files:
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


def metadata(root):
    path = root / 'worker.json'
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        raw = stream.read(8193)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or len(raw) > 8192:
        raise EngineServiceError('Invalid private engine worker record')
    value = json.loads(raw)
    if (not isinstance(value, dict) or value.get('version') != VERSION or
            value.get('socket') != str(socket_path(root)) or
            not isinstance(value.get('token'), str) or len(value['token']) != 64):
        raise EngineServiceError('Invalid engine worker identity')
    return value


def _failure(result, operation=None):
    reason = str(result.get('error') or 'The engine worker did not confirm the operation')[:1000]
    if result.get('status') == 'timeout' and operation == 'generate':
        error = TimeoutError(reason)
        error.engine_usage = result.get('usage')
        raise error
    if result.get('status') in ('busy', 'paused', 'timeout', 'uncertain'):
        from monitoring import ResourceWait
        raise ResourceWait({'profile': 'sutra-engine-v1', 'available': False,
                            'admitted': False, 'decision': result.get('decision'), 'reason': reason})
    raise EngineServiceError(reason)


def _exchange(record, request, *, seconds=150, on_thinking=None, on_progress=None):
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout(seconds))
    stream = None
    try:
        connection.connect(record['socket'])
        raw = json.dumps(dict(request, version=VERSION, token=record['token']),
                         separators=(',', ':'), ensure_ascii=False).encode() + b'\n'
        if len(raw) > FRAME_LIMIT:
            raise EngineServiceError('Engine request exceeds the transport allocation')
        connection.sendall(raw)
        stream = connection.makefile('rb')
        total = 0
        while True:
            frame = stream.readline(FRAME_LIMIT + 1)
            if not frame:
                raise EngineServiceError('Engine worker closed an incomplete request')
            total += len(frame)
            if not frame.endswith(b'\n') or len(frame) > FRAME_LIMIT or total > 16 * FRAME_LIMIT:
                raise EngineServiceError('Engine response exceeds the transport allocation')
            event = json.loads(frame)
            if not isinstance(event, dict):
                raise EngineServiceError('Invalid engine worker frame')
            kind = event.get('event')
            if kind == 'thinking':
                text = event.get('text')
                if not isinstance(text, str):
                    raise EngineServiceError('Invalid engine reasoning frame')
                if on_thinking:
                    on_thinking(text)
            elif kind == 'progress':
                if on_progress:
                    on_progress(event.get('data', {}))
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
        if stream is not None:
            stream.close()
        connection.close()


def ensure_service():
    root = directory()
    from engine_profiles import registry_path
    registry = str(registry_path())
    executable = os.environ.get('SUTRA_ENGINE_PYTHON')
    with lock_file(root / 'startup.lock'):
        try:
            record = metadata(root)
            live = _exchange(record, {'op': 'ping'}, seconds=2)
        except (OSError, ValueError, EngineServiceError):
            record, live = None, None
        if live is not None:
            if (live.get('runtime_hash') == runtime_hash() and record.get('registry') == registry
                    and record.get('python') == executable):
                return record
            if live.get('busy'):
                _failure({'status': 'busy', 'error': 'The previous engine worker is still busy; wait before upgrading it'})
            _exchange(record, {'op': 'shutdown'}, seconds=5)
            for _ in range(50):
                if not (root / 'worker.json').exists():
                    break
                time.sleep(.05)
            if (root / 'worker.json').exists():
                _failure({'status': 'uncertain', 'error': 'The previous engine worker has not confirmed shutdown; retry after it settles'})
        if not executable or not Path(executable).is_absolute() or not Path(executable).is_file():
            raise EngineServiceError('Set up Sutra engine dependencies in Model settings before loading a model')
        environment = {key: value for key, value in os.environ.items()
                       if key not in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV') and not key.startswith('STRATA_')}
        log_path = root / 'worker.log'
        if log_path.exists() and log_path.stat().st_size > 1024 * 1024:
            log_path.replace(root / 'worker.previous.log')
        with log_path.open('ab') as log:
            process = subprocess.Popen([executable, '-B', str(Path(__file__).with_name('engine_service.py')),
                                        '--directory', str(root), '--registry', registry],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       env=environment, start_new_session=True)
        for _ in range(100):
            if process.poll() is not None:
                raise EngineServiceError('Sutra engine worker exited during startup; inspect ' + str(log_path))
            try:
                record = metadata(root)
                live = _exchange(record, {'op': 'ping'}, seconds=2)
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
    ref = profile.get('ref') or profile['name']
    result = _exchange(ensure_service(), {'op': 'load', 'ref': ref,
                                         'profile_hash': profile['profile_hash']}, seconds=180)
    if result.get('profile_hash') != profile['profile_hash']:
        raise EngineServiceError('Loaded engine profile differs from the admitted profile')
    return result


def generate(profile, messages, max_tokens, thinking, schema, on_thinking=None,
             on_progress=None, context_tokens=None, expected_instance_id=None):
    result = _exchange(ensure_service(), {'op': 'generate', 'ref': profile.get('ref') or profile['name'],
        'profile_hash': profile['profile_hash'], 'messages': messages, 'max_tokens': max_tokens,
        'thinking': thinking, 'schema': schema, 'context_tokens': context_tokens,
        'expected_instance_id': expected_instance_id}, seconds=175,
        on_thinking=on_thinking, on_progress=on_progress)
    if expected_instance_id is not None and result.get('instance_id') != expected_instance_id:
        raise EngineServiceError('The engine instance changed during inference')
    return result


def status():
    try:
        return _exchange(metadata(directory()), {'op': 'status'}, seconds=2)
    except (OSError, ValueError, EngineServiceError):
        return {'ok': True, 'running': False, 'busy': False, 'profile': None}


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
