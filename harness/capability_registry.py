"""Release-owned capability descriptions; models can select IDs, never commands."""
import json
import platform
from pathlib import Path
from context_manager import InvalidUpdate, canonical, sha

MANIFEST = Path(__file__).with_name('capability-packages.json')


def registry():
    return json.loads(MANIFEST.read_text())


def identity():
    return sha(canonical(registry()))


def platform_key():
    arch = {'aarch64': 'arm64', 'AMD64': 'x86_64'}.get(platform.machine(), platform.machine())
    return platform.system().lower() + '-' + arch


def specification(name):
    if name not in registry()['tools']:
        raise InvalidUpdate('Unknown capability; arbitrary packages and commands are not supported')
    return dict(registry()['tools'][name], id=name)


def artifact(name):
    spec = specification(name)
    value = spec['artifacts'].get(platform_key(), spec['artifacts'].get('any'))
    if not value:
        raise InvalidUpdate('No approved package for this platform')
    return dict(value)


def catalog():
    return {'registry_hash': identity(), 'built_in': ['scoped_read', 'scoped_search', 'source_map', 'diagram'],
            'tools': [dict(id=name, version=spec['version'], description=spec['description'],
                           operations=['install', 'check'], execution_supported=platform.system() == 'Darwin',
                           install_supported=platform_key() in spec['artifacts'] or 'any' in spec['artifacts'])
                      for name, spec in registry()['tools'].items()]}
