"""Deterministic, source-bound work units for paged architecture judgments.

IDs authenticate excerpts and claim scope, never semantic support. Character
chunks are transfer granularity, not a limit on total work; the context adapter
and unchanged kernel decide whether each complete page fits.
"""
import copy
import json
import uuid
from context_manager import canonical, sha, InvalidUpdate, ContextBudgetExceeded

VERSION = 2


def static_statements(source):
    if type(source.get('claim_source_version')) is not int or source['claim_source_version'] != 2:
        return []
    from model_packet import projected_source_statements
    return projected_source_statements(source)


def source_paths(source):
    return sorted(set(source['excerpts']) | {statement['path'] for statement in static_statements(source)})


def _statement_in_excerpt(source, statement):
    text = source['excerpts'].get(statement['path'], '')
    start = source.get('excerpt_locations', {}).get(statement['path'], {}).get('start_line')
    if type(start) is not int:
        return False
    offset = statement['line'] - start
    lines = text.splitlines(True)
    return (0 <= offset < len(lines) and statement['text'] in
            ''.join(lines[offset:offset + statement['text'].count('\n') + 1]))


def inventory(candidate_id, source):
    result = {}
    for path, text in sorted(source['excerpts'].items()):
        # Keep line boundaries where possible; split very long source lines too.
        offset = 0
        while offset < len(text):
            end = min(len(text), offset + 512)
            if end < len(text):
                newline = text.rfind('\n', offset, end)
                if newline >= offset + 128: end = newline + 1
            quote = text[offset:end]
            eid = 'e' + sha(canonical([candidate_id, path, sha(text), offset, end]))[:12]
            line = source.get('excerpt_locations', {}).get(path, {}).get('start_line')
            result[eid] = {'path': path, 'text': quote, 'excerpt_start': offset, 'excerpt_end': end,
                           'start_line': line + text[:offset].count('\n') if line else None,
                           'end_line': line + text[:end].count('\n') if line else None}
            offset = end
    for statement in static_statements(source):
        if _statement_in_excerpt(source, statement):
            continue
        path, text, line = (statement[key] for key in ('path', 'text', 'line'))
        offset = 0
        while offset < len(text):
            end = min(len(text), offset + 512)
            if end < len(text):
                newline = text.rfind('\n', offset, end)
                if newline >= offset + 128: end = newline + 1
            eid = 'e' + sha(canonical(['delivered-static-source-v2', candidate_id, path, line,
                                       sha(text), offset, end]))[:12]
            result[eid] = {'kind': 'static_source_statement', 'path': path, 'text': text[offset:end],
                           'start_line': line + text[:offset].count('\n'),
                           'end_line': line + text[:end].count('\n'),
                           'meaning': 'Exact delivered static source text; not proof of runtime execution or order.'}
            offset = end
    return result


def affected(claims, evidence, ids):
    paths = {evidence[e]['path'] for e in ids}
    return [key for key, claim in claims.items() if paths.intersection(claim['paths'])]


def source_hash(source):
    value = {k: source.get(k) for k in ('excerpts', 'excerpt_locations', 'snapshot_receipts')}
    if type(source.get('claim_source_version')) is int and source['claim_source_version'] == 2:
        value.update(claim_source_version=2, static_source_statements=static_statements(source))
    return sha(canonical(value))


def packet(source, draft, candidate_id, plan, index, all_claims, evidence, separation, strict):
    item = plan['pages'][index]
    result = {'review_protocol': VERSION, 'review_plan': plan['id'], 'review_page': index,
            'contract': copy.deepcopy(source['contract']), 'workspace': source['workspace'],
            'constraint_echo': copy.deepcopy(source['constraint_echo']), 'pages': [],
            'request': 'Review the supplied claims against this page of fresh source evidence.',
            'candidate_id': candidate_id, 'candidate_hash': sha(canonical(draft)),
            'claims': {key: copy.deepcopy(all_claims[key]) for key in item['claims']},
            'source_evidence': {eid: copy.deepcopy(evidence[eid]) for eid in item['evidence']},
            'review_scope': {'page': index + 1, 'pages': len(plan['pages']),
                'meaning': 'Only this evidence page is visible. Judge each whole claim using it; absence here is not absence in the repository. Other pages are checked separately.'},
            'reviewer_selection': separation, 'require_distinct_reviewer': strict}
    if type(source.get('claim_timeout_recovery_version')) is int and source['claim_timeout_recovery_version'] == 1:
        result['claim_timeout_recovery_version'] = 1
    if type(source.get('review_transport_version')) is int and source['review_transport_version'] == 1:
        result['review_transport_version'] = 1
    if source.get('report_schema_version') == 2:
        result['evidence_choice_version'] = 2
    if type(source.get('claim_source_version')) is int and source['claim_source_version'] == 2:
        result['review_scope']['meaning'] += (' Entries labelled static_source_statement are delivered source syntax, '
                                             'not proof of runtime execution or order.')
    return result


def partition(claims, evidence, fits):
    """Pack source and claims together; every relevant pair must be covered."""
    pages = []
    current = []
    def blocks(ids):
        keys = affected(claims, evidence, ids)
        if not keys: return []
        if fits({'claims': keys, 'evidence': ids}): return [{'claims': keys, 'evidence': list(ids)}]
        if len(ids) > 1: return None
        # A single excerpt can concern many claims. Page those claims too.
        groups = []; batch = []
        for key in keys:
            proposal = {'claims': batch + [key], 'evidence': list(ids)}
            if not fits(proposal):
                if not batch: raise ContextBudgetExceeded('One claim and its source evidence cannot fit the reviewer allocation')
                groups.append({'claims': batch, 'evidence': list(ids)}); batch = [key]
                if not fits({'claims': batch, 'evidence': list(ids)}):
                    raise ContextBudgetExceeded('One claim and its source evidence cannot fit the reviewer allocation')
            else: batch.append(key)
        if batch: groups.append({'claims': batch, 'evidence': list(ids)})
        return groups
    for eid in evidence:
        if not affected(claims, evidence, [eid]): continue
        proposed = current + [eid]
        if blocks(proposed) is not None:
            current = proposed
        else:
            pages.extend(blocks(current)); current = [eid]
            blocks(current)  # A non-fitting single unit fails before any dispatch.
    if current: pages.extend(blocks(current))
    without = [key for key in claims if not any(key in page['claims'] for page in pages)]
    for key in without:
        item = {'claims': [key], 'evidence': []}
        if not fits(item): raise ContextBudgetExceeded('One unsupported claim cannot fit the reviewer allocation')
        pages.append(item)
    return pages


def validate_plan(plan, candidate_id, draft, source, claims, evidence):
    if (plan.get('version') != VERSION or plan.get('candidate_id') != candidate_id
            or plan.get('candidate_hash') != sha(canonical(draft)) or plan.get('source_hash') != source_hash(source)
            or plan.get('contract') != source['contract'] or type(plan.get('strict')) is not bool
            or plan.get('attempts_per_page') != 2 or not isinstance(plan.get('id'), str)):
        raise InvalidUpdate('Claim review plan binding changed')
    expected = {(key, eid) for key, claim in claims.items() for eid, entry in evidence.items() if entry['path'] in claim['paths']}
    got = set(); seen_claims = set()
    if not plan.get('pages'): raise InvalidUpdate('Empty claim review plan')
    for page in plan['pages']:
        keys, ids = page['claims'], page['evidence']
        if not keys or len(keys) != len(set(keys)) or len(ids) != len(set(ids)) or set(keys)-set(claims) or set(ids)-set(evidence):
            raise InvalidUpdate('Invalid claim review page addresses')
        for key in keys:
            pairs = {(key, eid) for eid in ids if evidence[eid]['path'] in claims[key]['paths']}
            if not pairs and any(k == key for k, _ in expected): raise InvalidUpdate('Claim page has no applicable source')
            if got.intersection(pairs): raise InvalidUpdate('Duplicate claim evidence coverage')
            got.update(pairs); seen_claims.add(key)
    if got != expected or seen_claims != set(claims): raise InvalidUpdate('Claim review plan omits source evidence or claims')


def create(s, sid, candidate_id, draft, source, claims, evidence, fits, strict):
    plan = {'id': uuid.uuid4().hex, 'version': VERSION, 'candidate_id': candidate_id,
            'candidate_hash': sha(canonical(draft)), 'source_hash': source_hash(source),
            'contract': copy.deepcopy(source['contract']), 'session': sid, 'strict': strict,
            'pages': partition(claims, evidence, fits), 'attempts_per_page': 2}
    validate_plan(plan, candidate_id, draft, source, claims, evidence)
    with s.db: s._audit('REPORT_REVIEW_PLAN', plan)
    return plan


def find(s, sid, candidate_id, plan_id=None):
    rows = s.db.execute("SELECT payload_json FROM page_audit WHERE event='REPORT_REVIEW_PLAN' ORDER BY seq DESC")
    for raw, in rows:
        plan = json.loads(raw)
        if plan['session'] == sid and plan['candidate_id'] == candidate_id and (plan_id is None or plan['id'] == plan_id): return plan
    if plan_id is not None: raise InvalidUpdate('Unknown claim review plan')
    return None


def attempts(s, sid, plan_id, index, through=None):
    rows = s.db.execute('''SELECT i.id,i.rowid,json_extract(r.record_json,'$.status') FROM invocations i
        JOIN deliveries d ON d.id=i.delivery_id LEFT JOIN invocation_receipts r ON r.invocation_id=i.id
        WHERE i.session=? AND json_extract(d.payload_json,'$.review_plan')=?
        AND json_extract(d.payload_json,'$.review_page')=? ORDER BY i.rowid''', (sid, plan_id, index))
    return [r for r in rows if through is None or r[1] <= through]
