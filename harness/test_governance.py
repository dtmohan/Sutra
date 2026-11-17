import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from budget_preflight import host_seed
from context_manager import Conflict, InvalidUpdate, canonical
from dag_runtime import run_paged
from governance import GovernedContext, POLICY_FILES, policy_hash
from paging import Budget, PageFault
from validate_dag import build_new, new_definitions


class ScriptModel:
    budget = Budget()
    model = 'deterministic-test-adapter'
    def __init__(self, script): self.script = script; self.calls = []
    def call(self, call_id, system, payload, schema):
        self.calls.append(copy.deepcopy(payload))
        answer = self.script(len(self.calls), payload)
        return answer, {'call_id': call_id, 'answer': copy.deepcopy(answer)}


def proposal(decision='ALLOW'):
    return {'action': 'run_tests', 'target': 'tests/test_policy.py', 'complete': False,
            'facts': [{'key': 'policy_marker', 'value': 'R-12', 'page_id': 'policy-entry'}],
            'unavailable_evidence': [], 'interpretation': decision,
            'interpretation_sources': ['rule-0', 'rule-1', 'policy-entry']}


class GovernanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        definition = next(d for d in new_definitions() if d['id'] == 'N06-permitted-branch')
        with patch('experiment.ingest', host_seed):
            self.case = build_new(definition, self.root / 'case', None)
        self.db = self.case['db']; self.session = self.case['session']

    def tearDown(self): self.tmp.cleanup()

    def run_model(self, model=None, **kwargs):
        c = self.case
        return run_paged(self.db, self.session, c['request'], c['required_facts'],
            model or ScriptModel(lambda n, p: proposal()), 'test', initial_resident=c['seed_pages'],
            policy_request=c['policy_request'], **kwargs)

    def store(self): return GovernedContext(self.db)

    def test_contract_epoch_survives_reopen_and_ignores_tool_observations(self):
        s = self.store(); one = s.current_contract(self.session)
        s.observe_file(self.session, 'new-observation', 'src/policy.py', self.root/'case/implementation.py', s.snapshot(self.session)['revision'])
        self.assertEqual(s.current_contract(self.session), one); s.close()
        s = self.store(); self.assertEqual(s.current_contract(self.session), one); s.close()

    def test_authorized_change_creates_linked_epoch_and_blocks_old_checkpoint(self):
        result = self.run_model(); s = self.store(); one = s.current_contract(self.session)
        text = 'Limit attempts to 4.'
        s.apply_user_patch(self.session, 'amendment', text, [{'key': 'max_attempts', 'value': '4', 'quote': text}], s.snapshot(self.session)['revision'])
        two = s.current_contract(self.session)
        self.assertEqual(two['epoch'], one['epoch'] + 1)
        self.assertEqual(two['previous_hash'], one['contract_hash'])
        self.assertNotEqual(two['origin_hash'], one['origin_hash'])
        with self.assertRaises(PageFault): s.load_checkpoint(self.session, result['checkpoint_id'])
        s.close()

    def test_response_after_contract_change_never_released(self):
        def change(n, payload):
            s = self.store(); text = 'Limit attempts to 4.'
            s.apply_user_patch(self.session, 'change', text, [{'key':'max_attempts','value':'4','quote':text}], s.snapshot(self.session)['revision']); s.close()
            return proposal()
        result = self.run_model(ScriptModel(change))
        self.assertIsNone(result['released_answer'])
        self.assertEqual(result['protocol']['state'], 'FIN')
        self.assertEqual(len(result['attempts']), 1)

    def test_assertion_receipts_bind_to_actual_delivery_and_response(self):
        result = self.run_model(); s = self.store()
        self.assertEqual(len(result['receipt_ids']), 3)
        self.assertTrue(s.verify_protocol(self.session))
        bodies = [json.loads(row[0]) for row in s.db.execute('SELECT record_json FROM assertion_receipts ORDER BY seq')]
        self.assertEqual({b['kind'] for b in bodies}, {'fact', 'interpretation', 'operation'})
        self.assertTrue(all(b['contract_hash'] == result['protocol']['contract_hash'] for b in bodies))
        self.assertTrue(all(b['status'] == 'accepted' for b in bodies))
        self.assertEqual(bodies[1]['semantic_check'], 'verified')
        s.close()

    def test_receipts_and_epochs_cannot_be_updated(self):
        self.run_model(); s = self.store()
        for table in ('assertion_receipts','contract_epochs','checkpoints','protocol_events'):
            with self.subTest(table=table), self.assertRaises(sqlite3.IntegrityError):
                s.db.execute('DELETE FROM ' + table)
            s.db.rollback()
        s.close()

    def test_protocol_event_mirror_detects_rewrite_omission_and_insertion(self):
        s = self.store()
        self.addCleanup(s.close)
        run = s.begin_run(self.session, 'audit probe', [], None)
        s.event(run, 'NACK', {'reason': 'original'})
        s.event(run, 'FIN', {'reason': 'settled'})
        original = s.run(run)['events'][1]
        self.assertIsInstance(original['at'], float)
        self.assertTrue(s.verify_protocol(self.session))

        changed_kind = canonical(dict(original, kind='BRIDGE'))
        changed_data = canonical(dict(original, data={'reason': 'rewritten'}))
        changed_time = canonical(dict(original, at=original['at']+1))
        for trigger, sql, params in (
                ('UPDATE', 'UPDATE protocol_events SET record_json=? WHERE run_id=? AND seq=2', (changed_kind, run)),
                ('UPDATE', 'UPDATE protocol_events SET record_json=? WHERE run_id=? AND seq=2', (changed_data, run)),
                ('UPDATE', 'UPDATE protocol_events SET record_json=? WHERE run_id=? AND seq=2', (changed_time, run)),
                ('DELETE', 'DELETE FROM protocol_events WHERE run_id=? AND seq=3', (run,)),
                (None, 'INSERT INTO protocol_events VALUES(?,?,?)', (run, 0, canonical(original)))):
            with self.subTest(sql=sql, params=params):
                s.db.execute('BEGIN IMMEDIATE')
                try:
                    if trigger:
                        s.db.execute('DROP TRIGGER immutable_protocol_events_' + trigger)
                    s.db.execute(sql, params)
                    self.assertTrue(s.audit_valid())
                    self.assertFalse(s.verify_protocol(self.session))
                finally:
                    s.db.rollback()
        self.assertTrue(s.verify_protocol(self.session))

    def test_protocol_event_mirror_accepts_historical_events_without_timestamps(self):
        s = self.store()

        def legacy_event(run_id, kind, data):
            seq = s.db.execute('SELECT COUNT(*) FROM protocol_events WHERE run_id=?', (run_id,)).fetchone()[0] + 1
            event = {'kind': kind, 'data': data}
            s.db.execute('INSERT INTO protocol_events VALUES(?,?,?)', (run_id, seq, canonical(event)))
            s._audit('PROTOCOL_EVENT', {'run_id': run_id, 'seq': seq, **event})

        try:
            with patch.object(s, '_event', legacy_event):
                run = s.begin_run(self.session, 'old audit probe', [], None)
                s.event(run, 'NACK', {'reason': 'original'})
        finally:
            s.close()
        s = self.store()
        self.addCleanup(s.close)
        self.assertTrue(s.verify_protocol(self.session))
        s.event(run, 'FIN', {'reason': 'settled'})
        self.assertTrue(s.verify_protocol(self.session))
        self.assertNotIn('at', s.run(run)['events'][0])
        self.assertIn('at', s.run(run)['events'][-1])

    def test_protocol_verification_distinguishes_unknown_and_empty_sessions(self):
        s = self.store()
        self.addCleanup(s.close)
        run = s.begin_run(self.session, 'audit probe', [], None)
        self.assertFalse(s.verify_protocol(run))
        s.create('empty-session')
        self.assertTrue(s.verify_protocol('empty-session'))

    def test_bounded_repair_uses_fresh_sources_without_replaying_bad_answer(self):
        model = ScriptModel(lambda n, p: proposal('DENY' if n == 1 else 'ALLOW'))
        result = self.run_model(model)
        self.assertEqual(result['protocol']['state'], 'ALLOW')
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(len(result['attempts']), 2)
        self.assertIn('repair', model.calls[1])
        self.assertNotIn('computed_decision', json.dumps(model.calls[1]['repair']))
        self.assertNotIn('DENY', json.dumps(model.calls[1]['repair']))
        s = self.store()
        self.assertEqual(s.checkpoint_status(self.session, result['attempts'][0]['checkpoint_id']), 'quarantined')
        self.assertEqual(s.checkpoint_status(self.session, result['checkpoint_id']), 'available')
        receipts = [json.loads(row[0]) for row in s.db.execute('SELECT record_json FROM assertion_receipts ORDER BY seq')]
        self.assertEqual(receipts[0]['claim_id'], receipts[3]['claim_id'])
        s.close()

    def test_repeated_bad_decision_stops_at_two_repairs(self):
        model = ScriptModel(lambda n, p: proposal('DENY'))
        result = self.run_model(model)
        self.assertEqual(len(model.calls), 3)
        self.assertEqual(result['protocol']['reason'], 'repair_exhausted')
        self.assertIsNone(result['released_answer'])
        self.assertTrue(all(not a['admission']['admitted'] for a in result['attempts']))

    def test_zero_repair_and_call_budget_are_enforced(self):
        for options in ({'max_repairs': 0}, {'max_calls': 1}):
            with self.subTest(options=options):
                model = ScriptModel(lambda n, p: proposal('DENY'))
                result = self.run_model(model, **options)
                self.assertEqual(len(model.calls), 1)
                self.assertEqual(result['protocol']['state'], 'FIN')
                self.assertIsNone(result['released_answer'])

    def test_dispatch_budget_survives_process_reopen(self):
        s = self.store(); run = s.begin_run(self.session, 'task', [], None, max_calls=1)
        s.reserve(run, 'DISPATCH', {'call_id': 'one'}); s.close()
        s = self.store()
        with self.assertRaises(InvalidUpdate): s.reserve(run, 'DISPATCH', {'call_id':'two'})
        s.close()

    def test_terminal_run_cannot_reopen_or_change_budget(self):
        result = self.run_model(); s = self.store(); run = result['protocol']['run_id']
        with self.assertRaises(InvalidUpdate): s.reserve(run, 'DISPATCH', {'call_id':'again'})
        with self.assertRaises(InvalidUpdate): s.event(run, 'BRIDGE', {})
        with self.assertRaises(sqlite3.IntegrityError):
            s.begin_run(self.session, 'replacement', [], None, run_id=run)
        s.close()

    def test_missing_rules_fin_without_repair_loop(self):
        result = self.run_model(ScriptModel(lambda n, p: proposal('DENY')), expand=False)
        self.assertEqual(result['protocol']['state'], 'FIN')
        self.assertEqual(len(result['attempts']), 1)
        self.assertTrue(result['protocol']['missing_inputs'])

    def test_quarantined_body_is_not_readable_or_in_catalog_after_reopen(self):
        result = self.run_model(ScriptModel(lambda n,p: proposal('DENY')), max_repairs=0)
        key = 'checkpoint-' + result['checkpoint_id']; s = self.store()
        with self.assertRaises(PageFault): s.read_page(self.session, key)
        self.assertNotIn(key, json.dumps(s.catalog(self.session, 'checkpoints')))
        self.assertEqual(s.page(self.session, key).authority, 'model')  # forensic data retained
        self.assertEqual(s.read_page(self.session, 'rule-0').authority, 'user')  # source retained
        s.close()

    def test_parent_quarantine_invalidates_descendant_checkpoint(self):
        parent = self.run_model()['checkpoint_id']
        child = self.run_model(checkpoint_ids=[parent])['checkpoint_id']
        s = self.store(); self.assertEqual(s.checkpoint_status(self.session, child), 'available')
        s.quarantine(self.session, parent, 'later evidence invalidated the parent')
        self.assertEqual(s.checkpoint_status(self.session, child), 'quarantined_ancestor')
        with self.assertRaises(PageFault): s.read_page(self.session, 'checkpoint-' + child)
        s.close()

    def test_bad_checkpoint_cannot_start_fresh_handoff(self):
        result = self.run_model(ScriptModel(lambda n,p: proposal('DENY')), max_repairs=0)
        model = ScriptModel(lambda n,p: proposal())
        with self.assertRaises(PageFault): self.run_model(model, checkpoint_ids=[result['checkpoint_id']])
        self.assertEqual(model.calls, [])

    def test_checkpoint_cannot_cross_task_sessions(self):
        result = self.run_model(); s = self.store(); s.create('other')
        with self.assertRaises(PageFault): s.load_checkpoint('other', result['checkpoint_id'])
        s.close()

    def test_source_revision_quarantines_checkpoint_without_deleting_sources(self):
        result = self.run_model(); s = self.store(); doc = s.document(self.session, 'policy-source')
        s.put_document(self.session, 'policy-source', doc['text']+'\nUpdated', source={'fixture':'revision'}, expected_version=1)
        self.assertTrue(s.checkpoint_status(self.session, result['checkpoint_id']).startswith('quarantined'))
        self.assertEqual(s.document(self.session, 'policy-source', 1)['text'], doc['text'])
        s.close()

    def test_resource_wait_does_not_taint_valid_checkpoints(self):
        good = self.run_model()['checkpoint_id']
        def unavailable(n, p): raise RuntimeError('critical thermal state')
        result = self.run_model(ScriptModel(unavailable))
        self.assertEqual(result['protocol']['state'], 'RESOURCE_WAIT')
        s = self.store(); self.assertEqual(s.checkpoint_status(self.session, good), 'available'); s.close()

    def test_logic_version_change_invalidates_checkpoint(self):
        result = self.run_model(); s = self.store(); s.logic_hash = 'different-validator'
        self.assertEqual(s.checkpoint_status(self.session, result['checkpoint_id']), 'quarantined_old_validator')
        s.close()

    def test_concurrent_producers_preserve_unique_receipt_sequence(self):
        s=self.store();s.current_contract(self.session);s.close()
        barrier=Barrier(4)
        def produce(n):
            def response(i,payload):
                if i==1:barrier.wait(timeout=10)
                return proposal()
            return self.run_model(ScriptModel(response))
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(produce,range(4)))
        self.assertTrue(all(r['protocol']['state']=='ALLOW' for r in results))
        s=self.store();self.assertTrue(s.verify_protocol(self.session))
        self.assertEqual(s.db.execute('SELECT COUNT(*) FROM assertion_receipts').fetchone()[0],12)
        s.close()

    def test_validator_hash_covers_transitive_policy_modules(self):
        for name in POLICY_FILES:
            (self.root/name).parent.mkdir(parents=True,exist_ok=True)
            (self.root/name).write_text('initial '+name)
        with patch('governance.__file__',str(self.root/'governance.py')):
            original=policy_hash()
            for name in ('protocol.py','dag_protocol.py','kernel.py'):
                path=self.root/name;old=path.read_text();path.write_text(old+' changed')
                self.assertNotEqual(policy_hash(),original);path.write_text(old)


if __name__ == '__main__': unittest.main()
