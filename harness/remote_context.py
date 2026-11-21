"""Exact native CUDA prompt counts, independent of financial reservations.

llama.cpp's count-only OpenAI route runs the same chat/schema conversion and
tokenizer as generation without scheduling inference. A physical-window proof
binds that request to the approved weight/runtime profile and live template.
Unavailable discovery keeps legacy conservative accounting; after opting in,
an unavailable or changed counter pauses rather than silently estimating fit.
The API contract is pinned in llama.cpp b12600 tools/server/server-context.cpp
(`handle_count_tokens` and `post_chat_completions`). Counts never substitute
for the remote process, weight-digest and fresh GPU/RAM admission checks.
"""
import copy
import http.client
import json
import re
import time

from context_manager import ContextBudgetExceeded, InvalidUpdate, canonical, sha
from smriti_adapter import SmritiAdapter
from token_usage import count

PROVIDER = 'sutra_remote'
BASIS = 'remote_llama_count_tokens_v1'
COUNT_PATH = '/v1/chat/completions/input_tokens'
MAX_COUNT = 2 ** 21
MAX_METADATA_BYTES = 65536
CONTEXT_SLACK = 8


def validate_window(value):
    keys = {'version', 'provider', 'reference', 'port', 'model', 'context_tokens',
            'profile_hash', 'model_sha256', 'executable_sha256', 'template_sha256', 'defaults_sha256'}
    from remote_profiles import is_remote
    if (not isinstance(value, dict) or set(value) != keys or value.get('provider') != PROVIDER
            or type(value.get('version')) is not int or value['version'] != 1
            or not is_remote(value.get('reference')) or type(value.get('port')) is not int
            or not 1 <= value['port'] <= 65535 or not isinstance(value.get('model'), str)
            or not 1 <= len(value['model']) <= 200 or not value['model'].isprintable()
            or type(value.get('context_tokens')) is not int
            or not 4096 <= value['context_tokens'] <= MAX_COUNT
            or any(not isinstance(value.get(field), str)
                   or re.fullmatch(r'[0-9a-f]{64}', value[field]) is None
                   for field in ('profile_hash', 'model_sha256', 'executable_sha256',
                                 'template_sha256', 'defaults_sha256'))):
        raise InvalidUpdate('Invalid trusted remote physical context window')
    return value


def wire_request(profile, system, payload, schema, response_reserve):
    """One transport projection shared by counting and actual generation."""
    from decoder_schema import for_decoder
    from local_model import LocalModel
    from model_packet import build
    packet = build(system, payload, schema)
    messages = packet['messages'] if packet else LocalModel.messages(None, system, payload)
    output = for_decoder(packet['schema'] if packet else schema, None)
    body = {'model': profile['model'], 'messages': messages,
            'stream': True, 'stream_options': {'include_usage': True},
            'max_tokens': response_reserve, 'temperature': 0.6,
            'top_p': 0.95, 'top_k': 20, 'min_p': 0, 'seed': 42,
            'cache_prompt': False,
            'response_format': {'type': 'json_schema', 'json_schema': {
                'name': 'sutra_response', 'strict': True, 'schema': output}}}
    return body, packet, output


def _rpc(profile, body):
    from lifecycle import timeout
    from remote_control import headers
    raw = canonical(body).encode()
    if len(raw) > 16 * 1024 ** 2:
        raise ValueError('Remote count request exceeds its transport allocation')
    connection = http.client.HTTPConnection('127.0.0.1', profile['port'], timeout=timeout(5))
    response = None
    try:
        connection.request('POST', COUNT_PATH, raw, headers(profile))
        response = connection.getresponse()
        data = response.read(MAX_METADATA_BYTES + 1)
        if response.status != 200 or len(data) > MAX_METADATA_BYTES:
            raise ValueError('Remote count-only service is unavailable')
        value = json.loads(data)
        tokens = count(value.get('input_tokens')) if isinstance(value, dict) else None
        if (not isinstance(value, dict) or value.get('object') != 'response.input_tokens'
                or tokens is None or not 1 <= tokens <= MAX_COUNT):
            raise ValueError('Remote count-only service returned invalid tokens')
        return tokens
    finally:
        if response is not None:
            response.close()
        connection.close()


def _descriptor(profile, props):
    settings = props.get('default_generation_settings') if isinstance(props, dict) else None
    template = props.get('chat_template') if isinstance(props, dict) else None
    if (not isinstance(template, str) or not 1 <= len(template.encode()) <= 60000
            or not isinstance(settings, dict) or settings.get('n_ctx') != profile['context_tokens']):
        raise ValueError('Remote count template or generation defaults are unavailable')
    return validate_window({
        'version': 1, 'provider': PROVIDER, 'reference': profile['name'],
        'port': profile['port'], 'model': profile['model'],
        'context_tokens': profile['context_tokens'], 'profile_hash': profile['profile_hash'],
        'model_sha256': profile['model_sha256'], 'executable_sha256': profile.get('executable_sha256'),
        'template_sha256': sha(template), 'defaults_sha256': sha(canonical(settings))})


def _identity(window):
    import remote_profiles
    from remote_control import ensure_tunnel, verify_server
    profile = remote_profiles.load(window['reference'])
    remote_profiles.require_assignment(profile)
    ensure_tunnel(profile)
    observed = _descriptor(profile, verify_server(profile)['props'])
    if observed != window:
        raise ValueError('Remote physical context identity changed')
    return profile


def discover(profile):
    """Opt in to the native exact counter only after an authenticated probe."""
    import remote_profiles
    from remote_control import ensure_tunnel, verify_server
    try:
        remote_profiles.require_assignment(profile)
        # Without an expected executable digest, dispatch cannot establish that
        # the counter and generator use the reviewed native runtime binary.
        if profile.get('executable_sha256') is None:
            return None
        ensure_tunnel(profile)
        descriptor = _descriptor(profile, verify_server(profile)['props'])
        body, _, _ = wire_request(profile, 'Return one JSON object.',
                                  {'request': 'Token measurement only.'}, {'type': 'object'}, 512)
        _rpc(profile, body)
        _identity(descriptor)
        return descriptor
    except (OSError, ValueError, TypeError, KeyError, http.client.HTTPException, InvalidUpdate):
        return None


def configure(provider):
    """Refresh live metadata without dropping an already established profile."""
    prior = provider.context_metadata.get('_sutra_context_window')
    if prior is not None:
        validate_window(prior)
        return prior
    window = discover(provider.profile)
    if window is not None:
        provider.context_metadata['_sutra_context_window'] = window
    return window


def _unavailable(window):
    from monitoring import ResourceWait
    reference = window['reference']
    raise ResourceWait({'profile': 'physical-context-v1', 'provider': PROVIDER,
        'model': reference,
        'available': False, 'admitted': False, 'decision': None,
        'blocker': 'measurement_unavailable', 'observed_at': time.time(),
        'runtime': {'model': reference, 'kind': 'remote_observation',
                    'stage': 'remote_token_count', 'inference_started': False},
        'reason': 'The exact remote token counter or its bound model/template identity is unavailable. '
                  'Resume after the selected remote server is ready; no inference was started.'}) from None


def _empty_enum(value):
    if isinstance(value, dict):
        return value.get('enum') == [] or any(_empty_enum(item) for item in value.values())
    if isinstance(value, list):
        return any(_empty_enum(item) for item in value)
    return False


def check(budget, system, payload, schema, *, planning=False):
    window = validate_window(budget.context_window)
    if budget.context_limit > window['context_tokens']:
        raise InvalidUpdate('Physical context allocation exceeds the verified remote window')
    try:
        profile = _identity(window)
        wire, packet, _ = wire_request(profile, system, payload, schema, budget.response_reserve)
        # Pre-source probes can have an impossible citation enum. The native
        # counter compiles the response grammar even though it never generates.
        # Count the same messages with a legal placeholder only for planning.
        # Its wire hash cannot validate as the actual invocation's proof.
        if planning and _empty_enum(wire['response_format']['json_schema']['schema']):
            wire['response_format']['json_schema']['schema'] = {'type': 'object'}
        # Do not cache across requests. The API exposes the template and
        # defaults but no process epoch; restarting the same approved model
        # with another native reasoning default can change its prefix. The
        # dispatch gate separately revalidates the exact live process/weights.
        tokens = _rpc(profile, wire)
        _identity(window)
    except (OSError, ValueError, TypeError, KeyError, http.client.HTTPException, InvalidUpdate):
        _unavailable(window)
    result = {'version': 1, 'basis': BASIS, 'window': copy.deepcopy(window),
              'wire_sha256': sha(canonical(wire)), 'packet_sha256': sha(canonical(packet)),
              'thinking': None, 'prompt_tokens': tokens, 'output_reserve': budget.response_reserve,
              'context_slack_tokens': CONTEXT_SLACK, 'context_tokens': budget.context_limit}
    frame = SmritiAdapter().context(tokens, budget.response_reserve + CONTEXT_SLACK, budget.context_limit)
    if tokens + budget.response_reserve + CONTEXT_SLACK > budget.context_limit or frame['decision'] != 'ALLOW':
        error = ContextBudgetExceeded('Exact remote prompt ' + str(tokens) + ' tokens + output reserve ' +
            str(budget.response_reserve) + ' tokens + framing margin ' + str(CONTEXT_SLACK) +
            ' tokens exceeds physical context ' + str(budget.context_limit) + ' tokens')
        error.physical_context = result
        raise error
    result['capacity'] = frame
    return result


def validate_proof(proof, budget, system, payload, schema, model_identity=None):
    """Offline replay validates committed structure, never contacts a live host."""
    window = validate_window(budget.context_window)
    if (not isinstance(proof, dict) or type(proof.get('version')) is not int or proof['version'] != 1
            or proof.get('basis') != BASIS or proof.get('window') != window
            or proof.get('context_tokens') != budget.context_limit
            or proof.get('output_reserve') != budget.response_reserve
            or type(proof.get('context_slack_tokens')) is not int
            or proof['context_slack_tokens'] != CONTEXT_SLACK or proof.get('thinking') is not None
            or count(proof.get('prompt_tokens')) is None or not 1 <= proof['prompt_tokens'] <= MAX_COUNT):
        raise InvalidUpdate('Missing or invalid exact remote physical context proof')
    wire, packet, _ = wire_request(window, system, payload, schema, budget.response_reserve)
    if (proof.get('wire_sha256') != sha(canonical(wire))
            or proof.get('packet_sha256') != sha(canonical(packet))):
        raise InvalidUpdate('Exact remote physical context proof differs from its invocation')
    if isinstance(model_identity, dict):
        expected = {'name': window['reference'], 'remote_profile_hash': window['profile_hash'],
                    'server_model': window['model'], 'server_context_tokens': window['context_tokens'],
                    'expected_model_sha256': window['model_sha256']}
        if any(model_identity.get(field) != value for field, value in expected.items()):
            raise InvalidUpdate('Exact remote physical context proof differs from its model identity')
    frame = SmritiAdapter().context(proof['prompt_tokens'], budget.response_reserve + CONTEXT_SLACK,
                                    budget.context_limit)
    if proof.get('capacity') != frame or frame['decision'] != 'ALLOW':
        raise InvalidUpdate('Exact remote physical context proof exceeds its allocation')
