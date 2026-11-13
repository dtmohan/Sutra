import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from budget_preflight import host_seed
from checked_protocol import SCHEMA, SYSTEM, HOST_FIELDS, attach_contract, worker_schema
from dag_runtime import run_paged
from evidence import EvidenceContext
from paging import Budget
from policy_check import check, evaluate, scenario_request, UnverifiablePolicy
from validate_dag import build_new, new_definitions, RETRY_POLICY, RELEASE_POLICY


class CheckedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.tmp.cleanup()

    def fixture(self, name='N06-permitted-branch', expand=True):
        definition = next(d for d in new_definitions() if d['id'] == name)
        with patch('experiment.ingest', host_seed):
            case = build_new(definition, self.root / name, None)
        store = EvidenceContext(case['db']); self.stores.append(store)
        payload, rid, _ = store.assemble(case['session'], case['request'],
            ['tests/test_policy.py'] + case['seed_pages'], required_facts=case['required_facts'],
            system=SYSTEM, schema=SCHEMA, final=True, expand=expand,
            policy_request=case.get('policy_request'))
        store.confirm_delivery(rid)
        return store, case, payload, rid

    def proposal(self, decision='ALLOW'):
        return {'action': 'run_tests', 'target': 'tests/test_policy.py', 'complete': False,
                'facts': [{'key': 'policy_marker', 'value': 'R-12', 'page_id': 'policy-entry'}],
                'unavailable_evidence': [], 'interpretation': decision,
                'interpretation_sources': ['rule-0', 'rule-1', 'policy-entry']}

    def test_host_envelope_persists_constraints_without_model_echo(self):
        store, case, payload, rid = self.fixture()
        proposal = self.proposal()
        result = store.finish(case['session'], rid, proposal, case['required_facts'])
        self.assertTrue(result['admission']['admitted'])
        self.assertEqual(result['model_proposal'], proposal)
        self.assertTrue(HOST_FIELDS.isdisjoint(proposal))
        self.assertEqual(result['answer'], attach_contract(proposal, payload['contract']))
        self.assertEqual(result['contract_origin']['revision'], payload['contract']['revision'])

    def test_model_cannot_inject_or_override_host_fields(self):
        _, _, payload, _ = self.fixture()
        for field in HOST_FIELDS:
            with self.subTest(field=field), self.assertRaises(ValueError):
                attach_contract(dict(self.proposal(), **{field: []}), payload['contract'])

    def test_contract_changes_during_generation_reject_old_envelope(self):
        store, case, _, rid = self.fixture()
        text = 'Limit attempts to 4.'
        store.apply_user_patch(case['session'], 'change', text,
            [{'key': 'max_attempts', 'value': '4', 'quote': text}], store.snapshot(case['session'])['revision'])
        result = store.finish(case['session'], rid, self.proposal(), case['required_facts'])
        self.assertFalse(result['admission']['admitted'])
        self.assertFalse(result['admission']['interpretation_semantics_verified'])

    def test_string_false_cannot_masquerade_as_boolean_completion(self):
        _, _, payload, _ = self.fixture()
        proposal = self.proposal(); proposal['complete'] = 'false'
        with self.assertRaises(ValueError):
            attach_contract(proposal, payload['contract'])

    def test_incorrect_release_decision_is_rejected(self):
        store, case, _, rid = self.fixture()
        result = store.finish(case['session'], rid, self.proposal('DENY'), case['required_facts'])
        self.assertFalse(result['admission']['admitted'])
        self.assertEqual(result['admission']['policy_verification']['computed_decision'], 'ALLOW')
        self.assertEqual(result['model_proposal']['interpretation'], 'DENY')

    def test_validated_decision_is_not_a_completion_claim(self):
        store, case, _, rid = self.fixture()
        bad = self.proposal(); bad.update(complete=True, action='report', target='')
        result = store.finish(case['session'], rid, bad, case['required_facts'])
        self.assertFalse(result['admission']['admitted'])
        self.assertFalse(result['admission']['interpretation_semantics_verified'])

    def test_missing_prerequisites_never_verified(self):
        store, case, payload, rid = self.fixture(expand=False)
        result = store.finish(case['session'], rid, self.proposal(), case['required_facts'])
        self.assertFalse(result['admission']['admitted'])
        self.assertEqual(result['admission']['policy_verification']['status'], 'unverifiable')
        abstain = self.proposal(''); abstain.update(action='clarify', target='', interpretation_sources=[])
        self.assertEqual(check(payload, abstain)['errors'], [])
        self.assertFalse(check(payload, abstain)['verified'])

    def test_source_revision_after_delivery_blocks_verification(self):
        store, case, _, rid = self.fixture()
        doc = store.document(case['session'], 'policy-source')
        store.put_document(case['session'], 'policy-source', doc['text'] + '\nChanged.',
            source={'fixture': 'revision'}, expected_version=1)
        result = store.finish(case['session'], rid, self.proposal(), case['required_facts'])
        self.assertFalse(result['admission']['admitted'])
        self.assertFalse(result['admission']['interpretation_semantics_verified'])

    def test_tampered_delivered_rule_cannot_be_certified(self):
        store, case, payload, _ = self.fixture()
        payload['pages'][1]['text'] = 'Forged rule'
        rid = store.prepare_delivery(case['session'], payload, 500); store.confirm_delivery(rid)
        result = store.finish(case['session'], rid, self.proposal(), case['required_facts'])
        self.assertFalse(result['admission']['admitted'])
        self.assertFalse(result['admission']['interpretation_semantics_verified'])

    def test_unrecognized_rule_and_extra_rule_fail_closed(self):
        for texts in [RELEASE_POLICY[:-1] + ['An emergency can bypass approval.'],
                      [RELEASE_POLICY[0], RELEASE_POLICY[1].replace('AND', 'OR') + ' Extra condition.']]:
            with self.subTest(texts=texts), self.assertRaises(UnverifiablePolicy):
                evaluate('release', texts, {'approval': True, 'tests': 'current and passed', 'freeze': False})

    def test_input_types_missing_fields_and_unknown_status_rejected(self):
        bad = ['approval=true; tests=passed; freeze=false.',
               'approval=true; tests=current and passed.',
               'approval=1; tests=current and passed; freeze=false.',
               'approval=true; tests=current and passed; freeze=false; freeze=true.',
               'approval=true; tests=current and passed; freeze=false; admin=true.']
        for scenario in bad:
            with self.subTest(scenario=scenario), self.assertRaises(ValueError):
                scenario_request('release', scenario)

    def test_retry_branch_matrix_and_changed_source_numbers(self):
        rows = [('production', 'premium', False, 'retry_limit=6'),
                ('production', 'basic', False, 'retry_limit=2'),
                ('production', 'premium', True, 'retry_limit=0'),
                ('production', 'basic', True, 'retry_limit=0'),
                ('development', 'premium', False, 'DISABLED'),
                ('development', 'basic', False, 'DISABLED'),
                ('development', 'premium', True, 'DISABLED'),
                ('development', 'basic', True, 'DISABLED')]
        for env, plan, incident, expected in rows:
            with self.subTest(env=env, plan=plan, incident=incident):
                self.assertEqual(evaluate('retry', RETRY_POLICY[:-1],
                    {'environment': env, 'plan': plan, 'incident': incident}), expected)
        changed = [t.replace('production', 'staging').replace('premium', 'enterprise')
                    .replace('retry_limit=6', 'retry_limit=17').replace('retry_limit=2', 'retry_limit=5')
                    .replace('retry_limit=0', 'retry_limit=1') for t in RETRY_POLICY[:-1]]
        self.assertEqual(evaluate('retry', changed, {'environment': 'staging', 'plan': 'enterprise', 'incident': False}), 'retry_limit=17')
        self.assertEqual(evaluate('retry', changed, {'environment': 'staging', 'plan': 'basic', 'incident': False}), 'retry_limit=5')
        self.assertEqual(evaluate('retry', changed, {'environment': 'staging', 'plan': 'enterprise', 'incident': True}), 'retry_limit=1')
        self.assertEqual(evaluate('retry', changed, {'environment': 'production', 'plan': 'enterprise', 'incident': False}), 'DISABLED')

    def test_release_truth_table(self):
        rows = [(False, False, False, 'DENY'), (False, False, True, 'DENY'),
                (False, True, False, 'DENY'), (False, True, True, 'DENY'),
                (True, False, False, 'DENY'), (True, False, True, 'DENY'),
                (True, True, False, 'ALLOW'), (True, True, True, 'DENY')]
        for approval, passed, freeze, expected in rows:
            with self.subTest(approval=approval, passed=passed, freeze=freeze):
                self.assertEqual(evaluate('release', RELEASE_POLICY[:-1],
                    {'approval': approval, 'tests': 'current and passed' if passed else 'stale', 'freeze': freeze}), expected)

    def test_formatting_mismatch_is_not_silently_repaired(self):
        _, _, payload, _ = self.fixture('N02-outer-scope')
        proposal = self.proposal('retry_limit=DISABLED')
        result = check(payload, proposal)
        self.assertEqual(result['status'], 'mismatch')
        self.assertEqual(result['computed_decision'], 'DISABLED')
        self.assertEqual(proposal['interpretation'], 'retry_limit=DISABLED')

    def test_unconfigured_interpretation_is_unverified(self):
        result = check({}, self.proposal())
        self.assertFalse(result['verified'])
        self.assertTrue(result['errors'])

    def test_model_claims_do_not_override_rule_sources(self):
        _, _, payload, _ = self.fixture('N10-untrusted-override')
        result = check(payload, self.proposal('ALLOW'))
        self.assertEqual(result['computed_decision'], 'DENY')
        self.assertFalse(result['verified'])
        payload['pages'][-1]['authority'] = 'model'
        # Mutating an unrelated model note cannot change the evaluated rule.
        self.assertEqual(check(payload, self.proposal('DENY'))['computed_decision'], 'DENY')

    def test_runtime_binds_envelope_and_rejects_semantic_mismatch(self):
        _, case, _, _ = self.fixture()
        proposal = self.proposal('DENY')
        class FakeModel:
            budget = Budget()
            def call(self, call_id, system, payload, schema):
                assert not HOST_FIELDS & set(schema['properties'])
                assert 'expected' not in payload
                return copy.deepcopy(proposal), {'call_id': call_id}
        result = run_paged(case['db'], case['session'], case['request'], case['required_facts'], FakeModel(),
            'test', initial_resident=case['seed_pages'], policy_request=case['policy_request'])
        self.assertFalse(result['admission']['admitted'])
        self.assertTrue(result['answer']['constraints'])
        self.assertEqual(result['model_proposal'], proposal)

    def test_schema_prevents_duplicate_agreement_and_extra_absence_entries(self):
        _, _, payload, _ = self.fixture('N08-agreeing-sources')
        fields = worker_schema(payload)['properties']
        self.assertEqual(fields['facts']['minItems'], 1)
        self.assertEqual(fields['facts']['maxItems'], 1)
        self.assertEqual(fields['unavailable_evidence']['maxItems'], 0)
        self.assertNotIn('enum', fields['facts']['items']['properties']['value'])
        self.assertEqual(fields['interpretation'], {'type': 'string'})

    def test_schema_requires_clarification_for_stale_policy(self):
        _, _, payload, _ = self.fixture('N09-source-revision')
        fields = worker_schema(payload)['properties']
        self.assertEqual(fields['action']['enum'], ['clarify'])
        self.assertEqual(fields['target']['enum'], [''])
        self.assertEqual(fields['facts']['maxItems'], 0)
        self.assertEqual(fields['unavailable_evidence']['items']['properties']['state']['enum'], ['stale'])

    def test_workflow_uses_task_tests_not_scenario_tests(self):
        _, _, payload, _ = self.fixture()
        fields = worker_schema(payload)['properties']
        self.assertEqual(fields['action']['enum'], ['run_tests'])
        self.assertEqual(fields['target']['enum'], ['tests/test_policy.py'])
        self.assertEqual(fields['interpretation'], {'type': 'string'})
        leaf = copy.deepcopy(payload)
        leaf['pages'] = [p for p in leaf['pages'] if p['id'] != 'rule-0']
        self.assertEqual(worker_schema(leaf)['properties']['action']['enum'], ['clarify'])

    def test_admission_rechecks_workflow_even_if_model_ignores_schema(self):
        store, case, _, rid = self.fixture()
        proposal = self.proposal(); proposal.update(action='report', target='')
        result = store.finish(case['session'], rid, proposal, case['required_facts'])
        self.assertFalse(result['admission']['admitted'])
        self.assertIn('proposal violates host workflow: action', result['admission']['errors'])


if __name__ == '__main__':
    unittest.main()
