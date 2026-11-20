"""Versioned file-scoped literal search through accepted architect decisions."""
import copy
import json
import unittest
from unittest.mock import patch

import architecture_role as role
import model_packet
import orchestration as machine
import repository_tools as repo
import review_budget
import review_discovery as discovery
from context_manager import InvalidUpdate, canonical, sha
from invocation_gateway import InvocationLedger
from lifecycle import Interrupted
from paging import PageFault
from test_discovery_note_budget import payload
import test_discovery_selection as fixture


class ScopedSearchCompatibilityTests(unittest.TestCase):
    def test_unflagged_wire_schema_and_charge_are_exact(self):
        source = payload(2)
        packet = model_packet.build(role.SYSTEM, source, role.schema(source))
        self.assertEqual(sha(canonical(packet)), '1298bdd720e25562276adce6fb6f24dacba204541dab587a238dd580b990a2e1')
        self.assertEqual(sha(canonical(packet['schema'])), '171b83605ec4fbf80baac2a57595f8090073191feb1d45f7d0f83f91d778d415')
        self.assertEqual(model_packet.descriptor(packet, 479)['input_reservation'], 5211)
        flagged = dict(source, discovery_search_version=1)
        scoped = model_packet.build(role.SYSTEM, flagged, role.schema(flagged))
        self.assertEqual(packet['schema'], scoped['schema'])
        self.assertIn('target may be one authorized repository file', scoped['messages'][0]['content'])
        self.assertIn('at most five literal-match snippets', scoped['messages'][0]['content'])
        self.assertEqual(packet, model_packet.build(role.SYSTEM, source, role.schema(source)))


class ScopedSearchGatewayTests(unittest.TestCase):
    tearDown = fixture.SelectionWireTests.tearDown
    provider = fixture.SelectionWireTests.provider
    step = fixture.SelectionWireTests.step
    until = fixture.SelectionWireTests.until
    discover = fixture.SelectionWireTests.discover

    def setUp(self):
        fixture.SelectionWireTests.setUp(self)
        for number in range(6):
            (self.ws / f'aaa{number}.py').write_text('# unrelated caller admit(\n')
        (self.ws / 'part3.py').write_text('# selected source\ndef admit(value):\n    return value\n')
        self.actions = [{'kind': 'search', 'target': 'part3.py', 'query': 'admit('}, {'kind': 'clarify'}]
        self.alter = None
        def model_reply(raw, packet):
            # These are the complete model-facing replies, before binding or
            # role validation. The reusable fixture normalizes old global
            # search targets; restore this model's explicitly chosen target.
            action = self.actions[min(len(self.calls), len(self.actions)-1)]
            if raw['tool']['kind'] == 'search': raw['tool']['target'] = action.get('target', '')
            if self.alter: self.alter(raw, packet)
        self.fault = model_reply

    def tool_records(self):
        return [record for record in repo.current_records(self.s, self.meta['active'])
                if record['operation'].get('invocation_id')]

    def test_scoped_search_finds_definition_after_five_unrelated_global_matches_and_charges_one_file(self):
        self.actions = [{'kind': 'search', 'query': 'admit('},
                        {'kind': 'search', 'target': 'part3.py', 'query': 'admit('},
                        {'kind': 'read', 'target': 'part3.py', 'line': 2}, {'kind': 'clarify'}]
        _, packet = self.discover()
        records = self.tool_records()
        self.assertEqual(len(records), 3)
        global_search, scoped, read = records
        self.assertEqual(len(global_search['result']['excerpts']), 5)
        self.assertTrue(global_search['result']['truncated'])
        self.assertTrue(all(row['path'].startswith('aaa') for row in global_search['result']['excerpts']))
        self.assertEqual(scoped['operation']['target'], 'part3.py')
        self.assertEqual(scoped['operation']['query'], 'admit(')
        self.assertEqual(scoped['operation']['search_version'], 1)
        self.assertEqual(scoped['operation']['task_cost']['source_bytes'], (self.ws / 'part3.py').stat().st_size)
        self.assertEqual(scoped['operation']['task_cost']['operations'], 1)
        self.assertEqual(scoped['operation']['task_cost']['seconds'], 20)
        self.assertEqual(scoped['result']['scanned_files'], 1)
        self.assertEqual([(e['path'], e['line']) for e in scoped['result']['excerpts']], [('part3.py', 2)])
        self.assertIn('def admit(value):', scoped['result']['excerpts'][0]['text'])
        self.assertEqual(read['operation']['line'], 2)
        self.assertEqual(packet['discovery_coverage']['read_files'], ['part3.py'])
        self.assertTrue(self.s.verify_invocations(self.meta['active']))

    def test_unflagged_global_search_and_replay_stay_unchanged(self):
        self.actions = [{'kind': 'search', 'query': 'admit('}, {'kind': 'clarify'}]
        with patch.object(discovery, 'SEARCH_VERSION', None):
            out, _ = self.discover()
        self.assertNotIn('discovery_search_version', self.calls[0][0])
        record = self.tool_records()[0]; op = record['operation']
        self.assertNotIn('search_version', op)
        self.assertEqual(op['target'], '')
        self.assertEqual(len(record['result']['excerpts']), 5)
        before = review_budget.usage(self.s, self.meta['active'])
        with machine.scope(out['execution_id']):
            replay = repo.execute(self.s, self.meta['active'], self.ws, self.root, 'search', query='admit(',
                invocation_id=op['invocation_id'], request_id=op['request_id'], resource_check=False)
        self.assertTrue(replay['replayed'])
        self.assertEqual(review_budget.usage(self.s, self.meta['active']), before)

    def test_unflagged_target_and_model_supplied_capability_do_not_authorize_search(self):
        self.actions = self.actions[:1]
        with patch.object(discovery, 'SEARCH_VERSION', None):
            _, packet = self.discover()
        self.assertEqual(packet['discovery_coverage']['stop_reason'], 'proposal_rejected')
        self.assertEqual(self.tool_records(), [])
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(all('Search requires one bounded literal query' in result['result']['admission']['errors']
                            for _, result in self.captured if 'result' in result))

    def test_traversal_and_uncommitted_target_are_rejected_before_repository_effect(self):
        self.actions = [{'kind': 'search', 'target': '../private.py', 'query': 'admit('}]
        _, packet = self.discover()
        self.assertEqual(packet['discovery_coverage']['stop_reason'], 'proposal_rejected')
        self.assertEqual(self.tool_records(), [])
        self.assertEqual(len(self.calls), 3)

    def test_host_call_cannot_enable_scope_without_an_accepted_selector(self):
        out, _ = self.discover(); sid = self.meta['active']; before = review_budget.usage(self.s, sid)
        count = len(self.tool_records())
        with machine.scope(out['execution_id']):
            for iid in (None, 'not-an-accepted-selector'):
                with self.assertRaises(InvalidUpdate):
                    repo.execute(self.s, sid, self.ws, self.root, 'search', target='part3.py', query='admit(',
                                 invocation_id=iid, resource_check=False)
        self.assertEqual(len(self.tool_records()), count)
        self.assertEqual(review_budget.usage(self.s, sid), before)

    def test_profile_query_cursor_and_model_flag_forgery_reject_without_effect(self):
        def tamper(raw, packet):
            for value in (True, 1.0, 2):
                changed = copy.deepcopy(packet); changed['discovery_search_version'] = value
                with self.assertRaisesRegex(InvalidUpdate, 'profile'):
                    discovery.check_request(self.s, self.meta['active'], changed)
            for query, line, cursor in (('', 1, None), ('x' * 81, 1, None), ('a\nb', 1, None),
                                        ('admit(', 2, None), ('admit(', 1, {'line': 1, 'column': 0, 'sha256': 'a'*64})):
                with self.assertRaises(InvalidUpdate):
                    repo.request_check(self.s, self.meta['active'], 'search', 'part3.py', query, line, cursor,
                                       search_version=1)
            raw['discovery_search_version'] = 1
        self.alter = tamper
        _, packet = self.discover()
        self.assertEqual(packet['discovery_coverage']['stop_reason'], 'proposal_rejected')
        self.assertEqual(self.tool_records(), [])

    def test_present_delivery_flag_is_insufficient_before_selector_acceptance(self):
        def unaccepted(raw, _):
            iid = self.s.db.execute("SELECT id FROM invocations WHERE json_extract(record_json,'$.role')='architect' ORDER BY rowid DESC LIMIT 1").fetchone()[0]
            with self.assertRaises((InvalidUpdate, PageFault)):
                repo.execute(self.s, self.meta['active'], self.ws, self.root, 'search', target='part3.py',
                             query='admit(', invocation_id=iid, resource_check=False)
        self.alter = unaccepted
        self.discover()
        self.assertEqual(len(self.tool_records()), 1)

    def test_symlink_target_excluded_from_catalog_is_not_followed(self):
        (self.ws / 'link.py').symlink_to(self.ws / 'part3.py')
        self.actions = [{'kind': 'search', 'target': 'link.py', 'query': 'admit('}]
        _, packet = self.discover()
        self.assertEqual(packet['discovery_coverage']['stop_reason'], 'proposal_rejected')
        self.assertEqual(self.tool_records(), [])

    def test_scoped_replay_keeps_exact_proposal_target_and_paid_cost(self):
        out, _ = self.discover(); sid = self.meta['active']; record = self.tool_records()[0]; op = record['operation']
        before = review_budget.usage(self.s, sid)
        arguments = dict(target='part3.py', query='admit(', invocation_id=op['invocation_id'],
                         request_id=op['request_id'], resource_check=False)
        with machine.scope(out['execution_id']):
            replay = repo.execute(self.s, sid, self.ws, self.root, 'search', **arguments)
            self.assertTrue(replay['replayed'])
            with self.assertRaisesRegex(InvalidUpdate, 'changed arguments'):
                repo.execute(self.s, sid, self.ws, self.root, 'search', **dict(arguments, target='part2.py'))
            with self.assertRaisesRegex(InvalidUpdate, 'changed search profile'):
                repo._replay(self.s, sid, op['request_id'], ('search', 'part3.py', 'admit(', 1), None,
                             op['invocation_id'], True, search_version=None)
        self.assertEqual(len(self.tool_records()), 1)
        self.assertEqual(review_budget.usage(self.s, sid), before)

    def test_accepted_scoped_replay_cannot_run_under_a_foreign_execution(self):
        out, _ = self.discover(); sid = self.meta['active']; op = self.tool_records()[0]['operation']
        owner = machine.node(self.s, out['execution_id'])
        fields = {key: copy.deepcopy(value) for key, value in owner.items()
                  if key not in ('id', 'kind', 'session', 'parent_id', 'initial_state', 'created_at')}
        fields.update(flow_id='another-flow', previous=None)
        with self.s.db:
            other = machine.create(self.s, 'execution', sid, None, **fields)
        before = review_budget.usage(self.s, sid)
        with machine.scope(other['id']), self.assertRaisesRegex(InvalidUpdate, 'another execution'):
            repo.execute(self.s, sid, self.ws, self.root, 'search', target='part3.py', query='admit(',
                         invocation_id=op['invocation_id'], request_id=op['request_id'], resource_check=False)
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertEqual(len(self.tool_records()), 1)

    def test_accepted_scoped_plan_tool_gap_survives_reload_and_model_switch_without_replay(self):
        ready = self.until('SYNTHESIS'); sid = self.meta['active']; execute = repo.execute
        def interrupt(*args, **kwargs):
            if kwargs.get('invocation_id') and args[4] == 'search':
                raise Interrupted('Fixture accepted selector before repository execution')
            return execute(*args, **kwargs)
        with patch.object(repo, 'execute', side_effect=interrupt):
            paused = self.step(ready)
        self.assertEqual(paused['state'], 'PAUSED')
        self.assertEqual(paused['stop']['status'], 'interrupted')
        self.assertEqual(len(self.calls), 1)
        first = self.calls[0][0]
        accepted = discovery._accepted(self.s, sid, discovery.history(self.s, sid, first['discovery_key']))
        iid = accepted[0][0]
        receipt = self.s.db.execute('SELECT record_json FROM invocation_receipts WHERE invocation_id=?', (iid,)).fetchone()[0]
        before = review_budget.usage(self.s, sid)
        self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite')
        self.meta = json.loads((self.root / 'workspace.json').read_text()); self.tier = 'high'
        resumed = self.step(paused, event='resume'); recovered = self.step(resumed)
        self.assertEqual(recovered['state'], 'SYNTHESIS')
        self.assertEqual(len(self.calls), 1)
        record = self.tool_records()[0]
        self.assertEqual(record['operation']['invocation_id'], iid)
        self.assertEqual(record['operation']['search_version'], 1)
        after = review_budget.usage(self.s, sid)
        self.assertEqual(after['used']['calls'], before['used']['calls'])
        self.assertEqual(after['used']['tokens'], before['used']['tokens'])
        self.assertEqual(after['used']['operations'], before['used']['operations'] + 1)
        self.assertEqual(after['used']['source_bytes'] - before['used']['source_bytes'], (self.ws / 'part3.py').stat().st_size)
        self.assertEqual(self.s.db.execute('SELECT record_json FROM invocation_receipts WHERE invocation_id=?', (iid,)).fetchone()[0], receipt)
        self.assertTrue(self.s.verify_invocations(sid))


if __name__ == '__main__': unittest.main()
