"""Managed native profiles and the legacy localhost Strata provider. The invocation gateway owns admission."""
from contextlib import contextmanager, nullcontext
import http.client
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from context_manager import InvalidUpdate, canonical, sha
from lifecycle import timeout
from local_model import LocalModel, OutputLimitExceeded, StructuredOutputRejected
from paging import Budget
from token_usage import count


MODEL = 'strata:high'
PROFILE_PREFIX = 'strata:profile/'


def is_strata(name):
    return name == MODEL or (isinstance(name, str) and
                            re.fullmatch(r'strata:profile/[a-z][a-z0-9_-]{0,63}', name) is not None)


def assigned_models():
    try:
        values = json.loads(os.environ.get('SUTRA_STRATA_MODELS', '[]'))
    except (ValueError, TypeError):
        raise InvalidUpdate('Invalid trusted Strata model assignments') from None
    if not isinstance(values, list) or len(values) > 16 or any(not is_strata(value) for value in values):
        raise InvalidUpdate('Invalid trusted Strata model assignments')
    return set(values)


def managed(name):
    return (isinstance(name, str) and name.startswith(PROFILE_PREFIX) or
            name == MODEL and (os.environ.get('SUTRA_STRATA_ONLY') == '1' or
                               any(value.startswith(PROFILE_PREFIX) for value in assigned_models())))


def native_command(action, name=None):
    if action not in ('catalog', 'inspect', 'activate'):
        raise InvalidUpdate('Unsupported native Strata operation')
    if name is not None and not is_strata(name):
        raise InvalidUpdate('Invalid native Strata profile reference')
    if action == 'activate' and name not in assigned_models():
        raise InvalidUpdate('Native Strata profile is not assigned in trusted model settings')
    node = os.environ.get('KARMA_NODE') or shutil.which('node')
    if not node or not Path(node).is_absolute():
        raise InvalidUpdate('Native Strata management requires an installed Node executable')
    command = [node, str(Path(__file__).resolve().parent.parent / 'strata_runtime.js'), action]
    if name is not None:
        command.append(name)
    env = dict(os.environ, ELECTRON_RUN_AS_NODE='1', SUTRA_PROVIDER_PYTHON=sys.executable)
    try:
        response = subprocess.run(command, capture_output=True, timeout=timeout(145 if action == 'activate' else 20), env=env)
        if len(response.stdout) > 262144:
            raise InvalidUpdate('Native Strata response exceeds the inspection limit')
        result = json.loads(response.stdout)
    except subprocess.TimeoutExpired:
        if action == 'activate':
            from monitoring import ResourceWait
            raise ResourceWait({'profile':'native-strata-v1','available':False,'admitted':False,
                                'decision':None,'blocker':'runtime_timeout','provider':'strata',
                                'observed_at':time.time(),
                                'reason':'Native activation response timed out; its owner may still be loading. Resume to inspect the saved native state; no model inference or budget reset occurred.'}) from None
        raise InvalidUpdate('Native Strata metadata inspection timed out') from None
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise InvalidUpdate('Native Strata management is unavailable: '+str(error)[:160]) from None
    if not isinstance(result, dict) or result.get('ok') is not True:
        error = (result.get('error') or 'Native Strata operation was not confirmed') if isinstance(result, dict) else 'Invalid native Strata response'
        if isinstance(result, dict) and result.get('status') in ('paused', 'busy', 'timeout'):
            from monitoring import ResourceWait
            raise ResourceWait({'profile':'native-strata-v1','available':False,'admitted':False,
                                'decision':result.get('decision'),'reason':str(error)[:600],
                                'blocker':{'busy':'runtime_busy','timeout':'runtime_timeout'}.get(result['status'],
                                    'model_admission' if result.get('decision') else 'measurement_unavailable'),
                                'provider':'strata','status':result['status'],'observed_at':time.time()})
        raise InvalidUpdate(str(error)[:600])
    if response.returncode != 0:
        raise InvalidUpdate('Native Strata command failed despite its response')
    return result


def profile_metadata(name, result=None):
    value = (native_command('inspect', name) if result is None else result).get('profile')
    if (not isinstance(value, dict) or value.get('name') != name or not is_strata(name) or
            not isinstance(value.get('model'), str) or not 1 <= len(value['model']) <= 200 or
            not value['model'].isprintable() or type(value.get('port')) is not int or not 1 <= value['port'] <= 65535 or
            type(value.get('context_tokens')) is not int or not 4096 <= value['context_tokens'] <= 2**31-1 or
            not re.fullmatch(r'[0-9a-f]{16}', str(value.get('profile_revision', ''))) or
            not re.fullmatch(r'[0-9a-f]{64}', str(value.get('source_sha256', '')))):
        raise InvalidUpdate('Native Strata profile metadata is invalid')
    return value


def profile_identity(name, profile):
    from model_identity import unknown
    return dict(unknown(name), backend='strata', basis='native_profile_metadata',
                server_model=profile['model'], server_context_tokens=profile['context_tokens'],
                native_profile_ref=name, native_profile_revision=profile['profile_revision'],
                native_source_sha256=profile['source_sha256'], native_port=profile['port'])

MAX_METADATA_BYTES = 65536
TRANSPORT_SECONDS = 150
# The installed Flash-Next model decoded at ~34 tokens/s. Keep an adaptive
# generation below that call's measured wall time with room for prefill and
# leave most of its allowance for the structured answer.
ADAPTIVE_MAX_GENERATION = 3072
ADAPTIVE_THINKING_LIMITS = {'low': 768, 'medium': 896, 'high': 1024}
HOST_JSON_DIRECTIVE = ('\n\nOUTPUT FORMAT REQUIREMENT: Return exactly one JSON object matching this JSON Schema. '
                       'No Markdown, code fences, commentary or text outside the JSON. '
                       'Use required fields, correct types and only allowed fields.\nJSON Schema:\n')
HOST_JSON_DIRECTIVE_BYTES = len(HOST_JSON_DIRECTIVE.encode())


def _host_json_capability(state):
    capability = state.get('structured_output')
    return (isinstance(capability, dict) and capability.get('method') == 'prompt_and_validate'
            and capability.get('constrained_decoding') is False
            and capability.get('stream_buffered') is True
            and isinstance(capability.get('formats'), list) and 'json_schema' in capability['formats'])


def _host_json_messages(messages, schema):
    # The schema is already reserved by model_packet.schema_allowance. Only
    # this fixed directive is additional; reserve it in template_allowance.
    messages = [dict(message) for message in messages]
    if not messages or messages[0].get('role') != 'system' or not isinstance(messages[0].get('content'), str):
        raise InvalidUpdate('Strata host JSON requires the committed system message')
    messages[0]['content'] += HOST_JSON_DIRECTIVE + canonical(schema)
    return messages


def _host_json_answer(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key (key withheld)')
            result[key] = value
        return result

    def constant(_):
        raise ValueError('non-finite JSON number')

    def number(value):
        result = float(value)
        if not math.isfinite(result):
            constant(value)
        return result

    try:
        answer = json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)
    except json.JSONDecodeError as exc:
        cause = 'invalid JSON: ' + _json_failure_cause(str(exc))
    except ValueError as exc:
        cause = str(exc) if str(exc) in ('duplicate JSON key (key withheld)', 'non-finite JSON number') else 'invalid JSON value'
    except (OverflowError, RecursionError):
        cause = 'JSON value exceeds the parser limit'
    else:
        if isinstance(answer, dict):
            return answer
        cause = 'expected one JSON object'
    raise StructuredOutputRejected('Strata rejected the structured reply. Cause: ' + cause +
        '. Return one JSON object matching the supplied schema.') from None


def _host_json_text(text):
    # A whole-response Markdown wrapper carries no JSON values. Never search
    # for objects inside prose or select among several candidate objects.
    fenced = re.fullmatch(r'\s*```(?:json)?[ \t]*\r?\n(.*)\r?\n```\s*', text, re.DOTALL)
    return (fenced[1], 'json_fence') if fenced else (text, 'none')


def _configured_int(key, lower, upper, default=None):
    value = os.environ.get(key)
    if value is None and default is not None:
        return default
    if not value or not value.isascii() or not value.isdecimal() or len(value) > 6:
        raise InvalidUpdate('Set a numeric ' + key + ' for Strata')
    value = int(value)
    if not lower <= value <= upper:
        raise InvalidUpdate('Strata setting is out of range: ' + key)
    return value


def enabled():
    return 'SUTRA_STRATA_PORT' in os.environ


def _headers():
    headers = {'Content-Type': 'application/json'}
    if key := os.environ.get('SUTRA_STRATA_API_KEY'):
        if not key.isascii() or not key.isprintable():
            raise InvalidUpdate('Strata API key must use printable ASCII characters')
        headers['Authorization'] = 'Bearer ' + key
    return headers


def status(port, expected_model=None):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout(3))
    response = None
    try:
        connection.request('GET', '/v1/status', headers=_headers())
        response = connection.getresponse()
        if response.status != 200:
            raise InvalidUpdate(f'Strata status returned HTTP {response.status}')
        body = response.read(MAX_METADATA_BYTES + 1)
        if len(body) > MAX_METADATA_BYTES:
            raise InvalidUpdate('Strata metadata exceeds the transport limit')
        value = json.loads(body)
    except (OSError, ValueError, http.client.HTTPException):
        raise InvalidUpdate('Strata status is unavailable on localhost') from None
    finally:
        if response is not None:
            response.close()
        connection.close()
    if not isinstance(value, dict) or value.get('service') != 'strata':
        raise InvalidUpdate('Configured port does not describe a Strata server')
    if value.get('loaded') is not True:
        raise InvalidUpdate('Load the local Strata model before selecting Strata')
    model, started = value.get('model'), value.get('started')
    context = value.get('context')
    limit = context.get('max_positions') if isinstance(context, dict) else None
    if (not isinstance(model, str) or not 1 <= len(model) <= 200 or not model.isprintable()
            or count(started) is None or count(limit) is None or not 4096 <= limit):
        raise InvalidUpdate('Strata requires a known model, server start and context window')
    if model != (expected_model if expected_model is not None else os.environ.get('SUTRA_STRATA_MODEL')):
        raise InvalidUpdate('Strata model does not match SUTRA_STRATA_MODEL')
    return value


def _identity(value, name=MODEL):
    from model_identity import unknown
    version = value.get('engine')
    return dict(unknown(name), backend='strata', basis='local_server_metadata',
                server_model=value['model'], server_started=value['started'],
                engine_version=version[:120] if isinstance(version, str) else None,
                server_context_tokens=value['context']['max_positions'])


def observe(name=MODEL):
    if managed(name):
        return profile_identity(name, profile_metadata(name))
    if name != MODEL:
        raise InvalidUpdate('Unknown Strata provider reference')
    return _identity(status(_configured_int('SUTRA_STRATA_PORT', 1, 65535)))


def append_catalog(catalog):
    catalog['models'] = [row for row in catalog['models'] if not is_strata(row.get('name'))]
    catalog.setdefault('errors', [])
    if 'SUTRA_STRATA_MODELS' in os.environ:
        try:
            native = native_command('catalog')
            profiles = native.get('profiles')
            if not isinstance(profiles, list) or len(profiles) > 128:
                raise InvalidUpdate('Invalid native Strata catalog')
            for profile in profiles:
                name = profile.get('name') if isinstance(profile, dict) else None
                try:
                    metadata = profile_metadata(name, {'profile':profile})
                except InvalidUpdate as exc:
                    catalog['errors'].append({'model':name,'provider':'strata','error':str(exc)})
                    continue
                catalog['models'].append({
                    'name':name, 'display_name':metadata.get('display_name', name),
                    'eligible':metadata.get('eligible', True) is True, 'capabilities':['completion'],
                    'provider':'strata', 'loadable':True, 'ready':metadata.get('ready', False) is True,
                    'active':metadata.get('active', False) is True, 'context_tokens':metadata['context_tokens'],
                    'parameter_size':metadata['model'], 'eligibility_reason':'Managed native Strata profile'})
            legacy_ref = native.get('legacy_high_ref')
            legacy = next((row for row in catalog['models'] if row['name'] == legacy_ref), None)
            if legacy is not None:
                catalog['models'].append(dict(legacy, name=MODEL, display_name='Strata · legacy selection', legacy_alias=True))
        except InvalidUpdate as exc:
            catalog['errors'].append({'provider':'strata', 'error':str(exc)})
        if any(row['name'] == MODEL for row in catalog['models']):
            return catalog
        if managed(MODEL):
            if MODEL in assigned_models():
                catalog['errors'].append({'model':MODEL,'provider':'strata',
                                          'error':'The legacy Strata selection is not registered as a native profile. Choose a named native profile.'})
            return catalog
    if not enabled():
        return catalog
    try:
        identity = observe()
    except InvalidUpdate as exc:
        catalog['errors'].append({'model': MODEL, 'error': str(exc)})
    else:
        catalog['models'].append({'name': MODEL, 'eligible': True, 'capabilities': ['completion'],
                                  'provider':'strata','parameter_size': identity['server_model'],
                                  'eligibility_reason': 'Explicit localhost Strata provider'})
    return catalog


def _json_failure_cause(message):
    # JSONDecodeError diagnostics contain a parser reason and numeric location.
    # Other ValueErrors (notably duplicate keys) can contain generated text.
    reasons = ("Expecting value", "Extra data", "Expecting property name enclosed in double quotes",
               "Invalid \\escape", "Invalid control character at", "Unterminated string starting at",
               "Expecting ',' delimiter", "Expecting ':' delimiter")
    for reason in reasons:
        if re.fullmatch(re.escape(reason) + r': line [0-9]{1,12} column [0-9]{1,12} \(char [0-9]{1,12}\)', message):
            return message
    if message.startswith('duplicate JSON key: '):
        return 'duplicate JSON key (key withheld)'
    if message in ('invalid JSON constant: NaN', 'invalid JSON constant: Infinity',
                   'invalid JSON constant: -Infinity'):
        return message
    return 'invalid JSON (provider detail withheld)'


def _structured_failure_cause(message):
    """Keep a bounded diagnostic without echoing generated keys or values."""
    if not isinstance(message, str) or len(message) > 4096:
        return 'structured output validation failed (provider detail withheld)'
    prefix = 'model did not return valid JSON: '
    if message.startswith(prefix):
        return 'invalid JSON: ' + _json_failure_cause(message[len(prefix):])
    if message.startswith('model output failed the JSON Schema at '):
        # jsonschema renders the offending value and arbitrary object keys in
        # its error text. Retain only known validator grammar, never that text.
        suffix = re.search(r" is not of type ('(?:object|array|string|number|integer|boolean|null)'(?:, '(?:object|array|string|number|integer|boolean|null)')*)$", message)
        if suffix and len(suffix[1]) <= 80:
            return 'JSON Schema: value is not of type ' + suffix[1]
        if message.endswith(' is a required property'):
            return 'JSON Schema: required property missing (property withheld)'
        if ': Additional properties are not allowed (' in message:
            return 'JSON Schema: additional properties are not allowed (properties withheld)'
        return 'JSON Schema mismatch (path and value withheld)'
    if message.startswith('structured output was incomplete (finish_reason='):
        return 'structured output was incomplete (provider finish reason withheld)'
    if message.startswith('could not validate structured output: '):
        return 'could not validate structured output (provider detail withheld)'
    return 'structured output validation failed (provider detail withheld)'


def _error_usage(chunk, error):
    """Error events may include finalized counts; never retain arbitrary metadata."""
    value = chunk.get('usage')
    if not isinstance(value, dict) and isinstance(error, dict):
        value = error.get('usage')
    if not isinstance(value, dict):
        return None
    inp, out = count(value.get('prompt_tokens')), count(value.get('completion_tokens'))
    if inp is None and out is None:
        return None
    return {'done': inp is not None and out is not None,
            'done_reason': 'structured_output_failed',
            'prompt_eval_count': inp, 'eval_count': out}


def _stream(response, transport_socket, started, byte_limit, on_thinking):
    """Read the Strata SSE dialect with an absolute deadline and bounded frames."""
    answer, reasoning = [], []
    total, final, ended, first_answer_s = 0, None, False, None
    frame_limit = 6 * byte_limit + MAX_METADATA_BYTES  # One buffered JSON value plus escaping.
    while True:
        from invocation_gateway import transport_timeout
        transport_socket.settimeout(transport_timeout(started, TRANSPORT_SECONDS))
        line = response.readline(frame_limit + 1)
        if not line:
            break
        if len(line) > frame_limit:
            raise RuntimeError('Strata stream frame exceeds the transport limit')
        if line.startswith(b':') or not line.strip():
            continue
        if not line.startswith(b'data:'):
            raise RuntimeError('Unexpected Strata stream frame')
        raw = line[5:].strip()
        if raw == b'[DONE]':
            ended = True
            break
        try:
            chunk = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError('Invalid Strata stream JSON: ' + _json_failure_cause(str(exc))) from None
        if not isinstance(chunk, dict):
            raise RuntimeError('Invalid Strata stream event')
        if 'error' in chunk:
            error = chunk['error']
            if isinstance(error, dict) and error.get('code') == 'structured_output_failed':
                if str(error.get('message')).startswith('structured output was incomplete (finish_reason=length)'):
                    failure = OutputLimitExceeded('Strata reached the allocated output limit')
                else:
                    cause = _structured_failure_cause(error.get('message'))
                    failure = StructuredOutputRejected('Strata rejected the structured reply. Cause: ' + cause +
                        '. Return one JSON object matching the supplied schema.')
                failure.strata_response = _error_usage(chunk, error)
                raise failure
            raise RuntimeError('Strata reported a generation error')
        if 'prompt_progress' in chunk and not chunk.get('choices'):
            continue
        choices = chunk.get('choices')
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict) or final:
            raise RuntimeError('Invalid Strata stream choice')
        choice = choices[0]
        delta = choice.get('delta')
        if not isinstance(delta, dict) or delta.get('tool_calls') or chunk.get('strata_rejected_tool_calls'):
            raise RuntimeError('Unexpected tool call or invalid Strata text event')
        text, thought = delta.get('content', ''), delta.get('reasoning_content', '')
        if not isinstance(text, str) or not isinstance(thought, str):
            raise RuntimeError('Invalid Strata stream text')
        total += len(text.encode()) + len(thought.encode())
        if total > byte_limit:
            raise RuntimeError('Strata stream exceeds the transport allocation')
        if text:
            if first_answer_s is None:
                first_answer_s = time.monotonic() - started
            answer.append(text)
        if thought:
            reasoning.append(thought)
            on_thinking(thought)
        if choice.get('finish_reason') is not None:
            final = chunk, choice['finish_reason']
    if not ended or final is None:
        raise RuntimeError('Incomplete Strata generation')
    chunk, finish = final
    usage = chunk.get('usage')
    usage = usage if isinstance(usage, dict) else {}
    return {'done': True, 'done_reason': finish,
            'prompt_eval_count': count(usage.get('prompt_tokens')),
            'eval_count': count(usage.get('completion_tokens')),
            'usage': usage, 'timings': chunk.get('timings'),
            'first_answer_chunk_s': first_answer_s,
            'message': {'role': 'assistant', 'content': ''.join(answer), 'thinking': ''.join(reasoning)}}


class StrataModel(LocalModel):
    def __init__(self, log_dir, review=False, model=MODEL):
        if not is_strata(model):
            raise InvalidUpdate('Unknown Strata provider reference')
        self.host_json_mode = False
        self.native_profile = profile_metadata(model) if managed(model) else None
        if self.native_profile is not None:
            self.port = self.native_profile['port']
            self.server_identity = None
            maximum = self.native_profile['context_tokens']
        else:
            self.port = _configured_int('SUTRA_STRATA_PORT', 1, 65535)
            state = status(self.port)
            self.server_identity = _identity(state, model)
            maximum = self.server_identity['server_context_tokens']
        self.context_cap = min(maximum, _configured_int('SUTRA_STRATA_CONTEXT', 4096, 65536, 65536))
        self.context_metadata = {
            'model_info': {'strata.context_length': maximum},
            '_sutra_runtime_cap': self.context_cap,
            '_sutra_provider': 'strata',
            # A cold profile's live format capability is unknown until
            # dispatch. Reserve the possible directive before memory planning.
            '_sutra_template_reserve': 256 + HOST_JSON_DIRECTIVE_BYTES,
            'thinking': {'values': ['low', 'medium', 'high'], 'default': 'high'},
        }
        from model_activity import thinking_control
        thinking = thinking_control(self.context_metadata, os.environ.get('SUTRA_THINKING', 'adaptive'))
        super().__init__(log_dir, self.port, model,
                         budget=Budget(context_limit=self.context_cap, response_reserve=2048 if review else 1024,
                                       template_reserve=self.context_metadata['_sutra_template_reserve']),
                         thinking=thinking)
        if self.native_profile is None:
            self._configure_structured_output(state)
        elif self.native_profile.get('ready') is True:
            try:
                state = status(self.port, self.native_profile['model'])
            except InvalidUpdate:
                state = None
        else:
            state = None
        if state is not None:
            from context_window import discover
            window = discover(self.port, state)
            if window is not None:
                self.context_metadata['_sutra_context_window'] = window

    def _configure_structured_output(self, state):
        self.host_json_mode = _host_json_capability(state)
        if self.host_json_mode and self.budget.template_reserve < 256 + HOST_JSON_DIRECTIVE_BYTES:
            raise InvalidUpdate('Strata host JSON directive exceeds the planned template allowance')

    def fresh_context_metadata(self):
        if self.native_profile is not None:
            if profile_identity(self.model, profile_metadata(self.model)) != profile_identity(self.model, self.native_profile):
                raise InvalidUpdate('Native Strata profile changed after context selection')
        elif _identity(status(self.port), self.model) != self.server_identity:
            raise InvalidUpdate('Strata server changed after context selection')
        return self.context_metadata

    @contextmanager
    def dispatch_gate(self, root, call_id, system, payload, schema):
        from runtime_memory import HEADROOM, denied, dispatch_context, enabled, fresh_resources, lease

        with (lease() if enabled() else nullcontext()):
            if self.native_profile is not None:
                activated = native_command('activate', self.model)
                fresh = profile_metadata(self.model, activated)
                if profile_identity(self.model, fresh) != profile_identity(self.model, self.native_profile):
                    raise InvalidUpdate('Native Strata profile changed during activation')
                live = activated.get('server')
                if (not isinstance(live, dict) or live.get('service') != 'strata' or live.get('loaded') is not True or
                        live.get('model') != fresh['model'] or count(live.get('started')) is None or
                        (live.get('context') or {}).get('max_positions') != fresh['context_tokens']):
                    raise InvalidUpdate('Native Strata readiness does not match the selected profile')
                self.server_identity = _identity(live, self.model)
            state = status(self.port, self.server_identity['server_model'])
            if _identity(state, self.model) != self.server_identity:
                raise InvalidUpdate('Strata server changed before dispatch')
            self._configure_structured_output(state)
            if self.native_profile is not None and state['context']['max_positions'] != self.native_profile['context_tokens']:
                raise InvalidUpdate('Native Strata context differs from the selected profile')
            activity = state.get('activity')
            if not isinstance(activity, dict) or type(activity.get('in_flight')) is not int or activity['in_flight'] != 0:
                denied('Strata is busy or its activity is unavailable.', kind='busy')
            hardware = fresh_resources(root)
            sample = hardware['sample']
            available = sample.get('available_bytes') if sample.get('profile') == 'portable-memory-v1' else (
                sample['free_bytes'] + sample['inactive_bytes'])
            from context_window import check, exact, runtime_context
            if exact(self.budget):
                self.context_measurement = check(self.budget, system, payload, schema, thinking=self.thinking)
                self.runtime_context_tokens = runtime_context(self.budget, self.context_measurement)
            else:
                self.runtime_context_tokens = dispatch_context(self, system, payload, schema)
            plan = {'decision': hardware['decision'], 'profile': 'warm-strata-v1', 'model': self.model,
                    'context_tokens': self.runtime_context_tokens, 'resident': True,
                    'available_bytes': available, 'system_reserve_bytes': HEADROOM,
                    'peak_memory_verified': False, 'source': 'Strata status and local hardware sample',
                    'estimate_scope': 'Native Strata is resident. Incremental inference memory is unmeasured.'}
            from runtime_memory import note_native_plan
            note_native_plan(plan)
            yield plan

    def call(self, call_id, system, payload, schema):
        from invocation_gateway import consume_dispatch
        from model_activity import ACTIVE
        from model_packet import build
        from reasoning_policy import check_provider
        from storage_policy import write_trace

        packet = build(system, payload, schema)
        consume_dispatch(call_id, self.model, system, payload, schema, packet=packet)
        check_provider(self, payload)
        charge = self.budget.check(system, payload, schema)
        from context_window import wire_request, exact, validate_proof
        body, _, wire_schema = wire_request(system, payload, schema, self.server_identity['server_model'],
                                           self.thinking, self.budget.response_reserve, host_json=self.host_json_mode)
        if exact(self.budget):
            validate_proof(getattr(self, 'context_measurement', None), self.budget, system, payload, schema)
            if self.context_measurement['wire_sha256'] != sha(canonical(body)):
                raise InvalidUpdate('Strata transport differs from its physical context proof')
        record = {'call_id': call_id, 'request': body, 'provider': 'strata',
                  'conservative_input_charge': charge,
                  'structured_output': {'mode': 'host_json' if self.host_json_mode else 'provider_json_schema',
                                        'basis': 'verified_prompt_and_validate' if self.host_json_mode else 'provider_default',
                                        'schema_sha256': sha(canonical(wire_schema))}}
        if exact(self.budget):
            record['physical_context'] = self.context_measurement
        activity = ACTIVE.get()
        if activity:
            activity.emit('control', requested=self.thinking, policy=payload.get('reasoning'))
        started = time.monotonic()
        from invocation_gateway import transport_seconds
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=timeout(transport_seconds(TRANSPORT_SECONDS)))
        response = None
        try:
            connection.request('POST', '/v1/chat/completions', json.dumps(body), _headers())
            # Strata serves HTTP/1.0, so HTTPConnection relinquishes its socket
            # on getresponse(). The response still owns its underlying stream.
            transport_socket = connection.sock
            response = connection.getresponse()
            if response.status in (401, 403):
                raise RuntimeError(f'Strata authentication failed (HTTP {response.status})')
            if response.status != 200:
                raise RuntimeError(f'Strata returned HTTP {response.status}')
            result = _stream(response, transport_socket, started, max(65536, self.budget.response_reserve * 128),
                             activity.thinking if activity else lambda _: None)
            record['response'] = result
            if result['done_reason'] == 'length':
                raise OutputLimitExceeded('Strata reached the allocated output limit')
            if result['done_reason'] != 'stop' or result['prompt_eval_count'] is None or result['eval_count'] is None:
                raise RuntimeError('Strata did not report a complete generation and token usage')
            if exact(self.budget) and result['prompt_eval_count'] != self.context_measurement['prompt_tokens']:
                raise RuntimeError('Strata prompt usage differs from its exact physical context measurement')
            if (result['prompt_eval_count'] + self.budget.response_reserve > self.runtime_context_tokens
                    or result['eval_count'] > body['max_tokens']):
                raise RuntimeError('Strata usage exceeds the admitted context or output budget')
            if self.native_profile is not None and _identity(status(self.port, self.server_identity['server_model']), self.model) != self.server_identity:
                raise InvalidUpdate('Native Strata server changed during inference')
            if self.host_json_mode:
                text, normalization = _host_json_text(result['message']['content'])
                record['structured_output']['normalization'] = normalization
                answer = _host_json_answer(text)
            else:
                answer = json.loads(result['message']['content'])
            record.update(answer=answer, ok=True)
        except Exception as exc:
            if getattr(exc, 'strata_response', None) is not None:
                record['response'] = exc.strata_response
            record.update(ok=False, error=str(exc))
            exc.call_record = record
            raise
        finally:
            if response is not None:
                response.close()
            connection.close()
            record['elapsed_s'] = time.monotonic() - started
            write_trace(self.log_dir, call_id, record)
        return answer, record
