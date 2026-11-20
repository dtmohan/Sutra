import copy
import json
import unittest
from unittest.mock import patch

import allocation_recovery
import evidence_admission
import native_access
import orchestration as m
import orchestration_flow as flow
import review_budget
from context_manager import InvalidUpdate
from invocation_gateway import InvocationLedger
from test_conversation import Fake, MODELS
import test_orchestration as fixtures


class SourceScopeRecoveryTests(unittest.TestCase):
    setUp = fixtures.OrchestrationTests.setUp
    tearDown = fixtures.OrchestrationTests.tearDown
    step = fixtures.OrchestrationTests.step
    until = fixtures.OrchestrationTests.until

    def provider(self, stage):
        if stage != 'conversation':
            return fixtures.OrchestrationTests.provider(self, stage)
        model = Fake(MODELS[self.tier], lambda result, _: result.update(
            intent='workspace', workspace_request={
                'operation': 'read', 'paths': ['app.py', 'tools.py', 'config.py']}))
        self.models.append((stage, model))
        return model

    def saved_bad_scope(self):
        # Recreate an older accepted router result without changing live data.
        with patch.object(flow, 'workspace_target',
                          side_effect=lambda proposal, prompt: (proposal, *evidence_admission.workspace_roots(proposal))):
            selected = self.step()
        self.assertEqual(selected['state'], 'NEED_ARM')
        saved = self.step(selected)
        self.assertEqual(saved['recovery']['kind'], 'source_scope_correction')
        self.assertEqual(saved['recovery']['source_roots'], ['.'])
        return saved

    def restart(self, out, **fields):
        return self.step(out, event='restart_review', confirmed=True,
                         binding=out['recovery']['binding'], **fields)

    def amend(self, out):
        flow.move(self.s, out['flow_id'], 'PAUSED', 'CONTEXT_STOPPED',
                  workflow_stop={'status': 'context_budget', 'error': 'Fixture allocation stop'})
        out = flow.response(self.s, out['flow_id'])
        preview = self.step(out, event='allocation_preview', additions={'tokens': 4096, 'source_bytes': 2048})
        return self.step(preview, event='allocation_grant', confirmed=True, proposal=preview['allocation_proposal'])

    def test_confirmed_restart_versions_scope_and_preserves_exact_spending_and_amendment_prefix(self):
        self.prompt = '\n' + self.prompt + '\n'
        saved = self.amend(self.saved_bad_scope())
        sid = self.meta['active']
        before = review_budget.usage(self.s, sid)
        old = copy.deepcopy(self.s.snapshot(sid))
        old_hash = self.s.origin(sid)['contract_hash']
        effective = allocation_recovery.effective(self.s, sid, before['base_limits'])
        self.assertEqual(before['allocation_version'], 1)
        with self.assertRaisesRegex(InvalidUpdate, 'explicit user confirmation'):
            self.step(saved, event='restart_review', binding=saved['recovery']['binding'])
        with self.assertRaisesRegex(InvalidUpdate, 'user-armed'):
            self.restart(saved, armed=False)
        self.assertEqual(review_budget.usage(self.s, sid), before)

        restarted = self.restart(saved)
        self.assertEqual(self.meta['active'], sid)
        self.assertNotEqual(restarted['flow_id'], saved['flow_id'])
        self.assertEqual(m.state(self.s, saved['flow_id']), 'CANCELLED')
        current = self.s.snapshot(sid)
        self.assertEqual(current['goal'], old['goal'])
        self.assertEqual(current['constraints'], dict(old['constraints'], allowed_paths=['.']))
        self.assertNotEqual(self.s.origin(sid)['contract_hash'], old_hash)
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertEqual(allocation_recovery.effective(self.s, sid, before['base_limits']), effective)
        self.assertEqual(restarted['conversation_id'], saved['conversation_id'])

        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        self.tier = 'high'
        self.assertEqual(review_budget.usage(self.s, sid), before)
        done = self.until('DONE', restarted)
        self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertEqual(self.s.snapshot(sid)['constraints'], current['constraints'])
        self.assertEqual(review_budget.usage(self.s, sid)['limits'], before['limits'])
        self.assertGreater(review_budget.usage(self.s, sid)['used']['calls'], before['used']['calls'])
        self.assertTrue(self.s.audit_valid())
        self.assertTrue(self.s.verify_invocations(sid))

    def test_interrupted_scope_commit_recovers_same_intent_without_double_spending(self):
        saved = self.amend(self.saved_bad_scope())
        sid = self.meta['active']
        before = review_budget.usage(self.s, sid)
        original_audit = self.s._audit

        def interrupt(event, payload):
            if event == 'NATIVE_PERMISSION_GRANT' and payload.get('operation') == 'read_scope_correction':
                raise RuntimeError('Interrupted after scope epoch')
            return original_audit(event, payload)

        with patch.object(self.s, '_audit', side_effect=interrupt), self.assertRaisesRegex(RuntimeError, 'Interrupted'):
            self.restart(saved)
        self.assertEqual(self.s.snapshot(sid)['constraints']['allowed_paths'], ['.'])
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        inspected = self.step(saved, event='inspect')
        self.assertEqual(inspected['recovery']['kind'], 'source_scope_correction')
        self.assertEqual(review_budget.usage(self.s, sid), before)
        restarted = self.restart(inspected)
        self.assertEqual(review_budget.usage(self.s, sid), before)
        records = [json.loads(raw) for raw, in self.s.db.execute(
            "SELECT payload_json FROM page_audit WHERE event='NATIVE_PERMISSION_GRANT'")]
        corrections = [record for record in records if record.get('operation') == 'read_scope_correction']
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]['carry_usage'], before['used'])
        with self.assertRaises(InvalidUpdate):
            self.restart(inspected)
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertEqual(self.step(restarted)['state'], 'WORKSPACE')

    def test_interrupted_before_epoch_reuses_intent_and_keeps_old_task_until_retry(self):
        saved = self.amend(self.saved_bad_scope())
        sid = self.meta['active']
        before = review_budget.usage(self.s, sid)
        contract = self.s.pinned(sid)
        with patch('vscode_bridge.save_contract', side_effect=RuntimeError('Interrupted before epoch')):
            with self.assertRaisesRegex(RuntimeError, 'Interrupted'):
                self.restart(saved)
        self.assertEqual(self.s.pinned(sid), contract)
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        inspected = self.step(saved, event='inspect')
        restarted = self.restart(inspected)
        self.assertEqual(review_budget.usage(self.s, sid), before)
        intents = [json.loads(raw) for raw, in self.s.db.execute(
            "SELECT payload_json FROM page_audit WHERE event='NATIVE_PERMISSION_INTENT'")]
        self.assertEqual(len([record for record in intents if record.get('operation') == 'read_scope_correction']), 1)
        self.assertEqual(self.step(restarted)['state'], 'WORKSPACE')

    def test_later_allocation_continues_old_prefix_instead_of_resetting_versions(self):
        saved = self.amend(self.saved_bad_scope())
        sid = self.meta['active']
        before = review_budget.usage(self.s, sid)
        restarted = self.restart(saved)
        active = self.step(restarted)
        amended = self.amend(active)
        after = review_budget.usage(self.s, sid)
        self.assertEqual(after['used'], before['used'])
        self.assertEqual(after['allocation_version'], 2)
        self.assertEqual(after['limits'], dict(before['limits'],
            tokens=before['limits']['tokens'] + 4096, source_bytes=before['limits']['source_bytes'] + 2048))
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM allocation_amendments').fetchone()[0], 2)
        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        self.assertEqual(review_budget.usage(self.s, sid), after)
        self.assertEqual(self.step(amended, event='inspect')['flow_id'], restarted['flow_id'])

    def test_explicit_narrow_user_target_or_unrelated_contract_change_has_no_correction(self):
        self.prompt = 'Review only app.py from this workspace.'
        saved = self.step()
        self.assertEqual(saved['state'], 'PAUSED')
        self.assertIsNone(saved['recovery'])
        self.step(saved, event='cancel')
        self.prompt = 'Review the workspace architecture.'
        saved = self.saved_bad_scope()
        sid = self.meta['active']
        from vscode_bridge import save_contract
        current = self.s.snapshot(sid)
        save_contract(self.s, sid, {'goal': 'A different user goal', 'constraints': current['constraints']}, self.ws)
        self.assertEqual(self.step(saved, event='inspect')['recovery']['kind'], 'binding_changed')

    def test_unavailable_historical_proposal_never_authorizes_scope_correction(self):
        from paging import PageFault
        saved = self.saved_bad_scope()
        before = review_budget.usage(self.s, self.meta['active'])
        with patch('native_access.handoff_source', side_effect=PageFault('Unsupported historical policy')):
            inspected = self.step(saved, event='inspect')
        self.assertIsNone(inspected['recovery'])
        self.assertEqual(review_budget.usage(self.s, self.meta['active']), before)

