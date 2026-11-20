"""Linked working memory is context, never source or permission authority."""
import copy
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from context_manager import ContextBudgetExceeded, InvalidUpdate, canonical, sha
from invocation_gateway import InvocationLedger
from paging import Budget, PageFault
import architecture_role as role
import model_packet
import context_window
import orchestration_flow as flow
import review_budget
import review_discovery as discovery
import test_discovery_selection as fixture
from test_context_window_recovery import CountService
from model_identity import unknown


def journal(count=15):
    paths = ['extension.js', 'bridge_worker.js'] + [f'harness/module_{n}_long_context_name.py' for n in range(7)]
    accepted, receipts = [], []
    for n in range(count):
        path = paths[n % len(paths)]
        entry = {'id': 'unit' + str(n), 'path': path, 'line': 1, 'column': 0}
        packet = {'evidence': [entry]}
        proposal = {'summary': ('😀' * 12 if n % 2 else 'working observation ') + str(n),
                    'remaining': ['Inspect extension.js bootstrap.'], 'findings': [],
                    'tool': {'kind': 'read', 'target': path}}
        accepted.append(('i' + str(n), packet, proposal))
        receipts.append({'id': 'receipt' + str(n), 'operation': {'kind': 'read', 'invocation_id': 'i' + str(n)},
                         'result': {'excerpts': [entry]}})
    return accepted, receipts


class MemoryDirectoryTests(unittest.TestCase):
    def test_fifteen_checkpoints_nine_paths_all_nested_refs_reachable_at512_bytes(self):
        accepted, receipts = journal()
        graph = discovery._memory_graph('session', 'key', accepted, receipts)
        visited, pending = set(), [graph['root']]
        while pending:
            ref = pending.pop()
            if ref in visited: continue
            visited.add(ref); page = graph['pages'][ref]
            self.assertEqual(ref, sha('discovery-memory-v1:' + canonical(
                ['session', 'key', {k: v for k, v in page.items() if k != 'ref'}])))
            if page['kind'] in ('index', 'working_memory_root'):
                self.assertLessEqual(len(canonical(page).encode()), 512)
            pending.extend(link['ref'] for link in page.get('links', []))
        self.assertEqual(visited, set(graph['pages']))
        self.assertEqual(graph['pages'][graph['root']]['notes'], 15)
        self.assertEqual(len(graph['source_refs']), 9)

    def test_prior_advertised_root_link_is_pageable_after_next_accepted_note(self):
        accepted, receipts = journal(2)
        first = discovery._memory_graph('session', 'key', accepted[:1], receipts[:1])
        old_root = first['root']; old_index = first['pages'][old_root]['links'][0]['ref']
        accepted[-1][1].update(discovery_memory_version=1,
            discovery_memory_root=copy.deepcopy(first['pages'][old_root]), discovery_memory_pages=[])
        accepted[-1][2]['remaining'] = ['Recall ' + old_index + ' and ' + old_root]
        graph = discovery._memory_graph('session', 'key', accepted, receipts)
        packet = {'request': 'Explain the architecture.', 'repository': {'last_tool': {}}, 'active_source_page': None}
        discovery._bind_memory(packet, 'session', 'key', accepted, receipts)
        selected = {p['ref'] for p in packet['discovery_memory_pages']}
        self.assertIn(old_index, selected); self.assertIn(old_root, selected)
        for ref, body in first['pages'].items():
            discovery._memory_resolve(graph, ref, 'session', 'key', accepted, receipts)
            self.assertEqual(graph['pages'][ref], body)
        with self.assertRaises(InvalidUpdate): discovery._memory_graph('foreign', 'key', accepted, receipts)
        with self.assertRaises(InvalidUpdate): discovery._memory_graph('session', 'foreign', accepted, receipts)

    def test_focused_older_exact_note_and_latest_gap_survive_eviction(self):
        accepted, receipts = journal()
        accepted[0][2]['summary'] = 'Earlier exact extension finding.'
        accepted[-1][2]['summary'] = 'Latest exact working note.'
        packet = {'request': 'Explain extension.js.', 'repository': {'last_tool': {'target': 'extension.js'}},
                  'active_source_page': None}
        discovery._bind_memory(packet, 'session', 'key', accepted, receipts)
        self.assertIn('i0', packet['discovery_memory_note_ids'])
        self.assertIn('i14', packet['discovery_memory_note_ids'])
        pages = packet['discovery_memory_pages']
        notes = [p['note'] for p in pages if p['kind'] == 'working_note_unverified']
        self.assertTrue(any(n['summary'] == 'Earlier exact extension finding.' for n in notes))
        self.assertTrue(any(n['summary'] == 'Latest exact working note.' for n in notes))

    def test_default_20_40_80_directory_work_is_linear_and_never_resolves_history(self):
        counts = []
        for count in (20, 40, 80):
            accepted, receipts = journal(count)
            for _, _, proposal in accepted: proposal['summary'] = 'long exact note ' + 'x'*1300
            # Simulate the advertised roots that actual accepted deliveries
            # preserve. Setup work is excluded; count the current lookup only.
            for ordinal, (_, packet, _) in enumerate(accepted):
                previous = discovery._memory_graph('session', 'key', accepted[:ordinal], receipts[:ordinal])
                packet.update(discovery_memory_version=1,
                    discovery_memory_root=previous['pages'][previous['root']], discovery_memory_pages=[])
            graph = discovery._memory_graph('session', 'key', accepted, receipts)
            packet = {'request': 'Explain extension.js.', 'repository': {'last_tool': {}}, 'active_source_page': None}
            with patch.object(discovery, '_memory_resolve', side_effect=AssertionError('Default lookup must not expand history')):
                discovery._memory_selection(packet, accepted, graph, 'session', 'key', receipts)
            self.assertEqual(graph['resolved_prefixes'], {})
            work = graph['work']['pages_built'] + graph['work']['historical_roots_checked']
            self.assertLessEqual(work, 6*count + 10)
            counts.append(work)
        self.assertLessEqual(counts[1], 2.2*counts[0]); self.assertLessEqual(counts[2], 2.2*counts[1])


class MemoryGatewayTests(unittest.TestCase):
    tearDown = fixture.SelectionWireTests.tearDown
    step = fixture.SelectionWireTests.step
    until = fixture.SelectionWireTests.until

    def setUp(self):
        fixture.SelectionWireTests.setUp(self)
        trusted = patch.dict(os.environ, KARMA_NODE=shutil.which('node'),
            KARMA_TYPESCRIPT=str(Path(__file__).resolve().parent.parent/'vendor/typescript/typescript.js'))
        trusted.start(); self.addCleanup(trusted.stop)
        for path in self.ws.iterdir(): path.unlink()
        (self.ws / 'harness').mkdir()
        self.paths = ['extension.js', 'bridge_worker.js'] + [f'harness/module_{n}_long_context_name.py' for n in range(7)]
        for n, name in enumerate(self.paths):
            body = ''.join(('    // ' if name.endswith('.js') else '    # ') +
                           f'current source module {n}, line {line:03}, exact immutable body\n' for line in range(45))
            text = ('function activate() {\n' + body + '    return 1;\n}\n' if name.endswith('.js') else
                    'def run():\n' + body + '    return 1\n')
            (self.ws / name).write_text(text)
        sequence = self.paths + self.paths[1:6] + ['extension.js']
        self.actions = [{'kind': 'read', 'target': name} for name in sequence] + [{'kind': 'clarify'}]
        self.tight = 23000
        self.models = []; self.calls = []; self.inspect = None; self.extra_output = None; self.counter = None
        def notes(raw, packet):
            if len(self.calls) == 1:
                raw['summary'] = 'Earlier extension bootstrap finding retained verbatim.'
            elif len(self.calls) == 14:
                raw['summary'] = 'Latest gap is the extension bootstrap.'
                raw['remaining'] = ['Recall earlier extension.js bootstrap findings.']
            if self.extra_output: self.extra_output(raw, packet)
            if self.inspect: self.inspect(packet)
        self.fault = notes

    def provider(self, stage):
        model = fixture.SelectionWireTests.provider(self, stage)
        if stage == 'synthesis':
            worker = model.discovery_provider
            worker.budget = Budget(self.tight if len(self.calls) >= 15 else 65536, 1024, 256)
            worker.context_metadata = {'model_info': {'fixture.context_length': 65536}, '_sutra_runtime_cap': 65536}
            if self.counter:
                worker.model = 'strata:profile/test-memory-' + self.tier
                window = dict(self.counter.descriptor(), context_tokens=32768)
                worker.context_metadata['_sutra_context_window'] = window
                worker.budget = Budget(32768, 1024, 256, context_profile=2,
                                       reservation_limit=65536, context_window=window)
                worker.model_identity = dict(unknown(worker.model), server_model=window['model'],
                    server_started=window['started'], server_context_tokens=window['context_tokens'])
            call = worker.call
            def completed(*args):
                result, record = call(*args); record['response']['done'] = True
                return result, record
            worker.call = completed
        return model

    def history(self, packet):
        sid = self.meta['active']
        accepted = discovery._accepted(self.s, sid, discovery.history(self.s, sid, packet['discovery_key']))
        return accepted, discovery._tool_receipts(self.s, sid, accepted)

    def fifteen(self):
        out = self.until('SYNTHESIS')
        for _ in range(15):
            out = self.step(out)
            self.assertEqual(out['state'], 'SYNTHESIS', out.get('stop'))
        self.assertEqual(len(self.calls), 15)
        return out

    def test_actual_tight_gateway_packet_keeps_root_latest_and_recalled_old_leaf_after_reload_switch(self):
        out = self.fifteen(); sid = self.meta['active']; packet = self.calls[-1][0]
        accepted, _ = self.history(packet); self.assertEqual(len(accepted), 15)
        receipts = self.s.db.execute('SELECT id,record_json FROM invocation_receipts ORDER BY seq').fetchall()
        source_rows = self.s.db.execute('SELECT id,record_json FROM repository_receipts ORDER BY rowid').fetchall()
        before = review_budget.usage(self.s, sid); contract = self.s.pinned(sid)
        self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite'); self.tier = 'high'
        self.inspect = lambda p: discovery.check_request(self.s, sid, p)
        saved = self.step(out)
        self.assertEqual(saved['state'], 'SYNTHESIS', saved.get('stop'))
        current, wire, _ = self.calls[-1]
        self.assertEqual(current['discovery_memory_version'], 1)
        self.assertLessEqual(len(canonical(current['discovery_memory_root']).encode()), 512)
        self.assertIn(accepted[1][0], current['discovery_memory_note_ids'])
        self.assertIn(accepted[-1][0], current['discovery_memory_note_ids'])
        body = json.loads(wire['messages'][1]['content']); memory = body['working_memory_unverified']
        texts = [p['note']['summary'] for p in memory['pages'] if p['kind'] == 'working_note_unverified']
        self.assertIn('Earlier extension bootstrap finding retained verbatim.', texts)
        self.assertIn('Latest gap is the extension bootstrap.', texts)
        self.assertNotIn('earlier_work_unverified', body)
        self.assertNotIn('discovery_notes_unverified', body)
        self.assertEqual(body['request'], self.prompt)
        self.assertLess(sum(len(e['text'].encode()) for e in current['evidence']), 9*2000)
        self.assertEqual(self.s.pinned(sid), contract)
        self.assertEqual(self.s.db.execute('SELECT id,record_json FROM invocation_receipts ORDER BY seq').fetchall()[:len(receipts)], receipts)
        self.assertEqual(self.s.db.execute('SELECT id,record_json FROM repository_receipts ORDER BY rowid').fetchall(), source_rows)
        self.assertEqual(review_budget.usage(self.s, sid)['used']['tokens'], before['used']['tokens'] + 480)
        self.assertTrue(self.s.verify_invocations(sid))

    def test_actual_gateway_old_index_ref_miss_resolves_after_reload_and_model_change(self):
        out = self.step(self.until('SYNTHESIS')); remembered = []
        def ask(raw, packet):
            if len(self.calls) == 1:
                remembered.append(packet['discovery_memory_root']['links'][0]['ref'])
                raw['remaining'] = ['Recall saved directory ' + remembered[0]]
        self.extra_output = ask; out = self.step(out); sid = self.meta['active']
        old = self.s.db.execute('SELECT id,record_json FROM invocation_receipts ORDER BY seq').fetchall()
        before = review_budget.usage(self.s, sid)
        self.s.close(); self.s = InvocationLedger(self.root/'state.sqlite'); self.tier = 'high'
        self.inspect = lambda packet: discovery.check_request(self.s, sid, packet)
        out = self.step(out)
        self.assertEqual(out['state'], 'SYNTHESIS', out.get('stop'))
        packet, wire, _ = self.calls[-1]
        pages = json.loads(wire['messages'][1]['content'])['working_memory_unverified']['pages']
        self.assertIn(remembered[0], {p['ref'] for p in pages})
        self.assertEqual(self.s.db.execute('SELECT id,record_json FROM invocation_receipts ORDER BY seq').fetchall()[:len(old)], old)
        self.assertEqual(review_budget.usage(self.s, sid)['used']['tokens'], before['used']['tokens'] + 480)
        self.assertTrue(self.s.verify_invocations(sid))

    def test_mandatory_root_latest_and_focused_note_are_not_dropped_on_capacity_stop(self):
        out = self.step(self.step(self.until('SYNTHESIS'))); sid = self.meta['active']
        before = review_budget.usage(self.s, sid); calls = len(self.calls); captured = []
        original = self.provider; funding = discovery._request_funding
        def small(stage):
            model = original(stage)
            if stage == 'synthesis':
                model.discovery_provider.budget = Budget(7000, 1024, 256)
                model.discovery_provider.context_metadata = {'model_info': {'fixture.context_length': 7000}, '_sutra_runtime_cap': 7000}
            return model
        def observe(*args, **kwargs):
            captured.append(copy.deepcopy(args[4])); return funding(*args, **kwargs)
        self.provider = small
        with patch.object(discovery, '_request_funding', side_effect=observe): paused = self.step(out)
        self.assertEqual(paused['state'], 'PAUSED'); self.assertEqual(paused['stop']['status'], 'context_budget')
        self.assertTrue(captured)
        floor = captured[-1]
        self.assertTrue(floor['discovery_memory_root'])
        self.assertTrue(floor['discovery_notes_unverified'])
        self.assertEqual(floor['discovery_memory_root']['latest_note'], next(
            p['ref'] for p in floor['discovery_memory_pages'] if p['kind']=='working_note_unverified'
            and p['checkpoint']==floor['discovery_previous']))
        self.assertEqual(len(self.calls), calls); self.assertEqual(review_budget.usage(self.s, sid), before)

    def test_exact_counter_failure_stops_before_call_and_receipts_replay_offline(self):
        self.counter = CountService(); self.counter.prompt_tokens = 400
        context_window._counts.clear()
        with patch.object(context_window, '_rpc', self.counter.rpc):
            out = self.step(self.step(self.until('SYNTHESIS'))); sid = self.meta['active']
            before = review_budget.usage(self.s, sid); calls = len(self.calls)
            self.counter.fail_counts = True; context_window._counts.clear(); stopped = self.step(out)
        self.assertEqual(stopped['state'], 'PAUSED'); self.assertEqual(stopped['stop']['status'], 'resource_wait')
        self.assertEqual(len(self.calls), calls); self.assertEqual(review_budget.usage(self.s, sid), before)
        self.s.close(); self.s = InvocationLedger(self.root/'state.sqlite')
        with patch.object(context_window, '_rpc', side_effect=AssertionError('Offline proof replay must not count')):
            self.assertTrue(self.s.verify_invocations(sid))

    def test_gateway_rejects_missing_forged_reordered_or_typed_root_pages_without_new_charge(self):
        out = self.until('SYNTHESIS')
        for _ in range(2): out = self.step(out)
        sid = self.meta['active']; checked = []
        def inspect(packet):
            discovery.check_request(self.s, sid, packet)
            before = review_budget.usage(self.s, sid)
            for change in ('root', 'missing_leaf', 'order', 'number_type', 'version_type', 'foreign_ref'):
                p = copy.deepcopy(packet)
                if change == 'root': p['discovery_memory_root']['ref'] = '0'*64
                elif change == 'missing_leaf': p['discovery_memory_pages'].pop()
                elif change == 'order': p['discovery_memory_pages'].reverse()
                elif change == 'number_type': p['discovery_memory_root']['notes'] = float(p['discovery_memory_root']['notes'])
                elif change == 'version_type': p['discovery_selection_version'] = 3.0
                else: p['discovery_memory_root']['links'][0]['ref'] = 'f'*64
                with self.subTest(change=change), self.assertRaises((InvalidUpdate, PageFault)):
                    discovery.check_request(self.s, sid, p)
            self.assertEqual(review_budget.usage(self.s, sid), before); checked.append(True)
        self.inspect = inspect; self.step(out); self.assertEqual(checked, [True])

    def test_quarantined_journal_or_changed_source_cannot_be_recalled(self):
        out = self.until('SYNTHESIS')
        for _ in range(2): out = self.step(out)
        sid = self.meta['active']; checked = []
        def inspect(packet):
            discovery.check_request(self.s, sid, packet); before = review_budget.usage(self.s, sid)
            source = (self.ws / 'extension.js').read_text()
            (self.ws / 'extension.js').write_text('function replaced() {}\n')
            with self.assertRaises((InvalidUpdate, PageFault)): discovery.check_request(self.s, sid, packet)
            (self.ws / 'extension.js').write_text(source)
            self.assertEqual(review_budget.usage(self.s, sid), before); checked.append(True)
        self.inspect = inspect; self.step(out); self.assertEqual(checked, [True])
        packet = self.calls[-1][0]; accepted, _ = self.history(packet); before = review_budget.usage(self.s, sid)
        self.s.quarantine_invocation(sid, accepted[0][0], 'Fixture independently rejected this checkpoint')
        with self.assertRaises((InvalidUpdate, PageFault)):
            discovery._accepted(self.s, sid, discovery.history(self.s, sid, packet['discovery_key']))
        self.assertEqual(review_budget.usage(self.s, sid), before)

    def test_unflagged_historical_packet_keeps_old_wire_receipt_and_offline_replay(self):
        with patch.object(discovery, 'MEMORY_VERSION', 0):
            out = self.step(self.until('SYNTHESIS')); self.step(out)
        sid = self.meta['active']; packet, original_wire, _ = self.calls[0]
        self.assertNotIn('discovery_memory_version', packet)
        self.assertEqual(model_packet.build(role.SYSTEM, packet, role.schema(packet)), original_wire)
        self.assertNotIn('working_memory_unverified', json.loads(original_wire['messages'][1]['content']))
        self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite')
        with patch('context_window._rpc', side_effect=AssertionError('Offline receipt replay must not count remotely')):
            self.assertTrue(self.s.verify_invocations(sid))


if __name__ == '__main__': unittest.main()
