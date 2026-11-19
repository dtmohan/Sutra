"""Tool-free conversation through the invocation gateway; summaries are not authority."""
import json
import re
import uuid
import os
from context_manager import InvalidUpdate, canonical, sha
from invocation_gateway import InvocationGateway
from local_model import LocalModel
from paging import Budget, PageFault
from protocol import obj
from karma_adapter import KarmaAdapter
from review_budget import DEFAULTS
import visuals
import conversation_memory as cm
import conversation_routing as routing

from conversation_prompt import DOCUMENT_PERCENT_ERROR, SYSTEM
from review_targets import (document_review_requested, has_document_text, refers_to_previous_review_target,
                            refers_to_retained_document)

CONSTRAINTS = dict(DEFAULTS, conversation_only=True, network_allowed=False,
                   dependencies_allowed=False, edits_allowed=False, tests_allowed=False,
                   output_format='conversation-v1')

ROUTING_CAPABILITIES = {
    'version': 'conversation-handoff-v2',
    'stage': 'answer_or_request_handoff',
    'direct_tools': [],
    'workspace_handoff': {'available': True, 'same_chat': True,
                          'may_request_while_unarmed': True,
                          'execution_requires_host_authorization': True},
    'permissions_source': 'workspace_contract_and_host_grants',
    'batch_size_is_not_workspace_scope': True,
}


def check_capability_claims(proposal):
    """Catch known first-person host-capability refusals, not general semantics.

    A failed check requests bounded repair. It never routes, grants permissions,
    dispatches tools, or claims to recognize arbitrary natural-language intent.
    """
    if proposal['intent'] != 'conversation':
        return
    answer = re.sub(r'```[\s\S]*?```|`[^`\n]*`|"[^"\n]*"|“[^”\n]*”', '', proposal['answer'])
    answer = '\n'.join(line for line in answer.splitlines() if not line.lstrip().startswith('>'))
    answer = answer.replace('*', '').replace('’', "'").lower()
    denials = (
        r"\b(?:i|sutra) (?:cannot|can't|can not|am unable to)\b.{0,100}\b(?:read|inspect|access|traverse|review)\b.{0,100}\b(?:files|workspace|codebase|source|repository)\b",
        r"\b(?:this (?:conversation|chat|session|harness)|sutra)\b.{0,35}\b(?:has no|does not have|lacks)\b.{0,30}\b(?:file|workspace|filesystem)[- a-z]{0,40}\b(?:tools|access)\b",
        r"\bi (?:have no|don't have|do not have|lack)\b.{0,40}\b(?:file|workspace|filesystem)[- a-z]{0,40}\b(?:tools|access)\b",
    )
    for clause in re.split(r'[.!?\n]', answer):
        if re.search(r'\b(?:directly|outside|unarmed|budget|allocation|permission)\b|\bthis (?:stage|invocation|model call)\b', clause):
            continue  # Legitimate scope/stage limits are not global denials.
        if any(re.search(pattern, clause) for pattern in denials):
            raise InvalidUpdate('File work can use intent=workspace for a same-chat host handoff. Stage tool restrictions do not disable the harness.')


class ConversationModel(LocalModel):
    def messages(self, system, payload):
        from model_packet import build
        return build(system, payload, schema(payload))['messages']

    def output_schema(self, payload, host_schema):
        return model_schema(payload, host_schema)


def model_schema(p, host_schema):
    import copy
    fmt = copy.deepcopy(host_schema)
    if p.get('response_contract') == 2:
        fmt['properties'].pop('context_binding')
        fmt['required'].remove('context_binding')
    return fmt


def bind_output(p, proposal):
    """Attach host metadata only after validating the raw, task-facing proposal."""
    from invocation_roles import validate_shape
    validate_shape(proposal, model_schema(p, schema(p)))
    return dict(proposal, context_binding=p['context_binding'])


DOCUMENT_EVIDENCE_REF = ('key', 'version', 'digest')
PERCENT = re.compile(r'(?<![\d.])(\d+(?:\.\d+)?)\s*(?:%|percent\b)', re.IGNORECASE)
BARE_VERDICT = re.compile(r'\W*(?:yes|no|go|hold|no[- ]go|pass|fail|approved|rejected|unknown)\W*', re.I)
DOCUMENT_CITATION = re.compile(r'(?<![\w-])D\d+-E\d+(?![\w-])')


def document_citation_ids(proposal):
    prose = proposal['answer'] + '\n' + proposal.get('basis', '')
    return list(dict.fromkeys([*proposal.get('citation_ids', []), *DOCUMENT_CITATION.findall(prose)]))


def retained_document_reference(s, sid, parent, *, unique=False):
    turns = cm.history(s, sid, parent)
    if unique and len({turn['document_ref']['key'] for turn in turns if 'document_ref' in turn}) != 1:
        return None
    for turn in turns:
        if 'document_ref' in turn:
            return turn['document_ref']
        if not refers_to_retained_document(turn['user']):
            break
    return None


def document_evidence(s, sid, ref, prompt, parent=None):
    permitted = (set(DOCUMENT_EVIDENCE_REF), {*DOCUMENT_EVIDENCE_REF, 'retained_from'},
                 {*DOCUMENT_EVIDENCE_REF, 'retained_from', 'selected_by'})
    if not isinstance(ref, dict) or set(ref) not in permitted:
        raise InvalidUpdate('Invalid document evidence reference')
    key = ref['key']
    match = re.fullmatch(r'document-bridge:([a-f0-9]{32})', key) if isinstance(key, str) else None
    if not match or type(ref['version']) is not int or ref['version'] < 1 or not isinstance(ref['digest'], str):
        raise InvalidUpdate('Invalid document evidence reference')
    source_prompt = prompt
    if 'retained_from' in ref:
        base_ref = {field: ref[field] for field in DOCUMENT_EVIDENCE_REF}
        if (not parent or parent != ref['retained_from']
                or retained_document_reference(s, sid, parent) != base_ref):
            raise InvalidUpdate('Document evidence does not match the retained conversation context')
        if 'selected_by' in ref:
            from document_routing import verify_selection
            verify_selection(s, sid, ref, prompt, parent)
        elif not refers_to_retained_document(prompt):
            raise InvalidUpdate('Document evidence does not match the retained conversation context')
        from orchestration import node
        source_prompt = node(s, match[1])['prompt']
    try:
        doc = s.document(sid, key)
    except PageFault as exc:
        raise InvalidUpdate('Document evidence is unavailable in this session') from exc
    origin = json.loads(doc['source_json'])
    expected_source = {'flow_id': match[1], 'request_hash': sha(source_prompt), 'kind': 'evidence_bridge'}
    if doc['authority'] == 'tool' and origin.get('kind') == 'workspace_document':
        from document_review import verifies
        valid_source = (origin.get('flow_id') == match[1] and origin.get('request_hash') == sha(source_prompt)
                        and verifies(s, sid, origin))
    else:
        valid_source = doc['authority'] == 'user' and origin == expected_source
    if (doc['version'] != ref['version'] or doc['digest'] != ref['digest'] or sha(doc['text']) != doc['digest']
            or not doc['text'].strip() or not valid_source):
        raise InvalidUpdate('Document evidence changed or belongs to a different request')
    fields = ('session', 'key', 'version', 'text', 'digest', 'authority', 'source_json')
    commit = canonical({'record': [doc[field] for field in fields]})
    if not s.audit_valid() or not s.db.execute(
        "SELECT 1 FROM page_audit WHERE event='DOCUMENT_COMMIT' AND payload_json=?",
        (commit,),
    ).fetchone():
        raise InvalidUpdate('Document evidence has no valid commit receipt')
    return dict(ref, text=doc['text'])


def supplied_document(p):
    if 'document_evidence' in p or has_document_text(p['request']):
        return True
    memory = p.get('memory') or {}
    return refers_to_previous_review_target(p['request']) and any(
        turn['id'] == memory.get('invocation_id') and has_document_text(turn['user'])
        for turn in memory.get('recent_turns', [])
    )


def schema(p):
    if p.get('document_stage_flow'):
        ids = [item['id'] for item in p['document_synthesis']['evidence']]
        fmt = obj({
            'intent': {'type': 'string', 'enum': ['conversation']},
            'context_binding': {'type': 'string', 'enum': [p['context_binding']]},
            'answer': {'type': 'string', 'minLength': 1, 'maxLength': 6000},
            'summary': {'type': 'string', 'minLength': 1, 'maxLength': 450},
            'citation_ids': {'type': 'array', 'minItems': min(1, len(ids)), 'maxItems': min(24, len(ids)),
                             'items': {'type': 'string', 'enum': ids} if ids else {'type': 'string'}},
            'escalation_reason': {'type': 'string', 'enum': ['none']},
            'page_requests': {'type': 'array', 'maxItems': 0, 'items': {'type': 'string'}},
            'diagram': {'type': 'null'},
        })
        if p.get('document_review_version') == 2:
            fmt['properties']['basis'] = {'type': 'string', 'maxLength': 700}
            fmt['required'].append('basis')
        return fmt
    available = [r['id'] for r in (p.get('memory') or {}).get('toc', [])]
    quotations = list(dict.fromkeys(q for q in re.split(r'(?<=[.!?])\s+|\n+', p['request']) if q.strip()))
    intents = ['conversation']
    if not supplied_document(p):
        intents.append('document')
        if not document_review_requested(p['request']):
            intents.insert(1,'workspace')
    fmt = obj({'intent': {'type': 'string', 'enum': intents},
                'context_binding': {'type': 'string', 'enum': [p['context_binding']]},
                'answer': {'type': 'string', 'minLength': 1, 'maxLength': 6000},
                'summary': {'type': 'string', 'minLength': 1, 'maxLength': p['memory_allocation']['summary_max_chars']},
                'escalation_reason': {'type':'string','enum':['none','missing_context','reasoning_capacity']},
                'context_question': {'type': 'string', 'minLength': 1, 'maxLength': 500},
                'page_requests': {'type': 'array', 'maxItems': 4 if available else 0, 'items': dict(type='string', **({'enum': available} if available else {}))},
                'facts': {'type': 'array', 'maxItems': 8, 'items': {'type': 'string', 'enum': quotations}},
                'workspace_request': obj({'operation': {'type': 'string', 'enum': ['read', 'edit', 'test', 'capability']},
                    'scope': {'type': 'string', 'enum': ['workspace']},
                    'capability': {'type':'string','enum':['ruff','pyright']},
                    'capability_action': {'type':'string','enum':['install','check']},
                    'paths': {'type': 'array', 'maxItems': 8, 'items': {'type': 'string', 'minLength': 1, 'maxLength': 240}}}),
                'diagram': visuals.diagram_schema()})
    # Older providers/fixtures may omit advisory fields. Absence grants nothing.
    fmt['required'] = [k for k in fmt['required'] if k not in ('facts', 'workspace_request', 'context_question')]
    if p.get('document_offer'):
        selection = {'type': 'string', 'enum': ['previous_snapshot', 'none']}
        fmt['properties'] = {'document_request': selection, **fmt['properties']}
        fmt['required'].insert(0, 'document_request')
    fmt['properties']['workspace_request']['required']=['operation','paths']
    from task_observations import add_schema
    return add_schema(fmt, 'commitment')


def contract(s, sid):
    state = s.snapshot(sid)
    if state['constraints'] != CONSTRAINTS or state['artifacts'] or not s.validate_chain(sid):
        raise InvalidUpdate('Conversation policy changed or has workspace artifacts')
    from calibration import current
    return {'session_id': sid, 'revision': state['revision'], 'task_id': state['task_id'],
            'goal': state['goal'], 'constraints': state['constraints'],
            'contract_hash': s.origin(sid)['contract_hash'],
            'calibration_binding': current(s, sid, usable=True)}


def chat_axis(workspace, chat_key):
    return sha(canonical(['sutra-conversation-axis-v1', str(workspace), chat_key])) if chat_key else None


def legacy_receipt_axis(s, invocation):
    """Recover the originating axis of a receipt written before axis binding."""
    if not s.db.execute("SELECT 1 FROM sqlite_master WHERE name='orchestration_events'").fetchone():
        return None
    row = s.db.execute("""
        SELECT node_id, seq FROM orchestration_events
        WHERE json_extract(record_json,'$.data.conversation_id')=?
        ORDER BY rowid LIMIT 1
    """, (invocation,)).fetchone()
    if not row:
        return None
    from orchestration import events, node
    flow = node(s, row[0])
    owner = events(s, row[0])[row[1] - 1]
    if flow['kind'] != 'flow' or owner['data'].get('conversation_id') != invocation:
        raise InvalidUpdate('Cannot verify the originating Sutra chat')
    return chat_axis(flow['workspace'], flow.get('chat_key'))


def require_chat_axis(s, sid, rows, axis):
    seed = s.db.execute("""
        SELECT json_extract(raw_content, '$.conversation_axis') FROM events
        WHERE session=? AND revision=1 AND kind='conversation_contract' AND json_valid(raw_content)
    """, (sid,)).fetchone()
    if seed and seed[0] is not None and seed[0] != axis:
        raise InvalidUpdate('Conversation belongs to another Sutra chat')
    for row in rows:
        owner = row.get('conversation_axis') or legacy_receipt_axis(s, row['id'])
        if owner != axis:
            raise InvalidUpdate('Conversation memory belongs to another Sutra chat')


def open_session(s, prompt, axis=None):
    sid = 'conversation-' + uuid.uuid4().hex
    s.create(sid)
    values = dict(CONSTRAINTS, task_id=sid, goal='Answer conversation or request a governed workspace handoff; this stage executes no workspace tools.')
    # Fixed host restrictions, recorded honestly as host authority. This does not
    # create/select a workspace task and cannot give the model any permissions.
    origin = {'user_request': prompt, 'host_policy': 'conversation-v1'}
    if axis:
        origin['conversation_axis'] = axis
    s._append(sid, uuid.uuid4().hex, 'host', 'conversation_contract', canonical(origin),
              [{'key': k, 'value': v} for k, v in values.items()], 0)
    s.current_contract(sid)
    return sid


def workspace_context(s, meta):
    sid = meta.get('active')
    if not sid:
        return None
    state = s.snapshot(sid)
    if not s.validate_chain(sid):
        raise InvalidUpdate('Workspace task chain invalid')
    from calibration import current
    return {'session_id': sid, 'goal': state['goal'], 'constraints': state['constraints'],
            'contract_hash': s.origin(sid)['contract_hash'],
            'calibration_binding': current(s, sid, usable=True)}


def memory(s, sid, iid):
    prior = cm.history(s, sid, iid)[0]
    return {'invocation_id': iid, 'model': prior['model'], 'summary_unverified': prior['summary']}


def binding(p):
    fields = [p['contract'], p['workspace_context'], p['routing_capabilities']]
    for key in ('document_evidence', 'document_offer', 'conversation_axis', 'document_stage_flow',
                'document_synthesis', 'document_review_version'):
        if key in p:
            fields.append(p[key])
    return sha(canonical(fields))


def workspace_handoff(s, sid, iid, prompt):
    row = s.db.execute('SELECT session,record_json,delivery_id FROM invocations WHERE id=?', (iid,)).fetchone()
    if not row or json.loads(row[1])['role'] != 'conversation':
        raise InvalidUpdate('Workspace handoff needs an admitted conversation receipt')
    note = memory(s, row[0], iid)
    p = json.loads(s.db.execute('SELECT payload_json FROM deliveries WHERE id=?', (row[2],)).fetchone()[0])
    from native_access import has_binding
    if (p['workspace_context'] != workspace_context(s, {'active': sid}) and not has_binding(s, sid, iid, p)) or p['request'].strip() != prompt.strip():
        raise InvalidUpdate('Conversation handoff belongs to another request or workspace contract')
    from task_observations import commitment, context
    observation = context(commitment(s, iid))
    return dict(note, authority='model_unverified', **({'task_commitment': observation} if observation else {}))


def check_workspace_handoff(s, sid, p):
    note = p.get('conversation_handoff')
    if note and note != workspace_handoff(s, sid, note['invocation_id'], p['request']):
        raise InvalidUpdate('Workspace conversation note differs from admitted receipt')


def document_synthesis(s, sid, flow, ref, prompt, parent):
    from document_stages import committed_plan, synthesis_packet
    plan = committed_plan(s, sid, flow)
    if (plan['document_ref'] != ref or plan['prompt_hash'] != sha(prompt)
            or plan['parent'] != parent):
        raise InvalidUpdate('Document synthesis differs from its scoped request')
    evidence = synthesis_packet(s, sid, flow)
    if not evidence['evidence']:
        raise InvalidUpdate('Document synthesis requires supporting quotations from the approved document')
    return evidence


def request_check(s, sid, p):
    if p.get('response_contract') != 2:
        raise InvalidUpdate('Conversation response contract changed')
    if p.get('routing_capabilities') != ROUTING_CAPABILITIES:
        raise InvalidUpdate('Host routing capabilities changed')
    if p['contract'] != contract(s, sid) or p['pages'] or p['context_binding'] != binding(p):
        raise InvalidUpdate('Conversation contract or context binding mismatch')
    require_chat_axis(s, sid, cm.history(s, sid, p['routing_state']['parent']), p.get('conversation_axis'))
    if not isinstance(p['request'], str) or not 1 <= len(p['request']) <= 1800 or not p['request'].strip():
        raise InvalidUpdate('Conversation prompt must be 1..1800 characters')
    if 'document_offer' in p:
        from document_routing import offer_for_request
        offer = p['document_offer']
        if (not isinstance(offer, dict) or 'document_evidence' in p
                or offer != offer_for_request(s, sid, offer.get('flow_id'), p['request'], p['routing_state']['parent'])):
            raise InvalidUpdate('Invalid retained document offer')
    if 'document_stage_flow' in p or 'document_synthesis' in p:
        from document_stages import committed_plan
        version = committed_plan(s, sid, p.get('document_stage_flow'))['version']
        if p.get('document_review_version') != (version if version == 2 else None):
            raise InvalidUpdate('Document synthesis review policy differs from the committed plan')
        expected = document_synthesis(s, sid, p.get('document_stage_flow'), p.get('document_evidence'),
                                      p['request'], p['routing_state']['parent'])
        if p.get('document_synthesis') != expected:
            raise InvalidUpdate('Document synthesis evidence differs from its admitted receipts')
    elif 'document_review_version' in p:
        raise InvalidUpdate('Document review policy requires a staged document')
    elif 'document_evidence' in p:
        supplied = p['document_evidence']
        if not isinstance(supplied, dict) or any(key not in supplied for key in DOCUMENT_EVIDENCE_REF):
            raise InvalidUpdate('Invalid document evidence payload')
        ref = {key: supplied[key] for key in DOCUMENT_EVIDENCE_REF}
        ref.update({key: supplied[key] for key in ('retained_from', 'selected_by') if key in supplied})
        if supplied != document_evidence(s, sid, ref, p['request'], p['routing_state']['parent']):
            raise InvalidUpdate('Delivered document text differs from its evidence receipt')
    if not re.fullmatch('[a-f0-9]{32}', p['_request_nonce']):
        raise InvalidUpdate('Missing conversation request identity')
    # Only ledger metadata is inspected here. Never load repository pages/results.
    if p['workspace_context']:
        current = workspace_context(s, {'active': p['workspace_context']['session_id']})
        if current != p['workspace_context']:
            raise InvalidUpdate('Workspace task changed during conversation')
    allocation = p['memory_allocation']
    budget = Budget(allocation['context_limit'], allocation['response_reserve'], allocation['template_reserve'])
    if allocation['summary_max_chars'] != cm.summary_limit(budget):
        raise InvalidUpdate('Conversation memory allocation changed')
    if p['memory'] and p['memory'] != cm.working_set(s, sid, p['memory']['invocation_id'], p['request'], budget, p, SYSTEM, schema):
        raise InvalidUpdate('Conversation memory differs from admitted receipts')
    routing.check(s,sid,p)
    from reasoning_policy import check
    check(s,sid,p)
    expected_reference = visuals.reference(s, sid, (p['memory'] or {}).get('invocation_id'), p['request'])
    if p.get('diagram_reference') != expected_reference:
        raise InvalidUpdate('Diagram reference differs from user-source receipts')


def validate(s, sid, p, proposal):
    request_check(s, sid, p)
    check_capability_claims(proposal)
    check_delivery(p, proposal)
    visuals.validate(proposal['diagram'], proposal['intent'])
    if any(quote not in p['request'] for quote in proposal.get('facts', [])):
        raise InvalidUpdate('Remembered fact is not an exact quote from the current user message')
    return {'admission': {'admitted': True, 'errors': [], 'semantic_correctness_verified': False},
            'checks': ['schema', 'tool_free_role', 'contract', 'calibration_binding',
                       'context_binding', 'receipt_memory', 'route', 'host_capability_claims', 'diagram_structure', 'user_description_provenance'], 'provisional': True}


def check_delivery(p, proposal):
    """Check actionable structure, not whether arbitrary prose is correct."""
    answer = proposal['answer'].strip()
    if not answer or len(re.findall(r'^\s*```[^\n]*$', answer, re.M)) % 2:
        raise InvalidUpdate('Complete the answer and close every fenced code block; summary cannot finish the answer.')
    if p.get('document_evidence'):
        staged = p.get('document_synthesis')
        if staged is None:
            text = p['document_evidence']['text']
        else:
            evidence = staged['evidence']
            text = '\n'.join(item['quote'] for item in evidence)
            ids = proposal.get('citation_ids', [])
            basis = proposal.get('basis', '').strip()
            if p.get('document_review_version') == 2 and not basis and BARE_VERDICT.fullmatch(answer):
                raise InvalidUpdate('Explain the decisive evidence for the verdict in answer or basis. The summary is private.')
            answer = '\n'.join((answer, basis))
            cited = set(document_citation_ids(proposal))
            if cited - {item['id'] for item in evidence} or (evidence and not ids) or len(ids) != len(set(ids)):
                raise InvalidUpdate('Cite evidence using only the supplied document evidence IDs.')
        if set(PERCENT.findall(answer)) - set(PERCENT.findall(text)):
            raise InvalidUpdate(DOCUMENT_PERCENT_ERROR + ' Use placeholders such as [measured improvement].')
    pages = proposal.get('page_requests', [])
    work=proposal.get('workspace_request', {})
    if (proposal['intent']=='document' or supplied_document(p)) and 'workspace_request' in proposal:
        raise InvalidUpdate('A document request cannot propose workspace operations')
    if 'scope' in work and (proposal['intent'] != 'workspace' or work.get('operation') != 'read' or work.get('paths')):
        raise InvalidUpdate('Whole-workspace scope requires a workspace read and empty paths')
    if work.get('operation')=='capability' and not all(k in work for k in ('capability','capability_action')):
        raise InvalidUpdate('Capability requests require capability and capability_action')
    if work.get('operation')!='capability' and any(k in work for k in ('capability','capability_action')):
        raise InvalidUpdate('Capability fields require a capability operation')
    if (proposal.get('workspace_request', {}).get('operation') in ('edit', 'test', 'capability')
            and proposal['intent'] != 'workspace'):
        raise InvalidUpdate('An edit/test workspace_request needs intent=workspace for host handoff; omit it for conversation.')
    if len(pages) != len(set(pages)):
        raise InvalidUpdate('Request each memory page only once.')
    question = proposal.get('context_question', '').strip()
    if proposal.get('document_request') == 'previous_snapshot' and (
            proposal['intent'] != 'document' or not p.get('document_offer') or pages or question
            or proposal['escalation_reason'] != 'none'):
        raise InvalidUpdate('Select the offered snapshot with document intent and omit competing context or routing requests.')
    if proposal.get('context_question') is not None and not question:
        raise InvalidUpdate('context_question must ask a specific question.')
    if pages and question:
        raise InvalidUpdate('Page available context first; ask context_question only when no relevant page is available.')
    if (proposal['intent'] == 'conversation' and proposal['escalation_reason'] == 'missing_context'
            and (p.get('memory') or {}).get('toc') and not pages and not question):
        raise InvalidUpdate('Set top-level page_requests from the TOC, or context_question if no listed page contains the missing evidence.')


def read(s, iid, *, staged_candidate=False):
    if not isinstance(iid, str) or not re.fullmatch('[a-f0-9]{32}', iid):
        raise InvalidUpdate('Invalid conversation receipt')
    row = s.db.execute("""SELECT i.session,i.record_json,d.payload_json FROM invocations i
                          JOIN deliveries d ON i.delivery_id=d.id AND i.session=d.session
                          WHERE i.id=?""", (iid,)).fetchone()
    if not row or json.loads(row[1])['role'] != 'conversation':
        raise InvalidUpdate('Unknown conversation receipt')
    packet = json.loads(row[2])
    verification = None
    if packet.get('document_review_version') == 2 and not staged_candidate:
        from document_stages import publication
        verification = publication(s, packet, iid)
    proposal = s.load_result(row[0], iid)
    from token_usage import history
    usage = history(s.db, run_id=json.loads(row[1])['run_id'])
    result = {'session': row[0], 'route': {'model': json.loads(row[1])['model']},
              'result': {'token_usage': usage, 'invocation_id': iid, 'admission': {'admitted': True}, 'accepted_output': proposal}}
    if verification:
        result['document_verification'] = verification
        result['result']['admission']['semantic_correctness_verified'] = False
        memory = packet['memory']
        result.update(memory_allocation=packet['memory_allocation'], model_capacity=packet['model_capacity'],
                      memory_status={key: memory[key] for key in ('stored_turns', 'omitted_turns', 'legacy_data_restored')}
                      if memory else {'stored_turns': 0, 'omitted_turns': 0}, reasoning=packet['reasoning'])
        from review_budget import decision
        result['budget'] = decision(s, row[0])
    if 'document_synthesis' in packet:
        evidence = packet['document_synthesis']
        spans = {section['id']: section for section in evidence['sections']}
        result['document_citations'] = [
            {'id': item['id'], 'quote': item['quote'],
             'start_line': spans[item['section']]['start_line'], 'end_line': spans[item['section']]['end_line']}
            for item in evidence['evidence'] if item['id'] in document_citation_ids(proposal)
        ]
    return result


def session_for(s,request,*,axis=None):
    prompt=request.get('prompt')
    if not isinstance(prompt,str) or not 1<=len(prompt)<=1800 or not prompt.strip():
        raise InvalidUpdate('Conversation prompt must be 1..1800 characters')
    parent=request.get('parent')
    sid=request.get('conversation_session')
    rows = []
    if parent:
        if not isinstance(parent,str) or not re.fullmatch('[a-f0-9]{32}',parent):
            raise InvalidUpdate('Invalid conversation receipt reference')
        row=s.db.execute('SELECT session FROM invocations WHERE id=?',(parent,)).fetchone()
        if not row:raise PageFault('Conversation memory unavailable. Start a new chat; workspace task is retained.')
        if sid and sid!=row[0]:raise InvalidUpdate('Conversation session changed during routing')
        sid=row[0];rows=cm.history(s,sid,parent)
    if sid:
        contract(s,sid)
        require_chat_axis(s,sid,rows,axis)
    return sid or open_session(s,prompt,axis)


def answer(s,meta,root,request,provider=None,*,axis=None,orchestrated=False):
    request=dict(request)
    if request.get('document_stage_flow') and not orchestrated:
        raise InvalidUpdate('Staged document synthesis requires the workflow')
    request['conversation_session']=session_for(s,request,axis=axis)
    calls=[]
    while True:
        result=_answer_step(s,meta,root,request,provider,axis)
        calls.extend(result['result'].get('token_usage',{}).get('calls',[]))
        from token_usage import summarize
        result['result']['token_usage']={'calls':calls,'totals':summarize(calls),'total_calls':len(calls),'limit':None}
        next_step=result.get('routing_followup')
        if request.get('one_step') or not next_step:
            return result
        if next_step['kind']=='blocked':
            result['result']['admission']={'admitted':False,'errors':[next_step['reason']]}
            result['result']['accepted_output']=None
            return result
        request['routing_continuation']=result['result']['invocation_id']


def _answer_step(s,meta,root,request,provider,axis):
    prompt=request['prompt'];parent=request.get('parent');sid=request['conversation_session']
    route,routing_state=routing.prepare(s,sid,request)
    p = {'response_contract': 2, '_request_nonce': uuid.uuid4().hex, 'contract': contract(s, sid),
         'workspace_context': workspace_context(s, meta), 'request': prompt,
         'routing_capabilities': json.loads(canonical(ROUTING_CAPABILITIES)),
         'pages': [], 'memory': None, 'route': route,'routing_state':routing_state,
         'requested_memory_pages':routing_state['requested_pages']}
    if axis:
        p['conversation_axis'] = axis
    p['diagram_reference'] = visuals.reference(s, sid, parent, prompt)
    from capabilities import context as tool_context
    p['tool_evidence']=tool_context(s,meta.get('active'))
    if request.get('document_stage_flow'):
        from document_stages import committed_plan
        p['document_stage_flow'] = request['document_stage_flow']
        p['document_evidence'] = request['document_evidence']
        if committed_plan(s, sid, p['document_stage_flow'])['version'] == 2:
            p['document_review_version'] = 2
        p['document_synthesis'] = document_synthesis(s, sid, p['document_stage_flow'],
                                                     p['document_evidence'], prompt, parent)
    elif 'document_evidence' in request:
        p['document_evidence'] = document_evidence(s, sid, request['document_evidence'], prompt, parent)
    elif 'document_offer_flow' in request:
        from document_routing import offer_for_request
        p['document_offer'] = offer_for_request(s, sid, request['document_offer_flow'], prompt, parent)
    p['context_binding'] = binding(p)
    real = provider is None
    if real:
        from api_runtime import provider_for
        provider = provider_for(root, route['model'], review=True, model_class=ConversationModel)
        from model_context import allocation
        provider.budget, capacity = allocation(getattr(provider, 'context_metadata', {}))
        provider.keep_alive = 0
    if isinstance(provider,dict):provider=provider[route['model']]
    if provider.model != route['model']:
        raise InvalidUpdate('Conversation provider differs from route')
    from reasoning_policy import prepare, capabilities
    preference = os.environ.get('SUTRA_THINKING', 'adaptive')
    cap = capabilities(getattr(provider, 'context_metadata', {}))
    p['reasoning'] = prepare(s, sid, p, cap, preference)
    budget = provider.budget
    p['memory_allocation'] = {'context_limit': budget.context_limit, 'response_reserve': budget.response_reserve,
                              'template_reserve': budget.template_reserve, 'summary_max_chars': cm.summary_limit(budget)}
    p['model_capacity'] = capacity if real else {'effective_context_tokens': budget.context_limit, 'source': 'injected_provider'}
    cm.materialize(s, sid, cm.history(s, sid, parent))
    p['memory'] = cm.working_set(s, sid, parent, prompt, budget, p, SYSTEM, schema)
    provider.thinking = p['reasoning']['requested']
    fmt = schema(p)
    provider.budget.check(SYSTEM, dict(p, repair={'errors': ['x' * 140] * 2}), fmt)
    run = s.begin_run(sid, prompt, [], None, max_calls=3, max_repairs=2)
    for attempt in range(3):
        s.reserve(run, 'ATTEMPT', {'attempt': attempt})
        if real:
            from monitoring import require_resources
            hardware = require_resources(root)
            with s.db:
                s._audit('RESOURCE_GATE', {'run_id': run, 'measurement': hardware})
        delivery = s.prepare_delivery(sid, p, provider.budget.check(SYSTEM, p, fmt))
        out = InvocationGateway(s.path, run, provider, ledger=s).invoke('conversation', uuid.uuid4().hex, SYSTEM, p, fmt, delivery)
        if out['admission']['admitted']:
            next_step=routing.followup(out['accepted_output'],p)
            if next_step:s.event(run,'BRIDGE',dict(next_step,invocation_id=out['invocation_id']))
            s.event(run,'ALLOW',{'reason':'routing_checkpoint' if next_step else 'conversation_admitted','invocation_id':out['invocation_id']})
            break
        s.event(run, 'NACK', {'errors': out['admission']['errors']})
        if attempt == 2 or out.get('blocked'):
            s.event(run, 'FIN', {'reason': 'conversation_repair_exhausted'})
            break
        request_check(s, sid, p)
        s.event(run, 'BRIDGE', {'reason': 'bounded_conversation_repair'})
        p['repair'] = {'errors': [e[:140] for e in out['admission']['errors'][:2]]}
    next_step=routing.followup(out['accepted_output'],p) if out['admission']['admitted'] else None
    out.pop('record', None)
    out.pop('model_proposal', None)
    from review_budget import decision
    return {'conversation': True, 'session': sid, 'result': out, 'route': route,'routing_followup':next_step,
            'reasoning': p['reasoning'],
            'memory_used': bool(parent), 'memory_allocation': p['memory_allocation'], 'model_capacity': p['model_capacity'],
            'memory_status': {k: p['memory'][k] for k in ('stored_turns', 'omitted_turns', 'legacy_data_restored')} if p['memory'] else {'stored_turns': 0, 'omitted_turns': 0}, 'budget': decision(s, sid), 'workspace_tools_used': False}
