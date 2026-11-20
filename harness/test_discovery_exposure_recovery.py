"""Synthetic body-exposure regression, not an LLM quality or latency claim.

The fixture requires actual caller/callee source bodies before it can report.
All ledger data is temporary; the 400167-unit prefix is governed synthetic
spending, not a copy or rewrite of the user's receipts.
"""
import copy
import json
import os
import sqlite3
import unittest
from unittest.mock import patch

from invocation_gateway import InvocationLedger
import orchestration as machine
import orchestration_flow as flow
import repository_tools as repo
import review_budget
import review_discovery as discovery
import test_discovery_source_recall as source_fixture
import test_discovery_working_set as paid_fixture


class ExposureRecoveryTests(unittest.TestCase):
    tearDown = source_fixture.SourceRecallTests.tearDown
    step = source_fixture.SourceRecallTests.step
    until = source_fixture.SourceRecallTests.until
    fifteen = source_fixture.SourceRecallTests.fifteen
    history = source_fixture.SourceRecallTests.history
    _pay_to = paid_fixture.DiscoveryWorkingSetTests._pay_to
    pay_to = paid_fixture.DiscoveryWorkingSetTests.pay_to

    def setUp(self):
        source_fixture.SourceRecallTests.setUp(self)
        defaults = patch.dict(review_budget.DEFAULTS, review_tokens=500000, review_calls=50)
        defaults.start(); self.addCleanup(defaults.stop)
        self.caller = 'bridge_worker.js'
        path = self.ws / self.caller
        path.write_text('function dispatch(request) {\n    return execute(request); // decisive caller body\n}\n' + path.read_text())
        # Keep the decisive callee text inside one short unit; long unrelated
        # source still drives eviction, without changing parent excerpt policy.
        callee = self.ws / self.source
        gate = '    gate = admit(request)  # requested authorization gate and receipt reservation\n'
        text = callee.read_text().replace(gate, '')
        callee.write_text(text.replace('def execute(request):\n', 'def execute(request):\n' + gate))
        self.actions[14] = {'kind': 'read', 'target': self.caller}
        old_notes = self.extra_output
        self.decisions = []
        def conditional(raw, packet):
            old_notes(raw, packet)
            if len(self.calls) < 15: return
            bodies = '\n'.join(entry['text'] for entry in packet['evidence'])
            exposed = 'decisive caller body' in bodies and 'gate = admit(request)' in bodies
            self.decisions.append({'exposed': exposed, 'packet': copy.deepcopy(packet)})
            raw.update(summary='Actual caller and authorization bodies are supplied.' if exposed else
                       'The note and index do not provide the missing authorization body.',
                       remaining=[] if exposed else ['Recall harness/capabilities.py admit() authorization gate.'],
                       tool={'kind': 'report' if exposed else 'read', 'target': '' if exposed else self.source,
                             'query': '', 'line': 1 if exposed else 164})
        self.extra_output = conditional

    def provider(self, stage):
        model = source_fixture.SourceRecallTests.provider(self, stage)
        if stage == 'synthesis':
            model.discovery_provider.context_metadata['thinking'] = {'values': [False, True], 'default': False}
        if stage != 'conversation':
            call = model.call
            def measured(*args):
                if 'report' in args[3].get('properties', {}):
                    bodies = '\n'.join(args[2].get('excerpts', {}).values())
                    self.assertIn('decisive caller body', bodies)
                    self.assertIn('gate = admit(request)', bodies)
                value, record = call(*args)
                record['response'] = {'done': True, 'prompt_eval_count': 400, 'eval_count': 80}
                return value, record
            model.call = measured
        return model

    def test_saved400167_reload_switch_rehydrates_bodies_then_reports_and_publishes_without_reread(self):
        captured = {}; advance = discovery.advance
        def watch(*args, **kwargs):
            if not captured:
                captured['packet'], captured['data'] = copy.deepcopy(args[2]), copy.deepcopy(args[3])
            return advance(*args, **kwargs)
        with patch.dict(os.environ, SUTRA_THINKING='adaptive'), \
             patch.object(discovery, 'SOURCE_RECALL_VERSION', 0), \
             patch.object(discovery, 'REASONING_VERSION', 0), \
             patch.object(discovery, 'advance', side_effect=watch):
            out = self.fifteen()
        sid = self.meta['active']; self.parent_packet = captured['packet']; self.parent_data = captured['data']
        self.pay_to(out, 400167)
        before = review_budget.usage(self.s, sid)
        self.assertEqual(before['used']['tokens'], 400167)
        self.assertEqual(before['limits']['tokens'] - before['used']['tokens'], 99833)
        self.assertEqual(before['limits']['calls'], 50)
        prefix = self.s.db.execute('SELECT id,record_json FROM invocation_receipts ORDER BY seq').fetchall()
        source_prefix = self.s.db.execute('SELECT id,record_json FROM repository_receipts ORDER BY rowid').fetchall()
        contract = self.s.pinned(sid)
        # Branch only the temporary fixture ledger to compare the old recipe
        # against exactly the same paid prefix and accepted source history.
        legacy = self.root / 'legacy.sqlite'
        with sqlite3.connect(legacy) as target: self.s.db.backup(target)
        saved_meta = copy.deepcopy(self.meta); saved_workspace = (self.root/'workspace.json').read_bytes()
        saved_models, saved_calls = list(self.models), copy.deepcopy(self.calls)
        self.s.close(); self.s = InvocationLedger(legacy)
        with patch.object(discovery, 'SOURCE_RECALL_VERSION', 0), patch.object(discovery, 'REASONING_VERSION', 0):
            old = self.step(out)
        self.assertEqual(old['state'], 'SYNTHESIS', old.get('stop'))
        self.assertFalse(self.decisions[-1]['exposed'])
        old_packet = self.decisions[-1]['packet']
        self.assertTrue(any(page['kind']=='source_page_index' and page['path']==self.source
                            for page in old_packet['discovery_memory_pages']))
        self.assertTrue(any(page['kind']=='working_note_unverified' and 'capabilities' in page['note']['summary']
                            for page in old_packet['discovery_memory_pages']))
        self.assertEqual(self.calls[-1][2]['tool']['kind'], 'read')
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM repository_receipts').fetchone()[0], len(source_prefix)+1)
        # Restore the untouched sibling ledger; this is not a financial reset.
        self.s.close(); self.s = InvocationLedger(self.root/'state.sqlite')
        self.meta = saved_meta; (self.root/'workspace.json').write_bytes(saved_workspace)
        self.models, self.calls = saved_models, saved_calls; self.tier = 'high'; self.decisions = []
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.inspect = lambda packet: discovery.check_request(self.s, sid, packet)
        with patch.dict(os.environ, SUTRA_THINKING='adaptive'), \
             patch.object(repo, 'execute', side_effect=AssertionError('Archived page-in must not reread a repository file')):
            accepted = self.step(out)
        self.assertEqual(accepted['state'], 'SYNTHESIS', accepted.get('stop'))
        self.assertTrue(self.decisions[-1]['exposed'])
        packet = self.decisions[-1]['packet']
        self.assertEqual(packet['discovery_source_recall_version'], 1)
        self.assertEqual(packet['discovery_reasoning_version'], 1)
        self.assertIs(packet['reasoning']['requested'], True)
        self.assertEqual(self.calls[-1][2]['tool']['kind'], 'report')
        after_report = review_budget.usage(self.s, sid)
        self.assertEqual(after_report['used']['tokens'], 400167 + 480)
        self.assertEqual(after_report['used']['source_bytes'], before['used']['source_bytes'])
        self.assertEqual(after_report['used']['operations'], before['used']['operations'])
        self.assertEqual(self.s.db.execute('SELECT id,record_json FROM repository_receipts ORDER BY rowid').fetchall(), source_prefix)
        self.s.close(); self.s = InvocationLedger(self.root/'state.sqlite'); self.tier = 'mid'
        with patch.dict(os.environ, SUTRA_THINKING='adaptive'):
            done = self.until('DONE', accepted)
        self.assertTrue(done['data']['result']['admission']['admitted'], done)
        self.assertIn('report', done['data']['result']['accepted_output'])
        self.assertEqual(len(self.decisions), 1)  # Saved report decision is not regenerated.
        self.assertEqual(self.s.pinned(sid), contract)
        self.assertEqual(self.s.db.execute('SELECT id,record_json FROM invocation_receipts ORDER BY seq').fetchall()[:len(prefix)], prefix)
        final = review_budget.usage(self.s, sid)
        self.assertGreater(final['used']['tokens'], 400647)
        self.assertLessEqual(final['used']['tokens'], 500000)
        self.assertEqual(final['limits'], before['limits'])
        self.assertTrue(self.s.verify_invocations(sid))


if __name__ == '__main__': unittest.main()
