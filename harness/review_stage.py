"""Bind prose review stages to the selected provider's available controls."""
from context_manager import ContextBudgetExceeded, InvalidUpdate, canonical, sha
from reasoning_policy import capabilities, ladder, make, validate as validate_reasoning


VERSION = 5
VERSIONS = (4, VERSION)
ROLES = ('section_writer', 'section_reviewer')
FULL_OUTPUT = 4096
SECTION_OUTPUT = 2048
REVIEW_OUTPUT = 512
REVIEW_OUTPUT_WITH_REASONING = 1024


def applies(role, payload):
    return role in ROLES and payload is not None and payload.get('section_version') in VERSIONS


def full_draft_supported(provider):
    from context_window import exact, validate_window
    from remote_context import supports_native_section_reasoning
    budget = provider.budget
    window = getattr(budget, 'context_window', None)
    if not exact(budget) or window is None or budget.response_reserve < FULL_OUTPUT:
        return False
    validate_window(window)
    cap = capabilities(getattr(provider, 'context_metadata', {}))
    return supports_native_section_reasoning(budget, provider.model) or False in cap['values']


def native_tokens(role, mode, output):
    maximum = (192 if role == 'section_reviewer' else 1024 if mode == 'full' else 768)
    return min(maximum, output // 2) or None


def _reviewer_limit(mode, version, capability, native):
    if version == VERSION and not native and False not in capability['values']:
        return REVIEW_OUTPUT_WITH_REASONING
    return REVIEW_OUTPUT if mode == 'full' else 256


def reviewer_output_limit(provider, *, version, mode):
    """Size the reply before constructing or admitting its stage packet."""
    from remote_context import supports_native_section_reasoning
    if type(version) is not int or version not in VERSIONS or mode not in ('full', 'sectioned'):
        raise InvalidUpdate('Invalid shared review allocation profile')
    cap = capabilities(getattr(provider, 'context_metadata', {}))
    native = supports_native_section_reasoning(provider.budget, provider.model)
    limit = _reviewer_limit(mode, version, cap, native)
    available = provider.budget.response_reserve
    if limit == REVIEW_OUTPUT_WITH_REASONING and available < limit:
        raise ContextBudgetExceeded(f'The reviewer needs {limit:,} output tokens for reasoning and its verdict. '
                                    f'The current allocation provides {available:,} output tokens.')
    return limit


def _stage(budget, role, model, payload, cap):
    from remote_context import supports_native_section_reasoning
    mode = payload.get('section_mode')
    version = payload.get('section_version')
    output = budget.response_reserve
    if (type(version) is not int or version not in VERSIONS or mode not in ('full', 'sectioned')
            or payload.get('section_role') != role or type(output) is not int):
        raise InvalidUpdate('Invalid shared review stage or output allocation')
    native = supports_native_section_reasoning(budget, model)
    if role == 'section_reviewer':
        limit = _reviewer_limit(mode, version, cap, native) if version == VERSION else REVIEW_OUTPUT
    else:
        limit = FULL_OUTPUT if mode == 'full' else SECTION_OUTPUT
    minimum = REVIEW_OUTPUT_WITH_REASONING if limit == REVIEW_OUTPUT_WITH_REASONING else 1
    if not minimum <= output <= limit:
        raise InvalidUpdate('Invalid shared review stage or output allocation')
    return {'version': 1, 'native_reasoning_tokens': native_tokens(role, mode, output) if native else None}


def _reasoning(cap, stage):
    if stage['native_reasoning_tokens'] is not None:
        return make(cap, 'model-default')
    available = ladder(cap)
    if False in cap['values'] or available == [False, True]:
        return make(cap, 'off')
    if len(available) > 1 and available[1] in ('minimal', 'low'):
        return make(cap, 'adaptive', step=1)
    return make(cap, 'model-default')


def bind(provider, role, payload):
    cap = capabilities(getattr(provider, 'context_metadata', {}))
    stage = _stage(provider.budget, role, provider.model, payload, cap)
    plan = _reasoning(cap, stage)
    provider.thinking = plan['requested']
    payload.update(review_stage=stage, reasoning=plan)


def validate(budget, role, model, payload):
    plan = payload.get('reasoning')
    validate_reasoning(plan)
    stage = _stage(budget, role, model, payload, plan['capabilities'])
    if canonical(stage) != canonical(payload.get('review_stage')):
        raise InvalidUpdate('Review stage differs from its admitted provider control')
    if plan != _reasoning(plan['capabilities'], stage):
        raise InvalidUpdate('Review stage reasoning differs from its admitted provider control')


def require(budget, role, model, payload):
    """Validate shared packets and preserve the historical RTX admission."""
    if applies(role, payload):
        validate(budget, role, model, payload)
    else:
        from remote_context import require_section_reasoning
        require_section_reasoning(budget, role, model, payload)


def verify_committed(envelope, receipt, raw_payload, payload):
    if envelope['role'] not in ROLES:
        return
    if sha(raw_payload) != envelope['payload_hash'] or receipt['payload_hash'] != envelope['payload_hash']:
        raise InvalidUpdate('Review stage evidence or provider control changed after dispatch')
    if applies(envelope['role'], payload):
        from paging import Budget
        validate(Budget(**envelope['budget_profile']), envelope['role'], envelope['model'], payload)
