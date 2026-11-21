"""Invocation-gateway transport for an approved, SSH-tunnelled CUDA model."""
from contextlib import contextmanager
import http.client
import json
import math
import os
import time

from context_manager import InvalidUpdate, canonical, sha
from lifecycle import timeout
from local_model import LocalModel, OutputLimitExceeded, StructuredOutputRejected
from paging import Budget
import remote_control
import remote_profiles
from token_usage import count


def append_catalog(value, path=None):
    return remote_profiles.append_catalog(value, path)


def _json_answer(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result

    def constant(_):
        raise ValueError('Non-finite JSON value')

    def number(raw):
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError('Non-finite JSON value')
        return value

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)
    except (ValueError, RecursionError):
        raise StructuredOutputRejected('Remote model reply is not one JSON object matching the supplied schema') from None
    if not isinstance(value, dict):
        raise StructuredOutputRejected('Remote model reply must be one JSON object matching the supplied schema')
    return value


def _read_stream(response, transport_socket, started, byte_limit, activity, transport):
    content, thoughts = [], []
    finish, usage, timings = None, None, None
    ended = False
    first_answer = None
    total = 0
    while True:
        from invocation_gateway import transport_timeout
        if transport_socket is not None:
            transport_socket.settimeout(transport_timeout(started))
        raw = response.readline(65537)
        if not raw:
            break
        if len(raw) > 65536:
            raise RuntimeError('Remote stream frame exceeds its transport allocation')
        if not raw.strip() or raw.startswith(b':'):
            continue
        if not raw.startswith(b'data:'):
            raise RuntimeError('Unexpected remote SSE frame')
        data = raw[5:].strip()
        if data == b'[DONE]':
            ended = True
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            raise RuntimeError('Remote stream contains invalid JSON') from None
        if not isinstance(chunk, dict) or chunk.get('error'):
            raise RuntimeError('Remote stream reported an invalid event or generation failure')
        transport['frames_received'] += 1
        observed_usage = chunk.get('usage')
        if observed_usage is not None:
            if not isinstance(observed_usage, dict):
                raise RuntimeError('Remote stream usage has an invalid shape')
            current = (count(observed_usage.get('prompt_tokens')), count(observed_usage.get('completion_tokens')))
            if any(value is None for value in current):
                raise RuntimeError('Remote stream did not report integer token usage')
            if usage is not None and current != usage:
                raise RuntimeError('Remote stream token usage changed after completion')
            usage = current
        if isinstance(chunk.get('timings'), dict):
            timings = chunk['timings']
        choices = chunk.get('choices')
        # OpenAI-compatible servers may send usage in a separate final frame.
        if choices == [] and observed_usage is not None:
            continue
        if (not isinstance(choices, list) or len(choices) != 1 or
                not isinstance(choices[0], dict) or finish is not None):
            raise RuntimeError('Remote stream choice changed after its final answer')
        choice = choices[0]
        delta = choice.get('delta')
        if not isinstance(delta, dict) or delta.get('tool_calls') or delta.get('function_call'):
            raise RuntimeError('Remote provider returned an unexpected tool call or invalid text event')
        text, thought = delta.get('content', ''), delta.get('reasoning_content', '')
        text = '' if text is None else text
        thought = '' if thought is None else thought
        if not isinstance(text, str) or not isinstance(thought, str):
            raise RuntimeError('Remote model stream text has an invalid type')
        total += len(text.encode()) + len(thought.encode())
        transport['content_bytes'] += len(text.encode())
        transport['reasoning_bytes'] += len(thought.encode())
        if total > byte_limit:
            raise RuntimeError('Remote model text exceeds its transport allocation')
        if text:
            content.append(text)
            if first_answer is None:
                first_answer = time.monotonic() - started
        if thought:
            thoughts.append(thought)
            if activity:
                activity.thinking(thought)
        reason = choice.get('finish_reason')
        if reason is not None:
            if not isinstance(reason, str):
                raise RuntimeError('Remote finish reason has an invalid type')
            finish = reason
    if not ended or finish is None or usage is None:
        raise RuntimeError('Remote generation ended without final finish and token-usage receipts')
    return {'done': True, 'done_reason': finish,
            'prompt_eval_count': usage[0], 'eval_count': usage[1],
            'message': {'role': 'assistant', 'content': ''.join(content), 'thinking': ''.join(thoughts)},
            'timings': timings, 'first_answer_chunk_s': first_answer}


class RemoteModel(LocalModel):
    def __init__(self, log_dir, model, review=False):
        self.profile = remote_profiles.load(model)
        self.remote_profile = self.profile
        maximum = self.profile['context_tokens']
        configured = os.environ.get('SUTRA_REMOTE_CONTEXT', str(maximum))
        if (not configured.isascii() or not configured.isdecimal() or
                not 4096 <= int(configured) <= 2**21):
            raise InvalidUpdate('Remote context cap must be 4096..2097152 tokens')
        self.context_cap = min(maximum, int(configured))
        self.context_metadata = {
            'model_info': {'remote_cuda.context_length': maximum},
            '_sutra_runtime_cap': self.context_cap,
            '_sutra_provider': 'sutra_remote',
            # The installed model is thinking-only. A generic off request is
            # recorded as unsupported, never advertised as effective control.
            'thinking': {'values': [], 'default': None},
            'thinking_only': self.profile['thinking_only'],
        }
        from model_activity import thinking_control
        thinking = thinking_control(self.context_metadata, os.environ.get('SUTRA_THINKING', 'adaptive'))
        super().__init__(log_dir, port=self.profile['port'], model=model,
                         budget=Budget(context_limit=self.context_cap, response_reserve=2048),
                         thinking=thinking)
        self.remote_instance = None
        self.expected_identity = remote_profiles.identity(model, self.profile)

    def fresh_context_metadata(self):
        current = remote_profiles.load(self.model)
        if current['profile_hash'] != self.profile['profile_hash']:
            raise InvalidUpdate('Remote provider profile changed after context selection')
        return self.context_metadata

    @contextmanager
    def dispatch_gate(self, root, call_id, system, payload, schema):
        from remote_memory import dispatch
        remote_profiles.require_assignment(self.profile)
        self.fresh_context_metadata()
        with dispatch(self, root, call_id, system, payload, schema) as plan:
            yield plan

    def call(self, call_id, system, payload, schema):
        from decoder_schema import for_decoder
        from invocation_gateway import consume_dispatch, transport_seconds
        from model_activity import ACTIVE
        from model_packet import build
        from reasoning_policy import check_provider
        from storage_policy import write_trace

        packet = build(system, payload, schema)
        consume_dispatch(call_id, self.model, system, payload, schema, packet=packet)
        check_provider(self, payload)
        remote_profiles.require_assignment(self.profile)
        self.fresh_context_metadata()
        if self.remote_instance is None:
            raise InvalidUpdate('Remote model has no admitted, verified GPU instance')
        charge = self.budget.check(system, payload, schema)
        messages = packet['messages'] if packet else self.messages(system, payload)
        wire_schema = for_decoder(packet['schema'] if packet else schema, None)
        body = {'model': self.profile['model'], 'messages': messages,
                'stream': True, 'stream_options': {'include_usage': True},
                'max_tokens': self.budget.response_reserve,
                'temperature': 0.6, 'top_p': 0.95, 'top_k': 20, 'min_p': 0,
                'seed': 42, 'cache_prompt': False,
                'response_format': {'type': 'json_schema', 'json_schema': {
                    'name': 'sutra_response', 'strict': True, 'schema': wire_schema}}}
        transport = {'version': 1, 'phase': 'connect', 'headers_received': False,
                     'frames_received': 0, 'content_bytes': 0, 'reasoning_bytes': 0}
        record = {'call_id': call_id, 'request': body, 'provider': 'sutra_remote',
                  'conservative_input_charge': charge, 'remote_profile_hash': self.profile['profile_hash'],
                  'remote_instance': self.remote_instance, 'transport': transport,
                  'structured_output': {'mode': 'provider_json_schema', 'basis': 'provider_default',
                                        'schema_sha256': sha(canonical(wire_schema))},
                  'reasoning_control': {'requested': self.thinking, 'effective': 'model_default',
                                        'switch_supported': False, 'thinking_only': self.profile['thinking_only']}}
        activity = ACTIVE.get()
        if activity:
            activity.emit('control', requested=self.thinking, policy=payload.get('reasoning'),
                          effective='model_default', switch_supported=False,
                          thinking_only=self.profile['thinking_only'])
        started = time.monotonic()
        connection = http.client.HTTPConnection('127.0.0.1', self.port,
                                                timeout=timeout(transport_seconds(self.transport_deadline_cap)))
        response = None
        self.runtime_completed = False
        try:
            remote_control.verify_server(self.profile)
            from remote_memory import require_dispatch_fresh, verify_instance
            verify_instance(self.profile, self.remote_instance)
            if self.health:
                if self.health.problem():
                    raise RuntimeError(self.health.problem())
                self.health.active = connection
                self.health.phase = call_id
            request_body = json.dumps(body)
            request_headers = remote_control.headers(self.profile)
            require_dispatch_fresh(self)
            connection.request('POST', '/v1/chat/completions', request_body, request_headers)
            transport['phase'] = 'response_headers'
            transport_socket = connection.sock
            response = connection.getresponse()
            transport.update(headers_received=True, phase='stream')
            if response.status != 200:
                raise RuntimeError('Remote inference returned HTTP ' + str(response.status))
            result = _read_stream(response, transport_socket, started,
                                  max(65536, self.budget.response_reserve * 128), activity, transport)
            self.runtime_completed = True
            record['response'] = result
            if result['done_reason'] == 'length':
                raise OutputLimitExceeded('Remote model reached the allocated output limit; reasoning and answer share that allocation')
            if result['done_reason'] != 'stop':
                raise RuntimeError('Remote model did not report a complete text generation')
            if (result['prompt_eval_count'] + self.budget.response_reserve > self.runtime_context_tokens or
                    result['eval_count'] > self.budget.response_reserve):
                raise RuntimeError('Remote usage exceeds the admitted context or output allocation')
            self.fresh_context_metadata()
            remote_control.verify_server(self.profile)
            verify_instance(self.profile, self.remote_instance)
            transport['phase'] = 'decode'
            try:
                answer = _json_answer(result['message']['content'])
            except StructuredOutputRejected as failure:
                try:
                    from output_shape import observe
                    record['output_shape'] = observe(result['message']['content'], result['message']['thinking'], failure)
                except Exception:
                    pass
                raise
            transport['phase'] = 'complete'
            record.update(answer=answer, ok=True)
        except BaseException as error:
            record.update(ok=False, error=str(error))
            error.call_record = record
            raise
        finally:
            if self.health:
                self.health.active = None
            if response is not None:
                response.close()
            connection.close()
            record['elapsed_s'] = time.monotonic() - started
            write_trace(self.log_dir, call_id, record)
        return answer, record
