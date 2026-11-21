"""Host-only report transport limits; financial/token profiles stay unchanged."""
from context_manager import InvalidUpdate

DEFAULT_SECONDS = 150
LONG_SECONDS = 360
BRIDGE_HEADROOM = 30
ROLES = {'chat_reviewer', 'report_reviewer', 'subagent'}


class BridgeStepRequired(RuntimeError):
    """Preparation used the bridge window; no model invocation was committed."""
    def __init__(self, seconds, available):
        self.seconds, self.available = seconds, available
        super().__init__('The model call needs a fresh bridge step for its admitted transport limit')


def eligible(role, payload):
    if role == 'subagent':
        if 'child_transport_version' not in payload:
            return False
        if (type(payload['child_transport_version']) is not int or payload['child_transport_version'] not in (1, 2)
                or not isinstance(payload.get('child_slice'), dict) or not payload['child_slice']):
            raise InvalidUpdate('Unsupported child transport timing profile')
        return True
    if role not in ROLES or 'review_transport_version' not in payload:
        return False
    flag = payload['review_transport_version']
    if type(flag) is not int or flag != 1:
        raise InvalidUpdate('Unsupported review transport timing profile')
    if role == 'report_reviewer': return payload.get('review_protocol') == 2
    from review_report import requested
    return requested(payload)


def provider_cap(provider):
    # The owned worker has a separate 150-second generation/drain protocol.
    from engine_profiles import is_engine
    if is_engine(getattr(provider, 'model', None)): return DEFAULT_SECONDS
    from lifecycle import hard_deadline_supported
    return (LONG_SECONDS if getattr(provider, 'transport_deadline_cap', None) == LONG_SECONDS
            and hard_deadline_supported() else DEFAULT_SECONDS)


def plan(provider, role, payload, *, ledger=None, run_id=None):
    if not eligible(role, payload): return None
    cap = provider_cap(provider)
    # Unsupported providers keep the existing transport and accounting recipe.
    if cap == DEFAULT_SECONDS: return None
    reserve = provider.budget.response_reserve
    if type(reserve) is not int or reserve < 1:
        raise InvalidUpdate('Invalid timed response reserve')
    seconds = LONG_SECONDS if reserve > 2048 else DEFAULT_SECONDS
    version = payload['child_transport_version'] if role == 'subagent' else 1
    if version == 2 and seconds > DEFAULT_SECONDS and ledger is not None:
        import math
        import orchestration
        import review_budget

        session = ledger.run(run_id)['session']
        owner = orchestration.ACTIVE.get() or orchestration.task(ledger, session)['id']
        frames = orchestration.gates(ledger, owner, {})
        task = review_budget.usage(ledger, session)
        if task is not None:
            frames.append({'limits': task['limits'], 'used': task['used']})
        remaining = min(frame['limits']['seconds'] - frame['used']['seconds'] for frame in frames)
        seconds = max(DEFAULT_SECONDS, min(seconds, math.floor(remaining)))
    # The gateway rechecks this proposed duration together with every other
    # reservation immediately before dispatch.
    return {'version': version, 'seconds': seconds,
            'response_reserve': reserve, 'provider_cap': cap}


def seconds(provider, role, payload):
    return (plan(provider, role, payload) or {}).get('seconds', DEFAULT_SECONDS)


def validate(value, role, payload, response_reserve):
    if value is None: return DEFAULT_SECONDS
    if not eligible(role, payload): raise InvalidUpdate('Transport timing requires a new report packet')
    if isinstance(value, dict) and value.get('version') != (
            payload['child_transport_version'] if role == 'subagent' else 1):
        raise InvalidUpdate('Transport timing version differs from its packet')
    return _reserved(value, role, response_reserve)


def _reserved(value, role, response_reserve):
    if (not isinstance(value, dict) or set(value) != {'version', 'seconds', 'response_reserve', 'provider_cap'}
            or any(type(value.get(k)) is not int for k in value)
            or value['version'] not in (1, 2) or value['provider_cap'] != LONG_SECONDS
            or role not in ROLES or (value['version'] == 2 and role != 'subagent')
            or value['response_reserve'] != response_reserve):
        raise InvalidUpdate('Invalid committed transport timing')
    maximum = LONG_SECONDS if response_reserve > 2048 else DEFAULT_SECONDS
    minimum = maximum if value['version'] == 1 else DEFAULT_SECONDS
    if not minimum <= value['seconds'] <= maximum:
        raise InvalidUpdate('Transport timing differs from its admitted output reserve')
    return value['seconds']


def reserved_seconds(envelope):
    value = envelope.get('transport_timing')
    if value is None: return DEFAULT_SECONDS
    # Offline accounting/reconciliation must not consult a provider or clock.
    return _reserved(value, envelope.get('role'), envelope.get('budget_profile', {}).get('response_reserve'))


def require_headroom(value):
    if value is None: return
    from lifecycle import remaining
    available = remaining()
    if available is not None and available < value['seconds'] + BRIDGE_HEADROOM:
        raise BridgeStepRequired(value['seconds'], max(0, available))
