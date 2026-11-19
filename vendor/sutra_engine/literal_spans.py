# Adapted from Strata serve/frontend.py, Copyright (c) 2026 Niko1221 and contributors.
# MIT license, see LICENSE. Sutra retains only pack-based byte-level tokenization.
from __future__ import annotations
import re

THINK_TAGS = {"<think>": "\U000F0E01", "</think>": "\U000F0E02"}

VISION_TAGS = {"<|vision_start|>": "\U000F0E03", "<|image_pad|>": "\U000F0E04", "<|vision_end|>": "\U000F0E05",
               "<|video_pad|>": "\U000F0E06"}

TOOL_LITERAL_TAGS = {"</tool_call>": "\U000F0E07", "<tool_call>": "\U000F0E08", "<tool_response>": "\U000F0E09",
                     "</tool_response>": "\U000F0E0A"}

LITERAL_TAGS = {**THINK_TAGS, **VISION_TAGS, **TOOL_LITERAL_TAGS}

THINK_MARKS = {v: k for k, v in LITERAL_TAGS.items()}

CONTROL_MARK0 = 0xF0E10

def literal_tags(controls) -> dict[str, str]:
    """LITERAL_TAGS plus a mark for each control token's text (`controls`: the tokenizer's CONTROL literals,
    <|im_start|>, <|im_end|>, <|endoftext|>, ...).  The rendered prompt is encoded with those literals parsed, so one
    written inside a message - a file an agent reads, a pasted chat template - opened or ended a turn there.  Longest
    first, as the tokenizer matches them: a literal inside a longer one is not marked before it."""
    tags = {**LITERAL_TAGS, **{c: chr(CONTROL_MARK0 + k) for k, c in enumerate(c for c in controls
                                                                                if c not in LITERAL_TAGS)}}
    return dict(sorted(tags.items(), key=lambda t: -len(t[0])))

def _mark(text: str, tags: dict[str, str] = LITERAL_TAGS) -> str:
    for tag, mark in tags.items():
        text = text.replace(tag, mark)
    return text

def _mark_deep(v, tags: dict[str, str] = LITERAL_TAGS):
    if isinstance(v, str):
        return _mark(v, tags)
    if isinstance(v, dict):
        return {k: _mark_deep(x, tags) for k, x in v.items()}
    if isinstance(v, list):
        return [_mark_deep(x, tags) for x in v]
    return v

def _has_tag(v, tags: dict[str, str] = LITERAL_TAGS) -> bool:
    if isinstance(v, str):
        return any(tag in v for tag in tags)
    if isinstance(v, dict):
        return any(_has_tag(x, tags) for x in v.values())
    if isinstance(v, list):
        return any(_has_tag(x, tags) for x in v)
    return False

_TOOL_RESULT_OPEN, _TOOL_RESULT_CLOSE = "<tool_response>", "</tool_response>"

def _keep_tool_result_wrap(content, tags: dict[str, str]):
    """A user message whose whole text is <tool_response>...</tool_response> (a client that formats a tool result the way
    the template does for a tool message) keeps that outer pair as the markers - the template tells such a turn from a
    real question by exactly that pair - and only the text inside is marked.  None: not such a message."""
    o, c = _TOOL_RESULT_OPEN, _TOOL_RESULT_CLOSE

    def wrap(text: str, first: bool, last: bool) -> str:
        i = text.index(o) + len(o) if first else 0
        j = text.rindex(c) if last else len(text)
        head = _mark(text[:i - len(o)], tags) + o if first else ""
        tail = c + _mark(text[j + len(c):], tags) if last else ""
        return head + _mark(text[i:j], tags) + tail

    if isinstance(content, str):
        body = content.strip()
        if len(body) >= len(o) + len(c) and body.startswith(o) and body.endswith(c):
            return wrap(content, True, True)
        return None
    if not (isinstance(content, list) and content and all(isinstance(x, dict) and "text" in x for x in content)):
        return None
    texts = [x["text"] for x in content]
    if not all(isinstance(t, str) for t in texts):
        return None
    body = "".join(texts).strip()
    if not (len(body) >= len(o) + len(c) and body.startswith(o) and body.endswith(c)):
        return None
    if not (texts[0].lstrip().startswith(o) and texts[-1].rstrip().endswith(c)):
        return None          # the pair is split across parts: mark it all, as before
    if len(content) == 1:
        return [{**content[0], "text": wrap(texts[0], True, True)}]
    return ([{**content[0], "text": wrap(texts[0], True, False)}] +
            [_mark_deep(x, tags) for x in content[1:-1]] +
            [{**content[-1], "text": wrap(texts[-1], False, True)}])

def mark_think_literals(messages: list[dict], tools: list[dict] | None, tags: dict[str, str] = LITERAL_TAGS):
    """#537: (messages, tools) with every literal <think> / </think> (#554: and vision marker) in their text swapped for
    LITERAL_TAGS' marks (or `tags`': literal_tags() adds the control tokens' texts), and
    whether there was one (None: no change, the same objects back - a prompt without them renders as it always did).
    An assistant message whose content opens with a whole <think>...</think> block (clients that send the reasoning
    inline) keeps that one block as the model's markers, as before."""
    if not _has_tag(messages, tags) and not _has_tag(tools, tags):
        return messages, tools, False
    out = []
    own = {t: v for t, v in tags.items() if t not in TOOL_LITERAL_TAGS}     # what an assistant message keeps as written
    for m in messages:
        m = dict(m)
        content = m.get("content")
        mine = own if m.get("role") == "assistant" else tags
        for k, v in m.items():
            if k != "role":
                m[k] = _mark_deep(v, mine)
        if m.get("role") == "user":
            kept = _keep_tool_result_wrap(content, tags)
            if kept is not None:
                m["content"] = kept
        if m.get("role") == "assistant" and isinstance(content, str) and content.lstrip().startswith("<think>") \
                and "</think>" in content:
            i, j = content.index("<think>") + len("<think>"), content.index("</think>")
            m["content"] = (content[:i] + _mark(content[i:j], own) + "</think>" +
                            _mark(content[j + len("</think>"):], own))
        out.append(m)
    return out, _mark_deep(tools, tags), True

_THINK_MARK_RE = re.compile("|".join(THINK_MARKS))

def unmark_think_literals(prompt: str, tags: dict[str, str] = LITERAL_TAGS) -> tuple[str, list[tuple[int, int]]]:
    """The rendered prompt with THINK_TAGS' marks turned back into the tags' text, and the (start, end) spans of those
    tags in it: the server encodes them as ordinary text (the tokenizer's encode_plain_spans)."""
    marks = THINK_MARKS if tags is LITERAL_TAGS else {v: k for k, v in tags.items()}
    mark_re = _THINK_MARK_RE if tags is LITERAL_TAGS else re.compile("|".join(map(re.escape, marks)))
    out, spans, pos, n = [], [], 0, 0
    for m in mark_re.finditer(prompt):
        out.append(prompt[pos:m.start()])
        n += m.start() - pos
        tag = marks[m.group(0)]
        out.append(tag)
        spans.append((n, n + len(tag)))
        n += len(tag)
        pos = m.end()
    out.append(prompt[pos:])
    return "".join(out), spans
