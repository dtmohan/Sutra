"""Automatic task continuation uses bound states, not generic paused retries."""
import copy
from contextlib import contextmanager
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from context_manager import InvalidUpdate
from invocation_gateway import InvocationLedger, InvocationGateway
from local_model import LocalModel
from monitoring import ResourceWait
import orchestration as machine
import orchestration_flow as flow
import review_budget
import task_continuation as continuation
import test_orchestration as fixture
import test_monitoring as telemetry_fixture


def saved(state='PAUSED', status='resource_wait', blocker='hardware_pressure'):
    return {'flow_id': 'a' * 32, 'cursor': 'b' * 64, 'binding': {'session': 'fixed'},
            'state': state, 'recovery': None,
            'stop': {'status': status, 'blocker': blocker, 'resume_phase': 'CHILDREN',
                     'measurement': {'available': True, 'decision': 'BRIDGE'}}}


class ContinuationDispositionTests(unittest.TestCase):
    def test_only_known_runnable_stages_continue_without_observing_devices(self):
        with patch('monitoring.require_resources', side_effect=AssertionError('Describe must be pure')):
            for state in continuation.RUNNABLE:
                result = continuation.describe(saved(state))
                self.assertEqual((result['disposition'], result['next_event']), ('continue', 'step'))
            for state in continuation.APPROVAL:
                self.assertEqual(continuation.describe(saved(state))['disposition'], 'approval')
            for state in ('EXECUTING_ACTION', 'CANCELLED', 'QUARANTINED', 'CREATED'):
                self.assertEqual(continuation.describe(saved(state))['disposition'], 'stopped')

    def test_paused_unknown_outcomes_and_limits_never_become_generic_auto_resume(self):
        for status in ('provider_wait', 'interrupted', 'child_slice_timeout', 'memory_fault',
                       'context_budget', 'task_budget', 'provenance_changed', 'claim_review_timeout'):
            with self.subTest(status=status):
                result = continuation.describe(saved(status=status))
                self.assertEqual(result['disposition'], 'stopped')
                self.assertIsNone(result['next_event'])
        self.assertEqual(continuation.describe(saved(status='evidence_bridge'))['disposition'], 'evidence')
        self.assertEqual(continuation.describe(saved(status='provenance_confirmation'))['disposition'], 'approval')

    def test_readiness_is_bound_to_exact_flow_cursor_binding_and_blocker(self):
        current = saved(); baseline = continuation.describe(current)
        readiness = {key: copy.deepcopy(baseline[key]) for key in ('flow_id', 'cursor', 'binding', 'fingerprint')}
        readiness['ready'] = True
        result = continuation.describe(current, readiness)
        self.assertEqual((result['disposition'], result['next_event']), ('continue', 'resume'))
        for key in ('flow_id', 'cursor', 'binding', 'fingerprint'):
            wrong = copy.deepcopy(readiness); wrong[key] = None
            with self.subTest(key=key), self.assertRaises(InvalidUpdate):
                continuation.describe(current, wrong)
        self.assertEqual(current, saved())

    def test_unbound_busy_and_unknown_started_inference_require_inspection(self):
        current = saved(blocker='runtime_busy')
        current['stop']['measurement']['runtime'] = {'kind': 'busy'}
        self.assertEqual(continuation.describe(current)['disposition'], 'stopped')
        current['stop']['measurement']['runtime'].update(model='qwen3:4b', inference_started=False)
        self.assertEqual(continuation.describe(current)['disposition'], 'wait')
        current['stop']['measurement']['runtime']['inference_started'] = True
        self.assertEqual(continuation.describe(current)['disposition'], 'stopped')

    def test_unsettled_receipt_stops_probing_without_observing_or_dispatching(self):
        db = Mock(); db.execute.return_value = [('unsettled-invocation', 'unsettled-call')]
        ledger = SimpleNamespace(db=db)
        current = saved()
        with patch('monitoring.require_resources', side_effect=AssertionError('Unknown work cannot be retried')), \
                patch.object(continuation, '_owned_call', return_value=True):
            observed = continuation.probe(ledger, None, current)
        self.assertFalse(observed['ready'])
        self.assertEqual(continuation.describe(current, observed)['disposition'], 'stopped')

    def test_unrelated_orphans_do_not_block_another_flows_readiness(self):
        db = Mock(); db.execute.return_value = [('other', 'other-call'), ('own', 'own-call')]
        ledger = SimpleNamespace(db=db)
        with patch.object(continuation, '_owned_call', side_effect=[False, True]):
            self.assertEqual(continuation._pending_call(ledger, 'current-flow'), 'own')
        with patch.object(continuation, '_owned_call', return_value=False):
            self.assertIsNone(continuation._pending_call(ledger, 'current-flow'))


class ContinuationRecoveryTests(unittest.TestCase):
    setUp = fixture.OrchestrationTests.setUp
    tearDown = fixture.OrchestrationTests.tearDown
    provider = fixture.OrchestrationTests.provider
    step = fixture.OrchestrationTests.step
    until = fixture.OrchestrationTests.until

    def paused(self):
        out = self.until('CHILDREN'); fid = out['flow_id']
        flow.move(self.s, fid, 'PAUSED', 'RESOURCE_STOPPED', workflow_stop=saved()['stop'])
        return flow.response(self.s, fid)

    def test_hardware_probe_and_recheck_preserve_spending_then_resume_after_reload(self):
        out = self.paused(); sid = self.meta['active']
        before = review_budget.usage(self.s, sid); contract = self.s.pinned(sid)
        calls = len(self.models); children = copy.deepcopy(out['children'])
        healthy = {'admitted': True, 'available': True, 'decision': 'ALLOW', 'sample': {'wall_time': 1}}
        with patch('monitoring.require_resources', return_value=healthy), \
                patch.object(flow, 'workflow_usage', side_effect=AssertionError('No history scan during probe')):
            probed = self.step(out, event='continuation_probe')
        self.assertTrue(probed['continuation_probe']['ready'])
        self.assertEqual(probed['continuation']['next_event'], 'resume')
        self.assertEqual(probed['state'], 'PAUSED')
        self.assertEqual(probed['cursor'], out['cursor'])
        self.assertEqual(len(self.models), calls)
        self.assertEqual(review_budget.usage(self.s, sid), before)
        blocked = ResourceWait({'admitted': False, 'available': False, 'reason': 'Fresh sample unavailable'})
        with patch('monitoring.require_resources', side_effect=blocked):
            denied = self.step(probed, event='resume', continuation_fingerprint=probed['continuation']['fingerprint'])
        self.assertFalse(denied['continuation_probe']['ready'])
        self.assertEqual(denied['state'], 'PAUSED')
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite'); self.tier = 'high'
        with patch('monitoring.require_resources', return_value=healthy), patch('runtime_memory.recover', return_value=None):
            resumed = self.step(denied, event='resume', continuation_fingerprint=denied['continuation']['fingerprint'])
        self.assertEqual(resumed['state'], 'CHILDREN')
        self.assertEqual(resumed['children'], children)
        self.assertEqual(self.s.pinned(sid), contract)
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertTrue(self.s.verify_invocations(sid))

    def test_unknown_receipt_and_stop_are_never_replayed_by_probe(self):
        out = self.paused()
        changed = copy.deepcopy(out); changed['stop']['status'] = 'provider_wait'
        with patch('monitoring.require_resources', side_effect=AssertionError('No unknown-outcome polling')):
            result = continuation.probe(self.s, self.root, changed)
        self.assertFalse(result['ready'])
        self.assertEqual(continuation.describe(changed)['disposition'], 'stopped')

    def test_reload_without_ui_collector_uses_fresh_at_check_measurement(self):
        out = self.paused(); sid = self.meta['active']
        before = review_budget.usage(self.s, sid)
        old = telemetry_fixture.sample(time.time() - 30, pressure=2)
        (self.root / 'telemetry.json').write_text(json.dumps(old))
        fresh = telemetry_fixture.sample(time.time())
        self.s.close(); self.s = InvocationLedger(self.root / 'state.sqlite')
        with patch('monitoring._collect_hardware', return_value=(fresh, 'native-telemetry')) as collect:
            result = self.step(out, event='continuation_probe')
        self.assertTrue(result['continuation_probe']['ready'])
        observed = result['continuation_probe']['observation']['hardware']
        self.assertEqual(observed['sample_source'], 'host_on_demand')
        self.assertEqual(observed['sample'], fresh)
        collect.assert_called_once()
        self.assertEqual(json.loads((self.root / 'telemetry.json').read_text()), old)
        self.assertEqual(review_budget.usage(self.s, sid), before)

    def test_unchanged_probes_update_one_snapshot_without_growing_audit_history(self):
        out = self.paused(); sid = self.meta['active']
        before = review_budget.usage(self.s, sid)
        def count():
            return self.s.db.execute("SELECT COUNT(*) FROM page_audit WHERE event='TASK_READINESS_PROBED'").fetchone()[0]
        for stamp in (100, 200):
            healthy = {'admitted': True, 'available': True, 'decision': 'ALLOW',
                       'sample': {'wall_time': stamp, 'free_bytes': stamp * 1024}}
            with patch('monitoring.require_resources', return_value=healthy):
                out = self.step(out, event='continuation_probe')
            self.assertEqual(count(), 1)
        latest = json.loads(self.s.db.execute('SELECT observation_json FROM task_readiness_heads WHERE flow_id=?',
                                              (out['flow_id'],)).fetchone()[0])
        self.assertEqual(latest['observation']['hardware']['sample']['wall_time'], 200)
        blocked = ResourceWait({'admitted': False, 'available': True, 'decision': 'BRIDGE',
                                'reason': 'Memory pressure remains elevated.'})
        for _ in range(2):
            with patch('monitoring.require_resources', side_effect=blocked):
                out = self.step(out, event='continuation_probe')
            self.assertEqual(count(), 2)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM task_readiness_heads').fetchone()[0], 1)
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertTrue(self.s.audit_valid())
        with self.s.db:
            self.s.db.execute("UPDATE task_readiness_heads SET material_hash='corrupt' WHERE flow_id=?", (out['flow_id'],))
        with patch('monitoring.require_resources', side_effect=blocked), self.assertRaisesRegex(InvalidUpdate, 'head binding changed'):
            self.step(out, event='continuation_probe')

    def test_dispatch_gate_busy_observation_binds_selected_model_before_inference(self):
        provider = LocalModel(self.root / 'calls', model='strata:high')
        @contextmanager
        def busy(*args):
            raise ResourceWait({'available': False, 'admitted': False, 'runtime': {'kind': 'busy'},
                                'reason': 'The selected provider is busy.'})
            yield
        provider.dispatch_gate = busy
        self.until('CHILDREN')
        session = self.meta['active']
        run = self.s.begin_run(session, self.prompt, [], None)
        gateway = InvocationGateway(self.s.path, run, provider, ledger=self.s)
        with patch('reasoning_policy.check_provider'), self.assertRaises(ResourceWait) as stopped:
            gateway.invoke('conversation', 'unused', 'No inference', {}, {}, 'unused')
        runtime = stopped.exception.state['runtime']
        self.assertEqual(runtime['model'], 'strata:high')
        self.assertIs(runtime['inference_started'], False)
        self.assertEqual(self.s.run(run)['events'][-1]['kind'], 'RESOURCE_WAIT')
        self.assertTrue(self.s.verify_protocol(session))

    def test_summary_once_includes_classifier_and_workspace_without_spending(self):
        out = self.until('DONE'); sid = self.meta['active']
        before = review_budget.usage(self.s, sid); count = len(self.models)
        result = self.step(out, event='runner_summary')
        calls = result['runner_usage']['calls']
        self.assertTrue(any(row['stage'] == 'conversation' for row in calls))
        self.assertTrue(any(row['stage'] == 'subagent' for row in calls))
        self.assertEqual(len(calls), len({row['invocation_id'] for row in calls}))
        self.assertIn('conversation', result)
        self.assertEqual(result['continuation']['disposition'], 'finished')
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertEqual(len(self.models), count)


if __name__ == '__main__':
    unittest.main()
