import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import conversation
from context_manager import ContextBudgetExceeded, InvalidUpdate
from engine_model import EngineModel
import engine_profiles
from invocation_gateway import InvocationLedger
from karma_adapter import KarmaAdapter
from model_catalog import combined
from model_routing import choose
from review_budget import usage
from test_engine_profiles import descriptor


HELLO = {'page_requests': [], 'intent': 'conversation', 'escalation_reason': 'none',
         'diagram': None, 'answer': 'Hello. How can I help?', 'summary': 'A hello exchange.'}


class DirectEngineModelTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.registry = self.root / 'models.json'
        self.models = {tier: 'sutra:engine/' + tier for tier in ('low', 'mid', 'high')}
        engine_profiles.write({tier: descriptor(self.root, tier, context) for tier, context in
                               (('low', 16384), ('mid', 24576), ('high', 32768))}, self.registry)
        environment = patch.dict(os.environ, SUTRA_ENGINE_REGISTRY=str(self.registry),
                                 SUTRA_ENGINE_MODELS=json.dumps(list(self.models.values())),
                                 SUTRA_ENGINE_ONLY='1', SUTRA_STRATA_MODELS='[]',
                                 SUTRA_RUNTIME_DIR=str(self.root / 'runtime'), SUTRA_THINKING='off')
        environment.start(); self.addCleanup(environment.stop)
        self.ledger = InvocationLedger(self.root / 'state.sqlite')
        self.addCleanup(self.ledger.close)
        self.client = SimpleNamespace(activate=self.activate, generate=self.generate)
        self.calls = []; self.instances = {}; self.reply = copy.deepcopy(HELLO)
        client = patch('engine_model._client', return_value=self.client)
        client.start(); self.addCleanup(client.stop)
        import time
        hardware = KarmaAdapter().hardware({'profile': 'portable-memory-v1', 'wall_time': time.time(),
                                           'physical_memory_bytes': 64 * 1024**3,
                                           'available_bytes': 40 * 1024**3}, time.time())
        for location in ('monitoring.require_resources', 'runtime_memory.fresh_resources'):
            guard = patch(location, return_value=hardware)
            guard.start(); self.addCleanup(guard.stop)

    def activate(self, profile):
        self.calls.append(('activate', profile['name']))
        instance = profile['profile_hash'][:32]
        self.instances[profile['name']] = instance
        return {'profile_hash': profile['profile_hash'], 'instance_id': instance,
                'context_tokens': profile['context_tokens'], 'pid': 12345}

    def generate(self, profile, messages, max_tokens, thinking, schema, **options):
        self.calls.append(('generate', profile['name']))
        self.assertEqual(options['expected_instance_id'], self.instances[profile['name']])
        self.assertEqual(thinking, False)
        self.assertNotIn('context_binding', schema.get('properties', {}))
        return {'content': json.dumps(self.reply), 'thinking': '', 'prompt_tokens': 124,
                'completion_tokens': 30, 'finish_reason': 'stop',
                'instance_id': self.instances[profile['name']],
                'timings': {'prompt_n': 124, 'predicted_n': 30, 'predicted_per_second': 25.0}}

    def ask(self, tier='low', parent=None, **extra):
        return conversation.answer(self.ledger, {}, self.root,
                                   {'prompt': 'Hello' if parent is None else 'Continue our same task.',
                                    'models': self.models, 'tier': tier, 'parent': parent,
                                    'one_step': True, **extra})

    def test_metadata_inspection_never_starts_a_worker(self):
        provider = EngineModel(self.root / 'calls', self.models['low'])
        provider.fresh_context_metadata()
        self.assertEqual(self.calls, [])
        self.assertIsNone(provider.engine_instance)

    def test_all_native_tiers_keep_context_contract_and_spending_without_foreign_runtime(self):
        parent = None; contract = None
        with patch('vscode_bridge.ollama', side_effect=AssertionError('Ollama is forbidden')), \
                patch('strata_model.native_command', side_effect=AssertionError('Strata Control is forbidden')), \
                patch('runtime_memory.api', side_effect=AssertionError('Ollama is forbidden')):
            for index, tier in enumerate(('low', 'mid', 'high'), 1):
                result = self.ask(tier, parent)
                self.assertTrue(result['result']['admission']['admitted'], result)
                session = result['session']
                if contract is None:
                    contract = self.ledger.current_contract(session)
                else:
                    self.assertEqual(self.ledger.current_contract(session), contract)
                    self.assertTrue(result['memory_used'])
                observed = usage(self.ledger, session)
                self.assertEqual(observed['used']['calls'], index)
                self.assertEqual(observed['used']['tokens'], index * 154)
                self.assertTrue(self.ledger.verify_invocations(session))
                token_record = result['result']['token_usage']['calls'][0]
                self.assertEqual(token_record['source'], 'sutra_engine_response')
                self.assertEqual(result['model_capacity']['source'], 'sutra_engine_metadata')
                parent = result['result']['invocation_id']
        self.assertEqual([name for event, name in self.calls if event == 'activate'], list(self.models.values()))

    def test_unassigned_model_and_missing_runtime_slot_cannot_activate(self):
        with patch.dict(os.environ, SUTRA_ENGINE_MODELS='[]'):
            with self.assertRaisesRegex(InvalidUpdate, 'not assigned'):
                self.ask()
        self.assertEqual(self.calls, [])
        with patch.dict(os.environ, SUTRA_RUNTIME_DIR=''):
            with self.assertRaisesRegex(InvalidUpdate, 'runtime directory'):
                self.ask()
        self.assertEqual(self.calls, [])

    def test_inadmissible_packet_cannot_replace_a_loaded_profile_or_spend_its_task_budget(self):
        first = self.ask('low')
        self.assertTrue(first['result']['admission']['admitted'])
        calls = list(self.calls)
        spent = copy.deepcopy(usage(self.ledger, first['session']))
        contract = self.ledger.current_contract(first['session'])
        provider = EngineModel(self.root / 'calls', self.models['high'])
        with self.assertRaises(ContextBudgetExceeded):
            with provider.dispatch_gate(self.root, 'oversized', 'Explain this text',
                                        {'text': 'x' * provider.budget.context_limit}, {'type': 'object'}):
                self.fail('An oversized packet must never reach inference')
        self.assertEqual(self.calls, calls, 'A rejected packet must not activate or generate')
        self.assertIsNone(provider.engine_instance)
        self.assertEqual(self.ledger.current_contract(first['session']), contract)
        self.assertEqual(usage(self.ledger, first['session']), spent)
        self.assertTrue(self.ledger.verify_invocations(first['session']))
    def test_worker_instance_and_output_limits_are_checked(self):
        original = self.client.generate
        def replaced(*args, **kwargs):
            result = original(*args, **kwargs)
            result['instance_id'] = 'f' * 32
            return result
        self.client.generate = replaced
        result = self.ask()
        self.assertFalse(result['result']['admission']['admitted'])
        self.assertIn('instance changed', result['result']['blocked'])
        self.assertTrue(self.ledger.verify_invocations(result['session']))

    def test_all_engine_catalog_does_not_call_strata_or_ollama(self):
        with patch('strata_model.native_command', side_effect=AssertionError('Strata Control is forbidden')):
            observed = combined(lambda *args: self.fail('Ollama is forbidden'))
        self.assertEqual({row['name'] for row in observed['models']}, set(self.models.values()))
        for tier in self.models:
            self.assertEqual(choose('Hello', self.models, tier, {'source_files': 0, 'selected_files': 0,
                                                              'unresolved_calls': 0})['model'], self.models[tier])

    def test_legacy_catalog_without_host_engine_configuration_does_not_read_global_registry(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch('engine_profiles.catalog', side_effect=AssertionError('No host opt-in')):
            result = combined(lambda *args: {'models': []})
        self.assertEqual(result['models'], [])

    def test_invalid_json_uses_bounded_protocol_repair(self):
        original = self.client.generate
        attempts = []
        def invalid(*args, **kwargs):
            result = original(*args, **kwargs)
            result['content'] = 'Unstructured commentary'
            attempts.append(result)
            return result
        self.client.generate = invalid
        result = self.ask()
        self.assertFalse(result['result']['admission']['admitted'])
        self.assertNotIn('blocked', result['result'])
        self.assertEqual(len(attempts), 3, 'Malformed replies use only the protocol repair allocation')
        self.assertEqual(usage(self.ledger, result['session'])['used']['calls'], 3)
        self.assertTrue(self.ledger.verify_invocations(result['session']))

    def test_artifact_change_before_dispatch_does_not_activate(self):
        provider = EngineModel(self.root / 'calls', self.models['low'])
        (self.root / 'model.gguf').write_bytes(b'changed artifact')
        with self.assertRaisesRegex(InvalidUpdate, 'artifacts changed'):
            provider.fresh_context_metadata()
        self.assertEqual(self.calls, [])

    def test_drained_timeout_settles_actual_usage_without_admitting_a_partial_answer(self):
        def timed_out(*args, **kwargs):
            error = TimeoutError('Native generation reached its time slice')
            error.engine_usage = {'prompt_tokens': 124, 'completion_tokens': 11,
                                  'finish_reason': 'cancelled', 'elapsed_s': 150}
            raise error
        self.client.generate = timed_out
        result = self.ask()
        receipt = result['result']
        self.assertFalse(receipt['admission']['admitted'])
        self.assertEqual(receipt['failure_kind'], 'timeout')
        self.assertEqual(usage(self.ledger, result['session'])['used']['calls'], 1)
        self.assertEqual(usage(self.ledger, result['session'])['used']['tokens'], 135)
        self.assertEqual(receipt['token_usage']['totals']['input_tokens'], 124)
        self.assertEqual(receipt['token_usage']['totals']['output_tokens'], 11)
        self.assertTrue(self.ledger.verify_invocations(result['session']))


if __name__ == '__main__': unittest.main()
