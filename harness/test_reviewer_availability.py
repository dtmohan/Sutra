"""Reviewer metadata outages retain accepted work without inference or refunds."""
import copy
from contextlib import contextmanager
import json
import os
import unittest
from unittest.mock import patch

import claim_pages as pages
import chat_runtime as chat
from context_manager import InvalidUpdate
from invocation_gateway import InvocationLedger
from monitoring import ResourceWait
import model_identity as identity
from paging import Budget
import report_review as review
import review_budget
import test_chat as fixture
import test_claim_pages as page_fixture
from test_model_identity import identity as known


@contextmanager
def candidates(rows, observe, *, errors=()):
    approved = {row['name'] for row in rows if row['name'].startswith('strata:')}
    with patch.dict(os.environ, SUTRA_ENGINE_ONLY='0'), \
            patch('model_catalog.combined', return_value={'models': rows, 'errors': list(errors)}), \
            patch('strata_model.assigned_models', return_value=approved), \
            patch('engine_profiles.assigned_models', return_value=set()), \
            patch.object(identity, 'observe', side_effect=observe) as probe:
        yield probe


class IdentityAvailabilityTests(unittest.TestCase):
    def test_auto_skips_each_unavailable_high_and_keeps_observed_low_identity(self):
        writer = known('local-assistant:latest', 'mid', family='qwen')
        low = known('qwen3:4b', 'low', family='qwen')
        for high in ('strata:high', 'strata:profile/test-high'):
            with self.subTest(high=high):
                rows = [{'name': high, 'eligible': True, 'parameter_count': 1},
                        {'name': low['name'], 'eligible': True, 'parameter_count': 4},
                        {'name': writer['name'], 'eligible': True, 'parameter_count': 32}]
                before = copy.deepcopy(rows)
                def probe(name, *args):
                    if name == high: raise InvalidUpdate('Strata status is unavailable on localhost')
                    return low if name == low['name'] else writer
                with candidates(rows, probe) as observed:
                    name, result = identity.choose(writer)
                self.assertEqual(name, 'qwen3:4b')
                self.assertEqual(result['reviewer_identity'], low)
                self.assertEqual(result['weight_relation'], 'different')
                identity.enforce(result, True)
                self.assertEqual(observed.call_count, 3)
                self.assertIn({'model': high, 'status': 'unavailable', 'stage': 'identity',
                               'error_kind': 'InvalidUpdate'}, result['identity_observations'])
                self.assertEqual(rows, before)

    def test_explicit_unavailable_is_not_replaced_by_an_available_model(self):
        writer = known('writer', 'mid')
        rows = [{'name': 'qwen3:4b', 'eligible': True}, {'name': 'strata:high', 'eligible': True}]
        def probe(name, *args):
            if name == 'strata:high': raise OSError('private provider response must not be repeated')
            return known(name, 'low')
        with candidates(rows, probe), self.assertRaises(identity.ReviewerUnavailable) as stopped:
            identity.choose(writer, 'strata:high')
        self.assertEqual(stopped.exception.requested_model, 'strata:high')
        self.assertNotIn('private provider response', str(stopped.exception.availability))
        self.assertTrue(any(row['status'] == 'identity_observed' for row in stopped.exception.availability))

    def test_catalog_outage_and_all_failed_probes_are_typed_without_separation(self):
        rows = [{'name': 'qwen3:4b', 'eligible': True}, {'name': 'strata:high', 'eligible': True}]
        with candidates(rows, lambda *args: (_ for _ in ()).throw(TimeoutError('unavailable'))), \
                self.assertRaises(identity.ReviewerUnavailable) as stopped:
            identity.choose(known('writer', 'writer'))
        self.assertEqual(len(stopped.exception.availability), 2)
        self.assertTrue(all(row['status'] == 'unavailable' for row in stopped.exception.availability))
        with candidates([], lambda *args: self.fail('No candidate to probe'),
                        errors=[{'model': 'strata:high', 'provider': 'strata', 'error': 'closed'}]), \
                self.assertRaises(identity.ReviewerUnavailable):
            identity.choose(known('writer', 'writer'), 'strata:high')

    def test_observable_unknown_identity_does_not_satisfy_strict_separation(self):
        rows = [{'name': 'qwen3:4b', 'eligible': True}]
        with candidates(rows, lambda name, *args: identity.unknown(name)):
            _, separation = identity.choose(known('writer', 'writer'))
        self.assertEqual(separation['weight_relation'], 'unknown')
        with self.assertRaises(InvalidUpdate): identity.enforce(separation, True)


class ReviewerAvailabilityRecoveryTests(unittest.TestCase):
    setUp = fixture.ChatTests.setUp
    tearDown = fixture.ChatTests.tearDown
    ask = fixture.ChatTests.ask
    candidate = page_fixture.ClaimPageTests.candidate
    reviewer = page_fixture.ClaimPageTests.reviewer

    def rows(self):
        return self.s.db.execute('SELECT i.id,i.record_json,r.record_json FROM invocations i '
            'LEFT JOIN invocation_receipts r ON r.invocation_id=i.id WHERE i.session=? ORDER BY i.rowid',
            (self.sid,)).fetchall()

    def stop(self, cid, *, requested='auto'):
        rows = [{'name': 'qwen3:4b', 'eligible': True}, {'name': 'strata:high', 'eligible': True}]
        with candidates(rows, lambda *args: (_ for _ in ()).throw(InvalidUpdate('Metadata unavailable'))), \
                patch('api_runtime.provider_for', side_effect=AssertionError('Unavailable identities cannot create providers')):
            return review.run(self.s, self.sid, self.storage, cid, requested=requested)

    def assert_no_attempt_stop(self, result, cid):
        stop = result['claim_review_stop']
        self.assertEqual(stop['status'], 'reviewer_unavailable')
        self.assertEqual(stop['candidate_id'], cid)
        self.assertFalse(stop['automatic_retry'])
        self.assertTrue({'plan_id', 'review_page', 'failed_invocation'}.isdisjoint(stop))
        self.assertFalse(result['admission']['admitted'])
        self.assertEqual(result['token_usage']['calls'], [])
        self.assertEqual(result['token_usage']['total_calls'], 0)

    def recover(self, cid):
        model = self.reviewer('qwen3:4b', 'low-weights')
        model.context_metadata = {'model_info': {'qwen.context_length': 65536}}
        rows = [{'name': model.model, 'eligible': True}]
        with candidates(rows, lambda *args: model.model_identity), \
                patch('api_runtime.provider_for', return_value=model), \
                patch.object(chat, 'answer', side_effect=AssertionError('Accepted writer must not replay')):
            result = review.run(self.s, self.sid, self.storage, cid, requested=model.model, strict=True)
        self.assertTrue(result['admission']['admitted'], result)
        _, publication = review.published(self.s, self.sid, cid, result['invocation_id'])
        self.assertEqual(publication['candidate_id'], cid)
        self.assertTrue(publication['all_reviewers_distinct'])
        return result, model

    def test_all_unavailable_before_plan_reload_then_low_recovers_saved_candidate(self):
        cid = self.candidate(); before = review_budget.usage(self.s, self.sid); rows = self.rows()
        draft = self.s.load_result(self.sid, cid); pinned = self.s.pinned(self.sid)
        stopped = self.stop(cid)
        self.assert_no_attempt_stop(stopped, cid)
        self.assertEqual(review_budget.usage(self.s, self.sid), before)
        self.assertEqual(self.rows(), rows); self.assertIsNone(pages.find(self.s, self.sid, cid))
        self.assertEqual(self.s.load_result(self.sid, cid), draft)
        self.s.close(); self.s = InvocationLedger(self.storage / 'state.sqlite')
        result, model = self.recover(cid)
        self.assertEqual(self.rows()[:len(rows)], rows)
        self.assertEqual(self.s.pinned(self.sid), pinned)
        self.assertEqual(len([row for row in self.rows() if json.loads(row[1])['role'] == 'chat_reviewer']), 1)
        self.assertEqual(review_budget.usage(self.s, self.sid)['used']['calls'], before['used']['calls'] + len(model.payloads))
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_completed_page_and_attempts_survive_unavailability_reload_and_model_switch(self):
        cid = self.candidate(); model = self.reviewer('first-reviewer', 'first-weights')
        partial = review.run(self.s, self.sid, self.storage, cid, model, strict=True, max_pages=1)
        self.assertIn('claim_review_pending', partial)
        plan = pages.find(self.s, self.sid, cid); complete = review.page_receipts(self.s, self.sid, plan)
        original_plan = copy.deepcopy(plan); before = review_budget.usage(self.s, self.sid); rows = self.rows()
        stopped = self.stop(cid)
        self.assert_no_attempt_stop(stopped, cid)
        self.assertEqual(review_budget.usage(self.s, self.sid), before)
        self.assertEqual(pages.find(self.s, self.sid, cid), original_plan)
        self.assertEqual(review.page_receipts(self.s, self.sid, plan), complete)
        self.s.close(); self.s = InvocationLedger(self.storage / 'state.sqlite')
        self.recover(cid)
        self.assertEqual(self.rows()[:len(rows)], rows)
        self.assertEqual(pages.find(self.s, self.sid, cid), original_plan)
        for index, iid in complete.items():
            self.assertEqual(review.page_receipts(self.s, self.sid, plan)[index], iid)
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_configuration_outage_after_identity_selection_has_no_plan_or_charge(self):
        cid = self.candidate(); before = review_budget.usage(self.s, self.sid); rows = self.rows()
        low = known('qwen3:4b', 'low-weights')
        with candidates([{'name': low['name'], 'eligible': True}], lambda *args: low), \
                patch('api_runtime.provider_for', side_effect=InvalidUpdate('Local status disappeared')):
            stopped = review.run(self.s, self.sid, self.storage, cid)
        self.assert_no_attempt_stop(stopped, cid)
        self.assertEqual(stopped['claim_review_stop']['availability'][-1]['stage'], 'provider_configuration')
        self.assertEqual(self.rows(), rows); self.assertEqual(review_budget.usage(self.s, self.sid), before)

    def test_automatic_claim_review_uses_available_capacity_and_discloses_same_weights(self):
        cid = self.candidate(); before = review_budget.usage(self.s, self.sid); rows = self.rows()
        _, env, _ = review.candidate(self.s, self.sid, cid)
        writer = env['model_identity']
        roomy = self.reviewer(writer['name'], writer['weights_sha256'])
        roomy.context_metadata = {'model_info': {'qwen.context_length': 65536}}
        too_large = known('large-checker', 'different')
        identities = {roomy.model: roomy.model_identity, too_large['name']: too_large}
        choices = [{'name': name, 'eligible': True} for name in identities]
        def provider(_root, name, *, review):
            if name != roomy.model:
                raise ResourceWait({'decision': 'BRIDGE', 'runtime': {'kind': 'model_capacity', 'inference_started': False}})
            return roomy
        with candidates(choices, lambda name, *_: identities[name]), \
                patch('api_runtime.provider_for', side_effect=provider) as probe:
            result = review.run(self.s, self.sid, self.storage, cid)
        self.assertTrue(result['admission']['admitted'], result)
        self.assertEqual([call.args[1] for call in probe.call_args_list], [too_large['name'], roomy.model])
        self.assertEqual(self.rows()[:len(rows)], rows)
        report, publication = review.published(self.s, self.sid, cid, result['invocation_id'])
        self.assertFalse(publication['all_reviewers_distinct'])
        self.assertEqual(publication['independence']['weight_relation'], 'same')
        self.assertEqual(publication['independence']['admission_attempts'], [{
            'model': too_large['name'], 'stage': 'resource_admission', 'status': 'unavailable',
            'blocker': 'model_admission', 'decision': 'BRIDGE', 'inference_started': False}])
        self.assertIn('same model weights as the writer', report['publication_note'])
        final = review_budget.usage(self.s, self.sid)
        self.assertEqual(final['limits'], before['limits'])
        self.assertEqual(final['used']['calls'], before['used']['calls'] + len(roomy.payloads))
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_pressure_preserves_strict_and_explicit_selection(self):
        cid = self.candidate(); before = review_budget.usage(self.s, self.sid); rows = self.rows()
        _, env, _ = review.candidate(self.s, self.sid, cid)
        same = env['model_identity']
        distinct = known('precise', 'distinct')
        identities = {m['name']: m for m in (same, distinct)}
        choices = [{'name': name, 'eligible': True} for name in identities]
        blocked = ResourceWait({'decision': 'BRIDGE', 'runtime': {'kind': 'model_capacity', 'inference_started': False}})
        with candidates(choices, lambda name, *_: identities[name]), \
                patch('api_runtime.provider_for', side_effect=blocked) as configure:
            result = review.run(self.s, self.sid, self.storage, cid, strict=True)
            self.assert_no_attempt_stop(result, cid)
            self.assertEqual([call.args[1] for call in configure.call_args_list], [distinct['name']])
            configure.reset_mock()
            with self.assertRaises(ResourceWait):
                review.run(self.s, self.sid, self.storage, cid, requested=distinct['name'])
            self.assertEqual([call.args[1] for call in configure.call_args_list], [distinct['name']])
        self.assertEqual(self.rows(), rows)
        self.assertEqual(review_budget.usage(self.s, self.sid), before)

    def test_no_automatic_switch_after_a_new_page_invocation(self):
        cid = self.candidate(); before = review_budget.usage(self.s, self.sid); rows = self.rows()
        first = self.reviewer('small-checker', 'distinct')
        first.context_metadata = {'model_info': {'qwen.context_length': 65536}}
        second = known('large-checker', 'other')
        identities = {first.model: first.model_identity, second['name']: second}
        choices = [{'name': first.model, 'eligible': True, 'parameter_count': 4},
                   {'name': second['name'], 'eligible': True, 'parameter_count': 8}]
        run_pages = review._review_pages
        def pause_after_invocation(*args, **kwargs):
            result = run_pages(*args, **kwargs)
            self.assertTrue(result['admission']['admitted'])
            raise ResourceWait({'decision': 'BRIDGE', 'runtime': {'kind': 'model_capacity', 'inference_started': False}})
        with candidates(choices, lambda name, *_: identities[name]), \
                patch('api_runtime.provider_for', return_value=first) as configure, \
                patch('report_review._review_pages', side_effect=pause_after_invocation), self.assertRaises(ResourceWait):
            review.run(self.s, self.sid, self.storage, cid, max_pages=1)
        self.assertEqual([call.args[1] for call in configure.call_args_list], [first.model])
        self.assertEqual(self.rows()[:len(rows)], rows)
        self.assertEqual(review_budget.usage(self.s, self.sid)['used']['calls'], before['used']['calls'] + 1)
        self.assertEqual(len(review.page_receipts(self.s, self.sid, pages.find(self.s, self.sid, cid))), 1)


if __name__ == '__main__': unittest.main()
