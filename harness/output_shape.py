"""Bounded syntax observations; never response text, repair or admission policy."""
import json


PROFILE = 'output-shape-v1'
PREFIX_CHARS = 512
_CATEGORIES = {'empty', 'whitespace_only', 'unobserved', 'object_open', 'array_open',
               'double_quote', 'single_quote', 'backtick', 'markup', 'bom',
               'ascii_control', 'unicode_whitespace', 'letter', 'digit', 'other'}
_MARKERS = {'none', 'json_fence', 'code_fence', 'think_open', 'think_close'}
_FIELDS = {'profile', 'observation_only', 'content_present', 'content_chars',
           'thinking_present', 'thinking_chars', 'inspected_prefix_chars', 'prefix_limit_chars',
           'prefix_truncated', 'first_nonspace_observed', 'first_nonspace_category',
           'known_prefix_marker', 'json_error_position', 'json_error_position_scope', 'semantic_assessment'}


def _position_value(value):
    if (not isinstance(value, dict) or len(value) != 3
            or set(value) != {'line', 'column', 'offset'}
            or any(type(n) is not int or not 0 <= n <= 2**53 - 1 for n in value.values())
            or value['line'] < 1 or value['column'] < 1):
        return None
    return dict(value)


def _position(error):
    if isinstance(error, json.JSONDecodeError):
        value = {'line': error.lineno, 'column': error.colno, 'offset': error.pos}
    else:
        value = getattr(error, 'json_error_position', None)
    return _position_value(value)


def validate(value):
    """Copy only a complete bounded host observation, never arbitrary fields."""
    if not isinstance(value, dict) or len(value) != len(_FIELDS) or set(value) != _FIELDS:
        raise ValueError('Invalid output-shape metadata fields')
    if (value['profile'] != PROFILE or value['observation_only'] is not True
            or value['semantic_assessment'] != 'unassessed'
            or value['json_error_position_scope'] != 'parser_input'):
        raise ValueError('Invalid output-shape profile')
    for key in ('content_chars', 'thinking_chars', 'inspected_prefix_chars', 'prefix_limit_chars'):
        if type(value[key]) is not int or not 0 <= value[key] <= 2**53 - 1:
            raise ValueError('Invalid output-shape count')
    for key in ('content_present', 'thinking_present', 'prefix_truncated', 'first_nonspace_observed'):
        if type(value[key]) is not bool:
            raise ValueError('Invalid output-shape observation flag')
    if (value['prefix_limit_chars'] != PREFIX_CHARS
            or value['inspected_prefix_chars'] != min(value['content_chars'], PREFIX_CHARS)
            or value['prefix_truncated'] != (value['content_chars'] > PREFIX_CHARS)
            or value['content_present'] != (value['content_chars'] > 0)
            or value['thinking_present'] != (value['thinking_chars'] > 0)):
        raise ValueError('Inconsistent output-shape counts')
    category, marker = value['first_nonspace_category'], value['known_prefix_marker']
    if (not isinstance(category, str) or category not in _CATEGORIES
            or not isinstance(marker, str) or marker not in _MARKERS):
        raise ValueError('Invalid output-shape category')
    absent = category in ('empty', 'whitespace_only', 'unobserved')
    if (value['first_nonspace_observed'] == absent
            or (category == 'empty') != (not value['content_present'])
            or category == 'whitespace_only' and value['prefix_truncated']
            or category == 'unobserved' and not value['prefix_truncated']
            or marker in ('json_fence', 'code_fence') and category != 'backtick'
            or marker in ('think_open', 'think_close') and category != 'markup'
            or absent and marker != 'none'):
        raise ValueError('Inconsistent output-shape category')
    position = value['json_error_position']
    if position is not None and _position_value(position) is None:
        raise ValueError('Invalid output-shape decoder position')
    result = dict(value)
    result['json_error_position'] = dict(position) if position is not None else None
    return result


def observe(content, thinking='', error=None):
    """Inspect at most 512 content characters, without retaining any of them.

    Whitespace here is JSON's space/tab/CR/LF. Other Unicode whitespace is a
    distinct category because a JSON parser need not accept it. A marker or a
    nonempty thought stream does not establish an unclosed thought or meaning.
    Full text lengths use O(1) string lengths; only the prefix is inspected.
    """
    if not isinstance(content, str) or not isinstance(thinking, str):
        raise ValueError('Output shape requires separated text streams')
    prefix = content[:PREFIX_CHARS]
    visible = prefix.lstrip(' \t\r\n')
    marker = 'none'
    if visible.startswith('```json') and (len(visible) == 7 or visible[7:8] in (' ', '\t', '\r', '\n')):
        marker = 'json_fence'
    elif visible.startswith('```'):
        marker = 'code_fence'
    elif visible.startswith('<think>'):
        marker = 'think_open'
    elif visible.startswith('</think>'):
        marker = 'think_close'
    first_observed = bool(visible)
    if not content:
        category = 'empty'
    elif not visible:
        category = 'unobserved' if len(content) > PREFIX_CHARS else 'whitespace_only'
    else:
        char = visible[0]
        category = {'{': 'object_open', '[': 'array_open', '"': 'double_quote',
                    "'": 'single_quote', '`': 'backtick', '<': 'markup', '\ufeff': 'bom'}.get(char)
        if category is None:
            category = ('ascii_control' if ord(char) < 32 or ord(char) == 127 else
                        'unicode_whitespace' if char.isspace() else
                        'letter' if char.isalpha() else 'digit' if char.isdigit() else 'other')
    return {'profile': PROFILE, 'observation_only': True,
            'content_present': bool(content), 'content_chars': len(content),
            'thinking_present': bool(thinking), 'thinking_chars': len(thinking),
            'inspected_prefix_chars': len(prefix), 'prefix_limit_chars': PREFIX_CHARS,
            'prefix_truncated': len(content) > PREFIX_CHARS,
            'first_nonspace_observed': first_observed, 'first_nonspace_category': category,
            'known_prefix_marker': marker, 'json_error_position': _position(error),
            'json_error_position_scope': 'parser_input',
            'semantic_assessment': 'unassessed'}
