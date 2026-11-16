"""Bounded review generation controls. No new permissions, budget or kernel policy.

Adaptive reviews start at the lowest supported control. At most one output-limit
repair can use an already allocated attempt, with the same input pins and output
reserve. Partial generations remain quarantined and fully charged.
"""
import os
from context_manager import InvalidUpdate
from reasoning_policy import capabilities, make, validate


def bind(provider, payload):
    plan = make(capabilities(getattr(provider, 'context_metadata', {})),
                os.environ.get('SUTRA_THINKING', 'adaptive'))
    provider.thinking = plan['requested']
    payload['reasoning'] = plan


def check(payload):
    # Historical packets remain verifiable; binding is mandatory for new calls
    # produced by the host review entry points.
    plan = payload.get('reasoning')
    if plan is not None:
        validate(plan)
        if plan != make(plan['capabilities'], plan['preference']):
            raise InvalidUpdate('Review reasoning differs from the initial supported control')


def repair(result, payload, used, attempts_left):
    plan = payload.get('reasoning') or {}
    if (used or not attempts_left or result.get('failure_kind') != 'output_limit'
            or plan.get('preference') != 'adaptive' or plan.get('requested') is not False):
        return None
    check(payload)
    return {'errors': [
        'The previous output exhausted its allowance. Return a compact complete JSON answer; keep every required field and exact constraint.',
        'Use fewer supported findings and shorter prose. Do not repeat the analysis. Partial output is not available or accepted.'
    ]}
