"""Host measurements for admitted handoff targets and missing evidence."""

import json
import re

from context_manager import InvalidUpdate, sha
from karma_adapter import KarmaAdapter
from review_budget import usage


_DIRECT_REVIEW = re.compile(
    r"(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?"
    r"(?:review|inspect|examine|analy[sz]e)\s+"
    r"(?:the|this|our|my)\s+(?:(?:current|open|whole|entire)\s+)?"
    r"(?:workspace(?:\s+project)?|code[ -]?base|repository|repo)\b",
    re.IGNORECASE,
)
_EXPLANATION = re.compile(
    r",?\s+and\s+(?:give|provide|offer)\s+(?:(?:me|us)\s+)?(?:a|an|the)\s+"
    r"(?:(?:detailed|high[ -]level|brief)\s+)?(?:overview|analysis|explanation|summary)\b",
    re.IGNORECASE,
)
_OTHER_WORK_OR_TARGET = re.compile(
    r"\b(?:edit|modif|chang|fix|patch|refactor|rewrit|implement|run|execut|install|deploy|"
    r"commit|push|fetch|download|delet|creat|writ)[a-z]*\b"
    r"|\b(?:attached|pasted|supplied|below|above|example|external|github|another|other|"
    r"only|solely|instead|previous|if|unless|before|after|once|without|except|excluding|"
    r"but|never|no|not|don['’]t)\b"
    r"|[\r\n`/\\\";:]|\w[\w.-]*\.\w",
    re.IGNORECASE,
)


def explicit_workspace_review(prompt):
    """Recognize a self-contained read request without looking at source."""
    text = prompt.strip()
    start = _DIRECT_REVIEW.match(text)
    if not start or _OTHER_WORK_OR_TARGET.search(text):
        return None
    tail = text[start.end():].rstrip(" .!?")
    if tail and (not _EXPLANATION.match(tail) or any(mark in tail for mark in ".!?")):
        return None
    work = {"operation": "read", "scope": "workspace", "paths": []}
    work, status, roots = workspace_target(work, text)
    return (work, roots) if status == "resolved" else None


def measure(store, session, target, evidence, recoverable):
    budget = usage(store, session) if session else None
    adapter = KarmaAdapter()
    exhausted = bool(budget and adapter.allocations(budget['used'], budget['limits'])['decision'] == 'COMPOUND_FIN')
    frame = adapter.evidence_admission(target_status=target, evidence_status=evidence,
                                       recovery_possible=recoverable, budget_exhausted=exhausted)
    return frame, budget


def workspace_roots(proposal):
    """Resolve explicit scope without probing the workspace."""
    from repository_tools import normalized

    paths = proposal.get('paths', [])
    if proposal.get('scope') == 'workspace' and not paths and proposal['operation'] == 'read':
        return 'resolved', ['.']
    if paths:
        try:
            if '.' in paths:
                raise InvalidUpdate('Whole workspace must be selected explicitly')
            return 'resolved', list(dict.fromkeys(normalized(path) for path in paths))
        except InvalidUpdate:
            return 'invalid', []
    if proposal['operation'] == 'capability' and proposal.get('capability_action') == 'install':
        # The verified capability ID is the target. No repository catalog is needed.
        return 'resolved', ['.']
    return 'missing', []


def workspace_target(proposal, prompt):
    """Bind read scope to user targets, rather than invented router filenames.

    A whole-project request names the authorized workspace as its target. A
    router may suggest starting files, but those are not a narrower user scope.
    Named paths still require an exact current-prompt reference; this helper
    never searches source or grants access.
    """
    if proposal.get('operation') != 'read':
        return proposal, *workspace_roots(proposal)
    paths = proposal.get('paths', [])
    # A filename, quoted directory or slash-delimited directory is an explicit
    # target even when the router mistakenly asks for the whole workspace.
    from repository_tools import EXTENSIONS
    extension = '|'.join(re.escape(item) for item in sorted(EXTENSIONS, key=len, reverse=True))
    def mentioned(name):
        match = re.search(r'(?<![\w./-])' + re.escape(name) + r'(?![\w/-]|\.[\w])', prompt)
        if not match:
            return False
        if '/' in name or any(name.endswith(suffix) for suffix in EXTENSIONS):
            return True
        # "project" and "tools logic" can be ordinary words, not directories.
        # Require directory syntax rather than authorizing a model's guess.
        return bool(re.search(r'[`"\']' + re.escape(name) + r'/?[`"\']|\b(?:under|folder|directory)\s+'
                              + re.escape(name) + r'\b|(?<![\w./-])' + re.escape(name) + r'/', prompt))
    named = [name for name in paths if mentioned(name)]
    file_target = re.search(r'(?<![\w])[^\s`"\'<>]+(?:' + extension + r')(?![\w])', prompt)
    directory_target = re.search(r'(?:\b[\w.-]+/)+(?:[\w.-]+/)?|[`"\'][\w.-]+/?[`"\']|\b(?:under|folder|directory)\s+[\w.-]+', prompt)
    if paths and len(named) == len(paths):
        return proposal, *workspace_roots(proposal)
    if named or file_target or directory_target:
        return proposal, 'missing', []
    if re.search(r'\b(?:workspace|code[ -]?base|project|repository|repo)\b', prompt, re.IGNORECASE):
        target = {'operation': 'read', 'scope': 'workspace', 'paths': []}
        return target, *workspace_roots(target)
    if paths:
        return proposal, 'missing', []
    return proposal, *workspace_roots(proposal)


def record_document(store, session, flow, text, *, read=None):
    key = 'document-bridge:' + flow['id']
    origin = {'kind': 'workspace_document' if read else 'evidence_bridge',
              'flow_id': flow['id'], 'request_hash': sha(flow['prompt'])}
    authority = 'tool' if read else 'user'
    if read:
        origin.update(read)
    if store.db.execute('SELECT 1 FROM document_heads WHERE session=? AND key=?', (session, key)).fetchone():
        # A bridge may have committed the document before being interrupted.
        record = store.document(session, key)
        if (record['version'] != 1 or record['text'] != text or record['digest'] != sha(text)
                or record['authority'] != authority or json.loads(record['source_json']) != origin):
            raise InvalidUpdate('A different document was already committed to this request')
    else:
        record = store.put_document(session, key, text, authority=authority, source=origin)
    return {field: record[field] for field in ('key', 'version', 'digest')}
