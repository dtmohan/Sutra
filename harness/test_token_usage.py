import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import conversation
from invocation_gateway import InvocationLedger
from monitoring import dashboard
from review_budget import usage as budget_usage
from test_conversation import Fake, MODELS
from token_usage import history, measured


class MeasuredModel(Fake):
    def call(self, *args):
        proposal, record = super().call(*args)
        record['response'] = {'prompt_eval_count': 1200, 'eval_count': 80}
        return proposal, record


class TokenUsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.s = InvocationLedger(self.root/'state.sqlite')

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def ask(self, provider=None, **request):
        return conversation.answer(self.s, {}, self.root,
                                   dict({'prompt': 'Hello', 'models': MODELS, 'tier': 'low'}, **request),
                                   provider or MeasuredModel())

    def test_counts_only_from_transport_metadata_and_zero_is_known(self):
        self.assertIsNone(measured({'answer': {'input_tokens': 500}})['input_tokens'])
        for value in [True, False, -1, '100', 1.5, float('inf'), 2**53]:
            self.assertIsNone(measured({'response': {'eval_count': value}})['output_tokens'])
        self.assertEqual(measured({'response': {'eval_count': 0}})['output_tokens'], 0)

    def test_repairs_and_withheld_calls_spend_tokens_without_changing_budget(self):
        model = MeasuredModel(fault=lambda r,p: r.update(context_binding='bad') if 'repair' not in p else None)
        answer = self.ask(model)
        counts = answer['result']['token_usage']
        self.assertTrue(answer['result']['admission']['admitted'])
        self.assertEqual(counts['totals']['calls'], 2)
        self.assertEqual(counts['totals']['input_tokens'], 2400)
        self.assertEqual(counts['totals']['output_tokens'], 160)
        self.assertEqual({r['status'] for r in counts['calls']}, {'accepted', 'quarantined'})
        charged = sum(r['conservative_input_charge'] + r['output_reserve'] for r in counts['calls'])
        self.assertEqual(budget_usage(self.s, answer['session'])['used']['tokens'], charged)
        self.assertNotEqual(charged, 2560)
        self.assertTrue(self.s.verify_invocations(answer['session']))

    def test_reload_model_switch_preserves_counts_without_replaying_or_adding_prior_usage(self):
        a = self.ask()
        self.s.close()
        self.s = InvocationLedger(self.root/'state.sqlite')
        b = self.ask(MeasuredModel('large'), parent=a['result']['invocation_id'], tier='high')
        self.assertEqual(b['result']['token_usage']['totals']['calls'], 1)
        self.assertEqual(b['session'], a['session'])
        self.assertTrue(b['memory_used'])
        self.assertEqual(history(self.s.db)['totals']['input_tokens'], 2400)
        self.assertEqual(conversation.read(self.s, a['result']['invocation_id'])['result']['token_usage']['totals']['input_tokens'], 1200)

    def test_failed_transport_with_received_counts_is_accounted(self):
        class Failed(MeasuredModel):
            def call(self, *args):
                _, record = super().call(*args)
                err = RuntimeError('Incomplete generation')
                err.call_record = record
                raise err
        a = self.ask(Failed())
        self.assertFalse(a['result']['admission']['admitted'])
        self.assertEqual(a['result']['token_usage']['totals']['output_tokens'], 80)
        self.assertEqual(a['result']['token_usage']['totals']['calls'], 1)

    def test_missing_counts_are_not_zero_and_dashboard_works_without_active_task(self):
        a = self.ask(Fake())
        totals = a['result']['token_usage']['totals']
        self.assertIsNone(totals['input_tokens'])
        self.assertEqual(totals['missing_input_tokens'], 1)
        d = dashboard(self.root)
        self.assertIsNone(d['session'])
        self.assertEqual(d['token_usage']['total_calls'], 1)
        self.assertEqual(d['token_usage']['calls'][0]['stage'], 'conversation')

    def test_history_is_bounded_and_legacy_pending_are_unknown(self):
        db = sqlite3.connect(':memory:')
        try:
            db.executescript('CREATE TABLE invocations(id,run_id,record_json); CREATE TABLE invocation_receipts(invocation_id,record_json);')
            for i in range(260):
                env = {'id': str(i), 'run_id': str(i), 'session': 'test', 'role': 'worker', 'model': 'local', 'created_at': i, 'charge': 100}
                db.execute('INSERT INTO invocations VALUES(?,?,?)', (str(i), str(i), json.dumps(env)))
            db.execute('INSERT INTO invocation_receipts VALUES(?,?)', ('259', json.dumps({'status': 'accepted'})))
            data = history(db)
            self.assertEqual(data['total_calls'], 260)
            self.assertEqual(len(data['calls']), 250)
            self.assertEqual(data['calls'][0]['status'], 'accepted')
            self.assertEqual(data['calls'][1]['status'], 'pending')
            self.assertIsNone(data['totals']['output_tokens'])
            self.assertEqual(data['totals']['missing_input_tokens'], 250)
        finally:
            db.close()
