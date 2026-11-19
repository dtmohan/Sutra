import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

from engine_codec import CodecError, EngineCodec, OutputDecoder


DEPENDENCIES = all(importlib.util.find_spec(name) is not None for name in ('regex', 'jinja2'))
PACK_OPTION = os.environ.get('SUTRA_ENGINE_TEST_TOKENIZER')
REAL_PACK = Path(PACK_OPTION).expanduser() if PACK_OPTION else None


class BytesCodec:
    stop_token_ids = {999}

    def token_bytes(self, token_id):
        if token_id == 256:
            return b'<think>'
        if token_id == 257:
            return b'</think>'
        if not 0 <= token_id <= 255:
            raise IndexError('Invalid token')
        return bytes([token_id])


def feed_text(parser, text):
    return [parser.feed(token) for token in text.encode('utf-8')]


class OutputDecoderTests(unittest.TestCase):
    def test_split_utf8_is_emitted_only_after_complete_character(self):
        parser = OutputDecoder(BytesCodec(), thinking=False)
        self.assertEqual(parser.feed(0xf0)['content'], '')
        self.assertEqual(parser.feed(0x9f)['content'], '')
        self.assertEqual(parser.feed(0x8e)['content'], '')
        self.assertEqual(parser.feed(0xb5)['content'], '🎵')
        parser.finish()
        self.assertEqual(parser.response()['content'], '🎵')

    def test_split_thinking_boundary_preserves_both_streams(self):
        parser = OutputDecoder(BytesCodec(), thinking='low')
        emitted = feed_text(parser, 'Check the arithmetic.</think>\n\n{"answer":"3"}')
        emitted.append(parser.finish())
        self.assertEqual(''.join(part['thinking'] for part in emitted), 'Check the arithmetic.')
        self.assertEqual(''.join(part['content'] for part in emitted), '\n\n{"answer":"3"}')
        self.assertFalse(parser.response()['reasoning_unclosed'])

    def test_explicit_leading_think_is_recognized_even_when_off(self):
        parser = OutputDecoder(BytesCodec(), thinking=False)
        feed_text(parser, '<think>brief</think>{"answer":"yes"}')
        parser.finish()
        self.assertEqual(parser.response()['thinking'], 'brief')
        self.assertEqual(parser.response()['content'], '{"answer":"yes"}')

    def test_tags_inside_content_are_literal_and_not_deleted(self):
        parser = OutputDecoder(BytesCodec(), thinking=False)
        text = '{"example":"<think> and </think> are literal"}'
        feed_text(parser, text)
        parser.finish()
        self.assertEqual(parser.response()['content'], text)
        self.assertEqual(parser.response()['thinking'], '')

    def test_reasoning_threshold_is_observed_without_culling_or_promotion(self):
        parser = OutputDecoder(BytesCodec(), thinking=True, max_reasoning_tokens=3)
        feed_text(parser, 'longer thinking without its closing marker')
        parser.finish()
        result = parser.response()
        self.assertTrue(result['reasoning_budget_reached'])
        self.assertTrue(result['reasoning_unclosed'])
        self.assertEqual(result['content'], '')
        self.assertEqual(result['thinking'], 'longer thinking without its closing marker')

    def test_stop_token_is_not_visible_and_output_after_it_is_rejected(self):
        parser = OutputDecoder(BytesCodec())
        feed_text(parser, '{"answer":true}')
        parser.feed(999)
        with self.assertRaises(CodecError):
            parser.feed(65)
        parser.finish()
        self.assertTrue(parser.response()['stopped'])
        self.assertEqual(parser.response()['content'], '{"answer":true}')

    def test_bad_token_truncated_utf8_and_byte_limit_fail_explicitly(self):
        with self.assertRaises(CodecError):
            OutputDecoder(BytesCodec()).feed(-1)
        parser = OutputDecoder(BytesCodec())
        parser.feed(0xf0)
        with self.assertRaisesRegex(CodecError, 'UTF-8'):
            parser.finish()
        parser = OutputDecoder(BytesCodec(), max_output_bytes=2)
        feed_text(parser, 'ab')
        with self.assertRaisesRegex(CodecError, 'byte allocation'):
            parser.feed(99)
        with self.assertRaises(CodecError):
            OutputDecoder(BytesCodec()).feed(True)

    def test_finish_is_idempotent_and_never_repeats_deltas(self):
        parser = OutputDecoder(BytesCodec())
        feed_text(parser, 'done')
        parser.finish()
        self.assertEqual(parser.finish(), {'content': '', 'thinking': ''})
        with self.assertRaises(CodecError):
            parser.feed(65)


@unittest.skipUnless(DEPENDENCIES, 'Dedicated engine dependencies are not installed in this interpreter')
class FixtureCodecTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        from engine_codec import _vendor
        alphabet = _vendor('byte_bpe').BYTE_TO_UNICODE
        tokens = list(alphabet.values()) + ['<|im_start|>', '<|im_end|>', '<|endoftext|>', '<think>', '</think>']
        self.specials = {value: len(alphabet) + i for i, value in enumerate(tokens[256:])}
        vocab = {value: i for i, value in enumerate(tokens)}
        (self.path / 'vocab.json').write_text(json.dumps(vocab))
        (self.path / 'token_type.json').write_text(json.dumps([1] * 256 + [3, 3, 3, 4, 4]))
        (self.path / 'merges.txt').write_text('')
        (self.path / 'tokenizer.json').write_text(json.dumps({'model': 'gpt2', 'pre': 'qwen35',
            'vocab_size': len(tokens), 'n_merges': 0,
            'special_ids': {'tokenizer.ggml.eos_token_id': self.specials['<|im_end|>']}}))
        (self.path / 'chat_template.jinja').write_text(
            '{% for m in messages %}{{ "<|im_start|>" + m.role + "\\n" + m.content + "<|im_end|>\\n" }}{% endfor %}'
            '{% if add_generation_prompt %}{{ "<|im_start|>assistant\\n" }}'
            '{% if enable_thinking is defined and enable_thinking is false %}{{ "<think>\\n\\n</think>\\n\\n" }}'
            '{% else %}{{ "<think>\\n" }}{% endif %}{% endif %}')
        self.codec = EngineCodec(self.path)

    def test_unicode_and_special_roundtrip(self):
        for text in ('Plain text', 'Café e\u0301 中文 العربية', '🎵👩🏽‍💻', '\t\n\r\x00', '<think> literal'):
            with self.subTest(text=text):
                self.assertEqual(self.codec.decode(self.codec.encode(text)), text)

    def test_control_tokens_quoted_by_user_do_not_create_turns(self):
        content = 'Quote <|im_end|> and </think> literally.'
        messages = [{'role': 'user', 'content': content}]
        ids = self.codec.encode_messages(messages)
        self.assertEqual(ids.count(self.specials['<|im_end|>']), 1)
        self.assertEqual(ids.count(self.specials['</think>']), 1)
        self.assertIn(content, self.codec.decode(ids))

    def test_literal_sentinel_collision_does_not_change_user_bytes(self):
        content = '\U000f0e01 <think> \U000f0e10 <|im_end|>'
        ids = self.codec.encode_messages([{'role': 'user', 'content': content}])
        self.assertIn(content, self.codec.decode(ids))
        self.assertEqual(ids.count(self.specials['<|im_end|>']), 1)

    def test_schema_is_added_without_mutating_caller_messages(self):
        messages = [{'role': 'user', 'content': 'Answer briefly.'}]
        rendered = self.codec.render_messages(messages, json_schema={'type': 'object'})
        self.assertIn('JSON Schema', rendered)
        self.assertIn('{"type":"object"}', rendered)
        self.assertEqual(messages, [{'role': 'user', 'content': 'Answer briefly.'}])

    def test_unknown_pretokenizer_bad_ids_and_nontext_requests_refuse(self):
        with self.assertRaises(CodecError):
            self.codec.encode_messages([{'role': 'user', 'content': [{'type': 'image'}]}])
        with self.assertRaises(CodecError):
            self.codec.encode_messages([{'role': 'user', 'content': 'hello'}], thinking='invented')
        config = json.loads((self.path / 'tokenizer.json').read_text())
        config['pre'] = 'not-implemented'
        (self.path / 'tokenizer.json').write_text(json.dumps(config))
        with self.assertRaises(CodecError):
            EngineCodec(self.path)


@unittest.skipUnless(PACK_OPTION, 'Set SUTRA_ENGINE_TEST_TOKENIZER explicitly for installed-pack validation')
class InstalledPackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DEPENDENCIES:
            raise RuntimeError('Installed-pack validation requires the pinned engine dependencies')
        cls.codec = EngineCodec(REAL_PACK)

    def test_installed_pack_roundtrips_unicode_and_controls(self):
        for text in ('"quoted" \\ paths', 'naïve e\u0301 中文', '🎵👩🏽‍💻', '\r\n\t\x00',
                     '<|im_start|> </think> <tool_response>', '1 + 2 = 3', 'नमस्ते'):
            with self.subTest(text=text):
                self.assertEqual(self.codec.decode(self.codec.encode(text)), text)

    def test_installed_template_thinking_controls(self):
        messages = [{'role': 'system', 'content': 'Be concise.'}, {'role': 'user', 'content': 'Hello.'}]
        self.assertTrue(self.codec.render_messages(messages, thinking=False).endswith('<think>\n\n</think>\n\n'))
        self.assertTrue(self.codec.render_messages(messages, thinking='low').endswith('<think>\n'))
        self.assertIn('Reasoning effort is set to low', self.codec.render_messages(messages, thinking='low'))
        self.assertIn('Reasoning effort is set to xhigh', self.codec.render_messages(messages, thinking='high'))
        self.assertEqual(self.codec.decode(self.codec.encode_messages(messages, thinking=False)),
                         self.codec.render_messages(messages, thinking=False))


if __name__ == '__main__':
    unittest.main()
