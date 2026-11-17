"""Host measurements for admitted handoff targets and missing evidence."""

import json

from context_manager import InvalidUpdate, sha
from karma_adapter import KarmaAdapter
from review_budget import usage


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


def record_document(store, session, flow, text):
    key = 'document-bridge:' + flow['id']
    origin = {'kind': 'evidence_bridge', 'flow_id': flow['id'], 'request_hash': sha(flow['prompt'])}
    if store.db.execute('SELECT 1 FROM document_heads WHERE session=? AND key=?', (session, key)).fetchone():
        # A bridge may have committed the document before being interrupted.
        record = store.document(session, key)
        if (record['version'] != 1 or record['text'] != text or record['digest'] != sha(text)
                or record['authority'] != 'user' or json.loads(record['source_json']) != origin):
            raise InvalidUpdate('A different document was already committed to this request')
    else:
        record = store.put_document(session, key, text, authority='user', source=origin)
    return {field: record[field] for field in ('key', 'version', 'digest')}
