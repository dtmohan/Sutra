import unittest

from context_manager import InvalidUpdate
from protocol import obj
import review_cases
from invocation_roles import validate_shape


class RequestedCasesTests(unittest.TestCase):
    def test_exact_tuples_keep_followups_and_decimal_spelling(self):
        cases = review_cases.requested(
            'Use workspace source. Give the exact check for Queue.offer pairs '
            '(-0.02, 0), (0.30, 0), and (0.04, -1). Can the wrapper reject a negative value? '
            'For Queue.window(60, 60), can both checks apply? Give the calculation. '
            'State what this establishes about upstream input quality. Cite source. Read only.')
        self.assertEqual(cases['instruction'], 'Give the exact check for Queue.offer pairs')
        self.assertEqual(list(cases['items'].values()), [
            '(-0.02, 0)', '(0.30, 0)', '(0.04, -1)', 'Can the wrapper reject a negative value?',
            'For Queue.window(60, 60), can both checks apply? Give the calculation.',
            'State what this establishes about upstream input quality.',
        ])
        self.assertIsNone(review_cases.requested('Explain Queue.offer(60, 60) from the workspace source.'))

    def test_numbered_cases_are_required_and_rendered_in_user_order(self):
        cases = review_cases.requested('Explain each outcome.\n1. The file exists\n2. The file is missing')
        fmt = review_cases.add_schema(obj({'answer': {'type': 'string'}}), cases)
        accepted = {'answer': 'Outcomes', 'case_answers': {'1': 'Read it.', '2': 'Evidence is unavailable.'}}
        validate_shape(accepted, fmt)
        review_cases.check(cases, accepted)
        with self.assertRaises(InvalidUpdate):
            validate_shape({'answer': 'Outcomes', 'case_answers': {'1': 'Read it.'}}, fmt)
        with self.assertRaisesRegex(InvalidUpdate, 'every numbered case'):
            review_cases.check(cases, {'case_answers': {'1': 'Read it.', '2': ' '}})
        self.assertIn('2. **The file is missing** — Evidence is unavailable.', review_cases.markdown(cases, accepted))

    def test_source_projection_quotes_literal_defaults_from_selected_files(self):
        text = ('class Rate:\n    budget: float = 0.3\n'
                '    def __init__(self, k=0.15):\n        self.k = k\n'
                '    def evaluate(self, x):\n        return x <= self.k\n')
        sources = {'rate.py': text, 'other.py': text}
        excerpts = {'rate.py': '    def evaluate(self, x):\n        return x <= self.k\n'}
        self.assertEqual(review_cases.source_details('Check Rate.evaluate.', sources, excerpts), {
            'rate.py': [
                {'owner': 'Rate', 'line': 2, 'text': '    budget: float = 0.3\n'},
                {'owner': 'Rate', 'line': 3, 'text': '    def __init__(self, k=0.15):\n        self.k = k\n'}
            ]})
        self.assertEqual(review_cases.source_details('Check another class.', sources, excerpts), {})
