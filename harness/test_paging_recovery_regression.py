"""Legacy child evidence survives paging recovery and parent wire projection.

These tests use deterministic injected providers. They establish source delivery,
lineage, retained findings and accounting behavior, not model understanding.
"""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from context_manager import ContextBudgetExceeded, canonical
from invocation_gateway import InvocationLedger
from model_packet import build
import chat_runtime
import orchestration as machine
import review_budget
import subagent_runtime as child
import test_orchestration as fixtures


class PagingRecoveryRegressionTests(unittest.TestCase):
    setUp = fixtures.OrchestrationTests.setUp
    tearDown = fixtures.OrchestrationTests.tearDown
    step = fixtures.OrchestrationTests.step
    until = fixtures.OrchestrationTests.until
    children = fixtures.OrchestrationTests.children
    provider = fixtures.OrchestrationTests.provider

    prior_finding = 'FIRST_SLICE_FINDING_5827: the supplied entry returns its partition value.'

    def assert_source_spans(self, packet):
        self.assertEqual(set(packet['excerpt_spans']), set(packet['excerpts']))
        for path, span in packet['excerpt_spans'].items():
            lines = (self.ws / path).read_text().splitlines(keepends=True)
            self.assertGreater(span['next_line'], span['start_line'])
            text = ''.join(lines[span['start_line'] - 1:span['next_line'] - 1])
            self.assertEqual(text, packet['excerpts'][path])
            self.assertEqual(span['sha256'], hashlib.sha256(text.encode()).hexdigest())

    def legacy_checkpoint(self):
        # The requested lower function is beyond the initial bounded excerpt.
        for index in range(4):
            (self.ws / f'part{index}.py').write_text(
                f'def part{index}():\n    return {index}\n\n'
                + '# Earlier source details remain outside this bounded excerpt.\n' * 95
                + f'\ndef rivet_detail_{index}():\n    return {724 + index}\n')
        self.prompt = 'Explain the project architecture and tools logic. ' + self.prompt
        out = self.children()
        branch = out['children'][0]['id']
        node = machine.node(self.s, branch)
        target = node['files'][-1]
        index = int(target.removeprefix('part').removesuffix('.py'))
        symbol = f'part{index}.rivet_detail_{index}'
        anchor = f'def rivet_detail_{index}():'
        original_call = fixtures.Child.call
        original_fit = child._fit_packet

        def legacy_fit(s, sid, packet, data, provider, slots):
            packet.pop('child_evidence_version', None)
            return original_fit(s, sid, packet, data, provider, slots)

        def continuing(worker, *args):
            result, record = original_call(worker, *args)
            result.update(answer=self.prior_finding, summary=self.prior_finding,
                          remaining=[f'Inspect {symbol} in this branch and explain its return value.'],
                          slice_status='continue')
            return result, record

        with patch.object(child, '_fit_packet', legacy_fit), \
                patch.object(fixtures.Child, 'call', continuing):
            saved = self.step(out)
        result = saved['child']['result']
        self.assertTrue(result['admission']['admitted'], result)
        first = self.models[-1][1].payloads[0]
        self.assertNotIn('child_evidence_version', first)
        self.assertNotIn(anchor, first['excerpts'].get(target, ''))
        self.assertEqual(machine.state(self.s, branch), 'READY')
        iid = result['invocation_id']
        raw = self.s.db.execute('SELECT record_json FROM invocation_receipts WHERE invocation_id=?',
                                (iid,)).fetchone()[0]
        return saved, branch, target, anchor, iid, raw

    def finish_and_check_parent(self, out):
        done = self.until('DONE', out)
        self.assertTrue(done['data']['result']['admission']['admitted'])
        parent = next(provider for stage, provider in self.models if stage == 'synthesis')
        payload = parent.payloads[0]
        before = copy.deepcopy(payload)
        packet = build(chat_runtime.SYSTEM, payload, chat_runtime.schema(payload))
        self.assertEqual(payload, before)
        body = json.loads(packet['messages'][1]['content'])
        self.assertIn(self.prior_finding, canonical(body['child_analysis_unverified']))
        self.assertFalse(payload['contract']['constraints']['edits_allowed'])
        self.assertFalse(payload['contract']['constraints']['network_allowed'])
        self.assertFalse(payload['contract']['constraints']['dependencies_allowed'])
        self.assertIn('CED724', canonical(payload['memory']))
        self.assertTrue(self.s.verify_invocations(self.meta['active']))
        self.assertTrue(self.s.audit_valid())

    def test_legacy_checkpoint_context_pause_reload_and_model_switch_preserve_findings(self):
        out, branch, target, anchor, iid, receipt = self.legacy_checkpoint()
        sid = self.meta['active']
        contract = self.s.pinned(sid)
        node = machine.node(self.s, branch)
        limits = machine.effective_limits(self.s, node)
        cursor = machine.slice_cursor(self.s, branch)
        history = machine.slice_history(self.s, branch, cursor)
        before = review_budget.usage(self.s, sid)
        calls = self.s.db.execute('SELECT COUNT(*) FROM invocations WHERE session=?', (sid,)).fetchone()[0]
        with patch.object(child, '_fit_packet', side_effect=ContextBudgetExceeded('Required context is temporarily unavailable')):
            paused = self.step(out)
        self.assertEqual(paused['state'], 'PAUSED')
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM invocations WHERE session=?', (sid,)).fetchone()[0], calls)
        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        self.tier = 'high'
        resumed = self.step(paused, event='resume')
        self.assertEqual(review_budget.usage(self.s, sid), before)
        reviewed = self.step(resumed)
        self.assertTrue(reviewed['child']['result']['admission']['admitted'])
        provider = self.models[-1][1]
        packet = provider.payloads[0]
        self.assertEqual(provider.model, fixtures.MODELS['high'])
        self.assertEqual(len(provider.payloads), 1)
        self.assertEqual(packet['child_evidence_version'], 3)
        self.assert_source_spans(packet)
        self.assertEqual(packet['child_slice'], cursor)
        self.assertEqual(packet['request'], node['context']['request'])
        self.assertIn(anchor, packet['excerpts'].get(target, ''))
        self.assertEqual(packet['child_checkpoint'][0]['invocation_id'], iid)
        self.assertIn(self.prior_finding, canonical(packet['child_checkpoint']))
        self.assertTrue(set(packet['source_paths']).issubset(node['files']))
        self.assertEqual(machine.node(self.s, branch), node)
        self.assertEqual(machine.effective_limits(self.s, node), limits)
        self.assertEqual(machine.slice_history(self.s, branch, cursor), history)
        self.assertEqual(self.s.pinned(sid), contract)
        self.assertEqual(self.s.db.execute('SELECT record_json FROM invocation_receipts WHERE invocation_id=?', (iid,)).fetchone()[0], receipt)
        charged = review_budget.usage(self.s, sid)
        self.assertEqual(charged['used']['calls'], before['used']['calls'] + 1)
        self.assertGreater(charged['used']['tokens'], before['used']['tokens'])
        self.assertEqual(charged['used']['operations'], before['used']['operations'])
        self.assertEqual(charged['used']['source_bytes'], before['used']['source_bytes'])
        self.assertEqual(machine.state(self.s, branch), 'DONE')
        self.finish_and_check_parent(reviewed)

    def test_replacement_after_rejection_reuses_legacy_next_check_and_preserves_spending(self):
        out, old, target, anchor, iid, receipt = self.legacy_checkpoint()
        sid = self.meta['active']
        old_node = machine.node(self.s, old)
        original = fixtures.Child.call

        def rejected(worker, *args):
            result, record = original(worker, *args)
            result['constraints'] = []
            return result, record

        with patch.object(fixtures.Child, 'call', rejected):
            stopped = self.step(out)
        self.assertEqual(stopped['stop']['status'], 'child_review_rejected')
        self.assertEqual(machine.state(self.s, old), 'QUARANTINED')
        old_spent = machine.spent(self.s, old)
        old_limits = machine.effective_limits(self.s, old_node)
        charged = review_budget.usage(self.s, sid)
        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        self.tier = 'high'
        resumed = self.step(stopped, event='resume')
        replacement = resumed['children'][0]['id']
        self.assertNotEqual(replacement, old)
        self.assertEqual(review_budget.usage(self.s, sid), charged)
        new = machine.node(self.s, replacement)
        for key in ('context', 'context_hash', 'binding', 'permissions', 'files'):
            self.assertEqual(new[key], old_node[key])
        self.assertEqual(new['limits'], {key: old_limits[key] - old_spent[key] for key in old_limits})
        reviewed = self.step(resumed)
        packet = self.models[-1][1].payloads[0]
        self.assertEqual(packet['child_evidence_version'], 3)
        self.assert_source_spans(packet)
        self.assertIn(anchor, packet['excerpts'].get(target, ''))
        self.assertEqual(packet['child_checkpoint'], [])
        self.assertEqual(packet['recovery_checkpoint'][0]['invocation_id'], iid)
        self.assertIn(self.prior_finding, canonical(packet['recovery_checkpoint']))
        self.assertEqual(machine.spent(self.s, old), old_spent)
        self.assertEqual(self.s.db.execute('SELECT record_json FROM invocation_receipts WHERE invocation_id=?', (iid,)).fetchone()[0], receipt)
        self.assertEqual(machine.state(self.s, replacement), 'DONE')
        self.finish_and_check_parent(reviewed)


if __name__ == '__main__':
    unittest.main()
