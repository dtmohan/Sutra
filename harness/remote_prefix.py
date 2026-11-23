"""Host-owned prompt-prefix scopes for the verified native review transport."""
import json
import os
from pathlib import Path
import re

from context_manager import InvalidUpdate, canonical, sha

ENVIRONMENT = 'SUTRA_REMOTE_REVIEW_PREFIX_REUSE_MODELS'
# The reviewed b12600 renderer places the first system message before user
# evidence. Restrict this first profile to the executable and template checked
# on the RTX until other renderers have passed the same isolation check.
EXECUTABLE_SHA256 = '0f8ec28998ac8f3eb2648d6432ef176817c44e6e81aa5d54ec95d331e76ef781'
TEMPLATE_SHA256 = 'af16f654968d47d09a6c604e00370b12dbc21f76456f59c9e98b82e84d9f6375'


def supported(window):
    return (isinstance(window, dict) and window.get('provider') == 'sutra_remote'
            and window.get('executable_sha256') == EXECUTABLE_SHA256
            and window.get('template_sha256') == TEMPLATE_SHA256)


def validate(prefix, window, system):
    from model_packet import role_for
    if (not supported(window) or role_for(system) != 'architect'
            or not isinstance(prefix, dict) or set(prefix) != {'version', 'scope_sha256', 'cache_prompt'}
            or type(prefix['version']) is not int or prefix['version'] != 1
            or type(prefix['cache_prompt']) is not bool
            or not isinstance(prefix['scope_sha256'], str)
            or re.fullmatch('[a-f0-9]{64}', prefix['scope_sha256']) is None):
        raise InvalidUpdate('Invalid remote review prefix scope')
    return prefix


def _enabled(model):
    try:
        models = json.loads(os.environ.get(ENVIRONMENT, '[]'))
    except ValueError:
        return False
    return (isinstance(models, list) and len(models) <= 16
            and all(isinstance(value, str) for value in models) and model in models)


def binding(ledger, run_id, window, instance):
    import orchestration
    if (not isinstance(instance, dict) or type(instance.get('pid')) is not int or instance['pid'] <= 0
            or not instance.get('started_at') or not instance.get('boot_id')
            or instance.get('executable_sha256') != window['executable_sha256']
            or instance.get('model_sha256') != window['model_sha256']):
        raise InvalidUpdate('Missing verified process identity for the remote review prefix')
    run = ledger.run(run_id)
    session = run['session']
    node_id = orchestration.ACTIVE.get()
    node = orchestration.checked(ledger, node_id) if node_id else None
    if node is not None and node['session'] != session:
        raise InvalidUpdate('Remote review prefix belongs to a different task')
    return sha(canonical({
        'version': 1, 'store': str(Path(ledger.path).resolve()), 'session': session,
        'node': node_id, 'epoch': run['config']['epoch'], 'contract': run['config']['contract_hash'],
        'policy': run['config']['logic_hash'], 'permissions': orchestration.permissions(ledger, session),
        'node_permissions': node['permissions'] if node is not None else None,
        'window': window, 'instance': instance,
    }))


def plan(ledger, run_id, role, provider):
    window = provider.budget.context_window
    if role != 'architect' or not supported(window) or not _enabled(provider.model):
        return None
    memory = getattr(provider, 'memory_plan', None) or {}
    sample = memory.get('hardware', {}).get('sample') or {}
    if not isolated(sample, window):
        return None
    return {'version': 1, 'scope_sha256': binding(ledger, run_id, window, memory.get('instance')),
            'cache_prompt': True}


def isolated(sample, window):
    return (sample.get('profile_hash') == window['profile_hash']
            and (sample.get('process') or {}).get('prefix_cache_isolated') is True
            and (sample.get('server') or {}).get('total_slots') == 1)
