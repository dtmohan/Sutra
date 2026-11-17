"""One approved workspace document read for a saved conversation."""
import hashlib
import json
import uuid

from context_manager import InvalidUpdate, canonical, sha
from evidence_admission import record_document
from review_budget import require

READ_SECONDS = 5
RESERVED = 'WORKSPACE_DOCUMENT_READ_RESERVED'


def approve(store, session, workspace, flow, path, size):
    from document_source import MAX_DOCUMENT_BYTES, read_document
    from document_text import extract_docx

    if type(size) is not int or not 1 <= size <= MAX_DOCUMENT_BYTES:
        raise InvalidUpdate('The DOCX is empty or exceeds the 512 KiB document read limit.')
    # Recover a committed extraction if the process stopped before the workflow
    # transition. The existing evidence remains bound to its approved path.
    key = 'document-bridge:' + flow['id']
    if store.db.execute('SELECT 1 FROM document_heads WHERE session=? AND key=?', (session, key)).fetchone():
        doc = store.document(session, key)
        origin = json.loads(doc['source_json'])
        if origin.get('kind') != 'workspace_document' or origin.get('path') != path:
            raise InvalidUpdate('This request already has evidence from a different source.')
        ref = {field: doc[field] for field in ('key', 'version', 'digest')}
        from conversation import document_evidence
        document_evidence(store, session, ref, flow['prompt'])
        return ref

    reservation = {'session': session, 'flow_id': flow['id'], 'path': path, 'bytes': size,
                   'request_hash': sha(flow['prompt']), 'id': uuid.uuid4().hex}
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        require(store, session, {'operations': 1, 'source_bytes': size, 'seconds': READ_SECONDS})
        store._audit(RESERVED, reservation)
    raw = read_document(workspace, path, limit=size)
    if len(raw) != size:
        raise InvalidUpdate('The file size changed after selection. Locate it again before reading.')
    text = extract_docx(raw)
    return record_document(store, session, flow, text, read={
        'path': path, 'bytes': size, 'read_id': reservation['id'],
        'source_sha256': hashlib.sha256(raw).hexdigest()})


def verifies(store, session, origin):
    reservation = {'session': session, 'flow_id': origin.get('flow_id'), 'path': origin.get('path'),
                   'bytes': origin.get('bytes'), 'request_hash': origin.get('request_hash'), 'id': origin.get('read_id')}
    digest = origin.get('source_sha256')
    return bool(isinstance(digest, str) and len(digest) == 64 and all(c in '0123456789abcdef' for c in digest)
                and store.db.execute(
        'SELECT 1 FROM page_audit WHERE event=? AND payload_json=?', (RESERVED, canonical(reservation))).fetchone())
