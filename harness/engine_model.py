"""Invocation-gateway provider for a Sutra-owned local engine worker."""
from contextlib import contextmanager
import json
import os
import re
import time

from context_manager import InvalidUpdate
import engine_profiles
from local_model import LocalModel, OutputLimitExceeded, StructuredOutputRejected
from paging import Budget
from token_usage import count


def _client():
    import engine_client
    return engine_client


def _instance(value, profile):
    if (not isinstance(value, dict) or value.get('profile_hash') != profile['profile_hash'] or
            not re.fullmatch(r'[a-f0-9]{32}', str(value.get('instance_id', ''))) or
            value.get('context_tokens') != profile['context_tokens'] or
            type(value.get('pid')) is not int or value['pid'] <= 0):
        raise InvalidUpdate('Sutra engine readiness differs from the selected profile')
    return value


class EngineModel(LocalModel):
    def __init__(self, log_dir, model, review=False):
        self.profile = engine_profiles.load(model)
        maximum = self.profile['context_tokens']
        configured = os.environ.get('SUTRA_ENGINE_CONTEXT', '65536')
        if not configured.isascii() or not configured.isdecimal() or not 4096 <= int(configured) <= 2**21:
            raise InvalidUpdate('Sutra engine context cap must be 4096..2097152 tokens')
        self.context_cap = min(maximum, int(configured))
        self.context_metadata = {
            'model_info': {'sutra_engine.context_length': maximum},
            '_sutra_runtime_cap': self.context_cap,
            '_sutra_provider': 'sutra_engine',
            'thinking': {'values': [False, True], 'default': True},
        }
        from model_activity import thinking_control
        thinking = thinking_control(self.context_metadata, os.environ.get('SUTRA_THINKING', 'adaptive'))
        super().__init__(log_dir, port=None, model=model,
                         budget=Budget(context_limit=self.context_cap, response_reserve=2048 if review else 1024),
                         thinking=thinking)
        self.engine_instance = None

    def fresh_context_metadata(self):
        current = engine_profiles.load(self.model)
        if current['profile_hash'] != self.profile['profile_hash']:
            raise InvalidUpdate('Sutra engine profile changed after context selection')
        return self.context_metadata

    @contextmanager
    def dispatch_gate(self, root, call_id, system, payload, schema):
        from runtime_memory import HEADROOM, dispatch_context, enabled, fresh_resources, lease
        if self.model not in engine_profiles.assigned_models():
            raise InvalidUpdate('Sutra engine profile is not assigned in trusted model settings')
        engine_profiles.require_approval(self.profile)
        if not enabled():
            raise InvalidUpdate('Sutra engine dispatch requires the shared host runtime directory')
        with lease():
            self.fresh_context_metadata()
            # Reject an oversized packet before loading or replacing a model.
            self.runtime_context_tokens = dispatch_context(self, system, payload, schema)
            self.engine_instance = _instance(_client().activate(self.profile), self.profile)
            hardware = fresh_resources(root)
            sample = hardware['sample']
            available = sample.get('available_bytes') if sample.get('profile') == 'portable-memory-v1' else (
                sample['free_bytes'] + sample['inactive_bytes'])
            plan = {'decision': hardware['decision'], 'profile': 'sutra-engine-v1',
                    'provider': 'sutra_engine', 'model': self.model,
                    'context_tokens': self.runtime_context_tokens, 'resident': True,
                    'available_bytes': available, 'system_reserve_bytes': HEADROOM,
                    'peak_memory_verified': False, 'source': 'Sutra engine readiness and fresh hardware',
                    'estimate_scope': 'Sutra-owned engine is resident. Incremental inference memory is unmeasured.',
                    'instance_id': self.engine_instance['instance_id']}
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
        if self.engine_instance is None:
            raise InvalidUpdate('Sutra engine has no admitted model instance')
        charge = self.budget.check(system, payload, schema)
        messages = packet['messages'] if packet else self.messages(system, payload)
        output_schema = packet['schema'] if packet else schema
        body = {'model': self.model, 'profile_hash': self.profile['profile_hash'],
                'messages': messages, 'schema': output_schema,
                'max_tokens': self.budget.response_reserve,
                'context_tokens': self.runtime_context_tokens, 'thinking': self.thinking,
                'instance_id': self.engine_instance['instance_id']}
        record = {'call_id': call_id, 'request': body, 'provider': 'sutra_engine',
                  'conservative_input_charge': charge}
        activity = ACTIVE.get()
        if activity:
            activity.emit('control', requested=self.thinking, policy=payload.get('reasoning'))
        started = time.monotonic()
        try:
            result = _client().generate(
                self.profile, messages, self.budget.response_reserve, self.thinking, output_schema,
                on_thinking=activity.thinking if activity else lambda text: None,
                on_progress=(lambda progress: activity.emit('progress', **progress)) if activity else None,
                context_tokens=self.runtime_context_tokens,
                expected_instance_id=self.engine_instance['instance_id'])
            if not isinstance(result, dict):
                raise RuntimeError('Sutra engine returned an invalid generation')
            content, thinking = result.get('content'), result.get('thinking', '')
            if not isinstance(content, str) or not isinstance(thinking, str):
                raise RuntimeError('Sutra engine returned invalid text')
            if len(content.encode()) + len(thinking.encode()) > max(65536, self.budget.response_reserve * 128):
                raise RuntimeError('Sutra engine text exceeds the transport allocation')
            response = {'done': True, 'done_reason': result.get('finish_reason'),
                        'prompt_eval_count': count(result.get('prompt_tokens')),
                        'eval_count': count(result.get('completion_tokens')),
                        'message': {'role': 'assistant', 'content': content, 'thinking': thinking},
                        'timings': result.get('timings'), 'instance_id': result.get('instance_id')}
            if result.get('instance_id') != self.engine_instance['instance_id']:
                raise InvalidUpdate('Sutra engine instance changed during inference')
            from engine_client import observed_usage
            observed_usage(result)
            if (response['prompt_eval_count'] + self.budget.response_reserve > self.runtime_context_tokens or
                    response['eval_count'] > self.budget.response_reserve):
                raise RuntimeError('Sutra engine usage exceeds the admitted context or output budget')
            record['response'] = response
            if response['done_reason'] == 'length':
                raise OutputLimitExceeded('Sutra engine reached the allocated output limit')
            if response['done_reason'] != 'stop' or response['prompt_eval_count'] is None or response['eval_count'] is None:
                raise RuntimeError('Sutra engine did not report a complete generation with token usage')
            self.fresh_context_metadata()
            try:
                answer = json.loads(content)
            except ValueError as failure:
                try:
                    from output_shape import observe
                    record['output_shape'] = observe(content, thinking, failure)
                except Exception:
                    pass  # Advisory diagnostics must not replace the parser failure.
                raise StructuredOutputRejected('Sutra engine reply is not one JSON value matching the supplied schema.') from None
            record.update(answer=answer, ok=True)
        except Exception as error:
            usage = getattr(error, 'engine_usage', None)
            if isinstance(error, TimeoutError) and isinstance(usage, dict):
                prompt, completion = count(usage.get('prompt_tokens')), count(usage.get('completion_tokens'))
                if (prompt is not None and completion is not None and
                        prompt + self.budget.response_reserve <= self.runtime_context_tokens and
                        completion <= self.budget.response_reserve):
                    # A drained native timeout still consumed these observed
                    # tokens. It supplies no completed answer to the gateway.
                    record['response'] = {
                        'done': False, 'done_reason': 'cancelled',
                        'usage_finalized': True,
                        'prompt_eval_count': prompt, 'eval_count': completion,
                        'message': {'role': 'assistant', 'content': '', 'thinking': ''},
                        'instance_id': self.engine_instance['instance_id'],
                    }
            record.update(ok=False, error=str(error))
            error.call_record = record
            raise
        finally:
            record['elapsed_s'] = time.monotonic() - started
            write_trace(self.log_dir, call_id, record)
        return answer, record


def append_catalog(catalog, path=None):
    catalog['models'] = [row for row in catalog['models'] if not engine_profiles.is_engine(row.get('name'))]
    catalog.setdefault('errors', [])
    try:
        observed = engine_profiles.catalog(path)
    except InvalidUpdate as error:
        catalog['errors'].append({'provider': 'sutra_engine', 'error': str(error)})
    else:
        catalog['models'].extend(observed['models'])
        catalog['errors'].extend(observed['errors'])
    return catalog
