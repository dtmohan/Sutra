"""Trusted user-owned descriptors for SSH-tunnelled GPU inference.

Profiles describe connection authority; no model response can create or widen
one. A configured digest is checked against remote file evidence at dispatch.
It is not independent attestation of a privileged remote host.
"""
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import sys
import tempfile

from context_manager import InvalidUpdate, canonical, sha


PREFIX = 'sutra:remote/'
ID = re.compile(r'[a-z][a-z0-9_-]{0,63}')
MAX_REGISTRY_BYTES = 256 * 1024
MAX_PROFILES = 32
FIELDS = {'id', 'name', 'ssh_alias', 'port', 'remote_port', 'api_key_file',
          'model', 'model_path', 'model_sha256', 'context_tokens', 'remote_task',
          'gpu_index', 'gpu_reserve_bytes', 'host_reserve_bytes',
          'working_set_bytes', 'gpu_uuid', 'executable_sha256', 'architecture',
          'thinking_only', 'cold_gpu_bytes', 'cold_host_bytes',
          'cold_context_tokens', 'profile_hash'}


def is_remote(name):
    return isinstance(name, str) and name.startswith(PREFIX) and ID.fullmatch(name[len(PREFIX):]) is not None


def registry_path(path=None):
    chosen = path or os.environ.get('SUTRA_REMOTE_REGISTRY')
    if chosen is not None:
        if not isinstance(chosen, (str, os.PathLike)) or not Path(chosen).is_absolute():
            raise InvalidUpdate('Remote provider registry path must be absolute')
        return Path(chosen)
    if sys.platform == 'darwin':
        root = Path.home() / 'Library/Application Support/Sutra'
    elif os.name == 'nt':
        root = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData/Local') / 'Sutra'
    else:
        root = Path.home() / '.local/share/Sutra'
    return root / 'Remote/models.json'


def _text(value, label, maximum=200):
    if (not isinstance(value, str) or not 1 <= len(value) <= maximum or
            any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise InvalidUpdate('Invalid remote provider ' + label)
    return value


def _integer(value, label, low, high):
    if type(value) is not int or not low <= value <= high:
        raise InvalidUpdate('Invalid remote provider ' + label)
    return value


def normalize(value):
    if not isinstance(value, dict) or set(value) - FIELDS:
        raise InvalidUpdate('Invalid remote provider descriptor fields')
    item = dict(value)
    identifier = item.get('id')
    if not isinstance(identifier, str) or ID.fullmatch(identifier) is None:
        raise InvalidUpdate('Invalid remote provider identifier')
    name = PREFIX + identifier
    if item.get('name', name) != name:
        raise InvalidUpdate('Remote provider name differs from its identifier')
    item['name'] = name
    alias = _text(item.get('ssh_alias'), 'SSH alias', 64)
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', alias) is None:
        raise InvalidUpdate('Remote provider requires one trusted SSH configuration alias')
    for field in ('port', 'remote_port'):
        _integer(item.get(field), field, 1, 65535)
    key = _text(item.get('api_key_file'), 'API key file', 4096)
    if not Path(key).is_absolute():
        raise InvalidUpdate('Remote API key path must be absolute')
    _text(item.get('model'), 'server model')
    model_path = _text(item.get('model_path'), 'model path', 4096)
    if not PureWindowsPath(model_path).is_absolute():
        raise InvalidUpdate('Remote GPU v1 requires an absolute Windows model path')
    if (not isinstance(item.get('model_sha256'), str) or
            re.fullmatch(r'[0-9a-f]{64}', item['model_sha256']) is None):
        raise InvalidUpdate('Remote model requires an expected SHA-256 digest')
    _integer(item.get('context_tokens'), 'context size', 4096, 2**21)
    item.setdefault('remote_task', 'SutraCUDAInference')
    task = _text(item['remote_task'], 'task name', 128)
    if re.fullmatch(r'[A-Za-z][A-Za-z0-9 _.-]{0,127}', task) is None:
        raise InvalidUpdate('Invalid remote inference task name')
    for field, default, lower, upper in (
            ('gpu_index', 0, 0, 31),
            ('gpu_reserve_bytes', 1024**3, 64 * 1024**2, 2**53 - 1),
            ('host_reserve_bytes', 2 * 1024**3, 64 * 1024**2, 2**53 - 1),
            ('working_set_bytes', 128 * 1024**2, 1, 2**53 - 1)):
        item.setdefault(field, default)
        _integer(item[field], field, lower, upper)
    if 'gpu_uuid' in item:
        if (not isinstance(item['gpu_uuid'], str) or
                re.fullmatch(r'GPU-[A-Za-z0-9-]{1,100}', item['gpu_uuid']) is None):
            raise InvalidUpdate('Invalid pinned remote GPU identity')
    if 'executable_sha256' in item:
        if (not isinstance(item['executable_sha256'], str) or
                re.fullmatch(r'[a-f0-9]{64}', item['executable_sha256']) is None):
            raise InvalidUpdate('Invalid pinned remote executable digest')
    cold_fields = {'cold_gpu_bytes', 'cold_host_bytes', 'cold_context_tokens'}
    if cold_fields & item.keys():
        if not cold_fields <= item.keys():
            raise InvalidUpdate('Remote cold allocation requires GPU, host and context coverage together')
        for field in ('cold_gpu_bytes', 'cold_host_bytes'):
            _integer(item[field], field, 1, 2**53 - 1)
        _integer(item['cold_context_tokens'], 'cold context coverage', 4096, 2**21)
        if item['cold_context_tokens'] < item['context_tokens']:
            raise InvalidUpdate('Remote context exceeds its approved cold allocation coverage')
    if 'architecture' in item:
        _text(item['architecture'], 'model architecture', 64)
    item.setdefault('thinking_only', True)
    if type(item['thinking_only']) is not bool:
        raise InvalidUpdate('Invalid remote reasoning capability declaration')
    claimed = item.pop('profile_hash', None)
    digest = sha(canonical(item))
    if claimed is not None and claimed != digest:
        raise InvalidUpdate('Remote provider descriptor changed after approval')
    return dict(item, profile_hash=digest)


def _trusted_bytes(path, limit, *, secret=False):
    """Read one same-user regular file without following links or logging it."""
    path = Path(path)
    descriptor = None
    try:
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or
                hasattr(os, 'getuid') and before.st_uid != os.getuid() or
                before.st_mode & (0o077 if secret else 0o022)):
            raise InvalidUpdate('Remote credential/profile file must be an unlinked, user-owned regular file with private write permissions')
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        observed = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns):
            raise InvalidUpdate('Remote credential/profile changed during access')
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        if (observed.st_size > limit or len(raw) > limit or
                (observed.st_size, observed.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
            raise InvalidUpdate('Remote credential/profile exceeds its bound or changed during access')
        return raw
    except OSError:
        raise InvalidUpdate('Remote credential/profile file is unavailable') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def api_key(profile):
    try:
        key = _trusted_bytes(profile['api_key_file'], 4096, secret=True).decode('ascii').strip()
    except (UnicodeError, KeyError):
        raise InvalidUpdate('Remote API credential is invalid') from None
    if not key or not key.isprintable() or any(character.isspace() for character in key):
        raise InvalidUpdate('Remote API credential must be one printable ASCII token')
    return key


def read(path=None):
    location = registry_path(path)
    if not location.exists():
        return {}
    try:
        document = json.loads(_trusted_bytes(location, MAX_REGISTRY_BYTES))
    except (ValueError, UnicodeError):
        raise InvalidUpdate('Remote provider registry is invalid JSON') from None
    if (not isinstance(document, dict) or set(document) != {'version', 'profiles'} or
            type(document['version']) is not int or document['version'] != 1 or
            not isinstance(document['profiles'], dict) or len(document['profiles']) > MAX_PROFILES):
        raise InvalidUpdate('Invalid remote provider registry schema')
    return document['profiles']


def load(reference, path=None):
    if not is_remote(reference):
        raise InvalidUpdate('Choose a registered Sutra remote provider')
    identifier = reference[len(PREFIX):]
    value = read(path).get(identifier)
    if not isinstance(value, dict) or value.get('id') != identifier:
        raise InvalidUpdate('Sutra remote provider is not registered')
    return normalize(value)


def write(profiles, path=None):
    if not isinstance(profiles, dict) or len(profiles) > MAX_PROFILES:
        raise InvalidUpdate('Invalid remote provider profile allocation')
    normalized = {}
    for identifier, value in profiles.items():
        item = normalize(value)
        if identifier != item['id']:
            raise InvalidUpdate('Remote registry key differs from its profile')
        normalized[identifier] = item
    data = (canonical({'version': 1, 'profiles': normalized}) + '\n').encode()
    if len(data) > MAX_REGISTRY_BYTES:
        raise InvalidUpdate('Remote provider registry exceeds its bound')
    destination = registry_path(path)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.remote-models-', dir=destination.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return normalized


def assigned_models():
    try:
        values = json.loads(os.environ.get('SUTRA_REMOTE_MODELS', '[]'))
    except (ValueError, TypeError):
        raise InvalidUpdate('Invalid trusted remote model assignments') from None
    if not isinstance(values, list) or len(values) > 16 or any(not is_remote(value) for value in values):
        raise InvalidUpdate('Invalid trusted remote model assignments')
    return set(values)


def require_assignment(profile):
    if profile['name'] not in assigned_models():
        raise InvalidUpdate('Remote provider is not assigned in trusted model settings')


def identity(reference, profile=None):
    from model_identity import family, unknown
    profile = profile or load(reference)
    # Same GGUF bytes shared with a single-model Ollama layer must not be
    # described as independent reviewer weights merely due to another host.
    weights = sha(canonical([{'mediaType': 'application/vnd.ollama.image.model',
                              'digest': 'sha256:' + profile['model_sha256']}]))
    architecture = profile.get('architecture')
    return dict(unknown(reference), backend='remote_cuda',
                basis='configured_GGUF_descriptor_verified_at_dispatch',
                weights_sha256=weights, architecture=architecture, family=family(architecture),
                remote_profile_hash=profile['profile_hash'],
                server_model=profile['model'], server_context_tokens=profile['context_tokens'],
                expected_model_sha256=profile['model_sha256'])


def catalog(path=None):
    rows, errors = [], []
    for identifier, descriptor in read(path).items():
        try:
            item = normalize(descriptor)
            if item['id'] != identifier:
                raise InvalidUpdate('Remote registry key differs from its profile')
        except InvalidUpdate as error:
            errors.append({'model': PREFIX + str(identifier), 'provider': 'sutra_remote', 'error': str(error)})
            continue
        rows.append({'name': item['name'], 'display_name': 'Remote GPU · ' + identifier + ' · ' + item['model'],
                     'model': item['model'], 'eligible': True, 'loadable': True, 'ready': False,
                     'provider': 'sutra_remote', 'capabilities': ['completion'],
                     'context_tokens': item['context_tokens'], 'profile_hash': item['profile_hash'],
                     'eligibility_reason': 'Registered SSH-tunnelled GPU provider; fresh remote admission required'})
    return {'models': rows, 'errors': errors}


def append_catalog(value, path=None):
    value['models'] = [row for row in value['models'] if not is_remote(row.get('name'))]
    value.setdefault('errors', [])
    try:
        observed = catalog(path)
    except InvalidUpdate as error:
        value['errors'].append({'provider': 'sutra_remote', 'error': str(error)})
    else:
        value['models'].extend(observed['models'])
        value['errors'].extend(observed['errors'])
    return value
