"""Real stage projections stay scoped, bounded and inert across receipt reads."""
import copy
import json
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

import coherence_observations as obs
from context_manager import canonical, sha


class NoLedgerAccess:
    def __getattr__(self, key):
        raise AssertionError('Capture must not access ledger attribute ' + key)


def envelope(role='architect', iid='a' * 32, session='task-one'):
    return {'role': role, 'id': iid, 'session': session,
            'contract_hash': 'b' * 64, 'delivery_id': 'd' * 32}


def payload(**extra):
    return {'contract': {'task_id': 'review-one', 'goal': 'Explain the requested mechanism.'},
            'request': 'Explain the requested mechanism.', 'context_binding': 'c' * 64,
            'conversation_axis': 'native-chat-one', 'pages': [], **extra}


def commitment():
    return {'kind': 'commitment', 'status': 'recorded', 'authority': 'model_unverified',
            'value': {'understanding': 'Explain and provide a diagram.',
                      'criteria': [{'id': 'c1', 'deliverable': 'Explain', 'done_when': 'Cite source.'},
                                   {'id': 'c2', 'deliverable': 'Diagram', 'done_when': 'Show the flow.'}],
                      'assumptions': ['The selected artifact is the intended one.']}}


class ProjectionTests(unittest.TestCase):
    def capture(self, role, p, proposal, observation=None, env=None):
        before = copy.deepcopy((p, proposal, observation))
        result = obs.capture(NoLedgerAccess(), env or envelope(role), p, proposal, observation)
        self.assertEqual((p, proposal, observation), before)
        if result is not None:
            self.assertLessEqual(len(canonical(result).encode()), obs.MAX_CAPTURE_BYTES)
            self.assertEqual(result['mode'], 'observe_only')
        return result

    def test_greetings_and_unrelated_roles_have_no_task_ceremony(self):
        self.assertIsNone(self.capture('conversation', payload(), {'intent': 'conversation', 'answer': 'Hello!'}))
        self.assertIsNone(self.capture('api_mapper', payload(), {'symbols': []}))

    def test_document_quote_is_provenance_and_extracted_fact_remains_unverified(self):
        p = payload(section_text='Under scenario A, revenue was 10. Under scenario B it was 20.', section={'id': 'D1'})
        out = self.capture('document_extract', p, {'notes': [{'fact': 'A had revenue of 10.',
                                                           'quote': 'Under scenario A, revenue was 10.'}]})
        self.assertEqual(out['status'], 'partial')
        self.assertEqual(out['coverage']['source_linked_claims'], 1)
        self.assertEqual(out['signals']['unsupported_claims'], 0)
        self.assertEqual(out['analysis_limits']['source_entailment'], 'unverified')
        self.assertFalse(out['analysis_limits']['semantic_correctness_verified'])
        self.assertNotIn('frame', out)
        self.assertNotIn('sources', out)

    def test_absent_document_quote_is_a_structural_issue_without_exception(self):
        out = self.capture('document_extract', payload(section_text='The source says ten.', section={'id': 'D1'}),
                           {'notes': [{'fact': 'It says twenty.', 'quote': 'It says twenty.'}]})
        self.assertFalse(out['signals']['references_valid'])
        self.assertIn('quotation_absent', {x['code'] for x in out['issues']})
        self.assertGreater(out['signals']['missing_references'], 0)

    def test_model_page_cannot_become_independent_source_evidence(self):
        p = payload(pages=[{'id': 'earlier-note', 'text': 'A prior model guessed this.',
                            'authority': 'model', 'version': 1}])
        out = self.capture('architect', p, {'summary': 'An idea.', 'remaining': [],
            'findings': [{'page_id': 'earlier-note', 'quote': 'A prior model guessed this.', 'note': 'Claimed fact.'}]})
        self.assertGreater(out['signals']['unsupported_claims'], 0)
        self.assertEqual(out['coverage']['source_linked_claims'], 0)

    def test_explicit_reviewer_conflict_is_an_indicator_and_not_semantic_proof(self):
        p = payload(claims={'c1': {'text': 'It always succeeds.', 'paths': ['a.py']}},
                    source_evidence={'e1': {'text': 'raise ValueError("failed")', 'path': 'a.py'}})
        out = self.capture('report_reviewer', p, {'claims': {'c1': {
            'verdict': 'contradicted', 'reason': 'A failure branch exists.', 'evidence_ids': ['e1']}}})
        self.assertEqual(out['signals']['conflict_indicators'], 1)
        self.assertEqual(out['model_verdicts_unverified']['contradicted'], 1)
        issue = next(i for i in out['issues'] if i['code'] == 'contradiction_indicator')
        self.assertEqual(issue['kind'], 'semantic_indicator')
        self.assertFalse(out['analysis_limits']['semantic_correctness_verified'])

    def test_unknown_review_reference_is_reported_with_no_new_source_authority(self):
        p = payload(claims={'c1': {'text': 'A claim.', 'paths': ['a.py']}}, source_evidence={})
        out = self.capture('report_reviewer', p, {'claims': {'c1': {
            'verdict': 'supported', 'reason': 'Trust me.', 'evidence_ids': ['foreign-evidence']}}})
        self.assertFalse(out['signals']['references_valid'])
        self.assertGreater(out['signals']['missing_references'], 0)

    def test_completion_conflict_and_unmet_criterion_are_model_indicators_only(self):
        p = payload(conversation_handoff={'task_commitment': commitment()})
        assessment = {'kind': 'assessment', 'status': 'recorded', 'value': {
            'judgement': 'complete', 'delivered': 'A partial explanation.', 'remaining': ['Diagram.'],
            'items': [{'criterion_id': 'c1', 'status': 'met', 'evidence': 'Claimed explanation.'},
                      {'criterion_id': 'c2', 'status': 'unmet', 'evidence': 'No diagram.'}]}}
        out = self.capture('chat_reviewer', p, {'answer': 'Here is the explanation.', 'remaining': ['Diagram.']}, assessment)
        self.assertEqual(out['coverage']['required_questions'], 0)
        self.assertEqual(out['coverage']['host_closed_questions'], 0)
        self.assertEqual(out['coverage']['answered_claimed_unverified'], 0)
        self.assertEqual(out['coverage']['model_claimed_met_criteria'], 1)
        self.assertEqual(out['signals']['open_obligations'], 0)
        self.assertEqual(out['coverage']['model_reported_unresolved_criteria'], 1)
        self.assertTrue({'completion_conflict', 'unresolved_commitment'} <= {i['code'] for i in out['issues']})

    def test_unparsed_narratives_and_missing_optional_assessment_do_not_warn(self):
        out = self.capture('chat_reviewer', payload(), {'answer': 'A coherent-looking answer.', 'remaining': []})
        self.assertEqual(out['status'], 'partial')
        self.assertEqual(out['issues'], [])
        self.assertEqual(out['signals']['unsupported_claims'], 0)
        self.assertIn('free_form_narrative', out['analysis_limits']['unparsed_fields'])

    def test_distinct_native_chat_artifacts_and_models_never_import_global_graphs(self):
        proposal = {'answer': 'Here is the document review.', 'remaining': []}
        p = payload(document_evidence={'key': 'document-A', 'version': 1, 'digest': 'a' * 64, 'text': 'A'})
        out = self.capture('conversation', p, dict(proposal, intent='document'))
        other = self.capture('conversation', dict(p, conversation_axis='native-chat-two'), dict(proposal, intent='document'))
        changed = self.capture('conversation', dict(p, document_evidence={'key': 'document-B', 'version': 1,
                              'digest': 'b' * 64, 'text': 'B'}), dict(proposal, intent='document'))
        self.assertNotEqual(out['binding']['chat'], other['binding']['chat'])
        self.assertNotEqual(out['active_context'], other['active_context'])
        self.assertNotEqual(out['active_context'], changed['active_context'])
        different_model = dict(envelope('conversation'), model='another-model')
        switched = self.capture('conversation', p, dict(proposal, intent='document'), env=different_model)
        self.assertEqual(out['frame_hash'], switched['frame_hash'])

    def test_stale_source_versions_are_preserved_not_normalized_current(self):
        p = payload(pages=[{'id': 'p1', 'text': 'A recorded fact.', 'authority': 'tool',
                            'version': 1, 'current_version': 2}])
        out = self.capture('architect', p, {'findings': [{'page_id': 'p1', 'quote': 'A recorded fact.', 'note': 'Fact.'}]})
        self.assertFalse(out['signals']['sources_current'])
        self.assertGreater(out['signals']['stale_sources'], 0)

    def page(self, name, value, **fields):
        return {'id': name, 'text': 'Host-declared structured facts accompany this page.',
                'facts': {'rate': value}, 'authority': 'tool', 'version': 1, 'status': 'current', **fields}

    def test_explicit_typed_page_fact_differences_are_indicators_not_truth(self):
        for left, right in ((True, False), (10, 11), ('10', '11')):
            with self.subTest(values=(left, right)):
                p = payload(pages=[self.page('p1', left), self.page('p2', right)])
                out = self.capture('chat_reviewer', p, {'answer': 'A comparison.', 'remaining': []})
                self.assertEqual(out['signals']['conflict_indicators'], 1)
                self.assertEqual(out['coverage']['source_linked_claims'], 2)
                self.assertIn('value_conflict_indicator', {i['code'] for i in out['issues']})
                self.assertFalse(out['analysis_limits']['semantic_correctness_verified'])
                again = self.capture('chat_reviewer', p, {'answer': 'A comparison.', 'remaining': []},
                    env=dict(envelope('chat_reviewer'), session='another-task', contract_hash='e' * 64))
                self.assertNotEqual(out['binding'], again['binding'])
                self.assertNotEqual(out['frame_hash'], again['frame_hash'])

    def test_explicit_subject_time_and_hypothetical_contexts_are_separate(self):
        scenarios = [
            ({'subject': 'A', 'time': '2025'}, {'subject': 'A', 'time': '2026'}),
            ({'subject': 'A', 'time': '2025'}, {'subject': 'B', 'time': '2025'}),
            ({'mode': 'factual'}, {'mode': 'hypothetical'}),
        ]
        for first, second in scenarios:
            with self.subTest(contexts=(first, second)):
                p = payload(pages=[self.page('p1', 10, context=first), self.page('p2', 20, context=second)])
                out = self.capture('chat_reviewer', p, {'answer': 'Scenario comparison.', 'remaining': []})
                self.assertEqual(out['signals']['conflict_indicators'], 0)
                self.assertEqual(out['coverage']['analyzed_items'], 2)
                self.assertEqual(out['signals']['cross_context_references'], 0)
        p = payload(pages=[self.page('p1', 10, context={'subject': 'A', 'time': '2025'}),
                           self.page('p2', 20, context={'subject': 'A', 'time': '2025'})])
        out = self.capture('chat_reviewer', p, {'answer': 'Same scope.', 'remaining': []})
        self.assertEqual(out['signals']['conflict_indicators'], 1)

    def test_stale_page_facts_are_not_compared_as_current_assertions(self):
        p = payload(pages=[self.page('old', 10, version=1, current_version=2),
                           self.page('new', 20, version=2, current_version=2)])
        out = self.capture('chat_reviewer', p, {'answer': 'A history.', 'remaining': []})
        self.assertEqual(out['signals']['conflict_indicators'], 0)
        self.assertGreater(out['signals']['stale_sources'], 0)
        self.assertEqual(out['coverage']['stale_fact_assertions_not_compared'], 1)

    def test_repeating_model_page_facts_never_creates_source_authority(self):
        p = payload(pages=[self.page('guess', True, authority='model', status='unverified')])
        out = self.capture('chat_reviewer', p, {'answer': 'A model guess.', 'remaining': []})
        self.assertEqual(out['coverage']['source_linked_claims'], 0)
        self.assertEqual(out['signals']['unsupported_claims'], 1)
        self.assertEqual(out['coverage']['host_closed_questions'], 0)

    def test_delivered_source_alias_collision_cannot_silently_replace_evidence(self):
        p = payload(excerpts={'x': 'original text'}, evidence=[{'id': 'x', 'text': 'replacement text'}])
        out = self.capture('architect', p, {'findings': [{'page_id': 'x', 'quote': 'replacement text', 'note': 'Fact.'}]})
        self.assertFalse(out['signals']['references_valid'])
        self.assertIn('ambiguous_source_reference', {x['code'] for x in out['issues']})

    def test_bounded_omission_and_malformed_optional_data_never_raise(self):
        claims = {'c' + str(i): {'text': 'A claim.', 'paths': []} for i in range(200)}
        judged = {key: {'verdict': 'insufficient_evidence', 'reason': 'Unknown.', 'evidence_ids': []} for key in claims}
        out = self.capture('report_reviewer', payload(claims=claims), {'claims': judged})
        self.assertEqual(out['status'], 'partial')
        self.assertFalse(out['signals']['within_limits'])
        self.assertEqual(len(out['issues']), obs.MAX_ISSUES)
        self.assertGreater(out['omitted_issues'], 0)
        malformed = self.capture('architect', payload(), {'remaining': {'bad': 'shape'}})
        self.assertEqual(malformed['status'], 'malformed')
        self.assertEqual(malformed['issues'], [])

    def test_quote_outside_bounded_scan_is_unassessed_not_falsely_absent(self):
        p = payload(section_text='x' * 70000 + ' quote at the tail', section={'id': 'D1'})
        out = self.capture('document_extract', p, {'notes': [{'fact': 'A tail quote.', 'quote': 'quote at the tail'}]})
        self.assertFalse(out['signals']['within_limits'])
        self.assertGreater(out['projection_omitted']['sources'], 0)
        self.assertNotIn('quotation_absent', {i['code'] for i in out['issues']})

    def test_invalid_host_identity_and_deep_model_shape_remain_bounded_diagnostics(self):
        bad = dict(envelope(), id=None, session={})
        out = self.capture('architect', payload(), {'summary': 'An answer.'}, env=bad)
        self.assertEqual(out['status'], 'malformed')
        self.assertEqual(out['invocation_id'], '')


class ReceiptReadsTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE invocation_receipts(session TEXT,seq INTEGER,invocation_id TEXT UNIQUE,id TEXT,record_json TEXT)')
        self.store = SimpleNamespace(db=self.db)
        self.observation = obs.capture(NoLedgerAccess(), envelope(), payload(), {'summary': 'A stage.', 'remaining': []})

    def tearDown(self):
        self.db.close()

    def append(self, number, *, observed=True, session='task-one', corrupt=False, oversized=False):
        iid = str(number).zfill(32)
        record = {'invocation_id': iid, 'session': session, 'status': 'accepted'}
        if observed:
            observation = copy.deepcopy(self.observation)
            observation['invocation_id'] = iid
            observation['binding']['session'] = session
            record['coherence_observation'] = observation
        if oversized:
            record['padding'] = 'x' * (obs.MAX_RECEIPT_BYTES + 1)
        raw = canonical(record); digest = sha('invocation-receipt-v1:' + raw)
        self.db.execute('INSERT INTO invocation_receipts VALUES(?,?,?,?,?)',
                        (session, number, iid, 'wrong' if corrupt else digest, raw))
        self.db.commit()
        return iid, record.get('coherence_observation')

    def test_single_receipt_read_is_scoped_hash_checked_and_never_reanalyzes(self):
        iid, expected = self.append(1)
        self.assertEqual(obs.read_observation(self.store, iid, 'task-one'), expected)
        self.assertIsNone(obs.read_observation(self.store, iid, 'task-two'))
        before = self.db.total_changes
        self.db.execute("UPDATE invocation_receipts SET record_json=record_json || ' ' WHERE invocation_id=?", (iid,))
        self.db.commit()
        self.assertIsNone(obs.read_observation(self.store, iid, 'task-one'))
        self.assertEqual(self.db.total_changes, before + 1)

    def test_latest_inspects_only_32_recent_rows_and_limits_results(self):
        self.append(1)
        for index in range(2, 35):
            self.append(index, observed=False)
        statements = []; self.db.set_trace_callback(statements.append)
        self.assertEqual(obs.latest(self.store), [])
        for index in range(35, 50):
            self.append(index)
        rows = obs.latest(self.store)
        self.assertEqual(len(rows), 12)
        self.assertEqual(rows[0]['observation']['invocation_id'], str(49).zfill(32))
        self.assertTrue(any('ORDER BY rowid DESC LIMIT 32' in sql for sql in statements))
        self.assertFalse(any('json_extract' in sql or 'COUNT(' in sql for sql in statements))

    def test_legacy_corrupt_and_oversized_receipts_are_safely_omitted(self):
        old, _ = self.append(1, observed=False)
        bad, _ = self.append(2, corrupt=True)
        large, _ = self.append(3, oversized=True)
        for iid in (old, bad, large):
            self.assertIsNone(obs.read_observation(self.store, iid))
        self.assertEqual(obs.latest(self.store), [])

    def test_full_stored_observation_has_space_for_gateway_metadata(self):
        observation = copy.deepcopy(self.observation)
        observation['padding'] = ''
        observation['padding'] = 'x' * (obs.MAX_CAPTURE_BYTES - len(canonical(observation).encode()))
        self.assertEqual(len(canonical(observation).encode()), obs.MAX_CAPTURE_BYTES)
        observation.update(kernel={'decision': 'ALLOW', 'explanation': 'x' * 3800},
                           model='m' * 160, elapsed_ns=123456, gateway_status='accepted')
        self.assertLessEqual(len(canonical(observation).encode()), obs.MAX_OBSERVATION_BYTES)
        self.observation = observation
        iid, expected = self.append(1)
        self.assertEqual(obs.read_observation(self.store, iid, 'task-one'), expected)
        self.assertEqual(obs.latest(self.store)[0]['observation'], expected)

    def test_diagnostic_reader_handles_absent_table_and_rejects_unbounded_limit(self):
        self.db.execute('DROP TABLE invocation_receipts')
        self.assertIsNone(obs.read_observation(self.store, 'a' * 32))
        self.assertEqual(obs.latest(self.store), [])
        for limit in (0, 13, True, 1.5):
            with self.assertRaises(ValueError):
                obs.latest(self.store, limit)

    def test_locked_diagnostic_database_cannot_break_a_workflow_read(self):
        with tempfile.TemporaryDirectory() as folder:
            path = folder + '/ledger.sqlite'
            writer = sqlite3.connect(path)
            writer.execute('CREATE TABLE invocation_receipts(session TEXT,invocation_id TEXT,id TEXT,record_json TEXT)')
            writer.commit()
            reader = sqlite3.connect(path, timeout=.01)
            try:
                writer.execute('BEGIN EXCLUSIVE')
                writer.execute("INSERT INTO invocation_receipts VALUES('session','id','digest','{}')")
                self.assertIsNone(obs.read_observation(SimpleNamespace(db=reader), 'id', 'session'))
                self.assertTrue(writer.in_transaction)
            finally:
                writer.rollback(); reader.close(); writer.close()


if __name__ == '__main__':
    unittest.main()
