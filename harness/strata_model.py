"""Explicit localhost Strata High provider. The invocation gateway owns admission."""
from contextlib import contextmanager, nullcontext
import http.client
import json
import os
import time

from context_manager import InvalidUpdate
from lifecycle import timeout
from local_model import LocalModel, OutputLimitExceeded, StructuredOutputRejected
from paging import Budget
from token_usage import count


MODEL = 'strata:high'
MAX_METADATA_BYTES = 65536


def _configured_int(key, lower, upper, default=None):
    value = os.environ.get(key)
    if value is None and default is not None:
        return default
    if not value or not value.isascii() or not value.isdecimal() or len(value) > 6:
        raise InvalidUpdate('Set a numeric ' + key + ' for Strata High')
    value = int(value)
    if not lower <= value <= upper:
        raise InvalidUpdate('Strata High setting is out of range: ' + key)
    return value


def enabled():
    return 'SUTRA_STRATA_PORT' in os.environ


def _headers():
    headers = {'Content-Type': 'application/json'}
    if key := os.environ.get('SUTRA_STRATA_API_KEY'):
        if not key.isascii() or not key.isprintable():
            raise InvalidUpdate('Strata High API key must use printable ASCII characters')
        headers['Authorization'] = 'Bearer ' + key
    return headers


def status(port):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout(3))
    response = None
    try:
        connection.request('GET', '/v1/status', headers=_headers())
        response = connection.getresponse()
        if response.status != 200:
            raise InvalidUpdate(f'Strata High status returned HTTP {response.status}')
        body = response.read(MAX_METADATA_BYTES + 1)
        if len(body) > MAX_METADATA_BYTES:
            raise InvalidUpdate('Strata High metadata exceeds the transport limit')
        value = json.loads(body)
    except (OSError, ValueError, http.client.HTTPException):
        raise InvalidUpdate('Strata High status is unavailable on localhost') from None
    finally:
        if response is not None:
            response.close()
        connection.close()
    if not isinstance(value, dict) or value.get('service') != 'strata':
        raise InvalidUpdate('Configured port does not describe a Strata server')
    if value.get('loaded') is not True:
        raise InvalidUpdate('Load the local Strata model before selecting Strata High')
    model, started = value.get('model'), value.get('started')
    context = value.get('context')
    limit = context.get('max_positions') if isinstance(context, dict) else None
    if (not isinstance(model, str) or not 1 <= len(model) <= 200 or not model.isprintable()
            or count(started) is None or count(limit) is None or not 4096 <= limit):
        raise InvalidUpdate('Strata High requires a known model, server start and context window')
    if model != os.environ.get('SUTRA_STRATA_MODEL'):
        raise InvalidUpdate('Strata High model does not match SUTRA_STRATA_MODEL')
    return value


def _identity(value):
    from model_identity import unknown
    version = value.get('engine')
    return dict(unknown(MODEL), backend='strata', basis='local_server_metadata',
                server_model=value['model'], server_started=value['started'],
                engine_version=version[:120] if isinstance(version, str) else None,
                server_context_tokens=value['context']['max_positions'])


def observe():
    return _identity(status(_configured_int('SUTRA_STRATA_PORT', 1, 65535)))


def append_catalog(catalog):
    catalog['models'] = [row for row in catalog['models'] if row.get('name') != MODEL]
    if not enabled():
        return catalog
    try:
        identity = observe()
    except InvalidUpdate as exc:
        catalog['errors'].append({'model': MODEL, 'error': str(exc)})
    else:
        catalog['models'].append({'name': MODEL, 'eligible': True, 'capabilities': ['completion'],
                                  'tier': 'high', 'parameter_size': identity['server_model'],
                                  'eligibility_reason': 'Explicit localhost Strata provider'})
    return catalog


def _stream(response, transport_socket, started, byte_limit, on_thinking):
    """Read the Strata SSE dialect with an absolute deadline and bounded frames."""
    answer, reasoning = [], []
    total, final, ended, first_answer_s = 0, None, False, None
    frame_limit = 6 * byte_limit + MAX_METADATA_BYTES  # One buffered JSON value plus escaping.
    while True:
        remaining = 150 - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError('Strata High transport deadline reached')
        transport_socket.settimeout(timeout(remaining))
        line = response.readline(frame_limit + 1)
        if not line:
            break
        if len(line) > frame_limit:
            raise RuntimeError('Strata High stream frame exceeds the transport limit')
        if line.startswith(b':') or not line.strip():
            continue
        if not line.startswith(b'data:'):
            raise RuntimeError('Unexpected Strata High stream frame')
        raw = line[5:].strip()
        if raw == b'[DONE]':
            ended = True
            break
        chunk = json.loads(raw)
        if not isinstance(chunk, dict):
            raise RuntimeError('Invalid Strata High stream event')
        if 'error' in chunk:
            error = chunk['error']
            if isinstance(error, dict) and error.get('code') == 'structured_output_failed':
                if str(error.get('message')).startswith('structured output was incomplete (finish_reason=length)'):
                    raise OutputLimitExceeded('Strata High reached the allocated output limit')
                raise StructuredOutputRejected('Strata rejected the structured reply. Return one JSON object matching the supplied schema.')
            raise RuntimeError('Strata High reported a generation error')
        if 'prompt_progress' in chunk and not chunk.get('choices'):
            continue
        choices = chunk.get('choices')
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict) or final:
            raise RuntimeError('Invalid Strata High stream choice')
        choice = choices[0]
        delta = choice.get('delta')
        if not isinstance(delta, dict) or delta.get('tool_calls') or chunk.get('strata_rejected_tool_calls'):
            raise RuntimeError('Unexpected tool call or invalid Strata High text event')
        text, thought = delta.get('content', ''), delta.get('reasoning_content', '')
        if not isinstance(text, str) or not isinstance(thought, str):
            raise RuntimeError('Invalid Strata High stream text')
        total += len(text.encode()) + len(thought.encode())
        if total > byte_limit:
            raise RuntimeError('Strata High stream exceeds the transport allocation')
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
        raise RuntimeError('Incomplete Strata High generation')
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
    def __init__(self, log_dir, review=False):
        self.port = _configured_int('SUTRA_STRATA_PORT', 1, 65535)
        state = status(self.port)
        self.server_identity = _identity(state)
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
        super().__init__(log_dir, self.port, MODEL,
                         budget=Budget(context_limit=self.context_cap, response_reserve=2048 if review else 1024),
                         thinking=thinking)

    def fresh_context_metadata(self):
        if _identity(status(self.port)) != self.server_identity:
            raise InvalidUpdate('Strata High server changed after context selection')
        return self.context_metadata

    @contextmanager
    def dispatch_gate(self, root, call_id, system, payload, schema):
        from runtime_memory import HEADROOM, denied, dispatch_context, enabled, fresh_resources, lease

        with (lease() if enabled() else nullcontext()):
            state = status(self.port)
            if _identity(state) != self.server_identity:
                raise InvalidUpdate('Strata High server changed before dispatch')
            activity = state.get('activity')
            if not isinstance(activity, dict) or type(activity.get('in_flight')) is not int or activity['in_flight'] != 0:
                denied('Strata High is busy or its activity is unavailable.', kind='busy')
            hardware = fresh_resources(root)
            sample = hardware['sample']
            available = sample.get('available_bytes') if sample.get('profile') == 'portable-memory-v1' else (
                sample['free_bytes'] + sample['inactive_bytes'])
            self.runtime_context_tokens = dispatch_context(self, system, payload, schema)
            plan = {'decision': hardware['decision'], 'profile': 'warm-strata-v1', 'model': self.model,
                    'context_tokens': self.runtime_context_tokens, 'resident': True,
                    'available_bytes': available, 'system_reserve_bytes': HEADROOM,
                    'peak_memory_verified': False, 'source': 'Strata status and local hardware sample',
                    'estimate_scope': 'Preloaded Strata. Incremental inference memory is unmeasured.'}
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
        record = {'call_id': call_id, 'request': body, 'provider': 'strata',
                  'conservative_input_charge': charge}
        activity = ACTIVE.get()
        if activity:
            activity.emit('control', requested=self.thinking, policy=payload.get('reasoning'))
        started = time.monotonic()
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=timeout(150))
        response = None
        try:
            connection.request('POST', '/v1/chat/completions', json.dumps(body), _headers())
            # Strata serves HTTP/1.0, so HTTPConnection relinquishes its socket
            # on getresponse(). The response still owns its underlying stream.
            transport_socket = connection.sock
            response = connection.getresponse()
            if response.status in (401, 403):
                raise RuntimeError(f'Strata High authentication failed (HTTP {response.status})')
            if response.status != 200:
                raise RuntimeError(f'Strata High returned HTTP {response.status}')
            result = _stream(response, transport_socket, started, max(65536, self.budget.response_reserve * 128),
                             activity.thinking if activity else lambda _: None)
            record['response'] = result
            if result['done_reason'] == 'length':
                raise OutputLimitExceeded('Strata High reached the allocated output limit')
            if result['done_reason'] != 'stop' or result['prompt_eval_count'] is None or result['eval_count'] is None:
                raise RuntimeError('Strata High did not report a complete generation and token usage')
            if (result['prompt_eval_count'] + self.budget.response_reserve > self.runtime_context_tokens
                    or result['eval_count'] > self.budget.response_reserve):
                raise RuntimeError('Strata High usage exceeds the admitted context or output budget')
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
