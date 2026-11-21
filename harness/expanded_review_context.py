"""A user-authorized larger writer window uses existing task and resource gates."""
import json
import os

from context_manager import InvalidUpdate


BASELINE = 32768
EXPANDED = 65536
ENVIRONMENT = 'SUTRA_EXPANDED_REVIEW_CONTEXT_MODELS'


def opted_in(model):
    try:
        names = json.loads(os.environ.get(ENVIRONMENT, '[]'))
    except ValueError as exc:
        raise InvalidUpdate('Invalid expanded review context settings') from exc
    if (not isinstance(names, list) or len(names) > 16
            or any(not isinstance(name, str) or not 1 <= len(name) <= 256 for name in names)):
        raise InvalidUpdate('Invalid expanded review context settings')
    return model in names


def prepare(provider):
    """Use the ordinary baseline until this writer needs its optional cap."""
    metadata = provider.context_metadata
    remote = metadata.get('_sutra_provider') == 'sutra_remote'
    mlx = getattr(provider, 'port', None) == 11434 and (metadata.get('details') or {}).get('runner') == 'mlx'
    if remote or mlx:
        provider.review_window_limit = BASELINE
        provider.expanded_review_enabled = opted_in(provider.model)


def ceiling(provider):
    if (getattr(provider, 'review_window_limit', None) != BASELINE
            or not getattr(provider, 'expanded_review_enabled', False)):
        return BASELINE
    from model_context import allocation
    metadata = provider.context_metadata
    admitted_cap = allocation(metadata)[0].context_limit
    if admitted_cap <= BASELINE:
        return BASELINE
    if metadata.get('_sutra_provider') == 'sutra_remote':
        profile = provider.profile
        window = metadata.get('_sutra_context_window') or {}
        if (profile['context_tokens'] == EXPANDED and profile.get('cold_context_tokens', 0) >= EXPANDED
                and window.get('context_tokens') == EXPANDED and window.get('profile_hash') == profile['profile_hash']):
            return min(EXPANDED, admitted_cap)
        return BASELINE
    measured = getattr(provider, 'memory_profile', None)
    return max(BASELINE, min(EXPANDED, admitted_cap, measured['points'][-1]['covered_tokens'])) if measured else BASELINE


def try_expand(s, sid, provider, allocation, required, charge, *, reason):
    """Stage a new fit check. The invocation gateway still admits every dispatch."""
    candidate = ceiling(provider)
    if not BASELINE < required <= candidate:
        return False
    budget = provider.budget
    slots = allocation['attempt_slots_reserved']
    cost = {'calls': slots, 'tokens': slots * (charge + budget.response_reserve)}
    if cost['tokens'] > allocation['available_tokens']:
        return False
    import orchestration as machine
    import review_budget
    owner = machine.ACTIVE.get() or machine.task(s, sid)['id']
    frames = machine.gates(s, owner, cost)
    task = review_budget.usage(s, sid)
    if any(frame['decision'] != 'ALLOW' for frame in frames) or (task and any(
            task['used'][name] + value > task['limits'][name] for name, value in cost.items())):
        return False
    from smriti_adapter import SmritiAdapter
    context = SmritiAdapter().context(required, 0, candidate)
    if context['decision'] != 'ALLOW':
        return False
    with s.db:
        s._audit('EXPANDED_REVIEW_CONTEXT_PLANNED', {
            'session': sid, 'node_id': owner, 'model': provider.model,
            'baseline': BASELINE, 'candidate_context': candidate, 'required_context': required,
            'basis': reason, 'context_gate': context, 'budget_gates': frames,
        })
    provider.review_window_limit = candidate
    return True


def packet_requirement(provider, charge, failure):
    """A financial refusal is separate from an exact physical fit failure."""
    budget = provider.budget
    if budget.context_profile == 1:
        return charge + budget.response_reserve
    physical = getattr(failure, 'physical_context', None)
    if not physical:
        return 0
    return physical['prompt_tokens'] + physical['output_reserve'] + physical['context_slack_tokens']


def bounded_recall_limit(budget, maximum=EXPANDED):
    """Limit deferred work without treating reservation bytes as exact tokens."""
    return min(maximum, budget.reservation_limit if budget.context_profile == 2 else maximum)
