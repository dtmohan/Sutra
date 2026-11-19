"""Trusted CLI entry for Sutra engine setup and user runtime controls."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import venv

from context_manager import InvalidUpdate
import engine_client
import engine_profiles
from monitoring import ResourceWait
from runtime_memory import lease


def python_path():
    configured = os.environ.get('SUTRA_ENGINE_PYTHON')
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            raise InvalidUpdate('The Sutra engine Python path must be absolute')
        return path
    return Path.home() / 'Library/Application Support/Sutra/Engine/python/bin/python'


def setup():
    executable = python_path()
    folder = executable.parent.parent
    # An explicit host setup action provisions only the owned environment.
    if executable.name not in ('python', 'python3') or executable.parent.name != 'bin':
        raise InvalidUpdate('Choose a Sutra-owned virtual environment for automatic engine setup')
    if not executable.exists():
        folder.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        venv.EnvBuilder(with_pip=True).create(str(folder))
    requirements = Path(__file__).resolve().parent.parent / 'requirements-engine.txt'
    result = subprocess.run([str(executable), '-m', 'pip', '--isolated', 'install',
        '--disable-pip-version-check', '--index-url', 'https://pypi.org/simple', '-r', str(requirements)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=170)
    log = folder.parent / 'setup.log'
    descriptor = os.open(str(log), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(result.stdout[-65536:])
    if result.returncode:
        raise InvalidUpdate('Sutra engine dependency setup failed. Inspect the local setup log.')
    return {'ok': True, 'python': str(executable), 'message': 'Sutra engine dependencies are installed. No model files were downloaded.'}


def handle(operation, ref=None):
    if operation == 'catalog':
        result = engine_profiles.catalog()
        return dict(result, ok=True, environment_ready=python_path().is_file())
    if operation == 'status':
        return dict(engine_client.status(), environment_ready=python_path().is_file())
    if operation == 'setup':
        return setup()
    with lease():
        if operation == 'load':
            if not engine_profiles.is_engine(ref) or ref not in engine_profiles.assigned_models():
                raise InvalidUpdate('The engine profile needs a trusted user selection before loading')
            profile = engine_profiles.load(ref)
            engine_profiles.require_approval(profile)
            result = engine_client.activate(profile)
            return dict(result, message='Sutra loaded and verified its owned engine worker.')
        if operation == 'unload':
            return dict(engine_client.unload(), message='Sutra unloaded its owned engine child.')
        if operation == 'shutdown':
            return dict(engine_client.shutdown(), message='Sutra stopped its owned worker.')
    raise InvalidUpdate('Unsupported engine control operation')


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=('catalog', 'status', 'setup', 'load', 'unload', 'shutdown'))
    parser.add_argument('ref', nargs='?')
    args = parser.parse_args(argv)
    try:
        result = handle(args.operation, args.ref)
    except ResourceWait as error:
        result = {'ok': False, 'status': 'paused', 'error': str(error),
                  'decision': getattr(error, 'state', {}).get('decision')}
    except (InvalidUpdate, RuntimeError, OSError, subprocess.SubprocessError) as error:
        result = {'ok': False, 'status': 'failed', 'error': str(error)[:1000]}
    print(json.dumps(result, allow_nan=False))
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())
