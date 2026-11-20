"""Coherent source pages keep byte-exact receipts and independently usable units."""
import copy
import json
import unittest
from unittest.mock import patch

from context_manager import ContextBudgetExceeded, InvalidUpdate, canonical, sha
from invocation_gateway import InvocationLedger
from lifecycle import Interrupted
from monitoring import ResourceWait
from paging import Budget
import orchestration as machine
import orchestration_flow as flow
import repository_tools as repo
import review_budget
import review_discovery as discovery
import test_architecture as architecture_fixture
import test_discovery_selection as selection_fixture
import test_orchestration as fixture


class SourcePageTests(unittest.TestCase):
    def test_host_profile_uses_committed_budget_and_hard_bounds(self):
        self.assertEqual(repo.discovery_read_profile(vars(Budget(65536, 2048, 256))),
                         {'version': 1, 'max_bytes': 16384, 'max_lines': 256})
        self.assertEqual(repo.discovery_read_profile(vars(Budget(8192, 1024, 256)))['max_bytes'], 2048)
        self.assertEqual(repo.discovery_read_profile(vars(Budget(8, 1, 1)))['max_bytes'], 4)

    def test_utf8_long_line_pages_have_exact_independent_contiguous_positions(self):
        text = 'α🙂' * 6000 + '\n' + 'return value\n' * 400
        profile = repo.discovery_read_profile(vars(Budget(65536, 2048, 256)))
        line, column, combined = 1, 0, ''
        for _ in range(12):
            units, cursor = repo.read_page(text, line, column, profile)
            self.assertTrue(units)
            page = ''.join(unit['text'] for unit in units)
            self.assertLessEqual(len(page.encode()), profile['max_bytes'])
            self.assertLessEqual(page.count('\n'), 256)
            for unit in units:
                self.assertLessEqual(len(unit['text'].encode()), 1200)
                self.assertTrue(unit['text'])
                lines = text.splitlines(True)
                offset = sum(map(len, lines[:unit['line']-1])) + unit['column']
                self.assertEqual(text[offset:offset+len(unit['text'])], unit['text'])
                self.assertEqual(unit['line_complete'], unit['end_position'] is None or unit['end_position']['column'] == 0)
            for before, after in zip(units, units[1:]):
                self.assertEqual(before['end_position'], {'line': after['line'], 'column': after['column']})
            combined += page
            if cursor is None: break
            line, column = cursor['line'], cursor['column']
        else: self.fail('Bounded fixture never reached EOF')
        self.assertEqual(combined, text)

    def test_page_size_changes_preserve_exact_cursor_and_256_line_cap(self):
        text = ''.join(f'line {i:04} actual source\n' for i in range(700))
        small = {'version': 1, 'max_bytes': 1200, 'max_lines': 256}
        large = {'version': 1, 'max_bytes': 16384, 'max_lines': 256}
        initial, cursor = repo.read_page(text, 1, 0, small)
        following, _ = repo.read_page(text, cursor['line'], cursor['column'], large)
        actual = ''.join(e['text'] for e in initial + following)
        self.assertTrue(text.startswith(actual)); self.assertGreater(len(actual.encode()), 1200)
        lines, next_position = repo.read_page('x\n' * 700, 1, 0, large)
        self.assertEqual(''.join(e['text'] for e in lines), 'x\n' * 256)
        self.assertEqual(next_position, {'line': 257, 'column': 0})


class LegacyReadTests(unittest.TestCase):
    setUp = architecture_fixture.ArchitectureTests.setUp
    tearDown = architecture_fixture.ArchitectureTests.tearDown
    tool = architecture_fixture.ArchitectureTests.tool

    def test_absent_profile_keeps_legacy_result_and_replay_hashes(self):
        text = 'é' * 900 + '\n' + 'legacy source\n' * 80
        (self.ws / 'entry.py').write_text(text)
        result = self.tool('read', target='entry.py', request_id='legacy-read')
        op = result['operation']; outcome = result['result']; body, end = repo.read_excerpt(text, 1)
        self.assertNotIn('read_profile', op)
        self.assertEqual(set(outcome), {'status', 'excerpts', 'versions', 'truncated', 'scanned_files', 'next_line', 'next_cursor'})
        self.assertEqual(set(outcome['excerpts'][0]), {'path', 'line', 'column', 'text', 'sha256', 'id'})
        self.assertEqual(outcome['excerpts'][0]['text'], body)
        self.assertEqual(outcome['next_cursor'], dict(end, sha256=outcome['versions']['entry.py']))
        raw, digest = self.s.db.execute('SELECT record_json,digest FROM repository_ops WHERE id=?', (op['id'],)).fetchone()
        receipt = self.s.db.execute('SELECT record_json FROM repository_receipts WHERE id=?', (result['id'],)).fetchone()[0]
        self.assertEqual(digest, sha('repository-op-v1:' + raw))
        self.assertEqual(result['id'], sha('repository-receipt-v1:' + receipt))
        replay = self.tool('read', target='entry.py', request_id='legacy-read')
        self.assertEqual(replay['id'], result['id']); self.assertTrue(replay['replayed'])
        self.assertEqual(canonical(replay['result']), canonical(outcome))
        with self.assertRaisesRegex(InvalidUpdate, 'changed read profile'):
            self.tool('read', target='entry.py', request_id='legacy-read',
                      read_profile={'version': 1, 'max_bytes': 1200, 'max_lines': 256})


class GovernedReadTests(unittest.TestCase):
    tearDown = fixture.OrchestrationTests.tearDown
    step = fixture.OrchestrationTests.step
    until = fixture.OrchestrationTests.until
    provider = selection_fixture.SelectionWireTests.provider

    def setUp(self):
        selection_fixture.SelectionWireTests.setUp(self)
        self.source = ''.join(f'    value += {i}  # coherent real source body {i:04}\n' for i in range(500))
        (self.ws / 'part0.py').write_text('def request_flow():\n' + self.source + '    return value\n')

    def accepted_gap(self):
        ready = self.until('SYNTHESIS'); execute = repo.execute
        def stop(*args, **kwargs):
            if kwargs.get('invocation_id'): raise Interrupted('Accepted plan before repository tool')
            return execute(*args, **kwargs)
        with patch.object(repo, 'execute', side_effect=stop): paused = self.step(ready)
        self.assertEqual(paused['state'], 'PAUSED', paused)
        packet = self.calls[0][0]; sid = self.meta['active']
        accepted = discovery._accepted(self.s, sid, discovery.history(self.s, sid, packet['discovery_key']))
        self.assertEqual(len(accepted), 1)
        iid, request, proposal = accepted[0]
        self.assertEqual(request['discovery_selection_version'], 3)
        return paused, iid, request, proposal

    def execute_accepted(self, paused, iid, packet, proposal, **changes):
        tool = proposal['tool']; args = dict(target=tool['target'], query=tool['query'], line=tool['line'],
            cursor=tool.get('cursor'), invocation_id=iid, request_id=discovery._request_id(iid),
            resource_check=False, read_profile=packet['read_profile'])
        args.update(changes)
        owner = flow.info(self.s, paused['flow_id'])[1]['root_node']
        with machine.scope(owner):
            return repo.execute(self.s, self.meta['active'], self.ws, self.root, tool['kind'], **args)

    def test_accepted_profile_reload_then_small_context_projection_preserves_full_receipt(self):
        paused, iid, packet, proposal = self.accepted_gap(); sid = self.meta['active']
        before = review_budget.usage(self.s, sid); contract = self.s.pinned(sid)
        self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite'); self.tier = 'high'
        result = self.execute_accepted(paused, iid, packet, proposal)
        units = result['result']['excerpts']; full = ''.join(e['text'] for e in units)
        self.assertGreater(len(full.encode()), 1200); self.assertGreater(len(units), 1)
        self.assertLessEqual(len(full.encode()), packet['read_profile']['max_bytes'])
        self.assertEqual(review_budget.usage(self.s, sid)['used']['tokens'], before['used']['tokens'])
        self.assertEqual(review_budget.usage(self.s, sid)['used']['calls'], before['used']['calls'])
        self.assertEqual(result['operation']['task_cost']['source_bytes'], (self.ws / 'part0.py').stat().st_size)
        self.assertEqual(review_budget.usage(self.s, sid)['used']['operations'], before['used']['operations'] + 1)
        self.assertEqual(self.s.pinned(sid), contract)
        rows = self.s.db.execute('SELECT record_json FROM repository_receipts WHERE operation_id=?',
                                (result['operation']['id'],)).fetchone()
        available = repo.evidence(self.s, sid, self.ws)
        projection = [dict(available[e['id']]) for e in units[-2:]]
        repo.validate_evidence(self.s, sid, self.ws, projection)
        # A smaller consumer can retain a contiguous suffix of earned units,
        # with no repository read, new model invocation, or receipt mutation.
        budget = Budget(4096, 512, 256)
        with self.assertRaises(ContextBudgetExceeded):
            budget.check('Inspected source', {'evidence': [available[e['id']] for e in units]}, {})
        with patch.object(repo, 'execute', side_effect=AssertionError('Must not re-read earned source')):
            while True:
                try: budget.check('Inspected source', {'evidence': projection}, {}); break
                except ContextBudgetExceeded: projection.pop(0)
            repo.validate_evidence(self.s, sid, self.ws, projection)
        self.assertTrue(projection); self.assertEqual(projection[0]['line'], units[-len(projection)]['line'])
        self.assertEqual(rows, self.s.db.execute('SELECT record_json FROM repository_receipts WHERE operation_id=?',
                                               (result['operation']['id'],)).fetchone())
        self.assertEqual(self.execute_accepted(paused, iid, packet, proposal)['id'], result['id'])
        self.assertEqual(len(self.calls), 1); self.assertTrue(self.s.verify_invocations(sid))

    def test_profile_cannot_be_model_supplied_widened_or_unbound(self):
        paused, iid, packet, proposal = self.accepted_gap(); sid = self.meta['active']
        before = review_budget.usage(self.s, sid)
        for profile in (None, dict(packet['read_profile'], max_bytes=16385),
                        dict(packet['read_profile'], max_lines=257), dict(packet['read_profile'], version=True)):
            with self.subTest(profile=profile), self.assertRaisesRegex(InvalidUpdate, 'Read profile'):
                self.execute_accepted(paused, iid, packet, proposal, read_profile=profile)
        with self.assertRaises(InvalidUpdate):
            repo.execute(self.s, sid, self.ws, self.root, 'read', target='part0.py',
                         read_profile=packet['read_profile'], resource_check=False)
        with self.assertRaises(InvalidUpdate):
            self.execute_accepted(paused, iid, packet, proposal, target='../private.py')
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertFalse(any(r['operation'].get('invocation_id') for r in repo.records(self.s, sid)))

    def test_resource_denial_does_not_spend_and_fresh_read_uses_actual_source_hash(self):
        paused, iid, packet, proposal = self.accepted_gap(); sid = self.meta['active']
        before = review_budget.usage(self.s, sid)
        with patch.object(repo, 'require_resources', side_effect=ResourceWait({'reason': 'Fixture resource wait'})):
            with self.assertRaises(ResourceWait):
                self.execute_accepted(paused, iid, packet, proposal, resource_check=True)
        self.assertEqual(review_budget.usage(self.s, sid), before)
        (self.ws / 'part0.py').write_text('changed source version\n')
        result = self.execute_accepted(paused, iid, packet, proposal)
        import hashlib
        self.assertEqual(result['result']['excerpts'][0]['text'], 'changed source version\n')
        self.assertEqual(result['result']['versions']['part0.py'], hashlib.sha256((self.ws / 'part0.py').read_bytes()).hexdigest())
        self.assertEqual(review_budget.usage(self.s, sid)['used']['tokens'], before['used']['tokens'])
        self.assertEqual(review_budget.usage(self.s, sid)['used']['calls'], before['used']['calls'])

    def test_governed_continuation_rejects_changed_utf8_source_without_fabricated_span(self):
        text = 'def request_flow():\n    value = "' + 'α🙂' * 6000 + '"\n    return value\n'
        (self.ws / 'part0.py').write_text(text)
        paused, iid, packet, proposal = self.accepted_gap()
        first = self.execute_accepted(paused, iid, packet, proposal)
        cursor = first['result']['next_cursor']; self.assertIsNotNone(cursor)
        self.actions = [{'kind': 'read', 'target': 'part0.py'},
                        {'kind': 'read', 'target': 'part0.py', 'line': cursor['line'], 'cursor': cursor}]
        resumed = self.step(paused, event='resume'); execute = repo.execute
        def stop(*args, **kwargs):
            if kwargs.get('invocation_id'): raise Interrupted('Second accepted decision before changed source')
            return execute(*args, **kwargs)
        with patch.object(repo, 'execute', side_effect=stop): stopped = self.step(resumed)
        self.assertEqual(stopped['state'], 'PAUSED')
        second_packet = self.calls[-1][0]; sid = self.meta['active']
        accepted = discovery._accepted(self.s, sid, discovery.history(self.s, sid, second_packet['discovery_key']))
        second_id, second_packet, second_proposal = accepted[-1]
        before = review_budget.usage(self.s, sid)
        (self.ws / 'part0.py').write_text(text.replace('α', 'β', 1))
        with self.assertRaisesRegex(InvalidUpdate, 'evidence is stale'):
            self.execute_accepted(stopped, second_id, second_packet, second_proposal)
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertFalse(any(r['operation'].get('invocation_id') == second_id for r in repo.records(self.s, sid)))
        self.assertEqual(len(self.calls), 2)

    def completed_projection(self, packet):
        sid = self.meta['active']; key = packet['discovery_key']
        accepted = discovery._accepted(self.s, sid, discovery.history(self.s, sid, key))
        receipts = discovery._tool_receipts(self.s, sid, accepted)
        p = copy.deepcopy(packet)
        return discovery._finish(self.s, sid, p, key, accepted, receipts, 'budget_boundary')['followups']

    def test_discovery_projection_rejoins_adjacent_utf8_units_into_exact_long_line(self):
        text = 'def request_flow():\n    value = "' + 'α🙂' * 1500 + '"\n    return value\n'
        (self.ws / 'part0.py').write_text(text)
        paused, iid, packet, proposal = self.accepted_gap()
        result = self.execute_accepted(paused, iid, packet, proposal)
        self.assertGreater(len(result['result']['excerpts']), 2)
        full = ''.join(e['text'] for e in result['result']['excerpts'])
        projected = self.completed_projection(packet)['part0.py']
        self.assertEqual(full, text); self.assertEqual(projected['text'], text)
        self.assertEqual((projected['line'], projected['column']), (1, 0))
        self.assertIsNone(projected['end_position']); self.assertTrue(projected['line_complete'])

    def test_discovery_projection_never_joins_nonadjacent_source_windows(self):
        self.actions = [{'kind': 'read', 'target': 'part0.py', 'line': 1},
                        {'kind': 'read', 'target': 'part0.py', 'line': 450}]
        paused, iid, packet, proposal = self.accepted_gap()
        first = self.execute_accepted(paused, iid, packet, proposal)
        self.assertLess(first['result']['next_cursor']['line'], 450)
        self.step(self.step(paused, event='resume'))
        self.assertEqual(len(self.calls), 2)
        followups = self.completed_projection(self.calls[-1][0])
        expected = ''.join((self.ws / 'part0.py').read_text().splitlines(True)[449:])
        self.assertEqual(followups['part0.py']['text'], expected)
        self.assertEqual(followups['part0.py']['line'], 450)


if __name__ == '__main__': unittest.main()
