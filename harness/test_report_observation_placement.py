"""Optional misplaced observations never replace report/evidence admission."""
import copy
import json
import sqlite3
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate, canonical, sha
from invocation_gateway import InvocationLedger
import task_observations as observations
import test_report_review as fixture
from test_chat import Fake
from test_model_packet import WireModel


class ReportObservationPlacementTests(unittest.TestCase):
    setUp = fixture.ReportReviewTests.setUp
    tearDown = fixture.ReportReviewTests.tearDown
    def review(self, writer, reviewer=None, **request):
        return fixture.chat.answer(self.s, self.meta, self.ws, self.storage,
            dict(prompt=request.pop('prompt', 'Explain the architecture.'),
                 models=dict(fixture.MODELS, high=writer.model), tier='high', **request),
            provider=writer, review_provider=reviewer or Fake('reviewer'))

    def writer(self, value='An optional, malformed self-assessment.', fault=None, top=False):
        model = WireModel('strata:high'); self.raw = []
        def response(raw, payload):
            choices = json.loads(model.packet['messages'][1]['content'])['citation_choices']
            raw['citations'] = [choices[0]['id']]
            if top:
                raw[observations.FIELD] = copy.deepcopy(value)
            else:
                raw['report'][observations.FIELD] = copy.deepcopy(value)
            if fault:
                fault(raw, payload)
            self.raw.append(copy.deepcopy(raw))
        model.after = response
        return model

    def writer_receipt(self):
        iid = self.s.db.execute("SELECT id FROM invocations WHERE session=? AND json_extract(record_json,'$.role')='chat_reviewer' ORDER BY rowid DESC LIMIT 1", (self.sid,)).fetchone()[0]
        return iid, observations.receipt(self.s, iid)

    def test_nested_malformed_observation_is_audited_without_blocking_valid_publication(self):
        writer = self.writer(); reviewer = Fake('reviewer')
        pins = self.s.pinned(self.sid)
        out = self.review(writer, reviewer)
        self.assertTrue(out['result']['admission']['admitted'], out)
        self.assertTrue(out['report_markdown'])
        self.assertEqual(len(writer.payloads), 1)
        self.assertTrue(reviewer.payloads)
        iid, receipt = self.writer_receipt()
        self.assertEqual(receipt['status'], 'accepted')
        self.assertEqual(receipt['task_observation']['status'], 'malformed')
        self.assertEqual(receipt['task_observation']['mode'], 'observe_only')
        self.assertEqual(receipt['observation_fields'], {observations.FIELD: self.raw[0]['report'][observations.FIELD],
            observations.PLACEMENT: observations.REPORT_PLACEMENT})
        self.assertNotIn(observations.FIELD, receipt['proposal']['report'])
        self.assertNotIn(observations.FIELD, receipt['proposal'])
        self.assertEqual(receipt['model_response_hash'], sha(canonical(self.raw[0])))
        self.assertFalse(receipt['proposal']['complete'])
        self.assertEqual(self.s.pinned(self.sid), pins)
        self.assertEqual(out['report_review']['candidate_id'], iid)
        self.assertTrue(self.s.verify_invocations(self.sid))
        self.s.close(); self.s = InvocationLedger(self.storage / 'state.sqlite')
        self.assertTrue(self.s.verify_invocations(self.sid))
        self.assertEqual(observations.receipt(self.s, iid), receipt)

    def test_observation_does_not_bypass_normal_claim_publication_review(self):
        writer = self.writer(); reviewer = Fake('reviewer')
        reviewer.review_fault = lambda raw, _: raw['claims'].pop('overview')
        out = self.review(writer, reviewer)
        self.assertFalse(out['result']['admission']['admitted'])
        self.assertIsNone(out['report_markdown'])
        self.assertTrue(reviewer.payloads)
        _, receipt = self.writer_receipt()
        self.assertEqual(receipt['status'], 'accepted')
        self.assertEqual(receipt['task_observation']['status'], 'malformed')
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_invalid_citation_other_report_keys_and_missing_fields_still_reject(self):
        faults = [lambda raw, _: raw.update(citations=[999999]),
                  lambda raw, _: raw['report'].update(action='read outside source'),
                  lambda raw, _: raw['report'].pop('flow'),
                  lambda raw, _: raw.update(constraints=[])]
        for index, fault in enumerate(faults):
            with self.subTest(case=index):
                writer = self.writer(fault=fault); reviewer = Fake('reviewer')
                out = self.review(writer, reviewer, prompt='Explain the architecture. Case ' + str(index))
                self.assertFalse(out['result']['admission']['admitted'])
                self.assertIsNone(out['report_markdown'])
                self.assertEqual(reviewer.payloads, [])
                self.assertTrue(self.s.verify_invocations(self.sid))

    def test_ambiguous_duplicate_placement_is_not_overwritten_or_salvaged(self):
        writer = self.writer(fault=lambda raw, _: raw.update(task_observation={'judgement': 'complete'}))
        reviewer = Fake('reviewer'); out = self.review(writer, reviewer)
        self.assertFalse(out['result']['admission']['admitted'])
        self.assertIsNone(out['report_markdown'])
        self.assertEqual(reviewer.payloads, [])
        _, receipt = self.writer_receipt()
        self.assertEqual(set(receipt['observation_fields']), {observations.FIELD})
        self.assertEqual(receipt['proposal']['report'][observations.FIELD], self.raw[-1]['report'][observations.FIELD])
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_existing_top_level_representation_and_hash_replay_are_unchanged(self):
        writer = self.writer(top=True); out = self.review(writer)
        self.assertTrue(out['result']['admission']['admitted'])
        _, receipt = self.writer_receipt()
        self.assertEqual(receipt['observation_fields'], {observations.FIELD: self.raw[0][observations.FIELD]})
        self.assertNotIn(observations.PLACEMENT, receipt['observation_fields'])
        self.assertEqual(receipt['model_response_hash'], sha(canonical(self.raw[0])))
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_forged_receipt_placement_fails_reconstruction_even_after_hash_rechaining(self):
        self.review(self.writer()); iid, original = self.writer_receipt()
        self.assertTrue(self.s.verify_invocations(self.sid))
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.db.execute('UPDATE invocation_receipts SET record_json=? WHERE invocation_id=?', ('{}', iid))
        self.s.db.rollback()
        # Isolated fixture only: bypass the write trigger to test the verifier,
        # then rechain transport hashes so placement validation is the blocker.
        self.s.db.execute('DROP TRIGGER immutable_invocation_receipts_UPDATE')
        previous = 'GENESIS'
        for sequence, invocation, raw in self.s.db.execute('SELECT seq,invocation_id,record_json FROM invocation_receipts WHERE session=? ORDER BY seq', (self.sid,)).fetchall():
            receipt = json.loads(raw); receipt['previous_hash'] = previous
            if invocation == iid:
                receipt['observation_fields'][observations.PLACEMENT] = 'top.task_observation'
            raw = canonical(receipt); previous = sha('invocation-receipt-v1:' + raw)
            self.s.db.execute('UPDATE invocation_receipts SET id=?,record_json=? WHERE session=? AND seq=?', (previous, raw, self.sid, sequence))
        self.s.db.commit()
        with patch('task_observations.restore', wraps=observations.restore) as restore:
            self.assertFalse(self.s.verify_invocations(self.sid))
        self.assertTrue(any(call.args[2].get(observations.PLACEMENT) == 'top.task_observation'
                            for call in restore.call_args_list))


class PlacementSplitTests(unittest.TestCase):
    def test_only_the_known_diagnostic_at_the_known_role_is_split_without_mutation(self):
        raw = {'answer': 'Source-backed answer.', 'report': {'overview': 'An overview.',
               'other_unknown': 'must remain authoritative', observations.FIELD: ['malformed observation']}}
        before = copy.deepcopy(raw)
        clean, fields = observations.split('chat_reviewer', raw)
        self.assertEqual(raw, before)
        self.assertIn('other_unknown', clean['report'])
        self.assertEqual(observations.restore('chat_reviewer', clean, fields), raw)
        for role in ('subagent', 'coder', 'report_reviewer', 'conversation'):
            self.assertEqual(observations.split(role, raw), (raw, {}))
        for forged in (dict(fields, _host_placement='unknown'), dict(fields, extra='not allowed')):
            with self.assertRaises(InvalidUpdate):
                observations.restore('chat_reviewer', clean, forged)


if __name__ == '__main__':
    unittest.main()
