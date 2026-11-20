"""Exact physical prompt windows, separate from immutable reservation charges.

Only the verified local Strata count-only API is supported. Counting never
loads a model or performs generation. Historical budget replay is independent
of this live measurement path.
"""
import copy
import http.client
import json
import re
import time

from context_manager import ContextBudgetExceeded, InvalidUpdate, canonical, sha
from smriti_adapter import SmritiAdapter
from token_usage import count

PROFILE = 2
MAX_COUNT = 2 ** 21
MAX_METADATA_BYTES = 65536
# The Strata text engine requires prompt + max_new + 8 <= context. This is
# physical framing room, not an additional financial output reservation.
STRATA_CONTEXT_SLACK = 8
_UNSET = object()
_counts = {}


def validate_window(value):
    keys = {'version', 'provider', 'port', 'model', 'started', 'context_tokens', 'template_sha256'}
    if (not isinstance(value, dict) or set(value) != keys or type(value.get('version')) is not int or value['version'] != 1
            or value.get('provider') != 'strata' or type(value.get('port')) is not int
            or not 1 <= value['port'] <= 65535 or not isinstance(value.get('model'), str)
            or not 1 <= len(value['model']) <= 200 or not value['model'].isprintable()
            or count(value.get('started')) is None or type(value.get('context_tokens')) is not int
            or not 4096 <= value['context_tokens'] <= MAX_COUNT
            or not isinstance(value.get('template_sha256'), str)
            or re.fullmatch(r'[0-9a-f]{64}', value['template_sha256']) is None):
        raise InvalidUpdate('Invalid trusted physical context window')
    return value


def _rpc(window, method, path, body=None):
    from lifecycle import timeout
    from strata_model import _headers
    connection = http.client.HTTPConnection('127.0.0.1', window['port'], timeout=timeout(5))
    try:
        raw = None if body is None else canonical(body)
        if raw is not None and len(raw.encode()) > 16 * 1024 ** 2:
            raise ValueError('Count request exceeds its transport allocation')
        connection.request(method, path, raw, _headers())
        response = connection.getresponse()
        data = response.read(MAX_METADATA_BYTES + 1)
        if response.status != 200 or len(data) > MAX_METADATA_BYTES:
            raise ValueError('Count metadata is unavailable')
        return json.loads(data)
    finally:
        connection.close()


def _identity(window):
    from strata_model import _host_json_capability
    state = _rpc(window, 'GET', '/v1/status')
    props = _rpc(window, 'GET', '/props')
    template = props.get('chat_template') if isinstance(props, dict) else None
    context = state.get('context') if isinstance(state, dict) else None
    settings = props.get('default_generation_settings') if isinstance(props, dict) else None
    if (not isinstance(state, dict) or state.get('service') != 'strata' or state.get('loaded') is not True
            or state.get('model') != window['model'] or state.get('started') != window['started']
            or not isinstance(context, dict) or context.get('max_positions') != window['context_tokens']
            or not _host_json_capability(state) or not isinstance(template, str)
            or not 1 <= len(template.encode()) <= 60000 or sha(template) != window['template_sha256']
            or props.get('model_alias') != window['model']
            or not isinstance(settings, dict) or settings.get('n_ctx') != window['context_tokens']):
        raise ValueError('Count identity changed')


def _unavailable():
    from monitoring import ResourceWait
    raise ResourceWait({'profile': 'physical-context-v1', 'provider': 'strata',
                        'available': False, 'admitted': False, 'decision': None,
                        'blocker': 'measurement_unavailable', 'observed_at': time.time(),
                        'reason': 'The exact local token counter or its model identity is unavailable. '
                                  'Resume after the selected Strata server is ready; no inference was started.'}) from None


def discover(port, state):
    """Opt in only after observing the capability and a successful count probe."""
    from strata_model import _host_json_capability
    if not isinstance(state, dict) or not _host_json_capability(state):
        return None
    try:
        partial = {'port': port}
        props = _rpc(partial, 'GET', '/props')
        if not isinstance(props, dict):
            return None
        template = props.get('chat_template')
        window = {'version': 1, 'provider': 'strata', 'port': port, 'model': state['model'],
                  'started': state['started'], 'context_tokens': state['context']['max_positions'],
                  'template_sha256': sha(template) if isinstance(template, str) else ''}
        validate_window(window)
        _identity(window)
        probe = _rpc(window, 'POST', '/v1/messages/count_tokens', {
            'model': window['model'], 'system': 'Return one JSON object.',
            'messages': [{'role': 'user', 'content': 'Token measurement only.'}],
            'thinking': {'type': 'disabled'}})
        measured = count(probe.get('input_tokens')) if isinstance(probe, dict) else None
        if measured is None or not 1 <= measured <= MAX_COUNT:
            return None
        _identity(window)
        return window
    except (OSError, ValueError, TypeError, KeyError, http.client.HTTPException, InvalidUpdate):
        return None


def wire_request(system, payload, schema, model, thinking, response_reserve, *, host_json=True):
    from local_model import LocalModel
    from model_packet import build
    from strata_model import (_host_json_messages, ADAPTIVE_MAX_GENERATION, ADAPTIVE_THINKING_LIMITS)
    packet = build(system, payload, schema)
    messages = packet['messages'] if packet else LocalModel.messages(None, system, payload)
    output = packet['schema'] if packet else schema
    body = {'model': model, 'messages': messages, 'stream': True, 'max_tokens': response_reserve,
            'temperature': 0, 'seed': 42}
    if host_json:
        body['messages'] = _host_json_messages(messages, output)
    else:
        body['response_format'] = {'type': 'json_schema', 'json_schema': {'name': 'sutra_proposal', 'schema': output}}
    if thinking is not None:
        body['reasoning_effort'] = 'none' if thinking is False else thinking
    limit = ADAPTIVE_THINKING_LIMITS.get(thinking)
    if (payload.get('reasoning') or {}).get('preference') == 'adaptive' and limit is not None:
        body['max_tokens'] = min(body['max_tokens'], ADAPTIVE_MAX_GENERATION)
        body['reasoning_budget_tokens'] = min(limit, body['max_tokens'] // 3)
    return body, packet, output


def _count(window, wire, thinking):
    messages = wire['messages']
    if (not messages or messages[0].get('role') != 'system' or
            any(message.get('role') == 'system' for message in messages[1:])):
        raise ValueError('Unsupported count-only message projection')
    body = {'model': window['model'], 'system': messages[0]['content'], 'messages': messages[1:]}
    if thinking is False:
        body['thinking'] = {'type': 'disabled'}
    elif isinstance(thinking, str) and thinking in ('low', 'medium', 'high'):
        body['output_config'] = {'effort': thinking}
    elif thinking not in (None, True):
        raise ValueError('Unsupported exact reasoning control')
    key = sha(canonical([window, wire]))
    cached = _counts.get(key)
    if thinking is not None and cached and time.monotonic() - cached[0] < 60:
        return cached[1]
    value = _rpc(window, 'POST', '/v1/messages/count_tokens', body)
    tokens = count(value.get('input_tokens')) if isinstance(value, dict) else None
    if tokens is None or not 1 <= tokens <= MAX_COUNT:
        raise ValueError('Invalid exact token count')
    if len(_counts) >= 256:
        _counts.pop(next(iter(_counts)))
    _counts[key] = time.monotonic(), tokens
    return tokens


def exact(budget):
    return getattr(budget, 'context_profile', 1) == PROFILE


def check(budget, system, payload, schema, *, thinking=_UNSET, planning=False):
    """Measure physical fit; reservation accounting remains Budget.check()."""
    if not exact(budget):
        return None
    window = validate_window(budget.context_window)
    if budget.context_limit > window['context_tokens']:
        raise InvalidUpdate('Physical context allocation exceeds the verified server window')
    if thinking is _UNSET:
        plan = payload.get('reasoning')
        controls = [plan['requested']] if isinstance(plan, dict) and 'requested' in plan else (
            [False, 'low', 'medium', 'high', None] if planning else [None])
    else:
        controls = [thinking]
    measured = []
    try:
        _identity(window)
        for control in controls:
            wire, packet, _ = wire_request(system, payload, schema, window['model'], control, budget.response_reserve)
            tokens = _count(window, wire, control)
            measured.append({'version': 1, 'basis': 'strata_count_tokens_v1', 'window': copy.deepcopy(window),
                             'wire_sha256': sha(canonical(wire)), 'packet_sha256': sha(canonical(packet)),
                             'thinking': control, 'prompt_tokens': tokens, 'output_reserve': budget.response_reserve,
                             'context_slack_tokens': STRATA_CONTEXT_SLACK, 'context_tokens': budget.context_limit})
        _identity(window)
    except (OSError, ValueError, TypeError, KeyError, http.client.HTTPException, InvalidUpdate):
        _unavailable()
    result = max(measured, key=lambda item: item['prompt_tokens'])
    physical_output = budget.response_reserve + STRATA_CONTEXT_SLACK
    capacity = SmritiAdapter().context(result['prompt_tokens'], physical_output, budget.context_limit)
    if result['prompt_tokens'] + physical_output > budget.context_limit or capacity['decision'] != 'ALLOW':
        error = ContextBudgetExceeded('Exact prompt ' + str(result['prompt_tokens']) + ' tokens + output reserve ' +
            str(budget.response_reserve) + ' tokens + Strata framing margin ' + str(STRATA_CONTEXT_SLACK) +
            ' tokens exceeds physical context ' + str(budget.context_limit) + ' tokens')
        error.physical_context = result
        raise error
    result['capacity'] = capacity
    return result


def validate_proof(proof, budget, system, payload, schema, model_identity=None):
    if not exact(budget):
        if proof is not None:
            raise InvalidUpdate('Legacy budget cannot carry an exact context proof')
        return
    window = validate_window(budget.context_window)
    if (not isinstance(proof, dict) or type(proof.get('version')) is not int or proof['version'] != 1 or proof.get('basis') != 'strata_count_tokens_v1'
            or proof.get('window') != window or proof.get('context_tokens') != budget.context_limit
            or proof.get('output_reserve') != budget.response_reserve
            or type(proof.get('context_slack_tokens')) is not int or proof['context_slack_tokens'] != STRATA_CONTEXT_SLACK
            or count(proof.get('prompt_tokens')) is None or not 1 <= proof['prompt_tokens'] <= MAX_COUNT):
        raise InvalidUpdate('Missing or invalid exact physical context proof')
    wire, packet, _ = wire_request(system, payload, schema, window['model'], proof.get('thinking'), budget.response_reserve)
    if (proof.get('wire_sha256') != sha(canonical(wire)) or proof.get('packet_sha256') != sha(canonical(packet))
            or ('reasoning' in payload and proof.get('thinking') != payload['reasoning']['requested'])
            or (isinstance(model_identity, dict) and (
                model_identity.get('server_model') != window['model']
                or model_identity.get('server_started') not in (None, window['started'])
                or model_identity.get('server_context_tokens') not in (None, window['context_tokens'])))):
        raise InvalidUpdate('Exact physical context proof differs from its invocation')
    frame = SmritiAdapter().context(proof['prompt_tokens'], budget.response_reserve + STRATA_CONTEXT_SLACK, budget.context_limit)
    if proof.get('capacity') != frame or frame['decision'] != 'ALLOW':
        raise InvalidUpdate('Exact physical context proof exceeds its allocation')


def runtime_context(budget, proof):
    if not exact(budget):
        return None
    required = proof['prompt_tokens'] + budget.response_reserve + STRATA_CONTEXT_SLACK
    return min(budget.context_limit, ((required + 256 + 1023) // 1024) * 1024)
