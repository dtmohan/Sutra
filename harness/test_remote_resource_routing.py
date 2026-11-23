"""Capacity fallback proposals never grant a provider, read, or inference."""
import copy
import json
import os
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate
import engine_profiles
import orchestration as machine
import orchestration_flow as flow
import remote_memory
import remote_profiles
import resource_routing as routing
import review_budget
import test_engine_profiles as engine_fixture
import test_orchestration as fixture
import test_remote_memory as remote_fixture
from test_conversation import MODELS


GIB = 1024 ** 3


class RemoteResourceRoutingTests(unittest.TestCase):
    setUp = fixture.OrchestrationTests.setUp
    tearDown = fixture.OrchestrationTests.tearDown
    provider = fixture.OrchestrationTests.provider
    step = fixture.OrchestrationTests.step
    until = fixture.OrchestrationTests.until

    def capacity_stop(self, stage='remote_admission'):
        profile = remote_profiles.normalize(dict(remote_fixture.profile(),
            context_tokens=65536, profile_hash=None))
        sample = remote_fixture.observation(profile)
        sample['model']['bytes'] = 18 * GIB
        sample['gpu'].update(free_bytes=1079 * 1024 ** 2,
                             used_bytes=24 * GIB - 1079 * 1024 ** 2)
        plan = remote_memory.evaluate(profile, sample, 65536, now=101)
        self.assertTrue(plan['available'])
        self.assertFalse(plan['admitted'])
        state = copy.deepcopy(plan['hardware'])
        state['runtime'] = {'kind': 'model_capacity', 'stage': stage,
            'model': profile['name'], 'inference_started': False, 'plan': plan}
        return profile, state

    def inventory(self):
        return {'models': [{'name': MODELS[tier], 'size': (i + 1) * GIB,
            'digest': str(i + 2) * 64, 'context_tokens': 8192,
            'capabilities': ['completion'], 'eligible': True}
            for i, tier in enumerate(('low', 'mid'))]}

    def options(self, profile):
        return {'tier': 'auto', 'armed': True,
                'models': dict(MODELS, high=profile['name'])}

    def test_remote_to_local_lower_context_is_a_proposal_without_spending_or_permission_changes(self):
        out = self.until('SYNTHESIS')
        _, data = flow.info(self.s, out['flow_id'])
        sid = self.meta['active']
        budget = review_budget.usage(self.s, sid)
        pinned = self.s.pinned(sid)
        children = copy.deepcopy(out['children'])
        for stage in ('remote_admission', 'remote_dispatch'):
            profile, state = self.capacity_stop(stage)
            request = self.options(profile)
            original = copy.deepcopy((data, request, state))
            with patch('remote_profiles.load', return_value=profile), \
                    patch.dict(os.environ, SUTRA_REMOTE_MODELS=json.dumps([profile['name']])), \
                    patch('remote_memory.time.time', return_value=101), \
                    patch('model_catalog.combined', return_value=self.inventory()), \
                    patch('model_identity.weights', return_value=None), \
                    patch('remote_memory.probe', side_effect=AssertionError('No remote request')), \
                    patch('runtime_memory.recover', side_effect=AssertionError('No model release')):
                proposed = routing.propose(self.s, out['flow_id'], data, request, state, 'SYNTHESIS')
            self.assertEqual(proposed['tier'], 'mid')
            self.assertEqual(proposed['model'], MODELS['mid'])
            self.assertEqual(proposed['candidate_context_tokens'], 8192)
            self.assertEqual(proposed['failed_plan']['context_tokens'], 65536)
            self.assertEqual(proposed['failed_size_bytes'], 18 * GIB)
            self.assertFalse(proposed['inference_started'])
            self.assertEqual(proposed['budget_frames'][0]['decision'], 'ALLOW')
            self.assertEqual((data, request, state), original)
            self.assertEqual(review_budget.usage(self.s, sid), budget)
            self.assertEqual(self.s.pinned(sid), pinned)
            self.assertEqual(out['children'], children)

    def test_remote_proof_rejects_identity_scope_busy_stale_and_non_capacity_stops(self):
        profile, good = self.capacity_stop()
        changes = [lambda state: state.update(available=False),
            lambda state: state.update(admitted=True),
            lambda state: state.update(blocker='runtime_busy'),
            lambda state: state['runtime'].update(kind='remote_observation'),
            lambda state: state['runtime'].update(stage='remote_pre_transport'),
            lambda state: state['runtime'].update(inference_started=True),
            lambda state: state['runtime'].update(model='sutra:remote/other'),
            lambda state: state['runtime']['plan'].update(context_tokens=131072),
            lambda state: state['runtime']['plan']['instance'].update(pid=4321),
            lambda state: state['runtime']['plan']['hardware']['sample'].update(profile_hash='0' * 64),
            lambda state: state['runtime']['plan']['hardware']['sample']['process'].update(model='other'),
            lambda state: state['runtime']['plan']['hardware']['sample']['server']['slots'][0].update(is_processing=True),
            lambda state: state['runtime']['plan'].update(estimated_increment_bytes=1),
            lambda state: state['runtime']['plan']['hardware']['sample'].update(wall_time=90)]
        with patch('remote_profiles.load', return_value=profile), \
                patch.dict(os.environ, SUTRA_REMOTE_MODELS=json.dumps([profile['name']])), \
                patch('remote_memory.time.time', return_value=101), \
                patch('model_catalog.combined', side_effect=AssertionError('No candidate inventory')):
            for change in changes:
                state = copy.deepcopy(good)
                change(state)
                with self.subTest(change=change):
                    self.assertIsNone(routing.remote_capacity_plan(state))
            self.assertIsNotNone(routing.remote_capacity_plan(good))
            recovered = dict(good, runtime_recovery={'admitted': False, 'after': good})
            self.assertEqual(routing.remote_capacity_plan(recovered), routing.remote_capacity_plan(good))
        with patch('remote_profiles.load', return_value=profile), \
                patch.dict(os.environ, SUTRA_REMOTE_MODELS='[]'), \
                patch('remote_memory.time.time', return_value=101):
            self.assertIsNone(routing.remote_capacity_plan(good))
        changed = dict(profile, profile_hash='f' * 64)
        with patch('remote_profiles.load', return_value=changed), \
                patch.dict(os.environ, SUTRA_REMOTE_MODELS=json.dumps([profile['name']])):
            self.assertIsNone(routing.remote_capacity_plan(good, historical=True))

    def test_historical_proof_identifies_saved_stop_but_does_not_grant_fresh_fallback(self):
        profile, state = self.capacity_stop()
        with patch('remote_profiles.load', return_value=profile), \
                patch.dict(os.environ, SUTRA_REMOTE_MODELS=json.dumps([profile['name']])), \
                patch('remote_memory.time.time', return_value=999):
            self.assertIsNone(routing.remote_capacity_plan(state))
            proof = routing.remote_capacity_plan(state, historical=True)
            self.assertEqual(proof['profile']['profile_hash'], profile['profile_hash'])
            self.assertEqual(proof['identity']['remote_profile_hash'], profile['profile_hash'])
            self.assertEqual(proof['model_bytes'], 18 * GIB)
            proof['plan']['context_tokens'] = 1
            self.assertEqual(state['runtime']['plan']['context_tokens'], 65536)

    def test_remote_fallback_remains_bounded_by_tiers_attempts_and_task_budget(self):
        out = self.until('SYNTHESIS')
        _, data = flow.info(self.s, out['flow_id'])
        profile, state = self.capacity_stop()
        req = self.options(profile)
        history = [{'kind': 'RESOURCE_MODEL_FALLBACK',
            'data': {'resource_fallback': {'model': MODELS[tier]}}} for tier in ('mid', 'low')]
        with patch('remote_profiles.load', return_value=profile), \
                patch.dict(os.environ, SUTRA_REMOTE_MODELS=json.dumps([profile['name']])), \
                patch('remote_memory.time.time', return_value=101), \
                patch('model_catalog.combined', return_value=self.inventory()) as inventory:
            for changed in (dict(req, tier='high'), dict(req, armed=False),
                            dict(req, models=dict(req['models'], high='unassigned'))):
                self.assertIsNone(routing.propose(self.s, out['flow_id'], data, changed, state, 'SYNTHESIS'))
            with patch('orchestration.gates', return_value=[{'decision': 'FIN'}]):
                self.assertIsNone(routing.propose(self.s, out['flow_id'], data, req, state, 'SYNTHESIS'))
            with patch('orchestration.events', return_value=history):
                self.assertIsNone(routing.propose(self.s, out['flow_id'], data, req, state, 'SYNTHESIS'))
            inventory.assert_not_called()
            with patch('orchestration.events', return_value=history[:1]):
                proposed = routing.propose(self.s, out['flow_id'], data, req, state, 'SYNTHESIS')
                self.assertEqual(proposed['tier'], 'low')
                self.assertEqual(proposed['bounded_switch']['used'], 1)

    def test_remote_candidates_need_proven_sizes_eligible_chat_context_and_distinct_known_weights(self):
        profile, state = self.capacity_stop()
        proof = {'identity': remote_profiles.identity(profile['name'], profile), 'model_bytes': 18 * GIB}
        models = self.options(profile)['models']
        for change in ({'size': None}, {'size': 18 * GIB}, {'digest': None},
                       {'digest': 'not-a-manifest'}, {'eligible': False},
                       {'context_tokens': 2048}, {'context_tokens': None}, {'provider': 'remote_llama'}):
            rows = self.inventory()
            for row in rows['models']:
                row.update(change)
            with self.subTest(change=change), patch('model_catalog.combined', return_value=rows):
                self.assertIsNone(routing._remote_candidate(models, profile['name'], [], proof))
        with patch('model_catalog.combined', return_value=self.inventory()), \
                patch('model_identity.weights', return_value=proof['identity']['weights_sha256']):
            self.assertIsNone(routing._remote_candidate(models, profile['name'], [], proof))

    def test_owned_engine_requires_bound_smaller_descriptor_existing_assignment_and_approval(self):
        raw = engine_fixture.descriptor(self.base, context=8192)
        raw['cold_memory_bytes'] = 5 * GIB
        native = engine_profiles.normalize(raw)
        profile, state = self.capacity_stop()
        proof = {'identity': remote_profiles.identity(profile['name'], profile), 'model_bytes': 18 * GIB}
        models = dict(self.options(profile)['models'], mid=native['name'])
        row = {'name': native['name'], 'eligible': True, 'provider': 'sutra_engine',
               'context_tokens': 8192, 'profile_hash': native['profile_hash']}
        env = {'SUTRA_ENGINE_MODELS': json.dumps([native['name']]),
               'SUTRA_ENGINE_APPROVALS': json.dumps({native['name']: native['profile_hash']})}
        with patch('model_catalog.combined', return_value={'models': [row]}), \
                patch('engine_profiles.load', return_value=native), patch.dict(os.environ, env):
            chosen = routing._remote_candidate(models, profile['name'], [], proof)
            self.assertEqual(chosen['tier'], 'mid')
            self.assertEqual(chosen['candidate_size_bytes'], 5 * GIB)
            self.assertEqual(chosen['candidate_manifest'], native['profile_hash'])
            self.assertEqual(chosen['candidate_identity']['engine_profile_hash'], native['profile_hash'])
            for changes in ({'SUTRA_ENGINE_MODELS': '[]'}, {'SUTRA_ENGINE_APPROVALS': '{}'}):
                with patch.dict(os.environ, changes):
                    self.assertIsNone(routing._remote_candidate(models, profile['name'], [], proof))
            for changes in ({'cold_memory_bytes': None}, {'cold_memory_bytes': 18 * GIB},
                            {'profile_hash': '0' * 64}, {'context_tokens': 16384}):
                with patch('engine_profiles.load', return_value=dict(native, **changes)):
                    self.assertIsNone(routing._remote_candidate(models, profile['name'], [], proof))

    def test_unassigned_remote_and_unknown_strata_sizes_cannot_be_candidates(self):
        profile, state = self.capacity_stop()
        proof = {'identity': remote_profiles.identity(profile['name'], profile), 'model_bytes': 18 * GIB}
        models = {'high': profile['name'], 'mid': 'sutra:remote/unassigned', 'low': 'strata:profile/low'}
        rows = {'models': [{'name': name, 'eligible': True, 'size': GIB, 'digest': '2' * 64,
                           'context_tokens': 8192} for name in models.values()]}
        with patch('model_catalog.combined', return_value=rows), \
                patch('strata_model.native_command', side_effect=AssertionError('No activation')):
            self.assertIsNone(routing._remote_candidate(models, profile['name'], [], proof))


if __name__ == '__main__':
    unittest.main()
