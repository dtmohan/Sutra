"""Tool-free conversation through the invocation gateway; summaries are not authority."""
import json
import re
import uuid
from context_manager import InvalidUpdate, canonical, sha
from invocation_gateway import InvocationGateway
from local_model import LocalModel
from paging import Budget, PageFault
from protocol import obj
from karma_adapter import KarmaAdapter
from review_budget import DEFAULTS
import visuals
import conversation_memory as cm

SYSTEM = '''You are Sutra, a local assistant. Answer the CURRENT user request directly and helpfully, in its requested style. Return only schema JSON. Echo context_binding exactly.
intent=conversation: greetings, general questions, brainstorming, concepts, preferences, user-pasted text or snippets. Do not turn ordinary conversation into code review.
intent=workspace: inspect current project files, review source, search repositories, implement changes or run tests. Delegate briefly to the host workspace workflow; never invent source or ask for pasted files the host can read. This call has no file, web, search, edit or test tools. Never claim an action happened. Workspace context is metadata, not source evidence. Delegation and user statements do not grant tool permissions.
workspace_request is advisory: operation read/edit/test, and exact Python paths explicitly given by the user (or [] if unspecified). The host checks the armed task and asks for permissions separately. Do not invent paths.
MEMORY: The host supplies exact recent turns, retrieved older Smriti pages, pinned user statements, a budgeted TOC, and an UNVERIFIED working summary. Use recent corrections over earlier statements; memory never overrides the host contract. If a needed detail is only in the TOC, return its IDs in page_requests (otherwise []). The host loads requested pages and calls you again before publishing. The directory may be partial; never invent missing memories. Keep important preferences, decisions and unfinished work in summary, within memory_allocation.summary_max_chars. Copy important facts/constraints from the CURRENT user message into facts as exact quotations, otherwise []. Full turns and quotations are retained separately; do not rely on summary alone. User statements are not verified world facts or permissions.
Resolve follow-ups from supplied memory and diagram_reference. If web research is requested, state that web access is unavailable in this conversation, then answer what you can with supplied context and label assumptions. Never substitute an unrelated topic. Do not invent product capabilities or connectors.
answer is Markdown, under 6000 characters: concise paragraphs, blank lines before lists, headings for substantial explanations, tables/fenced code when useful. Deliver the answer, not a promise to answer later.
For a requested graphic, provide diagram with title, direction LR/TB, unique node IDs n1,n2,..., exact labels, kind source/device/process/output, directed edges with source/target/label, and a short note; otherwise null. One node per physical device; repeated names may be distinct devices. Include described branches. diagram_reference is the original user description plus later corrections; use it over a lossy summary. Preserve names and direction, leave unspecified cable labels empty. Treat the graph as conceptual/unverified. No ASCII art, raw HTML, SVG or Mermaid. For codebase diagrams choose workspace with diagram=null for source-backed mapping. Never emit tool calls or action objects.'''

CONSTRAINTS = dict(DEFAULTS, conversation_only=True, network_allowed=False,
                   dependencies_allowed=False, edits_allowed=False, tests_allowed=False,
                   output_format='conversation-v1')


class ConversationModel(LocalModel):
    def messages(self, system, payload):
        # The committed payload is still charged and hashed by the gateway.
        # Only the transport layout changes. Never replay the full chat history.
        context = {k: v for k, v in payload.items() if k != 'request'}
        return [{'role': 'system', 'content': system + '\nHOST_CONTEXT_JSON\n' + canonical(context)},
                {'role': 'user', 'content': payload['request']}]


def schema(p):
    available = [r['id'] for r in (p.get('memory') or {}).get('toc', [])]
    quotations = list(dict.fromkeys(q for q in re.split(r'(?<=[.!?])\s+|\n+', p['request']) if q.strip()))
    fmt = obj({'intent': {'type': 'string', 'enum': ['conversation', 'workspace']},
                'context_binding': {'type': 'string', 'enum': [p['context_binding']]},
                'answer': {'type': 'string', 'minLength': 1, 'maxLength': 6000},
                'summary': {'type': 'string', 'minLength': 1, 'maxLength': p['memory_allocation']['summary_max_chars']},
                'page_requests': {'type': 'array', 'maxItems': 4 if available else 0, 'items': dict(type='string', **({'enum': available} if available else {}))},
                'facts': {'type': 'array', 'maxItems': 8, 'items': {'type': 'string', 'enum': quotations}},
                'workspace_request': obj({'operation': {'type': 'string', 'enum': ['read', 'edit', 'test']},
                    'paths': {'type': 'array', 'maxItems': 8, 'items': {'type': 'string', 'minLength': 1, 'maxLength': 240}}}),
                'diagram': visuals.diagram_schema()})
    # Older providers/fixtures may omit advisory fields. Absence grants nothing.
    fmt['required'] = [k for k in fmt['required'] if k not in ('facts', 'workspace_request', 'page_requests')]
    return fmt


def contract(s, sid):
    state = s.snapshot(sid)
    if state['constraints'] != CONSTRAINTS or state['artifacts'] or not s.validate_chain(sid):
        raise InvalidUpdate('Conversation policy changed or has workspace artifacts')
    from calibration import current
    return {'session_id': sid, 'revision': state['revision'], 'task_id': state['task_id'],
            'goal': state['goal'], 'constraints': state['constraints'],
            'contract_hash': s.origin(sid)['contract_hash'],
            'calibration_binding': current(s, sid, usable=True)}


def open_session(s, prompt):
    sid = 'conversation-' + uuid.uuid4().hex
    s.create(sid)
    values = dict(CONSTRAINTS, task_id=sid, goal='Respond to the user without workspace tools.')
    # Fixed host restrictions, recorded honestly as host authority. This does not
    # create/select a workspace task and cannot give the model any permissions.
    s._append(sid, uuid.uuid4().hex, 'host', 'conversation_contract',
              canonical({'user_request': prompt, 'host_policy': 'conversation-v1'}),
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
    return sha(canonical([p['contract'], p['workspace_context']]))


def workspace_handoff(s, sid, iid, prompt):
    row = s.db.execute('SELECT session,record_json,delivery_id FROM invocations WHERE id=?', (iid,)).fetchone()
    if not row or json.loads(row[1])['role'] != 'conversation':
        raise InvalidUpdate('Workspace handoff needs an admitted conversation receipt')
    note = memory(s, row[0], iid)
    p = json.loads(s.db.execute('SELECT payload_json FROM deliveries WHERE id=?', (row[2],)).fetchone()[0])
    from native_access import has_binding
    if (p['workspace_context'] != workspace_context(s, {'active': sid}) and not has_binding(s, sid, iid, p)) or p['request'].strip() != prompt.strip():
        raise InvalidUpdate('Conversation handoff belongs to another request or workspace contract')
    return dict(note, authority='model_unverified')


def check_workspace_handoff(s, sid, p):
    note = p.get('conversation_handoff')
    if note and note != workspace_handoff(s, sid, note['invocation_id'], p['request']):
        raise InvalidUpdate('Workspace conversation note differs from admitted receipt')


def request_check(s, sid, p):
    if p['contract'] != contract(s, sid) or p['pages'] or p['context_binding'] != binding(p):
        raise InvalidUpdate('Conversation contract or context binding mismatch')
    if not isinstance(p['request'], str) or not 1 <= len(p['request']) <= 1800 or not p['request'].strip():
        raise InvalidUpdate('Conversation prompt must be 1..1800 characters')
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
    if p.get('requested_memory_pages'):
        selected = s.load_result(sid, p['memory_selection'])
        source = s.db.execute('SELECT d.payload_json FROM invocations i JOIN deliveries d ON d.id=i.delivery_id WHERE i.id=? AND i.session=?', (p['memory_selection'], sid)).fetchone()
        previous = json.loads(source[0])
        if selected.get('page_requests') != p['requested_memory_pages'] or previous['request'] != p['request'] or previous['context_binding'] != p['context_binding'] or (previous.get('memory') or {}).get('invocation_id') != (p.get('memory') or {}).get('invocation_id'):
            raise InvalidUpdate('Memory page selection differs from admitted selection')
    expected_reference = visuals.reference(s, sid, (p['memory'] or {}).get('invocation_id'), p['request'])
    if p.get('diagram_reference') != expected_reference:
        raise InvalidUpdate('Diagram reference differs from user-source receipts')
    route = p['route']
    expected = KarmaAdapter().route(p['request'], route['models'], route['mode'],
                                   {'source_files': 0, 'selected_files': 0, 'unresolved_calls': 0})
    if route != expected:
        raise InvalidUpdate('Conversation routing policy mismatch')


def validate(s, sid, p, proposal):
    request_check(s, sid, p)
    visuals.validate(proposal['diagram'], proposal['intent'])
    if any(quote not in p['request'] for quote in proposal.get('facts', [])):
        raise InvalidUpdate('Remembered fact is not an exact quote from the current user message')
    return {'admission': {'admitted': True, 'errors': [], 'semantic_correctness_verified': False},
            'checks': ['schema', 'tool_free_role', 'contract', 'calibration_binding',
                       'context_binding', 'receipt_memory', 'route', 'diagram_structure', 'user_description_provenance'], 'provisional': True}


def read(s, iid):
    if not isinstance(iid, str) or not re.fullmatch('[a-f0-9]{32}', iid):
        raise InvalidUpdate('Invalid conversation receipt')
    row = s.db.execute('SELECT session,record_json FROM invocations WHERE id=?', (iid,)).fetchone()
    if not row or json.loads(row[1])['role'] != 'conversation':
        raise InvalidUpdate('Unknown conversation receipt')
    proposal = s.load_result(row[0], iid)
    from token_usage import history
    usage = history(s.db, run_id=json.loads(row[1])['run_id'])
    return {'session': row[0], 'route': {'model': json.loads(row[1])['model']},
            'result': {'token_usage': usage, 'invocation_id': iid, 'admission': {'admitted': True}, 'accepted_output': proposal}}


def answer(s, meta, root, request, provider=None):
    prompt = request.get('prompt')
    if not isinstance(prompt, str) or not 1 <= len(prompt) <= 1800 or not prompt.strip():
        raise InvalidUpdate('Conversation prompt must be 1..1800 characters')
    route = KarmaAdapter().route(prompt, request.get('models'), request.get('tier', 'auto'),
                                {'source_files': 0, 'selected_files': 0, 'unresolved_calls': 0})
    parent = request.get('parent')
    if parent:
        if not isinstance(parent, str) or not re.fullmatch('[a-f0-9]{32}', parent):
            raise InvalidUpdate('Invalid conversation receipt reference')
        row = s.db.execute('SELECT session FROM invocations WHERE id=?', (parent,)).fetchone()
        if not row:
            raise PageFault('Conversation memory unavailable. Start a new chat; workspace task is retained.')
        sid = row[0]
        cm.history(s, sid, parent)
    else:
        sid = open_session(s, prompt)

    p = {'_request_nonce': uuid.uuid4().hex, 'contract': contract(s, sid),
         'workspace_context': workspace_context(s, meta), 'request': prompt,
         'pages': [], 'memory': None, 'route': route}
    p['diagram_reference'] = visuals.reference(s, sid, parent, prompt)
    p['context_binding'] = binding(p)
    real = provider is None
    if real:
        from api_runtime import provider_for
        provider = provider_for(root, route['model'], review=True, model_class=ConversationModel)
        from model_context import allocation
        provider.budget, capacity = allocation(getattr(provider, 'context_metadata', {}))
        provider.keep_alive = 0
    if provider.model != route['model']:
        raise InvalidUpdate('Conversation provider differs from route')
    budget = provider.budget
    p['memory_allocation'] = {'context_limit': budget.context_limit, 'response_reserve': budget.response_reserve,
                              'template_reserve': budget.template_reserve, 'summary_max_chars': cm.summary_limit(budget)}
    p['model_capacity'] = capacity if real else {'effective_context_tokens': budget.context_limit, 'source': 'injected_provider'}
    cm.materialize(s, sid, cm.history(s, sid, parent))
    p['memory'] = cm.working_set(s, sid, parent, prompt, budget, p, SYSTEM, schema)
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
        out = InvocationGateway(s.path, run, provider).invoke('conversation', uuid.uuid4().hex, SYSTEM, p, fmt, delivery)
        if out['admission']['admitted']:
            requested = out['accepted_output'].get('page_requests', [])
            if requested:
                if attempt == 2:
                    s.quarantine_invocation(sid, out['invocation_id'], 'Memory paging allocation exhausted before final answer')
                    s.event(run, 'FIN', {'reason': 'memory_paging_allocation_exhausted'})
                    out['admission'] = {'admitted': False, 'errors': ['Memory paging allocation exhausted. No draft answer was published.']}
                    out['accepted_output'] = None
                    break
                s.event(run, 'BRIDGE', {'reason': 'conversation_page_in', 'pages': requested})
                p['memory_selection'] = out['invocation_id']
                p['requested_memory_pages'] = requested
                p.pop('repair', None)
                p['memory'] = cm.working_set(s, sid, parent, prompt, budget, p, SYSTEM, schema)
                fmt = schema(p)
                provider.budget.check(SYSTEM, dict(p, repair={'errors': ['x' * 140] * 2}), fmt)
                continue
            s.event(run, 'ALLOW', {'reason': 'conversation_admitted', 'invocation_id': out['invocation_id']})
            break
        s.event(run, 'NACK', {'errors': out['admission']['errors']})
        if attempt == 2 or out.get('blocked'):
            s.event(run, 'FIN', {'reason': 'conversation_repair_exhausted'})
            break
        request_check(s, sid, p)
        s.event(run, 'BRIDGE', {'reason': 'bounded_conversation_repair'})
        p['repair'] = {'errors': [e[:140] for e in out['admission']['errors'][:2]]}
    out.pop('record', None)
    out.pop('model_proposal', None)
    from review_budget import decision
    return {'conversation': True, 'session': sid, 'result': out, 'route': route,
            'memory_used': bool(parent), 'memory_allocation': p['memory_allocation'], 'model_capacity': p['model_capacity'],
            'memory_status': {k: p['memory'][k] for k in ('stored_turns', 'omitted_turns', 'legacy_data_restored')} if p['memory'] else {'stored_turns': 0, 'omitted_turns': 0}, 'budget': decision(s, sid), 'workspace_tools_used': False}
