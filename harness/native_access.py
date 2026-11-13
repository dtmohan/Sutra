"""Host-confirmed task permissions, separate from the binary armed switch."""
import json
from context_manager import InvalidUpdate, canonical, sha
import conversation


def handoff_source(s, iid):
    row = s.db.execute('SELECT session,delivery_id FROM invocations WHERE id=?', (iid,)).fetchone()
    if not row:
        raise InvalidUpdate('Permission request needs a conversation receipt')
    conversation.memory(s, row[0], iid)
    return json.loads(s.db.execute('SELECT payload_json FROM deliveries WHERE id=?', (row[1],)).fetchone()[0])


def bind(s, sid, iid):
    """Host records a specific handoff after task creation/permission confirmation."""
    p = handoff_source(s, iid)
    record = {'session': sid, 'conversation_id': iid, 'contract_hash': s.origin(sid)['contract_hash'],
              'request_hash': sha(p['request']), 'previous_workspace_context': p['workspace_context'],
              'workspace_context': conversation.workspace_context(s, {'active': sid})}
    with s.db:
        s._audit('NATIVE_CONVERSATION_BIND', record)


def has_binding(s, sid, iid, p):
    for raw, in s.db.execute("SELECT payload_json FROM page_audit WHERE event='NATIVE_CONVERSATION_BIND' ORDER BY seq DESC"):
        r = json.loads(raw)
        if r['session'] == sid and r['conversation_id'] == iid:
            return (r['request_hash'] == sha(p['request']) and r['previous_workspace_context'] == p['workspace_context']
                    and r['workspace_context'] == conversation.workspace_context(s, {'active': sid}) and s.audit_valid())
    return False


def grant(s, meta, workspace, request):
    from native_runtime import binding
    from repository_tools import normalized, scoped
    from host_actions import source_path
    from vscode_bridge import save_contract
    from review_budget import usage
    sid = binding(s, meta, request)
    old = s.snapshot(sid)
    c = dict(old['constraints'])
    roots = read_scope(s, sid)
    paths, tests = request.get('paths'), request.get('test_paths', [])
    operation = request.get('operation')
    if operation not in ('edit', 'test') or not isinstance(paths, list) or not 1 <= len(paths) <= 8 or len(paths) != len(set(paths)):
        raise InvalidUpdate('Confirm exact Python file permissions for this request')
    if not isinstance(tests, list) or not set(tests) <= set(paths) or (operation == 'test' and not tests):
        raise InvalidUpdate('Confirm exact permitted test paths')
    for name in paths:
        normalized(name)
        if not name.endswith('.py') or not scoped(name, roots):
            raise InvalidUpdate('Requested Python file is outside the armed read scope')
        source_path(workspace, name)
    source = handoff_source(s, request.get('conversation_id'))
    if source['workspace_context'] != conversation.workspace_context(s, {'active': sid}) and not has_binding(s, sid, request['conversation_id'], source):
        raise InvalidUpdate('Task changed while asking for permission')
    before = usage(s, sid)
    # Carry previous use into the new contract epoch; permission grants cannot
    # replenish the task's allocations or resurrect executable checkpoints.
    prior_hash = s.origin(sid)['contract_hash']
    actions_used = sum(json.loads(raw)['contract_hash'] == prior_hash for raw, in
                       s.db.execute('SELECT record_json FROM host_actions WHERE session=?', (sid,)))
    actions_left = c.get('max_actions', 12) - actions_used
    if actions_left <= 0:
        raise InvalidUpdate('Action allocation is exhausted; a permission change cannot reset it')
    c.update(allowed_paths=paths, native_task=True, review_only=False, auto_review=False,
             edits_allowed=c.get('edits_allowed', False) or operation == 'edit',
             tests_allowed=c.get('tests_allowed', False) or bool(tests),
             test_paths=sorted(set(tests) | set(c.get('test_paths', []))),
             test_timeout_ms=c.get('test_timeout_ms', 10000), max_actions=actions_left)
    if not c['test_paths']:
        c.pop('test_paths')
    if c['network_allowed'] or c['dependencies_allowed']:
        raise InvalidUpdate('Native actions require network and dependencies to remain disabled')
    with s.db:
        s._audit('NATIVE_PERMISSION_INTENT', {'session': sid, 'before': prior_hash,
            'constraints': c, 'goal': old['goal'], 'carry_usage': before['used'] if before else {},
            'conversation_id': request['conversation_id'], 'read_scope': roots})
    save_contract(s, sid, {'goal': old['goal'], 'constraints': c}, workspace)
    with s.db:
        s._audit('NATIVE_PERMISSION_GRANT', {'session': sid, 'before': prior_hash,
            'after': s.origin(sid)['contract_hash'], 'carry_usage': before['used'] if before else {},
            'operation': operation, 'paths': paths, 'tests': tests, 'read_scope': roots})
    for key in ('checkpoint', 'chat', 'auto', 'api_candidate', 'api_review', 'api_position', 'static_map'):
        meta.pop(key, None)
    bind(s, sid, request['conversation_id'])


def read_scope(s, sid):
    state = s.snapshot(sid)
    current = s.origin(sid)['contract_hash']
    for raw, in s.db.execute("SELECT payload_json FROM page_audit WHERE event='NATIVE_PERMISSION_GRANT' ORDER BY seq DESC"):
        r = json.loads(raw)
        if r['session'] == sid and r['after'] == current:
            if not s.audit_valid():
                raise InvalidUpdate('Read scope receipt is corrupt')
            return r['read_scope']
    for raw, in s.db.execute("SELECT payload_json FROM page_audit WHERE event='NATIVE_PERMISSION_INTENT' ORDER BY seq DESC"):
        r = json.loads(raw)
        if r['session'] == sid and r['before'] != current and r['constraints'] == state['constraints'] and r['goal'] == state['goal']:
            if not s.audit_valid():
                raise InvalidUpdate('Read scope intent is corrupt')
            return r['read_scope']
    return state['constraints']['allowed_paths']


def carry_usage(s, sid, contract_hash):
    for raw, in s.db.execute("SELECT payload_json FROM page_audit WHERE event='NATIVE_PERMISSION_GRANT' ORDER BY seq DESC"):
        r = json.loads(raw)
        if r['session'] == sid and r['after'] == contract_hash:
            if not s.audit_valid():
                raise InvalidUpdate('Permission allocation receipt is corrupt')
            return r['carry_usage']
    # Recovery if the bridge stopped after changing the contract but before
    # writing the completion record. The precommitted intent binds the exact
    # constraints/goal and cannot charge an unrelated later task revision.
    state = s.snapshot(sid)
    for raw, in s.db.execute("SELECT payload_json FROM page_audit WHERE event='NATIVE_PERMISSION_INTENT' ORDER BY seq DESC"):
        r = json.loads(raw)
        if r['session'] == sid and r['before'] != contract_hash and r['constraints'] == state['constraints'] and r['goal'] == state['goal']:
            if not s.audit_valid():
                raise InvalidUpdate('Permission allocation intent is corrupt')
            return r['carry_usage']
    return {}
