"""Fresh-page retention uses immutable source receipts and governed packet budgets."""
import copy
import json
import os
import unittest
import uuid
from unittest.mock import patch

from context_manager import ContextBudgetExceeded, InvalidUpdate, canonical
from invocation_gateway import InvocationGateway, InvocationLedger
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
import workspace_allocation
import test_governed_source_pages as pages_fixture
import test_orchestration as fixture
from test_chat import Fake, MODELS
from test_context_window_recovery import CountService
from test_discovery_selection import SelectionWireModel


class SizedCounter(CountService):
    def rpc(self, profile, method, path, body=None):
        if (method, path) == ('POST', '/v1/messages/count_tokens'):
            self.calls.append((copy.deepcopy(profile), method, path, copy.deepcopy(body)))
            # Deterministic conservative counter, with source size represented.
            return {'input_tokens': len(canonical(body).encode())}
        return super().rpc(profile, method, path, body)


class DiscoveryWorkingSetTests(unittest.TestCase):
    tearDown = fixture.OrchestrationTests.tearDown
    step = fixture.OrchestrationTests.step
    until = fixture.OrchestrationTests.until

    def setUp(self):
        pages_fixture.GovernedReadTests.setUp(self)
        self.prompt = 'Explain part0.request_flow and the architecture. ' + self.prompt
        for i in range(387):
            (self.ws / ('module_' + str(i) + '_' + 'x'*38 + '.py')).write_text('VALUE = 1\n')
        self.calls = []; self.fault = None
        self.actions = [{'kind': 'read', 'target': 'part0.py'}, {'kind': 'clarify'}]
        self.discovery_budget = Budget(27048, 2048, 256)  # Exactly 6,762B next page.
        self.exact = None; self.counter = None

    def provider(self, stage):
        if stage != 'synthesis': return fixture.OrchestrationTests.provider(self, stage)
        model = Fake(fixture.MODELS[self.tier]); model.budget = Budget(65536, 4096, 256)
        worker = SelectionWireModel(model.model, self.actions, self.calls, self.fault)
        worker.budget = self.discovery_budget
        worker.context_metadata = {'model_info': {'fixture.context_length': 65536}, '_sutra_runtime_cap': 65536}
        if self.exact:
            worker.model = 'strata:profile/working-set-' + self.tier
            worker.context_metadata = {'model_info': {'fixture.context_length': self.exact['context_tokens']},
                '_sutra_runtime_cap': self.exact['context_tokens'], '_sutra_context_window': self.exact}
            worker.model_identity = dict(unknown(worker.model), server_model=self.exact['model'],
                server_started=self.exact['started'], server_context_tokens=self.exact['context_tokens'])
            original = worker.call
            def counted(*args):
                answer, record = original(*args)
                body = next(call[3] for call in reversed(self.counter.calls)
                            if call[1:3] == ('POST', '/v1/messages/count_tokens'))
                record['response']['prompt_eval_count'] = len(canonical(body).encode())
                return answer, record
            worker.call = counted
        model.discovery_provider = worker
        self.models.append((stage, model)); return model

    def first_page(self):
        ready = self.until('SYNTHESIS'); captured = {}; advance = discovery.advance
        def watch(*args, **kwargs):
            captured['p'] = copy.deepcopy(args[2]); captured['data'] = copy.deepcopy(args[3])
            return advance(*args, **kwargs)
        with patch.object(discovery, 'advance', side_effect=watch): out = self.step(ready)
        self.assertEqual(out['state'], 'SYNTHESIS', out.get('stop'))
        sid = self.meta['active']; first = self.calls[0][0]
        records = discovery._tool_receipts(self.s, sid,
            discovery._accepted(self.s, sid, discovery.history(self.s, sid, first['discovery_key'])))
        record = records[-1]
        self.assertEqual(len(repo.catalog(self.s, sid)), 391)
        self.assertEqual(sum(len(e['text'].encode()) for e in record['result']['excerpts']), 6762)
        self.parent_packet = copy.deepcopy(captured['p']); self.parent_data = copy.deepcopy(captured['data'])
        return out, captured['p'], captured['data'], record

    def pay_to(self, out, target):
        with machine.scope(flow.info(self.s, out['flow_id'])[1]['root_node']):
            return self._pay_to(out, target)

    def _pay_to(self, out, target):
        sid = self.meta['active']; p = copy.deepcopy(self.parent_packet)
        model = Fake(p['route']['record']['choice']['model']); model.budget = Budget(65536, 4096, 256)
        chat.fit_packet(self.s, sid, p, copy.deepcopy(self.parent_data), model, False)
        def transport_failure(*args): raise OSError('Historical fixture transport failure retains its reservation')
        model.fault = transport_failure
        def pay():
            fmt = chat.schema(p); charge = model.budget.check(chat.SYSTEM, p, fmt)
            run = self.s.begin_run(sid, p['request'], [], None, max_calls=1, max_repairs=0)
            self.s.reserve(run, 'ATTEMPT', {'attempt': 0}); delivery = self.s.prepare_delivery(sid, p, charge)
            result = InvocationGateway(self.s.path, run, model, ledger=self.s).invoke('chat_reviewer', uuid.uuid4().hex,
                chat.SYSTEM, p, fmt, delivery)
            self.assertFalse(result['admission']['admitted'], result)
            self.s.event(run, 'FIN', {'invocation_id': result['invocation_id']})
        total = model.budget.charge(chat.SYSTEM, p, chat.schema(p)) + model.budget.response_reserve
        while target - review_budget.usage(self.s, sid)['used']['tokens'] > 2*total: pay()
        left = target - review_budget.usage(self.s, sid)['used']['tokens']
        for _ in range(3):
            charge = model.budget.charge(chat.SYSTEM, p, chat.schema(p))
            # Fixed bounded headroom lets the mandatory latest turn enter the
            # packet before recomputing the final exact financial reservation.
            model.budget = Budget(max(65536, left+16384), left-charge, model.budget.template_reserve)
            p['memory_budget'] = vars(model.budget).copy()
            p['memory'] = chat.conversation_pages(self.s, p, model.budget, materialize=True)
        pay(); self.assertEqual(review_budget.usage(self.s, sid)['used']['tokens'], target)

    def test_391_path_saved174398_prefix_reload_model_switch_keeps_all6762_unread_bytes(self):
        out, _, _, record = self.first_page(); sid = self.meta['active']; contract = self.s.pinned(sid)
        self.pay_to(out, 174398)
        before = review_budget.usage(self.s, sid)
        receipts = self.s.db.execute('SELECT id,record_json FROM invocation_receipts ORDER BY seq').fetchall()
        source_receipt = self.s.db.execute('SELECT record_json FROM repository_receipts WHERE id=?', (record['id'],)).fetchone()
        self.s.close(); self.s = InvocationLedger(self.root/'state.sqlite'); self.tier = 'high'
        self.discovery_budget = Budget(25200, 2048, 256)
        with patch.object(repo, 'execute', side_effect=AssertionError('Earned source must not be re-read')):
            result = self.step(out)
        self.assertEqual(result['state'], 'SYNTHESIS', result.get('stop'))
        packet, wire, _ = self.calls[-1]
        self.assertEqual(packet['discovery_selection_version'], 3)
        self.assertEqual(len(packet['repository']['catalog']), 391)
        self.assertLess(len(packet['catalog_window']), 391)
        self.assertNotIn('module_386_', wire['messages'][1]['content'])
        active = packet['active_source_page']
        self.assertFalse(active['partial_page']); self.assertIsNone(active['next_fresh_position'])
        self.assertEqual(active['prefix'], [e['id'] for e in record['result']['excerpts']])
        supplied = [e for e in packet['evidence'] if e['id'] in active['prefix']]
        self.assertEqual(''.join(e['text'] for e in supplied), ''.join(e['text'] for e in record['result']['excerpts']))
        self.assertEqual(sum(len(e['text'].encode()) for e in supplied), 6762)
        self.assertEqual(self.s.pinned(sid), contract)
        self.assertEqual(self.s.db.execute('SELECT id,record_json FROM invocation_receipts ORDER BY seq').fetchall()[:len(receipts)], receipts)
        self.assertEqual(self.s.db.execute('SELECT record_json FROM repository_receipts WHERE id=?', (record['id'],)).fetchone(), source_receipt)
        self.assertGreater(review_budget.usage(self.s, sid)['used']['tokens'], before['used']['tokens'])
        self.assertEqual(before['used']['tokens'], 174398)
        self.assertTrue(self.s.verify_invocations(sid))

    def test_smaller_exact_window_delivers_front_whole_units_with_honest_next_position(self):
        out, _, _, record = self.first_page(); sid = self.meta['active']
        # This byte-counting fixture also counts the mandatory linked-memory
        # floor. Leave room for that floor and a prefix, but not the full page.
        counter = SizedCounter(); self.counter = counter; self.exact = dict(counter.descriptor(), context_tokens=16384)
        self.discovery_budget = Budget(16384, 512, 256, context_profile=2,
            reservation_limit=65536, context_window=self.exact)
        self.tier = 'high'; context_window._counts.clear()
        with patch.object(context_window, '_rpc', counter.rpc), patch.object(repo, 'execute', side_effect=AssertionError('No source replay')):
            result = self.step(out)
        self.assertEqual(result['state'], 'SYNTHESIS', result.get('stop'))
        packet = self.calls[-1][0]; active = packet['active_source_page']; units = record['result']['excerpts']
        self.assertTrue(active['partial_page']); self.assertGreater(len(active['prefix']), 0)
        self.assertLess(len(active['prefix']), len(units)); count = len(active['prefix'])
        self.assertEqual(active['prefix'], [e['id'] for e in units[:count]])
        self.assertEqual(active['next_fresh_position'], {'line': units[count]['line'], 'column': units[count]['column']})
        self.assertEqual(packet['evidence'][0]['id'], units[0]['id'])
        self.assertGreater(len(packet['evidence'][0]['text'].encode()), 1000)
        self.assertTrue(discovery.history(self.s, sid, packet['discovery_key'])[-1][2]['admission']['admitted'])
        self.assertTrue(self.s.verify_invocations(sid))

    def test_one_unit_physical_overflow_is_honest_stop_without_source_or_charge_loss(self):
        out, _, _, record = self.first_page(); sid = self.meta['active']; before = review_budget.usage(self.s, sid)
        counter = SizedCounter(); self.counter = counter; self.exact = dict(counter.descriptor(), context_tokens=4096)
        self.discovery_budget = Budget(4096, 128, 256, context_profile=2,
            reservation_limit=65536, context_window=self.exact)
        self.tier = 'high'; context_window._counts.clear()
        with patch.object(context_window, '_rpc', counter.rpc): stopped = self.step(out)
        self.assertEqual(stopped['state'], 'PAUSED'); self.assertEqual(stopped['stop']['status'], 'context_budget')
        self.assertEqual(len(self.calls), 1); self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertEqual(repo.record_by_id(self.s, sid, record['id'])['result'], record['result'])

    def test_new_explicit_read_takes_priority_over_older_unseen_remainder(self):
        self.actions = [{'kind': 'read', 'target': 'part0.py'},
                        {'kind': 'read', 'target': 'part1.py'}, {'kind': 'clarify'}]
        out, _, _, first = self.first_page(); sid = self.meta['active']
        # Keep the new mandatory directory floor while still forcing a partial
        # coherent source page under this synthetic byte-as-token counter.
        counter = SizedCounter(); self.counter = counter; self.exact = dict(counter.descriptor(), context_tokens=16384)
        self.discovery_budget = Budget(16384, 512, 256, context_profile=2,
            reservation_limit=65536, context_window=self.exact)
        self.tier = 'high'; context_window._counts.clear()
        with patch.object(context_window, '_rpc', counter.rpc): second = self.step(out)
        self.assertEqual(second['state'], 'SYNTHESIS', second.get('stop'))
        self.assertTrue(self.calls[-1][0]['active_source_page']['partial_page'])
        before = self.s.db.execute('SELECT id,record_json FROM repository_receipts ORDER BY seq').fetchall()
        self.s.close(); self.s = InvocationLedger(self.root/'state.sqlite'); self.tier = 'low'
        with patch.object(context_window, '_rpc', counter.rpc), patch.object(repo, 'execute', side_effect=AssertionError('No read replay')):
            result = self.step(second)
        self.assertEqual(result['state'], 'SYNTHESIS', result.get('stop'))
        packet = self.calls[-1][0]
        self.assertEqual(packet['active_source_page']['path'], 'part1.py')
        self.assertTrue(any(page['path'] == 'part0.py' and page['unread_units'] > 0 for page in packet['source_page_toc']))
        self.assertEqual(repo.record_by_id(self.s, sid, first['id'])['result'], first['result'])
        self.assertEqual(self.s.db.execute('SELECT id,record_json FROM repository_receipts ORDER BY seq').fetchall(), before)

    def test_hard_financial_ceiling_pauses_without_retiring_current_source_or_refunding(self):
        out, _, _, record = self.first_page(); sid = self.meta['active']
        self.pay_to(out, 225000); before = review_budget.usage(self.s, sid)
        receipts = self.s.db.execute('SELECT id,record_json FROM invocation_receipts ORDER BY seq').fetchall()
        self.discovery_budget = Budget(7000, 512, 256)
        with patch.dict(os.environ, {workspace_allocation.ENVIRONMENT_KEY: '{"tokens":250000}'}):
            stopped = self.step(out)
        self.assertEqual(stopped['state'], 'PAUSED'); self.assertEqual(stopped['stop']['status'], 'task_budget')
        self.assertEqual(stopped['stop']['frame']['limiting_dimension'], 'tokens')
        self.assertEqual(stopped['stop']['automatic_allocation']['reason'], 'workspace_limit')
        self.assertEqual(len(self.calls), 1); self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertEqual(self.s.db.execute('SELECT id,record_json FROM invocation_receipts ORDER BY seq').fetchall(), receipts)
        self.assertEqual(repo.record_by_id(self.s, sid, record['id'])['result'], record['result'])


if __name__ == '__main__': unittest.main()
