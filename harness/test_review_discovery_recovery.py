"""Independent discovery recovery checks using only deterministic temp fixtures."""
import copy
import json
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate
from invocation_gateway import InvocationLedger, consume_dispatch
from lifecycle import Interrupted
from paging import Budget, PageFault
from review_role import constraint_rows
import orchestration as machine
import orchestration_flow as flow
import repository_tools as repo
import review_budget
import review_discovery as discovery
import review_source_receipts
import context_window
from monitoring import ResourceWait
from test_context_window_recovery import CountService
from karma_adapter import KarmaAdapter
import test_orchestration as fixture
from test_chat import Fake


class Architect:
    def __init__(self, model, calls):
        self.model, self.calls = model, calls
        self.budget = Budget(32768, 1024, 256)

    def call(self, cid, system, packet, schema):
        consume_dispatch(cid, self.model, system, packet, schema)
        self.calls.append(copy.deepcopy(packet))
        selected = {entry['path'] for entry in packet['evidence']}
        target = next((name for name in ('part0.py', 'part1.py') if name not in selected), '')
        kind = 'read' if target else 'report'
        value = {'task_id': packet['contract']['task_id'],
                 'constraints': constraint_rows(packet['contract']),
                 'summary': 'Inspect the real source before synthesis.',
                 'findings': [{'page_id': entry['id'], 'quote': entry['text'].splitlines()[0],
                               'note': 'Observed source in ' + entry['path']}
                              for entry in packet['evidence'][:2]],
                 'completed': [], 'remaining': ['Check the remaining request links.'],
                 'tool': {'kind': kind, 'target': target, 'query': '', 'line': 1},
                 'edges': [], 'complete': False}
        if getattr(self, 'fault', None):
            self.fault(value)
        return value, {'call_id': cid, 'ok': True, 'answer': value,
                       'response': {'prompt_eval_count': 400, 'eval_count': 80}}


class DiscoveryRecoveryTests(unittest.TestCase):
    tearDown = fixture.OrchestrationTests.tearDown
    step = fixture.OrchestrationTests.step
    until = fixture.OrchestrationTests.until

    def setUp(self):
        fixture.OrchestrationTests.setUp(self)
        self.prompt = 'Explain the architecture and tools logic. ' + self.prompt
        self.discovery_calls = []
        self.captured = []

    def provider(self, stage):
        if stage != 'synthesis':
            return fixture.OrchestrationTests.provider(self, stage)
        model = Fake(fixture.MODELS[self.tier])
        model.budget = Budget(32768, 4096, 256)
        model.discovery_provider = Architect(model.model, self.discovery_calls)
        model.discovery_provider.fault = getattr(self, 'discovery_fault', None)
        self.models.append((stage, model))
        return model

    def watch(self, *args, **kwargs):
        self.captured.append((copy.deepcopy(args[2]), copy.deepcopy(args[3])))
        return self.advance(*args, **kwargs)

    def discover(self):
        ready = self.until('SYNTHESIS')
        self.advance = discovery.advance
        with patch.object(discovery, 'advance', side_effect=self.watch):
            pending = self.step(ready)
        self.assertEqual(pending['state'], 'SYNTHESIS', pending)
        self.assertEqual(len(self.discovery_calls), 1)
        self.assertEqual(flow.info(self.s, pending['flow_id'])[1]['parent_evidence']['version'], 4)
        return pending

    def test_accepted_plan_tool_gap_survives_reload_and_model_switch_without_replay(self):
        ready = self.until('SYNTHESIS'); sid = self.meta['active']
        execute = repo.execute

        def stop_before_tool(*args, **kwargs):
            if kwargs.get('invocation_id'):
                raise Interrupted('Fixture stopped after the accepted decision.')
            return execute(*args, **kwargs)

        with patch.object(repo, 'execute', side_effect=stop_before_tool):
            paused = self.step(ready)
        self.assertEqual(paused['state'], 'PAUSED', paused)
        self.assertEqual(paused['stop']['status'], 'interrupted')
        self.assertEqual(len(self.discovery_calls), 1)
        first = self.discovery_calls[0]
        accepted = discovery._accepted(self.s, sid, discovery.history(self.s, sid, first['discovery_key']))
        self.assertEqual(len(accepted), 1)
        iid = accepted[0][0]
        old_receipt = self.s.db.execute('SELECT record_json FROM invocation_receipts WHERE invocation_id=?', (iid,)).fetchone()[0]
        before = review_budget.usage(self.s, sid)
        self.s.close(); self.s = InvocationLedger(self.root/'state.sqlite')
        self.meta = json.loads((self.root/'workspace.json').read_text()); self.tier = 'high'
        resumed = self.step(paused, event='resume')
        recovered = self.step(resumed)
        self.assertEqual(recovered['state'], 'SYNTHESIS', recovered)
        self.assertEqual(len(self.discovery_calls), 1)
        after = review_budget.usage(self.s, sid)
        self.assertEqual(after['used']['calls'], before['used']['calls'])
        self.assertEqual(after['used']['tokens'], before['used']['tokens'])
        self.assertEqual(after['used']['operations'], before['used']['operations'] + 1)
        records = discovery._tool_receipts(self.s, sid, accepted)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['operation']['invocation_id'], iid)
        self.assertEqual(records[0]['operation']['target'], 'part0.py')
        self.assertEqual(self.s.db.execute('SELECT record_json FROM invocation_receipts WHERE invocation_id=?', (iid,)).fetchone()[0], old_receipt)
        done = self.until('DONE', recovered)
        self.assertTrue(done['data']['result']['admission']['admitted'], done)
        self.assertEqual(len(self.discovery_calls), 3)
        self.assertEqual(self.discovery_calls[1]['derived_from'], [iid])
        for packet in (self.discovery_calls[0], self.discovery_calls[1]):
            self.assertTrue(any('CED724' in pin['quote']
                                for pin in packet['memory']['pinned_user_statements']))
            self.assertFalse(packet['contract']['constraints']['edits_allowed'])
            self.assertFalse(packet['contract']['constraints']['network_allowed'])
        self.assertTrue(self.s.verify_invocations(sid))

    def test_saved_discovery_checks_source_bytes_and_execution_identity(self):
        pending = self.discover(); sid = self.meta['active']
        packet = self.discovery_calls[0]
        p, data = self.captured[0]
        accepted = discovery._accepted(self.s, sid, discovery.history(self.s, sid, packet['discovery_key']))
        records = discovery._tool_receipts(self.s, sid, accepted)
        discovery._finish(self.s, sid, p, packet['discovery_key'], accepted, records, 'budget_boundary')
        self.assertEqual(set(discovery.load(self.s, sid, p)), {'part0.py'})
        forged = copy.deepcopy(p); forged['evidence_discovery']['receipts'] = []
        with self.assertRaisesRegex(InvalidUpdate, 'checkpoint changed'):
            discovery.load(self.s, sid, forged)
        wrong = copy.deepcopy(p); wrong['request'] = 'Another request'
        with self.assertRaisesRegex(InvalidUpdate, 'execution binding'):
            discovery.load(self.s, sid, wrong)
        wrong = copy.deepcopy(p); wrong['snapshot_owner'] = pending['flow_id']
        with self.assertRaisesRegex(InvalidUpdate, 'execution binding'):
            discovery.load(self.s, sid, wrong)
        owner = machine.node(self.s, p['snapshot_owner'])
        fields = {key: copy.deepcopy(value) for key, value in owner.items()
                  if key not in ('id', 'kind', 'session', 'parent_id', 'initial_state', 'created_at')}
        fields['flow_id'] = 'another-flow'
        fields['limits'] = dict(owner['limits'], actions=1000)
        with self.s.db:
            other = machine.create(self.s, 'execution', sid, None, **fields)
        wrong = copy.deepcopy(p); wrong['snapshot_owner'] = other['id']
        with self.assertRaisesRegex(InvalidUpdate, 'checkpoint changed|source owner'):
            discovery.load(self.s, sid, wrong)
        original = (self.ws/'part0.py').read_text()
        (self.ws/'part0.py').write_text(original.replace('return 0', 'return 9'))
        with self.assertRaises((PageFault, InvalidUpdate)):
            discovery.load(self.s, sid, p)
        self.assertEqual(self.discovery_calls, [packet])

    def test_three_invalid_out_of_catalog_plans_stop_partially_without_tool_or_refund(self):
        self.discovery_fault = lambda proposal: proposal['tool'].update(target='../private.py')
        ready = self.until('SYNTHESIS'); sid = self.meta['active']
        before = review_budget.usage(self.s, sid)
        old_ops = self.s.db.execute('SELECT id FROM repository_ops ORDER BY rowid').fetchall()
        current = ready
        for _ in range(3):
            current = self.step(current)
            self.assertEqual(current['state'], 'SYNTHESIS', current)
        after = review_budget.usage(self.s, sid)
        self.assertEqual(len(self.discovery_calls), 3)
        self.assertEqual(after['used']['calls'], before['used']['calls'] + 3)
        self.assertGreater(after['used']['tokens'], before['used']['tokens'])
        self.assertEqual(after['limits'], before['limits'])
        all_ops = self.s.db.execute('SELECT id,record_json FROM repository_ops ORDER BY rowid').fetchall()
        self.assertEqual([iid for iid, _ in all_ops[:len(old_ops)]], [row[0] for row in old_ops])
        self.assertFalse([json.loads(raw) for _, raw in all_ops if json.loads(raw).get('invocation_id')])
        saved = self.step(current)
        self.assertEqual(saved['state'], 'SYNTHESIS', saved)
        self.assertEqual(len(self.discovery_calls), 3)
        candidates = [model.payloads[-1] for stage, model in self.models
                      if stage == 'synthesis' and model.payloads]
        self.assertTrue(candidates)
        coverage = candidates[-1]['discovery_coverage']
        self.assertEqual(coverage['stop_reason'], 'proposal_rejected')
        self.assertFalse(coverage['exhaustive_semantic_review'])
        self.assertEqual(coverage['tool_results'], 0)

    def test_accepted_plan_cannot_import_a_tool_receipt_from_another_execution(self):
        ready = self.until('SYNTHESIS'); sid = self.meta['active']
        execute = repo.execute

        def stop_before_tool(*args, **kwargs):
            if kwargs.get('invocation_id'):
                raise Interrupted('Fixture stops before the tool.')
            return execute(*args, **kwargs)

        with patch.object(repo, 'execute', side_effect=stop_before_tool):
            self.step(ready)
        packet = self.discovery_calls[0]
        accepted = discovery._accepted(self.s, sid, discovery.history(self.s, sid, packet['discovery_key']))
        iid = accepted[0][0]
        owner = machine.node(self.s, packet['snapshot_owner'])
        fields = {key: copy.deepcopy(value) for key, value in owner.items()
                  if key not in ('id', 'kind', 'session', 'parent_id', 'initial_state', 'created_at')}
        fields['flow_id'] = 'another-flow'
        fields['limits'] = dict(owner['limits'], actions=1000)
        with self.s.db:
            other = machine.create(self.s, 'execution', sid, None, **fields)
        with machine.scope(other['id']):
            try:
                repo.execute(self.s, sid, self.ws, self.root, 'read', target='part0.py',
                             invocation_id=iid, request_id=discovery._request_id(iid), resource_check=False)
            except InvalidUpdate as exc:
                self.assertRegex(str(exc), 'discovery|proposal|execution|workflow|scope')
                return
        with self.assertRaises(InvalidUpdate):
            discovery._tool_receipts(self.s, sid, accepted)

    def test_renewed_execution_reuses_done_source_owner_and_discovery_chain(self):
        from execution_sessions import renew_after_grant
        pending = self.discover(); sid = self.meta['active']
        p, data = self.captured[0]
        old = p['snapshot_owner']
        first = self.discovery_calls[0]
        old_receipts = copy.deepcopy(p['snapshot_receipts'])
        initial = discovery._accepted(self.s, sid, discovery.history(self.s, sid, first['discovery_key']))
        first_iid = initial[0][0]
        with self.s.db:
            machine.transition(self.s, pending['flow_id'], 'PAUSED', 'FIXTURE_ALLOCATION_STOP',
                               {'workflow_stop': {'resume_phase': 'SYNTHESIS'}})
            renewed = renew_after_grant(self.s, pending['flow_id'])
        self.assertEqual(machine.state(self.s, old), 'DONE')
        self.assertEqual(renewed['previous'], old)
        request = {'prompt': self.prompt, 'conversation_id': pending['conversation_id'],
                   'child_results': flow.info(self.s, pending['flow_id'])[1]['child_results'],
                   'orchestration_parent': old}
        before = review_budget.usage(self.s, sid)
        model = Architect(fixture.MODELS['high'], self.discovery_calls)
        with machine.scope(renewed['id']):
            receipts, reused, plan = review_source_receipts.acquire(self.s, self.meta, self.ws,
                self.root, request, list(data['sources']), real=False)
            self.assertEqual(receipts, old_receipts)
            self.assertEqual(plan['snapshot_owner'], old)
            self.assertEqual(reused['sources'], data['sources'])
            progressed = discovery.advance(self.s, sid, p, data, self.root, model, False)
        self.assertFalse(progressed['complete'])
        self.assertEqual(p['snapshot_owner'], old)
        self.assertEqual(p['discovery_owner'], renewed['id'])
        self.assertEqual(len(self.discovery_calls), 2)
        self.assertEqual(self.discovery_calls[-1]['derived_from'], [first_iid])
        self.assertEqual(self.discovery_calls[-1]['discovery_key'], first['discovery_key'])
        after = review_budget.usage(self.s, sid)
        self.assertEqual(after['used']['calls'], before['used']['calls'] + 1)
        self.assertEqual(after['used']['operations'], before['used']['operations'] + 1)
        self.assertEqual(after['limits'], before['limits'])
        with machine.scope(renewed['id']):
            finished = discovery.advance(self.s, sid, p, data, self.root, model, False)
        self.assertTrue(finished['complete'])
        self.assertEqual(set(finished['followups']), {'part0.py', 'part1.py'})
        with self.s.db:
            machine.transition(self.s, renewed['id'], 'DONE', 'FIXTURE_FINISHED')
        self.assertEqual(discovery.load(self.s, sid, p), finished['followups'])

    def test_final_discovery_call_returns_before_writer_dispatch(self):
        pending = self.discover(); sid = self.meta['active']
        second = self.step(pending)
        saved = self.step(second)
        self.assertEqual(saved['state'], 'SYNTHESIS', saved)
        self.assertEqual(len(self.discovery_calls), 3)
        self.assertTrue(flow.info(self.s, saved['flow_id'])[1]['parent_evidence']['complete'])
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM invocations WHERE session=? "
            "AND json_extract(record_json,'$.role')='chat_reviewer'", (sid,)).fetchone()[0], 0)
        drafted = self.step(saved)
        self.assertEqual(drafted['state'], 'SYNTHESIS', drafted)
        self.assertEqual(len(self.discovery_calls), 3)
        self.assertTrue(flow.info(self.s, drafted['flow_id'])[1]['parent_checkpoint'])

    def counted_provider(self):
        counter = CountService()
        window = counter.descriptor()
        model = Architect('strata:profile/test-high', self.discovery_calls)
        model.context_metadata = {'model_info': {'fixture.context_length': 8192},
                                  '_sutra_runtime_cap': 8192, '_sutra_context_window': window}
        model.budget = Budget(8192, 1024, 256, context_profile=2,
                              reservation_limit=65536, context_window=window)
        return counter, model

    def test_exact_counter_unavailable_stops_before_dispatch_without_charge(self):
        self.discover(); sid = self.meta['active']; p, data = self.captured[0]
        before = review_budget.usage(self.s, sid)
        counter, model = self.counted_provider(); counter.fail_counts = True
        original = model.budget
        context_window._counts.clear()
        with machine.scope(p['snapshot_owner']), patch.object(context_window, '_rpc', counter.rpc):
            with self.assertRaises(ResourceWait):
                discovery.advance(self.s, sid, p, data, self.root, model, True)
        self.assertEqual(len(self.discovery_calls), 1)
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertEqual(model.budget, original)
        self.assertTrue(counter.calls)
        self.assertTrue(all(path != '/v1/chat/completions' for _, _, path, _ in counter.calls))

    def test_financial_preflight_keeps_writer_and_reviewer_calls_before_discovery(self):
        self.discover(); sid = self.meta['active']; p, data = self.captured[0]
        before = review_budget.usage(self.s, sid)
        counter, model = self.counted_provider()
        require = review_budget.require
        protected = []

        def stop_before_protected_calls(store, session, cost):
            if cost.get('calls') == 3:
                protected.append(copy.deepcopy(cost))
                frame = KarmaAdapter().allocations({'calls': 1}, {'calls': 3}, {'calls': 3})
                raise review_budget.BudgetStop(frame)
            return require(store, session, cost)

        context_window._counts.clear()
        with machine.scope(p['snapshot_owner']), patch.object(context_window, '_rpc', counter.rpc), \
                patch.object(review_budget, 'require', side_effect=stop_before_protected_calls):
            with self.assertRaises((machine.OrchestrationStop, review_budget.BudgetStop)):
                discovery.advance(self.s, sid, p, data, self.root, model, True)
        self.assertTrue(protected)
        self.assertEqual(protected[0]['calls'], 3)
        self.assertGreater(protected[0]['tokens'], 0)
        self.assertEqual(len(self.discovery_calls), 1)
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertNotIn('evidence_discovery', p)


if __name__ == '__main__':
    unittest.main()
