"""Focused tests for the offline replay; no model/runtime/user database access."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('discovery_replay',
    Path(__file__).with_name('evaluate-discovery-governor.py'))
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


def excerpt(text, line=1, column=0, version='a'*64, identity='generated-id'):
    return dict(path='entry.py', sha256=version, text=text, line=line, column=column, id=identity)


class CoverageTests(unittest.TestCase):
    def test_generated_ids_do_not_make_source_or_request_novel(self):
        coverage = replay.SpanCoverage()
        self.assertEqual(coverage.add(excerpt('hello\n', identity='first')), 6)
        self.assertEqual(coverage.add(excerpt('hello\n', identity='new')), 0)
        self.assertEqual(coverage.summary()['repeated_exact_spans'], 1)
        tool = dict(kind='read', target='entry.py', line=1, query='')
        self.assertEqual(replay.normalized_request(tool), replay.normalized_request(
            dict(tool, id='new', receipt='new', evidence_id='new')))

    def test_partial_unicode_overlap_is_measured_in_utf8_bytes(self):
        coverage = replay.SpanCoverage()
        self.assertEqual(coverage.add(excerpt('αβγ', column=2)), 6)
        self.assertEqual(coverage.add(excerpt('βγδ', column=3)), 2)
        self.assertEqual(coverage.summary()['unique_bytes'], 8)
        self.assertEqual(coverage.summary()['reread_bytes'], 4)

    def test_multiline_and_partial_line_replay(self):
        coverage = replay.SpanCoverage()
        self.assertEqual(coverage.add(excerpt('prefix\nnext\n')), 12)
        self.assertEqual(coverage.add(excerpt('fix\nnext\n', column=3)), 0)
        self.assertEqual(coverage.add(excerpt('xt\nlast', line=2, column=2)), 4)

    def test_new_source_version_counts_new_evidence(self):
        coverage = replay.SpanCoverage()
        self.assertEqual(coverage.add(excerpt('same')), 4)
        self.assertEqual(coverage.add(excerpt('same', version='b'*64)), 4)
        self.assertEqual(coverage.summary()['source_versions'], 2)

    def test_unlocated_search_never_invents_a_column_for_read_overlap(self):
        coverage = replay.SpanCoverage()
        self.assertEqual(coverage.add(excerpt('x'*10)), 10)
        self.assertEqual(coverage.add(excerpt('x'*10), located=False), 0)
        self.assertEqual(coverage.add(excerpt('x'*10, identity='new'), located=False), 0)
        summary = coverage.summary()
        self.assertEqual(summary['unique_bytes'], 10)
        self.assertEqual(summary['reread_bytes'], 0)
        self.assertEqual(summary['unlocated_delivered_bytes'], 20)
        self.assertEqual(summary['unlocated_unique_snippet_bytes'], 10)

    def test_malformed_source_refuses_partial_counts(self):
        with self.assertRaises(ValueError):
            replay.SpanCoverage().add(dict(path='entry.py', line=1, text='x'))

    def test_phase_crossing_is_retrospective_and_keeps_prior_spending(self):
        steps = [{'ordinal': i+1, 'settled_units': 20, 'new_source_bytes': 3} for i in range(5)]
        result = replay.phase_comparison(steps, 400, 50, [500, 1000])
        direct, original, expanded = result['comparisons'][:3]
        self.assertEqual(direct['first_crossing']['after_selector_attempt'], 3)
        self.assertEqual(original['first_crossing']['session_settled_units'], 460)
        self.assertIsNone(expanded['first_crossing'])
        self.assertEqual(result['comparisons'][4]['first_crossing']['after_selector_attempt'], 0)

    def test_component_benchmark_workloads_are_bounded_and_reproducible(self):
        first = replay.benchmark_workload(32, 'repeated')
        self.assertEqual(replay.canonical(first), replay.canonical(replay.benchmark_workload(32, 'repeated')))
        self.assertEqual(len(first['steps']), 32)
        self.assertEqual(len({s['record']['id'] for s in first['steps']}), 32)
        for invalid in (0, 129, True):
            with self.assertRaises(ValueError):
                replay.benchmark_workload(invalid, 'advancing')

    def test_timing_summary_reports_nearest_rank_p95(self):
        result = replay.timing_summary([i*10**6 for i in range(1, 21)])
        self.assertEqual(result['median_ms'], 10.5)
        self.assertEqual(result['p95_ms'], 19)

    def test_component_benchmark_calls_real_adapter_without_opening_a_ledger(self):
        harness = Path(__file__).resolve().parents[1]/'harness'
        sys.path.insert(0, str(harness))
        from discovery_governor import assess
        from karma_adapter import KarmaAdapter
        with patch.object(sqlite3, 'connect', side_effect=AssertionError('No benchmark ledger')):
            report = replay.benchmark(assess, KarmaAdapter(), repetitions=1, warmups=0, sizes=(8,))
        self.assertEqual(len(report['results']), 3)
        self.assertEqual([row['status'] for row in report['results']], ['green', 'partial_synthesis', 'yellow'])
        self.assertTrue(all(row['combined']['samples'] == 1 for row in report['results']))


class Fixture:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript('''
            CREATE TABLE invocations(id,session,delivery_id,record_json,digest);
            CREATE TABLE invocation_receipts(invocation_id,seq,id,record_json);
            CREATE TABLE deliveries(id,payload_json,payload_hash);
            CREATE TABLE repository_ops(id,session,record_json,digest);
            CREATE TABLE repository_receipts(operation_id,id,record_json,seq);
            CREATE TABLE orchestration_nodes(id,session,record_json,digest);
        ''')
        self.previous_invocation = self.previous_repository = 'GENESIS'

    def step(self, index, previous=None, status='accepted', text='x\n', generated_id=None, key='key'):
        iid, oid = 'inv-'+str(index), 'op-'+str(index)
        tool = dict(kind='read', target='entry.py', query='', line=1)
        payload = dict(discovery_key=key, discovery_previous=previous, discovery_owner='owner')
        phash = replay.digest(replay.canonical(payload))
        inv = dict(id=iid, session='test', model='fixture', created_at=index,
                   charge=10, budget_profile={'response_reserve': 4}, payload_hash=phash)
        raw = replay.canonical(inv)
        ihash = replay.digest('invocation-v1:'+raw)
        self.db.execute('INSERT INTO invocations VALUES(?,?,?,?,?)', (iid, 'test', iid, raw, ihash))
        self.db.execute('INSERT INTO deliveries VALUES(?,?,?)', (iid, replay.canonical(payload), phash))
        proposal = dict(mode='tool', tool=tool)
        rec = dict(session='test', seq=index, invocation_hash=ihash, previous_hash=self.previous_invocation,
                   payload_hash=phash,
                   response_hash=replay.digest(replay.canonical(proposal)), proposal=proposal, status=status,
                   elapsed_seconds=2, accounting_settlement={'used_tokens': 12})
        rraw = replay.canonical(rec)
        rhash = replay.digest('invocation-receipt-v1:'+rraw)
        self.db.execute('INSERT INTO invocation_receipts VALUES(?,?,?,?)', (iid, index, rhash, rraw))
        self.previous_invocation = rhash
        if status != 'accepted':
            return
        op = dict(id=oid, session='test', invocation_id=iid, kind='read', task_cost={'source_bytes': 20})
        raw = replay.canonical(op)
        ohash = replay.digest('repository-op-v1:'+raw)
        self.db.execute('INSERT INTO repository_ops VALUES(?,?,?,?)', (oid, 'test', raw, ohash))
        rec = dict(session='test', seq=index, previous_hash=self.previous_repository, operation_hash=ohash,
                   result={'status': 'ok', 'excerpts': [excerpt(text, identity=generated_id or iid)]})
        raw = replay.canonical(rec)
        rhash = replay.digest('repository-receipt-v1:'+raw)
        self.db.execute('INSERT INTO repository_receipts VALUES(?,?,?,?)', (oid, rhash, raw, index))
        self.previous_repository = rhash

    def close(self):
        self.db.commit()
        self.db.close()


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='sutra-replay-test-')
        self.path = Path(self.tmp.name)/'state.sqlite'

    def tearDown(self):
        self.tmp.cleanup()

    def fixture(self):
        f = Fixture(self.path)
        f.step(1)
        f.step(2, previous='inv-1', generated_id='different')
        owner = replay.canonical({'flow_id': 'flow'})
        f.db.execute('INSERT INTO orchestration_nodes VALUES(?,?,?,?)',
                     ('owner', 'test', owner, replay.digest('orchestration-node-v1:'+owner)))
        f.close()

    def test_saved_replay_preserves_bytes_and_charges_duplicate_reads(self):
        self.fixture()
        before = self.path.read_bytes()
        snapshot = replay.Snapshot(self.path)
        try:
            summary, steps = replay.replay(snapshot, 'test', flow='flow')
            with self.assertRaises(sqlite3.OperationalError):
                snapshot.db.execute('DELETE FROM repository_ops')
        finally:
            snapshot.close()
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(summary['session_accounting']['settled_units'], 24)
        self.assertEqual(summary['selected']['accepted_tool_steps'], 2)
        self.assertEqual(summary['selected']['repeated_requests_accepted_tools'], 1)
        self.assertEqual([s['new_source_bytes'] for s in steps], [2, 0])
        self.assertNotIn('"text":', json.dumps(steps))

    def test_corruption_fails_closed(self):
        self.fixture()
        db = sqlite3.connect(self.path)
        db.execute('UPDATE repository_receipts SET record_json=? WHERE seq=2', ('{}',))
        db.commit()
        db.close()
        snapshot = replay.Snapshot(self.path)
        try:
            with self.assertRaises((ValueError, KeyError)):
                replay.replay(snapshot, 'test')
        finally:
            snapshot.close()

    def test_delivery_scope_tampering_fails_its_invocation_binding(self):
        self.fixture()
        db = sqlite3.connect(self.path)
        db.execute('UPDATE deliveries SET payload_json=? WHERE id=?',
                   (replay.canonical({'discovery_key': 'foreign'}), 'inv-2'))
        db.commit()
        db.close()
        snapshot = replay.Snapshot(self.path)
        try:
            with self.assertRaisesRegex(ValueError, 'Delivery payload binding'):
                replay.replay(snapshot, 'test')
        finally:
            snapshot.close()

    def test_owner_scope_tampering_is_not_used_for_flow_filtering(self):
        self.fixture()
        db = sqlite3.connect(self.path)
        db.execute('UPDATE orchestration_nodes SET record_json=?',
                   (replay.canonical({'flow_id': 'foreign'}),))
        db.commit()
        db.close()
        snapshot = replay.Snapshot(self.path)
        try:
            with self.assertRaisesRegex(ValueError, 'owner binding'):
                replay.replay(snapshot, 'test', flow='foreign')
        finally:
            snapshot.close()

    def test_pending_effect_is_not_counted_as_a_receipt(self):
        self.fixture()
        db = sqlite3.connect(self.path)
        db.execute('DELETE FROM repository_receipts WHERE operation_id=?', ('op-2',))
        db.commit()
        db.close()
        snapshot = replay.Snapshot(self.path)
        try:
            summary, steps = replay.replay(snapshot, 'test')
        finally:
            snapshot.close()
        self.assertEqual(summary['selected_repository_accounting']['pending_operations'], 1)
        self.assertEqual(summary['selected']['accepted_tool_steps_with_receipt'], 1)
        self.assertEqual(steps[1]['repository_operations'], 1)
        self.assertEqual(steps[1]['repository_receipts'], 0)

    def test_wrong_flow_does_not_include_other_task(self):
        self.fixture()
        snapshot = replay.Snapshot(self.path)
        try:
            summary, steps = replay.replay(snapshot, 'test', flow='other')
        finally:
            snapshot.close()
        self.assertEqual(steps, [])
        self.assertEqual(summary['selected_accounting']['settled_units'], 0)
        self.assertEqual(summary['session_accounting']['settled_units'], 24)

    def test_governor_counterfactual_does_not_change_baseline_spending(self):
        f = Fixture(self.path)
        for index in range(1, 6):
            f.step(index, previous='inv-'+str(index-1) if index > 1 else None)
        f.close()
        source = Path(__file__).resolve().parents[1]/'harness/discovery_governor.py'
        spec = importlib.util.spec_from_file_location('fixture_governor', source)
        governor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(governor)
        snapshot = replay.Snapshot(self.path)
        try:
            summary, _ = replay.replay(snapshot, 'test', assess=governor.assess)
        finally:
            snapshot.close()
        comparison = summary['governor_comparisons'][0]
        self.assertEqual(comparison['first_yellow']['accepted_repository_steps'], 3)
        self.assertEqual(comparison['first_partial_synthesis']['accepted_repository_steps'], 5)
        self.assertEqual(summary['selected_accounting']['settled_units'], 60)
        self.assertEqual(summary['session_repository_accounting']['recorded_source_byte_charges'], 100)

    def test_distinct_discovery_keys_have_separate_governor_histories(self):
        f = Fixture(self.path)
        f.step(1, key='first')
        f.step(2, key='second')
        f.close()
        source = Path(__file__).resolve().parents[1]/'harness/discovery_governor.py'
        spec = importlib.util.spec_from_file_location('fixture_governor', source)
        governor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(governor)
        snapshot = replay.Snapshot(self.path)
        try:
            summary, _ = replay.replay(snapshot, 'test', assess=governor.assess)
            with self.assertRaisesRegex(ValueError, 'exactly one discovery key'):
                replay.replay(snapshot, 'test', synthesis_pair_units=10)
        finally:
            snapshot.close()
        comparisons = summary['governor_comparisons']
        self.assertEqual({row['discovery_key'] for row in comparisons}, {'first', 'second'})
        self.assertEqual([row['final_counts']['repository_operations'] for row in comparisons], [1, 1])


if __name__ == '__main__':
    unittest.main()
