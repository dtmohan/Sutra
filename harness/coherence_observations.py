"""Bounded, receipt-local projection of existing stage artifacts.

This observes explicit structure, not arbitrary prose. It neither calls a model
nor reads task history. Source roots are exact delivered strings; model facts,
verdicts, commitments and closure claims remain unverified. Optional diagnostics
never change admission, permissions, spending or task completion.
"""
import json
import sqlite3
from itertools import islice

from context_manager import canonical, sha, InvalidUpdate
from text_coherence import assess


PROFILE = 'stage-text-coherence-v1'
MAX_OBSERVATION_BYTES = 12288
MAX_CAPTURE_BYTES = 8192  # Leave 4 KiB for the gateway's model/timing/kernel fields.
MAX_RECEIPT_BYTES = 2 * 1024 * 1024
MAX_SCAN = 32
MAX_ISSUES = 16
_ROLES = {'conversation', 'chat_reviewer', 'architect', 'subagent', 'coder',
          'document_extract', 'report_reviewer'}
_ITEMS, _SOURCES, _EDGES, _CHARS = 96, 48, 192, 48000
_SOURCE_CHARS = 65536
_NOTICE = ('Structural observations only. Model findings and semantic judgments are '
           'unverified; observations do not authorize actions or certify completion.')


def _identity(value, fallback):
    if value is None:
        return fallback
    if (not isinstance(value, str) or not value.strip() or len(value) > 128
            or any(ord(c) < 32 for c in value)):
        raise ValueError('Invalid stage coherence identity')
    return value


def _short(value, limit=1800):
    if not isinstance(value, str):
        raise ValueError('Stage coherence text must be a string')
    # Control characters in diagnostic text are not executable or semantic data.
    return ''.join(c for c in value[:limit]
                   if (ord(c) >= 32 or c in '\n\t\r') and not 0xD800 <= ord(c) <= 0xDFFF)


def _prefix(kind, value):
    return kind + ':' + sha(str(value))[:24]


def _rows(value, maximum, omitted, key):
    if not isinstance(value, list):
        raise ValueError('Stage coherence rows must be a list')
    omitted[key] += max(0, len(value) - maximum)
    return value[:maximum]


class _Projection:
    def __init__(self, envelope, payload):
        contract = payload.get('contract') or {}
        if not isinstance(contract, dict):
            raise ValueError('Invalid stage contract')
        session = _identity(envelope.get('session'), 'unavailable-session')
        task = _identity(contract.get('task_id'),
                         _prefix('task', envelope.get('contract_hash', session)))
        axis = payload.get('conversation_axis')
        chat = _identity(axis, _prefix('task-chat', [session, task]))
        self.binding = {'session': session, 'task': task, 'chat': chat}
        if isinstance(envelope.get('contract_hash'), str):
            self.binding['contract_hash'] = _identity(envelope['contract_hash'], '')
        request = payload.get('request', contract.get('goal', ''))
        if not isinstance(request, str) or len(request) > 65536:
            raise ValueError('Stage request exceeds diagnostic allocation')
        self.request_hash = sha(request)
        self.binding['request_hash'] = self.request_hash
        if axis is not None:
            self.binding['axis'] = chat
        artifact = payload.get('document_evidence') or {}
        if not isinstance(artifact, dict):
            raise ValueError('Invalid stage artifact')
        artifact_key = [artifact.get('key'), artifact.get('version'), artifact.get('digest'),
                        payload.get('document_stage_flow'), payload.get('candidate_id')]
        self.context = _prefix('context', canonical([self.binding, artifact_key]))
        self.frame = {'format': 1, 'binding': self.binding, 'active_context': self.context,
                      'contexts': [{'id': self.context, 'mode': 'factual'}],
                      'sources': [], 'items': [], 'edges': [],
                      'required_question_ids': [], 'closed_question_ids': [],
                      'relevant_context_ids': [self.context]}
        self.envelope = envelope
        _identity(envelope.get('id'), 'unavailable-invocation')
        _identity(envelope.get('delivery_id'), 'unavailable-delivery')
        self.aliases = {}
        self.root_ids = {}
        self.omitted = {'items': 0, 'sources': 0, 'edges': 0, 'text_characters': 0}
        self.characters = 0
        self.extra_issues = []
        self.unparsed = set()
        self.verdicts = {'supported': 0, 'contradicted': 0, 'insufficient_evidence': 0}
        self.assessed_met = 0
        self.page_facts = []
        self.stale_facts = 0
        self._register_payload(payload)

    def issue(self, code, detail, items=(), kind='semantic_indicator'):
        if len(self.extra_issues) < 64:
            self.extra_issues.append({'code': code, 'items': list(items)[:4],
                                      'detail': _short(detail, 240), 'kind': kind})

    def alias(self, name, text, *, authority='source', version=1, current_version=None,
              locator=None, status='current', context_id=None):
        if not isinstance(name, str) or not name or not isinstance(text, str):
            raise ValueError('Invalid delivered coherence source')
        if len(self.aliases) >= 64 and name not in self.aliases:
            self.omitted['sources'] += 1
            return
        if type(version) is not int or version < 1:
            raise ValueError('Invalid delivered source version')
        current = version if current_version is None else current_version
        if type(current) is not int or current < 1:
            raise ValueError('Invalid current source version')
        row = {'text': text, 'authority': authority, 'version': version,
               'current_version': current, 'locator': locator or name, 'status': status,
               'context_id': context_id or self.context}
        if name in self.aliases and self.aliases[name] != row:
            self.issue('ambiguous_source_reference', 'A delivered reference names different source units.',
                       [_prefix('source', name)], 'structural')
            self.aliases[name] = None
        else:
            self.aliases[name] = row

    def _register_payload(self, p):
        for key in ('source_evidence', 'excerpts'):
            values = p.get(key, {})
            if not isinstance(values, dict):
                raise ValueError('Invalid delivered source collection')
            for index, (name, entry) in enumerate(values.items()):
                if index >= 64:
                    self.omitted['sources'] += len(values) - index
                    break
                if isinstance(entry, str):
                    self.alias(name, entry)
                elif isinstance(entry, dict):
                    self.alias(name, entry['text'], locator=str(entry.get('path', name)))
                else:
                    raise ValueError('Invalid delivered source entry')
        for key in ('evidence', 'pages'):
            values = p.get(key, [])
            if not isinstance(values, list):
                raise ValueError('Invalid delivered evidence collection')
            for entry in _rows(values, 64, self.omitted, 'sources'):
                if not isinstance(entry, dict):
                    raise ValueError('Invalid delivered evidence entry')
                self.alias(entry['id'], entry['text'],
                    authority=('source' if entry.get('authority') in ('user', 'file', 'tool', 'source')
                               or key == 'evidence' and 'authority' not in entry else 'model'),
                    version=entry.get('version', 1), current_version=entry.get('current_version'),
                    locator=str(entry.get('path', entry['id'])), status=entry.get('status', 'current'))
                if key == 'pages':
                    facts = entry.get('facts', {})
                    if not isinstance(facts, dict):
                        raise ValueError('Invalid explicit page facts')
                    room = max(0, 32 - len(self.page_facts))
                    for fact_key, value in islice(facts.items(), room):
                        self.page_facts.append((entry, fact_key, value))
                    self.omitted['items'] += max(0, len(facts) - room)
        staged = p.get('document_synthesis')
        if staged is not None:
            if not isinstance(staged, dict):
                raise ValueError('Invalid staged document evidence')
            for entry in _rows(staged.get('evidence', []), 64, self.omitted, 'sources'):
                self.alias(entry['id'], entry['quote'], locator=str(entry.get('section', entry['id'])))
        if 'section_text' in p:
            section = p.get('section') or {}
            self.alias('document-section', p['section_text'], locator=str(section.get('id', 'section')))
        if 'document_evidence' in p and 'text' in p['document_evidence']:
            doc = p['document_evidence']
            self.alias('document', doc['text'], version=doc.get('version', 1),
                       locator=str(doc.get('key', 'document')))

    def item(self, identity, kind, text, *, requires_evidence=False, **fields):
        if not isinstance(text, str) or not text.strip():
            return None
        if len(self.frame['items']) >= _ITEMS:
            self.omitted['items'] += 1
            return None
        value = _short(text, 1800)
        self.omitted['text_characters'] += max(0, len(text) - len(value))
        if self.characters + len(value) > _CHARS:
            self.omitted['items'] += 1
            return None
        iid = _prefix(kind, identity)
        if any(item['id'] == iid for item in self.frame['items']):
            self.issue('duplicate_stage_item', 'Duplicate structured item identity.', [iid], 'structural')
            return iid
        row = {'id': iid, 'context_id': self.context, 'kind': kind, 'text': value,
               'requires_evidence': requires_evidence, **fields}
        self.characters += len(value)
        self.frame['items'].append(row)
        return iid

    def edge(self, source, target, kind='supports', *, origin='model'):
        if source is None or target is None:
            return
        if len(self.frame['edges']) >= _EDGES:
            self.omitted['edges'] += 1
            return
        self.frame['edges'].append({'from': source, 'to': target, 'kind': kind, 'origin': origin})

    def root(self, alias, quote=None):
        source = self.aliases.get(alias)
        if source is None:
            if not self.omitted['sources']:
                return _prefix('missing-source', alias), None
            return None, None
        body = source['text']
        if quote is None:
            # Evidence IDs used by a reviewer name complete transfer units.
            # Over-sized units remain unassessed rather than silently truncated.
            if len(body) > 4096:
                self.omitted['sources'] += 1
                return None, None
            quote = body
        if not isinstance(quote, str) or not quote.strip():
            raise ValueError('Invalid source quotation')
        if len(quote) > 4096:
            self.omitted['sources'] += 1
            return None, None
        offset = body.find(quote, 0, _SOURCE_CHARS)
        if offset < 0:
            if len(body) > _SOURCE_CHARS:
                self.omitted['sources'] += 1
                return None, None
            self.issue('quotation_absent', 'A model quotation is absent from the delivered source.',
                       [_prefix('source', alias)], 'structural')
            return _prefix('missing-quotation', [alias, quote]), None
        rid = _prefix('source', canonical([alias, offset, quote]))
        if rid in self.root_ids:
            return rid, quote
        if len(self.frame['sources']) >= _SOURCES or self.characters + len(quote) * 2 > _CHARS:
            self.omitted['sources'] += 1
            return None, None
        self.characters += len(quote)
        source_row = {'id': rid, 'context_id': source['context_id'], 'binding': dict(self.binding),
                      'authority': source['authority'], 'text': quote, 'sha256': sha(quote),
                      'version': source['version'], 'current_version': source['current_version'],
                      'receipt_id': _prefix('delivery-source', [self.envelope.get('delivery_id'), alias]),
                      'locator': _short(source['locator'], 400) + ':' + str(offset)}
        self.root_ids[rid] = source_row
        self.frame['sources'].append(source_row)
        if source['status'] not in ('current', 'aging', 'unverified'):
            self.issue('source_state', 'Delivered evidence has state ' + str(source['status'])[:40] + '.', [rid], 'structural')
        return rid, quote

    def cite(self, iid, alias, quote=None):
        if iid is None:
            return
        rid, actual = self.root(alias, quote)
        if rid is None:
            return
        self.edge(iid, rid)
        if actual is not None:
            row = next(item for item in self.frame['items'] if item['id'] == iid)
            spans = row.setdefault('spans', [])
            if len(spans) < 4 and self.characters + len(actual) <= _CHARS:
                spans.append({'source_id': rid, 'start': 0, 'end': len(actual), 'quote': actual})
                self.characters += len(actual)


def _observe_task(p, payload, observation):
    ref = (payload.get('conversation_handoff') or {}).get('task_commitment') or {}
    if observation and observation.get('kind') == 'commitment':
        ref = observation
    value = ref.get('value') if isinstance(ref, dict) and ref.get('status') == 'recorded' else None
    questions = {}
    if isinstance(value, dict):
        for item in _rows(value.get('criteria', []), 6, p.omitted, 'items'):
            key = item['id']
            questions[key] = p.item('criterion:' + key, 'question', item['deliverable'] + ': ' + item['done_when'])
        for index, text in enumerate(_rows(value.get('assumptions', []), 3, p.omitted, 'items')):
            p.item('assumption:' + str(index), 'assumption', text)
    if not isinstance(observation, dict) or observation.get('kind') != 'assessment' or observation.get('status') != 'recorded':
        p.unparsed.add('task_completion')
        return
    value = observation.get('value') or {}
    assessed = _rows(value.get('items', []), 6, p.omitted, 'items')
    for item in assessed:
        criterion = item['criterion_id']
        target = questions.get(criterion)
        iid = p.item('assessment:' + criterion, 'claim', item['evidence'])
        if target and item['status'] == 'met':
            p.assessed_met += 1
            p.edge(iid, target, 'answers')
        elif item['status'] in ('unmet', 'partial'):
            p.issue('unresolved_commitment', 'The model reports a committed criterion as ' + item['status'] + '.',
                    [target or _prefix('criterion', criterion)])
    remaining = _rows(value.get('remaining', []), 6, p.omitted, 'items')
    if value.get('judgement') == 'complete' and (remaining or any(item.get('status') != 'met' for item in assessed)):
        p.issue('completion_conflict', 'The model claims completion while reporting remaining work or unmet criteria.')


def _observe_findings(p, proposal):
    values = proposal.get('findings', [])
    if isinstance(values, dict):
        p.omitted['items'] += max(0, len(values) - 32)
        values = list(islice(values.values(), 32))
    for index, row in enumerate(_rows(values, 32, p.omitted, 'items')):
        iid = p.item('finding:' + str(index), 'claim', row['note'], requires_evidence=True)
        p.cite(iid, row['page_id'], row['quote'])
    for index, row in enumerate(_rows(proposal.get('citations', []), 32, p.omitted, 'items')):
        if not isinstance(row, dict) or 'quote' not in row:
            p.unparsed.add('citation_semantics')
            continue
        iid = p.item('citation:' + str(index), 'claim', 'Model-selected source quotation.', requires_evidence=True)
        p.cite(iid, row.get('path', row.get('id', '')), row['quote'])


def _observe_document(p, proposal):
    for index, row in enumerate(_rows(proposal.get('notes', []), 6, p.omitted, 'items')):
        iid = p.item('document-fact:' + str(index), 'claim', row['fact'], requires_evidence=True)
        p.cite(iid, 'document-section', row['quote'])


def _observe_page_facts(p):
    """Explicit host-delivered fact keys are scoped assertions, not prose parsing."""
    for entry, key, value in p.page_facts:
        if (not isinstance(key, str) or not key or len(key) > 256
                or type(value) not in (str, int, bool, type(None))):
            p.unparsed.add('unrepresentable_page_fact')
            p.omitted['items'] += 1
            continue
        if (isinstance(value, str) and len(value) > 1024
                or type(value) is int and not -2**63 <= value < 2**63):
            p.omitted['items'] += 1
            continue
        qualifiers = entry.get('context', {})
        if not isinstance(qualifiers, dict):
            raise ValueError('Invalid explicit page context')
        mode = qualifiers.get('mode', entry.get('mode', 'factual'))
        subject = qualifiers.get('subject', entry.get('subject', ''))
        when = qualifiers.get('time', entry.get('time', ''))
        if (mode not in ('factual', 'hypothetical') or not isinstance(subject, str)
                or not isinstance(when, str) or len(subject) > 256 or len(when) > 256):
            raise ValueError('Invalid explicit fact qualifiers')
        context = p.context
        if mode != 'factual' or subject or when:
            context = _prefix('qualified-context', canonical([p.context, mode, subject, when]))
            if not any(row['id'] == context for row in p.frame['contexts']):
                if len(p.frame['contexts']) >= 16:
                    p.omitted['items'] += 1
                    continue
                p.frame['contexts'].append({'id': context, 'mode': mode, 'subject': subject, 'time': when})
                p.frame['relevant_context_ids'].append(context)
        original = p.aliases.get(entry['id'])
        if original is None:
            p.omitted['sources'] += 1
            continue
        alias = _prefix('page-fact', canonical([entry['id'], key]))
        body = canonical({'key': key, 'value': value})
        p.alias(alias, body, authority=original['authority'], version=original['version'],
                current_version=original['current_version'], locator='Structured fact in ' + entry['id'],
                status=original['status'], context_id=context)
        current = (original['version'] == original['current_version']
                   and original['status'] in ('current', 'aging', 'unverified'))
        fields = {'key': _prefix('fact-key', key), 'value': value} if current else {}
        if not current:
            p.stale_facts += 1
        iid = p.item(alias, 'claim', 'Recorded value for ' + key + ': ' + canonical(value),
                     requires_evidence=True, context_id=context, **fields)
        p.cite(iid, alias, body)


def _observe_claim_review(p, payload, proposal):
    claims = payload.get('claims', {})
    values = proposal.get('claims', {})
    if not isinstance(claims, dict) or not isinstance(values, dict):
        raise ValueError('Invalid structured claim review')
    for index, (key, row) in enumerate(values.items()):
        if index >= 48:
            p.omitted['items'] += len(values) - index
            break
        original = claims.get(key)
        if not isinstance(original, dict):
            p.issue('unknown_claim', 'A model verdict names a claim outside the delivered page.', kind='structural')
            continue
        iid = p.item('reviewed-claim:' + key, 'claim', original['text'],
                     requires_evidence=row.get('verdict') in ('supported', 'contradicted'))
        verdict = row.get('verdict')
        if verdict not in p.verdicts:
            raise ValueError('Invalid claim verdict')
        p.verdicts[verdict] += 1
        if verdict == 'contradicted':
            p.issue('contradiction_indicator', 'A model reviewer reports conflicting source evidence; semantic correctness is unverified.', [iid] if iid else [])
        elif verdict == 'insufficient_evidence':
            p.issue('unresolved_claim', 'A model reviewer reports insufficient evidence for this claim.', [iid] if iid else [])
        if 'evidence_ids' in row:
            for ref in _rows(row['evidence_ids'], 8, p.omitted, 'edges'):
                p.cite(iid, ref)
        else:
            for cite in _rows(row.get('evidence', []), 2, p.omitted, 'edges'):
                p.cite(iid, cite['path'], cite['quote'])


def _summary(p, envelope, payload, proposal, task_observation):
    _observe_task(p, payload, task_observation)
    _observe_page_facts(p)
    role = envelope['role']
    if role == 'document_extract':
        _observe_document(p, proposal)
    elif role == 'report_reviewer':
        _observe_claim_review(p, payload, proposal)
    else:
        _observe_findings(p, proposal)
    for index, text in enumerate(_rows(proposal.get('remaining', []), 16, p.omitted, 'items')):
        p.item('remaining:' + str(index), 'question', text)
    # Narratives are intentionally not split into supposedly factual claims.
    if any(proposal.get(key) for key in ('answer', 'summary', 'report')):
        p.unparsed.add('free_form_narrative')
    result = assess(p.frame)
    issues = result['issues'] + p.extra_issues
    result['omitted_issues'] += max(0, len(issues) - MAX_ISSUES)
    result['issues'] = issues[:MAX_ISSUES]
    result.update(profile=PROFILE, authority='host_structure_model_semantics_unverified', notice=_NOTICE,
                  invocation_id=envelope['id'], role=role, binding=p.binding,
                  active_context=p.context,
                  context_binding=payload.get('context_binding'), request_hash=p.request_hash,
                  frame_hash=sha(canonical(p.frame)), projection_omitted=dict(p.omitted),
                  model_verdicts_unverified=p.verdicts)
    result['analysis_limits']['unparsed_fields'] = sorted(p.unparsed)
    result['analysis_limits']['semantic_correctness_verified'] = False
    # Absence of semantic parsing, or bounded omission, never becomes a clear verdict.
    result['status'] = 'partial'
    if any(p.omitted.values()):
        result['signals']['within_limits'] = False
        result['analysis_limits']['projection_coverage'] = 'partial_input_omitted'
    else:
        result['analysis_limits']['projection_coverage'] = 'available_structured_fields'
    extras = {issue['code'] for issue in p.extra_issues}
    bad_references = {'quotation_absent', 'unknown_claim', 'duplicate_stage_item', 'ambiguous_source_reference'}
    if extras.intersection(bad_references):
        result['signals']['references_valid'] = False
        result['signals']['invalid_references'] += sum(issue['code'] in bad_references for issue in p.extra_issues)
    if 'source_state' in extras:
        result['signals']['sources_current'] = False
        result['signals']['stale_sources'] += sum(issue['code'] == 'source_state' for issue in p.extra_issues)
    result['signals']['conflict_indicators'] += p.verdicts['contradicted'] + int('completion_conflict' in extras)
    # Model-reported gaps are observations, not newly created host obligations.
    result['coverage']['model_reported_unresolved_criteria'] = sum(issue['code'] == 'unresolved_commitment' for issue in p.extra_issues)
    result['coverage']['model_claimed_met_criteria'] = p.assessed_met
    result['coverage']['stale_fact_assertions_not_compared'] = p.stale_facts
    sampled_ids = 0
    for key in ('open_obligations', 'answered_claimed_unverified', 'source_linked_claim_ids', 'unsupported_claim_ids'):
        sampled_ids += max(0, len(result.get(key, [])) - 16)
        result[key] = result.get(key, [])[:16]
    result['omitted_diagnostic_ids'] = sampled_ids
    while len(canonical(result).encode()) > MAX_CAPTURE_BYTES and result['issues']:
        result['issues'].pop(); result['omitted_issues'] += 1
    if len(canonical(result).encode()) > MAX_CAPTURE_BYTES:
        raise ValueError('Stage coherence observation exceeds allocation')
    return result


def _malformed(envelope):
    safe = lambda value, maximum: _short(value, maximum) if isinstance(value, str) else ''
    return {'format': 1, 'profile': PROFILE, 'mode': 'observe_only', 'status': 'malformed',
            'authority': 'host_structure_model_semantics_unverified', 'notice': _NOTICE,
            'invocation_id': safe(envelope.get('id', ''), 128),
            'role': safe(envelope.get('role', ''), 80),
            'issues': [], 'coverage': {},
            'signals': {'binding_valid': False, 'references_valid': False,
                        'sources_current': False, 'support_acyclic': False, 'within_limits': False,
                        'missing_references': 0, 'invalid_references': 0, 'stale_sources': 0,
                        'cross_context_references': 0, 'circular_support_components': 0,
                        'unsupported_claims': 0, 'open_obligations': 0,
                        'conflict_indicators': 0, 'model_closure_proposals': 0},
            'analysis_limits': {'free_text_coherence': 'unassessed',
                                'structured_checks': 'malformed_stage_input',
                                'semantic_correctness_verified': False}}


def capture(s, envelope, payload, proposal, task_observation=None):
    """Capture existing stage structure; ``s`` is intentionally never queried.

    Payload/source provenance is resolved by the invocation boundary. This
    diagnostic does not authenticate another task or import prior/global notes.
    """
    if (not isinstance(envelope, dict) or envelope.get('role') not in _ROLES
            or not isinstance(payload, dict) or not isinstance(proposal, dict)):
        return None
    if envelope['role'] == 'conversation' and not (
            proposal.get('intent') in ('workspace', 'document') or task_observation
            or 'document_evidence' in payload or 'document_synthesis' in payload):
        return None  # Greetings and ordinary chat do not create task ceremony.
    try:
        return _summary(_Projection(envelope, payload), envelope, payload, proposal, task_observation)
    except (ValueError, TypeError, KeyError, AttributeError, InvalidUpdate, RecursionError, OverflowError):
        # A diagnostic failure must never strand an admitted call or its charge.
        return _malformed(envelope)


def _stored(raw, expected, *, invocation_id=None, session=None):
    if (not isinstance(raw, str) or len(raw.encode()) > MAX_RECEIPT_BYTES
            or sha('invocation-receipt-v1:' + raw) != expected):
        return None
    record = json.loads(raw)
    if (invocation_id is not None and record.get('invocation_id') != invocation_id
            or session is not None and record.get('session') != session):
        return None
    observation = record.get('coherence_observation')
    if observation is None:
        return None
    if (not isinstance(observation, dict) or type(observation.get('format')) is not int or observation.get('format') != 1
            or observation.get('profile') != PROFILE or observation.get('mode') != 'observe_only'
            or observation.get('invocation_id') != record.get('invocation_id')
            or len(canonical(observation).encode()) > MAX_OBSERVATION_BYTES):
        return None
    binding = observation.get('binding')
    if isinstance(binding, dict) and binding.get('session') != record.get('session'):
        return None
    return observation


def read_observation(s, invocation_id, session=None):
    """Read one hash-checked immutable observation; never re-execute its graph."""
    if not isinstance(invocation_id, str) or not invocation_id:
        return None
    try:
        row = s.db.execute('SELECT id,CASE WHEN length(CAST(record_json AS BLOB))<=? THEN record_json END '
                           'FROM invocation_receipts WHERE invocation_id=?' +
                           (' AND session=?' if session is not None else ''),
                           (MAX_RECEIPT_BYTES, invocation_id, session) if session is not None else
                           (MAX_RECEIPT_BYTES, invocation_id)).fetchone()
        return _stored(row[1], row[0], invocation_id=invocation_id, session=session) if row else None
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError, sqlite3.Error):
        return None


def latest(s, limit=12):
    """Inspect at most 32 recent rows; missing old diagnostics need no replay."""
    if type(limit) is not int or not 1 <= limit <= 12:
        raise ValueError('Coherence history limit must be 1..12')
    try:
        rows = s.db.execute('SELECT id,CASE WHEN length(CAST(record_json AS BLOB))<=? THEN record_json END '
                            'FROM invocation_receipts ORDER BY rowid DESC LIMIT ?',
                            (MAX_RECEIPT_BYTES, MAX_SCAN))
    except sqlite3.Error:
        return []
    result = []
    for expected, raw in rows:
        try:
            observation = _stored(raw, expected)
            if observation is not None:
                record = json.loads(raw)
                result.append({'observation': observation, 'gateway_status': record.get('status', 'unavailable')})
                if len(result) == limit:
                    break
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
            continue
    return result
