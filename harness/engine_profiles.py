"""Trusted local descriptors for Sutra-owned engine workers.

Descriptor and file-metadata fingerprints detect configuration changes. They do
not independently attest an engine or hash multi-gigabyte model weights.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from context_manager import InvalidUpdate, canonical


PREFIX = 'sutra:engine/'
ID = re.compile(r'[a-z][a-z0-9_-]{0,63}')
MAX_REGISTRY_BYTES = 256 * 1024
MAX_PROFILES = 64
DESCRIPTOR_KEYS = {'id', 'name', 'model', 'engine', 'tokenizer_path', 'context_tokens',
                   'cold_memory_bytes', 'source_config', 'engine_source',
                   'artifact_bindings', 'profile_hash'}


def is_engine(name):
    return isinstance(name, str) and name.startswith(PREFIX) and ID.fullmatch(name[len(PREFIX):]) is not None


def _string(value, label, maximum=4096):
    if (not isinstance(value, str) or not 1 <= len(value) <= maximum or
            any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise InvalidUpdate('Invalid engine ' + label)
    return value


def _path(value, label):
    value = _string(value, label)
    if not Path(value).is_absolute():
        raise InvalidUpdate('Engine ' + label + ' must be absolute')
    return value


def registry_path(path=None):
    chosen = path or os.environ.get('SUTRA_ENGINE_REGISTRY')
    if chosen is not None:
        return Path(_path(str(chosen), 'registry path'))
    return Path.home() / 'Library/Application Support/Sutra/Engine/models.json'


def assigned_models():
    try:
        values = json.loads(os.environ.get('SUTRA_ENGINE_MODELS', '[]'))
    except (ValueError, TypeError):
        raise InvalidUpdate('Invalid trusted engine model assignments') from None
    if not isinstance(values, list) or len(values) > 16 or any(not is_engine(value) for value in values):
        raise InvalidUpdate('Invalid trusted engine model assignments')
    return set(values)


def _file_binding(path):
    location = Path(path)
    try:
        info = location.stat()
    except OSError as error:
        raise InvalidUpdate('Engine artifact is unavailable: ' + str(location)) from error
    if not stat.S_ISREG(info.st_mode):
        raise InvalidUpdate('Engine artifact is not a regular file: ' + str(location))
    return {'path': str(location), 'size': info.st_size, 'mtime_ns': info.st_mtime_ns,
            'device': info.st_dev, 'inode': info.st_ino}


def _bindings(profile):
    paths = {profile['engine']['executable']}
    tokenizer = Path(profile['tokenizer_path'])
    if not tokenizer.is_dir():
        raise InvalidUpdate('Engine tokenizer directory is unavailable')
    # These files define the text codec. Avoid scanning or reading model weights.
    assets = sorted(path for path in tokenizer.iterdir() if path.is_file() and
                    path.suffix in ('.json', '.txt', '.jinja'))
    if not assets or len(assets) > 128:
        raise InvalidUpdate('Engine tokenizer asset list is unavailable or exceeds its bound')
    paths.update(str(path) for path in assets)
    for argument in profile['engine']['args']:
        if Path(argument).is_absolute():
            location = Path(argument)
            if location.is_file():
                paths.add(argument)
            elif location.is_dir():
                children = [path for path in location.iterdir() if path.is_file() and
                            path.name in ('dense.bin', 'index.txt', 'native_experts.txt', 'conversions.json')]
                if len(children) > 128:
                    raise InvalidUpdate('Engine data directory exceeds its artifact allocation')
                paths.update(str(path) for path in children)
    metal = Path(profile['engine']['executable']).parent / 'metal'
    if metal.is_dir():
        for asset in metal.rglob('*'):
            if asset.is_file() and asset.suffix in ('.metal', '.h', '.hpp'):
                paths.add(str(asset))
            if len(paths) > 256:
                raise InvalidUpdate('Engine artifact list exceeds its bound')
    if len(paths) > 256:
        raise InvalidUpdate('Engine artifact list exceeds its bound')
    return [_file_binding(path) for path in sorted(paths)]


def normalize(value, verify_files=True):
    if not isinstance(value, dict) or set(value) - DESCRIPTOR_KEYS:
        raise InvalidUpdate('Invalid engine profile fields')
    name = value.get('id')
    if not isinstance(name, str) or ID.fullmatch(name) is None:
        raise InvalidUpdate('Invalid engine profile identity')
    reference = PREFIX + name
    if value.get('name', reference) != reference:
        raise InvalidUpdate('Engine profile reference differs from its identity')
    engine = value.get('engine')
    if not isinstance(engine, dict) or set(engine) != {'executable', 'args', 'cwd', 'environment'}:
        raise InvalidUpdate('Invalid engine execution descriptor')
    executable = _path(engine['executable'], 'executable')
    cwd = _path(engine['cwd'], 'working directory')
    arguments = engine['args']
    if not isinstance(arguments, list) or len(arguments) > 128:
        raise InvalidUpdate('Invalid engine argument allocation')
    arguments = [_string(argument, 'argument') for argument in arguments]
    if '--serve' in arguments or '--help' in arguments or '--version' in arguments:
        raise InvalidUpdate('Engine process control arguments belong to the worker')
    environment = engine['environment']
    if (not isinstance(environment, dict) or len(environment) > 32 or any(
            not isinstance(key, str) or not re.fullmatch(r'(?:STRATA_|GGML_|MLX_|OMP_)[A-Z0-9_]+', key)
            for key in environment)):
        raise InvalidUpdate('Invalid engine environment')
    environment = {key: _string(item, 'environment value') for key, item in environment.items()}
    context = value.get('context_tokens')
    if type(context) is not int or not 4096 <= context <= 2**21:
        raise InvalidUpdate('Engine context allocation must be 4096..2097152 tokens')
    native_context = None
    if '--max-context' in arguments:
        try:
            native_context = int(arguments[arguments.index('--max-context') + 1])
        except (ValueError, IndexError):
            raise InvalidUpdate('Engine max-context argument is invalid') from None
    if native_context is not None and native_context != context:
        raise InvalidUpdate('Engine arguments differ from the declared context allocation')
    cold = value.get('cold_memory_bytes')
    if cold is not None and (type(cold) is not int or not 0 < cold <= 2**53 - 1):
        raise InvalidUpdate('Engine cold memory estimate must be positive bytes or unknown')
    profile = {'id': name, 'name': reference, 'model': _string(value.get('model'), 'model name', 200),
               'engine': {'executable': executable, 'args': arguments, 'cwd': cwd,
                          'environment': environment},
               'tokenizer_path': _path(value.get('tokenizer_path'), 'tokenizer directory'),
               'context_tokens': context, 'cold_memory_bytes': cold}
    for key in ('source_config', 'engine_source'):
        if key in value:
            if key == 'source_config':
                profile[key] = _path(value[key], 'source configuration')
            else:
                # Provenance is display/diagnostic data, never executable input.
                if not isinstance(value[key], (str, dict)) or len(canonical(value[key])) > 4096:
                    raise InvalidUpdate('Invalid engine source provenance')
                profile[key] = value[key]
    if verify_files:
        if not Path(cwd).is_dir():
            raise InvalidUpdate('Engine working directory is unavailable')
        if not os.access(executable, os.X_OK):
            raise InvalidUpdate('Engine executable is not accessible')
        bindings = _bindings(profile)
        if 'artifact_bindings' in value and value['artifact_bindings'] != bindings:
            raise InvalidUpdate('Engine artifacts changed; reimport the model profile')
    else:
        bindings = value.get('artifact_bindings', [])
    profile['artifact_bindings'] = bindings
    fingerprint = hashlib.sha256(canonical(profile).encode()).hexdigest()
    if value.get('profile_hash', fingerprint) != fingerprint:
        raise InvalidUpdate('Engine profile fingerprint does not match its descriptor')
    profile['profile_hash'] = fingerprint
    return profile


def read(path=None):
    path = registry_path(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_REGISTRY_BYTES:
                raise InvalidUpdate('Invalid bounded engine registry')
            raw = handle.read(MAX_REGISTRY_BYTES + 1)
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise InvalidUpdate('Engine registry is unavailable') from error
    if len(raw) > MAX_REGISTRY_BYTES:
        raise InvalidUpdate('Engine registry exceeds its allocation')
    try:
        document = json.loads(raw)
    except ValueError:
        raise InvalidUpdate('Engine registry is not valid JSON') from None
    if (not isinstance(document, dict) or document.get('version') != 1 or
            set(document) != {'version', 'profiles'} or not isinstance(document['profiles'], dict) or
            len(document['profiles']) > MAX_PROFILES):
        raise InvalidUpdate('Invalid engine registry schema')
    return document['profiles']


def load(reference, path=None):
    if not is_engine(reference):
        raise InvalidUpdate('Choose a registered Sutra engine profile')
    key = reference[len(PREFIX):]
    value = read(path).get(key)
    if value is None:
        raise InvalidUpdate('Sutra engine profile is not registered')
    if not isinstance(value, dict) or value.get('id') != key:
        raise InvalidUpdate('Engine registry key differs from its profile')
    return normalize(value)


def write(profiles, path=None):
    if not isinstance(profiles, dict) or len(profiles) > MAX_PROFILES:
        raise InvalidUpdate('Invalid engine profile allocation')
    normalized = {}
    for key, value in profiles.items():
        item = normalize(value)
        if item['id'] != key:
            raise InvalidUpdate('Engine registry key differs from its profile')
        normalized[key] = item
    data = (canonical({'version': 1, 'profiles': normalized}) + '\n').encode()
    if len(data) > MAX_REGISTRY_BYTES:
        raise InvalidUpdate('Engine registry exceeds its allocation')
    destination = registry_path(path)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.models-', dir=destination.parent)
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


def catalog(path=None):
    rows, errors = [], []
    for key, descriptor in read(path).items():
        try:
            profile = normalize(descriptor)
            if profile['id'] != key:
                raise InvalidUpdate('Engine registry key differs from its profile')
        except InvalidUpdate as error:
            errors.append({'model': PREFIX + str(key), 'provider': 'sutra_engine', 'error': str(error)})
            continue
        rows.append({'name': profile['name'], 'display_name': 'Sutra engine · ' + key + ' · ' + profile['model'],
                     'model': profile['model'], 'eligible': True, 'loadable': True,
                     'ready': False, 'provider': 'sutra_engine', 'capabilities': ['completion'],
                     'context_tokens': profile['context_tokens'],
                     'profile_hash': profile['profile_hash'],
                     'eligibility_reason': 'Registered local Sutra engine profile'})
    return {'models': rows, 'errors': errors}


def identity(reference, profile=None):
    from model_identity import unknown
    profile = profile or load(reference)
    return dict(unknown(reference), backend='sutra_engine', basis='engine_descriptor_and_file_metadata',
                engine_model=profile['model'], engine_profile_hash=profile['profile_hash'],
                server_context_tokens=profile['context_tokens'])
