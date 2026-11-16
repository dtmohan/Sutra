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

from conversation_prompt import SYSTEM

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
        from conversation_prompt import messages
        return messages(system, payload, self.output_schema(payload, schema(payload)))

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



def schema(p):
    available = [r['id'] for r in (p.get('memory') or {}).get('toc', [])]
    quotations = list(dict.fromkeys(q for q in re.split(r'(?<=[.!?])\s+|\n+', p['request']) if q.strip()))
    fmt = obj({'intent': {'type': 'string', 'enum': ['conversation', 'workspace']},
                'context_binding': {'type': 'string', 'enum': [p['context_binding']]},
                'answer': {'type': 'string', 'minLength': 1, 'maxLength': 6000},
                'summary': {'type': 'string', 'minLength': 1, 'maxLength': p['memory_allocation']['summary_max_chars']},
                'escalation_reason': {'type':'string','enum':['none','missing_context','reasoning_capacity']},
                'context_question': {'type': 'string', 'minLength': 1, 'maxLength': 500},
                'page_requests': {'type': 'array', 'maxItems': 4 if available else 0, 'items': dict(type='string', **({'enum': available} if available else {}))},
                'facts': {'type': 'array', 'maxItems': 8, 'items': {'type': 'string', 'enum': quotations}},
                'workspace_request': obj({'operation': {'type': 'string', 'enum': ['read', 'edit', 'test']},
                    'paths': {'type': 'array', 'maxItems': 8, 'items': {'type': 'string', 'minLength': 1, 'maxLength': 240}}}),
                'diagram': visuals.diagram_schema()})
    # Older providers/fixtures may omit advisory fields. Absence grants nothing.
    fmt['required'] = [k for k in fmt['required'] if k not in ('facts', 'workspace_request', 'context_question')]
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


def open_session(s, prompt):
    sid = 'conversation-' + uuid.uuid4().hex
    s.create(sid)
    values = dict(CONSTRAINTS, task_id=sid, goal='Answer conversation or request a governed workspace handoff; this stage executes no workspace tools.')
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
    return sha(canonical([p['contract'], p['workspace_context'], p['routing_capabilities']]))


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


def request_check(s, sid, p):
    if p.get('response_contract') != 2:
        raise InvalidUpdate('Conversation response contract changed')
    if p.get('routing_capabilities') != ROUTING_CAPABILITIES:
        raise InvalidUpdate('Host routing capabilities changed')
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
    pages = proposal.get('page_requests', [])
    if (proposal.get('workspace_request', {}).get('operation') in ('edit', 'test')
            and proposal['intent'] != 'workspace'):
        raise InvalidUpdate('An edit/test workspace_request needs intent=workspace for host handoff; omit it for conversation.')
    if len(pages) != len(set(pages)):
        raise InvalidUpdate('Request each memory page only once.')
    question = proposal.get('context_question', '').strip()
    if proposal.get('context_question') is not None and not question:
        raise InvalidUpdate('context_question must ask a specific question.')
    if pages and question:
        raise InvalidUpdate('Page available context first; ask context_question only when no relevant page is available.')
    if (proposal['intent'] == 'conversation' and proposal['escalation_reason'] == 'missing_context'
            and (p.get('memory') or {}).get('toc') and not pages and not question):
        raise InvalidUpdate('Set top-level page_requests from the TOC, or context_question if no listed page contains the missing evidence.')


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


def session_for(s,request):
    prompt=request.get('prompt')
    if not isinstance(prompt,str) or not 1<=len(prompt)<=1800 or not prompt.strip():
        raise InvalidUpdate('Conversation prompt must be 1..1800 characters')
    parent=request.get('parent')
    sid=request.get('conversation_session')
    if parent:
        if not isinstance(parent,str) or not re.fullmatch('[a-f0-9]{32}',parent):
            raise InvalidUpdate('Invalid conversation receipt reference')
        row=s.db.execute('SELECT session FROM invocations WHERE id=?',(parent,)).fetchone()
        if not row:raise PageFault('Conversation memory unavailable. Start a new chat; workspace task is retained.')
        if sid and sid!=row[0]:raise InvalidUpdate('Conversation session changed during routing')
        sid=row[0];cm.history(s,sid,parent)
    if sid:contract(s,sid)
    return sid or open_session(s,prompt)


def answer(s,meta,root,request,provider=None):
    request=dict(request)
    request['conversation_session']=session_for(s,request)
    calls=[]
    while True:
        result=_answer_step(s,meta,root,request,provider)
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


def _answer_step(s,meta,root,request,provider=None):
    prompt=request['prompt'];parent=request.get('parent');sid=request['conversation_session']
    route,routing_state=routing.prepare(s,sid,request)
    p = {'response_contract': 2, '_request_nonce': uuid.uuid4().hex, 'contract': contract(s, sid),
         'workspace_context': workspace_context(s, meta), 'request': prompt,
         'routing_capabilities': json.loads(canonical(ROUTING_CAPABILITIES)),
         'pages': [], 'memory': None, 'route': route,'routing_state':routing_state,
         'requested_memory_pages':routing_state['requested_pages']}
    p['diagram_reference'] = visuals.reference(s, sid, parent, prompt)
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
        out = InvocationGateway(s.path, run, provider).invoke('conversation', uuid.uuid4().hex, SYSTEM, p, fmt, delivery)
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
