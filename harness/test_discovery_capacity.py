"""Downstream holdback uses real wire budgets without granting or spending."""
import copy
import json
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate, canonical, sha
from paging import Budget
import discovery_capacity as capacity
import orchestration as machine
from invocation_gateway import InvocationLedger
import test_accounting_batches as accounting_fixture
from test_context_window_recovery import CountService


# Captured from the original v1 helper before the empirical v2 calibration.
# Only fixture hashes/counts are retained; no user source or prompt is included.
LEGACY_PLAN = json.loads(r'''
{
  "version": 1,
  "profile": "discovery-downstream-capacity-v1",
  "mode": "planning_holdback",
  "binding": {
    "session": "fixture",
    "parent_hash": "4a600a928533846b0a640315e6a46355168bee505aaf91456837bffc630f622f",
    "source_view_hash": "59499fec71800e0b3c723a164a3f3574dc4b4b8c7deaaf802d892e84e7e5ffe3",
    "writer_budget_hash": "1a5ccdd284b496b41980add130375936e672ede2e451c459bc42bd0801f4a350",
    "reviewer_budget_hash": "1a5ccdd284b496b41980add130375936e672ede2e451c459bc42bd0801f4a350",
    "reviewer_budget_explicit": false
  },
  "durable_reservation": false,
  "full_publication_guaranteed": false,
  "reviewer_pages_held": 1,
  "repair_calls_held": 0,
  "reviewer_budget_source": "same_writer_budget_proxy",
  "cost_floor_kind": "measured_current_writer_and_representative_reviewer_page",
  "writer_view": "known_parent_planning_packet_not_final_dispatch",
  "dispatch_requires_fresh_fit": true,
  "unprotected_work": [
    "additional_claim_pages",
    "repair_attempts",
    "new_source_operations"
  ],
  "writer": {
    "input_charge": 6042,
    "output_reserve": 8192,
    "required_units": 14234,
    "financial_fits": true,
    "financial_limit_units": 65536,
    "physical_fit": true,
    "physical_method": "conservative_wire_bound",
    "physical_context_tokens": 65536,
    "physical_output_reserve": 8192,
    "packet_hash": "c8fbdaf6450eba111bb74fcd8159dd2540ee185166b1a88185fca209479bd667",
    "schema_hash": "4030fc3a66e6e2dd36a02203d4a1c938dd7873b541ae481ba6b40bb855f16578",
    "excerpt_files": 2,
    "excerpt_bytes": 74,
    "transport_seconds": 150
  },
  "reviewer": {
    "probe_input_charge": 3194,
    "output_reserve": 8192,
    "probe_required_units": 11386,
    "financial_fits": true,
    "physical_fit": true,
    "physical_method": "conservative_wire_bound",
    "physical_context_tokens": 65536,
    "physical_output_reserve": 8192,
    "page_ceiling_units": 65536,
    "probe_hash": "49cecfe9be162a0be96d3d75630d5faf71f16d69fcc2c2c81d58ab72258b23c5",
    "schema_hash": "104598d48160c385572e003b1cccd7bbf2fa27f0076b7ca77700e2a1df72b569",
    "known_source_units": 2,
    "transport_seconds": 150
  },
  "status": "measured",
  "cost_floor": {
    "calls": 2,
    "tokens": 25620,
    "seconds": 300
  },
  "holdback": {
    "calls": 2,
    "tokens": 79770,
    "seconds": 300
  },
  "digest": "54594c7e0573d220858f989b535c8634c403564a974d87bd9e70d02c06ed13b9"
}
''')


def inputs(contract=None):
    contract = contract or {'task_id': 'T', 'goal': 'Explain the architecture.',
        'constraints': {'allowed_paths': ['.'], 'edits_allowed': False,
                        'network_allowed': False, 'tests_allowed': False}}
    packet = {'contract': copy.deepcopy(contract), 'request': 'Explain the architecture.',
              'workspace': '/fixture', 'pages': [], 'parent_context_version': 3,
              'report_schema_version': 2, 'citation_policy_version': 2,
              'claim_source_version': 2, 'snapshot_receipts': ['s' * 64]}
    data = {'sources': {'entry.py': 'from leaf import run\ndef main():\n    return run()\n',
                        'leaf.py': 'def run():\n    return 1\n'},
            'graph': {'nodes': [], 'calls': [], 'diagnostics': [], 'module_dependencies': []}}
    return packet, data


class MeasureTests(unittest.TestCase):
    def test_v2_estimate_keeps_probe_declared_ceiling_and_stage_basis_separate(self):
        parent, data = inputs()
        budget = Budget(65536, 8192, 256)
        plan = capacity.measure(None, 'fixture', parent, data, budget)
        capacity.validate(plan)
        self.assertEqual(plan['version'], 2)
        self.assertEqual(plan['profile'], 'discovery-downstream-capacity-v2')
        reviewer = plan['reviewer']
        self.assertEqual(reviewer['estimated_headroom_units'],
                         min(reviewer['financial_limit_units'], 2 * reviewer['probe_required_units']))
        self.assertLess(plan['holdback']['tokens'], plan['stage_allowance_cost']['tokens'])
        self.assertEqual(plan['stage_allowance_cost'], LEGACY_PLAN['holdback'])
        self.assertEqual(plan['cost_floor'], LEGACY_PLAN['cost_floor'])
        self.assertEqual(plan['headroom_multiplier'], 2)
        self.assertFalse(plan['writer_capacity_guaranteed'])
        self.assertFalse(plan['reviewer_page_capacity_guaranteed'])

    def test_v2_headroom_is_capped_by_financial_allocation_not_physical_context(self):
        parent, data = inputs()
        window = dict(CountService().descriptor(), context_tokens=65536)
        reviewer = Budget(65536, 512, 256, context_profile=2,
                          reservation_limit=5200, context_window=window)
        with patch('context_window._rpc', side_effect=AssertionError('No inference or tokenization')):
            plan = capacity.measure(None, 'fixture', parent, data, Budget(65536, 8192, 256),
                                    reviewer_budget=reviewer)
        self.assertEqual(plan['status'], 'measured')
        self.assertLess(plan['reviewer']['probe_required_units'], 5200)
        self.assertEqual(plan['reviewer']['estimated_headroom_units'], 5200)
        self.assertEqual(plan['reviewer']['financial_limit_units'], 5200)
        self.assertEqual(plan['reviewer']['physical_context_tokens'], 65536)
        capacity.validate(plan)

    def test_captured_v1_plan_keeps_original_holdback_digest_and_gate_behavior(self):
        from karma_adapter import KarmaAdapter
        plan = copy.deepcopy(LEGACY_PLAN)
        capacity.validate(plan)
        before = copy.deepcopy(plan)
        def gates(s, owner, cost):
            return [KarmaAdapter().allocations({'tokens': 180000, 'calls': 10, 'seconds': 0},
                    {'tokens': 250000, 'calls': 40, 'seconds': 1800}, cost)]
        with patch('orchestration.gates', side_effect=gates), patch('review_budget.decision', return_value=None):
            result = capacity.gate(None, 'fixture', 'owner', plan, {'calls': 1, 'tokens': 12000})
        self.assertEqual(plan, before)
        self.assertEqual(plan['holdback']['tokens'], 79770)
        self.assertEqual(result['combined_cost']['tokens'], 91770)
        self.assertFalse(result['capacity_fits'])
        self.assertEqual(result['profile'], 'discovery-downstream-capacity-v1')

    def test_calibration_or_stage_basis_changes_are_rejected_even_after_rehash(self):
        parent, data = inputs()
        original = capacity.measure(None, 'fixture', parent, data, Budget(65536, 8192, 256))
        for mutate in (lambda p: p.update(headroom_multiplier=3),
                       lambda p: p['reviewer'].update(estimated_headroom_units=1),
                       lambda p: p['stage_allowance_cost'].update(tokens=p['holdback']['tokens'])):
            changed = copy.deepcopy(original); mutate(changed)
            changed['digest'] = sha('discovery-capacity-v2:' + canonical(
                {key: value for key, value in changed.items() if key != 'digest'}))
            with self.assertRaises(InvalidUpdate): capacity.validate(changed)

    def test_standalone_audit_validation_checks_binding_shape_and_session(self):
        parent, data = inputs()
        current = capacity.measure(None, 'fixture', parent, data, Budget(65536, 8192, 256))
        for original in (LEGACY_PLAN, current):
            for binding in (None, {}, dict(original['binding'], session=''),
                            dict(original['binding'], session='other\nchat'),
                            dict(original['binding'], parent_hash='not-a-digest'),
                            dict(original['binding'], reviewer_budget_explicit=1)):
                changed = copy.deepcopy(original); changed['binding'] = binding
                changed['digest'] = sha('discovery-capacity-v' + str(changed['version']) + ':' + canonical(
                    {key: value for key, value in changed.items() if key != 'digest'}))
                with self.assertRaises(InvalidUpdate): capacity.validate(changed)

    def test_real_wire_charges_and_explicit_stage_budgets_without_io_or_mutation(self):
        import chat_runtime
        import report_review
        parent, data = inputs()
        writer, reviewer = Budget(65536, 4096, 256), Budget(32768, 2048, 512)
        before = copy.deepcopy((parent, data, vars(writer), vars(reviewer)))
        charged = []
        original = Budget.charge

        def record(budget, system, packet, schema):
            value = original(budget, system, packet, schema)
            charged.append((system, value, packet, schema))
            return value

        with patch.object(Budget, 'charge', record), \
                patch('context_window._rpc', side_effect=AssertionError('No tokenizer/provider call')), \
                patch('review_budget.require', side_effect=AssertionError('No allocation amendment')):
            plan = capacity.measure(None, 'A', parent, data, writer, reviewer_budget=reviewer)
        self.assertEqual(plan['status'], 'measured')
        self.assertEqual((parent, data, vars(writer), vars(reviewer)), before)
        self.assertEqual([item[0] for item in charged], [chat_runtime.SYSTEM, report_review.SYSTEM])
        self.assertEqual(plan['writer']['required_units'], charged[0][1] + 4096)
        self.assertEqual(plan['reviewer']['probe_required_units'], charged[1][1] + 2048)
        estimated = min(32768, 2 * (charged[1][1] + 2048))
        self.assertEqual(plan['holdback']['tokens'], charged[0][1] + 4096 + estimated)
        self.assertEqual(plan['stage_allowance_cost']['tokens'], charged[0][1] + 4096 + 32768)
        self.assertEqual(plan['holdback']['calls'], 2)
        self.assertFalse(plan['durable_reservation'])
        self.assertFalse(plan['full_publication_guaranteed'])
        self.assertLessEqual(len(canonical(plan).encode()), capacity.MAX_PLAN_BYTES)

    def test_unchosen_reviewer_and_unknown_extra_pages_are_disclosed(self):
        parent, data = inputs()
        data['sources']['leaf.py'] += '# separately transferred source\n' * 100
        plan = capacity.measure(None, 'A', parent, data, Budget(65536, 4096, 256))
        self.assertEqual(plan['reviewer_budget_source'], 'same_writer_budget_proxy')
        self.assertGreater(plan['reviewer']['known_source_units'], 1)
        self.assertEqual(plan['reviewer_pages_targeted'], 1)
        self.assertFalse(plan['reviewer_page_capacity_guaranteed'])
        self.assertEqual(plan['repair_calls_held'], 0)
        self.assertIn('additional_claim_pages', plan['unprotected_work'])
        self.assertLessEqual(plan['cost_floor']['tokens'], plan['holdback']['tokens'])

    def test_budget_switch_and_source_task_or_constraint_changes_invalidate_binding(self):
        parent, data = inputs()
        small, large = Budget(32768, 2048, 256), Budget(65536, 8192, 256)
        plan = capacity.measure(None, 'A', parent, data, small)
        capacity.validate(plan, sid='A', parent_packet=parent, data=data, writer_budget=small)
        for sid, p, d, budget in [
                ('B', parent, data, small), ('A', parent, data, large),
                ('A', dict(parent, request='Review another request.'), data, small),
                ('A', parent, dict(data, sources={'changed.py': 'def changed(): pass\n'}), small)]:
            with self.subTest(sid=sid, budget=budget.context_limit), self.assertRaises(InvalidUpdate):
                capacity.validate(plan, sid=sid, parent_packet=p, data=d, writer_budget=budget)
        changed = copy.deepcopy(parent)
        changed['contract']['constraints']['network_allowed'] = True
        with self.assertRaises(InvalidUpdate):
            capacity.validate(plan, sid='A', parent_packet=changed, data=data, writer_budget=small)
        revised = capacity.measure(None, 'A', parent, data, large)
        self.assertNotEqual(plan['binding'], revised['binding'])
        self.assertGreater(revised['holdback']['tokens'], plan['holdback']['tokens'])

    def test_exact_window_financial_shortage_does_not_trim_earned_source(self):
        parent, data = inputs()
        data['sources']['leaf.py'] += '# paid evidence about caller and callee\n' * 120
        window = dict(CountService().descriptor(), context_tokens=65536)
        generous = Budget(65536, 4096, 256, context_profile=2,
                          reservation_limit=200000, context_window=window)
        tight = generous.with_limits(reservation_limit=4096)
        with patch('context_window._rpc', side_effect=AssertionError('No runtime counting')):
            first = capacity.measure(None, 'A', parent, data, generous, reviewer_budget=generous)
            second = capacity.measure(None, 'A', parent, data, tight, reviewer_budget=generous)
        self.assertEqual(first['status'], 'measured')
        self.assertEqual(second['status'], 'measured')
        self.assertEqual(first['writer']['excerpt_bytes'], second['writer']['excerpt_bytes'])
        self.assertTrue(first['writer']['financial_fits'])
        self.assertFalse(second['writer']['financial_fits'])
        self.assertTrue(second['writer']['physical_fit'])

    def test_exact_physical_unknown_does_not_claim_failure_or_call_tokenizer(self):
        parent, data = inputs()
        window = dict(CountService().descriptor(), context_tokens=4096)
        budget = Budget(4096, 1024, 256, context_profile=2,
                        reservation_limit=65536, context_window=window)
        with patch('context_window._rpc', side_effect=AssertionError('No runtime counting')):
            plan = capacity.measure(None, 'A', parent, data, budget)
        self.assertEqual(plan['status'], 'measured')
        self.assertIsNone(plan['writer']['physical_fit'])
        self.assertTrue(plan['writer']['financial_fits'])

    def test_unavailable_view_has_no_invented_capacity(self):
        parent, data = inputs()
        data['sources'] = {}
        plan = capacity.measure(None, 'A', parent, data, Budget(65536, 4096, 256))
        self.assertEqual(plan['status'], 'unavailable')
        self.assertIsNone(plan['holdback'])
        self.assertIsNone(plan['cost_floor'])
        with patch('orchestration.gates', side_effect=AssertionError('No made-up cost')):
            result = capacity.gate(None, 'A', 'owner', plan)
        self.assertIsNone(result['capacity_fits'])
        self.assertEqual(result['frames'], [])

    def test_mandatory_memory_is_measured_without_materializing_or_mutating_it(self):
        parent, data = inputs()
        memory = {'pinned_user_statements': [{'quote': 'Read only. No network.'}],
                  'recent_turns': [{'user': 'Preserve project Cedar and ticket CED724.'}]}
        before = copy.deepcopy(memory)
        actual = Budget.charge
        supplied = []

        def charge(budget, system, packet, schema):
            if 'memory' in packet:
                supplied.append(copy.deepcopy(packet['memory']))
            return actual(budget, system, packet, schema)

        with patch('chat_runtime.conversation_pages', return_value=memory) as retrieve, \
                patch.object(Budget, 'charge', charge):
            plan = capacity.measure(None, 'A', parent, data, Budget(65536, 4096, 256))
        self.assertEqual(plan['status'], 'measured')
        self.assertEqual(memory, before)
        self.assertEqual(supplied, [memory])
        self.assertEqual(retrieve.call_args.kwargs, {'mandatory_only': True})

    def test_unfitted_reviewer_does_not_invent_an_upper_bound_below_its_probe(self):
        parent, data = inputs()
        plan = capacity.measure(None, 'A', parent, data, Budget(65536, 4096, 256),
                                reviewer_budget=Budget(4096, 2048, 256))
        self.assertEqual(plan['status'], 'unavailable')
        self.assertIsNone(plan['holdback'])

    def test_metadata_corruption_invalid_controls_and_cross_session_are_rejected(self):
        parent, data = inputs()
        plan = capacity.measure(None, 'A', parent, data, Budget(65536, 4096, 256))
        changed = copy.deepcopy(plan)
        changed['holdback']['tokens'] = 0
        with self.assertRaises(InvalidUpdate): capacity.validate(changed)
        changed['digest'] = sha('discovery-capacity-v' + str(changed['version']) + ':' + canonical(
            {key: value for key, value in changed.items() if key != 'digest'}))
        with self.assertRaises(InvalidUpdate): capacity.validate(changed)
        with self.assertRaises(InvalidUpdate): capacity.gate(None, 'B', 'owner', plan)
        for seconds in (True, 0, 3601):
            with self.assertRaises(InvalidUpdate):
                capacity.measure(None, 'A', parent, data, Budget(), writer_seconds=seconds)
        for cost in ({'tokens': -1}, {'calls': True}, {'tokens': float('nan')}, {'unknown': 1}):
            with self.assertRaises(InvalidUpdate): capacity.gate(None, 'A', 'owner', plan, cost)


class ActualAllocationTests(unittest.TestCase):
    setUp = accounting_fixture.AccountingBatchTests.setUp
    tearDown = accounting_fixture.AccountingBatchTests.tearDown
    child = accounting_fixture.AccountingBatchTests.child
    dispatch = accounting_fixture.AccountingBatchTests.dispatch
    finish = accounting_fixture.AccountingBatchTests.finish

    def plan(self, budget=None, **kwargs):
        parent, data = inputs(self.s.pinned(self.sid))
        result = capacity.measure(self.s, self.sid, parent, data,
                                  budget or Budget(65536, 4096, 256), **kwargs)
        self.assertEqual(result['status'], 'measured')
        return result, parent, data

    def test_exact_kernel_boundary_and_failed_holdback_never_grow_or_spend(self):
        plan, _, _ = self.plan()
        limits = self.root['limits']
        before = machine.spent(self.s, self.root['id'])
        from review_budget import usage
        task_before = usage(self.s, self.sid)
        count = self.s.db.execute('SELECT COUNT(*) FROM orchestration_charges').fetchone()[0]
        next_tokens = limits['tokens'] - plan['holdback']['tokens']
        yes = capacity.gate(self.s, self.sid, self.root['id'], plan, {'tokens': next_tokens})
        no = capacity.gate(self.s, self.sid, self.root['id'], plan, {'tokens': next_tokens + 1})
        self.assertTrue(yes['capacity_fits'])
        self.assertFalse(no['capacity_fits'])
        self.assertEqual(no['reason'], 'downstream_holdback')
        self.assertTrue(any(f['limiting_dimension'] == 'tokens' and f['decision'] != 'ALLOW'
                            for f in no['frames']))
        self.assertEqual(machine.spent(self.s, self.root['id']), before)
        self.assertEqual(usage(self.s, self.sid), task_before)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM orchestration_charges').fetchone()[0], count)

    def test_ancestor_active_sibling_holdback_is_honored(self):
        plan, _, _ = self.plan()
        target = self.child(cost={'operations': 1, 'calls': 20, 'tokens': 100000,
                                  'source_bytes': 1024, 'seconds': 5000, 'actions': 1})
        self.assertTrue(capacity.gate(self.s, self.sid, target['id'], plan)['capacity_fits'])
        reserved = self.root['limits']['tokens'] - plan['holdback']['tokens'] + 1
        self.child(cost={'operations': 1, 'calls': 20, 'tokens': reserved,
                         'source_bytes': 1024, 'seconds': 5000, 'actions': 1})
        result = capacity.gate(self.s, self.sid, target['id'], plan)
        self.assertFalse(result['capacity_fits'])
        self.assertTrue(any(f.get('reserved_for_other_children', {}).get('tokens', 0) >= reserved
                            for f in result['frames']))

    def test_reload_and_budget_switch_do_not_refund_pending_or_settled_spending(self):
        plan, parent, data = self.plan()
        work = self.child(cost={'operations': 1, 'calls': 1, 'tokens': 1000,
                                'source_bytes': 100, 'seconds': 10, 'actions': 1})
        owner = self.dispatch(work, cost=dict(work['limits']))
        before = machine.spent(self.s, self.root['id'])
        capacity.gate(self.s, self.sid, self.root['id'], plan)
        self.assertEqual(machine.spent(self.s, self.root['id']), before)
        self.s.close(); self.s = InvocationLedger(self.path)
        capacity.validate(plan, sid=self.sid, parent_packet=parent, data=data,
                          writer_budget=Budget(65536, 4096, 256))
        capacity.gate(self.s, self.sid, self.root['id'], plan)
        self.assertEqual(machine.spent(self.s, self.root['id']), before)
        self.finish(work, owner)
        settled = machine.spent(self.s, self.root['id'])
        stronger, _, _ = self.plan(Budget(65536, 8192, 256))
        capacity.gate(self.s, self.sid, self.root['id'], stronger)
        self.assertEqual(machine.spent(self.s, self.root['id']), settled)
        self.assertGreaterEqual(settled['tokens'], 1000)
        self.assertFalse(parent['contract']['constraints']['network_allowed'])

    def test_transport_seconds_and_unknown_physical_fit_remain_separate(self):
        plan, _, _ = self.plan(writer_seconds=360, reviewer_seconds=360)
        self.assertEqual(plan['holdback']['seconds'], 720)
        remaining = self.root['limits']['seconds'] - 719
        result = capacity.gate(self.s, self.sid, self.root['id'], plan, {'seconds': remaining})
        self.assertFalse(result['capacity_fits'])
        self.assertTrue(any(f['limiting_dimension'] == 'seconds' and f['decision'] != 'ALLOW'
                            for f in result['frames']))
        window = dict(CountService().descriptor(), context_tokens=4096)
        budget = Budget(4096, 1024, 256, context_profile=2,
                        reservation_limit=65536, context_window=window)
        unknown, _, _ = self.plan(budget)
        result = capacity.gate(self.s, self.sid, self.root['id'], unknown)
        self.assertTrue(result['financial_fits'])
        self.assertIsNone(result['capacity_fits'])
        self.assertEqual(result['reason'], 'physical_fit_unknown')

    def test_per_call_financial_denial_has_decisive_kernel_frame(self):
        window = dict(CountService().descriptor(), context_tokens=65536)
        writer = Budget(65536, 4096, 256, context_profile=2,
                        reservation_limit=4096, context_window=window)
        plan, _, _ = self.plan(writer, reviewer_budget=Budget(65536, 4096, 256))
        self.assertTrue(plan['writer']['physical_fit'])
        result = capacity.gate(self.s, self.sid, self.root['id'], plan)
        self.assertFalse(result['capacity_fits'])
        self.assertEqual(result['reason'], 'downstream_per_call_allocation')
        decision = next(frame for frame in result['frames']
                        if frame.get('scope') == 'downstream_per_call_reservation'
                        and frame.get('role') == 'writer')
        self.assertNotEqual(decision['decision'], 'ALLOW')
        self.assertEqual(decision['limits'], {'tokens': 4096})
        self.assertEqual(decision['reserved_next']['tokens'], plan['writer']['required_units'])

    def test_known_legacy_physical_shortage_also_carries_context_frame(self):
        parent, data = inputs(self.s.pinned(self.sid))
        parent['request'] += ' Explain each architecture boundary carefully.' * 120
        plan = capacity.measure(self.s, self.sid, parent, data, Budget(4096, 1024, 256),
                                reviewer_budget=Budget(65536, 4096, 256))
        self.assertEqual(plan['status'], 'measured')
        self.assertFalse(plan['writer']['physical_fit'])
        result = capacity.gate(self.s, self.sid, self.root['id'], plan)
        self.assertFalse(result['capacity_fits'])
        physical = [frame for frame in result['frames']
                    if frame.get('scope') == 'downstream_physical_window']
        self.assertEqual(len(physical), 1)
        self.assertNotEqual(physical[0]['decision'], 'ALLOW')
        self.assertEqual(physical[0]['role'], 'writer')


if __name__ == '__main__':
    unittest.main()
