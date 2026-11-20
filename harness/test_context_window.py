"""Exact window measurements neither reprice reservations nor authorize output."""
import copy
import json
import unittest
from unittest.mock import patch

from context_manager import ContextBudgetExceeded, InvalidUpdate, canonical, sha
from invocation_gateway import _permit, request_hash
from monitoring import ResourceWait
from paging import Budget
import context_window as window
import conversation
from strata_model import StrataModel
from test_context_window_recovery import CountService
from test_strata_model import BenchmarkServer, PROMPT_VALIDATION, StrataTests


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.service = CountService()
        self.profile = self.service.descriptor()
        self.budget = Budget(4096, 512, 479, context_profile=2,
                             reservation_limit=20000, context_window=self.profile)
        self.rpc = patch.object(window, '_rpc', self.service.rpc)
        self.rpc.start(); self.addCleanup(self.rpc.stop)
        window._counts.clear()

    def test_legacy_profile_serialization_and_financial_charge_stay_unchanged(self):
        old = Budget(4096, 512, 479)
        self.assertEqual(vars(old), {'context_limit': 4096, 'response_reserve': 512, 'template_reserve': 479})
        payload = {'source': 'x' * 8000}
        charge = self.budget.check('system', payload, {})
        self.assertEqual(charge, old.charge('system', payload, {}))
        self.assertGreater(charge, self.budget.context_limit)
        with self.assertRaises(ContextBudgetExceeded):
            old.check('system', payload, {})
        proof = window.check(self.budget, 'system', payload, {}, thinking=False)
        self.assertEqual(proof['prompt_tokens'], 500)
        self.assertLess(proof['prompt_tokens'] + self.budget.response_reserve, self.budget.context_limit)
        self.assertEqual(vars(Budget(**json.loads(canonical(vars(self.budget))))), vars(self.budget))
        self.assertEqual(self.budget.with_limits(response_reserve=256).reservation_limit, 20000)

    def test_wrong_counts_fail_closed_without_generation(self):
        for count in (None, True, False, -1, 0, '500', 500.0, 2 ** 21 + 1):
            with self.subTest(count=count):
                window._counts.clear(); self.service.prompt_tokens = count
                with self.assertRaises(ResourceWait):
                    window.check(self.budget, 'system', {}, {}, thinking=False)
        self.assertTrue(all(call[2] != '/v1/chat/completions' for call in self.service.calls))

    def test_wrong_server_epoch_model_template_or_window_rejects_before_counting(self):
        base = self.service.rpc
        cases = (('/v1/status', 'started', 112), ('/v1/status', 'model', 'other-model'),
                 ('/props', 'chat_template', 'changed-template'),
                 ('/v1/status', 'context', {'max_positions': 4096}))
        for path, key, value in cases:
            def changed(profile, method, route, body=None):
                result = base(profile, method, route, body)
                if route == path:
                    result[key] = value
                return result
            with self.subTest(key=key), patch.object(window, '_rpc', changed), self.assertRaises(ResourceWait):
                window.check(self.budget, 'system', {}, {}, thinking=False)

    def test_physical_overflow_is_not_a_financial_budget_failure(self):
        self.service.prompt_tokens = 4000
        self.assertLess(self.budget.check('system', {}, {}) + 512, self.budget.reservation_limit)
        with self.assertRaisesRegex(ContextBudgetExceeded, 'physical context') as caught:
            window.check(self.budget, 'system', {}, {}, thinking=False)
        self.assertEqual(caught.exception.physical_context['prompt_tokens'], 4000)

    def test_strata_framing_margin_is_physical_and_cannot_spend_reservation_units(self):
        before = self.budget.check('system', {}, {})
        self.service.prompt_tokens = self.budget.context_limit - self.budget.response_reserve - 6
        with self.assertRaisesRegex(ContextBudgetExceeded, 'Strata framing margin 8'):
            window.check(self.budget, 'system', {}, {}, thinking=False)
        window._counts.clear()
        self.service.prompt_tokens = self.budget.context_limit - self.budget.response_reserve - 9
        proof = window.check(self.budget, 'system', {}, {}, thinking=False)
        self.assertEqual(proof['context_slack_tokens'], 8)
        self.assertEqual(proof['output_reserve'], self.budget.response_reserve)
        self.assertEqual(self.budget.check('system', {}, {}), before)
        window.validate_proof(proof, self.budget, 'system', {}, {})
        with self.assertRaises(InvalidUpdate):
            window.validate_proof(dict(proof, context_slack_tokens=0), self.budget, 'system', {}, {})
        self.assertGreaterEqual(window.runtime_context(self.budget, proof),
                                proof['prompt_tokens'] + self.budget.response_reserve + 8)

    def test_positive_reasoning_and_host_schema_are_counted_as_transported(self):
        payload = {'reasoning': {'preference': 'adaptive', 'requested': 'low'}}
        proof = window.check(self.budget, 'system', payload, {'type': 'object'}, thinking='low')
        counted = [call[3] for call in self.service.calls if call[2] == '/v1/messages/count_tokens'][-1]
        self.assertEqual(counted['output_config'], {'effort': 'low'})
        self.assertNotIn('thinking', counted)
        self.assertIn('OUTPUT FORMAT REQUIREMENT', counted['system'])
        self.assertTrue(counted['system'].endswith(canonical({'type': 'object'})))
        wire, _, _ = window.wire_request('system', payload, {'type': 'object'}, self.profile['model'], 'low', 512)
        self.assertEqual(proof['wire_sha256'], sha(canonical(wire)))

    def test_absent_control_planning_checks_every_supported_template_prefix(self):
        window.check(self.budget, 'system', {}, {}, planning=True)
        requests = [call[3] for call in self.service.calls if call[2] == '/v1/messages/count_tokens']
        self.assertEqual(len(requests), 5)
        self.assertEqual(requests[0]['thinking'], {'type': 'disabled'})
        self.assertEqual([item['output_config']['effort'] for item in requests[1:4]], ['low', 'medium', 'high'])
        self.assertNotIn('thinking', requests[4])
        self.assertNotIn('output_config', requests[4])

    def test_proof_is_bound_to_body_reserve_epoch_and_physical_count(self):
        proof = window.check(self.budget, 'system', {}, {}, thinking=False)
        identity = {'server_model': self.profile['model'], 'server_started': self.profile['started'],
                    'server_context_tokens': self.profile['context_tokens']}
        window.validate_proof(proof, self.budget, 'system', {}, {}, identity)
        with self.assertRaisesRegex(InvalidUpdate, 'differs from its invocation'):
            window.validate_proof(proof, self.budget, 'changed-system', {}, {}, identity)
        for changed in (dict(identity, server_started=112), dict(identity, server_context_tokens=4096)):
            with self.assertRaises(InvalidUpdate):
                window.validate_proof(proof, self.budget, 'system', {}, {}, changed)
        with self.assertRaises(InvalidUpdate):
            window.validate_proof(proof, self.budget.with_limits(response_reserve=256), 'system', {}, {}, identity)
        for value in (True, -1, 0, '500'):
            with self.subTest(value=value), self.assertRaises(InvalidUpdate):
                window.validate_proof(dict(proof, prompt_tokens=value), self.budget, 'system', {}, {}, identity)

    def test_counter_discovery_is_opt_in_and_unavailable_probe_stays_conservative(self):
        state = self.service.rpc(self.profile, 'GET', '/v1/status')
        # Discovery fetches props before it can build the complete descriptor.
        def discovery_rpc(profile, method, path, body=None):
            return self.service.rpc(dict(self.profile, **profile), method, path, body)
        with patch.object(window, '_rpc', discovery_rpc):
            self.assertEqual(window.discover(self.profile['port'], state), self.profile)
            self.service.fail_counts = True
            self.assertIsNone(window.discover(self.profile['port'], state))
        self.assertIsNone(window.discover(self.profile['port'], {}))
        self.assertIsNone(window.discover(self.profile['port'], None))

    def test_profile_validation_rejects_nonintegers_and_window_expansion(self):
        for profile in (dict(self.profile, version=True), dict(self.profile, port=True),
                        dict(self.profile, started=True), dict(self.profile, template_sha256='not-a-hash')):
            with self.subTest(profile=profile), self.assertRaises(InvalidUpdate):
                Budget(4096, 512, context_profile=2, reservation_limit=20000, context_window=profile)
        for limit in (True, None, -1, 0, 20000.0):
            with self.subTest(limit=limit), self.assertRaises(InvalidUpdate):
                Budget(4096, 512, context_profile=2, reservation_limit=limit, context_window=self.profile)
        with self.assertRaises(InvalidUpdate):
            Budget(16384, 512, context_profile=2, reservation_limit=20000, context_window=self.profile)


class CountingServer(BenchmarkServer):
    def do_GET(self):
        if self.path != '/props':
            return super().do_GET()
        value = {'chat_template': CountService.template, 'model_alias': self.server.status['model'],
                 'default_generation_settings': {'n_ctx': self.server.status['context']['max_positions']}}
        raw = json.dumps(value).encode(); self.send_response(200)
        self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def do_POST(self):
        if self.path != '/v1/messages/count_tokens':
            return super().do_POST()
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.count_requests.append(request)
        raw = json.dumps({'input_tokens': self.server.count_tokens}).encode(); self.send_response(200)
        self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)


class ExactTransportTests(unittest.TestCase):
    setUp = StrataTests.setUp
    ask = StrataTests.ask

    def invoke(self, tokens):
        self.server.RequestHandlerClass = CountingServer
        self.server.status['structured_output'] = dict(PROMPT_VALIDATION)
        self.server.count_requests = []; self.server.count_tokens = tokens
        self.ask()
        raw, delivered = self.ledger.db.execute('SELECT i.record_json,d.payload_json FROM invocations i '
            'JOIN deliveries d ON d.id=i.delivery_id ORDER BY i.rowid DESC LIMIT 1').fetchone()
        envelope, payload = json.loads(raw), json.loads(delivered)
        provider = StrataModel(self.root / 'calls')
        profile = provider.context_metadata['_sutra_context_window']
        provider.budget = Budget(provider.context_cap, 2048, provider.budget.template_reserve,
                                 context_profile=2, reservation_limit=40000, context_window=profile)
        window._counts.clear()
        provider.context_measurement = window.check(provider.budget, conversation.SYSTEM, payload,
            envelope['schema'], thinking=False)
        provider.runtime_context_tokens = window.runtime_context(provider.budget, provider.context_measurement)
        from model_packet import build
        packet = build(conversation.SYSTEM, payload, envelope['schema'])
        permit = _permit.set({'used': False, 'hash': request_hash('exact-http', provider.model,
            conversation.SYSTEM, payload, envelope['schema']), 'packet_hash': sha(canonical(packet))})
        try:
            return provider.call('exact-http', conversation.SYSTEM, payload, envelope['schema'])
        finally:
            _permit.reset(permit)

    def test_matching_physical_count_uses_the_exact_http_body_and_usage(self):
        answer, record = self.invoke(124)
        self.assertTrue(answer['answer'])
        self.assertEqual(record['physical_context']['prompt_tokens'], 124)
        self.assertEqual(record['physical_context']['wire_sha256'], sha(canonical(self.server.posts[-1][0])))
        self.assertEqual(record['response']['prompt_eval_count'], 124)

    def test_completed_usage_drift_is_quarantined_even_inside_rounding_margin(self):
        with self.assertRaisesRegex(RuntimeError, 'differs from its exact physical context measurement') as caught:
            self.invoke(500)
        self.assertEqual(caught.exception.call_record['response']['prompt_eval_count'], 124)
        self.assertEqual(caught.exception.call_record['response']['eval_count'], 30)


if __name__ == '__main__':
    unittest.main()
