"""Physical counting cannot reprice history or change task authority.

The count-only service and transport are deterministic fixtures. These tests
exercise integration and recovery, not tokenizer accuracy or actual RAM use.
"""
import copy
import json
import unittest
from unittest.mock import patch

from context_manager import ContextBudgetExceeded, InvalidUpdate, canonical, sha
from invocation_gateway import InvocationLedger, consume_dispatch
from model_identity import unknown
from model_packet import build, descriptor, host_fields
from paging import Budget
import context_window as window
import monitoring
import orchestration as machine
import review_budget
import runtime_memory
import strata_model
import test_orchestration as fixtures
import test_paging_recovery_regression as recovery_fixtures


class CountService:
    template = 'fixture chat template with explicit generation framing'

    def __init__(self):
        self.calls = []
        self.fail_counts = False
        self.prompt_tokens = 500

    def descriptor(self, model='fixture-native', port=19600):
        return {'version': 1, 'provider': 'strata', 'port': port, 'model': model,
                'started': 111, 'context_tokens': 8192,
                'template_sha256': sha(self.template)}

    def rpc(self, profile, method, path, body=None):
        self.calls.append((copy.deepcopy(profile), method, path, copy.deepcopy(body)))
        if (method, path) == ('GET', '/v1/status'):
            return {'service': 'strata', 'loaded': True, 'model': profile['model'],
                    'started': profile['started'], 'context': {'max_positions': profile['context_tokens']},
                    'structured_output': {'method': 'prompt_and_validate', 'constrained_decoding': False,
                                          'stream_buffered': True, 'formats': ['json_schema']}}
        if (method, path) == ('GET', '/props'):
            return {'chat_template': self.template, 'model_alias': profile['model'],
                    'default_generation_settings': {'n_ctx': profile['context_tokens']}}
        if (method, path) == ('POST', '/v1/messages/count_tokens'):
            if self.fail_counts:
                raise OSError('Exact counter fixture is unavailable')
            return {'input_tokens': self.prompt_tokens}
        raise AssertionError('Unexpected count-only operation: ' + method + ' ' + path)


class ExactChild(fixtures.Child):
    model_interface = 1

    def __init__(self, model, profile, fault=None):
        super().__init__(model, fault)
        self.budget = Budget(4096, 1024, 256, context_profile=2,
                             reservation_limit=65536, context_window=profile)
        self.model_identity = dict(unknown(model), server_model=profile['model'],
                                   server_started=profile['started'],
                                   server_context_tokens=profile['context_tokens'])

    def call(self, cid, system, payload, schema):
        packet = build(system, payload, schema)
        with patch('test_chat.consume_dispatch',
                   side_effect=lambda *args: consume_dispatch(*args, packet=packet)):
            result, record = super().call(cid, system, payload, schema)
        for key in host_fields(packet['role'], payload, schema, packet['version'], result):
            result.pop(key, None)
        if packet['version'] in (2, 3):
            choices = schema['properties']['citations']['items']['enum']
            result['citations'] = [choices.index(item) + 1 for item in result['citations']]
        record['response'] = {'done': True, 'prompt_eval_count': 500, 'eval_count': 80}
        record['provider'] = 'strata'
        return result, record


class ContextWindowRecoveryTests(unittest.TestCase):
    setUp = fixtures.OrchestrationTests.setUp
    tearDown = fixtures.OrchestrationTests.tearDown
    step = fixtures.OrchestrationTests.step
    until = fixtures.OrchestrationTests.until
    children = fixtures.OrchestrationTests.children
    provider = fixtures.OrchestrationTests.provider
    legacy_checkpoint = recovery_fixtures.PagingRecoveryRegressionTests.legacy_checkpoint
    prior_finding = recovery_fixtures.PagingRecoveryRegressionTests.prior_finding

    def exact_provider(self, service, *, fault=None, native_model='fixture-native', port=19600):
        model = ExactChild('strata:profile/test-high', service.descriptor(native_model, port), fault)
        self.exact_model = model
        # Only this test's configured High route uses the native fixture.
        self.model_patch = patch.dict(fixtures.MODELS, high=model.model)
        self.model_patch.start()
        self.addCleanup(self.model_patch.stop)
        previous = self.provider
        self.provider = lambda stage: model if stage == 'subagent' else previous(stage)
        self.tier = 'high'
        return model

    def test_legacy_receipt_then_exact_window_model_switch_keeps_reservations_and_pins(self):
        out, branch, _, _, old_id, old_raw = self.legacy_checkpoint()
        sid = self.meta['active']
        contract = self.s.pinned(sid)
        node = machine.node(self.s, branch)
        before = review_budget.usage(self.s, sid)
        old_envelope = json.loads(self.s.db.execute('SELECT record_json FROM invocations WHERE id=?',
                                                  (old_id,)).fetchone()[0])
        self.assertEqual(set(old_envelope['budget_profile']),
                         {'context_limit', 'response_reserve', 'template_reserve'})
        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        service = CountService()
        exact = self.exact_provider(service)
        with patch.object(window, '_rpc', service.rpc):
            reviewed = self.step(out)
        self.assertTrue(reviewed['child']['result']['admission']['admitted'], reviewed)
        packet = exact.payloads[0]
        iid = reviewed['child']['result']['invocation_id']
        envelope = json.loads(self.s.db.execute('SELECT record_json FROM invocations WHERE id=?', (iid,)).fetchone()[0])
        charged = descriptor(build(__import__('subagent_runtime').SYSTEM, packet,
                                   envelope['schema']), exact.budget.template_reserve)['input_reservation']
        self.assertGreater(charged, exact.budget.context_limit)
        self.assertEqual(envelope['charge'], charged)
        self.assertEqual(envelope['budget_profile'], vars(exact.budget))
        self.assertEqual(packet['contract'], contract)
        self.assertEqual(machine.node(self.s, branch), node)
        self.assertIn('CED724', canonical(packet['memory']))
        self.assertFalse(contract['constraints']['edits_allowed'])
        self.assertFalse(contract['constraints']['network_allowed'])
        self.assertFalse(contract['constraints']['dependencies_allowed'])
        self.assertEqual(review_budget.usage(self.s, sid)['used']['tokens'], before['used']['tokens'] + 580)
        self.assertEqual(self.s.db.execute('SELECT record_json FROM invocation_receipts WHERE invocation_id=?',
                                         (old_id,)).fetchone()[0], old_raw)
        self.assertTrue(any(call[2] == '/v1/messages/count_tokens' for call in service.calls))
        audit_rows = self.s.db.execute('SELECT COUNT(*) FROM page_audit').fetchone()[0]
        hardware = {'profile': 'hardware-v1', 'available': True, 'admitted': True,
                    'decision': 'ALLOW', 'reclaimable_estimate_gib': 8, 'sample': {'cpu_busy_pct': 0}}
        with patch.object(monitoring, 'resource_state', return_value=hardware), \
                patch.object(runtime_memory, 'snapshot', return_value={'enabled': False}):
            view = monitoring.dashboard(self.root)
        self.assertEqual(view['context']['context_profile'], 2)
        self.assertEqual(view['context']['reservation_limit'], 65536)
        self.assertGreater(view['context']['conservative_input_charge'], view['context']['limit'])
        self.assertEqual(view['context']['latest_physical_window']['prompt_tokens'], 500)
        self.assertEqual(view['context']['latest_physical_window']['context_tokens'], 4096)
        self.assertEqual(view['context']['latest_physical_window']['model'], exact.model)
        self.assertEqual(view['context']['latest_physical_window']['role'], 'subagent')
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM page_audit').fetchone()[0], audit_rows)
        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        with patch.object(window, '_rpc', side_effect=OSError('Counter intentionally offline for history read')):
            self.assertTrue(self.s.load_result(sid, old_id))
            self.assertTrue(self.s.load_result(sid, iid))
            self.assertTrue(self.s.verify_invocations(sid))

    def test_exact_counter_failure_pauses_without_charging_then_reload_switch_resumes(self):
        out, branch, _, _, old_id, old_raw = self.legacy_checkpoint()
        service = CountService()

        def continuing(result, payload):
            result.update(slice_status='continue',
                          remaining=['Inspect part0.rivet_detail_0 next.'])

        exact = self.exact_provider(service, fault=continuing)
        with patch.object(window, '_rpc', service.rpc):
            saved = self.step(out)
        self.assertTrue(saved['child']['result']['admission']['admitted'], saved)
        exact_id = saved['child']['result']['invocation_id']
        sid = self.meta['active']
        node = machine.node(self.s, branch)
        before = review_budget.usage(self.s, sid)
        calls = self.s.db.execute('SELECT COUNT(*) FROM invocations WHERE session=?', (sid,)).fetchone()[0]
        service.fail_counts = True
        window._counts.clear()
        with patch.object(window, '_rpc', service.rpc):
            stopped = self.step(saved)
        self.assertEqual(stopped['state'], 'PAUSED')
        self.assertEqual(stopped['stop']['status'], 'resource_wait')
        self.assertEqual(len(exact.payloads), 1)
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM invocations WHERE session=?', (sid,)).fetchone()[0], calls)
        with patch.object(window, '_rpc', side_effect=OSError('No live count on receipt loading')):
            self.assertTrue(self.s.load_result(sid, old_id))
            self.assertTrue(self.s.load_result(sid, exact_id))
        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        replacement = ExactChild('strata:profile/test-alt', service.descriptor('fixture-alternate', 19601))
        alternate_patch = patch.dict(fixtures.MODELS, high=replacement.model)
        alternate_patch.start()
        self.addCleanup(alternate_patch.stop)
        previous = self.provider
        self.provider = lambda stage: replacement if stage == 'subagent' else previous(stage)
        service.fail_counts = False
        with patch('runtime_memory.recover', return_value=None), \
                patch('monitoring.require_resources', return_value={'admitted': True, 'decision': 'ALLOW'}):
            resumed = self.step(stopped, event='resume')
        with patch.object(window, '_rpc', service.rpc):
            reviewed = self.step(resumed)
        self.assertTrue(reviewed['child']['result']['admission']['admitted'], reviewed)
        self.assertEqual(len(replacement.payloads), 1)
        self.assertEqual(machine.node(self.s, branch), node)
        self.assertEqual(machine.state(self.s, branch), 'DONE')
        self.assertEqual(review_budget.usage(self.s, sid)['used']['tokens'], before['used']['tokens'] + 580)
        self.assertEqual(self.s.db.execute('SELECT record_json FROM invocation_receipts WHERE invocation_id=?',
                                         (old_id,)).fetchone()[0], old_raw)
        self.assertTrue(any(call[0].get('model') == 'fixture-alternate' and call[2] == '/v1/messages/count_tokens'
                            for call in service.calls))
        self.assertTrue(self.s.verify_invocations(sid))

    def test_runtime_window_uses_exact_tokens_while_financial_check_keeps_byte_charge(self):
        service = CountService()
        budget = Budget(4096, 1024, 256 + strata_model.HOST_JSON_DIRECTIVE_BYTES, context_profile=2,
                        reservation_limit=20000, context_window=service.descriptor())
        payload = {'text': 'Fresh immutable source data. ' * 250}
        state = service.rpc(service.descriptor(), 'GET', '/v1/status')
        state['activity'] = {'in_flight': 0}
        provider = strata_model.StrataModel.__new__(strata_model.StrataModel)
        provider.budget, provider.thinking, provider.native_profile = budget, None, None
        provider.model, provider.port = 'strata:high', service.descriptor()['port']
        provider.server_identity = strata_model._identity(state, provider.model)
        financial = budget.check('Fixture system.', payload, {'type': 'object'})
        self.assertGreater(financial, budget.context_limit)
        hardware = {'decision': 'ALLOW', 'sample': {'free_bytes': 8 * 1024**3, 'inactive_bytes': 0}}
        with patch.object(window, '_rpc', service.rpc), \
                patch.object(strata_model, 'status', return_value=state), \
                patch.object(runtime_memory, 'enabled', return_value=False), \
                patch.object(runtime_memory, 'fresh_resources', return_value=hardware), \
                patch.object(runtime_memory, 'note_native_plan'):
            with provider.dispatch_gate(self.root, 'fixture', 'Fixture system.', payload, {'type': 'object'}) as plan:
                physical = provider.runtime_context_tokens
                self.assertEqual(plan['context_tokens'], physical)
        self.assertGreaterEqual(physical, service.prompt_tokens + budget.response_reserve)
        self.assertLess(physical, budget.context_limit)
        self.assertLess(physical, financial + budget.response_reserve)
        self.assertEqual(budget.check('Fixture system.', payload, {'type': 'object'}), financial)
        limited = budget.with_limits(reservation_limit=financial + budget.response_reserve - 1)
        with self.assertRaises(ContextBudgetExceeded):
            limited.check('Fixture system.', payload, {'type': 'object'})
        with patch.object(window, '_rpc', service.rpc):
            proof = window.check(limited, 'Fixture system.', payload, {'type': 'object'})
        self.assertEqual(proof['capacity']['decision'], 'ALLOW')

    def test_same_named_server_restart_cannot_register_a_stale_physical_proof(self):
        out, _, _, _, _, _ = self.legacy_checkpoint()
        service = CountService()
        exact = self.exact_provider(service)
        exact.model_identity['server_started'] += 1
        before = review_budget.usage(self.s, self.meta['active'])
        count = self.s.db.execute('SELECT COUNT(*) FROM invocations').fetchone()[0]
        with patch.object(window, '_rpc', service.rpc), \
                self.assertRaisesRegex(InvalidUpdate, 'physical context proof|server identity'):
            self.step(out)
        self.assertEqual(exact.payloads, [])
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM invocations').fetchone()[0], count)
        self.assertEqual(review_budget.usage(self.s, self.meta['active']), before)


if __name__ == '__main__':
    unittest.main()
