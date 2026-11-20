"""A syntax failure can be diagnosed without preserving generated prefixes."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from output_shape import PREFIX_CHARS, observe, validate
from storage_policy import write_trace
import test_engine_model as native_fixture
import test_strata_model as strata_fixture


class OutputShapeTests(unittest.TestCase):
    def test_classifies_prose_fences_and_think_markers_without_retaining_them(self):
        examples = [('Private prose with a confidential source.', 'letter', 'none'),
                    (' \n```json\n{"private":1}\n```', 'backtick', 'json_fence'),
                    ('```python\nsecret()\n```', 'backtick', 'code_fence'),
                    ('<think>private thought', 'markup', 'think_open'),
                    ('</think>{}', 'markup', 'think_close'),
                    ('{"private":1}', 'object_open', 'none'),
                    ('["private"]', 'array_open', 'none')]
        for content, category, marker in examples:
            with self.subTest(category=category, marker=marker):
                value = observe(content, 'private thought')
                self.assertEqual(validate(value), value)
                self.assertEqual(value['first_nonspace_category'], category)
                self.assertEqual(value['known_prefix_marker'], marker)
                self.assertEqual(value['thinking_chars'], len('private thought'))
                self.assertNotIn('private', json.dumps(value))
                self.assertNotIn('Private', json.dumps(value))
                self.assertTrue(value['observation_only'])
                self.assertEqual(value['semantic_assessment'], 'unassessed')
                self.assertNotIn('decision', value)
                self.assertNotIn('repair', value)

    def test_empty_content_and_nonempty_reasoning_do_not_prove_an_unclosed_thought(self):
        value = observe('', 'A private deliberation without an answer.')
        self.assertFalse(value['content_present'])
        self.assertTrue(value['thinking_present'])
        self.assertEqual(value['first_nonspace_category'], 'empty')
        self.assertFalse(value['first_nonspace_observed'])
        self.assertNotIn('reasoning_unclosed', value)

    def test_prefix_work_is_bounded_and_hidden_tail_is_explicitly_unobserved(self):
        content = ' ' * (PREFIX_CHARS + 20) + '{"private": true}'
        value = observe(content)
        self.assertEqual(value['content_chars'], len(content))
        self.assertEqual(value['inspected_prefix_chars'], PREFIX_CHARS)
        self.assertTrue(value['prefix_truncated'])
        self.assertFalse(value['first_nonspace_observed'])
        self.assertEqual(value['first_nonspace_category'], 'unobserved')
        self.assertEqual(value['known_prefix_marker'], 'none')

    def test_json_error_location_comes_from_decoder_not_generated_diagnostics(self):
        content = '{"private": "x"\n"second": 2}'
        try:
            json.loads(content)
        except json.JSONDecodeError as error:
            value = observe(content, error=error)
            self.assertEqual(value['json_error_position'],
                {'line': error.lineno, 'column': error.colno, 'offset': error.pos})
        self.assertNotIn('private', json.dumps(value))
        self.assertIsNone(observe(content, error=ValueError('private failure'))['json_error_position'])

    def test_wrapped_decoder_position_is_copied_and_untrusted_attributes_are_rejected(self):
        position = {'line': 2, 'column': 7, 'offset': 13}
        error = SimpleNamespace(json_error_position=position)
        value = observe('not JSON', error=error)
        self.assertEqual(value['json_error_position'], position)
        self.assertIsNot(value['json_error_position'], position)
        for invalid in ({'line': True, 'column': 1, 'offset': 0},
                        {'line': 0, 'column': 1, 'offset': 0},
                        {'line': 1, 'column': 1, 'offset': 10**10000},
                        dict(position, raw='private source'), 'private location'):
            with self.subTest(kind=type(invalid).__name__):
                self.assertIsNone(observe('x', error=SimpleNamespace(json_error_position=invalid))['json_error_position'])

    def test_unicode_whitespace_bom_controls_and_surrogates_do_not_leak(self):
        for content, category in [('\u00a0{}', 'unicode_whitespace'), ('\ufeff{}', 'bom'),
                                  ('\x00private', 'ascii_control'), ('\ud800private', 'other'),
                                  (' \t\r\n', 'whitespace_only')]:
            with self.subTest(category=category):
                value = observe(content)
                self.assertEqual(value['first_nonspace_category'], category)
                self.assertNotIn('private', json.dumps(value))

    def test_observation_is_deterministic_and_rejects_nontext_without_action(self):
        content = '{"value": 1}'; before = copy.deepcopy(content)
        self.assertEqual(observe(content), observe(content))
        self.assertEqual(content, before)
        for content, thinking in ((None, ''), ({'private': 1}, ''), ('{}', ['private'])):
            with self.subTest(content_type=type(content).__name__), self.assertRaises(ValueError):
                observe(content, thinking)

    def test_strict_validator_rejects_text_extra_fields_and_inconsistent_counts(self):
        original = observe('private prose', 'private thought')
        for change in ({'raw_prefix': 'private source'}, {'content_chars': True},
                       {'thinking_chars': 10**10000}, {'prefix_limit_chars': 4096},
                       {'first_nonspace_category': 'private generated text'},
                       {'known_prefix_marker': 'think_open'}, {'observation_only': False},
                       {'json_error_position': {'line': 1, 'column': 1, 'offset': 0, 'raw': 'private'}}):
            with self.subTest(fields=list(change)), self.assertRaises(ValueError):
                validate(dict(original, **change))
        for content in ('', ' ' * 20, ' ' * (PREFIX_CHARS + 10), '\ufeff{}', '<think>private'):
            value = observe(content)
            self.assertEqual(validate(value), value)
        copied = validate(original)
        self.assertIsNot(copied, original)

    def test_compact_trace_retains_only_valid_shape_without_generated_bodies(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / 'calls'
            shape = observe('private generated text', 'private thought')
            record = {'provider': 'strata', 'request': {}, 'ok': False, 'error': 'invalid JSON',
                      'response': {'message': {'content': 'private generated text', 'thinking': 'private thought'}},
                      'output_shape': shape}
            write_trace(folder, 'good', record)
            value = json.loads((folder / 'good.json').read_text())
            self.assertEqual(value['output_shape'], shape)
            self.assertNotIn('private', json.dumps(value))
            record['output_shape'] = dict(shape, raw='private output')
            write_trace(folder, 'bad', record)
            value = json.loads((folder / 'bad.json').read_text())
            self.assertNotIn('output_shape', value)
            self.assertNotIn('private', json.dumps(value))


class StrataOutputShapeTests(unittest.TestCase):
    setUp = strata_fixture.StrataTests.setUp
    ask = strata_fixture.StrataTests.ask

    def test_actual_host_json_failure_records_shape_and_preserves_measured_repair(self):
        from review_budget import usage
        self.server.status['structured_output'] = dict(strata_fixture.PROMPT_VALIDATION)
        self.server.frames = [strata_fixture.text_frames('Private unstructured response'), strata_fixture.frames()]
        result = self.ask()
        self.assertTrue(result['result']['admission']['admitted'])
        self.assertEqual(len(self.server.posts), 2)
        self.assertEqual(usage(self.ledger, result['session'])['used']['tokens'], 308)
        traces = [json.loads(path.read_text()) for path in (self.root / 'calls').glob('*.json')]
        shapes = [row['output_shape'] for row in traces if 'output_shape' in row]
        self.assertEqual(len(shapes), 1)
        self.assertEqual(shapes[0]['first_nonspace_category'], 'letter')
        self.assertEqual(shapes[0]['json_error_position'], {'line': 1, 'column': 1, 'offset': 0})
        self.assertNotIn('Private unstructured response', json.dumps(traces))
        self.assertNotIn('Private unstructured response', json.dumps(self.server.posts[1]))


class NativeOutputShapeTests(unittest.TestCase):
    setUp = native_fixture.DirectEngineModelTests.setUp
    activate = native_fixture.DirectEngineModelTests.activate
    generate = native_fixture.DirectEngineModelTests.generate
    ask = native_fixture.DirectEngineModelTests.ask

    def invalid_reply(self):
        original = self.client.generate
        def invalid(*args, **kwargs):
            value = original(*args, **kwargs)
            value['content'] = '```json\n{"private": "secret"}\n```'
            value['thinking'] = 'private deliberation'
            return value
        self.client.generate = invalid

    def test_native_failure_retains_shape_but_does_not_introduce_fence_salvage(self):
        from review_budget import usage
        self.invalid_reply(); result = self.ask()
        self.assertFalse(result['result']['admission']['admitted'])
        self.assertEqual(usage(self.ledger, result['session'])['used']['calls'], 3)
        traces = [json.loads(path.read_text()) for path in (self.root / 'calls').glob('*.json')]
        self.assertEqual(len(traces), 3)
        for row in traces:
            value = row['output_shape']; self.assertEqual(validate(value), value)
            self.assertEqual(value['known_prefix_marker'], 'json_fence')
            self.assertEqual(value['first_nonspace_category'], 'backtick')
            self.assertEqual(value['json_error_position'], {'line': 1, 'column': 1, 'offset': 0})
            self.assertEqual(value['thinking_chars'], len('private deliberation'))
        self.assertNotIn('private', json.dumps(traces))
        self.assertNotIn('secret', json.dumps(traces))

    def test_diagnostic_fault_preserves_existing_parser_failure_and_charged_attempts(self):
        from review_budget import usage
        self.invalid_reply()
        with patch('output_shape.observe', side_effect=RuntimeError('observer unavailable')):
            result = self.ask()
        self.assertFalse(result['result']['admission']['admitted'])
        self.assertEqual(usage(self.ledger, result['session'])['used']['calls'], 3)
        traces = [json.loads(path.read_text()) for path in (self.root / 'calls').glob('*.json')]
        self.assertTrue(all('output_shape' not in row for row in traces))
        self.assertTrue(all('Sutra engine reply is not one JSON value' in row['error'] for row in traces))


if __name__ == '__main__':
    unittest.main()
