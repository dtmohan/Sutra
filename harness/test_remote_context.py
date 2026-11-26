"""Exact native counts separate physical fit from charged task reservations."""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from context_manager import ContextBudgetExceeded, InvalidUpdate, canonical, sha
from invocation_gateway import InvocationGateway, InvocationLedger, consume_dispatch
from model_packet import build, descriptor, host_fields
from monitoring import ResourceWait
from paging import Budget
import context_window
import chat_runtime
import model_context
import orchestration as machine
import remote_context
import remote_profiles
import provider_resources
import review_budget
import review_context
from test_remote_profiles import descriptor as remote_descriptor
import test_orchestration as fixtures
import test_paging_recovery_regression as recovery


class RemoteCounter:
    template = 'Exact pinned native chat template.'

    def __init__(self):
        self.calls = []
        self.tokens = 500
        self.fail = False

    def server(self, profile):
        return {'props': {'chat_template': self.template,
                          'default_generation_settings': {'n_ctx': profile['context_tokens'],
                              'generation_prompt': '<assistant><think>', 'reasoning_format': 'deepseek'}}}

    def rpc(self, profile, body):
        self.calls.append((copy.deepcopy(profile), copy.deepcopy(body)))
        if self.fail:
            raise TimeoutError('Counter intentionally unavailable')
        return self.tokens


class RemoteWindowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.registry = self.root / 'registry.json'
        self.profile = remote_profiles.write({'test': remote_descriptor(self.root, identifier='test',
            context_tokens=16384, executable_sha256='b' * 64)}, self.registry)['test']
        env = patch.dict(os.environ, SUTRA_REMOTE_REGISTRY=str(self.registry),
                         SUTRA_REMOTE_MODELS='["sutra:remote/test"]')
        env.start(); self.addCleanup(env.stop)
        self.service = RemoteCounter()
        for mocked in (patch('remote_control.verify_server', self.service.server),
                       patch('remote_control.ensure_tunnel', return_value={'ready': True}),
                       patch.object(remote_context, '_rpc', self.service.rpc)):
            mocked.start(); self.addCleanup(mocked.stop)
        self.window = remote_context.discover(self.profile)
        self.assertIsNotNone(self.window)
        self.budget = Budget(16384, 4096, 256, context_profile=2,
                             reservation_limit=65536, context_window=self.window)

    def test_twenty_kilobyte_reservation_can_fit_exact_sixteen_k_window_without_repricing(self):
        payload = {'request': 'Bounded source: ' + 'unchanged source evidence. ' * 900}
        old = Budget(16384, 4096, 256)
        charge = self.budget.check('Review the supplied evidence.', payload, {'type': 'object'})
        self.assertGreater(charge + self.budget.response_reserve, 16384)
        with self.assertRaises(ContextBudgetExceeded):
            old.check('Review the supplied evidence.', payload, {'type': 'object'})
        proof = context_window.check(self.budget, 'Review the supplied evidence.', payload, {'type': 'object'})
        self.assertEqual(proof['prompt_tokens'], 500)
        self.assertEqual(proof['basis'], remote_context.BASIS)
        self.assertEqual(charge, old.charge('Review the supplied evidence.', payload, {'type': 'object'}))
        wire, _, _ = remote_context.wire_request(self.profile, 'Review the supplied evidence.', payload,
                                                {'type': 'object'}, 4096)
        self.assertEqual(self.service.calls[-1][1], wire)
        self.assertFalse(any('/v1/chat/completions' == item[0].get('path') for item in self.service.calls))

    def test_allocation_keeps_physical_limit_and_remaining_financial_share_separate(self):
        metadata = {'model_info': {'remote_cuda.context_length': 16384},
                    '_sutra_provider': 'sutra_remote', '_sutra_context_window': self.window}
        provider = SimpleNamespace(context_metadata=metadata)
        budget, _ = model_context.allocation(metadata)
        self.assertEqual(budget.context_profile, 2)
        provider.budget = budget
        usage = {'limits': {'tokens': 100000}, 'used': {'tokens': 20000}}
        before = copy.deepcopy(usage)
        with patch('review_budget.usage', return_value=usage), \
                patch('orchestration.task', return_value={'id': 'task'}), \
                patch('orchestration.gates', return_value=[usage]):
            allocation = review_context.context_budget(provider, None, 'session', slots=2)
        self.assertEqual(usage, before)
        self.assertEqual(provider.budget.context_limit, 16384)
        self.assertEqual(provider.budget.reservation_limit, 40000)
        self.assertEqual(allocation['available_tokens'], 80000)
        self.assertEqual(allocation['reservation_limit_units'], 40000)

    def test_actual_physical_overflow_is_distinct_and_preserves_financial_charge(self):
        self.service.tokens = 16000
        charge = self.budget.check('system', {}, {})
        with self.assertRaisesRegex(ContextBudgetExceeded, 'physical context') as caught:
            context_window.check(self.budget, 'system', {}, {})
        self.assertEqual(caught.exception.physical_context['prompt_tokens'], 16000)
        self.assertEqual(self.budget.check('system', {}, {}), charge)

    def test_runtime_context_uses_remote_proof_slack_at_a_rounding_boundary(self):
        self.service.tokens = 3832  # 8192 after 4096 output, eight slack and 256 runtime headroom.
        current = context_window.check(self.budget, 'system', {}, {})
        self.assertEqual(context_window.runtime_context(self.budget, current), 8192)

        with patch.object(remote_context, 'CONTEXT_SLACK', 17):
            proof = context_window.check(self.budget, 'system', {}, {})
            context_window.validate_proof(proof, self.budget, 'system', {}, {})
            self.assertEqual(proof['context_slack_tokens'], 17)
            self.assertEqual(context_window.runtime_context(self.budget, proof), 9216)

    def test_pre_source_probe_counts_with_legal_schema_but_cannot_authorize_dispatch(self):
        schema = {'type': 'object', 'properties': {'citations': {'type': 'array', 'minItems': 1,
            'items': {'type': 'integer', 'enum': []}}}, 'required': ['citations']}
        original = copy.deepcopy(schema)

        def native_count(profile, wire):
            if remote_context._empty_enum(wire['response_format']['json_schema']['schema']):
                raise ValueError('The native grammar cannot compile an empty enum')
            return 500

        with patch.object(remote_context, '_rpc', native_count):
            proof = context_window.check(self.budget, 'system', {}, schema, planning=True)
            self.assertEqual(proof['prompt_tokens'], 500)
            with self.assertRaises(ResourceWait):
                context_window.check(self.budget, 'system', {}, schema)
        self.assertEqual(schema, original)
        with self.assertRaisesRegex(InvalidUpdate, 'differs from its invocation'):
            context_window.validate_proof(proof, self.budget, 'system', {}, schema)

    def test_parent_synthesis_retains_large_pins_when_exact_sixteen_k_window_fits(self):
        # This is the saved-review failure class: enough parent money remains,
        # but mandatory UTF-8 context alone exceeds the physical token number.
        pin = 'Keep every supplied requirement unchanged. ' * 550
        memory = {'pinned_user_statements': [{'quote': pin}],
                  'recent_turns': [{'user': 'Explain the architecture. Read only. No network.'}]}
        contract = {'task_id': 'saved-review', 'goal': 'Explain the architecture.',
                    'constraints': {'allowed_paths': ['.'], 'edits_allowed': False, 'network_allowed': False}}
        from review_role import constraint_rows
        packet = {'contract': copy.deepcopy(contract), 'request': contract['goal'],
                  'constraint_echo': constraint_rows(contract), 'parent_context_version': 3,
                  'report_schema_version': 2}
        provider = SimpleNamespace(model=self.profile['name'], budget=self.budget,
            context_metadata={'model_info': {'remote_cuda.context_length': 16384},
                              '_sutra_provider': 'sutra_remote', '_sutra_context_window': self.window})
        usage = {'limits': {'tokens': 500000}, 'used': {'tokens': 19470}}
        before = copy.deepcopy(usage)
        source = {'sources': {'entry.py': 'def main():\n    return run()\n'},
                  'graph': {'nodes': [], 'calls': [], 'diagnostics': [], 'module_dependencies': []}}
        with patch('review_budget.usage', return_value=usage), \
                patch('orchestration.task', return_value={'id': 'saved-review'}), \
                patch('orchestration.gates', return_value=[usage]), \
                patch.object(chat_runtime, 'conversation_pages', return_value=copy.deepcopy(memory)):
            schema, _ = chat_runtime.fit_packet(None, 'session', packet, source, provider, real=True)
        charge = provider.budget.check(chat_runtime.SYSTEM, packet, schema)
        proof = context_window.check(provider.budget, chat_runtime.SYSTEM, packet, schema)
        self.assertGreater(charge + provider.budget.response_reserve, 16384)
        self.assertLess(proof['prompt_tokens'] + provider.budget.response_reserve, 16384)
        self.assertEqual(packet['memory'], memory)
        self.assertEqual(packet['contract'], contract)
        self.assertEqual(usage, before)
        self.assertEqual(provider.budget.reservation_limit, 480530 // 2)
        self.assertEqual(packet['excerpts']['entry.py'], source['sources']['entry.py'])

    def test_counter_failure_or_changed_template_never_falls_back_to_an_estimate(self):
        self.service.fail = True
        with self.assertRaises(ResourceWait) as stopped:
            context_window.check(self.budget, 'system', {}, {})
        self.assertEqual(stopped.exception.state['model'], self.profile['name'])
        self.assertEqual(stopped.exception.state['runtime']['model'], self.profile['name'])
        self.assertEqual(stopped.exception.state['runtime']['stage'], 'remote_token_count')
        self.assertIs(stopped.exception.state['runtime']['inference_started'], False)
        self.assertEqual(stopped.exception.category, 'measurement_unavailable')
        self.service.fail = False
        self.service.template = 'Another tokenizer template.'
        with self.assertRaises(ResourceWait):
            context_window.check(self.budget, 'system', {}, {})

    def test_proof_replays_offline_and_rejects_changed_route_reserve_or_payload(self):
        proof = context_window.check(self.budget, 'system', {}, {})
        identity = remote_profiles.identity(self.profile['name'], self.profile)
        wire = self.service.calls[-1][1]
        self.assertIs(wire['cache_prompt'], False)
        self.assertEqual(proof['wire_sha256'], sha(canonical(wire)))
        with patch.object(remote_context, '_identity', side_effect=AssertionError('Offline replay made a network call')):
            context_window.validate_proof(proof, self.budget, 'system', {}, {}, identity)
            unproved_cache_wire = dict(wire, cache_prompt=True)
            changed = dict(proof, wire_sha256=sha(canonical(unproved_cache_wire)))
            with self.assertRaisesRegex(InvalidUpdate, 'differs from its invocation'):
                context_window.validate_proof(changed, self.budget, 'system', {}, {}, identity)
        for budget, system, payload, model in (
                (self.budget.with_limits(response_reserve=2048), 'system', {}, identity),
                (self.budget, 'changed system', {}, identity),
                (self.budget, 'system', {'request': 'changed'}, identity),
                (self.budget, 'system', {}, dict(identity, expected_model_sha256='c' * 64)),
                (self.budget, 'system', {}, dict(identity, remote_profile_hash='c' * 64))):
            with self.subTest(model=model), self.assertRaises(InvalidUpdate):
                context_window.validate_proof(proof, budget, system, payload, {}, model)

    def test_profile_switch_cannot_reuse_prior_count_or_proof(self):
        first = context_window.check(self.budget, 'system', {}, {})
        alternate = remote_profiles.normalize(dict(self.profile, model='new-model', model_sha256='c' * 64,
                                                    profile_hash=None))
        remote_profiles.write({'test': alternate}, self.registry)
        replacement = remote_context.discover(alternate)
        budget = Budget(16384, 4096, context_profile=2, reservation_limit=65536,
                        context_window=replacement)
        count_before = len(self.service.calls)
        second = context_window.check(budget, 'system', {}, {})
        self.assertEqual(len(self.service.calls), count_before + 1)
        self.assertNotEqual(first['window'], second['window'])
        with self.assertRaises(InvalidUpdate):
            context_window.validate_proof(first, budget, 'system', {}, {})

    def test_section_roles_bind_native_reasoning_caps_to_exact_count_and_proof(self):
        from invocation_roles import expected_request

        window = dict(self.window, **remote_context.PARENT_REASONING_IDENTITY)
        for role, reserve, cap in (('section_writer', 2048, 768),
                                   ('section_writer', 4096, 1024),
                                   ('section_reviewer', 1024, 384),
                                   ('section_reviewer', 512, 192)):
            with self.subTest(role=role, reserve=reserve):
                budget = Budget(16384, reserve, context_profile=2, reservation_limit=65536,
                                context_window=window)
                payload = {'request': 'Review the example.', 'section': 'Overview', 'section_role': role,
                           'section_plan': 'a' * 64, 'attempt': 0, 'memory_budget': vars(budget),
                           'evidence': {'a': {'path': 'example.py', 'text': 'def run(): return 1'}}}
                claim = {'text': 'The function returns one.', 'sources': ['a']}
                if reserve == 512:
                    payload.update(section_version=2, claim_index=0, claim=claim)
                elif reserve == 4096:
                    payload.update(section_version=3, requirements={}, pinned=[],
                                   section_sources=[['a'], ['a'], ['a']])
                elif role == 'section_reviewer':
                    payload['claims'] = [claim]
                system, spec = expected_request(role, payload)
                remote_context.require_section_reasoning(budget, role, window['reference'], payload)
                with patch.object(remote_context, '_identity', return_value=self.profile) as identity:
                    proof = context_window.check(budget, system, payload, spec)
                self.assertEqual(identity.call_count, 2)
                identity.assert_called_with(window, parent_reasoning=True)
                wire = self.service.calls[-1][1]
                self.assertEqual((wire['reasoning_budget_tokens'], wire['max_tokens']), (cap, reserve))
                self.assertEqual(proof['reasoning_budget_tokens'], cap)
                self.assertEqual(proof['wire_sha256'], sha(canonical(wire)))
                self.assertFalse({'section_plan', 'section_role', 'memory_budget', 'claim_index'} &
                                 set(json.loads(wire['messages'][-1]['content'])))
                with patch.object(remote_context, '_identity',
                                  side_effect=AssertionError('Proof replay must be offline')):
                    context_window.validate_proof(proof, budget, system, payload, spec)
                    with self.assertRaises(InvalidUpdate):
                        context_window.validate_proof(dict(proof, reasoning_budget_tokens=cap + 1),
                                                      budget, system, payload, spec)
                with self.assertRaises(InvalidUpdate):
                    remote_context.wire_request(window, system, dict(payload, section_role=None), spec, reserve)
                for incompatible, model in ((self.budget, window['reference']),
                                            (budget.with_limits(response_reserve=cap), window['reference']),
                                            (budget, 'sutra:remote/other')):
                    with self.assertRaises(ResourceWait) as caught:
                        remote_context.require_section_reasoning(incompatible, role, model, payload)
                    self.assertIs(caught.exception.state['runtime']['inference_started'], False)

    def test_gateway_uses_claim_reasoning_cap_at_both_admission_checks(self):
        from invocation_roles import expected_request

        window = dict(self.window, **remote_context.PARENT_REASONING_IDENTITY)
        budget = Budget(16384, 512, context_profile=2, reservation_limit=65536,
                        context_window=window)
        role = 'section_reviewer'
        payload = {'section_role': role, 'section_version': 2, 'claim_index': 0,
                   'claim': {'text': 'The function returns one.', 'sources': ['a']},
                   'evidence': {'a': {'path': 'example.py', 'text': 'def run(): return 1'}}}
        system, spec = expected_request(role, payload)
        provider = SimpleNamespace(budget=budget, model=window['reference'])
        gateway = InvocationGateway('unused.sqlite', 'run', provider)

        class ReachedNextCheck(Exception):
            pass

        with patch('reasoning_policy.check_provider', side_effect=ReachedNextCheck):
            with self.assertRaises(ReachedNextCheck):
                gateway.invoke(role, 'call', system, payload, spec, 'delivery')
        with patch('invocation_timing.validate', side_effect=ReachedNextCheck):
            with self.assertRaises(ReachedNextCheck):
                InvocationLedger.register(None, 'run', 'call', role, payload, system, spec,
                                          'delivery', provider.model, 1024, budget=vars(budget))

    def test_shared_review_count_replays_with_and_without_verified_native_reasoning(self):
        from invocation_roles import expected_request
        from review_stage import bind

        role = 'section_writer'
        verified = dict(self.window, **remote_context.PARENT_REASONING_IDENTITY)
        for window, mode, reserve, cap in ((verified, 'full', 4096, 1024),
                                           (self.window, 'sectioned', 2048, None)):
            with self.subTest(mode=mode):
                budget = Budget(16384, reserve, context_profile=2, reservation_limit=65536,
                                context_window=window)
                provider = SimpleNamespace(budget=budget, model=window['reference'], context_metadata={})
                payload = {'request': 'Review the example.', 'section_index': 0, 'section_role': role,
                           'section_version': 4, 'section_mode': mode, 'attempt': 0,
                           'section_sources': [['a'], ['a'], ['a']], 'requirements': {}, 'pinned': [],
                           'evidence': {'a': {'path': 'example.py', 'text': 'def run(): return 1'}}}
                bind(provider, role, payload)
                system, spec = expected_request(role, payload)
                with patch.object(remote_context, '_identity', return_value=self.profile) as identity:
                    proof = context_window.check(budget, system, payload, spec)
                self.assertEqual(identity.call_count, 2)
                if cap is None:
                    identity.assert_called_with(window)
                else:
                    identity.assert_called_with(window, parent_reasoning=True)
                wire = self.service.calls[-1][1]
                self.assertEqual(wire.get('reasoning_budget_tokens'), cap)
                self.assertEqual(proof.get('reasoning_budget_tokens'), cap)
                self.assertFalse({'reasoning', 'review_stage', 'section_mode'} &
                                 set(json.loads(wire['messages'][-1]['content'])))
                with patch.object(remote_context, '_identity',
                                  side_effect=AssertionError('Proof replay must be offline')):
                    context_window.validate_proof(proof, budget, system, payload, spec)

    def reviewed_prefix_budget(self):
        import remote_prefix

        template = patch.object(remote_prefix, 'TEMPLATE_SHA256', sha(self.service.template))
        template.start()
        self.addCleanup(template.stop)
        profile = dict(self.profile, executable_sha256=remote_prefix.EXECUTABLE_SHA256)
        profile.pop('profile_hash')
        profile = remote_profiles.write({'test': profile}, self.registry)['test']
        window = remote_context.discover(profile)
        budget = Budget(16384, 4096, 256, context_profile=2,
                        reservation_limit=65536, context_window=window)
        return profile, budget

    def test_prefix_proof_binds_wire_scope_and_replays_with_opt_in_disabled(self):
        import architecture_role
        from test_discovery_selection import LEGACY_FIXTURE

        profile, budget = self.reviewed_prefix_budget()
        window = budget.context_window
        system = architecture_role.SYSTEM
        payload = copy.deepcopy(LEGACY_FIXTURE['payload'])
        spec = architecture_role.schema(payload)
        identity = remote_profiles.identity(profile['name'], profile)
        prefix = {'version': 1, 'scope_sha256': 'a' * 64, 'cache_prompt': True}
        legacy = context_window.check(budget, system, payload, spec)
        legacy_body = self.service.calls[-1][1]
        proof = context_window.check(budget, system, payload, spec, prefix=prefix)
        cached_body = self.service.calls[-1][1]
        self.assertIs(legacy_body['cache_prompt'], False)
        self.assertNotIn('prefix_reuse', legacy)
        self.assertEqual(proof['prefix_reuse'], prefix)
        self.assertIs(cached_body['cache_prompt'], True)
        self.assertEqual(proof['wire_sha256'], sha(canonical(cached_body)))
        self.assertEqual(proof['prompt_tokens'], legacy['prompt_tokens'])
        self.assertEqual(cached_body['response_format'], legacy_body['response_format'])
        self.assertEqual(json.loads(cached_body['messages'][-1]['content']),
                         json.loads(legacy_body['messages'][-1]['content']))
        self.assertNotEqual(cached_body['messages'][0]['content'], legacy_body['messages'][0]['content'])
        with patch.dict(os.environ, SUTRA_REMOTE_REVIEW_PREFIX_REUSE_MODELS='[]'), \
                patch.object(remote_context, '_identity', side_effect=AssertionError('Proof replay must be offline')):
            for stored in (legacy, proof):
                context_window.validate_proof(stored, budget, system, payload, spec, identity)
            for changed in (dict(prefix, scope_sha256='b' * 64), dict(prefix, cache_prompt=False)):
                with self.subTest(prefix=changed), self.assertRaises(InvalidUpdate):
                    context_window.validate_proof(dict(proof, prefix_reuse=changed), budget, system, payload, spec, identity)
        # A model-facing payload cannot select an optional wire projection.
        body, _, _ = remote_context.wire_request(window, system, dict(payload, prefix_reuse=prefix), spec, 4096)
        self.assertEqual(body, legacy_body)

    def test_direct_architect_calibration_rotates_scope_and_preserves_historical_proof(self):
        import architecture_role
        import calibration
        import remote_prefix
        from test_calibration import profile as fixture_calibration
        from test_discovery_selection import LEGACY_FIXTURE
        from vscode_bridge import handle

        profile, budget = self.reviewed_prefix_budget()
        window = budget.context_window
        instance = {'pid': 724, 'boot_id': 'fixture-boot', 'started_at': 'fixture-start',
                    'executable_sha256': window['executable_sha256'], 'model_sha256': window['model_sha256']}
        workspace, storage = self.root / 'repo', self.root / 'store'
        workspace.mkdir()
        (workspace / 'source.py').write_text('def work():\n    return 724\n')
        task = handle(storage, workspace, {'op': 'review_new', 'goal': 'Review the source.', 'roots': ['.']})
        sid = task['session']
        ledger = InvocationLedger(storage / 'state.sqlite')
        self.addCleanup(ledger.close)
        self.assertIsNone(machine.ACTIVE.get(), 'Direct architecture calls have no explicit active scope')
        before = ledger.begin_run(sid, 'Review the source.', [], None)
        first_scope = remote_prefix.binding(ledger, before, window, instance)

        archived = copy.deepcopy(LEGACY_FIXTURE['payload'])
        archived['contract'] = ledger.pinned(sid)
        spec = architecture_role.schema(archived)
        # Existing v1 proofs persist the opaque scope marker supplied by 0.58.0.
        historical = context_window.check(budget, architecture_role.SYSTEM, archived, spec,
            prefix={'version': 1, 'scope_sha256': 'a' * 64, 'cache_prompt': True})

        ref = calibration.register(ledger, fixture_calibration())
        calibration.bind(ledger, sid, [ref], calibration.EMPTY_HASH)
        after = ledger.begin_run(sid, 'Review the source.', [], None)
        past, current = (ledger.run(run)['config'] for run in (before, after))
        for key in ('contract_hash', 'epoch', 'logic_hash', 'request_hash'):
            self.assertEqual(past[key], current[key])
        self.assertNotEqual(past['calibration_binding'], current['calibration_binding'])
        self.assertEqual(remote_prefix.binding(ledger, before, window, instance), first_scope)
        self.assertNotEqual(remote_prefix.binding(ledger, after, window, instance), first_scope)
        with patch.object(remote_context, '_identity', side_effect=AssertionError('Historical proof replay must be offline')), \
                patch.object(remote_prefix, 'binding', side_effect=AssertionError('Replay must use the stored scope')):
            context_window.validate_proof(historical, budget, architecture_role.SYSTEM, archived, spec,
                                          remote_profiles.identity(profile['name'], profile))

    def test_discovery_requires_pinned_runtime_and_unavailable_probe_stays_conservative(self):
        unpinned = dict(self.profile)
        unpinned.pop('executable_sha256')
        self.assertIsNone(remote_context.discover(unpinned))
        self.service.fail = True
        self.assertIsNone(remote_context.discover(self.profile))
        provider = SimpleNamespace(context_metadata={}, profile=self.profile)
        self.assertIsNone(remote_context.configure(provider))
        self.assertNotIn('_sutra_context_window', provider.context_metadata)
        provider.context_metadata['_sutra_context_window'] = self.window
        self.assertEqual(remote_context.configure(provider), self.window)


class ExactRemoteChild(fixtures.Child):
    model_interface = 1

    def __init__(self, profile, window, fault=None):
        super().__init__(profile['name'], fault)
        self.profile = profile
        self.budget = Budget(16384, 2048, context_profile=2,
                             reservation_limit=65536, context_window=window)
        self.context_metadata = {'model_info': {'remote_cuda.context_length': 16384},
                                '_sutra_provider': 'sutra_remote', '_sutra_context_window': window}
        self.model_identity = remote_profiles.identity(self.model, profile)

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
        record.update(response={'done': True, 'prompt_eval_count': 500, 'eval_count': 80},
                      provider='sutra_remote')
        return result, record


class RemoteSavedTaskTests(unittest.TestCase):
    setUp = fixtures.OrchestrationTests.setUp
    tearDown = fixtures.OrchestrationTests.tearDown
    step = fixtures.OrchestrationTests.step
    until = fixtures.OrchestrationTests.until
    children = fixtures.OrchestrationTests.children
    provider = fixtures.OrchestrationTests.provider
    legacy_checkpoint = recovery.PagingRecoveryRegressionTests.legacy_checkpoint
    prior_finding = recovery.PagingRecoveryRegressionTests.prior_finding

    def test_auto_counter_pause_recovers_remote_host_after_reload_without_mac_admission(self):
        saved, branch, _, _, legacy_id, legacy_raw = self.legacy_checkpoint()
        sid = self.meta['active']
        contract = self.s.pinned(sid)
        node = machine.node(self.s, branch)
        registry = self.root / 'auto-remote-models.json'
        profile = remote_profiles.write({'auto': remote_descriptor(self.root, identifier='auto',
            context_tokens=16384, executable_sha256='b' * 64)}, registry)['auto']
        service = RemoteCounter()
        previous = self.provider
        with patch.dict(os.environ, SUTRA_REMOTE_REGISTRY=str(registry),
                        SUTRA_REMOTE_MODELS=canonical([profile['name']])), \
                patch.dict(fixtures.MODELS, {tier: profile['name'] for tier in ('low', 'mid', 'high')}), \
                patch('remote_control.verify_server', service.server), \
                patch('remote_control.ensure_tunnel', return_value={'ready': True}), \
                patch.object(remote_context, '_rpc', service.rpc):
            window = remote_context.discover(profile)
            def continuing(result, _):
                result.update(slice_status='continue', remaining=['Inspect remaining source declarations.'])
            first = ExactRemoteChild(profile, window, continuing)
            self.provider = lambda stage: first if stage == 'subagent' else previous(stage)
            self.tier = 'auto'
            reviewed = self.step(saved)
            self.assertTrue(reviewed['child']['result']['admission']['admitted'], reviewed)
            prefix = self.s.db.execute('SELECT id,record_json FROM invocation_receipts '
                'WHERE session=? ORDER BY seq', (sid,)).fetchall()
            before = review_budget.usage(self.s, sid)
            service.fail = True
            paused = self.step(reviewed)
            self.assertEqual(paused['state'], 'PAUSED')
            self.assertEqual(paused['stop']['status'], 'resource_wait')
            self.assertEqual(paused['stop']['measurement']['runtime']['model'], profile['name'])
            self.assertEqual(provider_resources.stopped_model(paused['stop'],
                {'tier': 'auto', 'models': fixtures.MODELS}), profile['name'])
            self.assertEqual(review_budget.usage(self.s, sid), before)
            self.assertEqual(len(first.payloads), 1)
            self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite')
            service.fail = False
            second = ExactRemoteChild(profile, window)
            self.provider = lambda stage: second if stage == 'subagent' else previous(stage)
            def remote_ready(root, *, model=None, **kwargs):
                self.assertEqual(model, profile['name'], 'Auto must recover the saved remote counter host')
                return {'admitted': True, 'decision': 'ALLOW'}
            with patch('provider_resources.require_resources', side_effect=remote_ready) as checks, \
                    patch('runtime_memory.recover', side_effect=AssertionError('Wrong local recovery host')), \
                    patch('monitoring.require_resources', side_effect=AssertionError('Wrong local admission host')):
                resumed = self.step(paused, event='resume')
                self.assertEqual(resumed['state'], 'CHILDREN', resumed)
                self.assertEqual(review_budget.usage(self.s, sid), before)
                finished = self.step(resumed)
            self.assertEqual(checks.call_count, 2)  # Remote recovery, then fresh admission.
            self.assertTrue(finished['child']['result']['admission']['admitted'], finished)
            self.assertEqual(len(second.payloads), 1)
            self.assertEqual(machine.node(self.s, branch), node)
            self.assertEqual(self.s.pinned(sid), contract)
            self.assertEqual(self.s.db.execute('SELECT id,record_json FROM invocation_receipts '
                'WHERE session=? ORDER BY seq', (sid,)).fetchall()[:len(prefix)], prefix)
            self.assertEqual(self.s.db.execute('SELECT record_json FROM invocation_receipts '
                'WHERE invocation_id=?', (legacy_id,)).fetchone()[0], legacy_raw)
            final = review_budget.usage(self.s, sid)
            self.assertEqual(final['used']['calls'], before['used']['calls'] + 1)
            self.assertEqual(final['used']['tokens'], before['used']['tokens'] + 580)
            self.assertEqual(final['limits'], before['limits'])
            self.assertIn(self.prior_finding, canonical(second.payloads[0]['child_checkpoint']))
            self.assertIn('CED724', canonical(second.payloads[0]['memory']))
            self.assertTrue(self.s.verify_invocations(sid))
            self.assertTrue(self.s.audit_valid())

    def test_saved_review_counter_pause_reload_and_new_model_preserve_context_and_spending(self):
        out, branch, _, _, old_id, old_raw = self.legacy_checkpoint()
        sid = self.meta['active']
        contract = self.s.pinned(sid)
        node = machine.node(self.s, branch)
        registry = self.root / 'remote-models.json'
        profiles = remote_profiles.write({name: remote_descriptor(self.root, identifier=name,
            context_tokens=16384, model='fixture-' + name, model_sha256=digest * 64,
            executable_sha256='b' * 64) for name, digest in (('first', 'a'), ('second', 'c'))}, registry)
        service = RemoteCounter()
        with patch.dict(os.environ, SUTRA_REMOTE_REGISTRY=str(registry),
                        SUTRA_REMOTE_MODELS=canonical([p['name'] for p in profiles.values()])), \
                patch('remote_control.verify_server', service.server), \
                patch('remote_control.ensure_tunnel', return_value={'ready': True}), \
                patch.object(remote_context, '_rpc', service.rpc):
            def continuing(result, _):
                result.update(slice_status='continue', remaining=['Inspect remaining source declarations.'])
            first = ExactRemoteChild(profiles['first'], remote_context.discover(profiles['first']), continuing)
            previous = self.provider
            self.provider = lambda stage: first if stage == 'subagent' else previous(stage)
            self.tier = 'high'
            with patch.dict(fixtures.MODELS, high=first.model):
                saved = self.step(out)
            self.assertTrue(saved['child']['result']['admission']['admitted'], saved)
            exact_id = saved['child']['result']['invocation_id']
            old_exact = self.s.db.execute('SELECT record_json FROM invocation_receipts WHERE invocation_id=?',
                                        (exact_id,)).fetchone()[0]
            before = review_budget.usage(self.s, sid)
            self.assertEqual(before['used']['calls'], 2)  # Legacy and exact child in this workspace task.
            service.fail = True
            with patch.dict(fixtures.MODELS, high=first.model):
                paused = self.step(saved)
            self.assertEqual(paused['state'], 'PAUSED')
            self.assertEqual(paused['stop']['status'], 'resource_wait')
            self.assertEqual(review_budget.usage(self.s, sid), before)
            self.assertEqual(len(first.payloads), 1)
            self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite')
            service.fail = False
            second = ExactRemoteChild(profiles['second'], remote_context.discover(profiles['second']))
            self.provider = lambda stage: second if stage == 'subagent' else previous(stage)
            with patch.dict(fixtures.MODELS, high=second.model), \
                    patch('runtime_memory.recover', return_value=None), \
                    patch('provider_resources.recover', return_value=None), \
                    patch('provider_resources.require_resources', return_value={'admitted': True, 'decision': 'ALLOW'}), \
                    patch('monitoring.require_resources', return_value={'admitted': True, 'decision': 'ALLOW'}):
                resumed = self.step(paused, event='resume')
                self.assertNotEqual(resumed['state'], 'PAUSED', resumed)
                reviewed = self.step(resumed)
            self.assertTrue(reviewed['child']['result']['admission']['admitted'], reviewed)
            self.assertEqual(len(second.payloads), 1)
            self.assertEqual(machine.node(self.s, branch), node)
            self.assertEqual(self.s.pinned(sid), contract)
            self.assertEqual(review_budget.usage(self.s, sid)['used']['tokens'], before['used']['tokens'] + 580)
            packet = second.payloads[0]
            self.assertIn(self.prior_finding, canonical(packet['child_checkpoint']))
            self.assertIn('CED724', canonical(packet['memory']))
            for key in ('edits_allowed', 'network_allowed', 'dependencies_allowed'):
                self.assertFalse(packet['contract']['constraints'][key])
            for iid, expected in ((old_id, old_raw), (exact_id, old_exact)):
                self.assertEqual(self.s.db.execute('SELECT record_json FROM invocation_receipts WHERE invocation_id=?',
                                                 (iid,)).fetchone()[0], expected)
            with patch.object(remote_context, '_identity', side_effect=AssertionError('Receipt replay must be offline')):
                self.assertTrue(self.s.load_result(sid, old_id))
                self.assertTrue(self.s.load_result(sid, exact_id))
                self.assertTrue(self.s.verify_invocations(sid))
            self.assertTrue(self.s.audit_valid())


class NativeCounterHTTPTests(unittest.TestCase):
    def test_native_count_http_is_bounded_authenticated_and_rejects_invalid_token_shapes(self):
        responses = []
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((self.path, self.headers.get('Authorization'),
                                 json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                raw = json.dumps(responses[-1]).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(raw)))
                self.end_headers(); self.wfile.write(raw)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        try:
            with patch('remote_control.headers', return_value={'Authorization': 'Bearer fixture-secret'}):
                for value in (None, True, -1, 0, '500', 500.0, 2 ** 21 + 1):
                    responses.append({'object': 'response.input_tokens', 'input_tokens': value})
                    with self.subTest(value=value), self.assertRaises(ValueError):
                        remote_context._rpc({'port': server.server_port}, {'messages': []})
                responses.append({'object': 'response.input_tokens', 'input_tokens': 3000})
                self.assertEqual(remote_context._rpc({'port': server.server_port}, {'messages': []}), 3000)
                responses.append({'input_tokens': 3000})
                with self.assertRaises(ValueError):
                    remote_context._rpc({'port': server.server_port}, {'messages': []})
        finally:
            server.shutdown(); server.server_close(); worker.join(2)
        self.assertTrue(all(row[0] == remote_context.COUNT_PATH for row in requests))
        self.assertTrue(all(row[1] == 'Bearer fixture-secret' for row in requests))


if __name__ == '__main__':
    unittest.main()
