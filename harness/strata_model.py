"""Managed native profiles and the legacy localhost Strata provider. The invocation gateway owns admission."""
from contextlib import contextmanager, nullcontext
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from context_manager import InvalidUpdate
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
                                'decision':None,'reason':'Native activation response timed out; its owner may still be loading. Resume to inspect the saved native state; no model inference or budget reset occurred.'}) from None
        raise InvalidUpdate('Native Strata metadata inspection timed out') from None
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise InvalidUpdate('Native Strata management is unavailable: '+str(error)[:160]) from None
    if not isinstance(result, dict) or result.get('ok') is not True:
        error = (result.get('error') or 'Native Strata operation was not confirmed') if isinstance(result, dict) else 'Invalid native Strata response'
        if isinstance(result, dict) and result.get('status') in ('paused', 'busy', 'timeout'):
            from monitoring import ResourceWait
            raise ResourceWait({'profile':'native-strata-v1','available':False,'admitted':False,
                                'decision':result.get('decision'),'reason':str(error)[:600]})
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


def _stream(response, transport_socket, started, byte_limit, on_thinking):
    """Read the Strata SSE dialect with an absolute deadline and bounded frames."""
    answer, reasoning = [], []
    total, final, ended, first_answer_s = 0, None, False, None
    frame_limit = 6 * byte_limit + MAX_METADATA_BYTES  # One buffered JSON value plus escaping.
    while True:
        remaining = TRANSPORT_SECONDS - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError('Strata transport deadline reached')
        transport_socket.settimeout(timeout(remaining))
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
        chunk = json.loads(raw)
        if not isinstance(chunk, dict):
            raise RuntimeError('Invalid Strata stream event')
        if 'error' in chunk:
            error = chunk['error']
            if isinstance(error, dict) and error.get('code') == 'structured_output_failed':
                if str(error.get('message')).startswith('structured output was incomplete (finish_reason=length)'):
                    raise OutputLimitExceeded('Strata reached the allocated output limit')
                raise StructuredOutputRejected('Strata rejected the structured reply. Return one JSON object matching the supplied schema.')
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
            'thinking': {'values': ['low', 'medium', 'high'], 'default': 'high'},
        }
        from model_activity import thinking_control
        thinking = thinking_control(self.context_metadata, os.environ.get('SUTRA_THINKING', 'adaptive'))
        super().__init__(log_dir, self.port, model,
                         budget=Budget(context_limit=self.context_cap, response_reserve=2048 if review else 1024),
                         thinking=thinking)

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
            if self.native_profile is not None and state['context']['max_positions'] != self.native_profile['context_tokens']:
                raise InvalidUpdate('Native Strata context differs from the selected profile')
            activity = state.get('activity')
            if not isinstance(activity, dict) or type(activity.get('in_flight')) is not int or activity['in_flight'] != 0:
                denied('Strata is busy or its activity is unavailable.', kind='busy')
            hardware = fresh_resources(root)
            sample = hardware['sample']
            available = sample.get('available_bytes') if sample.get('profile') == 'portable-memory-v1' else (
                sample['free_bytes'] + sample['inactive_bytes'])
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
        body = {'model': self.server_identity['server_model'],
                'messages': packet['messages'] if packet else self.messages(system, payload),
                'response_format': {'type': 'json_schema', 'json_schema': {
                    'name': 'sutra_proposal', 'schema': packet['schema'] if packet else schema}},
                'stream': True, 'max_tokens': self.budget.response_reserve, 'temperature': 0, 'seed': 42}
        if self.thinking is not None:
            body['reasoning_effort'] = 'none' if self.thinking is False else self.thinking
        thinking_limit = ADAPTIVE_THINKING_LIMITS.get(self.thinking)
        if (payload.get('reasoning') or {}).get('preference') == 'adaptive' and thinking_limit is not None:
            body['max_tokens'] = min(body['max_tokens'], ADAPTIVE_MAX_GENERATION)
            body['reasoning_budget_tokens'] = min(thinking_limit, body['max_tokens'] // 3)
        record = {'call_id': call_id, 'request': body, 'provider': 'strata',
                  'conservative_input_charge': charge}
        activity = ACTIVE.get()
        if activity:
            activity.emit('control', requested=self.thinking, policy=payload.get('reasoning'))
        started = time.monotonic()
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=timeout(TRANSPORT_SECONDS))
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
            if (result['prompt_eval_count'] + self.budget.response_reserve > self.runtime_context_tokens
                    or result['eval_count'] > body['max_tokens']):
                raise RuntimeError('Strata usage exceeds the admitted context or output budget')
            if self.native_profile is not None and _identity(status(self.port, self.server_identity['server_model']), self.model) != self.server_identity:
                raise InvalidUpdate('Native Strata server changed during inference')
            answer = json.loads(result['message']['content'])
            record.update(answer=answer, ok=True)
        except Exception as exc:
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
