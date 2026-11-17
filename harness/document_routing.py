"""Bind a classifier's snapshot choice to its committed host offer."""
import json
import re

from context_manager import InvalidUpdate, sha
from orchestration import events, node
from paging import PageFault
from review_targets import may_use_retained_document


REF_FIELDS = {'key', 'version', 'digest', 'retained_from'}


def offer_for_request(s, sid, flow_id, prompt, parent):
    if not isinstance(flow_id, str) or not re.fullmatch(r'[a-f0-9]{32}', flow_id):
        raise InvalidUpdate('Invalid document offer workflow')
    flow = node(s, flow_id)
    if (flow.get('kind') != 'flow' or not flow.get('chat_key') or not flow.get('workspace')
            or not may_use_retained_document(prompt)
            or not parent or flow.get('conversation_parent') != parent or flow.get('prompt') != prompt):
        raise InvalidUpdate('Document offer belongs to a different request or conversation')
    offers = [event['data'] for event in events(s, flow_id)
              if event['kind'] == 'DOCUMENT_OFFERED' and event['to'] == 'CLASSIFYING']
    if len(offers) != 1 or offers[0].get('conversation_session') != sid:
        raise InvalidUpdate('No unique document offer for this conversation session')
    ref = offers[0].get('document_offer')
    if not isinstance(ref, dict) or set(ref) != REF_FIELDS or ref['retained_from'] != parent:
        raise InvalidUpdate('Invalid offered document reference')
    key = ref['key']
    origin = re.fullmatch(r'document-bridge:([a-f0-9]{32})', key) if isinstance(key, str) else None
    if not origin:
        raise InvalidUpdate('Invalid offered document origin')
    source = node(s, origin[1])
    if (source.get('kind') != 'flow' or source.get('chat_key') != flow['chat_key']
            or source.get('workspace') != flow['workspace']):
        raise InvalidUpdate('Offered document is outside the originating chat or workspace')
    from conversation import retained_document_reference
    try:
        retained = retained_document_reference(s, sid, parent, unique=True)
    except PageFault as exc:
        raise InvalidUpdate('Offered document ancestry is unavailable') from exc
    if retained != {k: v for k, v in ref.items() if k != 'retained_from'}:
        raise InvalidUpdate('Offered document does not match retained conversation evidence')
    return {'flow_id': flow_id, 'ref': ref}


def verify_selection(s, sid, ref, prompt, parent):
    if not isinstance(ref, dict) or set(ref) != REF_FIELDS | {'selected_by'}:
        raise InvalidUpdate('Invalid selected document reference')
    selected_by = ref['selected_by']
    if not isinstance(selected_by, str) or not re.fullmatch(r'[a-f0-9]{32}', selected_by):
        raise InvalidUpdate('Invalid document selection receipt')
    row = s.db.execute('''
        SELECT d.payload_json, d.payload_hash, json_extract(r.record_json,'$.payload_hash')
        FROM invocations i JOIN deliveries d ON d.id=i.delivery_id
        JOIN invocation_receipts r ON r.invocation_id=i.id AND r.session=i.session
        WHERE i.id=? AND i.session=? AND json_extract(i.record_json,'$.role')='conversation'
    ''', (selected_by, sid)).fetchone()
    if not row or sha(row[0]) != row[1] or row[1] != row[2]:
        raise InvalidUpdate('Document selection has no verified conversation delivery')
    payload = json.loads(row[0])
    offered = payload.get('document_offer')
    if (payload.get('request') != prompt or (payload.get('routing_state') or {}).get('parent') != parent
            or not isinstance(offered, dict)):
        raise InvalidUpdate('Document selection belongs to a different request or conversation')
    try:
        proposal = s.load_result(sid, selected_by)
    except PageFault as exc:
        raise InvalidUpdate('Document selection was not accepted') from exc
    if proposal.get('intent') != 'document' or proposal.get('document_request') != 'previous_snapshot':
        raise InvalidUpdate('Conversation receipt did not select the offered document')
    verified = offer_for_request(s, sid, offered.get('flow_id'), prompt, parent)
    if verified != offered or verified['ref'] != {k: v for k, v in ref.items() if k != 'selected_by'}:
        raise InvalidUpdate('Selected document does not match the committed offer')
