"""Measured discovery funding reuses existing workspace gates and paid prefixes."""
import copy
import json
import os
import unittest
import uuid
from unittest.mock import patch

from context_manager import ContextBudgetExceeded
from invocation_gateway import InvocationGateway
from lifecycle import Interrupted
from model_identity import unknown
from paging import Budget
import architecture_role as role
import chat_runtime as chat
import context_window
import orchestration as machine
import orchestration_flow as flow
import repository_tools as repo
import review_budget
import review_discovery as discovery
import workspace_allocation as allocation
import test_orchestration as fixture
from test_chat import Fake, MODELS
from test_context_window_recovery import CountService
from test_discovery_selection import SelectionWireModel


class DiscoveryAllocationTests(unittest.TestCase):
    tearDown = fixture.OrchestrationTests.tearDown
    step = fixture.OrchestrationTests.step
    until = fixture.OrchestrationTests.until

    def setUp(self):
        fixture.OrchestrationTests.setUp(self)
        legacy = patch.object(discovery, 'SELECTION_VERSION', 2)
        legacy.start(); self.addCleanup(legacy.stop)
        self.prompt = 'Explain the architecture. ' + self.prompt

    def provider(self, stage):
        if stage != 'synthesis': return fixture.OrchestrationTests.provider(self, stage)
        model = Fake(fixture.MODELS[self.tier])
        model.discovery_provider = SelectionWireModel(model.model, [{'kind': 'clarify'}], [])
        return model

    def paid_prefix(self, ready):
        sid = self.meta['active']
        model = Fake()
        with patch('report_review.run', side_effect=Interrupted('Only retain the historical writer receipt')):
            try:
                chat.answer(self.s, self.meta, self.ws, self.root,
                    {'prompt': self.prompt, 'conversation_id': ready['conversation_id'],
                     'models': MODELS, 'tier': 'low'}, provider=model)
            except Interrupted:
                pass
        row = self.s.db.execute("SELECT i.id,d.payload_json FROM invocations i JOIN deliveries d ON d.id=i.delivery_id "
            "WHERE i.session=? AND json_extract(i.record_json,'$.role')='chat_reviewer' ORDER BY i.rowid DESC LIMIT 1", (sid,)).fetchone()
        packet = json.loads(row[1]); model.budget = Budget(**packet['memory_budget'])
        total = model.budget.charge(chat.SYSTEM, packet, chat.schema(packet)) + model.budget.response_reserve
        def pay(packet, model):
            run = self.s.begin_run(sid, packet['request'], [], None, max_calls=1, max_repairs=0)
            self.s.reserve(run, 'ATTEMPT', {'attempt': 0})
            fmt = chat.schema(packet); delivery = self.s.prepare_delivery(sid, packet, model.budget.check(chat.SYSTEM, packet, fmt))
            out = InvocationGateway(self.s.path, run, model, ledger=self.s).invoke('chat_reviewer', uuid.uuid4().hex,
                chat.SYSTEM, packet, fmt, delivery)
            self.assertTrue(out['admission']['admitted'], out)
            self.s.event(run, 'ALLOW', {'invocation_id': out['invocation_id']})
        while 199496 - review_budget.usage(self.s, sid)['used']['tokens'] > 2*total:
            pay(packet, model)
        left = 199496 - review_budget.usage(self.s, sid)['used']['tokens']
        for _ in range(3):
            charge = model.budget.charge(chat.SYSTEM, packet, chat.schema(packet))
            model.budget = Budget(max(32768, left+1), left-charge, model.budget.template_reserve)
            packet['memory_budget'] = vars(model.budget).copy()
            packet['memory'] = chat.conversation_pages(self.s, packet, model.budget, materialize=True)
        pay(packet, model)
        self.assertEqual(review_budget.usage(self.s, sid)['used']['tokens'], 199496)
        return self.s.db.execute('SELECT id,record_json FROM invocation_receipts ORDER BY seq').fetchall()

    def prepare(self):
        ready = self.until('SYNTHESIS')
        captured = {}
        def stop(*args, **kwargs):
            captured['packet'] = copy.deepcopy(args[2]); captured['data'] = copy.deepcopy(args[3])
            raise Interrupted('Capture the prepared parent before inference')
        with patch.object(discovery, 'advance', side_effect=stop):
            paused = self.step(ready)
        self.assertEqual(paused['state'], 'PAUSED')
        p, data = captured['packet'], captured['data']
        with machine.scope(p['snapshot_owner']):
            prefix = self.paid_prefix(ready)
            for i in range(180):
                (self.ws/('extra_'+str(i)+'_'+'x'*90+'.py')).write_text('VALUE = 1\n')
            repo.execute(self.s, self.meta['active'], self.ws, self.root, 'catalog', resource_check=False)
        counter = CountService(); profile = dict(counter.descriptor(), context_tokens=32768)
        model = SelectionWireModel('strata:profile/allocation-high', [{'kind': 'clarify'}], [])
        model.context_metadata = {'model_info': {'fixture.context_length': 32768},
                                  '_sutra_runtime_cap': 32768, '_sutra_context_window': profile}
        model.budget = Budget(32768, 2048, 256, context_profile=2,
                             reservation_limit=50504, context_window=profile)
        model.model_identity = dict(unknown(model.model), server_model=profile['model'],
            server_started=profile['started'], server_context_tokens=profile['context_tokens'])
        context_window._counts.clear()
        return paused, p, data, counter, model, prefix

    def stop_for_funding(self, p, data, counter, model):
        with machine.scope(p['snapshot_owner']), patch.object(context_window, '_rpc', counter.rpc):
            with self.assertRaises(machine.OrchestrationStop) as stopped:
                discovery.advance(self.s, self.meta['active'], p, data, self.root, model, True)
        self.assertEqual(stopped.exception.frame['limiting_dimension'], 'tokens')
        return stopped.exception

    def test_50504_remaining_measures_mandatory_memory_then_renews_without_new_reservations(self):
        paused, p, data, counter, model, prefix = self.prepare(); sid = self.meta['active']
        before = review_budget.usage(self.s, sid); old = machine.node(self.s, p['snapshot_owner'])
        counts = self.s.db.execute('SELECT COUNT(*) FROM orchestration_charges').fetchone()[0]
        self.assertEqual(before['limits']['tokens']-before['used']['tokens'], 50504)
        fault = self.stop_for_funding(p, data, counter, model)
        self.assertEqual(model.calls, [])
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM orchestration_charges').fetchone()[0], counts)
        with patch.dict(os.environ, {allocation.ENVIRONMENT_KEY: '{}'}):
            renewed = flow.budget_pause(self.s, self.meta, self.root, {'armed': True}, paused['flow_id'],
                'SYNTHESIS', {'status': 'task_budget', 'stage': 'SYNTHESIS', 'frame': fault.frame})
        self.assertEqual(renewed['state'], 'SYNTHESIS', renewed.get('stop'))
        owner = machine.node(self.s, renewed['execution_id'])
        self.assertEqual(owner['previous'], old['id'])
        self.assertEqual(owner['permissions'], old['permissions'])
        self.assertEqual(owner['binding'], old['binding'])
        self.assertEqual(review_budget.usage(self.s, sid)['used'], before['used'])
        self.assertEqual(review_budget.usage(self.s, sid)['allocation_version'], 1)
        self.assertEqual(self.s.db.execute('SELECT id,record_json FROM invocation_receipts ORDER BY seq').fetchall(), prefix)
        with machine.scope(owner['id']), patch.object(context_window, '_rpc', counter.rpc):
            result = discovery.advance(self.s, sid, p, data, self.root, model, True)
        self.assertTrue(result['complete'])
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM orchestration_charges').fetchone()[0], counts+1)
        self.assertEqual(self.s.db.execute('SELECT id,record_json FROM invocation_receipts ORDER BY seq').fetchall()[:len(prefix)], prefix)
        self.assertEqual(p['discovery_owner'], owner['id'])
        self.assertEqual(p['snapshot_owner'], old['id'])

    def test_physical_overflow_never_requests_task_funding(self):
        _, p, data, counter, model, prefix = self.prepare(); counter.prompt_tokens = 32768
        before = review_budget.usage(self.s, self.meta['active'])
        with machine.scope(p['snapshot_owner']), patch.object(context_window, '_rpc', counter.rpc):
            result = discovery.advance(self.s, self.meta['active'], p, data, self.root, model, True)
        self.assertTrue(result['complete'])
        self.assertEqual(result['checkpoint']['stop_reason'], 'budget_boundary')
        self.assertEqual(model.calls, [])
        self.assertEqual(review_budget.usage(self.s, self.meta['active']), before)
        self.assertEqual(self.s.db.execute('SELECT id,record_json FROM invocation_receipts ORDER BY seq').fetchall(), prefix)

    def test_workspace_ceiling_denies_without_spending_reset_or_retry_loop(self):
        paused, p, data, counter, model, prefix = self.prepare(); sid = self.meta['active']
        before = review_budget.usage(self.s, sid); fault = self.stop_for_funding(p, data, counter, model)
        with patch.dict(os.environ, {allocation.ENVIRONMENT_KEY: json.dumps({'tokens': 250000})}):
            stopped = flow.budget_pause(self.s, self.meta, self.root, {'armed': True}, paused['flow_id'],
                'SYNTHESIS', {'status': 'task_budget', 'stage': 'SYNTHESIS', 'frame': fault.frame})
        self.assertEqual(stopped['state'], 'PAUSED')
        self.assertEqual(stopped['stop']['automatic_allocation']['reason'], 'workspace_limit')
        self.assertEqual(model.calls, [])
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertEqual(self.s.db.execute('SELECT id,record_json FROM invocation_receipts ORDER BY seq').fetchall(), prefix)

    def test_legacy_projection_does_not_promote_financial_shortage_to_a_new_gate(self):
        counter = CountService(); profile = counter.descriptor()
        budget = Budget(8192, 1024, 256, context_profile=2, reservation_limit=1000, context_window=profile)
        for p in ({}, {'discovery_selection_version': 0}):
            with patch.object(machine, 'gates', side_effect=AssertionError('Legacy budgeting changed')):
                discovery._request_funding(None, None, {}, budget, p, {}, ContextBudgetExceeded('financial'))


if __name__ == '__main__': unittest.main()
