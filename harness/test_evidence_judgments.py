"""Evidence choices derive reviewer status without admitting invented support."""
import copy
import json
import unittest

import claim_pages
from context_manager import InvalidUpdate, canonical, sha
from invocation_gateway import InvocationLedger
import model_packet
import report_review
import review_budget
import test_chat as fixture
import test_evidence_sets as set_fixture
from test_model_identity import identity
from test_model_packet import WireModel


def judgment_packet():
    return dict(set_fixture.evidence_packet(), review_judgment_version=1)


def raw_choices():
    return {'claims': {
        'overview': {'reason': 'Both modules contain the described definitions.',
                     'supporting_evidence_ids': ['eb1', 'ea1', 'eb1'], 'conflicting_evidence_ids': []},
        'components/0': {'reason': 'The supplied return conflicts with the claimed value.',
                         'supporting_evidence_ids': [], 'conflicting_evidence_ids': ['ea2', 'ea1', 'ea2']},
        'flow/0': {'reason': 'This page does not establish the complete call path.',
                   'supporting_evidence_ids': [], 'conflicting_evidence_ids': []},
    }}


class EvidenceJudgmentBindingTests(unittest.TestCase):
    def bind(self, payload, raw):
        return model_packet.bind_output('report_reviewer', payload, report_review.schema(payload), raw,
                                        model_packet.REVIEW_JUDGMENT_VERSION)

    def test_new_wire_selects_evidence_channels_while_host_schema_keeps_explicit_judgments(self):
        payload = judgment_packet()
        host_schema = report_review.schema(payload)
        unchanged = copy.deepcopy(host_schema)
        wire = model_packet.build(report_review.SYSTEM, payload, host_schema)
        self.assertEqual(wire['version'], model_packet.REVIEW_JUDGMENT_VERSION)
        self.assertEqual(wire['version'], 15)
        for key, judgment in wire['schema']['properties']['claims']['properties'].items():
            self.assertEqual(set(judgment['properties']),
                             {'reason', 'supporting_evidence_ids', 'conflicting_evidence_ids'})
            offered = {eid for eid, entry in payload['source_evidence'].items()
                       if entry['path'] in payload['claims'][key]['paths']}
            for field in ('supporting_evidence_ids', 'conflicting_evidence_ids'):
                self.assertEqual(set(judgment['properties'][field]['items']['enum']), offered)
        self.assertEqual(host_schema, unchanged)
        self.assertIn('verdict', host_schema['properties']['claims']['properties']['overview']['properties'])

    def test_support_conflict_and_empty_channels_derive_only_the_corresponding_status(self):
        payload, raw = judgment_packet(), raw_choices()
        original = copy.deepcopy(raw)
        bound, _ = self.bind(payload, raw)
        self.assertEqual(raw, original)
        expected = {'overview': ('supported', ['eb1', 'ea1']),
                    'components/0': ('contradicted', ['ea2', 'ea1']),
                    'flow/0': ('insufficient_evidence', [])}
        for key, (verdict, ids) in expected.items():
            self.assertEqual(bound['claims'][key],
                             {'verdict': verdict, 'reason': original['claims'][key]['reason'],
                              'evidence_ids': ids})
        self.assertEqual(bound['complete'], False)
        self.assertEqual(bound['constraints'], payload['constraint_echo'])
        recovered = model_packet.recover_judgment_choices({'claims': copy.deepcopy(bound['claims'])})
        proof = model_packet.evidence_set_changes('report_reviewer', payload, raw, bound)
        restored = model_packet.restore_evidence_sets('report_reviewer', payload,
                                                      report_review.schema(payload), recovered, proof,
                                                      model_packet.REVIEW_JUDGMENT_VERSION)
        self.assertEqual(restored, raw)
        self.assertEqual(proof['version'], 2)
        self.assertEqual(proof['raw_channels']['overview']['supporting_evidence_ids'], ['eb1', 'ea1', 'eb1'])
        self.assertEqual(proof['raw_channels']['components/0']['conflicting_evidence_ids'], ['ea2', 'ea1', 'ea2'])

    def test_unknown_other_claim_and_nonstring_ids_are_not_rescued_by_derivation_or_dedup(self):
        for channel in ('supporting_evidence_ids', 'conflicting_evidence_ids'):
            for ids in (['ea1', 'invented', 'ea1'], ['ea1', 'eb1', 'ea1'], [True, True]):
                with self.subTest(channel=channel, ids=ids):
                    raw = raw_choices()
                    item = raw['claims']['components/0']
                    item.update(supporting_evidence_ids=[], conflicting_evidence_ids=[])
                    item[channel] = ids
                    before = copy.deepcopy(raw)
                    with self.assertRaises((InvalidUpdate, ValueError)):
                        self.bind(judgment_packet(), raw)
                    self.assertEqual(raw, before)

    def test_model_authored_verdict_and_simultaneous_support_and_conflict_are_rejected(self):
        explicit = raw_choices()
        explicit['claims']['overview']['verdict'] = 'supported'
        ambiguous = raw_choices()
        ambiguous['claims']['overview']['conflicting_evidence_ids'] = ['ea1']
        for raw in (explicit, ambiguous):
            with self.subTest(raw=raw), self.assertRaises((InvalidUpdate, ValueError)):
                self.bind(judgment_packet(), raw)

    def test_raw_shape_limits_still_apply_before_normalization(self):
        malformed = raw_choices()
        del malformed['claims']['components/0']['reason']
        oversized = raw_choices()
        oversized['claims']['components/0']['conflicting_evidence_ids'] = ['ea1'] * 4
        missing = raw_choices()
        del missing['claims']['flow/0']['supporting_evidence_ids']
        for raw in (malformed, oversized, missing):
            with self.subTest(raw=raw), self.assertRaises((InvalidUpdate, ValueError)):
                self.bind(judgment_packet(), raw)

    def test_legacy_no_profile_wire_stays_identical_to_released_0541(self):
        payload = set_fixture.evidence_packet(current=False)
        wire = model_packet.build(report_review.SYSTEM, payload, report_review.schema(payload))
        self.assertEqual(wire['version'], 1)
        self.assertEqual(sha(canonical(wire)),
                         '5d66f77d7341ed567da86e77269b86f44ef169d37bd7a5b458ba098799c1b717')
        old = set_fixture.raw_judgments()
        bound, _ = model_packet.bind_output('report_reviewer', payload, report_review.schema(payload), old)
        self.assertEqual(bound['claims'], old['claims'])

    def test_claim_page_inherits_only_the_explicit_candidate_profile(self):
        source = judgment_packet()
        source.update(workspace='/synthetic/repo', review_intent_version=1, report_schema_version=2)
        plan = {'id': 'synthetic-plan',
                'pages': [{'claims': list(source['claims']), 'evidence': list(source['source_evidence'])}]}
        draft = {'report': {'overview': 'Fixture report.'}}
        current = claim_pages.packet(source, draft, 'candidate', plan, 0, source['claims'],
                                     source['source_evidence'], {}, False)
        self.assertEqual(current['review_judgment_version'], 1)
        legacy_source = copy.deepcopy(source)
        legacy_source.pop('review_judgment_version')
        legacy = claim_pages.packet(legacy_source, draft, 'candidate', plan, 0, source['claims'],
                                    source['source_evidence'], {}, False)
        self.assertNotIn('review_judgment_version', legacy)


class EvidenceJudgmentGatewayTests(unittest.TestCase):
    setUp = fixture.ChatTests.setUp
    tearDown = fixture.ChatTests.tearDown

    def candidate(self, *, large=False):
        from unittest.mock import patch
        import auto_review
        import chat_runtime

        if large:
            (self.ws / 'entry.py').write_text(
                'from leaf import run\ndef start():\n' +
                ''.join(f'    # source span {index:02}: retain concrete entry module evidence.\n'
                        for index in range(16)) + '    return run()\n')
            auto_review.step(self.s, self.meta, self.ws, self.storage,
                             {'explain': False}, provider=fixture.Fake())
        writer = fixture.Fake()
        writer.model_identity = identity(writer.model, 'writer-weights')
        with patch.object(report_review, 'run', side_effect=InvalidUpdate('defer checker')):
            out = chat_runtime.answer(self.s, self.meta, self.ws, self.storage,
                                      {'prompt': 'Explain the project and its control flow.',
                                       'models': fixture.MODELS, 'tier': 'low'}, provider=writer)
        return out['result']['invocation_id']

    def reviewer(self):
        reviewer = WireModel('independent-reviewer')
        reviewer.model_identity = identity(reviewer.model, 'reviewer-weights')
        return reviewer

    def rejected_receipts(self, candidate_id):
        return [json.loads(raw) for raw, in self.s.db.execute(
            "SELECT r.record_json FROM invocation_receipts r JOIN invocations i ON i.id=r.invocation_id "
            "JOIN deliveries d ON d.id=i.delivery_id WHERE i.session=? AND "
            "json_extract(i.record_json,'$.role')='report_reviewer' AND "
            "json_extract(d.payload_json,'$.candidate_id')=? ORDER BY i.rowid", (self.sid, candidate_id))]

    def check_rejected_raw(self, *, ambiguous):
        candidate_id = self.candidate()
        reviewer = self.reviewer()
        originals = []

        def invalid(raw, payload):
            key = next(iter(raw['claims']))
            item = raw['claims'][key]
            if ambiguous:
                item['conflicting_evidence_ids'] = list(item['supporting_evidence_ids'])
                self.assertTrue(item['conflicting_evidence_ids'])
            else:
                item['supporting_evidence_ids'] = ['unknown-source-id']
            originals.append(copy.deepcopy(raw))

        reviewer.after = invalid
        before = review_budget.usage(self.s, self.sid)
        result = report_review.run(self.s, self.sid, self.storage, candidate_id, reviewer, strict=True)
        self.assertFalse(result['admission']['admitted'], result)
        receipts = self.rejected_receipts(candidate_id)
        self.assertEqual(len(receipts), 2)
        for receipt, original in zip(receipts, originals):
            self.assertEqual(receipt['status'], 'quarantined')
            self.assertEqual(receipt['host_fields'], {})
            self.assertEqual(receipt['proposal'], original)
            self.assertEqual(receipt['model_response_hash'], sha(canonical(original)))
            self.assertNotIn('evidence_reference_sets', receipt)
        after = review_budget.usage(self.s, self.sid)
        self.assertEqual(after['used']['calls'], before['used']['calls'] + 2)
        self.assertEqual(after['used']['tokens'], before['used']['tokens'] + 2 * sum(reviewer.counts))
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_unknown_raw_ids_are_quarantined_without_changing_the_auditable_response(self):
        self.check_rejected_raw(ambiguous=False)

    def test_ambiguous_raw_channels_are_quarantined_without_changing_the_auditable_response(self):
        self.check_rejected_raw(ambiguous=True)

    def test_gateway_binds_empty_choices_without_inventing_evidence_and_preserves_raw_receipt(self):
        candidate_id = self.candidate()
        reviewer = self.reviewer()
        originals = []

        def choose(raw, payload):
            self.assertEqual(reviewer.packet['version'], model_packet.REVIEW_JUDGMENT_VERSION)
            self.assertEqual(payload['review_judgment_version'], 1)
            for judgment in raw['claims'].values():
                judgment.update(supporting_evidence_ids=[], conflicting_evidence_ids=[],
                                reason='The delivered page does not settle this whole claim.')
            originals.append(copy.deepcopy(raw))

        reviewer.after = choose
        before = review_budget.usage(self.s, self.sid)
        contract = self.s.pinned(self.sid)
        reviewed = report_review.run(self.s, self.sid, self.storage, candidate_id, reviewer, strict=True)
        self.assertTrue(reviewed['admission']['admitted'], reviewed)
        self.assertEqual(len(originals), 1)
        iid = reviewed['invocation_id']
        receipt = json.loads(self.s.db.execute(
            'SELECT record_json FROM invocation_receipts WHERE invocation_id=?', (iid,)).fetchone()[0])
        self.assertEqual(receipt['model_response_hash'], sha(canonical(originals[0])))
        for key, judgment in receipt['proposal']['claims'].items():
            self.assertEqual(judgment, {'verdict': 'insufficient_evidence', 'evidence_ids': [],
                                        'reason': originals[0]['claims'][key]['reason']})
        published, meta = report_review.published(self.s, self.sid, candidate_id, iid)
        self.assertTrue(meta['omitted'])
        self.assertEqual(published['report']['flow'], [])
        self.assertFalse(meta['semantic_correctness_verified'])
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

    def test_duplicate_selected_channel_retains_raw_proof_and_replays_after_reload(self):
        candidate_id = self.candidate(large=True)
        reviewer = self.reviewer()
        originals = []

        def duplicate(raw, payload):
            for key, judgment in raw['claims'].items():
                allowed = [eid for eid, entry in payload['source_evidence'].items()
                           if entry['path'] in payload['claims'][key]['paths']]
                if len(allowed) > 1:
                    judgment['supporting_evidence_ids'] = [allowed[0], allowed[0]]
                judgment['conflicting_evidence_ids'] = []
            self.assertTrue(any(len(item['supporting_evidence_ids']) >
                                len(set(item['supporting_evidence_ids'])) for item in raw['claims'].values()))
            originals.append(copy.deepcopy(raw))

        reviewer.after = duplicate
        before = review_budget.usage(self.s, self.sid)
        result = report_review.run(self.s, self.sid, self.storage, candidate_id, reviewer, strict=True)
        self.assertTrue(result['admission']['admitted'], result)
        self.assertEqual(len(originals), 1)
        iid = result['invocation_id']
        receipt = json.loads(self.s.db.execute(
            'SELECT record_json FROM invocation_receipts WHERE invocation_id=?', (iid,)).fetchone()[0])
        self.assertEqual(receipt['model_response_hash'], sha(canonical(originals[0])))
        self.assertEqual(receipt['evidence_reference_sets']['version'], 2)
        for key, channels in receipt['evidence_reference_sets']['raw_channels'].items():
            self.assertEqual(channels, {'supporting_evidence_ids': originals[0]['claims'][key]['supporting_evidence_ids']})
        for key, judgment in receipt['proposal']['claims'].items():
            self.assertEqual(judgment['verdict'], 'supported')
            self.assertEqual(judgment['evidence_ids'],
                             list(dict.fromkeys(originals[0]['claims'][key]['supporting_evidence_ids'])))
        self.assertTrue(self.s.verify_invocations(self.sid))
        after = review_budget.usage(self.s, self.sid)
        self.assertEqual(after['used']['calls'], before['used']['calls'] + 1)
        self.assertEqual(after['used']['tokens'], before['used']['tokens'] + sum(reviewer.counts))
        self.assertEqual(after['limits'], before['limits'])
        self.s.close()
        self.s = InvocationLedger(self.storage / 'state.sqlite')
        self.assertEqual(self.s.load_result(self.sid, iid), receipt['proposal'])
        self.assertTrue(self.s.verify_invocations(self.sid))
        self.assertEqual(review_budget.usage(self.s, self.sid), after)

    def test_post_binding_host_rejection_retains_reversible_raw_channels_and_multiplicity(self):
        from unittest.mock import patch

        candidate_id = self.candidate(large=True)
        reviewer = self.reviewer()
        originals = []

        def duplicate(raw, payload):
            for key, item in raw['claims'].items():
                choices = [eid for eid, entry in payload['source_evidence'].items()
                           if entry['path'] in payload['claims'][key]['paths']]
                if len(choices) > 1:
                    item['supporting_evidence_ids'] = [choices[0], choices[0]]
            originals.append(copy.deepcopy(raw))

        reviewer.after = duplicate
        original_validate = report_review.validate

        def host_rejects(s, sid, payload, proposal):
            original_validate(s, sid, payload, proposal)
            raise InvalidUpdate('Injected host rejection after successful evidence binding')

        before = review_budget.usage(self.s, self.sid)
        with patch.object(report_review, 'validate', side_effect=host_rejects):
            result = report_review.run(self.s, self.sid, self.storage, candidate_id, reviewer, strict=True)
        self.assertFalse(result['admission']['admitted'], result)
        receipts = self.rejected_receipts(candidate_id)
        self.assertEqual(len(receipts), 2)
        for receipt, original in zip(receipts, originals):
            self.assertEqual(receipt['status'], 'quarantined')
            self.assertTrue(receipt['host_fields'])
            self.assertEqual(receipt['evidence_reference_sets']['version'], 2)
            self.assertEqual(receipt['model_response_hash'], sha(canonical(original)))
            for key, item in receipt['proposal']['claims'].items():
                self.assertEqual(item['verdict'], 'supported')
                self.assertEqual(item['evidence_ids'],
                                 list(dict.fromkeys(original['claims'][key]['supporting_evidence_ids'])))
        self.assertTrue(self.s.verify_invocations(self.sid))
        after = review_budget.usage(self.s, self.sid)
        self.assertEqual(after['used']['calls'], before['used']['calls'] + 2)
        self.assertEqual(after['used']['tokens'], before['used']['tokens'] + 2 * sum(reviewer.counts))
        self.assertEqual(after['limits'], before['limits'])

    def test_claim_choice_profile_is_bound_to_the_candidate_across_recovery(self):
        candidate_id = self.candidate()
        reviewer = self.reviewer()
        result = report_review.run(self.s, self.sid, self.storage, candidate_id, reviewer, strict=True)
        self.assertTrue(result['admission']['admitted'], result)
        payload = reviewer.payloads[0]
        self.assertEqual(payload['review_judgment_version'], 1)
        for version in (None, True, 0, 2, '1'):
            with self.subTest(version=version):
                changed = copy.deepcopy(payload)
                if version is None:
                    changed.pop('review_judgment_version')
                else:
                    changed['review_judgment_version'] = version
                with self.assertRaises(InvalidUpdate):
                    report_review.request_check(self.s, self.sid, changed)


if __name__ == '__main__':
    unittest.main()
