"""Shared logical memory lifecycle. Never a source of permission or budget authority.

Exact data remains in the existing immutable stores. These bounded indexes describe
confirmed prompt working sets, not RAM residency or proof that a model used a page.
Selection/validation and dashboard polling remain read-only. No inferred semantic
fact is promoted to host evidence and no fault silently relaxes a constraint.
"""
import json
from pathlib import Path
import time

from context_manager import ContextBudgetExceeded, canonical, sha

POLICY = json.loads(Path(__file__).with_name('memory-policy.json').read_text())
LIMITS = POLICY['diagnostics']
RECOVERY = {
    'missing': 'restore_verified_original_or_report_gap',
    'missing_source': 'restore_source_within_granted_scope',
    'source_changed': 'refresh_source_and_dependents',
    'superseded': 'select_current_version_and_dependents',
    'expired': 'refresh_from_authorized_source',
    'stale_dependency': 'refresh_source_and_dependents',
    'missing_dependency': 'restore_dependency_from_verified_original',
    'unverified_dependency': 'obtain_authoritative_evidence',
    'dependency_cycle': 'quarantine_and_inspect',
    'corrupt': 'quarantine_and_inspect',
    'denied': 'request_host_permission',
    'capacity': 'repartition_or_select_larger_eligible_context',
    'unavailable': 'inspect_retained_evidence',
}


class PageFault(Exception):
    def __init__(self, message, *, code='unavailable', key=None):
        super().__init__(message)
        self.code = code if code in RECOVERY else 'unavailable'
        self.key = key

    def details(self):
        return {'code': self.code, 'page': self.key, 'error': str(self),
                'recovery': RECOVERY[self.code], 'automatic_retry': False}


def fault_details(exc):
    if isinstance(exc, PageFault):
        return exc.details()
    return PageFault(str(exc), code='capacity' if isinstance(exc, ContextBudgetExceeded)
                     else 'unavailable').details()


def eviction_closure(pages, required):
    """Oldest optional page plus dependents, never a pinned prerequisite.

    Selection ordering is supplied by the adapter. Diagnostic refault counters
    cannot influence selection or make authoritative replay nondeterministic.
    """
    for key in pages:
        if key in required:
            continue
        dropping = {key}
        while True:
            more = {k for k, p in pages.items() if any(dep in dropping for dep, _ in p.dependencies)}
            if more <= dropping:
                break
            dropping |= more
        if not dropping & required:
            return dropping
    raise ContextBudgetExceeded('Required pages and prerequisites cannot be evicted')


def initialize(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS memory_events(
        seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, kind TEXT NOT NULL,
        session TEXT NOT NULL, record_json TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS memory_working_sets(
        delivery_id TEXT PRIMARY KEY, session TEXT NOT NULL, owner TEXT NOT NULL,
        previous_id TEXT, refs_json TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS memory_counters(name TEXT PRIMARY KEY, value INTEGER NOT NULL);
    ''')
    db.commit()


def event(db, kind, session, value):
    """Caller owns transaction. Only bounded diagnostic metadata, never page text."""
    db.execute('INSERT INTO memory_events(at,kind,session,record_json) VALUES(?,?,?,?)',
               (time.time(), kind, session, canonical(value)))
    db.execute('DELETE FROM memory_events WHERE seq NOT IN (SELECT seq FROM memory_events ORDER BY seq DESC LIMIT ?)',
               (LIMITS['memory_events'],))


def observe_fault(s, session, exc):
    # Called at a host operation boundary, never by status(), read_page() or a
    # validator. The append-only audit preserves the fault beyond UI retention.
    value = fault_details(exc)
    with s.db:
        s._audit('MEMORY_FAULT', dict(session=session, **value))
        event(s.db, 'fault', session, value)


def recovered(s, session, page, source):
    value = {'page': page.key, 'version': page.version, 'hash': page.content_hash,
             'source': source, 'authority': page.authority}
    with s.db:
        s._audit('MEMORY_RESTORED', dict(session=session, **value))
        event(s.db, 'restored', session, value)


def _refs(payload):
    refs = [(p['id'], p['version'], p['hash']) for p in payload.get('pages', [])]
    memory = payload.get('memory') or {}
    for field in ('recent_turns', 'retrieved_turns'):
        refs += [(p['page'], p['version'], p['hash']) for p in memory.get(field, [])]
    # Workspace reviewers carry fresh excerpts backed by snapshot receipts,
    # rather than legacy pages. Track their exact text/span too, without making
    # this disposable index a source cache. Version zero is reserved for these
    # derived references; persisted Page versions begin at one.
    for path, text in payload.get('excerpts', {}).items():
        span = payload.get('excerpt_locations', {}).get(path)
        refs.append(('excerpt:' + path, 0, sha(canonical([text, span]))))
    return sorted(set(refs))


def _wire_refs(payload, envelope):
    """Describe delivered semantic units, never cache residency or comprehension.

    Read the final versioned model projection. Host-only receipts, trimmed notes
    and omitted source bodies must not appear as delivered context in telemetry.
    Unversioned legacy transports keep their historical diagnostic recipe.
    """
    if not envelope.get('model_interface'):
        return _refs(payload)
    from invocation_roles import expected_request
    from model_packet import build
    system, schema = expected_request(envelope['role'], payload)
    packet = build(system, payload, schema)
    refs = []
    def visit(value, address):
        if isinstance(value, dict):
            # Bodies, notes and directory nodes have separate observable units.
            if any(key in value for key in ('text', 'section_text', 'summary_unverified',
                                            'pinned_user_statements', 'answer_unverified', 'note', 'links',
                                            'toc', 'user', 'assistant_unverified')):
                identity = value.get('ref') or value.get('id') or value.get('path') or address
                refs.append(('wire:' + str(identity), 0, sha(canonical(value))))
            if address.endswith('/excerpts'):
                for path, text in value.items():
                    refs.append(('wire:excerpt:' + path, 0, sha(canonical(text))))
            for key, child in value.items():
                visit(child, address + '/' + key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, address + '/' + str(index))
    for index, message in enumerate(packet['messages']):
        if message.get('role') == 'system':
            continue
        try:
            body = json.loads(message['content'])
        except (TypeError, ValueError):
            body = {'text': message['content']}
        visit(body, str(index))
    return sorted(set(refs))


def _cached_refs(raw):
    # A disposable index is not trusted evidence. Damage limits diagnostic
    # history rather than aborting a governed call after inference succeeded.
    try:
        refs = json.loads(raw)
        if not isinstance(refs, list) or any(
                not isinstance(p, list) or len(p) != 3 or not isinstance(p[0], str)
                or type(p[1]) is not int or not isinstance(p[2], str) for p in refs):
            return None
        return {tuple(p) for p in refs}
    except (ValueError, TypeError):
        return None


def observe_delivery(db, rid):
    """Atomic with delivery_success. Only successful transport counts as page-in.

    Conversation predecessors follow receipt ancestry, so forks do not evict one
    another. Workspace work is isolated by session, role and orchestration node.
    Refaults are a lower bound within the bounded retained working-set history.
    """
    session, raw = db.execute('SELECT session,payload_json FROM deliveries WHERE id=?', (rid,)).fetchone()
    payload = json.loads(raw)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    env = {}
    if 'invocations' in tables:
        row = db.execute('SELECT record_json FROM invocations WHERE delivery_id=?', (rid,)).fetchone()
        if row:
            env = json.loads(row[0])
    try:
        refs = _wire_refs(payload, env)
    except Exception as exc:
        # Optional telemetry cannot revoke an otherwise successful delivery.
        # Do not count host-only context as a substitute for an unavailable
        # final-wire manifest, and never serialize arbitrary exception text.
        event(db, 'working_set_unavailable', session,
              {'delivery_id': rid, 'exception_type': type(exc).__name__, 'diagnostic_only': True})
        return
    owner = str(env.get('role', 'pages')) + ':' + str((env.get('orchestration') or {}).get('node_id') or '')
    if env.get('role') == 'conversation':
        owner = 'conversation'  # Receipt ancestry, rather than per-call node, isolates forks.
    previous = None
    if env.get('role') == 'conversation':
        parent = (payload.get('memory') or {}).get('invocation_id')
        if parent:
            row = db.execute('SELECT delivery_id FROM invocations WHERE id=? AND session=?', (parent, session)).fetchone()
            previous = row[0] if row else None
    else:
        row = db.execute('SELECT delivery_id FROM memory_working_sets WHERE session=? AND owner=? ORDER BY rowid DESC LIMIT 1',
                         (session, owner)).fetchone()
        previous = row[0] if row else None
    prior, seen, cursor, visited = set(), set(), previous, set()
    history_gap = False
    while cursor and cursor not in visited:
        visited.add(cursor)
        row = db.execute('SELECT previous_id,refs_json FROM memory_working_sets WHERE delivery_id=? AND session=? AND owner=?',
                         (cursor, session, owner)).fetchone()
        if not row:
            break
        page_refs = _cached_refs(row[1])
        if page_refs is None:
            history_gap = True
            break
        if cursor == previous:
            prior = page_refs
        seen |= page_refs
        cursor = row[0]
    current = set(refs)
    loaded, evicted = current - prior, prior - current
    refaults = loaded & seen
    counts = {'deliveries': 1, 'page_ins': len(loaded), 'evictions': len(evicted), 'refaults': len(refaults)}
    for name, amount in counts.items():
        db.execute('INSERT INTO memory_counters VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=value+excluded.value', (name, amount))
    db.execute('INSERT INTO memory_working_sets VALUES(?,?,?,?,?)', (rid, session, owner, previous, canonical(refs)))
    db.execute('DELETE FROM memory_working_sets WHERE rowid NOT IN (SELECT rowid FROM memory_working_sets ORDER BY rowid DESC LIMIT ?)',
               (LIMITS['working_sets'],))
    event(db, 'working_set', session, dict(delivery_id=rid, owner=owner, resident_pages=len(refs),
          **counts, thrashing_observed=len(refaults) >= 2, diagnostic_history_gap=history_gap))


def snapshot(db):
    """Read-only monitoring, including old stores without this schema."""
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result = {'version': POLICY['version'], 'retention': POLICY['retention'], 'counters': {}, 'events': [],
              'history_limit': LIMITS['working_sets'], 'diagnostic_only': True, 'diagnostic_issues': 0}
    if not {'memory_events', 'memory_counters', 'memory_working_sets'} <= tables:
        return result
    result['counters'] = {name:value for name, value in db.execute('SELECT name,value FROM memory_counters')
                          if type(value) is int and value >= 0}
    for at, kind, sid, raw in db.execute('SELECT at,kind,session,record_json FROM memory_events ORDER BY seq DESC LIMIT 12'):
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):raise ValueError('Malformed diagnostic')
            result['events'].append(dict(value, at=at, kind=kind, session=sid))
            if kind == 'working_set_unavailable':
                result['diagnostic_issues'] += 1
        except (ValueError, TypeError):
            result['diagnostic_issues'] += 1
    return result
