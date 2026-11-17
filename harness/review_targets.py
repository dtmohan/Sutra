"""Recognize explicit document review targets without opening files."""

import re
from typing import Optional


_REVIEW = (
    r"(?:review|read|inspect|examine|analy[sz]e|summari[sz]e|proofread|critique|"
    r"assess|evaluate|look\s+at|comment\s+on)"
)
_QUALIFIERS = r"(?:(?:the|this|my|our|your|attached|following)\s+)*"
_SUFFIX = r'''\.(?P<kind>docx?|pdf)(?=$|[\s"`',;:!?()\[\]<>]|\.(?=\s|$))'''
_PATH_PREFIX = r"(?:file://(?:localhost)?/|[a-z]:[/\\]|\\\\|~[/\\]|\.\.?[/\\]|/|[\w.-]+[/\\])"
# An earlier filename followed by prose must not become part of a later target.
_PATH = rf'''{_PATH_PREFIX}(?:(?!\.\w+\s)[^\r\n<>"`*?])+?'''
_QUOTED_NAME = r'''["`](?!(?:https?|ftp)://)[\w~./\\][^\r\n<>"`*?]*?'''

_DIRECT_TARGET = re.compile(
    rf"\b{_REVIEW}\s+{_QUALIFIERS}(?:(?:file|document|from)\s+)*"
    rf"(?:{_QUOTED_NAME}|{_PATH}|[\w][\w.-]*){_SUFFIX}",
    re.IGNORECASE,
)
_DOCUMENT_REVIEW = re.compile(
    rf"\b{_REVIEW}\s+{_QUALIFIERS}"
    r"(?:file|document|r[eé]sum[eé]|cv|cover\s+letter|draft|report|paper|proposal|pdf|docx?)\b"
    r"(?!\s+(?:api|builder|code|converter|creation|export|extraction|fixture|generation|handling|"
    r"implementation|integration|library|parser|pipeline|reader|renderer|support|writer)\b)",
    re.IGNORECASE,
)
_LOCAL_TARGET = re.compile(rf"(?<![\w:./\\]){_PATH}{_SUFFIX}", re.IGNORECASE)
_NEGATED = re.compile(r"\b(?:do\s+not|don['’]t|never|without|instead\s+of)\s+$", re.IGNORECASE)


def document_input_notice(prompt: str) -> Optional[str]:
    """Return a notice for a named unsupported document requested for review."""
    if not isinstance(prompt, str):
        return None
    target = next(
        (match for match in _DIRECT_TARGET.finditer(prompt) if not _NEGATED.search(prompt[:match.start()])),
        None,
    )
    if target is None and any(
        not _NEGATED.search(prompt[:match.start()]) for match in _DOCUMENT_REVIEW.finditer(prompt)
    ):
        target = _LOCAL_TARGET.search(prompt)
    if target is None:
        return None
    kind = target.group("kind").upper()
    return f"Sutra needs the text from this {kind} file. Paste it here to continue the review."
