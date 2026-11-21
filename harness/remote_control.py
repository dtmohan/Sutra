"""Bounded trusted SSH and loopback control for remote CUDA providers.

The SSH alias, forwarding ports and scripts come from host code and the user
registry. No model text is used as shell code or as a connection destination.
"""
import base64
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading

from context_manager import InvalidUpdate, canonical, sha
from lifecycle import timeout
import remote_profiles


MAX_METADATA_BYTES = 1024 * 1024
_tunnel_lock = threading.Lock()


def _ssh():
    executable = shutil.which('ssh')
    if not executable or not Path(executable).is_absolute():
        raise InvalidUpdate('Remote provider requires an installed SSH client')
    return executable


def _options():
    return ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ForwardAgent=no', '-o', 'PermitLocalCommand=no',
            '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=15',
            '-o', 'ServerAliveCountMax=2']


def control_socket(profile):
    uid = str(os.getuid()) if hasattr(os, 'getuid') else 'user'
    # macOS TMPDIR can already be ~70 bytes. OpenSSH appends a temporary
    # suffix while binding, so use a short private directory below /private/tmp.
    short_root = (Path('/private/tmp') if sys.platform == 'darwin' else
                  Path('/tmp') if os.name == 'posix' else Path(tempfile.gettempdir()))
    directory = short_root / ('sutra-remote-' + uid)
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or
            hasattr(os, 'getuid') and info.st_uid != os.getuid()):
        raise InvalidUpdate('Remote SSH control directory must be private and user-owned')
    # Model/context/memory-policy revisions do not change this approved SSH
    # connection. Reuse only our private master for the same exact forwarding
    # authority; a different alias or either port receives a separate socket.
    authority = {field: profile[field] for field in ('ssh_alias', 'port', 'remote_port')}
    return directory / (sha(canonical(authority))[:24] + '.sock')


def ensure_tunnel(profile):
    """Start one pinned-key SSH forward, never adopt an arbitrary local server."""
    remote_profiles.require_assignment(profile)
    remote_profiles.api_key(profile)  # Fail before opening any new connection.
    target = control_socket(profile)
    command = [_ssh(), *_options(), '-S', str(target)]
    with _tunnel_lock:
        if target.exists():
            result = subprocess.run(command + ['-O', 'check', profile['ssh_alias']],
                                    capture_output=True, timeout=timeout(5))
            if result.returncode == 0:
                return {'ready': True, 'owned': True, 'port': profile['port']}
            # Only remove this profile's stale private Unix socket, not any
            # user SSH configuration or a different model's running tunnel.
            info = target.lstat()
            if not stat.S_ISSOCK(info.st_mode):
                raise InvalidUpdate('Remote SSH control path is not a private socket')
            target.unlink()
        probe = socket.socket()
        try:
            probe.settimeout(timeout(0.2))
            occupied = probe.connect_ex(('127.0.0.1', profile['port'])) == 0
        finally:
            probe.close()
        if occupied:
            raise InvalidUpdate('Remote provider forwarding port is already in use by an unowned endpoint; choose an unused local port')
        forward = '127.0.0.1:{}:127.0.0.1:{}'.format(profile['port'], profile['remote_port'])
        try:
            result = subprocess.run(command + ['-fNT', '-M', '-o', 'ExitOnForwardFailure=yes',
                                              '-L', forward, profile['ssh_alias']],
                                    capture_output=True, timeout=timeout(12))
        except (OSError, subprocess.SubprocessError):
            raise InvalidUpdate('Remote SSH tunnel is unavailable; verify the trusted alias and pinned host key') from None
        if result.returncode != 0:
            raise InvalidUpdate('Remote SSH tunnel could not be established with the approved alias and pinned host key')
    return {'ready': True, 'owned': True, 'port': profile['port']}


def headers(profile):
    return {'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + remote_profiles.api_key(profile)}


def request_json(profile, path, payload=None, timeout_seconds=5):
    if path not in ('/health', '/props', '/v1/models', '/slots'):
        raise InvalidUpdate('Unsupported remote provider metadata operation')
    if payload is not None:
        raise InvalidUpdate('Remote provider metadata reads cannot carry a payload')
    connection = http.client.HTTPConnection('127.0.0.1', profile['port'], timeout=timeout(timeout_seconds))
    response = None
    try:
        connection.request('GET', path, headers=headers(profile))
        response = connection.getresponse()
        if response.status != 200:
            raise InvalidUpdate('Remote provider metadata returned HTTP ' + str(response.status))
        raw = response.read(MAX_METADATA_BYTES + 1)
        if len(raw) > MAX_METADATA_BYTES:
            raise InvalidUpdate('Remote provider metadata exceeds its allocation')
        result = json.loads(raw)
        if not isinstance(result, (dict, list)):
            raise InvalidUpdate('Remote provider metadata must be an object or array')
        return result
    except (OSError, ValueError, http.client.HTTPException):
        raise InvalidUpdate('Remote provider metadata is unavailable through its private tunnel') from None
    finally:
        if response is not None:
            response.close()
        connection.close()


def ps_literal(value):
    if not isinstance(value, str) or any(ord(character) < 32 for character in value):
        raise InvalidUpdate('Invalid trusted PowerShell literal')
    return "'" + value.replace("'", "''") + "'"


def run_ps(profile, script, timeout_seconds=15):
    """Run a host-authored metadata script using the approved SSH alias."""
    remote_profiles.require_assignment(profile)
    if not isinstance(script, str) or len(script.encode()) > 96 * 1024:
        raise InvalidUpdate('Remote host inspection script exceeds its allocation')
    script = "$ProgressPreference='SilentlyContinue'\n$ErrorActionPreference='Stop'\n" + script
    # Windows' default SSH shell is cmd.exe, whose command line is limited to
    # 8191 characters. Keep only this fixed loader in argv; send the bounded
    # host-authored UTF-8 script through stdin instead of expanding it 8/3x.
    loader = ("[Console]::InputEncoding=[Text.Encoding]::UTF8;"
              "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
              "& ([ScriptBlock]::Create([Console]::In.ReadToEnd()))")
    encoded = base64.b64encode(loader.encode('utf-16le')).decode('ascii')
    command = 'powershell.exe -NoProfile -NonInteractive -EncodedCommand ' + encoded
    try:
        result = subprocess.run([_ssh(), *_options(), profile['ssh_alias'], command],
                                input=script.encode('utf-8'), capture_output=True,
                                timeout=timeout(timeout_seconds))
    except (OSError, subprocess.SubprocessError):
        raise InvalidUpdate('Remote host inspection timed out or its pinned SSH connection is unavailable') from None
    if result.returncode != 0:
        raise InvalidUpdate('Remote host inspection failed; no model inference was admitted')
    if len(result.stdout) > MAX_METADATA_BYTES or len(result.stderr) > MAX_METADATA_BYTES:
        raise InvalidUpdate('Remote host inspection exceeds its metadata allocation')
    try:
        value = json.loads(result.stdout.decode('utf-8-sig'))
    except (ValueError, UnicodeError):
        raise InvalidUpdate('Remote host inspection did not return one bounded JSON value') from None
    if not isinstance(value, (dict, list)):
        raise InvalidUpdate('Remote host inspection metadata has an invalid shape')
    return value


def verify_server(profile):
    health = request_json(profile, '/health')
    props = request_json(profile, '/props')
    models = request_json(profile, '/v1/models')
    rows = models.get('data') if isinstance(models, dict) else None
    settings = props.get('default_generation_settings') if isinstance(props, dict) else None
    if (not isinstance(health, dict) or health.get('status') != 'ok' or
            not isinstance(rows, list) or len(rows) != 1 or
            not isinstance(rows[0], dict) or rows[0].get('id') != profile['model'] or
            props.get('model_alias') != profile['model'] or
            props.get('model_path') != profile['model_path'] or
            not isinstance(settings, dict) or settings.get('n_ctx') != profile['context_tokens'] or
            props.get('total_slots') != 1):
        raise InvalidUpdate('Remote model, context or single-slot server changed after selection')
    return {'props': props, 'models': models, 'health': health}
