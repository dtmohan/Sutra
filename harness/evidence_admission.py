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
_TRAILING_PLEASE = re.compile(r"(?:\s*,\s*|\s+)please$", re.IGNORECASE)
_OTHER_WORK_OR_TARGET = re.compile(
    r"\b(?:edit|modif|chang|fix|patch|refactor|rewrit|implement|run|execut|install|deploy|"
    r"commit|push|fetch|download|delet|creat|writ)[a-z]*\b"
    r"|\b(?:attached|pasted|supplied|below|above|example|external|github|another|other|"
    r"only|solely|instead|previous|if|unless|before|after|once|without|except|excluding|"
    r"but|never|no|not|don['’]t)\b"
    r"|[\r\n`/\\\";:]|\w[\w.-]*\.\w",
    re.IGNORECASE,
)
_PYTHON_PATH = r"(?:[\w.-]+/)*[\w.-]+\.py"
_TEST_PATH = rf"(?:`{_PYTHON_PATH}`|\"{_PYTHON_PATH}\"|'{_PYTHON_PATH}'|{_PYTHON_PATH})"
_TEST_SUPPORT = rf"{_TEST_PATH}(?:(?:\s*,\s*(?:and\s+)?|\s+and\s+){_TEST_PATH})*"
_DIRECT_TEST = re.compile(
    rf"(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?(?:run|execute)\s+"
    rf"(?P<label>(?:the\s+)?(?:(?:existing|permitted)\s+)?(?:python\s+)?"
    rf"(?:unittest|unit\s+tests?|tests?)(?:\s+file)?(?:\s+in)?\s+)?"
    rf"(?P<target>{_TEST_PATH})"
    rf"(?:(?:\s+with\s+|\.\s+include\s+(?:(?:its|the)\s+)?"
    rf"(?:(?:one|two|three|four|five|six|seven)\s+)?supporting\s+(?:source\s+)?files?\s+)"
    rf"(?P<support>{_TEST_SUPPORT})\s+in\s+(?:its|the)\s+read-only\s+(?:test\s+)?snapshot)?"
    r"(?:[.!?]\s+(?:do not edit|leave (?:all (?:three )?)?files unchanged)\.?)?[.!?]?",
    re.IGNORECASE,
)
_HTML_ENTRY = r"(?:[\w.-]+/)*[\w.-]+\.html?"
_BROWSER_TARGET = rf"(?:`{_HTML_ENTRY}`|\"{_HTML_ENTRY}\"|'{_HTML_ENTRY}'|{_HTML_ENTRY})"
_APP_TARGET = r"(?:the(?:\s+same)?|this|that|our|my)\s+(?:(?:local|web|browser)\s+)?(?:app(?:lication)?|(?:web\s*)?(?:site|page))"
_APP_REFERENCE = frozenset(('it', 'this', 'that'))
_BROWSER_REQUEST = re.compile(
    r"\s*(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?"
    r"(?P<verb>launch|run|open|start|render)\s+"
    rf"(?P<target>{_BROWSER_TARGET}|{_APP_TARGET}|it|this|that)"
    r"(?:\s+again)?"
    r"(?P<medium>\s+(?:in|using|with)\s+(?:(?:a|the)\s+)?(?:Chrome|(?:web\s+)?browser))?"
    r"(?:\s+again)?"
    r"(?:(?:[,\s]+and\s+|[,\s]+then\s+)(?P<feedback>"
    r"(?:critique|review|inspect|analy[sz]e|test|describe|check|see|tell\s+(?:me|us))\b|"
    r"(?:give|provide|share)\s+(?:(?:me|us|a|an|your|some)\s+)*(?:feedback|thoughts|impressions|opinion|critique|review)\b)"
    r"[^\r\n]*)?"
    r"\s*[.!?]*\s*",
    re.IGNORECASE,
)
_BROWSER_DECLINED = re.compile(
    r"\b(?:without|avoid|skip|do\s+not|don['’]t|never)\s+(?:(?:actually|ever|automatically)\s+)*"
    r"(?:run(?:ning)?|launch(?:ing)?|render(?:ing)?|start(?:ing)?|open(?:ing)?|"
    r"execut(?:e|ing)|brows(?:e|ing)|us(?:e|ing)\s+(?:(?:a|the)\s+)?(?:browser|Chrome))\b|"
    r"\b(?:only|just)\s+(?:(?:looking\s+)?at\s+)?(?:the\s+)?(?:source|code|JavaScript)\b|"
    r"\b(?:source|code|JavaScript|static analysis)\s+(?:alone|only)\b|"
    r"\b(?:skip|avoid|without)\s+(?:(?:the|a)\s+)?(?:browser|Chrome)\b|"
    r"\b(?:no\s+browser|browser\s+(?:is\s+)?not\s+needed)\b",
    re.IGNORECASE,
)
_BROWSER_FEEDBACK = re.compile(
    r"\s*(?:(?:so|and)\s+)?(?:what\s+(?:do|would|should)\s+(?:you|we|I)\s+"
    r"(?:think|change|fix|improve|prioriti[sz]e)|what(?:['’]s|\s+is)\s+your\s+"
    r"(?:take|opinion|feedback)|where\s+would\s+you\s+(?:start|spend\s+time))\b",
    re.IGNORECASE,
)
_VISUAL_FEEDBACK = re.compile(
    r"\s*(?:would|will|is|does)\s+[^.!?\r\n]{1,90}\b(?:readable|usable|fit\s+on|clip|overflow)\b",
    re.IGNORECASE,
)
_BROWSER_FEEDBACK_TARGET = re.compile(
    r"\b(?:app(?:lication)?|web\s*(?:site|page)|page|watch|dashboard|interface|UI|canvas|"
    r"display|layout|render(?:ing)?|viewport|phone|browser|controls?)\b|" + _HTML_ENTRY,
    re.IGNORECASE,
)
_PREPARED_BROWSER_PRIORITY = re.compile(
    r"(?:what\s+(?:would|should)\s+(?:you|we|I)\s+(?:fix|change|improve|prioriti[sz]e)|"
    r"where\s+would\s+you\s+(?:start|spend\s+time))"
    r"(?:\s+(?:first|next))?(?:\s+for\s+(?:that|this|it|this goal))?"
    r"(?:,?\s+and\s+why)?[.?!]?",
    re.IGNORECASE,
)
_SAME_BROWSER_SUBJECT = re.compile(
    r"\b(?:this|that|same|current)\s+(?:web\s*)?(?:app(?:lication)?|site|page|dashboard)\b"
    r"|\b(?:the|your)\s+(?:browser\s+(?:review|run|results?|observations?|check)|rendered page)\b",
    re.IGNORECASE,
)
_OTHER_BROWSER_SUBJECT = re.compile(
    r"\b(?:another|different|new|other|separate|unrelated)\s+"
    r"(?:project|topic|app(?:lication)?|(?:web\s*)?site|page|dashboard)\b",
    re.IGNORECASE,
)
_PREFACE_ACTION = re.compile(
    r"(?:^\s*|\b(?:I need you to|please)\s+)(?:please\s+)?"
    r"(?:edit|implement|run|launch|open|install|deploy|commit|push|upload|delete|replace)\b",
    re.IGNORECASE,
)
_FEEDBACK_FILLER = frozenset({
    'after','before','could','different','first','from','help','need','people','result','same',
    'should','test','that','these','they','this','those','trust','want','what','when','which',
    'with','without','would','your',
})


def explicit_workspace_review(prompt):
    """Recognize a self-contained read request without looking at source."""
    text = prompt.strip()
    if _OTHER_WORK_OR_TARGET.search(text):
        return None
    text = _TRAILING_PLEASE.sub("", text.rstrip(" .!?"))
    start = _DIRECT_REVIEW.match(text)
    if not start:
        return None
    tail = text[start.end():]
    if tail and (not _EXPLANATION.match(tail) or any(mark in tail for mark in ".!?")):
        return None
    work = {"operation": "read", "scope": "workspace", "paths": []}
    work, status, roots = workspace_target(work, text)
    return (work, roots) if status == "resolved" else None


def explicit_workspace_test(prompt):
    """Resolve a named unittest and any explicit snapshot files without reading them."""
    text = prompt.strip()
    match = _DIRECT_TEST.fullmatch(text) if '\n' not in text and '\r' not in text else None
    if not match:
        return None
    from repository_tools import normalized

    target = match['target'].strip("`'\"")
    basename = target.rsplit('/', 1)[-1].lower()
    if not (match['label'] or basename.startswith('test') or basename.endswith('_test.py')):
        return None
    support = [part.group(0).strip("`'\"") for part in re.finditer(_TEST_PATH, match['support'] or '')]
    try:
        paths = list(dict.fromkeys(normalized(name) for name in [target, *support]))
    except InvalidUpdate:
        return None
    if len(paths) > 8 or any(not name.endswith('.py') for name in paths):
        return None
    return {'operation': 'test', 'paths': paths, 'test_paths': [target]}, paths


def explicit_project_patch(prompt):
    """Find named edit targets and the supported project test request in user text."""
    command = r'(?:^|[.!?]\s+|,\s*|\band\s+)(?:run|execute)\s+(?:the\s+)?(?:(?:project\s+)?tests?\s+(?:using|with)\s+)?'
    command += r'[`"]?python3?\s+-m\s+unittest\s+discover\s+-s\s+tests(?:[`"]|(?=\s|[.!?]|$))'
    isolated = (re.search(r'\b(?:in|using)\s+an?\s+isolated\s+(?:git\s+)?worktree\b', prompt, re.I)
                and re.search(r'(?:^|[.!?]\s+|,\s*|\band\s+)(?:run|execute)\s+(?:the\s+)?project\s+tests\b', prompt, re.I))
    if not (isolated or re.search(command, prompt, re.I)):
        return None
    if not re.match(r'\s*(?:please\s+)?(?:fix|patch|edit|modify|update|refactor)\b', prompt, re.I):
        return None
    named = re.search(rf'\b(?:fix|patch|edit|modify|update|refactor|across|in)\s+'
                      rf'(?:(?:both|these|the following)\s+)?(?:files?\s+)?'
                      rf'(?P<paths>{_TEST_SUPPORT})(?=[\s,.!?]|$)', prompt, re.I)
    if not named:
        return None
    from repository_tools import normalized
    try:
        paths = sorted({normalized(match.group().strip("`'\"")) for match in re.finditer(
            _TEST_PATH, named['paths'])})
    except InvalidUpdate:
        return None
    if not 2 <= len(paths) <= 8:
        return None
    return {'operation': 'project_patch', 'paths': paths}


def browser_entry(value):
    """Return a normalized local HTML entry, or no admissible path."""
    if not isinstance(value, str) or not re.fullmatch(_HTML_ENTRY, value, re.I):
        return None
    from repository_tools import normalized
    try:
        return normalized(value)
    except InvalidUpdate:
        return None


def browser_review_intent(prompt):
    """Recognize a current request to observe an app before interpreting it."""
    match = _BROWSER_REQUEST.fullmatch(prompt)
    if not match or _BROWSER_DECLINED.search(prompt):
        return None
    target = match['target'].strip("`'\"")
    named = browser_entry(target)
    if not named and not (target.lower() in _APP_REFERENCE or re.fullmatch(_APP_TARGET, target, re.I)):
        return None
    # A bare 'open' or 'render' can mean editing or explaining the source.
    if not match['medium'] and not match['feedback'] and match['verb'].lower() in ('open', 'render'):
        return None
    refers_to_prior = not named and (target.lower() in _APP_REFERENCE
                                     or re.match(r'(?:the\s+same|that)\b', target, re.I))
    return {'entry': named or (None if refers_to_prior else 'index.html'), 'referred': not bool(named)}


def resolve_browser_target(prompt, candidates=()):
    """Bind an explicit run to an exact entry or request clarification.

    The caller supplies entry names verified in the originating chat and
    workspace. This resolver never reads files or grants an action.
    """
    intent = browser_review_intent(prompt)
    if not intent:
        return None
    entry = intent['entry']
    if intent['referred']:
        verified = {browser_entry(candidate) for candidate in candidates} - {None}
        if len(verified) == 1:
            entry = verified.pop()
        elif len(verified) > 1:
            entry = None
    return {'operation': 'browser_review', 'paths': [entry] if entry else []}


def explicit_browser_review(prompt):
    """Preserve the directly resolvable form of a browser request."""
    work = resolve_browser_target(prompt)
    return work if work and work['paths'] else None


def browser_feedback_intent(prompt):
    """Identify a feedback question when the caller has verified an active app."""
    if _BROWSER_DECLINED.search(prompt):
        return False
    lead = re.split(r'[.?!](?:\s+|$)', prompt, maxsplit=1)[0]
    match = _BROWSER_FEEDBACK.match(lead)
    if not match:
        return bool(_VISUAL_FEEDBACK.match(lead) and _BROWSER_FEEDBACK_TARGET.search(lead))
    tail = lead[match.end():].strip()
    return bool(re.fullmatch(r"(?:(?:of|about|with)?\s*(?:it|this|here|first|next|overall|now))?"
                             r"(?:,?\s+and\s+why)?", tail, re.I)
                or _BROWSER_FEEDBACK_TARGET.search(tail))


def browser_feedback_goal(prompt):
    """Find a declarative goal followed by a short, referential priority question."""
    if len(prompt)>1800 or _BROWSER_DECLINED.search(prompt):
        return None
    parts = re.split(r"(?<=[.?!])\s+|[\r\n]+",prompt.strip(),maxsplit=3)
    if len(parts)<2 or not _PREPARED_BROWSER_PRIORITY.fullmatch(parts[-1]):
        return None
    preface = ' '.join(parts[:-1])
    if (_OTHER_BROWSER_SUBJECT.search(preface)
            or any(part.endswith('?') or _PREFACE_ACTION.search(part) for part in parts[:-1])):
        return None
    return preface


def browser_goal_matches(goal, evidence):
    """Check a follow-up against the recorded entry and its verified observations."""
    if (_SAME_BROWSER_SUBJECT.search(goal)
            or re.search(r"(?<![\w/.-])"+re.escape(evidence['entry'])+r"(?![\w/.-])",goal,re.I)):
        return True

    def terms(text):
        words = re.findall(r"[^\W\d_]{4,}",text.casefold())
        return {word.removesuffix('s') for word in words} - _FEEDBACK_FILLER

    requested = terms(goal)
    return any(len(requested & terms(row['text']))>=2 for row in evidence['observations']
               if row['kind'] not in ('network','console'))


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
    if patch := explicit_project_patch(prompt):
        return patch, 'resolved', ['.']
    if test := explicit_workspace_test(prompt):
        work, roots = test
        return work, 'resolved', roots
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
