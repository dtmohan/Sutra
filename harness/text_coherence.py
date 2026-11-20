"""Bounded checks of an explicit host-projected text graph.

This does not parse prose, authenticate receipts or certify entailment. The
caller resolves provenance/current versions and owns the task binding. Model
findings, proposed answers and supersession remain unverified data. Nothing in
this module changes a task, grants access, spends capacity or calls a model.

The frame is a private host projection, never a model-supplied request. Authority,
required/closed question IDs and host relationship origins must come from host
records; validating their shape cannot establish that a caller is authorized.
"""
import hashlib
import json

from context_manager import InvalidUpdate


LIMITS = {'contexts': 32, 'sources': 64, 'items': 128, 'edges': 256,
          'text_characters': 65536, 'spans_per_item': 8, 'issues': 16,
          'item_ids_per_issue': 8, 'item_text': 4096, 'quote_text': 4096}
_BINDING = {'session', 'task', 'chat'}
_BINDING_OPTIONAL = {'workspace', 'contract_hash', 'request_hash', 'axis'}
_TOP = {'format', 'binding', 'active_context', 'contexts', 'sources', 'items',
        'edges', 'required_question_ids', 'closed_question_ids', 'relevant_context_ids'}
ANALYSIS_LIMITS = {'semantic_graph_coverage': 'partial', 'free_text_coherence': 'unassessed',
                   'source_entailment': 'unverified', 'source_authenticity': 'caller_resolved'}


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - set(optional):
        raise InvalidUpdate('Malformed text coherence projection')


def _text(value, maximum=4096, *, empty=False):
    if (not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip())
            or any(ord(c) < 32 and c not in '\t\n\r' for c in value)):
        raise InvalidUpdate('Invalid text coherence string')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise InvalidUpdate('Text coherence strings must be valid UTF-8') from None
    return value


def _identity(value):
    _text(value, 128)
    if not value.isprintable(): raise InvalidUpdate('Invalid text coherence identity')
    return value


def _binding(value):
    _keys(value, _BINDING, _BINDING_OPTIONAL)
    for text in value.values(): _identity(text)


def _positive(value):
    if type(value) is not int or not 1 <= value < 2**63:
        raise InvalidUpdate('Invalid text coherence version')


def _list(value):
    if not isinstance(value, list): raise InvalidUpdate('Text coherence collection must be a list')


def _result(frame):
    return {'format': 1, 'mode': 'observe_only', 'status': 'assessed',
            'analysis_limits': dict(ANALYSIS_LIMITS), 'issues': [], 'omitted_issues': 0,
            'omitted_issue_item_ids': 0,
            'coverage': dict(contexts=len(frame['contexts']), sources=len(frame['sources']),
                             items=len(frame['items']), edges=len(frame['edges']),
                             required_questions=len(frame['required_question_ids']),
                             host_closed_questions=0, answered_claimed_unverified=0,
                             source_linked_claims=0, unsupported_claims=0, analyzed_items=0),
            'signals': dict(binding_valid=True, references_valid=True, sources_current=True,
                            support_acyclic=True, within_limits=True, missing_references=0,
                            invalid_references=0, stale_sources=0, cross_context_references=0,
                            circular_support_components=0, unsupported_claims=0,
                            open_obligations=0, conflict_indicators=0, model_closure_proposals=0),
            'open_obligations': [], 'answered_claimed_unverified': [],
            'source_linked_claim_ids': [], 'unsupported_claim_ids': []}


def _partial(frame, reason):
    result = _result(frame)
    result.update(status='partial', issues=[{'code': 'input_limit', 'items': [],
                                             'detail': reason[:200], 'kind': 'structural'}])
    result['signals'].update(within_limits=False, references_valid=False,
                             sources_current=False, support_acyclic=False)
    # False here means not established: no omitted graph is labeled clear.
    result['analysis_limits']['structured_checks'] = 'not_performed_input_limit'
    return result


def assess(frame):
    """Check format-1 structured data without inference or unbounded traversal.

    Host-supplied ``closed_question_ids`` alone record closure. An evidence-linked
    answer is still only a claimed answer. Source links establish provenance,
    never that a conclusion follows. Context IDs separate hypothetical worlds,
    subjects and times; only explicit host edges may import across contexts.
    """
    _keys(frame, _TOP - {'closed_question_ids', 'relevant_context_ids'},
          {'closed_question_ids', 'relevant_context_ids'})
    if type(frame['format']) is not int or frame['format'] != 1:
        raise InvalidUpdate('Unsupported text coherence format')
    _binding(frame['binding']); _identity(frame['active_context'])
    for name in ('contexts', 'sources', 'items', 'edges', 'required_question_ids'):
        _list(frame[name])
    closed_ids = frame.get('closed_question_ids', [])
    _list(closed_ids)
    relevant_context_ids = frame.get('relevant_context_ids', [frame['active_context']])
    _list(relevant_context_ids)
    for name in ('contexts', 'sources', 'items', 'edges'):
        if len(frame[name]) > LIMITS[name]: return _partial(frame, 'Too many ' + name)
    if len(frame['required_question_ids']) > LIMITS['items'] or len(closed_ids) > LIMITS['items']:
        return _partial(frame, 'Too many question references')
    if len(relevant_context_ids) > LIMITS['contexts']:
        return _partial(frame, 'Too many relevant context references')

    contexts = {}; sources = {}; items = {}; characters = 0
    for context in frame['contexts']:
        _keys(context, {'id', 'mode'}, {'subject', 'time'})
        cid = _identity(context['id'])
        if cid in contexts or context['mode'] not in ('factual', 'hypothetical'):
            raise InvalidUpdate('Invalid text context identity or mode')
        for name in ('subject', 'time'):
            _text(context.get(name, ''), 256, empty=True)
            characters += len(context.get(name, ''))
        if characters > LIMITS['text_characters']: return _partial(frame, 'Text character allocation exceeded')
        contexts[cid] = context
    if frame['active_context'] not in contexts:
        raise InvalidUpdate('Unknown active text context')
    for cid in relevant_context_ids:
        _identity(cid)
        if cid not in contexts: raise InvalidUpdate('Unknown relevant text context')
    if len(relevant_context_ids) != len(set(relevant_context_ids)):
        raise InvalidUpdate('Duplicate relevant context identity')
    selected_contexts = set(relevant_context_ids) | {frame['active_context']}
    for source in frame['sources']:
        _keys(source, {'id', 'context_id', 'binding', 'authority', 'text', 'sha256',
                       'version', 'current_version', 'receipt_id'}, {'locator'})
        sid = _identity(source['id']); _identity(source['context_id']); _binding(source['binding'])
        if sid in sources or source['authority'] not in ('source', 'model'):
            raise InvalidUpdate('Invalid text source identity or authority')
        _identity(source['receipt_id'])
        if 'locator' in source:
            _text(source['locator'], 512); characters += len(source['locator'])
        if not isinstance(source['text'], str): raise InvalidUpdate('Invalid text source body')
        characters += len(source['text'])
        if characters > LIMITS['text_characters']: return _partial(frame, 'Text character allocation exceeded')
        _text(source['text'], LIMITS['text_characters'], empty=True)
        _positive(source['version']); _positive(source['current_version'])
        digest = source['sha256']
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise InvalidUpdate('Invalid text source digest')
        sources[sid] = source
    for item in frame['items']:
        _keys(item, {'id', 'context_id', 'kind', 'text'}, {'key', 'value', 'requires_evidence', 'spans'})
        iid = _identity(item['id']); _identity(item['context_id'])
        if iid in items or iid in sources or item['kind'] not in ('question', 'claim', 'assumption'):
            raise InvalidUpdate('Invalid text item identity or kind')
        if isinstance(item['text'], str) and len(item['text']) > LIMITS['item_text']:
            return _partial(frame, 'An item text exceeds its character allocation')
        _text(item['text']); characters += len(item['text'])
        if characters > LIMITS['text_characters']: return _partial(frame, 'Text character allocation exceeded')
        if 'key' in item:
            _identity(item['key']); characters += len(item['key'])
        if 'value' in item:
            value = item['value']
            if value is not None and type(value) not in (str, bool, int):
                raise InvalidUpdate('Coherence values must be bounded scalar observations')
            if isinstance(value, str):
                _text(value, 1024, empty=True); characters += len(value)
            if type(value) is int and not -2**63 <= value < 2**63:
                raise InvalidUpdate('Coherence integer exceeds scalar allocation')
        if characters > LIMITS['text_characters']: return _partial(frame, 'Text character allocation exceeded')
        if 'requires_evidence' in item and type(item['requires_evidence']) is not bool:
            raise InvalidUpdate('Invalid coherence evidence requirement')
        spans = item.get('spans', [])
        _list(spans)
        if len(spans) > LIMITS['spans_per_item']: return _partial(frame, 'Too many source spans')
        for span in spans:
            _keys(span, {'source_id', 'start', 'end', 'quote'})
            if isinstance(span['quote'], str) and len(span['quote']) > LIMITS['quote_text']:
                return _partial(frame, 'A source quote exceeds its character allocation')
            _identity(span['source_id']); _text(span['quote'])
            characters += len(span['quote'])
            if characters > LIMITS['text_characters']: return _partial(frame, 'Text character allocation exceeded')
            if type(span['start']) is not int or type(span['end']) is not int:
                raise InvalidUpdate('Source span offsets must be integers')
        items[iid] = item
    for ids in (frame['required_question_ids'], closed_ids):
        for iid in ids: _identity(iid)
        if len(ids) != len(set(ids)): raise InvalidUpdate('Duplicate question identity')
    edges = []
    for edge in frame['edges']:
        _keys(edge, {'from', 'to', 'kind'}, {'origin', 'cross_context'})
        _identity(edge['from']); _identity(edge['to'])
        if edge['kind'] not in ('supports', 'depends_on', 'answers', 'supersedes'):
            raise InvalidUpdate('Unknown text relationship')
        if edge.get('origin', 'model') not in ('model', 'host') or type(edge.get('cross_context', False)) is not bool:
            raise InvalidUpdate('Invalid text relationship provenance')
        if edge.get('cross_context') and edge.get('origin', 'model') != 'host':
            raise InvalidUpdate('Only the host may register a cross-context relationship')
        edges.append(edge)

    result = _result(frame); signals = result['signals']; issues = []
    missing = set(); invalid = set(); cross = set(); bad = set(); valid_sources = set()

    def issue(code, ids, detail, kind='structural'):
        row = {'code': code, 'items': list(ids), 'detail': detail[:200], 'kind': kind}
        if row not in issues: issues.append(row)

    for sid, source in sources.items():
        valid = source['authority'] == 'source' and bool(source['text'])
        if source['binding'] != frame['binding']:
            signals['binding_valid'] = False; valid = False; invalid.add(('binding', sid))
            issue('source_binding_mismatch', [sid], 'Source belongs to another task, chat or contract scope.')
        if source['context_id'] not in contexts:
            missing.add(source['context_id']); valid = False
            issue('unknown_context', [sid], 'Source context is absent from the projected graph.')
        if hashlib.sha256(source['text'].encode()).hexdigest() != source['sha256']:
            invalid.add(('digest', sid)); valid = False
            issue('source_digest_mismatch', [sid], 'Source body differs from its supplied digest.')
        if source['version'] != source['current_version']:
            signals['stale_sources'] += 1; valid = False
            issue('stale_source', [sid], 'Source version is not the current host-resolved version.')
        if valid: valid_sources.add(sid)

    deps = {iid: set() for iid in items}; answers = []; supersessions = []
    for iid, item in items.items():
        if item['context_id'] not in contexts:
            missing.add(item['context_id']); bad.add(iid)
            issue('unknown_context', [iid], 'Item context is absent from the projected graph.')
        for span in item.get('spans', []):
            sid = span['source_id']; source = sources.get(sid)
            if source is None:
                missing.add(sid); bad.add(iid)
                issue('missing_source', [iid, sid], 'Quoted source is absent; page it in before relying on it.')
                continue
            if (not 0 <= span['start'] < span['end'] <= len(source['text'])
                    or source['text'][span['start']:span['end']] != span['quote']):
                invalid.add(('span', iid, sid)); bad.add(iid)
                issue('invalid_source_span', [iid, sid], 'Quote or offsets do not match the supplied source text.')
                continue
            if source['context_id'] != item['context_id']:
                cross.add((iid, sid)); bad.add(iid)
                issue('cross_context_reference', [iid, sid], 'A source span belongs to a different context.')
                continue
            deps[iid].add(sid)

    for edge in edges:
        left, right = edge['from'], edge['to']; a = items.get(left); b = items.get(right) or sources.get(right)
        if a is None or b is None:
            missing.update(x for x in (left, right) if x not in items and x not in sources)
            if left in items: bad.add(left)
            issue('missing_reference', [left, right], 'A declared relationship names an unavailable item.')
            continue
        if a['context_id'] != b['context_id'] and not edge.get('cross_context', False):
            cross.add((left, right)); bad.add(left)
            issue('cross_context_reference', [left, right], 'A relationship crosses contexts without a host registration.')
            continue
        ca, cb = contexts.get(a['context_id']), contexts.get(b['context_id'])
        if (ca and cb and ca['mode'] == 'factual' and cb['mode'] == 'hypothetical'
                and edge['kind'] in ('supports', 'depends_on')):
            cross.add((left, right)); bad.add(left)
            issue('hypothetical_premise', [left, right], 'A hypothetical premise cannot establish a factual claim.')
            continue
        if edge['kind'] in ('supports', 'depends_on'):
            deps[left].add(right)
        elif edge['kind'] == 'answers':
            if right not in items or items[right]['kind'] != 'question' or a['kind'] != 'claim':
                invalid.add(('answer', left, right))
                issue('invalid_answer_reference', [left, right], 'Answers must relate a claim to a question.')
            else: answers.append((left, right))
        elif edge['kind'] == 'supersedes':
            if right not in items or a['kind'] != 'claim' or items[right]['kind'] != 'claim':
                invalid.add(('supersedes', left, right))
                issue('invalid_supersession', [left, right], 'Supersession must identify two claims.')
            elif edge.get('origin', 'model') != 'host':
                signals['model_closure_proposals'] += 1
                issue('unverified_supersession', [left, right], 'Model-proposed supersession does not retire a prior claim.', 'semantic_indicator')
            else: supersessions.append((left, right))

    required = set(frame['required_question_ids']); closed = set()
    for iid in required | set(closed_ids):
        if iid not in items or items[iid]['kind'] != 'question':
            missing.add(iid); issue('unknown_question', [iid], 'Host obligation references an unavailable question.')
        elif items[iid]['context_id'] != frame['active_context']:
            cross.add(('obligation', iid))
            issue('inactive_obligation', [iid], 'An obligation belongs to a different active context.')
        elif iid in closed_ids: closed.add(iid)
    relevant = {iid for iid, item in items.items() if item['context_id'] in selected_contexts} | (required & items.keys())
    relevant.update(left for left, right in answers if right in required)
    frontier = list(relevant)
    while frontier:
        for child in deps[frontier.pop()]:
            if child in items and child not in relevant:
                relevant.add(child); frontier.append(child)

    # Explicit bounded reachability: even a cycle with an additional source root
    # remains a cycle. Every declared premise must have a non-circular basis.
    reach = {}
    for iid in sorted(relevant):
        visited = set(); todo = list(deps[iid])
        while todo:
            child = todo.pop()
            if child in items and child not in visited:
                visited.add(child); todo.extend(deps[child])
        reach[iid] = visited
    cyclic = {iid for iid in relevant if iid in reach[iid]}; grouped = set()
    for iid in sorted(cyclic):
        if iid in grouped: continue
        component = sorted(other for other in cyclic if other in reach[iid] and iid in reach[other])
        grouped.update(component); bad.update(component); signals['circular_support_components'] += 1
        issue('circular_support', component, 'Derived claims rely on themselves or one another.')
    rooted = set(valid_sources)
    for _ in range(len(relevant)):
        added = {iid for iid in relevant - rooted - bad if deps[iid] and deps[iid] <= rooted}
        if not added: break
        rooted.update(added)

    retired = set()
    replacement = {iid: set() for iid in items}
    for left, right in supersessions: replacement[left].add(right)
    for left, right in supersessions:
        a, b = items[left], items[right]
        seen = set(); todo = [right]
        while todo:
            other = todo.pop()
            if other not in seen:
                seen.add(other); todo.extend(replacement[other])
        if (left in seen or left not in rooted or a['context_id'] != b['context_id'] or not a.get('key')
                or a.get('key') != b.get('key')):
            invalid.add(('supersedes', left, right))
            issue('invalid_supersession', [left, right], 'Host update needs a current source basis and the same scoped fact key.')
        else: retired.add(right)
    source_linked = sorted(iid for iid in relevant & rooted if items[iid]['kind'] == 'claim')
    unsupported = sorted(iid for iid in relevant - rooted if items[iid]['kind'] == 'claim'
                         and items[iid].get('requires_evidence', True)
                         and contexts.get(items[iid]['context_id'], {}).get('mode') == 'factual')
    for iid in unsupported:
        issue('unsupported_claim', [iid], 'No complete, current, non-circular source basis is registered for this claim.')

    fact_groups = {}
    for iid in sorted(relevant - retired):
        item = items[iid]; context = contexts.get(item['context_id'], {})
        if item['kind'] != 'claim' or context.get('mode') != 'factual' or not item.get('key') or item.get('value') is None:
            continue
        key = (item['context_id'], context.get('subject', ''), context.get('time', ''), item['key'])
        fact_groups.setdefault(key, []).append(iid)
    for ids in fact_groups.values():
        values = {json.dumps(items[iid]['value'], ensure_ascii=False, sort_keys=True) for iid in ids}
        if len(values) > 1:
            signals['conflict_indicators'] += 1
            issue('value_conflict_indicator', ids, 'Scoped structured values differ; semantic contradiction is not established.', 'semantic_indicator')

    answered = sorted({right for left, right in answers if right in required and left in rooted})
    open_ids = sorted(required - closed)
    result.update(open_obligations=open_ids, answered_claimed_unverified=answered,
                  source_linked_claim_ids=source_linked, unsupported_claim_ids=unsupported)
    signals.update(references_valid=not (missing or invalid or cross), sources_current=not signals['stale_sources'],
                   support_acyclic=not cyclic, missing_references=len(missing), invalid_references=len(invalid),
                   cross_context_references=len(cross), unsupported_claims=len(unsupported), open_obligations=len(open_ids))
    result['coverage'].update(host_closed_questions=len(required & closed), answered_claimed_unverified=len(answered),
                               source_linked_claims=len(source_linked), unsupported_claims=len(unsupported), analyzed_items=len(relevant))
    result['issues'] = issues[:LIMITS['issues']]
    for row in result['issues']:
        omitted = max(0, len(row['items']) - LIMITS['item_ids_per_issue'])
        if omitted:
            row['items'] = row['items'][:LIMITS['item_ids_per_issue']]
            row['omitted_items'] = omitted
            result['omitted_issue_item_ids'] += omitted
    result['omitted_issues'] = max(0, len(issues) - LIMITS['issues'])
    return result
