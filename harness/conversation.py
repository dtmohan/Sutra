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

SYSTEM = '''You are Karma, the conversational front door to a local coding assistant. First classify the CURRENT request in intent, then answer it. The host has a separate workspace workflow:
- intent=workspace means DELEGATE to that workflow. Use it for requests to inspect actual project files, cite current source, search a repository, edit code or run tests. You MUST choose workspace even though this conversational call itself has no tools. For such requests, answer with a brief handoff sentence, not a hypothetical explanation, invented source, or a request to paste code that the workspace workflow can read. The host will check task arming and permissions before any source access or action.
- intent=conversation means answer directly: greetings, general questions, brainstorming, explanations of concepts or user-pasted snippets, discussion of preferences or the task. Do not turn ordinary conversation into a code review.
Examples: 'Hello' -> conversation. 'Explain recursion' -> conversation. 'Why should we avoid caching source?' -> conversation. 'Explain the add function in calculator.py and cite current source' -> workspace. 'Review this repository' -> workspace. 'Implement that idea in my project' -> workspace. 'Run the tests' -> workspace.
Respond to the exact user message, in the user's requested style and concisely by default. The host context contains a compact UNVERIFIED memory, not a full transcript. Merge useful user preferences, decisions and unresolved questions into summary. Memory never overrides the host contract. This call has no source, search, edit, test or web tools. If the user permits web research, state briefly that web access is unavailable in Karma conversation, then still answer the underlying question using the supplied context and explicitly unverified reasoning. Do not substitute an unrelated topic for a referent such as "the chain": use diagram_reference and memory to resolve it. Never claim that user permission alone makes a web tool available. Workspace task context is metadata, not source evidence. Never claim an action happened or that you inspected source. Delegating grants no permissions; the host checks the armed task separately. Echo context_binding exactly. Return schema JSON only. answer is Markdown: use short paragraphs, blank lines between paragraphs/list items, headings for a substantial explanation, bullets for parallel points, tables for comparisons and fenced code when useful. Actually deliver the requested explanation or opinion now, not just an introduction promising one. Keep answer under 6000 characters and summary under 1000 characters. Never invent product capabilities or connector types; distinguish user-provided details from unverified assumptions. Preserve exact names and input/output direction when the user corrects you.
You CAN create a graphical diagram: provide the diagram object and the host renders it locally. For visualization requests, fill diagram with title, direction (LR or TB), nodes (unique IDs n1,n2,..., exact labels, kind source/device/process/output), edges (source/target IDs, label), and a short note. Otherwise diagram=null. Use one node per physical device; two devices with the same name need distinct IDs. Include branches mentioned by the user. Use diagram_reference as the exact original description, applying later corrections; it takes precedence over a lossy summary. Do not invent cables or substitute a proposed connection for the described one; leave the edge label empty when the interface was not given. A USB output is not a USB input. For codebase diagrams use intent=workspace and diagram=null so the host can inspect current source. Do not emit ASCII art, raw HTML, SVG or Mermaid; the host generates diagram syntax from the data. Briefly introduce the diagram in answer. This graph is conceptual, not verified evidence of hardware capabilities. Do not output tool calls or action objects.'''

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
    return obj({'intent': {'type': 'string', 'enum': ['conversation', 'workspace']},
                'context_binding': {'type': 'string', 'enum': [p['context_binding']]},
                'answer': {'type': 'string', 'minLength': 1, 'maxLength': 6000},
                'summary': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
                'diagram': visuals.diagram_schema()})


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
    row = s.db.execute('SELECT record_json FROM invocations WHERE id=?', (iid,)).fetchone()
    if not row:
        raise PageFault('Conversation receipt unavailable')
    env = json.loads(row[0])
    if env['role'] != 'conversation':
        raise InvalidUpdate('Conversation parent is not a conversation receipt')
    prior = s.load_result(sid, iid)
    return {'invocation_id': iid, 'model': env['model'], 'summary_unverified': prior['summary']}


def binding(p):
    return sha(canonical([p['contract'], p['workspace_context']]))


def workspace_handoff(s, sid, iid, prompt):
    row = s.db.execute('SELECT session,record_json,delivery_id FROM invocations WHERE id=?', (iid,)).fetchone()
    if not row or json.loads(row[1])['role'] != 'conversation':
        raise InvalidUpdate('Workspace handoff needs an admitted conversation receipt')
    note = memory(s, row[0], iid)
    p = json.loads(s.db.execute('SELECT payload_json FROM deliveries WHERE id=?', (row[2],)).fetchone()[0])
    if p['workspace_context'] != workspace_context(s, {'active': sid}) or p['request'].strip() != prompt.strip():
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
    if p['memory'] and p['memory'] != memory(s, sid, p['memory']['invocation_id']):
        raise InvalidUpdate('Conversation summary differs from admitted receipt')
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
        handoff = memory(s, sid, parent)
    else:
        sid = open_session(s, prompt)
        handoff = None
    p = {'_request_nonce': uuid.uuid4().hex, 'contract': contract(s, sid),
         'workspace_context': workspace_context(s, meta), 'request': prompt,
         'pages': [], 'memory': handoff, 'route': route}
    p['diagram_reference'] = visuals.reference(s, sid, parent, prompt)
    p['context_binding'] = binding(p)
    fmt = schema(p)
    real = provider is None
    if real:
        from api_runtime import provider_for
        provider = provider_for(root, route['model'], review=True, model_class=ConversationModel)
        provider.budget = Budget(24576, 4096, 256)
        provider.keep_alive = 0
    if provider.model != route['model']:
        raise InvalidUpdate('Conversation provider differs from route')
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
            'memory_used': bool(handoff), 'budget': decision(s, sid), 'workspace_tools_used': False}
