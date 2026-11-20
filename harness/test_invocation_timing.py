"""Admitted timing is shared by kernel costs, transports and offline recovery."""
import copy
import io
import json
import signal
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

from context_manager import InvalidUpdate, canonical
from invocation_gateway import InvocationGateway, _permit, request_hash, transport_seconds, transport_timeout
from model_packet import build, descriptor
from paging import Budget
import chat_runtime as chat
import invocation_gateway as gateway
import invocation_timing as timing
import lifecycle
import local_model
import review_budget
import strata_model
import test_chat as fixture
from test_model_packet import WireModel


class TimingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.payload = {'review_transport_version': 1, 'request': 'Explain architecture.',
                        'contract': {'goal': 'Architecture review'}}
        self.provider = SimpleNamespace(model='local-mid', budget=Budget(65536, 8192, 256),
                                        transport_deadline_cap=360)

    def test_new_profile_is_bounded_by_role_output_and_actual_provider_support(self):
        self.assertEqual(timing.seconds(self.provider, 'chat_reviewer', self.payload), 360)
        self.assertEqual(timing.seconds(self.provider, 'subagent', self.payload), 150)
        self.assertEqual(timing.seconds(self.provider, 'architect', self.payload), 150)
        self.assertEqual(timing.seconds(self.provider, 'conversation', self.payload), 150)
        with self.assertRaises(InvalidUpdate):
            timing.seconds(self.provider, 'chat_reviewer', dict(self.payload, review_transport_version=None))

    def test_old_absence_small_output_and_owned_engine_stay_150(self):
        old = dict(self.payload); old.pop('review_transport_version')
        self.assertIsNone(timing.plan(self.provider, 'chat_reviewer', old))
        self.provider.budget = Budget(65536, 2048, 256)
        self.assertEqual(timing.seconds(self.provider, 'chat_reviewer', self.payload), 150)
        self.provider.model = 'sutra:engine/owned'; self.provider.budget = Budget(65536, 8192, 256)
        self.assertIsNone(timing.plan(self.provider, 'chat_reviewer', self.payload))
        self.assertEqual(timing.seconds(self.provider, 'chat_reviewer', self.payload), 150)
        self.provider.model = 'local-mid'
        with patch.object(lifecycle, 'hard_deadline_supported', return_value=False):
            self.assertIsNone(timing.plan(self.provider, 'chat_reviewer', self.payload))

    def test_forged_seconds_output_reserve_or_version_is_rejected(self):
        value = timing.plan(self.provider, 'chat_reviewer', self.payload)
        self.assertEqual(timing.validate(value, 'chat_reviewer', self.payload, 8192), 360)
        for change in ({'seconds': 361}, {'seconds': True}, {'version': 2}, {'response_reserve': 4096},
                       {'provider_cap': 1000}, {'extra': 1}):
            with self.subTest(change=change), self.assertRaises(InvalidUpdate):
                timing.validate(dict(value, **change), 'chat_reviewer', self.payload, 8192)
        with self.assertRaises(InvalidUpdate): timing.validate(value, 'subagent', self.payload, 8192)

    def test_insufficient_bridge_headroom_defers_before_any_dispatch(self):
        value = timing.plan(self.provider, 'chat_reviewer', self.payload)
        with patch.object(lifecycle, 'remaining', return_value=380):
            with self.assertRaises(timing.BridgeStepRequired): timing.require_headroom(value)
        with patch.object(lifecycle, 'remaining', return_value=450): timing.require_headroom(value)

    def test_strata_stream_uses_the_same_committed_deadline(self):
        for seconds in (360, 150):
            clock = SimpleNamespace(now=0.)
            clock.monotonic = lambda: clock.now
            event = {'choices': [{'delta': {'content': '{}'}, 'finish_reason': 'stop'}],
                     'usage': {'prompt_tokens': 100, 'completion_tokens': 5}}
            frames = iter([b': loading\n', ('data: '+json.dumps(event)+'\n').encode(), b'data: [DONE]\n'])
            def line(_):
                clock.now += 200 if clock.now == 0 else 1
                return next(frames)
            token = _permit.set({'transport_seconds': seconds, 'transport_deadline': float(seconds)})
            try:
                with patch.object(gateway, 'time', clock), patch.object(strata_model, 'time', clock):
                    if seconds == 360:
                        result = strata_model._stream(SimpleNamespace(readline=line), Mock(), 0., 65536, lambda _: None)
                        self.assertEqual(result['message']['content'], '{}')
                        self.assertEqual(result['eval_count'], 5)
                    else:
                        with self.assertRaises(TimeoutError):
                            strata_model._stream(SimpleNamespace(readline=line), Mock(), 0., 65536, lambda _: None)
            finally: _permit.reset(token)

    @unittest.skipUnless(lifecycle.hard_deadline_supported(), 'Requires an enforceable main-thread alarm')
    def test_absolute_call_alarm_bounds_a_dribbling_read_and_restores_bridge(self):
        with lifecycle.deadline(2):
            with self.assertRaises(TimeoutError):
                with lifecycle.call_deadline(.03):
                    while True: time.sleep(.005)
            self.assertGreater(lifecycle.remaining(), 1.5)
            self.assertGreater(signal.getitimer(signal.ITIMER_REAL)[0], 1.5)


class TimedWire(WireModel):
    transport_deadline_cap = 360
    interrupt = False

    def call(self, cid, system, payload, schema):
        self.observed_seconds = transport_seconds()
        if self.interrupt: raise KeyboardInterrupt('Simulated bridge exit after durable admission')
        return super().call(cid, system, payload, schema)


class TimingLedgerTests(unittest.TestCase):
    setUp = fixture.ChatTests.setUp
    tearDown = fixture.ChatTests.tearDown

    def packet(self):
        model = WireModel(); model.budget = Budget(65536, 4096, 256)
        with patch('report_review.run', side_effect=InvalidUpdate('Retain only a draft fixture')):
            fixture.ChatTests.ask(self, 'Explain the architecture.', tier='low', provider=model)
        return copy.deepcopy(model.payloads[0])

    def invoke(self, p, model):
        model.budget = Budget(**p['memory_budget'])
        model.context_metadata = {'thinking': p['reasoning']['capabilities']}
        model.thinking = p['reasoning']['requested']
        fmt = chat.schema(p)
        run = self.s.begin_run(self.sid, p['request'], [], None, max_calls=1, max_repairs=0)
        self.s.reserve(run, 'ATTEMPT', {'attempt': 0})
        delivery = self.s.prepare_delivery(self.sid, p, model.budget.check(chat.SYSTEM, p, fmt))
        return InvocationGateway(self.s.path, run, model, ledger=self.s).invoke('chat_reviewer', uuid.uuid4().hex,
            chat.SYSTEM, p, fmt, delivery)

    def test_360_admission_and_provider_permit_match_without_changing_wire_or_thinking(self):
        p = self.packet(); p['review_transport_version'] = 1
        old = dict(p); old.pop('review_transport_version')
        self.assertEqual(build(chat.SYSTEM, p, chat.schema(p)), build(chat.SYSTEM, old, chat.schema(old)))
        model = TimedWire(); out = self.invoke(p, model)
        self.assertTrue(out['admission']['admitted'], out)
        env = json.loads(self.s.db.execute('SELECT record_json FROM invocations WHERE id=?', (out['invocation_id'],)).fetchone()[0])
        cost = json.loads(self.s.db.execute('SELECT cost_json FROM orchestration_charges WHERE id=?',
            (env['orchestration']['charge_id'],)).fetchone()[0])
        self.assertEqual(cost['seconds'], 360)
        self.assertEqual(model.observed_seconds, 360)
        self.assertEqual(env['transport_timing']['seconds'], 360)
        self.assertEqual(model.thinking, p['reasoning']['requested'])
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_pending_and_interrupted_calls_retain_exact_committed_seconds_offline(self):
        base = self.packet()
        for marked, seconds in ((True, 360), (False, 150)):
            p = copy.deepcopy(base)
            if marked: p['review_transport_version'] = 1
            else: p.pop('review_transport_version', None)
            before = review_budget.usage(self.s, self.sid)
            model = TimedWire(); model.interrupt = True
            with self.assertRaises(KeyboardInterrupt): self.invoke(p, model)
            pending = review_budget.usage(self.s, self.sid)
            self.assertAlmostEqual(pending['used']['seconds'], before['used']['seconds']+seconds)
            lifecycle.reconcile(self.s, self.ws)
            after = review_budget.usage(self.s, self.sid)
            self.assertEqual(after, pending)
            receipt = json.loads(self.s.db.execute('SELECT record_json FROM invocation_receipts ORDER BY seq DESC LIMIT 1').fetchone()[0])
            self.assertEqual(receipt['elapsed_seconds'], seconds)
            self.assertEqual(receipt['status'], 'quarantined')
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_denied_360_seconds_never_reaches_provider(self):
        p = self.packet(); p['review_transport_version'] = 1
        before = self.s.db.execute('SELECT COUNT(*) FROM invocations').fetchone()[0]
        original = review_budget.require
        seen = []
        def deny(s, sid, cost):
            if cost.get('seconds') == 360:
                from karma_adapter import KarmaAdapter
                seen.append(cost)
                used = {key: 0 for key in cost}
                limits = {key: max(value, 1) for key, value in cost.items()}
                limits['seconds'] = 300
                raise review_budget.BudgetStop(KarmaAdapter().allocations(used, limits, cost))
            return original(s, sid, cost)
        model = TimedWire()
        with patch.object(review_budget, 'require', side_effect=deny):
            with self.assertRaises(review_budget.BudgetStop): self.invoke(p, model)
        self.assertTrue(seen)
        self.assertEqual(model.payloads, [])
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM invocations').fetchone()[0], before)

    def test_local_transport_accepts_200_second_stream_only_under_committed_long_limit(self):
        p = self.packet(); fmt = chat.schema(p)
        for seconds in (360, 150):
            clock = SimpleNamespace(now=0.)
            clock.monotonic = lambda: clock.now
            chunks = iter([
                {'message': {'content': ''}, 'done': False},
                {'message': {'content': '{"value":1}'}, 'done': True, 'done_reason': 'stop',
                 'prompt_eval_count': 100, 'eval_count': 5},
            ])
            def line(_):
                clock.now += 200 if clock.now == 0 else 1
                return (json.dumps(next(chunks))+'\n').encode()
            response = SimpleNamespace(status=200, readline=line)
            connection = Mock(); connection.getresponse.return_value = response
            model = local_model.LocalModel(self.storage/'calls', model='fixture-small', budget=Budget(**p['memory_budget']))
            model.thinking = p['reasoning']['requested']; model.context_metadata = {'thinking': p['reasoning']['capabilities']}
            packet = build(chat.SYSTEM, p, fmt)
            token = _permit.set({'used': False, 'hash': request_hash('slow', model.model, chat.SYSTEM, p, fmt),
                'packet_hash': descriptor(packet, model.budget.template_reserve)['packet_hash'],
                'transport_seconds': seconds, 'transport_deadline': float(seconds)})
            try:
                with patch.object(local_model, 'time', clock), patch.object(gateway, 'time', clock), \
                        patch('http.client.HTTPConnection', return_value=connection):
                    if seconds == 360:
                        answer, _ = model.call('slow', chat.SYSTEM, p, fmt)
                        self.assertEqual(answer, {'value': 1})
                    else:
                        with self.assertRaises(TimeoutError): model.call('slow', chat.SYSTEM, p, fmt)
            finally: _permit.reset(token)


if __name__ == '__main__': unittest.main()
