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


def _clear_previous_scope(meta):
    for key in ('checkpoint', 'chat', 'auto', 'api_candidate', 'api_review', 'api_position', 'static_map'):
        meta.pop(key, None)


def grant(s, meta, workspace, request):
    from native_runtime import binding
    from evidence_admission import explicit_project_patch
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
    if (operation not in ('edit', 'test', 'project_patch') or not isinstance(paths, list) or not 1 <= len(paths) <= 8
            or any(not isinstance(name, str) for name in paths) or len(paths) != len(set(paths))):
        raise InvalidUpdate('Confirm exact Python file permissions for this request')
    if (not isinstance(tests, list) or any(not isinstance(name, str) for name in tests)
            or not set(tests) <= set(paths) or (operation == 'test' and not tests)):
        raise InvalidUpdate('Confirm exact permitted test paths')
    existing_tests = set(c.get('test_paths', []))
    if operation == 'edit' and not existing_tests.issubset(paths):
        raise InvalidUpdate('The confirmed edit scope must include previously permitted test paths')
    paths = sorted(set(paths) | existing_tests)
    tests = sorted(set(tests) | existing_tests)
    if len(paths) > 8:
        raise InvalidUpdate('Choose at most eight exact Python paths including previously permitted tests')
    for name in paths:
        normalized(name)
        if not name.endswith('.py') or not scoped(name, roots):
            raise InvalidUpdate('Requested Python file is outside the armed read scope')
        source_path(workspace, name)
    source = handoff_source(s, request.get('conversation_id'))
    if operation == 'project_patch':
        if (roots != ['.'] or explicit_project_patch(source['request']) !=
                {'operation': operation, 'paths': paths} or tests):
            raise InvalidUpdate('Project worktree access requires the named Python targets and whole-project read scope')
        if c.get('edits_allowed') and paths != c['allowed_paths']:
            raise InvalidUpdate('Start a separate task to prepare a patch on a new isolated scope')
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
             test_paths=tests,
             test_timeout_ms=c.get('test_timeout_ms', 10000), max_actions=actions_left)
    if operation == 'project_patch' or 'project_patch_allowed' in c:
        c['project_patch_allowed'] = operation == 'project_patch'
    if not c['test_paths']:
        c.pop('test_paths')
    if c['network_allowed'] or c['dependencies_allowed']:
        raise InvalidUpdate('Native actions require network and dependencies to remain disabled')
    with s.db:
        s._audit('NATIVE_PERMISSION_INTENT', {'session': sid, 'before': prior_hash,
            'constraints': c, 'goal': old['goal'], 'carry_usage': before['used'] if before else {},
            'conversation_id': request['conversation_id'], 'read_scope': roots,
            'operation': operation, 'flow_id': meta.get('_flow')})
    save_contract(s, sid, {'goal': old['goal'], 'constraints': c}, workspace)
    with s.db:
        s._audit('NATIVE_PERMISSION_GRANT', {'session': sid, 'before': prior_hash,
            'after': s.origin(sid)['contract_hash'], 'carry_usage': before['used'] if before else {},
            'operation': operation, 'paths': paths, 'tests': tests, 'read_scope': roots})
    _clear_previous_scope(meta)
    bind(s, sid, request['conversation_id'])


def settle_interrupted_grant(s, meta, previous, iid, operation):
    """Restore the handoff for an exact grant already committed to the contract."""
    from calibration import current as calibration_binding
    sid = meta.get('active')
    if not iid or sid != previous['session'] or operation not in ('edit', 'test', 'project_patch'):
        return False
    origin = s.origin(sid)
    if (origin['previous_hash'] != previous['contract_hash']
            or calibration_binding(s, sid, usable=True)['hash'] != previous['calibration_hash']):
        return False
    state = s.snapshot(sid)
    constraints = state['constraints']
    permission = {'edit': 'edits_allowed', 'test': 'tests_allowed', 'project_patch': 'project_patch_allowed'}[operation]
    if (not constraints.get('native_task') or constraints.get('network_allowed') is not False
            or constraints.get('dependencies_allowed') is not False
            or constraints.get(permission) is not True):
        return False
    for raw, in s.db.execute(
            "SELECT payload_json FROM page_audit WHERE event='NATIVE_PERMISSION_INTENT' "
            "AND json_extract(payload_json,'$.session')=? AND json_extract(payload_json,'$.before')=? "
            "AND json_extract(payload_json,'$.conversation_id')=? ORDER BY seq DESC",
            (sid, previous['contract_hash'], iid)):
        intent = json.loads(raw)
        if intent['constraints'] != constraints or intent['goal'] != state['goal']:
            continue
        if 'operation' in intent or 'flow_id' in intent:
            if intent.get('operation') != operation or intent.get('flow_id') != meta.get('_flow'):
                continue
        else:
            # The older intent omitted the operation and workflow. Require its
            # exact completion receipt before carrying that grant forward.
            row = s.db.execute(
                "SELECT payload_json FROM page_audit WHERE event='NATIVE_PERMISSION_GRANT' "
                "AND json_extract(payload_json,'$.session')=? AND json_extract(payload_json,'$.before')=? "
                "AND json_extract(payload_json,'$.after')=? AND json_extract(payload_json,'$.operation')=? "
                "ORDER BY seq DESC LIMIT 1",
                (sid, previous['contract_hash'], origin['contract_hash'], operation)).fetchone()
            if not row:
                continue
            settled = json.loads(row[0])
            if (settled.get('read_scope') != intent['read_scope']
                    or settled.get('carry_usage') != intent['carry_usage']
                    or settled.get('paths') != constraints.get('allowed_paths')):
                continue
        if not s.audit_valid():
            raise InvalidUpdate('Interrupted permission grant has a corrupt receipt chain')
        source = handoff_source(s, iid)
        if not has_binding(s, sid, iid, source):
            bind(s, sid, iid)
        _clear_previous_scope(meta)
        return True
    return False


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


def _epoch_at(s, sid, digest):
    return next((record for record, value in s._contract_history(sid) if value == digest), None)


def _contract_at(s, sid, digest):
    epoch = _epoch_at(s, sid, digest)
    return epoch['contract'] if epoch else None


def _scope_intent(s, sid, before, flow_id, constraints, goal):
    for raw, in s.db.execute("SELECT payload_json FROM page_audit WHERE event='NATIVE_PERMISSION_INTENT' ORDER BY seq DESC"):
        record = json.loads(raw)
        if (record.get('operation') == 'read_scope_correction' and record['session'] == sid
                and record.get('authority') == 'host_user_confirmation'
                and record['before'] == before and record.get('flow_id') == flow_id
                and record['constraints'] == constraints and record['goal'] == goal):
            if not s.audit_valid():
                raise InvalidUpdate('Read-scope correction intent is corrupt')
            return record
    return None


def read_scope_correction(s, flow, data, binding):
    """Recognize only a router-invented scope for the original broad read ask."""
    from evidence_admission import workspace_target
    from paging import PageFault

    sid = binding['session']
    origin = _contract_at(s, sid, binding['contract_hash'])
    if not origin or origin['goal'].strip() != flow['prompt'].strip():
        return None
    old = origin['constraints']
    if (old.get('allowed_paths') == ['.'] or old.get('auto_review') is not True or old.get('review_only') is not True
            or any(old.get(key) is not False for key in
                   ('edits_allowed', 'tests_allowed', 'network_allowed', 'dependencies_allowed'))):
        return None
    iid = data.get('conversation_id')
    if not iid:
        return None
    try:
        source = handoff_source(s, iid)
    except PageFault:
        # An unavailable historical proposal cannot prove a scope correction.
        # Leave normal policy/binding recovery in charge instead of recursively
        # failing while the monitor inspects an unrelated old review.
        return None
    if source['request'] != flow['prompt']:
        return None
    # handoff_source validated this accepted tool-free historical receipt. Its
    # router proposal is provenance for the mistake, never a fresh model result.
    proposal = json.loads(s.db.execute(
        'SELECT record_json FROM invocation_receipts WHERE invocation_id=?', (iid,)).fetchone()[0])['proposal']
    proposed = proposal.get('workspace_request') or {}
    if proposal.get('intent') != 'workspace' or proposed.get('operation') != 'read':
        return None
    work, status, roots = workspace_target(proposed, flow['prompt'])
    if (status != 'resolved' or roots != ['.'] or old.get('allowed_paths') == roots
            or set(proposed.get('paths', [])) != set(old.get('allowed_paths', []))
            or data.get('permission') != proposed
            or set(data.get('source_roots', [])) != set(old.get('allowed_paths', []))):
        return None
    constraints = dict(old, allowed_paths=roots)
    current = s.snapshot(sid)
    if current['goal'] != origin['goal']:
        return None
    current_origin = s.origin(sid)
    if current_origin['contract_hash'] != binding['contract_hash'] and not (
            current['constraints'] == constraints and current_origin['previous_hash'] == binding['contract_hash']
            and _scope_intent(s, sid, binding['contract_hash'], flow['id'], constraints, origin['goal'])):
        return None
    if current['constraints'] not in (old, constraints):
        return None
    return {'session': sid, 'before': binding['contract_hash'], 'flow_id': flow['id'],
            'constraints': constraints, 'goal': origin['goal'], 'conversation_id': iid,
            'read_scope': roots, 'old_read_scope': old['allowed_paths'], 'permission': work}


def correct_read_scope(s, meta, workspace, correction):
    """Version one confirmed read-only target correction without a budget refill."""
    from allocation_recovery import effective
    from review_budget import usage
    from vscode_bridge import save_contract

    sid = correction['session']
    constraints = correction['constraints']
    record = _scope_intent(s, sid, correction['before'], correction['flow_id'], constraints, correction['goal'])
    if s.origin(sid)['contract_hash'] == correction['before']:
        before = usage(s, sid)
        limits, version, head = effective(s, sid, before['base_limits'])
        fresh = {key: value for key, value in correction.items() if key != 'permission'}
        fresh.update(authority='host_user_confirmation', operation='read_scope_correction',
                     carry_usage=before['used'], carry_allocation={
                         'base_limits': before['base_limits'], 'limits': limits,
                         'version': version, 'head': head})
        if record != fresh:
            with s.db:
                s._audit('NATIVE_PERMISSION_INTENT', fresh)
            record = fresh
        save_contract(s, sid, {'goal': correction['goal'], 'constraints': constraints}, workspace)
    elif (not record or s.snapshot(sid)['constraints'] != constraints
          or s.origin(sid)['previous_hash'] != correction['before']):
        raise InvalidUpdate('Read-scope correction no longer matches this task')
    after = s.origin(sid)['contract_hash']
    settled = s.db.execute(
        "SELECT 1 FROM page_audit WHERE event='NATIVE_PERMISSION_GRANT' "
        "AND json_extract(payload_json,'$.session')=? AND json_extract(payload_json,'$.after')=?",
        (sid, after)).fetchone()
    if not settled:
        with s.db:
            s._audit('NATIVE_PERMISSION_GRANT', dict(record, after=after))
    bind(s, sid, correction['conversation_id'])
    for key in ('checkpoint', 'chat', 'auto', 'api_candidate', 'api_review', 'api_position', 'static_map'):
        meta.pop(key, None)


def allocation_carry(s, sid, contract_hash):
    """Read the authenticated allocation prefix of a corrected contract epoch."""
    epoch = _epoch_at(s, sid, contract_hash)
    if not epoch:
        return None
    current = epoch['contract']
    for event in ('NATIVE_PERMISSION_GRANT', 'NATIVE_PERMISSION_INTENT'):
        for raw, in s.db.execute('SELECT payload_json FROM page_audit WHERE event=? ORDER BY seq DESC', (event,)):
            record = json.loads(raw)
            if (record.get('operation') != 'read_scope_correction' or record['session'] != sid
                    or record.get('authority') != 'host_user_confirmation'
                    or record['constraints'] != current['constraints'] or record['goal'] != current['goal']):
                continue
            if ((event == 'NATIVE_PERMISSION_GRANT' and record.get('after') != contract_hash)
                    or record['before'] == contract_hash or epoch['previous_hash'] != record['before']):
                continue
            if not s.audit_valid():
                raise InvalidUpdate('Read-scope allocation carry is corrupt')
            saved = record.get('carry_allocation')
            if not isinstance(saved, dict) or set(saved) != {'base_limits', 'limits', 'version', 'head'}:
                raise InvalidUpdate('Read-scope allocation carry has invalid fields')
            return record
    return None
