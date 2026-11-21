"""Evidence-ID set normalization preserves source scope, judgments and raw receipts."""
import copy
import json
import unittest
from unittest.mock import patch

import auto_review
import chat_runtime
from context_manager import InvalidUpdate, canonical, sha
from invocation_gateway import InvocationLedger
import model_packet
import report_review
import review_budget
import test_chat as fixture
from test_model_identity import identity
from test_model_packet import WireModel


def evidence_packet(*, current=True):
    contract = {'task_id': 'evidence-fixture', 'goal': 'Review the supplied claims.',
                'constraints': {'review_only': True}}
    result = {
        'contract': contract, 'constraint_echo': [{'key': 'review_only', 'value': 'true'}],
        'review_protocol': 2, 'evidence_choice_version': 2,
        'claims': {'overview': {'paths': ['a.py', 'b.py'], 'text': 'Describe both modules.'},
                   'components/0': {'paths': ['a.py'], 'text': 'Describe module A.'},
                   'flow/0': {'paths': ['b.py'], 'text': 'Describe module B.'}},
        'source_evidence': {
            'ea1': {'path': 'a.py', 'text': 'def start():\n'},
            'ea2': {'path': 'a.py', 'text': '    value = 1\n'},
            'ea3': {'path': 'a.py', 'text': '    return value\n'},
            'eb1': {'path': 'b.py', 'text': 'def finish():\n'},
            'eb2': {'path': 'b.py', 'text': '    return 2\n'},
        },
    }
    if current:
        result['evidence_set_version'] = 1
    return result


def raw_judgments():
    return {'claims': {
        'overview': {'verdict': 'supported', 'reason': 'Both modules are supplied.',
                     'evidence_ids': ['eb1', 'ea1', 'eb1']},
        'components/0': {'verdict': 'contradicted', 'reason': 'The source returns a different value.',
                         'evidence_ids': ['ea2', 'ea1', 'ea2']},
        'flow/0': {'verdict': 'insufficient_evidence', 'reason': 'The complete call path is not shown.',
                   'evidence_ids': []},
    }}


class EvidenceSetBindingTests(unittest.TestCase):
    def bind(self, payload, raw):
        return model_packet.bind_output('report_reviewer', payload, report_review.schema(payload), raw)

    def test_valid_repeated_ids_form_stable_sets_without_mutating_raw_or_judgments(self):
        payload = evidence_packet()
        raw = raw_judgments()
        untouched = copy.deepcopy(raw)
        bound, fields = self.bind(payload, raw)
        self.assertEqual(raw, untouched)
        self.assertEqual(bound['claims']['overview']['evidence_ids'], ['eb1', 'ea1'])
        self.assertEqual(bound['claims']['components/0']['evidence_ids'], ['ea2', 'ea1'])
        for key, judgment in raw['claims'].items():
            self.assertEqual(bound['claims'][key]['verdict'], judgment['verdict'])
            self.assertEqual(bound['claims'][key]['reason'], judgment['reason'])
        again = {'claims': copy.deepcopy(bound['claims'])}
        rebound, again_fields = self.bind(payload, again)
        self.assertEqual(rebound, bound)
        self.assertEqual(again_fields, fields)
        self.assertEqual(bound['complete'], False)
        self.assertEqual(bound['constraints'], payload['constraint_echo'])

    def test_unknown_and_other_claim_ids_are_rejected_before_normalization(self):
        payload = evidence_packet()
        for ids in (['ea1', 'invented', 'ea1'], ['ea1', 'eb1', 'ea1'], [True, True]):
            with self.subTest(ids=ids):
                raw = raw_judgments()
                raw['claims']['components/0']['evidence_ids'] = ids
                untouched = copy.deepcopy(raw)
                with self.assertRaises((InvalidUpdate, ValueError)):
                    self.bind(payload, raw)
                self.assertEqual(raw, untouched)

    def test_deduplication_cannot_rescue_an_oversized_or_malformed_raw_response(self):
        payload = evidence_packet()
        invalid = []
        oversized = raw_judgments()
        oversized['claims']['components/0']['evidence_ids'] = ['ea1'] * 4
        invalid.append(oversized)
        missing = raw_judgments()
        del missing['claims']['components/0']['reason']
        invalid.append(missing)
        verdict = raw_judgments()
        verdict['claims']['components/0']['verdict'] = 'approved'
        invalid.append(verdict)
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises((InvalidUpdate, ValueError)):
                self.bind(payload, raw)

    def test_legacy_duplicate_ids_remain_unchanged_and_fail_the_existing_host_guard(self):
        payload = evidence_packet(current=False)
        raw = raw_judgments()
        original = copy.deepcopy(raw)
        wire = model_packet.build(report_review.SYSTEM, payload, report_review.schema(payload))
        bound, _ = self.bind(payload, raw)
        self.assertEqual(raw, original)
        self.assertEqual(bound['claims'], original['claims'])
        self.assertNotIn('evidence_set_version', json.loads(wire['messages'][1]['content']))
        with patch.object(report_review, 'request_check', return_value=None):
            with self.assertRaisesRegex(InvalidUpdate, 'Duplicate evidence IDs'):
                report_review.validate(None, None, payload, bound)

    def test_normalization_proof_restores_exact_order_and_multiplicity(self):
        payload = evidence_packet()
        raw = raw_judgments()
        bound, _ = self.bind(payload, raw)
        proof = model_packet.evidence_set_changes('report_reviewer', payload, raw, bound)
        self.assertEqual(proof['raw_ids'], {
            'overview': ['eb1', 'ea1', 'eb1'],
            'components/0': ['ea2', 'ea1', 'ea2'],
        })
        normalized = {'claims': copy.deepcopy(bound['claims'])}
        restored = model_packet.restore_evidence_sets(
            'report_reviewer', payload, report_review.schema(payload), normalized, proof)
        self.assertEqual(restored, raw)
        self.assertEqual(normalized, {'claims': bound['claims']})
        for changed in (
                dict(proof, version=True),
                dict(proof, raw_ids={'overview': ['ea1', 'eb1', 'ea1']}),
                dict(proof, raw_ids={'components/0': ['ea2', 'ea1', 'ea2', 'ea1']}),
                dict(proof, raw_ids={'components/0': ['ea2', 'ea1']}),
                dict(proof, raw_ids={'not-a-claim': ['ea1', 'ea1']})):
            with self.subTest(proof=changed), self.assertRaises((InvalidUpdate, ValueError)):
                model_packet.restore_evidence_sets(
                    'report_reviewer', payload, report_review.schema(payload), normalized, changed)


class EvidenceSetGatewayTests(unittest.TestCase):
    setUp = fixture.ChatTests.setUp
    tearDown = fixture.ChatTests.tearDown

    def candidate(self):
        # More than one evidence chunk per file lets a bounded raw response
        # repeat a permitted ID without exceeding the raw decoder's list cap.
        (self.ws / 'entry.py').write_text(
            'from leaf import run\ndef start():\n' +
            ''.join(f'    # retained source line {index:02}: evidence from the entry module.\n'
                    for index in range(16)) + '    return run()\n')
        auto_review.step(self.s, self.meta, self.ws, self.storage, {'explain': False}, provider=fixture.Fake())
        writer = fixture.Fake()
        writer.model_identity = identity(writer.model, 'writer-weights')
        with patch.object(chat_runtime, 'REVIEW_JUDGMENT_VERSION', None), \
                patch.object(report_review, 'run', side_effect=InvalidUpdate('defer checker')):
            out = chat_runtime.answer(self.s, self.meta, self.ws, self.storage,
                                      {'prompt': 'Explain the project and its control flow.',
                                       'models': fixture.MODELS, 'tier': 'low'}, provider=writer)
        return out['result']['invocation_id']

    def test_real_gateway_retains_raw_hash_and_charges_one_normalized_claim_call(self):
        candidate_id = self.candidate()
        reviewer = WireModel('independent-reviewer')
        reviewer.model_identity = identity(reviewer.model, 'reviewer-weights')
        raw_responses = []

        def duplicate(raw, payload):
            self.assertEqual(payload['evidence_set_version'], 1)
            for key, judgment in raw['claims'].items():
                choices = [eid for eid, source in payload['source_evidence'].items()
                           if source['path'] in payload['claims'][key]['paths']]
                if len(choices) >= 2:
                    judgment['evidence_ids'] = [choices[0], choices[0]]
            self.assertTrue(any(len(v['evidence_ids']) > len(set(v['evidence_ids']))
                                for v in raw['claims'].values()))
            raw_responses.append(copy.deepcopy(raw))

        reviewer.after = duplicate
        before = review_budget.usage(self.s, self.sid)
        contract = self.s.pinned(self.sid)
        result = report_review.run(self.s, self.sid, self.storage, candidate_id, reviewer, strict=True)
        self.assertTrue(result['admission']['admitted'], result)
        self.assertEqual(len(raw_responses), 1)
        iid = result['invocation_id']
        receipt = json.loads(self.s.db.execute(
            'SELECT record_json FROM invocation_receipts WHERE invocation_id=?', (iid,)).fetchone()[0])
        self.assertEqual(receipt['model_response_hash'], sha(canonical(raw_responses[0])))
        changed_sets = receipt['evidence_reference_sets']['raw_ids']
        self.assertTrue(changed_sets)
        for key, raw_ids in changed_sets.items():
            self.assertEqual(raw_ids, raw_responses[0]['claims'][key]['evidence_ids'])
        for key, judgment in receipt['proposal']['claims'].items():
            self.assertEqual(judgment['evidence_ids'], list(dict.fromkeys(raw_responses[0]['claims'][key]['evidence_ids'])))
            self.assertEqual(judgment['verdict'], raw_responses[0]['claims'][key]['verdict'])
            self.assertEqual(judgment['reason'], raw_responses[0]['claims'][key]['reason'])
        self.assertNotEqual(receipt['model_response_hash'], sha(canonical({'claims': receipt['proposal']['claims']})))
        after = review_budget.usage(self.s, self.sid)
        self.assertEqual(after['limits'], before['limits'])
        self.assertEqual(after['used']['calls'], before['used']['calls'] + 1)
        self.assertEqual(after['used']['tokens'], before['used']['tokens'] + sum(reviewer.counts))
        self.assertEqual(after['used']['source_bytes'], before['used']['source_bytes'])
        self.assertEqual(self.s.pinned(self.sid), contract)
        self.assertTrue(self.s.verify_invocations(self.sid))
        self.s.close()
        self.s = InvocationLedger(self.storage / 'state.sqlite')
        self.assertEqual(self.s.load_result(self.sid, iid), receipt['proposal'])
        self.assertTrue(self.s.verify_invocations(self.sid))
        self.assertEqual(review_budget.usage(self.s, self.sid), after)
        # Even after a privileged rewrite recomputes this last receipt's digest,
        # changing retained raw multiplicity cannot retain the old response hash.
        corrupted = copy.deepcopy(receipt)
        key = next(iter(corrupted['evidence_reference_sets']['raw_ids']))
        corrupted['evidence_reference_sets']['raw_ids'][key].append(changed_sets[key][0])
        for name, in self.s.db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='invocation_receipts'").fetchall():
            self.s.db.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        raw_receipt = canonical(corrupted)
        with self.s.db:
            self.s.db.execute('UPDATE invocation_receipts SET record_json=?,id=? WHERE invocation_id=?',
                              (raw_receipt, sha('invocation-receipt-v1:' + raw_receipt), iid))
        self.assertFalse(self.s.verify_invocations(self.sid))

    def test_review_packet_cannot_drop_or_change_its_candidate_bound_normalization_version(self):
        candidate_id = self.candidate()
        reviewer = WireModel('independent-reviewer')
        reviewer.model_identity = identity(reviewer.model, 'reviewer-weights')
        result = report_review.run(self.s, self.sid, self.storage, candidate_id, reviewer, strict=True)
        self.assertTrue(result['admission']['admitted'], result)
        payload = reviewer.payloads[0]
        self.assertEqual(payload['evidence_set_version'], 1)
        for value in (None, 2, True):
            with self.subTest(value=value):
                changed = copy.deepcopy(payload)
                if value is None:
                    changed.pop('evidence_set_version')
                else:
                    changed['evidence_set_version'] = value
                with self.assertRaisesRegex(InvalidUpdate, 'binding changed|evidence-set representation'):
                    report_review.request_check(self.s, self.sid, changed)


if __name__ == '__main__':
    unittest.main()
