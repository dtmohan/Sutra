"""Bounded review generation controls. No new permissions, budget or kernel policy.

Adaptive reviews can use one higher control for multi-file report synthesis.
Output-limit repair uses an already allocated attempt and keeps the same input
pins and output reserve. Partial generations remain quarantined and charged.
"""
import os
from context_manager import InvalidUpdate
from reasoning_policy import capabilities, ladder, make, validate
from review_context import MAX_REVIEW_OUTPUT_RESERVE
from review_report import requested


def _synthesis_headroom(payload):
    return (payload.get('memory_budget', {}).get('response_reserve', 0) >= MAX_REVIEW_OUTPUT_RESERVE
            and len(payload.get('excerpts', {})) > 1 and requested(payload))


def bind(provider, payload, *, bounded_selection=False, preference=None, synthesis_attempts=0):
    preference = preference or os.environ.get('SUTRA_THINKING', 'adaptive')
    if bounded_selection and preference != 'off':
        preference = 'adaptive'
    cap = capabilities(getattr(provider, 'context_metadata', {}))
    step = int(preference == 'adaptive' and synthesis_attempts > 1
               and len(ladder(cap)) > 1 and _synthesis_headroom(payload))
    plan = make(cap, preference, step)
    provider.thinking = plan['requested']
    payload['reasoning'] = plan


def check(payload, *, synthesis=False):
    # Historical packets remain verifiable; binding is mandatory for new calls
    # produced by the host review entry points.
    plan = payload.get('reasoning')
    if plan is not None:
        validate(plan)
        if plan['step'] != 0 and not (synthesis and plan['step'] == 1 and _synthesis_headroom(payload)):
            raise InvalidUpdate('Review reasoning exceeds the control allowed for this packet')


def repair(result, payload, used, attempts_left, *, synthesis=False):
    plan = payload.get('reasoning') or {}
    if (used or not attempts_left or result.get('failure_kind') != 'output_limit'
            or plan.get('preference') != 'adaptive'):
        return None
    if plan.get('requested') is not False and not (synthesis and plan.get('step') == 1):
        return None
    check(payload, synthesis=synthesis)
    return {'errors': [
        'The previous output exhausted its allowance. Return a compact complete JSON answer; keep every required field and exact constraint.',
        'Use fewer supported findings and shorter prose. Do not repeat the analysis. Partial output is not available or accepted.'
    ]}
