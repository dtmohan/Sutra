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
PARENT_REASONING_BUDGET_TOKENS = 2048
SECTION_REASONING_BUDGETS = {'section_writer': 768, 'section_reviewer': 384}
CLAIM_REASONING_BUDGET_TOKENS = 192

# llama.cpp b12600's OpenAI adapter enables the sampling budget only when
# its parsed chat template supplies a reasoning end tag. Scope recovery to
# the measured b12600 executable and Qwen3.8 GGUF/template combination.
PARENT_REASONING_IDENTITY = {
    'executable_sha256': '0f8ec28998ac8f3eb2648d6432ef176817c44e6e81aa5d54ec95d331e76ef781',
    'model_sha256': 'd15d4b6de55f662ac40e867702d31afe738d032fa06b829b2b1a21a49d4a48ee',
    'template_sha256': 'af16f654968d47d09a6c604e00370b12dbc21f76456f59c9e98b82e84d9f6375',
}


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


def _parent_reasoning_window(window):
    return isinstance(window, dict) and all(
        window.get(field) == value for field, value in PARENT_REASONING_IDENTITY.items())


def supports_parent_output_recovery(budget):
    """Only a proven physical-window profile can request the native budget."""
    window = budget.context_window
    if not _parent_reasoning_window(window):
        return False
    validate_window(window)
    return budget.context_profile == 2 and budget.response_reserve >= 2 * PARENT_REASONING_BUDGET_TOKENS


def supports_native_section_reasoning(budget, model):
    window = budget.context_window
    return (budget.context_profile == 2 and _parent_reasoning_window(window)
            and validate_window(window)['reference'] == model)


def _section_reasoning_tokens(role, payload):
    if role == 'section_reviewer' and payload is not None and 'claim_index' in payload:
        return CLAIM_REASONING_BUDGET_TOKENS
    if role == 'section_writer' and payload is not None and payload.get('section_version') == 3:
        return 1024
    return SECTION_REASONING_BUDGETS.get(role)


def require_section_reasoning(budget, role, model, payload=None):
    """Wait before dispatch if this section cannot use native bounded reasoning."""
    tokens = _section_reasoning_tokens(role, payload)
    if tokens is None:
        return
    window = budget.context_window
    if (_parent_reasoning_window(window) and validate_window(window)['reference'] == model
            and budget.context_profile == 2 and budget.response_reserve >= 2 * tokens):
        return
    from monitoring import ResourceWait
    raise ResourceWait({'profile': 'physical-context-v1', 'provider': PROVIDER,
        'model': model, 'available': False, 'admitted': False, 'decision': None,
        'blocker': 'measurement_unavailable', 'observed_at': time.time(),
        'runtime': {'model': model, 'kind': 'remote_observation',
                    'stage': 'remote_section_reasoning', 'inference_started': False},
        'reason': 'This report section requires the verified RTX native reasoning control and its reserved output. '
                  'Resume with the supported model available.'})


def _reasoning_budget(window, system, payload, schema, response_reserve):
    from model_packet import role_for
    from review_stage import applies, native_tokens
    role = role_for(system)
    if applies(role, payload):
        stage = payload.get('review_stage') or {}
        tokens = stage.get('native_reasoning_tokens')
        if tokens is not None:
            if (not _parent_reasoning_window(window) or type(tokens) is not int
                    or tokens != native_tokens(role, payload['section_mode'], response_reserve)):
                raise InvalidUpdate('Shared review native reasoning differs from the verified transport')
            validate_window(window)
        return tokens
    section_tokens = _section_reasoning_tokens(role, payload)
    if section_tokens is not None:
        if (payload.get('section_role') != role or 'parent_draft_recovery' in payload
                or not _parent_reasoning_window(window) or response_reserve < 2 * section_tokens):
            raise InvalidUpdate('Bounded section reasoning requires the verified RTX context and output reserve')
        validate_window(window)
        return section_tokens
    if 'parent_draft_recovery' not in payload:
        return None
    recovery = payload['parent_draft_recovery']
    if (not _parent_reasoning_window(window)
            or role != 'chat_reviewer' or 'report' not in schema.get('properties', {})
            or not isinstance(payload.get('parent_repair'), dict)
            or type(payload.get('report_schema_version')) is not int or payload['report_schema_version'] != 2
            or not isinstance(recovery, dict)
            or set(recovery) != {'version', 'failed_invocation', 'failed_receipt', 'reasoning_budget_tokens'}
            or type(recovery['version']) is not int or recovery['version'] != 1
            or not isinstance(recovery['failed_invocation'], str)
            or re.fullmatch(r'[0-9a-f]{32}', recovery['failed_invocation']) is None
            or not isinstance(recovery['failed_receipt'], str)
            or re.fullmatch(r'[0-9a-f]{64}', recovery['failed_receipt']) is None
            or type(recovery['reasoning_budget_tokens']) is not int
            or recovery['reasoning_budget_tokens'] != PARENT_REASONING_BUDGET_TOKENS
            or response_reserve < 2 * PARENT_REASONING_BUDGET_TOKENS):
        raise InvalidUpdate('Bounded remote reasoning requires the verified recovered parent draft')
    validate_window(window)
    return recovery['reasoning_budget_tokens']


def wire_request(profile, system, payload, schema, response_reserve, *, prefix=None):
    """One transport projection shared by counting and actual generation."""
    from decoder_schema import for_decoder
    from local_model import LocalModel
    from model_packet import build
    reasoning_budget = _reasoning_budget(profile, system, payload, schema, response_reserve)
    if prefix is None:
        packet = build(system, payload, schema)
    else:
        from model_packet import ARCHITECT_STABLE_FIRST
        from remote_prefix import validate
        validate(prefix, profile, system)
        packet = build(system, payload, schema, packing=ARCHITECT_STABLE_FIRST)
        first = packet['messages'][0]
        first['content'] = '[' + prefix['scope_sha256'] + ']\n' + first['content']
    messages = packet['messages'] if packet else LocalModel.messages(None, system, payload)
    output = for_decoder(packet['schema'] if packet else schema, None)
    body = {'model': profile['model'], 'messages': messages,
            'stream': True, 'stream_options': {'include_usage': True},
            'max_tokens': response_reserve, 'temperature': 0.6,
            'top_p': 0.95, 'top_k': 20, 'min_p': 0, 'seed': 42,
            'cache_prompt': False,
            'response_format': {'type': 'json_schema', 'json_schema': {
                'name': 'sutra_response', 'strict': True, 'schema': output}}}
    if reasoning_budget is not None:
        body['reasoning_budget_tokens'] = reasoning_budget
    if prefix is not None:
        body.update(cache_prompt=prefix['cache_prompt'], cache_reuse=0)
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


def verify_parent_reasoning(window, profile, props):
    """Bind generation to the same live reasoning template used for counting."""
    if (not _parent_reasoning_window(window) or _descriptor(profile, props) != window
            or '<think>' not in props['chat_template'] or '</think>' not in props['chat_template']):
        raise InvalidUpdate('Remote bounded reasoning template changed or is unavailable')


def verify_prefix_window(window, profile, props):
    if _descriptor(profile, props) != window:
        raise InvalidUpdate('Remote review prefix template or model changed after exact counting')


def _identity(window, *, parent_reasoning=False):
    import remote_profiles
    from remote_control import ensure_tunnel, verify_server
    profile = remote_profiles.load(window['reference'])
    remote_profiles.require_assignment(profile)
    ensure_tunnel(profile)
    props = verify_server(profile)['props']
    if parent_reasoning:
        verify_parent_reasoning(window, profile, props)
    elif _descriptor(profile, props) != window:
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


def check(budget, system, payload, schema, *, planning=False, prefix=None):
    window = validate_window(budget.context_window)
    if budget.context_limit > window['context_tokens']:
        raise InvalidUpdate('Physical context allocation exceeds the verified remote window')
    reasoning_budget = _reasoning_budget(window, system, payload, schema, budget.response_reserve)
    try:
        identity_kwargs = {'parent_reasoning': True} if reasoning_budget is not None else {}
        profile = _identity(window, **identity_kwargs)
        wire, packet, _ = wire_request(window, system, payload, schema, budget.response_reserve, prefix=prefix)
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
        _identity(window, **identity_kwargs)
    except (OSError, ValueError, TypeError, KeyError, http.client.HTTPException, InvalidUpdate):
        _unavailable(window)
    result = {'version': 1, 'basis': BASIS, 'window': copy.deepcopy(window),
              'wire_sha256': sha(canonical(wire)), 'packet_sha256': sha(canonical(packet)),
              'thinking': None, 'prompt_tokens': tokens, 'output_reserve': budget.response_reserve,
              'context_slack_tokens': CONTEXT_SLACK, 'context_tokens': budget.context_limit}
    if reasoning_budget is not None:
        result['reasoning_budget_tokens'] = reasoning_budget
    if prefix is not None:
        result['prefix_reuse'] = copy.deepcopy(prefix)
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
    prefix = proof.get('prefix_reuse')
    if 'prefix_reuse' in proof and prefix is None:
        raise InvalidUpdate('Missing remote review prefix scope')
    wire, packet, _ = wire_request(window, system, payload, schema, budget.response_reserve, prefix=prefix)
    reasoning_budget = wire.get('reasoning_budget_tokens')
    if (reasoning_budget is None and 'reasoning_budget_tokens' in proof
            or reasoning_budget is not None and (type(proof.get('reasoning_budget_tokens')) is not int
                                                  or proof['reasoning_budget_tokens'] != reasoning_budget)):
        raise InvalidUpdate('Exact remote physical context proof differs from its reasoning budget')
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
