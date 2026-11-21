"""Select the measured inference host without changing task authority or spending."""
import copy

from context_manager import InvalidUpdate


def remote_model(provider=None, model=None):
    from remote_profiles import is_remote
    name = model if model is not None else getattr(provider, 'model', None)
    return name if is_remote(name) else None


def _remote_hardware(plan):
    """Normalize the adapter's explicit negative frame, never a bare ALLOW."""
    from remote_memory import PROFILE
    if not isinstance(plan, dict):
        raise InvalidUpdate('Remote resource adapter returned an invalid observation')
    hardware = plan.get('hardware')
    if hardware is None and plan.get('admitted') is False:
        hardware = plan
    if (not isinstance(hardware, dict) or hardware.get('profile') != PROFILE or
            hardware.get('provider') != 'remote_llama' or hardware.get('adapter') != 'karma' or
            type(hardware.get('profile_version')) is not int or hardware['profile_version'] != 1 or
            type(hardware.get('available')) is not bool or type(hardware.get('admitted')) is not bool or
            hardware.get('decision') not in ('ALLOW', 'BRIDGE', 'FIN', 'COMPOUND_FIN') or
            hardware['admitted'] != (hardware['decision'] == 'ALLOW') or
            hardware['admitted'] and (not hardware['available'] or not isinstance(hardware.get('sample'), dict))):
        raise InvalidUpdate('Remote resource adapter returned no valid hardware observation')
    return copy.deepcopy(hardware)


def _remote_wait(hardware, plan, name, stage):
    from monitoring import ResourceWait
    state = copy.deepcopy(hardware)
    runtime = dict(state.get('runtime') or {})
    runtime.update(stage=stage, model=name, inference_started=False, plan=copy.deepcopy(plan))
    runtime.setdefault('kind', 'model_capacity' if state.get('blocker') == 'model_admission'
                       else 'remote_observation')
    state['runtime'] = runtime
    raise ResourceWait(state)


def require_resources(root, *, provider=None, model=None, committed=False):
    """Remote dispatch uses its leased observation, never local inference RAM."""
    name = remote_model(provider, model)
    if name is None:
        from monitoring import require_resources as local_resources
        return local_resources(root)
    if committed:
        plan = getattr(provider, 'memory_plan', None)
        hardware = plan.get('hardware') if isinstance(plan, dict) else None
        if not isinstance(hardware, dict) or not hardware.get('admitted'):
            raise InvalidUpdate('Remote dispatch has no admitted GPU observation')
        from remote_memory import evaluate
        current = evaluate(provider.profile,hardware.get('sample'),provider.runtime_context_tokens)
        if not current.get('admitted'):
            _remote_wait(_remote_hardware(current), current, name, 'remote_dispatch')
        return copy.deepcopy(current['hardware'])
    if provider is None:
        from remote_model import RemoteModel
        provider = RemoteModel(root / 'calls', model=name, review=True)
    from remote_memory import admission
    plan = admission(provider, root)
    hardware = _remote_hardware(plan)
    if not hardware['admitted']:
        _remote_wait(hardware, plan, name, 'remote_admission')
    return hardware


def recover(root, audit=None, *, model=None):
    if remote_model(model=model) is None:
        from runtime_memory import recover as local_recovery
        return local_recovery(root, audit)
    from monitoring import ResourceWait
    try:
        hardware = require_resources(root, model=model)
    except ResourceWait as error:
        hardware = copy.deepcopy(error.state)
    result = {'profile': 'remote-nvidia-v1', 'action': 'observe_remote_gpu',
              'before': None, 'after': hardware, 'admitted': bool(hardware.get('admitted')),
              'max_release_actions': 0, 'automatic_inference': False,
              'release_kernel': None, 'model': model}
    if audit:
        audit('REMOTE_MEMORY_RECOVERY_OBSERVED', result)
    return result


def stopped_model(stop, request=None):
    """An explicit new tier takes precedence over a prior provider's blocker."""
    request = request or {}
    models, tier = request.get('models') or {}, request.get('tier', 'auto')
    if tier in ('low', 'mid', 'high') and tier in models:
        return models[tier]
    measurement = stop.get('measurement') or {}
    runtime = measurement.get('runtime') or {}
    return runtime.get('model') or (runtime.get('plan') or {}).get('model') or stop.get('model')
