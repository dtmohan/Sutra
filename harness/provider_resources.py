"""Select the measured inference host without changing task authority or spending."""
import copy

from context_manager import InvalidUpdate


def remote_model(provider=None, model=None):
    from remote_profiles import is_remote
    name = model if model is not None else getattr(provider, 'model', None)
    return name if is_remote(name) else None


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
            from monitoring import ResourceWait
            state=copy.deepcopy(current.get('hardware',current))
            state['runtime']={'kind':'model_capacity','stage':'remote_dispatch',
                              'model':name,'inference_started':False,'plan':current}
            raise ResourceWait(state)
        return copy.deepcopy(current['hardware'])
    if provider is None:
        from remote_model import RemoteModel
        provider = RemoteModel(root / 'calls', model=name, review=True)
    from remote_memory import admission
    plan = admission(provider, root)
    hardware = plan.get('hardware')
    if not isinstance(hardware, dict):
        raise InvalidUpdate('Remote resource adapter returned no hardware observation')
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
