"""Bounded diagram data and selective paging of user-supplied descriptions."""
import json
import re
from context_manager import InvalidUpdate, canonical, sha
from protocol import obj


def diagram_schema():
    text = lambda n: {'type': 'string', 'maxLength': n}
    return dict(obj({
        'title': dict(text(100), minLength=1),
        'direction': {'type': 'string', 'enum': ['LR', 'TB']},
        'nodes': {'type': 'array', 'minItems': 1, 'maxItems': 16,
                  'items': obj({'id': dict(text(12), minLength=1), 'label': dict(text(100), minLength=1),
                                'kind': {'type': 'string', 'enum': ['source', 'device', 'process', 'output']}})},
        'edges': {'type': 'array', 'maxItems': 24,
                  'items': obj({'source': dict(text(12), minLength=1), 'target': dict(text(12), minLength=1), 'label': text(60)})},
        'note': text(240)}), type=['object', 'null'])


def validate(diagram, intent):
    if diagram is None:
        return
    if intent != 'conversation':
        raise InvalidUpdate('Workspace diagrams must use source-backed workspace analysis')
    ids = [n['id'] for n in diagram['nodes']]
    if len(ids) != len(set(ids)) or any(not re.fullmatch('n[0-9]{1,3}', n) for n in ids):
        raise InvalidUpdate('Diagram node IDs must be unique n1, n2, ...')
    for e in diagram['edges']:
        if e['source'] not in ids or e['target'] not in ids:
            raise InvalidUpdate('Diagram relationship has a missing endpoint')
    if len({(e['source'], e['target'], e['label']) for e in diagram['edges']}) != len(diagram['edges']):
        raise InvalidUpdate('Duplicate diagram relationship')
    labels = [diagram['title'], diagram['note']] + [n['label'] for n in diagram['nodes']] + [e['label'] for e in diagram['edges']]
    if any(re.search(r'[\x00-\x1f\x7f]', t) for t in labels):
        raise InvalidUpdate('Diagram labels cannot contain control characters')


def reference(s, sid, parent, prompt):
    """Page one exact chain description plus later explicit corrections, not history.

    The caller has already validated the admitted parent and its ancestry. These
    excerpts are user descriptions, not verified device specs or permissions.
    """
    cursor = parent
    newer = [(None, prompt)]
    while cursor:
        row = s.db.execute('SELECT delivery_id,session,record_json FROM invocations WHERE id=?', (cursor,)).fetchone()
        if not row or row[1] != sid or json.loads(row[2])['role'] != 'conversation':
            raise InvalidUpdate('Diagram reference ancestry mismatch')
        payload = json.loads(s.db.execute('SELECT payload_json FROM deliveries WHERE id=?', (row[0],)).fetchone()[0])
        newer.append((cursor, payload['request']))
        cursor = (payload.get('memory') or {}).get('invocation_id')
    selected = next((i for i, (_, text) in enumerate(newer) if len(re.findall(r'->|→', text)) >= 2), None)
    if selected is None:
        return None
    iid, text = newer[selected]
    corrections = [{'invocation_id': i, 'text': t} for i, t in reversed(newer[:selected])
                   if re.search(r'\b(correction|actually|instead|does not|doesn.t|not have)\b', t, re.I)][-2:]
    return {'authority': 'user_description_unverified', 'invocation_id': iid,
            'text': text, 'sha256': sha(text), 'later_corrections': corrections}
