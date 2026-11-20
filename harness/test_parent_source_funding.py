"""Earned parent source survives financial recovery without repricing history.

The saved 0.51.6 writer had a 17,253-unit budget (69,015 remaining / four),
5,751 output units, 611 map bytes and just 605 source bytes: analyze=12,
architecture_runtime=134, repository_tools=459. The fixtures below reproduce
the financial/physical boundary with governed temporary receipts, not inference
or a copy of the user's ledger. They do not establish real report quality.
"""
import copy
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from context_manager import ContextBudgetExceeded, InvalidUpdate, canonical
from invocation_gateway import InvocationGateway, InvocationLedger
from monitoring import ResourceWait
from paging import Budget
import chat_runtime as chat
import context_window
import orchestration as machine
import orchestration_flow as flow
import repository_tools as repo
import review_budget
import review_discovery as discovery
import workspace_allocation
import test_discovery_selection as fixture
import test_discovery_working_set as paid_fixture
from test_context_window_recovery import CountService
from test_model_packet import WireModel


class ParentSourceFundingTests(unittest.TestCase):
    tearDown = fixture.SelectionWireTests.tearDown
    step = fixture.SelectionWireTests.step
    until = fixture.SelectionWireTests.until
    provider = fixture.SelectionWireTests.provider
    discover = fixture.SelectionWireTests.discover
    pay_to = paid_fixture.DiscoveryWorkingSetTests.pay_to
    _pay_to = paid_fixture.DiscoveryWorkingSetTests._pay_to

    def setUp(self):
        fixture.SelectionWireTests.setUp(self)
        # This is the saved task's amended ceiling, rather than a spend reset.
        defaults = patch.dict(review_budget.DEFAULTS, review_tokens=312500)
        defaults.start(); self.addCleanup(defaults.stop)
        for index in range(3):
            (self.ws / f'part{index}.py').write_text(
                f'def part{index}():\n' + ''.join(
                    f'    value = {line}  # paid coherent source for module {index}, line {line:04}\n'
                    for line in range(112)) + '    return value\n')
        self.actions = [{'kind': 'read', 'target': f'part{index}.py'} for index in range(3)] + [{'kind': 'clarify'}]

    def prepared(self, *, paid=False):
        out, packet = self.discover(); sid = self.meta['active']
        data = chat.runtime.load_partition(self.s, sid, str(self.ws), packet['snapshot_receipts'], packet['snapshot_owner'])
        data['followups'] = discovery.load(self.s, sid, packet)
        self.assertEqual(set(data['followups']), {'part0.py', 'part1.py', 'part2.py'})
        self.assertTrue(all(len(entry['text'].encode()) > 2400 for entry in data['followups'].values()))
        self.parent_packet, self.parent_data = copy.deepcopy(packet), copy.deepcopy(data)
        if paid: self.pay_to(out, 260340)
        packet['parent_source_funding_version'] = 1
        return out, packet, data

    def model(self, packet, cap=65536):
        return SimpleNamespace(model=packet['route']['record']['choice']['model'], budget=Budget(cap, 4096, 256),
            context_metadata={'model_info': {'fixture.context_length': cap}, '_sutra_runtime_cap': cap})

    def rows(self):
        return self.s.db.execute('SELECT id,record_json FROM invocation_receipts ORDER BY seq').fetchall()

    def source_rows(self):
        return self.s.db.execute('SELECT id,record_json FROM repository_receipts ORDER BY rowid').fetchall()

    def test_saved260340_funds_full_three_paid_pages_then_reload_model_switch_without_replay(self):
        out, packet, data = self.prepared(paid=True); sid = self.meta['active']
        before = review_budget.usage(self.s, sid); receipts = self.rows(); sources = self.source_rows()
        contract = self.s.pinned(sid); original = machine.node(self.s, out['execution_id'])
        notes, children = copy.deepcopy(packet['discovery_notes_unverified']), copy.deepcopy(packet.get('child_results', []))
        self.assertEqual(before['used']['tokens'], 260340)
        self.assertEqual(before['limits']['tokens'], 312500)
        model = self.model(packet)
        with machine.scope(original['id']), self.assertRaises(machine.OrchestrationStop) as stopped:
            chat.fit_packet(self.s, sid, packet, data, model, True)
        self.assertEqual(stopped.exception.frame['limiting_dimension'], 'tokens')
        expected = {path: entry['text'] for path, entry in data['followups'].items()}
        for path, text in expected.items(): self.assertEqual(packet['excerpts'][path], text)
        self.assertGreater(packet['evidence_coverage']['excerpt_bytes'], 18000)
        self.assertEqual(packet['map_bytes'], 512)
        self.assertEqual(packet['parent_discovery_note_limit'], 0)
        self.assertEqual(packet['discovery_notes_unverified'], notes)
        self.assertEqual(packet.get('child_results', []), children)
        from model_packet import build
        wire = json.loads(build(chat.SYSTEM, packet, chat.schema(packet))['messages'][1]['content'])
        self.assertEqual(wire['discovery_notes_unverified'], [])
        self.assertFalse(wire.get('child_analysis_unverified'))
        self.assertEqual(packet['evidence_coverage']['child_reports'], 0)
        self.assertEqual(packet['memory_budget']['context_limit'], 65536)
        self.assertEqual(set(packet['memory_budget']), {'context_limit', 'response_reserve', 'template_reserve'})
        self.assertEqual(stopped.exception.frame['reserved_next']['calls'], 2)
        self.assertEqual(review_budget.usage(self.s, sid), before)
        stop = {'status': 'task_budget', 'stage': 'SYNTHESIS', 'frame': stopped.exception.frame}
        with patch.dict(os.environ, {workspace_allocation.ENVIRONMENT_KEY: '{}'}):
            resumed = flow.budget_pause(self.s, self.meta, self.root, {'armed': True}, out['flow_id'], 'SYNTHESIS', stop)
        self.assertEqual(resumed['state'], 'SYNTHESIS', resumed.get('stop'))
        funded = review_budget.usage(self.s, sid)
        self.assertGreater(funded['limits']['tokens'], 312500); self.assertEqual(funded['used'], before['used'])
        self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite'); self.tier = 'high'
        model = self.model(packet)
        with machine.scope(resumed['execution_id']), patch.object(repo, 'execute', side_effect=AssertionError('Paid source must not be re-read')):
            _, attempts = chat.fit_packet(self.s, sid, packet, data, model, True)
            chat.request_check(self.s, sid, packet)
        self.assertEqual(attempts, 3)
        for path, text in expected.items(): self.assertEqual(packet['excerpts'][path], text)
        self.assertEqual(self.rows(), receipts); self.assertEqual(self.source_rows(), sources)
        self.assertEqual(review_budget.usage(self.s, sid), funded); self.assertEqual(self.s.pinned(sid), contract)
        renewed = machine.node(self.s, resumed['execution_id'])
        self.assertEqual(renewed['permissions'], original['permissions'])
        self.assertEqual(renewed['context'], original['context'])
        self.assertIn('CED724', canonical(packet['memory']))
        self.assertFalse(contract['constraints']['network_allowed']); self.assertFalse(contract['constraints']['edits_allowed'])
        self.assertTrue(self.s.verify_invocations(sid)); self.assertTrue(self.s.audit_valid())

    def test_hard_workspace_ceiling_keeps_source_and_all_paid_receipts(self):
        out, packet, data = self.prepared(paid=True); sid = self.meta['active']
        before = review_budget.usage(self.s, sid); rows = self.rows(); sources = self.source_rows()
        with machine.scope(out['execution_id']), self.assertRaises(machine.OrchestrationStop) as stopped:
            chat.fit_packet(self.s, sid, packet, data, self.model(packet), True)
        with patch.dict(os.environ, {workspace_allocation.ENVIRONMENT_KEY: '{"tokens":312500}'}):
            paused = flow.budget_pause(self.s, self.meta, self.root, {'armed': True}, out['flow_id'], 'SYNTHESIS',
                {'status': 'task_budget', 'stage': 'SYNTHESIS', 'frame': stopped.exception.frame})
        self.assertEqual(paused['state'], 'PAUSED')
        self.assertEqual(paused['stop']['automatic_allocation']['reason'], 'workspace_limit')
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertEqual(self.rows(), rows); self.assertEqual(self.source_rows(), sources)
        self.assertTrue(all(len(packet['excerpts'][path].encode()) > 2400 for path in data['followups']))

    def test_physical_prefix_is_whole_and_honest_without_a_financial_amendment(self):
        out, packet, data = self.prepared(); sid = self.meta['active']; before = review_budget.usage(self.s, sid)
        rows, sources = self.rows(), self.source_rows(); model = self.model(packet, cap=16384)
        with machine.scope(out['execution_id']):
            _, attempts = chat.fit_packet(self.s, sid, packet, data, model, True)
            chat.request_check(self.s, sid, packet)
        self.assertEqual(attempts, 3)
        self.assertLess(packet['evidence_coverage']['excerpt_bytes'], sum(len(v['text'].encode()) for v in data['followups'].values()))
        for path, text in packet['excerpts'].items():
            if path in data['followups']:
                self.assertTrue(data['followups'][path]['text'].startswith(text)); self.assertTrue(text.endswith('\n'))
                self.assertEqual(packet['excerpt_locations'][path]['start_line'], data['followups'][path]['line'])
        self.assertEqual(packet['map_bytes'], 512)
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertEqual(self.rows(), rows); self.assertEqual(self.source_rows(), sources)

    def test_unfit_mandatory_packet_never_requests_financial_capacity(self):
        out, packet, data = self.prepared(); sid = self.meta['active']; before = review_budget.usage(self.s, sid)
        gates = machine.gates
        def only_measure(*args):
            if args[2]: raise AssertionError('An unfit physical packet cannot ask for money')
            return gates(*args)
        with machine.scope(out['execution_id']), patch.object(machine, 'gates', side_effect=only_measure), \
                self.assertRaises(ContextBudgetExceeded):
            chat.fit_packet(self.s, sid, packet, data, self.model(packet, cap=4096), True)
        self.assertEqual(review_budget.usage(self.s, sid), before)

    def test_legacy_four_shares_and_new_optional_limits_are_versioned(self):
        _, packet, data = self.prepared(); sid = self.meta['active']
        packet.pop('parent_source_funding_version')
        model = self.model(packet)
        task = {'limits': {'tokens': 312500}, 'used': {'tokens': 243485}}
        with patch('review_budget.usage', return_value=task), patch.object(machine, 'gates', return_value=[task]):
            chat.fit_packet(self.s, sid, packet, data, model, True)
        # The exact saved budget split is kept for absent historical markers.
        self.assertEqual(model.budget.context_limit, 17253)
        self.assertEqual(model.budget.response_reserve, 5751)
        self.assertNotIn('parent_discovery_note_limit', packet)
        for key, value in (('parent_source_funding_version', True), ('parent_source_funding_version', 2),
                           ('parent_discovery_note_limit', 0)):
            bad = copy.deepcopy(packet); bad[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(InvalidUpdate):
                chat.request_check(self.s, sid, bad)
        for key, value in (('parent_discovery_note_limit', True),
                           ('parent_child_note_limit', len(packet.get('child_results', [])) + 1),
                           ('parent_memory_extra_bytes', 4097)):
            bad = dict(packet, parent_source_funding_version=1); bad[key] = value
            with self.subTest(key=key, value=value), self.assertRaisesRegex(InvalidUpdate, 'Invalid parent optional'):
                chat.request_check(self.s, sid, bad)

    def test_exact_metadata_with_initial_three_field_budget_uses_counted_path(self):
        out, packet, data = self.prepared(); sid = self.meta['active']; counter = CountService()
        model = self.model(packet, cap=32768)
        model.context_metadata['_sutra_context_window'] = dict(counter.descriptor(), context_tokens=32768)
        self.assertEqual(model.budget.context_profile, 1)
        context_window._counts.clear()
        with machine.scope(out['execution_id']), patch.object(context_window, '_rpc', counter.rpc), \
                patch.object(chat, '_fit_source_funded_packet', side_effect=AssertionError('Exact metadata must retain physical counting')):
            chat.fit_packet(self.s, sid, packet, data, model, True)
        self.assertEqual(model.budget.context_profile, 2)
        self.assertTrue(any(call[2] == '/v1/messages/count_tokens' for call in counter.calls))

    def test_paid_writer_refits_then_reload_switch_keeps_three_attempt_ceiling(self):
        ready, _, _ = self.prepared(); sid = self.meta['active']; previous_provider = self.provider
        self.provider = lambda stage: None if stage == 'synthesis' else previous_provider(stage)
        calls, fits = [], []
        invoke, fitted = InvocationGateway.invoke, chat.fit_packet
        dispatches = 0
        def provider(root, name, **kwargs):
            model = WireModel(name)
            def reject(answer, packet):
                choices = chat.citation_options(packet)
                answer['citations'] = [choices.index(item) + 1 for item in answer['citations']]
                answer['answer'] = ''
            model.after = reject
            model.budget = Budget(65536, 4096, 256)
            model.context_metadata = {'model_info': {'fixture.context_length': 65536}, '_sutra_runtime_cap': 65536}
            transport = model.call
            def measured(*args):
                value, record = transport(*args)
                record['response'] = {'done': True, 'prompt_eval_count': 400, 'eval_count': 80}
                calls.append(copy.deepcopy(args[2])); return value, record
            model.call = measured
            return model
        def record_fit(store, session, packet, *args, **kwargs):
            result = fitted(store, session, packet, *args, **kwargs)
            fits.append((review_budget.usage(store, session)['used']['tokens'], copy.deepcopy(packet)))
            return result
        def pause(gateway, role, *args):
            nonlocal dispatches
            if role == 'chat_reviewer':
                dispatches += 1
                if dispatches == 2: raise ResourceWait({'admitted': False, 'reason': 'Fixture physical measurement unavailable'})
            return invoke(gateway, role, *args)
        with patch('api_runtime.provider_for', side_effect=provider), \
                patch('monitoring.require_resources', return_value={'admitted': True}), \
                patch.object(repo, 'require_resources', return_value={'admitted': True}), \
                patch.object(chat, 'fit_packet', side_effect=record_fit), \
                patch.object(InvocationGateway, 'invoke', pause):
            stopped = self.step(ready)
            for _ in range(3):
                if stopped['state'] == 'PAUSED': break
                self.assertEqual(stopped['state'], 'SYNTHESIS')
                stopped = self.step(stopped)
        self.assertEqual(stopped['state'], 'PAUSED', stopped.get('stop'))
        self.assertEqual(stopped['stop']['status'], 'resource_wait')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['parent_source_funding_version'], 1)
        self.assertEqual(calls[0]['parent_repair']['attempt_limit'], 3)
        self.assertEqual(len(fits), 2)
        self.assertEqual(fits[1][0] - fits[0][0], 480)
        receipts, sources = self.rows(), self.source_rows(); before = review_budget.usage(self.s, sid)
        contract = self.s.pinned(sid)
        self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite'); self.tier = 'high'
        resumed = self.step(stopped, event='resume')
        with patch('api_runtime.provider_for', side_effect=provider), \
                patch('monitoring.require_resources', return_value={'admitted': True}), \
                patch.object(repo, 'require_resources', return_value={'admitted': True}):
            done = self.until('DONE', resumed)
        self.assertFalse(done['data']['result']['admission']['admitted'])
        self.assertEqual(len(calls), 3)
        for packet in calls:
            self.assertEqual(packet['_request_nonce'], calls[0]['_request_nonce'])
            self.assertEqual(packet['parent_repair'], calls[0]['parent_repair'])
            self.assertEqual(packet['parent_source_funding_version'], 1)
            self.assertEqual(packet['contract'], contract)
        self.assertNotEqual(calls[-1]['route']['record']['choice']['model'], calls[0]['route']['record']['choice']['model'])
        self.assertEqual(self.rows()[:len(receipts)], receipts)
        self.assertEqual(self.source_rows()[:len(sources)], sources)
        self.assertEqual(review_budget.usage(self.s, sid)['used']['tokens'], before['used']['tokens'] + 960)
        self.assertTrue(self.s.verify_invocations(sid))


if __name__ == '__main__': unittest.main()
