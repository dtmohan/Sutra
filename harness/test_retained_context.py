"""Continuity directories retain whole scoped receipts without new tool spend."""
import copy
import json
import unittest
from unittest.mock import patch

from context_manager import ContextBudgetExceeded, InvalidUpdate, canonical
from invocation_gateway import InvocationLedger
from paging import PageFault
import orchestration as machine
import retained_context as retained
import review_budget
import repository_tools as repo
import test_discovery_selection as selection


class RetainedContextTests(unittest.TestCase):
    setUp = selection.SelectionWireTests.setUp
    tearDown = selection.SelectionWireTests.tearDown
    provider = selection.SelectionWireTests.provider
    step = selection.SelectionWireTests.step
    until = selection.SelectionWireTests.until
    discover = selection.SelectionWireTests.discover

    def prepared(self):
        _, p = self.discover()
        return self.meta['active'], p

    def test_all_notes_and_source_units_survive_a_large_window_without_new_spend(self):
        sid, p = self.prepared()
        before = review_budget.usage(self.s, sid)
        count = self.s.db.execute('SELECT count(*) FROM repository_receipts').fetchone()[0]
        view = retained.bind(self.s, sid, p, limit=65536)
        self.assertEqual(view['totals']['notes'], 3)
        self.assertEqual(view['deferred'], {'notes': 0, 'source_units': 0, 'pages': 0})
        self.assertEqual(len(retained.note_pages(view)), 3)
        units = retained.source_units(view)
        self.assertEqual({entry['path'] for entry in units}, {'part0.py', 'part1.py'})
        repo.validate_evidence(self.s, sid, str(self.ws), units)
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM repository_receipts').fetchone()[0], count)
        self.assertEqual(retained.check(self.s, sid, p), view)

    def test_small_window_defers_whole_pages_and_nested_lookup_recovers_them(self):
        sid, p = self.prepared()
        full = retained.bind(self.s, sid, p, limit=65536)
        small = retained.refit(self.s, sid, p, 2200)
        self.assertEqual(full['root'], small['root'])
        self.assertGreater(small['deferred']['pages'], 0)
        self.assertLessEqual(retained.contents_bytes(small), 2200)
        seen = {}; pending = [small['root']['ref']]
        while pending:
            ref = pending.pop()
            if ref in seen:
                continue
            seen[ref] = retained.lookup(self.s, sid, p, ref)
            pending.extend(link['ref'] for link in seen[ref].get('links', []))
        self.assertEqual(sum(node['kind'] == 'working_note_unverified' for node in seen.values()), 3)
        self.assertEqual(sum(node['kind'] == 'retained_source_unit' for node in seen.values()), 2)
        source_ref = next(ref for ref, page in seen.items() if page['kind'] == 'retained_source_unit')
        expected = copy.deepcopy(seen[source_ref])
        recalled = retained.refit(self.s, sid, p, 4096, required_refs=(source_ref,))
        self.assertIn(expected, recalled['pages'])
        self.assertIn(source_ref, recalled['requested_refs'])
        self.assertEqual(retained.lookup(self.s, sid, p, source_ref), expected)

    def test_required_page_never_silently_drops_when_model_window_shrinks(self):
        sid, p = self.prepared()
        source_ref = next(page['ref'] for page in p['retained_context']['pages']
                          if page['kind'] == 'retained_source_unit')
        retained.refit(self.s, sid, p, 4096, required_refs=(source_ref,))
        before = copy.deepcopy(p)
        with self.assertRaises(ContextBudgetExceeded):
            retained.refit(self.s, sid, p, 512)
        self.assertEqual(p, before)
        self.assertIn(source_ref, p['retained_context']['requested_refs'])

    def test_reload_model_switch_preserves_refs_constraints_and_spent_budgets(self):
        sid, p = self.prepared()
        full = copy.deepcopy(p['retained_context'])
        before = review_budget.usage(self.s, sid)
        pins = self.s.pinned(sid)
        self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite')
        self.tier = 'high'
        small = retained.refit(self.s, sid, p, 2200)
        self.assertEqual(small['root'], full['root'])
        self.assertEqual(small['journal'], full['journal'])
        for node in full['pages']:
            self.assertEqual(retained.lookup(self.s, sid, p, node['ref']), node)
        self.assertEqual(self.s.pinned(sid), pins)
        self.assertEqual(review_budget.usage(self.s, sid), before)

    def test_source_change_and_quarantined_note_fail_closed(self):
        sid, p = self.prepared()
        ref = p['retained_context']['root']['ref']
        (self.ws / 'part0.py').write_text('CHANGED = True\n')
        with self.assertRaises((PageFault, InvalidUpdate)):
            retained.lookup(self.s, sid, p, ref)

    def test_quarantined_ancestor_cannot_become_a_retained_working_note(self):
        sid, p = self.prepared()
        iid = p['retained_context']['journal']['invocations'][0]
        self.s.quarantine_invocation(sid, iid, 'test quarantine')
        with self.assertRaises(PageFault):
            retained.check(self.s, sid, p)

    def test_forged_body_root_foreign_ref_and_wrong_request_are_rejected(self):
        sid, p = self.prepared()
        bad = copy.deepcopy(p)
        bad['retained_context']['pages'][0]['note']['summary'] = 'Forged retained memory'
        with self.assertRaises(InvalidUpdate):
            retained.check(self.s, sid, bad)
        bad = copy.deepcopy(p); bad['retained_context']['root']['ref'] = '0' * 64
        with self.assertRaises(InvalidUpdate):
            retained.lookup(self.s, sid, bad, p['retained_context']['root']['ref'])
        with self.assertRaises(PageFault):
            retained.lookup(self.s, sid, p, '0' * 64)
        bad = copy.deepcopy(p); bad['retained_context']['journal'] = {'invocations': 'not a list', 'receipts': []}
        with self.assertRaises(InvalidUpdate):
            retained.check(self.s, sid, bad)
        bad = copy.deepcopy(p); bad['request'] = 'Review an unrelated resume instead.'
        with self.assertRaises(InvalidUpdate):
            retained.check(self.s, sid, bad)

    def test_self_rehashed_journal_omission_cannot_replace_committed_directory(self):
        sid, p = self.prepared()
        bad = copy.deepcopy(p)
        bad['retained_context']['journal']['invocations'].pop()
        with self.assertRaises((InvalidUpdate, PageFault)):
            retained.check(self.s, sid, bad)

    def test_nonadjacent_source_units_remain_independent_and_source_linked(self):
        # Both nonadjacent reads retain exact original positions, rather than
        # replacing the first body with the last same-path result.
        (self.ws / 'part0.py').write_text(''.join('VALUE_%03d = %d\n' % (i, i) for i in range(100)))
        self.actions = [{'kind': 'read', 'target': 'part0.py', 'line': 1},
                        {'kind': 'read', 'target': 'part0.py', 'line': 80},
                        {'kind': 'read', 'target': 'part1.py'}, {'kind': 'report'}]
        sid, p = self.prepared()
        view = retained.bind(self.s, sid, p, limit=65536)
        units = [e for e in retained.source_units(view) if e['path'] == 'part0.py']
        self.assertGreaterEqual(len(units), 2)
        self.assertIn(1, {e['line'] for e in units})
        self.assertIn(80, {e['line'] for e in units})
        self.assertEqual(len({e['id'] for e in units}), len(units))
        for entry in units:
            self.assertIn(entry['text'], (self.ws / 'part0.py').read_text())
        repo.validate_evidence(self.s, sid, str(self.ws), units)

    def test_compatible_execution_lineage_includes_previous_but_other_flow_does_not(self):
        sid, p = self.prepared()
        original = machine.node(self.s, p['discovery_owner'])
        with self.s.db:
            foreign = machine.create(self.s, 'execution', sid, original['parent_id'],
                flow_id='another-flow', binding=original['binding'], permissions=original['permissions'],
                context=dict(original['context'], request='Another independent request'),
                context_hash='1' * 64, limits=original['limits'])
        q = copy.deepcopy(p); q['request'] = 'Another independent request'
        q.pop('evidence_discovery', None)
        q['snapshot_owner'] = q['discovery_owner'] = foreign['id']
        q['conversation_handoff'] = copy.deepcopy(p.get('conversation_handoff'))
        fresh = retained.bind(self.s, sid, q, owner=foreign['id'], limit=4096)
        self.assertEqual(fresh['totals']['notes'], 0)
        self.assertEqual(fresh['totals']['source_units'], 0)
        old_ref = p['retained_context']['root']['ref']
        with self.assertRaises(PageFault):
            retained.lookup(self.s, sid, q, old_ref)

    def test_next_execution_recall_maps_old_refs_and_delivers_the_same_whole_unit(self):
        sid, old = self.prepared()
        old_page = next(page for page in old['retained_context']['pages']
                        if page['kind'] == 'retained_source_unit')
        previous = machine.node(self.s, old['discovery_owner'])
        with self.s.db:
            machine.transition(self.s, previous['id'], 'DONE', 'TEST_CHECKPOINT')
            next_owner = machine.create(self.s, 'execution', sid, previous['parent_id'],
                flow_id=previous['flow_id'], binding=previous['binding'], permissions=previous['permissions'],
                context=previous['context'], context_hash=previous['context_hash'],
                limits=previous['limits'], previous=previous['id'])
        p = copy.deepcopy(old); p['discovery_owner'] = next_owner['id']
        before = review_budget.usage(self.s, sid)
        retained.bind(self.s, sid, p, owner=next_owner['id'], limit=2200)
        self.assertNotEqual(p['retained_context']['root']['ref'], old['retained_context']['root']['ref'])
        refs = retained.recall(self.s, sid, p, old, [old_page['ref']])
        recalled = retained.refit(self.s, sid, p, 4096, required_refs=refs)
        selected = next(page for page in recalled['pages'] if page['ref'] in refs)
        self.assertEqual(selected['evidence'], old_page['evidence'])
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertEqual(self.s.pinned(sid), p['contract'])

    def test_manual_task_owner_follows_explicit_ancestry_not_all_session_requests(self):
        import test_architecture as architecture
        fixture = architecture.ArchitectureTests(); fixture.setUp()
        try:
            first = fixture.step(architecture.Model())
            second = fixture.step(architecture.Model('read', 'broker.py'))
            owner = machine.task(fixture.s, fixture.sid)['id']
            row = fixture.s.db.execute("""SELECT d.payload_json FROM invocations i
                JOIN deliveries d ON d.id=i.delivery_id WHERE i.id=?""",
                (second['result']['invocation_id'],)).fetchone()
            p = json.loads(row[0]); p['derived_from'] = [second['result']['invocation_id']]
            p['request'] = 'Now synthesize this exact architecture review.'
            expected = retained.bind(fixture.s, fixture.sid, p, owner=owner, limit=65536)
            self.assertEqual(expected['totals']['notes'], 2)
            self.assertEqual(len(retained.source_units(expected)), 2)
            fixture.meta.pop('checkpoint', None)
            unrelated = architecture.Model('read', 'entry.py')
            fixture.step(unrelated)
            q = copy.deepcopy(unrelated.payloads[0]); q['derived_from'] = []
            q['request'] = 'Another independent task without those parent notes.'
            fresh = retained.bind(fixture.s, fixture.sid, q, owner=owner, limit=65536)
            self.assertEqual(fresh['totals']['notes'], 0)
            self.assertEqual(fresh['totals']['source_units'], 0)
            with self.assertRaises(PageFault):
                retained.recall(fixture.s, fixture.sid, q, p, [expected['root']['ref']])
        finally:
            fixture.tearDown()

    def test_exact_minimum_floor_retires_optional_pages_without_dropping_required_unit(self):
        sid, p = self.prepared()
        source_ref = next(page['ref'] for page in p['retained_context']['pages']
                          if page['kind'] == 'retained_source_unit')
        full = retained.refit(self.s, sid, p, 65536, required_refs=[source_ref])
        before = copy.deepcopy(full)
        floor = retained.minimum_bytes(full)
        self.assertLess(floor, retained.contents_bytes(full))
        self.assertEqual(full, before)
        small = retained.refit(self.s, sid, p, floor)
        self.assertEqual(retained.contents_bytes(small), floor)
        self.assertEqual([page['ref'] for page in small['pages']], [source_ref])
        self.assertEqual(small['requested_refs'], [source_ref])
        self.assertEqual(retained.minimum_bytes(small), floor)
        self.assertEqual(small['root'], full['root'])

    def test_directory_expansion_over_128_leaves_is_rejected_in_preflight(self):
        import review_discovery as discovery
        directory = discovery.memory_directory('bounded-test', 'preflight-test')
        leaves = [directory['put']({'kind': 'working_note_unverified', 'ordinal': i,
                    'note': {'summary': 'An unverified observation', 'remaining': [], 'citation_paths': []},
                    'checkpoint': str(i)}) for i in range(129)]
        index = directory['index']('notes', [{'label': str(i), 'ref': ref} for i, ref in enumerate(leaves)])
        root = directory['put']({'kind': 'retained_memory_root', 'notes': 129, 'source_units': 0,
                                'links': [{'label': 'notes', 'ref': index}]})
        graph = dict(directory, root=root, note_refs=leaves, source_refs=[],
                     journal={'invocations': [], 'receipts': []})
        binding = {}; view = retained._pack(binding, graph, 1500, ())
        p = {'retained_context_version': 1, 'retained_context': view}
        # This isolates the bounded expansion policy; real journal/source
        # validation is exercised by the gateway, quarantine and forgery tests.
        with patch.object(retained, '_bound_graph', return_value=(view, binding, graph)):
            with self.assertRaisesRegex(InvalidUpdate, '128 whole pages.*smaller directory branch'):
                retained.validate_requests(None, None, p, [root])

    def test_host_journal_metadata_does_not_become_a_model_window_minimum(self):
        sid, p = self.prepared()
        binding = retained._binding(self.s, sid, p, None)
        graph = retained._graph(self.s, sid, binding)
        # Packing geometry only: these synthetic IDs are not persisted or
        # accepted as a journal. The existing real graph retains exact bodies.
        graph['journal'] = {'invocations': [format(i, '064x') for i in range(300)],
                            'receipts': [format(i + 300, '064x') for i in range(300)]}
        view = retained._pack(binding, graph, 1024, ())
        self.assertLessEqual(retained.contents_bytes(view), 1024)
        self.assertGreater(len(canonical(view).encode()), 40000)
        self.assertEqual(view['root'], graph['pages'][graph['root']])
        self.assertGreater(view['deferred']['pages'], 0)

    def test_invalid_limits_and_missing_explicit_owner_do_not_mutate_packet(self):
        sid, p = self.prepared()
        before = copy.deepcopy(p)
        for limit in (True, 0, -1, retained.MAX_LIMIT + 1):
            with self.assertRaises(InvalidUpdate):
                retained.bind(self.s, sid, p, limit=limit)
        self.assertEqual(p, before)
        q = {k: v for k, v in p.items() if k not in ('snapshot_owner', 'discovery_owner')}
        with self.assertRaises(InvalidUpdate):
            retained.bind(self.s, sid, q)


class RetainedRecallTests(unittest.TestCase):
    def setUp(self):
        import test_architecture as architecture
        self.architecture = architecture
        self.fixture = architecture.ArchitectureTests(); self.fixture.setUp()
        self.s, self.sid = self.fixture.s, self.fixture.sid

    def tearDown(self):
        self.fixture.tearDown()

    def pending(self, *, limit=1500, root_request=False, expect_accepted=True):
        import uuid
        import architecture_role as role
        from invocation_gateway import InvocationGateway
        from review_role import compact
        model_class = self.architecture.Model
        self.fixture.step(model_class())
        previous = self.fixture.step(model_class('read', 'broker.py'))['result']['invocation_id']
        raw = self.s.db.execute("""SELECT d.payload_json FROM invocations i
            JOIN deliveries d ON d.id=i.delivery_id WHERE i.id=?""", (previous,)).fetchone()[0]
        p = json.loads(raw); p['derived_from'] = [previous]
        p['handoff'] = compact(self.s.load_result(self.sid, previous))
        budget = repo.usage(self.s, self.sid)
        p['repository'].update(budget=budget, operations_remaining=max(0, budget['limit'] - budget['used']),
                               last_tool=repo.feedback(self.s, self.sid))
        self.owner = machine.task(self.s, self.sid)['id']; p['retained_context_owner'] = self.owner
        retained.bind(self.s, self.sid, p, owner=self.owner, limit=limit)
        p['evidence'] = retained.source_units(p['retained_context'])
        nodes = [p['retained_context']['root']] + p['retained_context']['pages']
        known = {link['ref'] for node in nodes for link in node.get('links', [])}
        visible = {node['ref'] for node in nodes}
        ref = (p['retained_context']['root']['ref'] if root_request else
               next(ref for ref in known - visible
                    if retained.lookup(self.s, self.sid, p, ref)['kind'] == 'retained_source_unit'))
        model = model_class('clarify', '', fault=lambda result: result.update(remaining=['Recall context: ' + ref]))
        from paging import Budget
        model.budget = Budget(65536, 1024)
        schema = role.schema(p); charge = model.budget.check(role.SYSTEM, p, schema)
        run = self.s.begin_run(self.sid, p['request'], [], None, max_calls=1, max_repairs=0)
        self.s.reserve(run, 'ATTEMPT', {'attempt': 0})
        delivery = self.s.prepare_delivery(self.sid, p, charge)
        out = InvocationGateway(self.s.path, run, model, ledger=self.s).invoke(
            'architect', uuid.uuid4().hex, role.SYSTEM, p, schema, delivery)
        if not expect_accepted:
            self.assertFalse(out['admission']['admitted'])
            return p, None, out
        self.assertTrue(out['admission']['admitted'], out['admission'])
        current = copy.deepcopy(p); current['derived_from'] = [out['invocation_id']]
        current['handoff'] = compact(out['accepted_output'])
        retained.bind(self.s, self.sid, current, owner=self.owner, limit=65536)
        current['evidence'] = retained.source_units(current['retained_context'])
        return p, current, {'invocation_id': out['invocation_id'], 'refs': [ref]}

    def test_exact_remaining_requests_are_bounded_deduplicated_and_profile_gated(self):
        ref = 'a' * 64
        p = {'retained_context_version': 1, 'retained_context': {
            'root': {'ref': 'b' * 64, 'kind': 'retained_memory_root', 'links': [{'ref': ref}]}, 'pages': []}}
        proposal = {'remaining': ['Ordinary unfinished analysis', 'Recall context: ' + ref,
                                  'Recall context: ' + ref]}
        self.assertEqual(retained.requests(p, proposal), [ref])
        self.assertEqual(retained.requests({}, proposal), [])
        for bad in ('Recall context:' + ref, 'Recall context: ' + ref + ' ',
                    'Recall context: ' + 'A' * 64, 'Recall context: not-an-address'):
            with self.assertRaises(InvalidUpdate):
                retained.requests(p, {'remaining': [bad]})
        with self.assertRaises(InvalidUpdate):
            retained.requests(p, {'remaining': ['Recall context: ' + '0' * 64]})
        refs = [format(number, '064x') for number in range(retained.MAX_RECALL_REFS + 1)]
        p['retained_context']['root']['links'] = [{'ref': value} for value in refs]
        with self.assertRaises(InvalidUpdate):
            retained.requests(p, {'remaining': ['Recall context: ' + value for value in refs]})

    def test_327_character_ordinary_gap_remains_valid_through_the_real_architecture_gateway(self):
        import uuid
        import architecture_role as role
        from invocation_gateway import InvocationGateway
        from review_role import compact
        from paging import Budget
        _, current, _ = self.pending()
        ordinary = 'Ordinary architecture gap. ' + 'x' * 300
        self.assertEqual(len(ordinary), 327)
        model = self.architecture.Model('clarify', '',
            fault=lambda proposal: proposal.update(remaining=[ordinary]))
        model.budget = Budget(65536, 1024)
        schema = role.schema(current)
        charge = model.budget.check(role.SYSTEM, current, schema)
        run = self.s.begin_run(self.sid, current['request'], [], None, max_calls=1, max_repairs=0)
        self.s.reserve(run, 'ATTEMPT', {'attempt': 0})
        delivery = self.s.prepare_delivery(self.sid, current, charge)
        out = InvocationGateway(self.s.path, run, model, ledger=self.s).invoke(
            'architect', uuid.uuid4().hex, role.SYSTEM, current, schema, delivery)
        self.assertTrue(out['admission']['admitted'], out['admission'])
        self.assertEqual(out['accepted_output']['remaining'], [ordinary])
        self.assertLessEqual(len(canonical(compact(out['accepted_output'])).encode()), 1600)
        self.assertEqual(retained.requests(current, out['accepted_output']), [])
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_ordinary_note_count_is_role_owned_not_a_recall_parser_limit(self):
        import architecture_role as role
        from invocation_roles import validate_shape
        from review_role import compact
        p = {'contract': {'constraints': {}}, 'phase': 'explore', 'evidence': [],
             'repository': {'budget': {'decision': 'ALLOW'}}, 'retained_context_version': 1,
             'retained_context': {'root': {'ref': 'a' * 64, 'kind': 'retained_memory_root',
                                          'links': []}, 'pages': []}}
        ordinary = ['x'] * 1025
        validate_shape(ordinary, role.schema(p)['properties']['remaining'])
        self.assertEqual(retained.requests(p, {'remaining': ordinary}), [])
        # Schema/parser compatibility alone is not full-role admission: the
        # unchanged architecture aggregate 1600-byte note ceiling still rejects
        # this intentionally large list of ordinary notes.
        proposal = {'summary': 'Partial review', 'findings': [], 'completed': [], 'remaining': ordinary}
        self.assertGreater(len(canonical(compact(proposal)).encode()), 1600)
        for malformed in ('plain text', [1], None):
            with self.assertRaises(InvalidUpdate):
                retained.requests(p, {'remaining': malformed})

    def test_accepted_request_is_replayed_into_the_actual_next_model_packet(self):
        import uuid
        import architecture_role as role
        from invocation_gateway import InvocationGateway
        from model_packet import build
        prior, current, request = self.pending()
        expected = retained.lookup(self.s, self.sid, prior, request['refs'][0])['evidence']
        pins = self.s.pinned(self.sid); before = review_budget.usage(self.s, self.sid)
        operations = self.s.db.execute('SELECT count(*) FROM repository_receipts').fetchone()[0]
        self.s.close(); self.s = InvocationLedger(self.fixture.storage / 'state.sqlite')
        self.fixture.s = self.s
        view = retained.apply_recall(self.s, self.sid, current, request)
        self.assertIn(expected, retained.source_units(view))
        self.assertTrue(view['requested_refs'])
        self.assertEqual(retained.apply_recall(self.s, self.sid, current, request), view)
        self.assertEqual(review_budget.usage(self.s, self.sid), before)
        self.assertEqual(self.s.pinned(self.sid), pins)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM repository_receipts').fetchone()[0], operations)
        current['evidence'] = retained.source_units(view)
        model = self.architecture.Model('clarify', ''); model.model = 'fixture-switched-model'
        from paging import Budget
        model.budget = Budget(65536, 1024)
        schema = role.schema(current)
        wire = build(role.SYSTEM, current, schema)
        model_body = json.loads(wire['messages'][-1]['content'])
        self.assertIn(expected['text'], [entry['text'] for entry in model_body['evidence']])
        charge = model.budget.check(role.SYSTEM, current, schema)
        run = self.s.begin_run(self.sid, current['request'], [], None, max_calls=1, max_repairs=0)
        self.s.reserve(run, 'ATTEMPT', {'attempt': 0})
        delivery = self.s.prepare_delivery(self.sid, current, charge)
        out = InvocationGateway(self.s.path, run, model, ledger=self.s).invoke(
            'architect', uuid.uuid4().hex, role.SYSTEM, current, schema, delivery)
        self.assertTrue(out['admission']['admitted'], out['admission'])
        self.assertIn(expected, model.payloads[0]['evidence'])
        with self.assertRaises(InvalidUpdate):
            retained.requests(current, {'remaining': ['Recall context: ' + view['requested_refs'][0]]})
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM repository_receipts').fetchone()[0], operations)
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_missing_foreign_forged_duplicate_and_quarantined_records_fail_without_effect(self):
        _, current, request = self.pending()
        before = review_budget.usage(self.s, self.sid)
        malformed = [dict(request, extra=True), dict(request, refs=[]),
                     dict(request, refs=request['refs'] * 2),
                     dict(request, refs=['0' * 64]), dict(request, invocation_id='missing')]
        for record in malformed:
            with self.assertRaises((InvalidUpdate, PageFault)):
                retained.apply_recall(self.s, self.sid, current, record)
        self.s.quarantine_invocation(self.sid, request['invocation_id'], 'test recall quarantine')
        with self.assertRaises(PageFault):
            retained.apply_recall(self.s, self.sid, current, request)
        self.assertEqual(review_budget.usage(self.s, self.sid), before)

    def test_oversized_requested_subtree_is_rejected_before_pending_is_accepted(self):
        prior, _, out = self.pending(limit=1500, root_request=True, expect_accepted=False)
        self.assertGreater(prior['retained_context']['deferred']['pages'], 0)
        self.assertIn('request a smaller directory branch', ' '.join(out['admission']['errors']))
        with self.assertRaisesRegex(InvalidUpdate, 'request a smaller directory branch'):
            retained.validate_requests(self.s, self.sid, prior, [prior['retained_context']['root']['ref']])

    def test_already_visible_subtree_cannot_drive_recall_loops(self):
        prior, _, out = self.pending(limit=65536, root_request=True, expect_accepted=False)
        self.assertEqual(prior['retained_context']['deferred']['pages'], 0)
        self.assertIn('no previously deferred body', ' '.join(out['admission']['errors']))
        with self.assertRaisesRegex(InvalidUpdate, 'no previously deferred body'):
            retained.validate_requests(self.s, self.sid, prior, [prior['retained_context']['root']['ref']])


class RetainedRecoveryTests(unittest.TestCase):
    setUp = selection.SelectionWireTests.setUp
    tearDown = selection.SelectionWireTests.tearDown
    provider = selection.SelectionWireTests.provider
    step = selection.SelectionWireTests.step
    until = selection.SelectionWireTests.until
    discover = selection.SelectionWireTests.discover

    def invoke_writer(self, p, ref=None):
        import uuid
        import chat_runtime as chat
        from invocation_gateway import InvocationGateway
        from test_chat import Fake
        from paging import Budget
        model = Fake(p['route']['record']['choice']['model'])
        model.budget = Budget(65536, 4096, 256)
        if ref:
            model.fault = lambda proposal, packet: proposal.update(remaining=['Recall context: ' + ref])
        fmt = chat.schema(p); charge = model.budget.check(chat.SYSTEM, p, fmt)
        run = self.s.begin_run(self.sid, p['request'], [], None, max_calls=1, max_repairs=0)
        self.s.reserve(run, 'ATTEMPT', {'attempt': 0})
        delivery = self.s.prepare_delivery(self.sid, p, charge)
        with machine.scope(self.owner):
            out = InvocationGateway(self.s.path, run, model, ledger=self.s).invoke(
                'chat_reviewer', uuid.uuid4().hex, chat.SYSTEM, p, fmt, delivery)
        self.assertTrue(out['admission']['admitted'], out['admission'])
        return out

    def crashed_writer(self):
        import chat_runtime as chat
        import static_runtime
        import review_discovery as discovery
        from test_chat import Fake
        from paging import Budget
        current, p = self.discover(); self.sid = self.meta['active']; self.owner = p['discovery_owner']
        data = static_runtime.load_partition(self.s, self.sid, self.ws,
                                              p['snapshot_receipts'], p['snapshot_owner'])
        data['followups'] = discovery.load(self.s, self.sid, p)
        model = Fake(p['route']['record']['choice']['model']); model.budget = Budget(65536, 4096, 256)
        with machine.scope(self.owner):
            chat.fit_packet(self.s, self.sid, p, data, model, False)
        retained.refit(self.s, self.sid, p, 1500)
        nodes = [p['retained_context']['root']] + p['retained_context']['pages']
        known = {link['ref'] for node in nodes for link in node.get('links', [])}
        visible = {node['ref'] for node in nodes}
        ref = next(ref for ref in known - visible
                   if retained.lookup(self.s, self.sid, p, ref)['kind'] == 'working_note_unverified')
        out = self.invoke_writer(p, ref)
        # Intentionally do not call _finish_answer or save a flow context_recall.
        import orchestration_flow as flow
        self.assertIsNone(flow.info(self.s, current['flow_id'])[1].get('context_recall'))
        request = {'prompt': p['request'],
                   'conversation_id': (p.get('conversation_handoff') or {}).get('invocation_id'),
                   'child_results': [item['invocation_id'] for item in p.get('child_results', [])],
                   'orchestration_parent': p.get('orchestration_parent')}
        return p, request, {'invocation_id': out['invocation_id'], 'refs': [ref]}

    def test_receipt_only_crash_recovers_after_reload_and_later_normal_acceptance_supersedes_it(self):
        p, request, expected = self.crashed_writer()
        before = review_budget.usage(self.s, self.sid); pins = self.s.pinned(self.sid)
        receipts = self.s.db.execute('SELECT id,record_json FROM repository_receipts ORDER BY seq').fetchall()
        path = self.s.path; self.s.close(); self.s = InvocationLedger(path)
        with patch.object(repo, 'execute', side_effect=AssertionError('Recovery cannot reread source')):
            record = retained.recover_requests(self.s, self.sid, self.owner, request)
            self.assertEqual(record, expected)
            current = copy.deepcopy(p)
            retained.refit(self.s, self.sid, current, 65536)
            view = retained.apply_recall(self.s, self.sid, current, record)
            self.assertTrue(view['requested_refs'])
        self.assertEqual(review_budget.usage(self.s, self.sid), before)
        self.assertEqual(self.s.pinned(self.sid), pins)
        self.assertEqual(self.s.db.execute('SELECT id,record_json FROM repository_receipts ORDER BY seq').fetchall(), receipts)
        # This is a real later accepted draft, not a claim of publication.
        self.invoke_writer(current)
        self.assertIsNone(retained.recover_requests(self.s, self.sid, self.owner, request))
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_model_switch_successor_retains_the_request_but_foreign_contexts_cannot_recover_it(self):
        p, request, expected = self.crashed_writer()
        before = review_budget.usage(self.s, self.sid)
        old = machine.node(self.s, self.owner)
        with self.s.db:
            foreign = machine.create(self.s, 'execution', self.sid, old['parent_id'],
                flow_id='independent-flow', binding=old['binding'], permissions=old['permissions'],
                context=old['context'], context_hash=old['context_hash'], limits=old['limits'])
        self.assertIsNone(retained.recover_requests(self.s, self.sid, foreign['id'], request))
        self.assertIsNone(retained.recover_requests(self.s, self.sid, self.owner,
                                                    dict(request, prompt='A separate task')))
        self.assertIsNone(retained.recover_requests(self.s, self.sid, self.owner,
                                                    dict(request, conversation_id='0' * 32)))
        with self.s.db:
            machine.transition(self.s, self.owner, 'DONE', 'TEST_MODEL_SWITCH_CHECKPOINT')
            successor = machine.create(self.s, 'execution', self.sid, old['parent_id'],
                flow_id=old['flow_id'], binding=old['binding'], permissions=old['permissions'],
                context=old['context'], context_hash=old['context_hash'], limits=old['limits'], previous=self.owner)
        path = self.s.path; self.s.close(); self.s = InvocationLedger(path); self.tier = 'high'
        record = retained.recover_requests(self.s, self.sid, successor['id'], request)
        self.assertEqual(record, expected)
        current = copy.deepcopy(p); current['discovery_owner'] = successor['id']
        retained.bind(self.s, self.sid, current, owner=successor['id'], limit=65536)
        view = retained.apply_recall(self.s, self.sid, current, record, owner=successor['id'])
        self.assertTrue(view['requested_refs'])
        self.assertEqual(review_budget.usage(self.s, self.sid), before)
        self.assertIsNone(retained.recover_requests(self.s, self.sid, self.owner, request))

    def test_quarantined_latest_request_is_not_replaced_by_older_context(self):
        _, request, expected = self.crashed_writer()
        before = review_budget.usage(self.s, self.sid)
        self.s.quarantine_invocation(self.sid, expected['invocation_id'], 'test receipt-only crash quarantine')
        with self.assertRaises(PageFault):
            retained.recover_requests(self.s, self.sid, self.owner, request)
        self.assertEqual(review_budget.usage(self.s, self.sid), before)


if __name__ == '__main__':
    unittest.main()
