"""Source-aware discovery reasoning and orchestration-only one-shot controls.

All transports/counters are deterministic fixtures, not native inference or a
claim that a particular reasoning control improves report quality.
"""
import copy
import json
import os
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import architecture_role as role
import context_window
import model_packet
import orchestration as machine
import review_budget
import review_discovery as discovery
import review_reasoning
import subagent_runtime
import test_discovery_selection as fixture
import test_orchestration as child_fixture
import vscode_bridge as bridge
from context_manager import InvalidUpdate, canonical, sha
from invocation_gateway import InvocationLedger
from model_identity import unknown
from paging import Budget
from reasoning_policy import check_provider, make
from test_context_window_recovery import CountService
from test_discovery_note_budget import payload as old_payload


VALUES = ['low', 'medium', 'high']


def source_packet(flag=True, source=True):
    packet = old_payload(2)
    if flag: packet['discovery_reasoning_version'] = 1
    if not source: packet.update(initial_excerpts={}, evidence=[])
    return packet


def provider(values=VALUES):
    return SimpleNamespace(context_metadata={'thinking': {'values': list(values),
                           'default': values[-1] if values else None}},
                           budget=Budget(32768, 2048, 479), thinking=None)


class DiscoveryControlTests(unittest.TestCase):
    def test_new_profile_honors_overrides_and_source_controls_without_inventing_support(self):
        for preference, expected, step in (('adaptive', 'low', 1), ('on', 'low', 0),
                                            ('off', False, 0), ('model-default', None, 0)):
            with self.subTest(preference=preference):
                model = provider(); packet = source_packet()
                review_reasoning.bind(model, packet, bounded_selection=True, preference=preference)
                self.assertEqual(packet['reasoning']['preference'], preference)
                self.assertEqual(packet['reasoning']['step'], step)
                self.assertIs(model.thinking, expected) if expected is None or expected is False else self.assertEqual(model.thinking, expected)
                review_reasoning.check(packet); check_provider(model, packet)
        for values in ([], ['unfamiliar'], [True, 'low']):
            model = provider(values); packet = source_packet()
            review_reasoning.bind(model, packet, bounded_selection=True, preference='adaptive')
            self.assertIsNone(model.thinking)
            review_reasoning.check(packet)
        model = provider(); packet = source_packet(source=False)
        review_reasoning.bind(model, packet, bounded_selection=True, preference='adaptive')
        self.assertIs(model.thinking, False)
        packet['evidence'] = [{'text': 'def dispatch():\n    return result\n'}]
        review_reasoning.bind(model, packet, bounded_selection=True, preference='adaptive')
        self.assertEqual(model.thinking, 'low')
        packet['evidence'] = []
        with self.assertRaises(InvalidUpdate): review_reasoning.check(packet)
        review_reasoning.bind(model, packet, bounded_selection=True, preference='adaptive')
        self.assertIs(model.thinking, False); review_reasoning.check(packet)

    def test_exact_marker_profile_and_supported_rung_are_required(self):
        model = provider(); packet = source_packet()
        review_reasoning.bind(model, packet, bounded_selection=True, preference='adaptive')
        for key, value in (('discovery_reasoning_version', True), ('discovery_reasoning_version', 1.0),
                           ('discovery_reasoning_version', 2), ('discovery_selection_version', 3.0),
                           ('evidence_discovery_version', 4.0), ('phase', 'report')):
            changed = copy.deepcopy(packet); changed[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(InvalidUpdate):
                review_reasoning.check(changed)
        for step in (0, 2):
            changed = copy.deepcopy(packet)
            changed['reasoning'] = make(packet['reasoning']['capabilities'], 'adaptive', step)
            with self.assertRaises(InvalidUpdate): review_reasoning.check(changed)
        changed = copy.deepcopy(packet); changed.pop('reasoning')
        with self.assertRaises(InvalidUpdate): review_reasoning.check(changed)

    def test_existing_strata_adaptive_caps_do_not_change_explicit_on_or_output_reserve(self):
        for preference, expected_budget in (('adaptive', 682), ('on', None), ('off', None), ('model-default', None)):
            packet = source_packet(); model = provider()
            review_reasoning.bind(model, packet, bounded_selection=True, preference=preference)
            wire, _, _ = context_window.wire_request(role.SYSTEM, packet, role.schema(packet),
                'fixture-native', model.thinking, model.budget.response_reserve)
            self.assertEqual(wire['max_tokens'], 2048)
            self.assertEqual(wire.get('reasoning_budget_tokens'), expected_budget)
            self.assertEqual(model.budget.response_reserve, 2048)
            if preference == 'model-default': self.assertNotIn('reasoning_effort', wire)
        packet = source_packet(); model = provider(); model.budget = Budget(32768, 8192, 479)
        review_reasoning.bind(model, packet, bounded_selection=True, preference='adaptive')
        wire, _, _ = context_window.wire_request(role.SYSTEM, packet, role.schema(packet), 'fixture-native', model.thinking, 8192)
        self.assertEqual((wire['max_tokens'], wire['reasoning_budget_tokens']), (3072, 768))
        self.assertEqual(model.budget.response_reserve, 8192)

    def test_absent_profile_preserves_old_control_wire_charge_and_offline_proof(self):
        packet = source_packet(flag=False); model = provider()
        review_reasoning.bind(model, packet, bounded_selection=True, preference='on')
        self.assertEqual(packet['reasoning']['preference'], 'adaptive'); self.assertIs(model.thinking, False)
        built = model_packet.build(role.SYSTEM, packet, role.schema(packet))
        self.assertEqual(sha(canonical(built)), '1298bdd720e25562276adce6fb6f24dacba204541dab587a238dd580b990a2e1')
        self.assertEqual(model_packet.descriptor(built, 479)['input_reservation'], 5211)
        service = CountService(); window = service.descriptor()
        budget = Budget(8192, 2048, 479, context_profile=2, reservation_limit=65536, context_window=window)
        context_window._counts.clear()
        with patch.object(context_window, '_rpc', service.rpc):
            proof = context_window.check(budget, role.SYSTEM, packet, role.schema(packet), planning=True)
        identity = {'server_model': window['model'], 'server_started': window['started'],
                    'server_context_tokens': window['context_tokens']}
        modern = source_packet(); review_reasoning.bind(model, modern, bounded_selection=True, preference='adaptive')
        with patch.object(context_window, '_rpc', side_effect=AssertionError('Proof replay must stay offline')):
            context_window.validate_proof(proof, budget, role.SYSTEM, packet, role.schema(packet), identity)
            with self.assertRaises(InvalidUpdate):
                context_window.validate_proof(proof, budget, role.SYSTEM, modern, role.schema(modern), identity)


class DiscoveryReasoningGatewayTests(unittest.TestCase):
    setUp_base = fixture.SelectionWireTests.setUp
    tearDown = fixture.SelectionWireTests.tearDown
    step = fixture.SelectionWireTests.step
    until = fixture.SelectionWireTests.until
    discover = fixture.SelectionWireTests.discover

    def setUp(self):
        self.setUp_base()
        self.preference = 'adaptive'; self.values = list(VALUES); self.counter = None
        self.wires = []; self.inspect = None

    def provider(self, stage):
        model = fixture.SelectionWireTests.provider(self, stage)
        if stage == 'synthesis':
            worker = model.discovery_provider
            worker.context_metadata = {'thinking': {'values': self.values,
                'default': self.values[-1] if self.values else None}}
            server_model = 'fixture-native'
            if self.counter:
                worker.model = 'strata:profile/discovery-reasoning-' + self.tier
                server_model = 'fixture-native-' + self.tier
                window = dict(self.counter.descriptor(server_model, 19600 if self.tier == 'low' else 19601),
                              context_tokens=32768, started=111 if self.tier == 'low' else 222)
                worker.budget = Budget(32768, 2048, 479, context_profile=2, reservation_limit=65536, context_window=window)
                worker.context_metadata['_sutra_context_window'] = window
                worker.model_identity = dict(unknown(worker.model), server_model=server_model,
                    server_started=window['started'], server_context_tokens=32768)
            original = worker.call
            def transport(cid, system, packet, schema):
                review_reasoning.check(packet); check_provider(worker, packet)
                if self.inspect: self.inspect(packet)
                wire, _, _ = context_window.wire_request(system, packet, schema, server_model,
                    worker.thinking, worker.budget.response_reserve)
                self.wires.append(copy.deepcopy(wire))
                result, record = original(cid, system, packet, schema)
                record['response']['done'] = True
                if self.counter:
                    record['response']['prompt_eval_count'] = self.counter.prompt_tokens
                    record['provider'] = 'strata'
                return result, record
            worker.call = transport
        return model

    def completed(self):
        rpc = patch.object(context_window, '_rpc', self.counter.rpc) if self.counter else nullcontext()
        with patch.dict(os.environ, SUTRA_THINKING=self.preference), rpc:
            return self.discover()

    def assert_controls(self, preference, requested, step):
        self.preference = preference
        _, packet = self.completed()
        self.assertEqual(packet['discovery_coverage']['stop_reason'], 'evidence_complete')
        self.assertEqual(len(self.calls), 3)
        for source, _, _ in self.calls:
            self.assertEqual(source['discovery_reasoning_version'], 1)
            self.assertEqual(source['reasoning']['preference'], preference)
            self.assertEqual(source['reasoning']['requested'], requested)
            self.assertEqual(source['reasoning']['step'], step)
            self.assertIn('CED724', canonical(source['memory']))
            self.assertEqual(source['memory_budget']['response_reserve'], 2048)
        self.assertTrue(self.s.verify_invocations(self.meta['active']))

    def test_actual_gateway_adaptive_uses_first_supported_positive(self): self.assert_controls('adaptive', 'low', 1)
    def test_actual_gateway_explicit_on_is_not_rewritten(self): self.assert_controls('on', 'low', 0)
    def test_actual_gateway_explicit_off_is_preserved(self): self.assert_controls('off', False, 0)
    def test_actual_gateway_model_default_is_preserved(self): self.assert_controls('model-default', None, 0)

    def test_unknown_controls_retain_default_even_with_actual_source(self):
        self.values = []; self.assert_controls('adaptive', None, 0)
        self.assertTrue(all('reasoning_effort' not in wire for wire in self.wires))

    def test_exact_gateway_proof_binds_positive_wire_and_rejects_forged_controls(self):
        self.counter = CountService(); context_window._counts.clear()
        def inspect(packet):
            for key, value in (('discovery_reasoning_version', True), ('discovery_reasoning_version', 1.0),
                               ('discovery_reasoning_version', 2)):
                changed = copy.deepcopy(packet); changed[key] = value
                with self.assertRaises(InvalidUpdate): role.request_check(self.s, self.meta['active'], changed)
            changed = copy.deepcopy(packet)
            changed['reasoning'] = make(packet['reasoning']['capabilities'], 'adaptive', 2)
            with self.assertRaises(InvalidUpdate): role.request_check(self.s, self.meta['active'], changed)
            raw = self.s.db.execute("SELECT record_json FROM invocations WHERE json_extract(record_json,'$.role')='architect' ORDER BY rowid DESC LIMIT 1").fetchone()[0]
            env = json.loads(raw); proof = copy.deepcopy(env['physical_context']); proof['thinking'] = False
            with self.assertRaises(InvalidUpdate):
                context_window.validate_proof(proof, Budget(**env['budget_profile']), role.SYSTEM, packet,
                                              env['schema'], env['model_identity'])
        self.inspect = inspect
        self.assert_controls('adaptive', 'low', 1)
        rows = self.s.db.execute("SELECT record_json FROM invocations WHERE json_extract(record_json,'$.role')='architect' ORDER BY rowid").fetchall()
        for (raw,), wire in zip(rows, self.wires):
            env = json.loads(raw); proof = env['physical_context']
            self.assertEqual(proof['thinking'], 'low')
            self.assertEqual(proof['prompt_tokens'], 500)
            self.assertEqual(proof['wire_sha256'], sha(canonical(wire)))
            self.assertEqual(wire['reasoning_budget_tokens'], 682)
            self.assertEqual(env['budget_profile']['response_reserve'], 2048)
        count_bodies = [row[3] for row in self.counter.calls if row[2] == '/v1/messages/count_tokens']
        self.assertTrue(count_bodies)
        self.assertTrue(all(body.get('output_config') == {'effort': 'low'} for body in count_bodies))

    def test_counter_stop_reload_and_model_switch_preserve_paid_context_and_retry_limit(self):
        self.counter = CountService(); context_window._counts.clear()
        with patch.dict(os.environ, SUTRA_THINKING='adaptive'), patch.object(context_window, '_rpc', self.counter.rpc):
            out = self.step(self.until('SYNTHESIS')); sid = self.meta['active']
            self.assertEqual(len(self.calls), 1)
            rows = self.s.db.execute('SELECT id,record_json FROM invocation_receipts ORDER BY seq').fetchall()
            before = review_budget.usage(self.s, sid); contract = self.s.pinned(sid)
            self.counter.fail_counts = True; context_window._counts.clear()
            stopped = self.step(out)
        self.assertEqual(stopped['state'], 'PAUSED', stopped.get('stop'))
        self.assertEqual(stopped['stop']['status'], 'resource_wait')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(review_budget.usage(self.s, sid), before)
        iid = self.s.db.execute("SELECT id FROM invocations WHERE json_extract(record_json,'$.role')='architect' ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        with patch.object(context_window, '_rpc', side_effect=AssertionError('Accepted history must stay offline')):
            self.assertTrue(self.s.load_result(sid, iid)); self.assertTrue(self.s.verify_invocations(sid))
        self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite'); self.tier = 'high'
        self.counter.fail_counts = False
        with patch.dict(os.environ, SUTRA_THINKING='adaptive'), patch.object(context_window, '_rpc', self.counter.rpc):
            resumed = self.step(stopped, event='resume'); saved = self.step(resumed)
        self.assertEqual(saved['state'], 'SYNTHESIS', saved.get('stop'))
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(review_budget.usage(self.s, sid)['used']['tokens'], before['used']['tokens'] + 580)
        self.assertEqual(self.s.db.execute('SELECT id,record_json FROM invocation_receipts ORDER BY seq').fetchall()[:len(rows)], rows)
        self.assertEqual(self.s.pinned(sid), contract)
        for packet, _, _ in self.calls:
            self.assertEqual(packet['reasoning']['requested'], 'low')
            self.assertIn('CED724', canonical(packet['memory']))
            self.assertFalse(packet['contract']['constraints']['edits_allowed'])
        windows = [json.loads(raw)['physical_context']['window'] for raw, in self.s.db.execute(
            "SELECT record_json FROM invocations WHERE json_extract(record_json,'$.role')='architect' ORDER BY rowid")]
        self.assertNotEqual(windows[0]['model'], windows[1]['model'])
        self.assertNotEqual(windows[0]['started'], windows[1]['started'])
        self.assertTrue(self.s.verify_invocations(sid))

    def test_raw_model_cannot_set_reasoning_or_add_retries(self):
        self.counter = CountService(); context_window._counts.clear()
        self.fault = lambda raw, _: raw.update(reasoning={'requested': 'high'})
        _, packet = self.completed()
        self.assertEqual(packet['discovery_coverage']['stop_reason'], 'proposal_rejected')
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(packet['discovery_coverage']['tool_results'], 0)
        rows = self.s.db.execute("SELECT r.record_json FROM invocation_receipts r JOIN invocations i ON i.id=r.invocation_id WHERE json_extract(i.record_json,'$.role')='architect'").fetchall()
        self.assertEqual(len(rows), 3)
        for raw, in rows:
            receipt = json.loads(raw)
            self.assertEqual(receipt['status'], 'quarantined')
            self.assertEqual(receipt['accounting_settlement']['used_tokens'], 580)


class OneShotChildScopeTests(unittest.TestCase):
    setUp = child_fixture.OrchestrationTests.setUp
    tearDown = child_fixture.OrchestrationTests.tearDown
    step = child_fixture.OrchestrationTests.step
    until = child_fixture.OrchestrationTests.until
    children = child_fixture.OrchestrationTests.children

    def provider(self, stage):
        model = child_fixture.OrchestrationTests.provider(self, stage)
        if stage == 'subagent': model.context_metadata = {'thinking': {'values': VALUES, 'default': 'high'}}
        return model

    def test_one_shot_parent_on_keeps_child_base_policy_pins_and_financial_charge(self):
        with patch.dict(os.environ, SUTRA_THINKING='on', SUTRA_CHILD_THINKING='adaptive'):
            ready = self.children(); before = self.s.db.execute('SELECT id,record_json FROM invocation_receipts ORDER BY seq').fetchall()
            out = self.step(ready)
        self.assertTrue(out['child']['result']['admission']['admitted'], out)
        child = next(model for stage, model in reversed(self.models) if stage == 'subagent')
        packet = child.payloads[0]
        self.assertEqual(packet['reasoning']['preference'], 'adaptive')
        self.assertIs(packet['reasoning']['requested'], False)
        self.assertIn('CED724', canonical(packet['memory']))
        alternative = copy.deepcopy(packet)
        review_reasoning.bind(provider(), alternative, preference='on')
        self.assertEqual(model_packet.build(subagent_runtime.SYSTEM, packet, subagent_runtime.schema(packet)),
                         model_packet.build(subagent_runtime.SYSTEM, alternative, subagent_runtime.schema(alternative)))
        self.assertEqual(self.s.db.execute('SELECT id,record_json FROM invocation_receipts ORDER BY seq').fetchall()[:len(before)], before)
        self.assertTrue(self.s.verify_invocations(self.meta['active']))

    def test_request_environment_clears_and_restores_child_override_on_next_request_and_error(self):
        with patch.dict(os.environ, {'SUTRA_THINKING': 'off'}, clear=False):
            os.environ.pop('SUTRA_CHILD_THINKING', None)
            baseline = dict(os.environ)
            with self.assertRaisesRegex(RuntimeError, 'fixture'):
                with bridge.request_environment({'SUTRA_THINKING': 'on', 'SUTRA_CHILD_THINKING': 'adaptive'}, baseline):
                    self.assertEqual(os.environ['SUTRA_THINKING'], 'on')
                    self.assertEqual(os.environ['SUTRA_CHILD_THINKING'], 'adaptive')
                    raise RuntimeError('fixture')
            self.assertEqual(os.environ['SUTRA_THINKING'], 'off')
            self.assertNotIn('SUTRA_CHILD_THINKING', os.environ)
            with bridge.request_environment({'SUTRA_THINKING': 'off', 'SUTRA_CHILD_THINKING': None}, baseline):
                self.assertNotIn('SUTRA_CHILD_THINKING', os.environ)
                self.assertEqual(os.environ['SUTRA_THINKING'], 'off')
            self.assertEqual(os.environ['SUTRA_THINKING'], 'off')
            self.assertNotIn('SUTRA_CHILD_THINKING', os.environ)

    def test_present_malformed_child_preference_cannot_fall_back_to_parent_boost(self):
        with patch.dict(os.environ, SUTRA_THINKING='on'):
            for value in ('', 'invalid', '1'):
                with patch.dict(os.environ, SUTRA_CHILD_THINKING=value), self.assertRaises(InvalidUpdate):
                    subagent_runtime._child_preference()
            for value in ('adaptive', 'off', 'on', 'model-default'):
                with patch.dict(os.environ, SUTRA_CHILD_THINKING=value):
                    self.assertEqual(subagent_runtime._child_preference(), value)
            with patch.dict(os.environ):
                os.environ.pop('SUTRA_CHILD_THINKING', None)
                self.assertIsNone(subagent_runtime._child_preference())


if __name__ == '__main__': unittest.main()
