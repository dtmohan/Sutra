"""Recognize document review targets and explicitly supplied document text."""

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
    rf"(?P<target>(?:{_QUOTED_NAME}|{_PATH}|[\w][\w.-]*){_SUFFIX})",
    re.IGNORECASE,
)
_DOCUMENT_REVIEW = re.compile(
    rf"\b{_REVIEW}\s+{_QUALIFIERS}"
    r"(?:file|document|r[eé]sum[eé]|cv|cover\s+letter|draft|report|paper|proposal|pdf|docx?)\b"
    r"(?!\s+(?:api|builder|code|converter|creation|export|extraction|fixture|generation|handling|"
    r"implementation|integration|library|parser|pipeline|reader|renderer|support|writer)\b)",
    re.IGNORECASE,
)
_LOCAL_TARGET = re.compile(rf"(?<![\w:./\\])(?P<target>{_PATH}{_SUFFIX})", re.IGNORECASE)
_DOCUMENT_LINE = re.compile(r"""[^\r\n<>"`*?]+?\.docx""", re.IGNORECASE)
_NEGATED = re.compile(r"\b(?:do\s+not|don['’]t|never|without|instead\s+of)\s+$", re.IGNORECASE)

DOCUMENT_TEXT_REQUIRED = "Paste the document or the sections you want reviewed under a “Document text:” heading."

_CAREER_DOCUMENT = r"(?:r[eé]sum[eé]|cv|cover[ -]+letter)"
_REQUEST_CLAUSE = (
    r"(?:^|(?<=[.!?;\n]))\s*"
    r"(?:(?:can|could|would|will)\s+you\s+|i\s+(?:want|need|would\s+like)\s+you\s+to\s+)?"
    r"(?:(?:please|also|now)\s+)*"
)
_CAREER_REVIEW = re.compile(
    rf"{_REQUEST_CLAUSE}(?:"
    rf"{_REVIEW}\s+{_QUALIFIERS}{_CAREER_DOCUMENT}\b"
    r"(?!\.[\w-]|\s+(?:api|behavior|builder|button|checkpoint|code|command|converter|export|"
    r"extraction|feature|fixture|flag|function|generator|generation|handler|handling|implementation|"
    r"integration|library|logic|mode|module|option|parameter|parser|parsing|pipeline|reader|renderer|"
    r"state|support|variable|workflow|writer)s?\b)"
    rf"|(?:do|give\s+me|provide|run|i\s+would\s+like|i\s+want)\s+"
    rf"(?:(?:a|the|this|my|your)\s+)*{_CAREER_DOCUMENT}\s+review\b)",
    re.IGNORECASE,
)
_DOCUMENT_TEXT = re.compile(
    rf"^[ \t]*(?:document|{_CAREER_DOCUMENT})[ \t]+text:[ \t]*(?:\r?\n|$)",
    re.IGNORECASE | re.MULTILINE,
)
_DOCUMENT_REFERENCE = re.compile(rf"(?:{_PATH}|[\w][\w.-]*){_SUFFIX}", re.IGNORECASE)
_REFERENTIAL_REVIEW = re.compile(
    rf"{_REQUEST_CLAUSE}(?:{_REVIEW}|take\s+a\s+look\s+at)\s+(?:it|that|this)"
    r"(?:\s+(?:now|please|again|then))*[.!?]*\s*$",
    re.IGNORECASE,
)


def refers_to_previous_review_target(prompt: str) -> bool:
    """Match an unqualified follow-up whose object comes from the preceding turn."""
    return bool(_REFERENTIAL_REVIEW.fullmatch(prompt))


def _named_document_kind(prompt: str) -> Optional[str]:
    target = _named_document(prompt)
    return target.group("kind").upper() if target else None


def _named_document(prompt: str):
    target = next(
        (match for match in _DIRECT_TARGET.finditer(prompt) if not _NEGATED.search(prompt[:match.start()])),
        None,
    )
    if target is None and any(
        not _NEGATED.search(prompt[:match.start()]) for match in _DOCUMENT_REVIEW.finditer(prompt)
    ):
        target = _LOCAL_TARGET.search(prompt)
    return target


def document_file_target(prompt: str) -> Optional[str]:
    """Find an explicitly named DOCX in a document review request."""
    if not isinstance(prompt, str) or has_document_text(prompt):
        return None
    named = _named_document(prompt)
    targets = {named.group('target').lstrip('"`')} if named and named.group('kind').lower() == 'docx' else set()
    if _CAREER_REVIEW.search(prompt) or _DOCUMENT_REVIEW.search(prompt):
        for line in prompt.splitlines()[1:]:
            line = line.strip().strip('"`').strip()
            if _DOCUMENT_LINE.fullmatch(line):
                targets.add(line)
    return next(iter(targets)) if len(targets) == 1 else None


def document_text(prompt: str, *, leading=False) -> Optional[str]:
    """Return the supplied body under a document heading."""
    if not isinstance(prompt, str):
        return None
    prompt = prompt.strip()
    heading = _DOCUMENT_TEXT.search(prompt)
    if not heading or (leading and heading.start() != 0):
        return None
    body = prompt[heading.end():].strip()
    candidate = body.strip("`'\"")
    return body if body and not (_DOCUMENT_REFERENCE.fullmatch(candidate) or _DOCUMENT_LINE.fullmatch(candidate)) else None


def has_document_text(prompt: str) -> bool:
    return document_text(prompt) is not None


def document_review_requested(prompt: str) -> bool:
    """Recognize direct requests without treating a resume command as a subject."""
    return isinstance(prompt, str) and bool(
        _CAREER_REVIEW.search(prompt) or _DOCUMENT_TEXT.search(prompt) or _named_document_kind(prompt)
    )


def document_input_notice(prompt: str) -> Optional[str]:
    """Ask for document text when the request has no explicit text body."""
    if not isinstance(prompt, str) or has_document_text(prompt):
        return None
    kind = _named_document_kind(prompt)
    if kind:
        return f"Sutra needs the text from this {kind} file. {DOCUMENT_TEXT_REQUIRED}"
    if _CAREER_REVIEW.search(prompt) or _DOCUMENT_TEXT.search(prompt):
        return DOCUMENT_TEXT_REQUIRED
    return None
