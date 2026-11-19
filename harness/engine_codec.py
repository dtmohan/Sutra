"""Text-only model codec for Sutra-owned engine workers.

Model assets supply vocabulary and chat template. The byte tokenizer and
literal-token protection are separately vendored MIT components, not imports
from a Strata installation. No model calls, downloads or conversation storage
occur here.
"""
from __future__ import annotations

import codecs
import copy
import importlib.util
import json
from pathlib import Path
import sys


class CodecError(ValueError):
    """The model assets or generated stream do not meet the codec contract."""


class CodecDependencyError(RuntimeError):
    """The dedicated engine environment needs its declared dependencies."""


def _vendor(name):
    module_name = '_sutra_engine_' + name
    if module_name not in sys.modules:
        source = Path(__file__).resolve().parent.parent / 'vendor' / 'sutra_engine' / (name + '.py')
        spec = importlib.util.spec_from_file_location(module_name, source)
        if spec is None or spec.loader is None:
            raise CodecDependencyError('Sutra engine codec component is unavailable: ' + name)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[module_name] = module
    return sys.modules[module_name]


def _asset(path, bound):
    with path.open('rb') as stream:
        data = stream.read(bound + 1)
    if len(data) > bound:
        raise CodecError('Tokenizer asset exceeds its allocation: ' + path.name)
    return data.decode('utf-8')


def _thinking(value):
    if value is False or value in ('off', 'none'):
        return {'enable_thinking': False}, False
    if value is None or value is True or value in ('on', 'model-default'):
        return {}, True
    if value in ('low', 'medium', 'high', 'xhigh'):
        return {'reasoning_effort': 'xhigh' if value == 'high' else value}, True
    raise CodecError('Unsupported engine reasoning control')


class EngineCodec:
    def __init__(self, tokenizer_path):
        self.path = Path(tokenizer_path)
        if not self.path.is_absolute() or not self.path.is_dir():
            raise CodecError('An installed absolute tokenizer directory is required')
        try:
            from jinja2.sandbox import ImmutableSandboxedEnvironment
            bpe = _vendor('byte_bpe')
        except ImportError as error:
            raise CodecDependencyError('Install requirements-engine.txt in the Sutra engine environment') from error
        try:
            config = json.loads(_asset(self.path / 'tokenizer.json', 65536))
            vocab = json.loads(_asset(self.path / 'vocab.json', 32 * 1024**2))
            types = json.loads(_asset(self.path / 'token_type.json', 8 * 1024**2))
            merge_text = _asset(self.path / 'merges.txt', 32 * 1024**2)
            merges = merge_text.split('\n') if merge_text else []
            template = _asset(self.path / 'chat_template.jinja', 1024**2)
            if (not isinstance(config, dict) or config.get('model') != 'gpt2' or
                    not isinstance(vocab, dict) or not 256 <= len(vocab) <= 1_000_000 or
                    not isinstance(types, list) or len(types) != len(vocab) or
                    any(type(t) is not int or t < 0 for t in types)):
                raise CodecError('Unsupported tokenizer asset structure')
            tokens = [None] * len(vocab)
            for token, index in vocab.items():
                if (not isinstance(token, str) or type(index) is not int or
                        not 0 <= index < len(tokens) or tokens[index] is not None):
                    raise CodecError('Tokenizer vocabulary IDs must be unique and contiguous')
                tokens[index] = token
            if any(token is None for token in tokens):
                raise CodecError('Tokenizer vocabulary has a missing ID')
            if config.get('vocab_size', len(tokens)) != len(tokens):
                raise CodecError('Tokenizer vocabulary size differs from its metadata')
            if config.get('n_merges', len(merges)) != len(merges):
                raise CodecError('Tokenizer merge count differs from its metadata')
            self.tokenizer = bpe.Tokenizer(tokens, merges, types,
                                           config.get('pre') or 'qwen35', config.get('special_ids') or None)
            self.config = config
        except (OSError, UnicodeError, KeyError, TypeError, ValueError) as error:
            if isinstance(error, CodecError):
                raise
            raise CodecError('Could not load compatible tokenizer assets: ' + str(error)[:300]) from error

        def raise_exception(message):
            raise CodecError(str(message))

        environment = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True,
                                                     extensions=['jinja2.ext.loopcontrols'])
        environment.filters['tojson'] = lambda value, ensure_ascii=False, indent=None, separators=None, sort_keys=False: json.dumps(
            value, ensure_ascii=ensure_ascii, indent=indent, separators=separators, sort_keys=sort_keys)
        environment.globals['raise_exception'] = raise_exception
        self.template_source = template
        self.template = environment.from_string(template)
        self.literals = _vendor('literal_spans')
        self.literal_tags = self.literals.literal_tags(self.tokenizer.control_tokens)
        self.stop_token_ids = set()
        for marker in ('<|im_end|>', '<|endoftext|>'):
            ids = self.tokenizer.encode(marker, parse_special=True)
            if len(ids) == 1:
                self.stop_token_ids.add(ids[0])
        if not self.stop_token_ids:
            identifiers = config.get('special_ids') or {}
            value = identifiers.get('tokenizer.ggml.eos_token_id', identifiers.get('eos'))
            if type(value) is int and 0 <= value < len(tokens):
                self.stop_token_ids.add(value)
        if not self.stop_token_ids:
            raise CodecError('Tokenizer has no known complete end-of-turn token')
        end = self.tokenizer.encode('</think>', parse_special=True)
        self.thinking_end_token_id = end[0] if len(end) == 1 else None

    def _messages(self, messages, json_schema=None):
        if not isinstance(messages, list) or not messages or len(messages) > 1024:
            raise CodecError('Engine messages must be a nonempty bounded list')
        result = []
        for message in messages:
            if (not isinstance(message, dict) or message.get('role') not in
                    ('system', 'developer', 'user', 'assistant', 'tool') or
                    not isinstance(message.get('content'), str) or
                    set(message) - {'role', 'content', 'reasoning_content'} or
                    ('reasoning_content' in message and not isinstance(message['reasoning_content'], str))):
                raise CodecError('The direct engine codec accepts text-only messages')
            result.append(copy.deepcopy(message))
        # Match the upstream template's omission of completed empty assistant
        # examples. The final message is retained even when empty.
        result = [message for index, message in enumerate(result) if index == len(result) - 1 or
                  message['role'] != 'assistant' or message['content'].strip()]
        if json_schema is not None:
            if not isinstance(json_schema, dict):
                raise CodecError('The requested output schema must be a JSON object')
            text = json.dumps(json_schema, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
            if len(text.encode('utf-8')) > 262144:
                raise CodecError('The output schema exceeds the codec allocation')
            instruction = ('Respond with exactly one JSON object matching this JSON Schema. '
                           'Do not wrap the object in Markdown.\n' + text)
            if result[0]['role'] == 'system':
                result[0]['content'] += '\n\n' + instruction
            else:
                result.insert(0, {'role': 'system', 'content': instruction})
        return result

    def _render(self, messages, thinking=False, add_generation_prompt=True, json_schema=None):
        kwargs, _ = _thinking(thinking)
        messages = self._messages(messages, json_schema)
        # Mark and restore literal special-token text so only the chat template
        # can create control tokens. User quotations remain ordinary bytes.
        tags = self.literal_tags
        source = json.dumps(messages, ensure_ascii=False) + self.template_source
        if any(marker in source for marker in tags.values()):
            # A user's private-use character must not accidentally become a
            # structural token when protected literals are restored.
            tags, point = {}, 0xF1000
            for literal in self.literal_tags:
                while chr(point) in source:
                    point += 1
                tags[literal] = chr(point)
                point += 1
        marked, _, changed = self.literals.mark_think_literals(messages, None, tags)
        rendered = self.template.render(messages=marked, tools=None,
                                        add_generation_prompt=add_generation_prompt, **kwargs)
        if len(rendered.encode('utf-8')) > 16 * 1024**2:
            raise CodecError('Rendered engine prompt exceeds its allocation')
        return self.literals.unmark_think_literals(rendered, tags) if changed else (rendered, ())

    def render_messages(self, messages, thinking=False, add_generation_prompt=True, json_schema=None):
        return self._render(messages, thinking, add_generation_prompt, json_schema)[0]

    def encode_messages(self, messages, thinking=False, add_generation_prompt=True, json_schema=None):
        rendered, plain = self._render(messages, thinking, add_generation_prompt, json_schema)
        return self.tokenizer.encode(rendered, parse_special=True, plain=plain)

    def encode(self, text, parse_special=False):
        if not isinstance(text, str):
            raise CodecError('Tokenizer input must be text')
        return self.tokenizer.encode(text, parse_special=parse_special)

    def decode(self, ids):
        return self.tokenizer.decode(ids, errors='strict')

    def token_bytes(self, token_id):
        if type(token_id) is not int:
            raise CodecError('Engine token IDs must be integers')
        return self.tokenizer.token_bytes(token_id)

    def parser(self, thinking=False, max_reasoning_tokens=None, max_output_bytes=1024**2):
        return OutputDecoder(self, thinking, max_reasoning_tokens, max_output_bytes)


class OutputDecoder:
    """Incremental UTF-8 and leading reasoning boundaries; no answer repair.

    Reasoning and content are retained separately. Reaching an optional
    reasoning threshold is observable, never a reason to discard output or
    silently promote unfinished reasoning into an answer.
    """
    def __init__(self, codec, thinking=False, max_reasoning_tokens=None, max_output_bytes=1024**2):
        _, active = _thinking(thinking)
        if (max_reasoning_tokens is not None and
                (type(max_reasoning_tokens) is not int or max_reasoning_tokens < 0)):
            raise CodecError('Reasoning observation threshold must be a nonnegative integer')
        if type(max_output_bytes) is not int or not 1 <= max_output_bytes <= 16 * 1024**2:
            raise CodecError('Output byte allocation must be 1..16777216')
        self.codec, self.state = codec, 'thinking' if active else 'content'
        self.max_reasoning_tokens, self.max_output_bytes = max_reasoning_tokens, max_output_bytes
        self.decoder = codecs.getincrementaldecoder('utf-8')('strict')
        self.buffer, self.content, self.thinking = '', '', ''
        self.leading, self.finished, self.stopped = True, False, False
        self.output_bytes, self.generated_tokens, self.reasoning_tokens = 0, 0, 0

    def _route(self, text, final=False):
        self.buffer += text
        emitted = {'content': '', 'thinking': ''}
        if self.leading:
            stripped = self.buffer.lstrip()
            if not final and ('<think>'.startswith(stripped) or not stripped):
                return emitted
            if stripped.startswith('<think>'):
                self.buffer = stripped[len('<think>'):]
                self.state = 'thinking'
            self.leading = False
        if self.state == 'thinking':
            marker = '</think>'
            where = self.buffer.find(marker)
            if where >= 0:
                emitted['thinking'] = self.buffer[:where]
                self.buffer = self.buffer[where + len(marker):]
                self.state = 'content'
            else:
                held = 0
                if not final:
                    for size in range(1, len(marker)):
                        if self.buffer.endswith(marker[:size]):
                            held = size
                end = len(self.buffer) - held
                emitted['thinking'], self.buffer = self.buffer[:end], self.buffer[end:]
        if self.state == 'content':
            emitted['content'], self.buffer = self.buffer, ''
        self.content += emitted['content']
        self.thinking += emitted['thinking']
        return emitted

    def feed(self, token_id):
        if self.finished or self.stopped:
            raise CodecError('Engine output arrived after the turn ended')
        if type(token_id) is not int:
            raise CodecError('Engine token IDs must be integers')
        self.generated_tokens += 1
        if token_id in self.codec.stop_token_ids:
            self.stopped = True
            return {'content': '', 'thinking': ''}
        if self.state == 'thinking':
            self.reasoning_tokens += 1
        try:
            raw = self.codec.token_bytes(token_id)
            self.output_bytes += len(raw)
            if self.output_bytes > self.max_output_bytes:
                raise CodecError('Generated text exceeds its output byte allocation')
            text = self.decoder.decode(raw, final=False)
        except (UnicodeError, IndexError, KeyError) as error:
            raise CodecError('Engine emitted invalid token bytes') from error
        return self._route(text)

    def finish(self):
        if self.finished:
            return {'content': '', 'thinking': ''}
        try:
            tail = self.decoder.decode(b'', final=True)
        except UnicodeError as error:
            raise CodecError('Engine output ended inside a UTF-8 character') from error
        result = self._route(tail, final=True)
        self.finished = True
        return result

    def response(self):
        return {'content': self.content, 'thinking': self.thinking,
                'generated_tokens': self.generated_tokens,
                'reasoning_tokens_observed': self.reasoning_tokens,
                'reasoning_budget_reached': self.max_reasoning_tokens is not None and
                    self.reasoning_tokens >= self.max_reasoning_tokens,
                'reasoning_unclosed': self.state == 'thinking',
                'stopped': self.stopped, 'finished': self.finished}
