"""Re-supply exact archived source; journal metadata is not a source body."""
import copy
import json
import os
import unittest
from unittest.mock import patch

from context_manager import ContextBudgetExceeded, InvalidUpdate, canonical, sha
from invocation_gateway import InvocationLedger
from paging import Budget, PageFault
import architecture_role as role
import context_window
import model_packet
import repository_tools as repo
import review_budget
import review_discovery as discovery
import test_discovery_memory as fixture
from test_context_window_recovery import CountService


class SourceRecallTests(unittest.TestCase):
    tearDown = fixture.MemoryGatewayTests.tearDown
    step = fixture.MemoryGatewayTests.step
    until = fixture.MemoryGatewayTests.until
    provider = fixture.MemoryGatewayTests.provider
    history = fixture.MemoryGatewayTests.history
    fifteen = fixture.MemoryGatewayTests.fifteen

    def setUp(self):
        fixture.MemoryGatewayTests.setUp(self)
        self.source = 'harness/capabilities.py'
        # The long function and extra source index raise the mandatory byte
        # floor; both legacy and new recipes use this same finite window.
        self.tight = 28672
        (self.ws / self.paths[0]).unlink()
        self.paths[0] = self.source
        preamble = ''.join('# preamble %03d\n' % n for n in range(163))
        body = ''.join('    # unrelated setup %03d %s\n' % (n, 'padding ' * 8) for n in range(85))
        text = preamble + 'def execute(request):\n' + body
        self.admit_line = len(text.splitlines()) + 1
        text += '    gate = admit(request)  # requested authorization gate and receipt reservation\n'
        text += '    return receipt(gate)\n'
        (self.ws / self.source).write_text(text)
        sequence = self.paths + self.paths[1:7]
        self.actions = [{'kind': 'read', 'target': path, **({'line': 164} if path == self.source else {})}
                        for path in sequence] + [{'kind': 'clarify'}]
        def notes(raw, packet):
            if len(self.calls) == 1:
                raw['summary'] = 'Earlier capabilities.execute source contains an authorization gate and receipt reservation.'
            if len(self.calls) == 14:
                raw['summary'] = 'Need the saved capabilities authorization gate body.'
                raw['remaining'] = ['Recall harness/capabilities.py admit() authorization gate and receipt reservation.']
        self.extra_output = notes

    def saved_prefix(self):
        # These are real accepted pre-profile packets, not rewritten receipts.
        with patch.object(discovery, 'SOURCE_RECALL_VERSION', 0): out = self.fifteen()
        self.assertEqual(len(self.calls), 15)
        return out

    def next_call(self, out):
        before = len(self.calls)
        for _ in range(8):
            out = self.step(out)
            self.assertEqual(out['state'], 'SYNTHESIS', out.get('stop'))
            if len(self.calls) > before: return out
        self.fail('No governed selector call after bounded host transitions')

    def test_reproduces_metadata_recall_with_evicted_capabilities_body_without_new_profile(self):
        out = self.saved_prefix()
        with patch.object(discovery, 'SOURCE_RECALL_VERSION', 0): self.next_call(out)
        packet, wire, _ = self.calls[-1]
        self.assertNotIn(self.source, {entry['path'] for entry in packet['evidence']})
        pages = json.loads(wire['messages'][1]['content'])['working_memory_unverified']['pages']
        self.assertTrue(any(page['kind'] == 'source_page_index' and page['path'] == self.source for page in pages))
        self.assertTrue(any(page['kind'] == 'working_note_unverified' and
                            'capabilities' in page['note']['summary'] for page in pages))

    def test_reload_model_switch_pages_in_later_admit_unit_without_repository_effect_or_refund(self):
        out = self.saved_prefix(); sid = self.meta['active']
        accepted, source = self.history(self.calls[-1][0])
        original_units = {entry['id']: entry for entry in discovery._entries(self.s, sid, self.ws, source)}
        old = self.s.db.execute('SELECT id,record_json FROM invocation_receipts ORDER BY seq').fetchall()
        operations = self.s.db.execute('SELECT id,record_json FROM repository_receipts ORDER BY rowid').fetchall()
        before = review_budget.usage(self.s, sid); contract = self.s.pinned(sid)
        self.s.close(); self.s = InvocationLedger(self.root/'state.sqlite'); self.tier = 'high'
        with patch.object(repo, 'execute', side_effect=AssertionError('Receipt page-in is not a repository tool')):
            self.next_call(out)
        packet, wire, _ = self.calls[-1]
        required = set(packet['discovery_source_recall']['required_unit_ids'])
        recalled = [entry for entry in packet['evidence'] if entry['path'] == self.source and entry['id'] in required]
        self.assertEqual(len(recalled), 1)
        self.assertIn('gate = admit(request)', recalled[0]['text'])
        self.assertGreater(recalled[0]['line'], 164)  # The header is not the requested mechanism.
        self.assertEqual(recalled[0], original_units[recalled[0]['id']])
        body = json.loads(wire['messages'][1]['content'])
        projected = next(entry for entry in body['evidence'] if entry['id'] == recalled[0]['id'])
        self.assertEqual(projected['text'], recalled[0]['text'])
        self.assertEqual(projected['column'], recalled[0].get('column', 0))
        self.assertEqual(self.s.pinned(sid), contract)
        self.assertEqual(self.s.db.execute('SELECT id,record_json FROM invocation_receipts ORDER BY seq').fetchall()[:len(old)], old)
        self.assertEqual(self.s.db.execute('SELECT id,record_json FROM repository_receipts ORDER BY rowid').fetchall(), operations)
        after = review_budget.usage(self.s, sid)
        self.assertEqual(after['used']['tokens'], before['used']['tokens'] + 480)
        self.assertEqual(after['used']['source_bytes'], before['used']['source_bytes'])
        self.assertEqual(after['used']['operations'], before['used']['operations'])
        self.assertTrue(self.s.verify_invocations(sid))

    def test_gateway_rejects_foreign_forged_missing_and_reordered_recall_floors(self):
        out = self.saved_prefix(); sid = self.meta['active']; checked = []
        def inspect(packet):
            discovery.check_request(self.s, sid, packet)
            before = review_budget.usage(self.s, sid)
            required = packet['discovery_source_recall']['required_unit_ids']
            for kind in ('missing', 'order', 'descriptor', 'version', 'key'):
                changed = copy.deepcopy(packet)
                if kind == 'missing':
                    changed['evidence'] = [entry for entry in changed['evidence'] if entry['id'] != required[0]]
                    changed['source_paths'] = sorted({entry['path'] for entry in changed['evidence']})
                elif kind == 'order': changed['evidence'].reverse()
                elif kind == 'descriptor': changed['discovery_source_recall']['required_unit_ids'][0] = 'foreign-unit'
                elif kind == 'version': changed['discovery_source_recall_version'] = 1.0
                else: changed['discovery_key'] = 'f'*64
                with self.subTest(kind=kind), self.assertRaises((InvalidUpdate, PageFault)):
                    discovery.check_request(self.s, sid, changed)
            with self.assertRaises((InvalidUpdate, PageFault)):
                discovery.check_request(self.s, 'foreign-session', packet)
            path = self.ws / self.source; raw = path.read_bytes(); stat = path.stat()
            path.write_bytes(raw.replace(b'admit(request)', b'ADMIT(request)'))
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            try:
                with self.assertRaises((InvalidUpdate, PageFault)): discovery.check_request(self.s, sid, packet)
            finally: path.write_bytes(raw)
            self.assertEqual(review_budget.usage(self.s, sid), before); checked.append(True)
        self.inspect = inspect; self.next_call(out); self.assertEqual(checked, [True])
        packet = self.calls[-1][0]; accepted, _ = self.history(packet)
        before = review_budget.usage(self.s, sid)
        self.s.quarantine_invocation(sid, accepted[0][0], 'Independently withheld archive')
        with self.assertRaises((InvalidUpdate, PageFault)):
            discovery._accepted(self.s, sid, discovery.history(self.s, sid, packet['discovery_key']))
        self.assertEqual(review_budget.usage(self.s, sid), before)

    def test_capacity_stop_keeps_exact_recall_floor_and_fresh_prefix_without_dispatch(self):
        out = self.saved_prefix(); sid = self.meta['active']; before = review_budget.usage(self.s, sid)
        captured = []; original_provider = self.provider; funding = discovery._request_funding
        def small(stage):
            model = original_provider(stage)
            if stage == 'synthesis':
                model.discovery_provider.budget = Budget(9000, 1024, 256)
                model.discovery_provider.context_metadata = {'model_info': {'fixture.context_length': 9000}, '_sutra_runtime_cap': 9000}
            return model
        def observe(*args, **kwargs):
            captured.append(copy.deepcopy(args[4])); return funding(*args, **kwargs)
        self.provider = small
        with patch.object(discovery, '_request_funding', side_effect=observe): paused = self.step(out)
        self.assertEqual(paused['state'], 'PAUSED'); self.assertEqual(paused['stop']['status'], 'context_budget')
        self.assertTrue(captured)
        floor = captured[-1]; ids = {entry['id'] for entry in floor['evidence']}
        self.assertTrue(set(floor['discovery_source_recall']['required_unit_ids']) <= ids)
        prefix = floor['active_source_page']['prefix']
        self.assertEqual([entry['id'] for entry in floor['evidence'][-len(prefix):]], prefix)
        self.assertEqual(len(self.calls), 15); self.assertEqual(review_budget.usage(self.s, sid), before)

    def test_explicit_reread_is_still_admitted_and_charged_as_a_new_effect(self):
        out = self.saved_prefix(); sid = self.meta['active']
        self.actions[-1] = {'kind': 'read', 'target': self.source, 'line': 164}
        before = review_budget.usage(self.s, sid)
        ops = self.s.db.execute('SELECT COUNT(*) FROM repository_ops').fetchone()[0]
        self.next_call(out)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM repository_ops').fetchone()[0], ops + 1)
        self.assertGreater(review_budget.usage(self.s, sid)['used']['source_bytes'], before['used']['source_bytes'])
        self.assertEqual(self.calls[-1][2]['tool']['line'], 164)

    def test_old_absent_flag_wire_and_receipts_replay_without_exact_counter(self):
        with patch.object(discovery, 'SOURCE_RECALL_VERSION', 0):
            out = self.step(self.until('SYNTHESIS')); self.step(out)
        sid = self.meta['active']; packet, wire, _ = self.calls[0]
        self.assertNotIn('discovery_source_recall_version', packet)
        self.assertEqual(model_packet.build(role.SYSTEM, packet, role.schema(packet)), wire)
        self.s.close(); self.s = InvocationLedger(self.root/'state.sqlite')
        with patch.object(context_window, '_rpc', side_effect=AssertionError('Replay must stay offline')):
            self.assertTrue(self.s.verify_invocations(sid))

    def test_exact_counter_failure_stops_before_call_and_receipts_replay_offline(self):
        self.counter = CountService(); self.counter.prompt_tokens = 400
        context_window._counts.clear()
        with patch.object(context_window, '_rpc', self.counter.rpc):
            out = self.step(self.step(self.until('SYNTHESIS'))); sid = self.meta['active']
            before = review_budget.usage(self.s, sid); calls = len(self.calls)
            self.counter.fail_counts = True; context_window._counts.clear(); stopped = self.step(out)
        self.assertEqual(stopped['state'], 'PAUSED'); self.assertEqual(len(self.calls), calls)
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.s.close(); self.s = InvocationLedger(self.root/'state.sqlite')
        with patch.object(context_window, '_rpc', side_effect=AssertionError('Replay must not count')):
            self.assertTrue(self.s.verify_invocations(sid))

    def test_physical_tail_shrink_keeps_recalled_body_and_coherent_fresh_prefix(self):
        out = self.saved_prefix(); sid = self.meta['active']
        class UnitCounter(CountService):
            def rpc(self, profile, method, path, body=None):
                if (method, path) == ('POST', '/v1/messages/count_tokens'):
                    self.calls.append((copy.deepcopy(profile), method, path, copy.deepcopy(body)))
                    user = body['messages'][-1]['content']
                    if isinstance(user, list): user = ''.join(part.get('text', '') for part in user)
                    evidence = json.loads(user).get('evidence', [])
                    # A deterministic synthetic tokenizer, not a claim about
                    # real byte/token ratios or any installed model.
                    self.last_count = 1000 + 1000 * len(evidence)
                    return {'input_tokens': self.last_count}
                return super().rpc(profile, method, path, body)
        self.counter = UnitCounter(); self.tier = 'high'; original = self.provider
        def physical(stage):
            model = original(stage)
            if stage == 'synthesis':
                worker = model.discovery_provider
                worker.budget = worker.budget.with_limits(context_limit=4096)
                call = worker.call
                def measured(*args):
                    result, record = call(*args)
                    record['response']['prompt_eval_count'] = self.counter.last_count
                    return result, record
                worker.call = measured
            return model
        self.provider = physical; context_window._counts.clear()
        with patch.object(context_window, '_rpc', self.counter.rpc), \
                patch.object(repo, 'execute', side_effect=AssertionError('Page-in must not reread')):
            self.next_call(out)
        packet = self.calls[-1][0]
        self.assertTrue(packet['active_source_page']['partial_page'])
        prefix = packet['active_source_page']['prefix']
        self.assertEqual(len(prefix), 1)
        self.assertEqual([entry['id'] for entry in packet['evidence'][-len(prefix):]], prefix)
        required = set(packet['discovery_source_recall']['required_unit_ids'])
        self.assertTrue(required <= {entry['id'] for entry in packet['evidence']})
        self.assertTrue(any(entry['path'] == self.source and 'admit(request)' in entry['text'] for entry in packet['evidence']))
        self.s.close(); self.s = InvocationLedger(self.root/'state.sqlite')
        with patch.object(context_window, '_rpc', side_effect=AssertionError('Offline replay must not count')):
            self.assertTrue(self.s.verify_invocations(sid))


class RecallRankingTests(unittest.TestCase):
    def test_late_literal_and_explicit_line_or_ref_select_whole_units_not_header(self):
        def entry(eid, receipt, line, text, end):
            return {'id': eid, 'receipt': receipt, 'path': 'module.py', 'line': line, 'column': 7,
                    'text': text, 'end_position': end, 'sha256': 'a'*64, 'line_complete': False}
        header = entry('header', 'r1', 164, 'def execute(request):\n    # setup\n', {'line': 166, 'column': 0})
        gate = entry('gate', 'r1', 240, '    gate = admit(request)  # 😀\r    receipt = reserve(gate)', {'line': 241, 'column': 27})
        other = entry('other', 'r2', 300, '    other = admit(request)\n', {'line': 301, 'column': 0})
        packet = {'request': 'Explain the module architecture.',
                  'discovery_memory_pages': [{'kind': 'source_page_index', 'ref': 'ref1', 'path': 'module.py', 'receipt': 'r1'},
                                             {'kind': 'source_page_index', 'ref': 'ref2', 'path': 'module.py', 'receipt': 'r2'}],
                  'active_source_page': None}
        for hint, expected in [('Need module.py admit() gate and receipt.', 'gate'),
                               ('Inspect module.py:240.', 'gate'), ('Recall ref2.', 'other')]:
            accepted = [('last', {}, {'remaining': [hint]})]
            descriptor, required = discovery._source_recall(packet, accepted, [header, gate, other])
            self.assertEqual(descriptor['required_unit_ids'], [expected])
            self.assertEqual(required[0], {'gate': gate, 'other': other}[expected])
        original = copy.deepcopy(gate)
        ordered = discovery._recall_order([header, gate, other], [gate], ['gate', 'header', 'other'], [])
        self.assertEqual(ordered[-1], original)
        self.assertEqual(gate, original)


if __name__ == '__main__': unittest.main()
