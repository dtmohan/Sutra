"""Delivery history stays visible without claiming semantic review progress."""
import copy
import json
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate, canonical
from invocation_gateway import InvocationLedger
import architecture_role as role
import model_packet
import review_discovery as discovery
from static_runtime import load_partition
import test_discovery_selection as fixture


class DiscoveryProgressTests(unittest.TestCase):
    setUp = fixture.SelectionWireTests.setUp
    tearDown = fixture.SelectionWireTests.tearDown
    provider = fixture.SelectionWireTests.provider
    step = fixture.SelectionWireTests.step
    until = fixture.SelectionWireTests.until
    discover = fixture.SelectionWireTests.discover

    def read_history(self):
        sid = self.meta['active']; packet = self.calls[-1][0]
        accepted = discovery._accepted(self.s, sid, discovery.history(self.s, sid, packet['discovery_key']))
        receipts = discovery._tool_receipts(self.s, sid, accepted)
        return sid, accepted, receipts

    @patch.object(discovery, 'MEMORY_VERSION', 0)
    def test_supplied_paths_survive_note_and_old_evidence_retirement_without_banning_rereads(self):
        self.actions = [{'kind': 'read', 'target': 'part0.py'}, {'kind': 'read', 'target': 'part1.py'},
                        {'kind': 'read', 'target': 'part0.py'}, {'kind': 'clarify'}]
        self.discover(); sid, accepted, receipts = self.read_history()
        packet, wire, _ = self.calls[-1]
        self.assertEqual(packet['discovery_note_version'], 2)
        progress = packet['supplied_source_progress']
        self.assertEqual(progress['total_paths'], 2)
        self.assertEqual({row['path'] for row in progress['paths']}, {'part0.py', 'part1.py'})
        self.assertIn('not reviewed or understood', progress['meaning'])
        body = json.loads(wire['messages'][-1]['content'])
        self.assertEqual(body['supplied_source_progress'], progress)
        self.assertNotIn('discovery_progress_bytes', body)
        # The reread was admitted and its new page became the active source.
        self.assertEqual(packet['active_source_page']['path'], 'part0.py')
        complete = discovery.supplied_source_progress(self.s, sid, self.ws, receipts, accepted, 1024)
        self.assertEqual(complete['read_pages'], 3)
        self.assertEqual(complete['overlapping_read_pages'], 1)
        before = copy.deepcopy(complete)
        self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite'); self.tier = 'high'
        self.assertEqual(discovery.supplied_source_progress(self.s, sid, self.ws, receipts, accepted, 1024), before)

        # Exercise the fitter all the way to its protected floors. Neither the
        # current source prefix nor the fact of earlier delivery may disappear.
        prior = accepted[:-1]
        older, active, prefix, pages = discovery._page_working_set(self.s, sid, self.ws, receipts, prior)
        data = load_partition(self.s, sid, self.ws, packet['snapshot_receipts'], packet['snapshot_owner'])
        rows = discovery._supplied_progress_rows(self.s, sid, self.ws, receipts, prior)
        trimmed = copy.deepcopy(packet)
        while discovery._shrink_optional(trimmed, data, packet['request'], '', active, prefix, pages, rows): pass
        self.assertEqual(trimmed['discovery_notes_unverified'], [])
        self.assertEqual(trimmed['evidence'], prefix)
        self.assertEqual(trimmed['discovery_progress_bytes'], 512)
        self.assertEqual(trimmed['supplied_source_progress']['total_paths'], 2)
        self.assertLessEqual(len(canonical(trimmed['supplied_source_progress']).encode()), 512)
        self.assertTrue(self.s.verify_invocations(sid))

    def test_gateway_request_rederives_progress_and_rejects_forged_or_malformed_profiles(self):
        checked = []
        def inspect(_, packet):
            sid = self.meta['active']
            discovery.check_request(self.s, sid, packet)
            for marker in ('discovery_note_version', 'discovery_progress_version'):
                for invalid in (True, 2.0, 99):
                    changed = copy.deepcopy(packet); changed[marker] = invalid
                    with self.assertRaisesRegex(InvalidUpdate, 'profile'):
                        discovery.check_request(self.s, sid, changed)
            for invalid in (511, 2049, True, 1024.0):
                changed = copy.deepcopy(packet); changed['discovery_progress_bytes'] = invalid
                with self.assertRaisesRegex(InvalidUpdate, 'allocation'):
                    discovery.check_request(self.s, sid, changed)
            changed = copy.deepcopy(packet)
            changed['supplied_source_progress']['total_paths'] += 1
            with self.assertRaisesRegex(InvalidUpdate, 'progress changed'):
                discovery.check_request(self.s, sid, changed)
            if packet['supplied_source_progress']['paths']:
                changed = copy.deepcopy(packet)
                changed['supplied_source_progress']['paths'][0]['path'] = 'never-supplied.py'
                with self.assertRaisesRegex(InvalidUpdate, 'progress changed'):
                    discovery.check_request(self.s, sid, changed)
            checked.append(packet['discovery_previous'])
        self.fault = inspect
        self.discover()
        self.assertEqual(len(checked), 3)
        self.assertTrue(self.s.verify_invocations(self.meta['active']))

    def test_rejected_call_does_not_retire_or_certify_its_supplied_page(self):
        self.actions = [{'kind': 'read', 'target': 'part0.py'}, {'kind': 'read', 'target': 'part1.py'},
                        {'kind': 'read', 'target': 'part0.py'}, {'kind': 'clarify'}]
        def reject_second(raw, _):
            if len(self.calls) == 1: raw.update(summary='x' * 1600, remaining=[])
        self.fault = reject_second
        self.discover()
        third = self.calls[2][0]
        self.assertEqual(third['supplied_source_progress']['total_paths'], 0)
        self.assertEqual(third['active_source_page']['path'], 'part0.py')
        sid, accepted, receipts = self.read_history()
        progress = discovery.supplied_source_progress(self.s, sid, self.ws, receipts, accepted, 1024)
        self.assertEqual([row['path'] for row in progress['paths']], ['part0.py'])
        self.assertFalse(any(record['operation']['target'] == 'part1.py' for record in receipts))


class ProgressPackingTests(unittest.TestCase):
    def test_receipt_and_eof_positions_match_cr_unicode_and_split_crlf_paging(self):
        def unit(eid, receipt, line, column, text, end):
            return {'id': eid, 'receipt': receipt, 'path': 'lines.py', 'line': line, 'column': column,
                    'text': text, 'sha256': 'a' * 64, 'end_position': end, 'line_complete': False}
        def rows(entries):
            receipts = [{'id': entry['receipt'], 'operation': {'kind': 'read'}} for entry in entries]
            accepted = [('accepted', {'evidence': entries}, {})]
            with patch.object(discovery, '_entries', return_value=entries):
                return discovery._supplied_progress_rows(None, None, None, receipts, accepted)
        for separator in ('\r', '\r\n', '\n', '\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029'):
            with self.subTest(separator=repr(separator)):
                text = 'a' + separator + 'b'
                explicit = unit('explicit', 'r1', 7, 3, text, {'line': 8, 'column': 1})
                self.assertEqual(rows([explicit])[0]['spans'][0][1], (8, 1))
                eof = dict(explicit, end_position=None)
                self.assertEqual(rows([eof])[0]['spans'][0][1], (8, 1))
                trailing = dict(eof, text='a' + separator)
                self.assertEqual(rows([trailing])[0]['spans'][0][1], (8, 0))
        # A boundary within CRLF stays in the original physical source line.
        left = unit('left', 'r1', 1, 0, 'a\r', {'line': 1, 'column': 2})
        self.assertEqual(rows([left])[0]['spans'][0][1], (1, 2))
        right = unit('right', 'r2', 1, 2, '\nb', None)
        merged = rows([left, right])[0]['spans']
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0][:3], ((1, 0), (2, 1), True))
        # The undisplayed character at line2:1 must remain a real gap.
        before = unit('before', 'r1', 1, 0, 'a\rb', {'line': 2, 'column': 1})
        after = unit('after', 'r2', 2, 2, 'x', None)
        self.assertEqual(len(rows([before, after])[0]['spans']), 2)

    def test_exact_unicode_positions_disjoint_spans_repeats_and_byte_bound(self):
        def entry(eid, receipt, line, text, end):
            return {'id': eid, 'receipt': receipt, 'path': 'unicode.py', 'line': line, 'column': 0,
                    'text': text, 'sha256': 'a' * 64, 'end_position': end, 'line_complete': True}
        one = entry('one', 'r1', 1, '😀\n', {'line': 2, 'column': 0})
        two = entry('two', 'r2', 5, 'tail😀', None)
        repeat = entry('repeat', 'r3', 1, '😀\n', {'line': 2, 'column': 0})
        partial = dict(two, text='tail')
        records = [{'id': rid, 'operation': {'kind': 'read'}} for rid in ('r1', 'r2', 'r3')]
        accepted = [('i1', {'evidence': [one, partial]}, {}),
                    ('i2', {'evidence': [one, two]}, {}), ('i3', {'evidence': [repeat]}, {})]
        with patch.object(discovery, '_entries', return_value=[one, two, repeat]):
            for limit in (512, 1024, 2048):
                progress = discovery.supplied_source_progress(None, None, None, records, accepted, limit)
                self.assertLessEqual(len(canonical(progress).encode()), limit)
                self.assertEqual(progress['total_spans'], 2)
                self.assertEqual(progress['read_pages'], 3)
                self.assertEqual(progress['overlapping_read_pages'], 1)
            spans = progress['paths'][0]['spans']
            self.assertEqual([span['start_line'] for span in spans], [1, 5])
            self.assertEqual(spans[0]['end_position'], {'line': 2, 'column': 0})
            self.assertIsNone(spans[1]['end_position'])
            excluded = discovery.supplied_source_progress(None, None, None, records,
                [('i0', {'evidence': [partial]}, {})], 1024)
            self.assertEqual(excluded['total_paths'], 0)

    def test_many_paths_and_sparse_ranges_are_honestly_bounded(self):
        rows = [{'path': ('module_' + str(n) + '_😀.py'), 'spans': [((1, 0), (2, 0), False, n),
                 ((100, 0), (101, 0), False, n)], 'recent': n,
                 'read_pages': 2, 'overlapping_read_pages': 0} for n in range(100)]
        for limit in (512, 1024, 2048):
            progress = discovery._progress_view(rows[::-1], limit)
            self.assertLessEqual(len(canonical(progress).encode()), limit)
            self.assertEqual(progress['total_paths'], 100)
            self.assertEqual(progress['omitted_paths'], 100 - len(progress['paths']))
            self.assertEqual(progress['omitted_spans'], 200 - sum(len(p['spans']) for p in progress['paths']))
        for invalid in (511, 2049, True, 1024.0):
            with self.assertRaises(InvalidUpdate): discovery._progress_view(rows, invalid)


if __name__ == '__main__': unittest.main()
