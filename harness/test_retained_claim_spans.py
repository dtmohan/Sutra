"""Evidence transfer deduplicates exact spans, never repeated text elsewhere."""
import copy
import unittest

import claim_pages
from context_manager import sha


def fixture(excerpt='def run():\n    return 7\n', start_line=10, column=0):
    return {'excerpts': {'entry.py': excerpt},
            'excerpt_locations': {'entry.py': {'start_line': start_line, 'column': column}},
            'retained_context_version': 1, 'retained_context': {'pages': []}}


def unit(text='    return 7\n', line=11, column=0, version='a' * 64, identity='read-1'):
    return {'id': identity, 'path': 'entry.py', 'line': line, 'column': column,
            'sha256': version, 'text': text}


def append(source, entry):
    source['retained_context']['pages'].append({'kind': 'retained_source_unit', 'evidence': entry})


class RetainedClaimSpanTests(unittest.TestCase):
    def test_exact_delivered_span_needs_no_duplicate_claim_review_page(self):
        source = fixture(); entry = unit(); append(source, entry)
        before = copy.deepcopy(source)
        self.assertTrue(claim_pages.covered_by_excerpt(source, entry))
        inventory = claim_pages.inventory('candidate', source)
        self.assertEqual(len(inventory), 1)
        self.assertEqual(next(iter(inventory.values()))['text'], source['excerpts']['entry.py'])
        self.assertFalse(any(row.get('kind') == 'retained_source_unit' for row in inventory.values()))
        self.assertEqual(source, before)

    def test_identical_text_at_a_different_position_remains_independent_evidence(self):
        source = fixture(); entry = unit(line=42); append(source, entry)
        self.assertFalse(claim_pages.covered_by_excerpt(source, entry))
        retained = [row for row in claim_pages.inventory('candidate', source).values()
                    if row.get('kind') == 'retained_source_unit']
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0]['start_line'], 42)
        self.assertEqual(retained[0]['text'], entry['text'])

    def test_duplicate_paid_receipts_of_the_same_file_version_and_span_are_one_review_unit(self):
        source = fixture()
        first = unit(line=42)
        append(source, first)
        append(source, dict(first, id='read-2', receipt='another-paid-receipt'))
        self.assertEqual(len(source['retained_context']['pages']), 2)
        inventory = claim_pages.inventory('candidate', source)
        retained = [row for row in inventory.values() if row.get('kind') == 'retained_source_unit']
        self.assertEqual(len(retained), 1)
        self.assertEqual(len(source['retained_context']['pages']), 2)

    def test_distinct_versions_paths_columns_and_text_are_not_merged(self):
        source = fixture()
        first = unit(line=42)
        variants = [first, dict(first, id='version', sha256='b' * 64),
                    dict(first, id='position', column=4), dict(first, id='other', path='other.py'),
                    dict(first, id='different', text='    return 8\n')]
        for entry in variants:
            append(source, entry)
        retained = [row for row in claim_pages.inventory('candidate', source).values()
                    if row.get('kind') == 'retained_source_unit']
        self.assertEqual(len(retained), len(variants))

    def test_column_offsets_and_multiline_substrings_match_only_their_real_position(self):
        source = fixture('prefix target target\nλ next line\n', column=3)
        for entry in (unit('target', line=10, column=10),
                      unit('target\nλ next', line=10, column=17),
                      unit('next', line=11, column=2)):
            with self.subTest(entry=entry):
                self.assertTrue(claim_pages.covered_by_excerpt(source, entry))
        for entry in (unit('target', line=10, column=9), unit('target', line=11, column=10),
                      unit('next', line=11, column=3)):
            with self.subTest(entry=entry):
                self.assertFalse(claim_pages.covered_by_excerpt(source, entry))

    def test_missing_positions_or_unknown_version_never_remove_evidence(self):
        source = fixture()
        source['excerpt_locations'] = {}
        self.assertFalse(claim_pages.covered_by_excerpt(source, unit()))
        source = fixture()
        source['source_versions'] = {'entry.py': 'b' * 64}
        self.assertFalse(claim_pages.covered_by_excerpt(source, unit()))
        first = unit(line=42); first.pop('sha256')
        append(source, first); append(source, dict(first, id='second-unversioned'))
        retained = [row for row in claim_pages.inventory('candidate', source).values()
                    if row.get('kind') == 'retained_source_unit']
        self.assertEqual(len(retained), 2)

    def test_historical_unmarked_inventory_and_source_hash_are_unchanged(self):
        source = fixture(); source.pop('retained_context_version'); source.pop('retained_context')
        inventory = claim_pages.inventory('candidate', source)
        self.assertEqual(len(inventory), 1)
        expected = sha(__import__('context_manager').canonical({
            key: source.get(key) for key in ('excerpts', 'excerpt_locations', 'snapshot_receipts')}))
        self.assertEqual(claim_pages.source_hash(source), expected)


if __name__ == '__main__':
    unittest.main()
