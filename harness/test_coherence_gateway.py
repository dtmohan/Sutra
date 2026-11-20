"""Coherence diagnostics cannot acquire authority or spend another model call."""
import copy
import json
import unittest
from unittest.mock import patch

from context_manager import canonical, sha
from invocation_gateway import InvocationLedger
from karma_adapter import KarmaAdapter
from kernel_boundary import KernelBoundary
from paging import Budget
from review_budget import usage
import conversation
import chat_runtime
import test_chat
from test_conversation import Fake as Router
from test_task_observations import COMMITMENT, assessment


def signals(**changes):
    result = dict(binding_valid=True, references_valid=True, sources_current=True,
                  support_acyclic=True, within_limits=True, missing_references=0,
                  invalid_references=0, stale_sources=0, cross_context_references=0,
                  circular_support_components=0, unsupported_claims=0,
                  open_obligations=0, conflict_indicators=0, model_closure_proposals=0)
    result.update(changes)
    return result


class CoherenceAdapterTests(unittest.TestCase):
    def test_open_questions_are_normal_and_flags_never_grant_permissions(self):
        for pending in (0, 1, 128):
            frame = KarmaAdapter().coherence(signals(open_obligations=pending))
            self.assertEqual(frame['decision'], 'ALLOW')
            self.assertTrue(frame['observation_only'])
            self.assertFalse(frame['semantic_correctness_verified'])
            self.assertNotIn('permissions', frame)
            self.assertNotIn('allocation', frame)

    def test_structural_and_semantic_flags_suggest_bridge_not_semantic_fin(self):
        for flag, value in (('missing_references', 1), ('stale_sources', 2),
                            ('circular_support_components', 1), ('conflict_indicators', 3),
                            ('within_limits', False), ('binding_valid', False)):
            with self.subTest(flag=flag):
                frame = KarmaAdapter(KernelBoundary(k=.27, sigma_clause=2)).coherence(signals(**{flag: value}))
                self.assertEqual(frame['decision'], 'BRIDGE')
                self.assertEqual(frame['omega'], .54)
                self.assertEqual(frame['sigma'], 0.)
                self.assertTrue(frame['observation_only'])

    def test_malformed_counts_cannot_be_numeric_authority(self):
        for bad in (True, -1, 4097, 1.5, float('nan'), '1'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                KarmaAdapter().coherence(signals(conflict_indicators=bad))
        for bad in ({}, dict(signals(), permissions={'write': True}), signals(binding_valid=1)):
            with self.assertRaises(ValueError): KarmaAdapter().coherence(bad)


class CoherenceGatewayTests(unittest.TestCase):
    setUp = test_chat.ChatTests.setUp
    tearDown = test_chat.ChatTests.tearDown

    def route(self):
        prompt = 'Explain the entry and leaf. Include an example. Keep this read only.'
        def answer(result, packet):
            result.update(intent='workspace', answer='Reviewing the source.',
                          task_observation=copy.deepcopy(COMMITMENT))
        model = Router(test_chat.MODELS['low'], answer)
        routed = conversation.answer(self.s, self.meta, self.storage,
            {'prompt': prompt, 'models': test_chat.MODELS, 'tier': 'low'}, provider=model)
        return routed, prompt, model

    def work(self, routed, prompt, tier='high', value=None):
        def answer(result, packet):
            result['task_observation'] = assessment() if value is None else copy.deepcopy(value)
        model = test_chat.Fake(test_chat.MODELS[tier], answer)
        model.budget = Budget(32768 if tier == 'high' else 16384, 2048, 256)
        result = chat_runtime.answer(self.s, self.meta, self.ws, self.storage,
            {'prompt': prompt, 'models': test_chat.MODELS, 'tier': tier,
             'conversation_id': routed['result']['invocation_id']}, provider=model)
        return result, model

    def test_receipt_binds_diagnostic_without_new_prompt_call_or_permission(self):
        routed, prompt, model = self.route()
        result = routed['result']
        self.assertTrue(result['admission']['admitted'])
        self.assertEqual(len(model.payloads), 1)
        self.assertNotIn('coherence_observation', model.payloads[0])
        self.assertNotIn('coherence_observation', result['accepted_output'])
        note = result['coherence_observation']
        self.assertEqual(note['mode'], 'observe_only')
        self.assertTrue(note['kernel']['observation_only'])
        self.assertEqual(note['gateway_status'], 'accepted')
        raw = self.s.db.execute('SELECT record_json FROM invocation_receipts WHERE invocation_id=?',
                                (result['invocation_id'],)).fetchone()[0]
        receipt = json.loads(raw)
        self.assertEqual(receipt['coherence_observation'], note)
        self.assertEqual(sha('invocation-receipt-v1:' + raw), result['invocation_receipt_id'])
        self.assertTrue(self.s.verify_invocations(routed['session']))
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM host_actions').fetchone()[0], 0)

    def test_reload_model_switch_retains_context_constraints_and_all_spending(self):
        routed, prompt, _ = self.route()
        original_contract = self.s.current_contract(self.sid)
        first, large = self.work(routed, prompt)
        first_id = first['result']['invocation_id']
        raw = self.s.db.execute('SELECT record_json FROM invocation_receipts WHERE invocation_id=?', (first_id,)).fetchone()[0]
        before = usage(self.s, self.sid)
        self.s.close(); self.s = InvocationLedger(self.storage / 'state.sqlite')
        second, small = self.work(routed, prompt, tier='low')
        after = usage(self.s, self.sid)
        self.assertTrue(first['result']['admission']['admitted'])
        self.assertTrue(second['result']['admission']['admitted'])
        self.assertEqual(len(large.payloads), 1)
        self.assertEqual(len(small.payloads), 1)
        self.assertEqual(large.payloads[0]['request'], small.payloads[0]['request'])
        self.assertEqual(large.payloads[0]['contract'], small.payloads[0]['contract'])
        self.assertEqual(self.s.current_contract(self.sid), original_contract)
        self.assertEqual(before['limits'], after['limits'])
        self.assertEqual(after['used']['calls'], before['used']['calls'] + 1)
        self.assertGreater(after['used']['tokens'], before['used']['tokens'])
        self.assertEqual(self.s.db.execute('SELECT record_json FROM invocation_receipts WHERE invocation_id=?',
                                         (first_id,)).fetchone()[0], raw)
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_diagnostic_failure_never_withholds_valid_work_or_strands_a_reservation(self):
        routed, prompt, _ = self.route()
        before = usage(self.s, self.sid)
        with patch('coherence_observations.capture', side_effect=RuntimeError('untrusted diagnostic text')):
            result, model = self.work(routed, prompt)
        self.assertTrue(result['result']['admission']['admitted'])
        self.assertEqual(len(model.payloads), 1)
        self.assertEqual(result['result']['coherence_observation']['status'], 'unavailable')
        self.assertNotIn('untrusted diagnostic text', canonical(result['result']['coherence_observation']))
        after = usage(self.s, self.sid)
        self.assertEqual(after['used']['calls'], before['used']['calls'] + 1)
        pending = self.s.db.execute('SELECT count(*) FROM invocations i LEFT JOIN invocation_receipts r ON r.invocation_id=i.id WHERE r.id IS NULL').fetchone()[0]
        self.assertEqual(pending, 0)
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_loading_existing_result_does_not_reanalyse_or_promote_diagnostics(self):
        routed, prompt, _ = self.route()
        result, _ = self.work(routed, prompt)
        iid = result['result']['invocation_id']
        before = usage(self.s, self.sid)
        with patch('coherence_observations.capture', side_effect=AssertionError('no diagnostic replay')):
            loaded = self.s.load_result(self.sid, iid)
        self.assertNotIn('coherence_observation', loaded)
        self.assertEqual(loaded, result['result']['accepted_output'])
        self.assertEqual(usage(self.s, self.sid)['used'], before['used'])

    def test_cancellation_during_check_reconciles_without_replay_or_refund(self):
        from lifecycle import Interrupted, reconcile
        routed, prompt, _ = self.route()
        before = usage(self.s, self.sid)
        with patch('coherence_observations.capture', side_effect=Interrupted('cancelled at checkpoint')):
            with self.assertRaises(Interrupted): self.work(routed, prompt)
        pending = self.s.db.execute('SELECT count(*) FROM invocations i LEFT JOIN invocation_receipts r ON r.invocation_id=i.id WHERE r.id IS NULL').fetchone()[0]
        self.assertEqual(pending, 1)
        contract = copy.deepcopy(self.s.current_contract(self.sid))
        # This fixture has one sequential writer; reconcile never calls a model.
        with patch.object(test_chat.Fake, 'call', side_effect=AssertionError('no replay')):
            reconcile(self.s, self.ws)
        after = usage(self.s, self.sid)
        self.assertEqual(self.s.current_contract(self.sid), contract)
        self.assertEqual(after['limits'], before['limits'])
        self.assertEqual(after['used']['calls'], before['used']['calls'] + 1)
        self.assertGreater(after['used']['tokens'], before['used']['tokens'])
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM invocations i LEFT JOIN invocation_receipts r ON r.invocation_id=i.id WHERE r.id IS NULL').fetchone()[0], 0)
        settled = after['used']
        reconcile(self.s, self.ws)
        self.assertEqual(usage(self.s, self.sid)['used'], settled)


if __name__ == '__main__': unittest.main()
