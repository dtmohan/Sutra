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
CLAIM_BATCH_SIZE = 4


def static_statements(source):
    if type(source.get('claim_source_version')) is not int or source['claim_source_version'] != 2:
        return []
    from model_packet import projected_source_statements
    return projected_source_statements(source)


def source_paths(source):
    return sorted(set(source['excerpts']) | {statement['path'] for statement in static_statements(source)}
                  | {unit['path'] for unit in retained_sources(source)})


def retained_sources(source):
    if source.get('retained_context_version') != 1:
        return []
    from retained_context import source_units
    return source_units(source['retained_context'])


def covered_by_excerpt(source, unit):
    """A fresh delivered excerpt already contains this exact source position.

    Callers validate freshness before assembling either view. Equal text at a
    different line/column is not the same span. Unknown positions never justify
    eliding a body; no source or receipt is read by this projection helper.
    """
    path, text = unit.get('path'), unit.get('text')
    excerpt = source.get('excerpts', {}).get(path)
    location = source.get('excerpt_locations', {}).get(path, {})
    start, line = location.get('start_line'), unit.get('line')
    base_column, column = location.get('column', 0), unit.get('column', 0)
    if (not isinstance(text, str) or not text or not isinstance(excerpt, str)
            or type(start) is not int or start < 1 or type(line) is not int or line < start
            or type(base_column) is not int or base_column < 0
            or type(column) is not int or column < 0):
        return False
    known_version = source.get('source_versions', {}).get(path)
    if known_version is not None and unit.get('sha256') != known_version:
        return False
    rows = excerpt.splitlines(keepends=True)
    relative_line = line - start
    if relative_line >= len(rows):
        return False
    relative_column = column - (base_column if relative_line == 0 else 0)
    if not 0 <= relative_column < len(rows[relative_line]):
        return False
    offset = sum(map(len, rows[:relative_line])) + relative_column
    return excerpt.startswith(text, offset)


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
    # Retained source units can contain disjoint windows from the same file.
    # Each has already passed the writer's scoped receipt/freshness check.
    # Independent reviewers receive these exact bodies, never saved model notes.
    seen_retained = set()
    for unit in retained_sources(source):
        text = unit['text']
        if covered_by_excerpt(source, unit):
            continue
        version = unit.get('sha256')
        if (isinstance(version, str) and len(version) == 64
                and all(char in '0123456789abcdef' for char in version)):
            identity = (unit['path'], version, unit.get('line'), unit.get('column', 0), text)
            if identity in seen_retained:
                continue
            seen_retained.add(identity)
        offset = 0
        while offset < len(text):
            end = min(len(text), offset + 512)
            if end < len(text):
                newline = text.rfind('\n', offset, end)
                if newline >= offset + 128: end = newline + 1
            eid = 'e' + sha(canonical(['retained-source-v1', candidate_id, unit['id'], sha(text), offset, end]))[:12]
            line = unit.get('line')
            result[eid] = {'kind': 'retained_source_unit', 'path': unit['path'], 'text': text[offset:end],
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
    if source.get('retained_context_version') == 1:
        value.update(retained_context_version=1, retained_source_units=retained_sources(source))
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
    if type(source.get('review_intent_version')) is int and source['review_intent_version'] == 1:
        result['evidence_set_version'] = 1
    if type(source.get('review_judgment_version')) is int and source['review_judgment_version'] == 1:
        result['review_judgment_version'] = 1
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


def partition_claims(claims, evidence, fits, batch_size=CLAIM_BATCH_SIZE):
    """Keep broad overview evidence from inflating every claim batch."""
    groups = [['overview']] if 'overview' in claims else []
    keys = [key for key in claims if key != 'overview']
    groups.extend(keys[i:i + batch_size] for i in range(0, len(keys), batch_size))
    return [page for group in groups
            for page in partition({key: claims[key] for key in group}, evidence, fits)]


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


def create(s, sid, candidate_id, draft, source, claims, evidence, fits, strict, *, bounded_claims=False):
    pack = partition_claims if bounded_claims else partition
    plan = {'id': uuid.uuid4().hex, 'version': VERSION, 'candidate_id': candidate_id,
            'candidate_hash': sha(canonical(draft)), 'source_hash': source_hash(source),
            'contract': copy.deepcopy(source['contract']), 'session': sid, 'strict': strict,
            'pages': pack(claims, evidence, fits), 'attempts_per_page': 2}
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


def output_failure(s, sid, plan, index, invocation_id):
    """Bind refinement to the actual settled output failure and its page."""
    row = s.db.execute('''SELECT d.payload_json,i.record_json,i.digest,r.record_json,r.id FROM invocations i
        JOIN deliveries d ON d.id=i.delivery_id JOIN invocation_receipts r ON r.invocation_id=i.id
        WHERE i.id=? AND i.session=?''', (invocation_id, sid)).fetchone()
    if not row:
        raise InvalidUpdate('Claim review output failure receipt is unavailable')
    packet_raw, env_raw, env_digest, receipt_raw, receipt_digest = row
    packet, envelope, receipt = map(json.loads, (packet_raw, env_raw, receipt_raw))
    if (sha('invocation-v1:' + env_raw) != env_digest
            or sha('invocation-receipt-v1:' + receipt_raw) != receipt_digest
            or sha(packet_raw) != envelope['payload_hash'] or receipt['invocation_hash'] != env_digest
            or envelope['id'] != invocation_id or receipt['invocation_id'] != invocation_id
            or envelope['role'] != 'report_reviewer' or packet.get('review_plan') != plan['id']
            or packet.get('review_page') != index or packet.get('candidate_id') != plan['candidate_id']
            or receipt.get('status') != 'quarantined' or receipt.get('failure_kind') != 'output_limit'):
        raise InvalidUpdate('Claim review output failure differs from its committed page')
    return packet


def refinement_parent(s, sid, plan):
    refinement = plan.get('refinement')
    if refinement is None:
        return None
    if (not isinstance(refinement, dict)
            or set(refinement) != {'previous', 'page', 'count', 'failed_invocation'}
            or type(refinement['page']) is not int or type(refinement['count']) is not int
            or refinement['count'] < 2 or refinement['previous'] == plan['id']):
        raise InvalidUpdate('Invalid claim page refinement')
    old = find(s, sid, plan['candidate_id'], refinement['previous'])
    index, count = refinement['page'], refinement['count']
    if (not 0 <= index < len(old['pages'])
            or any(old.get(key) != plan.get(key) for key in
                   ('version', 'candidate_hash', 'source_hash', 'contract', 'strict', 'attempts_per_page'))
            or old['pages'][:index] != plan['pages'][:index]
            or old['pages'][index + 1:] != plan['pages'][index + count:]
            or len(plan['pages']) != len(old['pages']) - 1 + count):
        raise InvalidUpdate('Claim refinement changed an unaffected page or its binding')
    old_page = old['pages'][index]
    old_claims, old_evidence = set(old_page['claims']), set(old_page['evidence'])
    for page in plan['pages'][index:index + count]:
        keys, evidence = set(page['claims']), set(page['evidence'])
        if not keys <= old_claims or not evidence <= old_evidence or (keys, evidence) == (old_claims, old_evidence):
            raise InvalidUpdate('Claim refinement did not narrow the failed page')
    output_failure(s, sid, old, index, refinement['failed_invocation'])
    return old, index, count


def page_attempts(s, sid, plan, index, through=None):
    """Inherited pages retain their admitted receipts and their spent attempts."""
    result = []
    seen = set()
    while True:
        if plan['id'] in seen:
            raise InvalidUpdate('Cyclic claim page refinement')
        seen.add(plan['id'])
        result.extend(attempts(s, sid, plan['id'], index, through))
        parent = refinement_parent(s, sid, plan)
        if parent is None:
            break
        previous, split, count = parent
        if split <= index < split + count:
            break
        plan = previous
        if index >= split + count:
            index -= count - 1
    return sorted(result, key=lambda row: row[1])


def refine(s, sid, plan, index, failed, draft, source, claims, evidence, fits):
    """Replace one failed page with smaller disjoint claim/evidence pair sets."""
    output_failure(s, sid, plan, index, failed)
    item = plan['pages'][index]
    page_claims = {key: claims[key] for key in item['claims']}
    units = {eid: evidence[eid] for eid in item['evidence']}
    if len(page_claims) > 1:
        batch = min(CLAIM_BATCH_SIZE, len(page_claims) // 2)
        smaller = partition_claims(page_claims, units, fits, batch)
    elif len(units) > 1:
        middle = len(units) // 2
        keys = list(units)
        smaller = [subpage for group in (keys[:middle], keys[middle:])
                   for subpage in partition(page_claims, {eid: units[eid] for eid in group}, fits)]
    else:
        return None
    refined = dict(plan, id=uuid.uuid4().hex,
                   pages=plan['pages'][:index] + smaller + plan['pages'][index + 1:],
                   refinement={'previous': plan['id'], 'page': index, 'count': len(smaller),
                               'failed_invocation': failed})
    validate_plan(refined, plan['candidate_id'], draft, source, claims, evidence)
    refinement_parent(s, sid, refined)
    with s.db: s._audit('REPORT_REVIEW_PLAN', refined)
    return refined
