"""Bounded review generation controls. No new permissions, budget or kernel policy.

Adaptive reviews can use one higher control for multi-file report synthesis.
Output-limit repair and positive Strata Adaptive synthesis timeouts use an
already allocated attempt and keep the same input pins and output reserve.
Partial generations remain quarantined and charged.
"""
import os
from context_manager import InvalidUpdate
from reasoning_policy import capabilities, ladder, make, validate
from review_context import MAX_REVIEW_OUTPUT_RESERVE
from review_report import requested


def _synthesis_headroom(payload):
    return (payload.get('memory_budget', {}).get('response_reserve', 0) >= MAX_REVIEW_OUTPUT_RESERVE
            and len(payload.get('excerpts', {})) > 1 and requested(payload))


def _claim_headroom(payload):
    return (type(payload.get('claim_evaluation_version')) is int
            and payload['claim_evaluation_version'] == 1
            and type(payload.get('claim_response_reserve')) is int
            and payload['claim_response_reserve'] >= 4096
            and payload.get('review_protocol') == 2
            and bool(payload.get('claims')) and bool(payload.get('source_evidence')))


def _discovery_profile(payload):
    if 'discovery_reasoning_version' not in payload:
        return False
    if (type(payload['discovery_reasoning_version']) is not int or payload['discovery_reasoning_version'] != 1
            or type(payload.get('evidence_discovery_version')) is not int or payload['evidence_discovery_version'] != 4
            or type(payload.get('discovery_selection_version')) is not int or payload['discovery_selection_version'] != 3
            or payload.get('phase') != 'explore'):
        raise InvalidUpdate('Unsupported discovery reasoning profile')
    return True


def _discovery_source(payload):
    initial = payload.get('initial_excerpts') or {}
    if isinstance(initial, dict) and any(isinstance(text, str) and text.strip() for text in initial.values()):
        return True
    return any(isinstance(entry, dict) and isinstance(entry.get('text'), str) and entry['text'].strip()
               for entry in payload.get('evidence', []))


def bind(provider, payload, *, bounded_selection=False, preference=None, synthesis_attempts=0,
         evaluating_claims=False):
    preference = preference or os.environ.get('SUTRA_THINKING', 'adaptive')
    discovery = _discovery_profile(payload)
    if bounded_selection and not discovery and preference != 'off':
        preference = 'adaptive'
    cap = capabilities(getattr(provider, 'context_metadata', {}))
    if evaluating_claims:
        payload['claim_evaluation_version'] = 1
        payload['claim_response_reserve'] = provider.budget.response_reserve
    step = int(preference == 'adaptive' and len(ladder(cap)) > 1
               and ((synthesis_attempts > 1 and _synthesis_headroom(payload))
                    or (evaluating_claims and _claim_headroom(payload))
                    or (discovery and _discovery_source(payload))))
    plan = make(cap, preference, step)
    provider.thinking = plan['requested']
    payload['reasoning'] = plan


def check(payload, *, synthesis=False):
    # Historical packets remain verifiable; binding is mandatory for new calls
    # produced by the host review entry points.
    discovery = _discovery_profile(payload)
    plan = payload.get('reasoning')
    if discovery and plan is None:
        raise InvalidUpdate('Discovery reasoning profile requires its selected control')
    if plan is not None:
        validate(plan)
        if discovery:
            step = int(plan['preference'] == 'adaptive' and len(ladder(plan['capabilities'])) > 1
                       and _discovery_source(payload))
            if plan['step'] != step:
                raise InvalidUpdate('Discovery reasoning differs from its source context and supported controls')
            return
        if plan['step'] != 0 and not (plan['step'] == 1 and (
                (synthesis and _synthesis_headroom(payload)) or _claim_headroom(payload))):
            raise InvalidUpdate('Review reasoning exceeds the control allowed for this packet')


def repair(result, payload, used, attempts_left, *, synthesis=False):
    plan = payload.get('reasoning') or {}
    if used or not attempts_left or plan.get('preference') != 'adaptive':
        return None
    positive_synthesis = synthesis and plan.get('step') == 1
    positive_claim = plan.get('step') == 1 and _claim_headroom(payload)
    failure = result.get('failure_kind')
    if failure == 'timeout' and positive_synthesis and (result.get('record') or {}).get('provider') == 'strata':
        reason = 'The previous generation stopped before delivering a complete answer.'
    elif failure == 'output_limit' and (plan.get('requested') is False or positive_synthesis or positive_claim):
        reason = 'The previous output exhausted its allowance.'
    else:
        return None
    check(payload, synthesis=synthesis)
    return {'errors': [
        reason + ' Return compact JSON with every required field and exact constraint.',
        'Use fewer supported findings and shorter prose. Do not repeat the analysis. Partial output is not available or accepted.'
    ]}


def claim_timeout_mode(payload):
    """New host policy may spend an existing page retry in a fresh bridge unit."""
    if (type(payload.get('claim_timeout_recovery_version')) is not int
            or payload['claim_timeout_recovery_version'] != 1):
        return None
    check(payload)
    plan = payload.get('reasoning') or {}
    if plan.get('preference') == 'adaptive' and plan.get('step') == 1 and _claim_headroom(payload):
        return 'automatic_off'
    if plan.get('preference') == 'on' and plan.get('requested') is not False:
        return 'explicit_off'
    return 'manual'
