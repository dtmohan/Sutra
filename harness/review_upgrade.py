"""An explicit release handoff for already joined read-only reviews."""
from contextlib import contextmanager
import json
from pathlib import Path

from context_manager import InvalidUpdate

PREDECESSOR = 'd58edf88efad29d2dfc1d81a9c2cb02260a6b6f22795efeb578f7cdaa2a41c9c'
SYNTHESIS_PREDECESSOR = 'df19c45e777027c01f34d13dbd3c6e46d149bab6a3bbcfb735479f468480299f'
COMPACT_WRITER_PREDECESSOR = 'a57157dbafb20e77c3b4821fc12d53c99611c4e5de6a9c1eb3a76e8146c0a500'
SUMMARY_REPAIR_PREDECESSOR = '1159af17874e347e75644c6d4583dcebb9db212cab69852e009638b3d1e3c43a'
SUMMARY_PROJECTION_PREDECESSOR = 'a0d9d8e3c9660ce67a34333c097795c398591e6ced9ffa1ad4083437aa94b306'
RESULT_ROLES = {'architect', 'subagent', 'chat_reviewer', 'report_reviewer'}
REPOSITORY_KINDS = {'catalog', 'list', 'read', 'search', 'static_map', 'api_extract'}
_MANIFEST = Path(__file__).with_name('review-upgrade.json')


def release_pair(s):
    """Authorize only the exact packaged successor, never an arbitrary later patch."""
    try:
        value = json.loads(_MANIFEST.read_text())
    except (OSError, ValueError):
        return False
    return value == {'version': 1, 'previous': PREDECESSOR, 'current': s.logic_hash}


def _scope(s, fid, data, predecessor=PREDECESSOR, *, target=None):
    import orchestration as machine
    root = data.get('root_node')
    if not root or (predecessor != s.logic_hash and not release_pair(s)):
        return None
    owner = machine.node(s, root)
    flow = machine.node(s, fid)
    if (owner.get('kind') != 'execution' or owner.get('flow_id') != fid
            or owner.get('previous') or data.get('result_parent') not in (None, root)):
        return None
    sid = owner['session']
    old = owner['binding']
    current = machine.binding(s, sid)
    task = s.snapshot(sid)
    constraints = task['constraints']
    if (old != dict(current, logic_hash=predecessor)
            or data.get('binding') != {key: value for key, value in current.items() if key != 'logic_hash'}
            or not (constraints.get('auto_review') is True and constraints.get('review_only') is True)
            or any(constraints.get(key) is not False for key in (
                'edits_allowed', 'tests_allowed', 'network_allowed', 'dependencies_allowed'))
            or owner['permissions'] != machine.permissions(s, sid)
            or owner['context'].get('request') != flow.get('prompt')):
        return None
    joined = next((event for event in reversed(machine.events(s, root)) if event['kind'] == 'JOIN'), None)
    if not joined or not data.get('child_results') or joined['data']['invocations'] != data['child_results']:
        return None
    parent = machine.node(s, owner['parent_id'])
    if parent.get('kind') != 'task' or parent.get('binding') != old:
        return None
    return {'version': 1, 'flow_id': fid, 'root': root, 'parent': parent['id'],
            'workspace': flow['workspace'], 'previous': old,
            'current': dict(current, logic_hash=target) if target else current,
            'join_receipt': joined['receipt']}


def parent_origin(s, fid, data, *, target=None):
    """Authorize a closed root's artifacts, including its recorded 0.57.1 handoff."""
    import orchestration as machine
    if (machine.state(s, fid) != 'DONE' or not data.get('root_node')
            or not data.get('git_provenance')):
        return None
    root = machine.node(s, data['root_node'])
    logic = root['binding']['logic_hash']
    if logic not in {s.logic_hash, PREDECESSOR, SYNTHESIS_PREDECESSOR}:
        return None
    stamp = _scope(s, fid, data, logic, target=target)
    if not stamp or machine.state(s, root['id']) != 'DONE':
        return None
    policies = [logic]
    if logic == PREDECESSOR:
        adopted = _scope(s, fid, data, PREDECESSOR, target=SYNTHESIS_PREDECESSOR)
        if data.get('review_policy_upgrade') != adopted:
            return None
        policies.append(SYNTHESIS_PREDECESSOR)
    return dict(stamp, artifact_policies=policies)


def prior_synthesis(s, fid, data):
    """Verify a single, closed continuation under its exact historical policy."""
    import orchestration as machine
    flow = machine.node(s, fid)
    link = flow.get('parent_synthesis_predecessor')
    if (not link or not data.get('root_node') or machine.state(s, fid) != 'DONE'
            or machine.state(s, data['root_node']) != 'DONE'
            or machine.node(s, link['flow_id']).get('parent_synthesis_predecessor')):
        return None
    logic = machine.node(s, data['root_node'])['binding']['logic_hash']
    if logic != s.logic_hash and (logic != COMPACT_WRITER_PREDECESSOR or not release_pair(s)):
        return None
    expected = _continued(s, fid, data, link, target=logic)
    return expected if expected and data.get('review_policy_upgrade') == expected else None


def _continued(s, fid, data, link, *, target=None):
    import orchestration as machine
    from orchestration_flow import info
    old_flow, old_data = info(s, link['flow_id'])
    if old_flow.get('parent_synthesis_predecessor'):
        prior = prior_synthesis(s, old_flow['id'], old_data) or closed_summary_repair(s, old_flow['id'], old_data)
        if not prior:
            return None
        _, original = info(s, prior['origin_flow_id'])
        origin = parent_origin(s, prior['origin_flow_id'], original, target=target)
    else:
        origin = parent_origin(s, old_flow['id'], old_data, target=target)
    old_root = old_data.get('root_node')
    if not origin or old_root != link['execution_id'] or not data.get('root_node'):
        return None
    root = machine.node(s, data['root_node'])
    previous = machine.node(s, old_root)
    if (root['flow_id'] != fid or root['previous'] != old_root
            or root['parent_id'] != origin['parent'] or root['binding'] != origin['current']
            or root['permissions'] != previous['permissions'] or root['context'] != previous['context']
            or data.get('result_parent') != origin['root']
            or data.get('binding') != old_data.get('binding')
            or data.get('git_provenance') != old_data.get('git_provenance')
            or data.get('child_results') != old_data.get('child_results')):
        return None
    return dict(origin, flow_id=fid, origin_flow_id=origin['flow_id'], active_root=root['id'])


def _active_continuation(s, fid, data, *, target=None):
    """Verify an exact provisional runtime's link before moving its active writer."""
    import orchestration as machine
    link = machine.node(s, fid).get('parent_synthesis_predecessor')
    if not link or not data.get('root_node') or not release_pair(s):
        return None
    active = machine.node(s, data['root_node'])
    if active['binding']['logic_hash'] != SUMMARY_REPAIR_PREDECESSOR:
        return None
    expected = _continued(s, fid, data, link, target=SUMMARY_REPAIR_PREDECESSOR)
    recorded = next((event['data'].get('review_policy_upgrade') for event in machine.events(s, fid)
                     if event['kind'] == 'PARENT_REPORT_CONTINUATION'), None)
    if not expected or expected != recorded or not (data.get('parent_output_retry') or {}).get('repair_invocation'):
        return None
    current = machine.binding(s, active['session'])
    return dict(expected, active_previous=active['binding'],
                current=dict(current, logic_hash=target) if target else current)


def closed_summary_repair(s, fid, data):
    """Recover format-only writer work across the previously admitted runtime handoff."""
    import orchestration as machine
    if (not data.get('root_node') or machine.state(s, fid) != 'DONE'
            or machine.state(s, data['root_node']) != 'DONE'):
        return None
    stamp = _active_continuation(s, fid, data, target=SUMMARY_PROJECTION_PREDECESSOR)
    return stamp if stamp and data.get('review_policy_upgrade') == stamp else None


def offer(s, fid, data):
    import orchestration as machine
    stop = data.get('workflow_stop') or {}
    if (machine.state(s, fid) != 'PAUSED' or stop.get('resume_phase') != 'SYNTHESIS'
            or stop.get('status') not in ('resource_wait', 'claim_review_output_limit')
            or not data.get('git_provenance')):
        return None
    linked = _active_continuation(s, fid, data)
    if linked and (machine.state(s, linked['active_root']) == 'READY'
                   and not s.db.execute('SELECT 1 FROM orchestration_charges WHERE node_id=? LIMIT 1',
                                        (linked['active_root'],)).fetchone()):
        return linked
    return _scope(s, fid, data)


def recorded(s, fid, data=None):
    if not fid:
        return None
    if data is None:
        row = s.db.execute(
            "SELECT 1 FROM orchestration_events WHERE node_id=? "
            "AND json_extract(record_json,'$.kind') IN ('REVIEW_POLICY_UPGRADED','PARENT_REPORT_CONTINUATION') LIMIT 1",
            (fid,)).fetchone()
        if not row:
            return None
        from orchestration_flow import info
        data = info(s, fid)[1]
    stamp = data.get('review_policy_upgrade')
    if not stamp:
        return None
    import orchestration as machine
    predecessor = machine.node(s, fid).get('parent_synthesis_predecessor')
    if stamp.get('active_previous'):
        expected = _active_continuation(s, fid, data)
    else:
        expected = _continued(s, fid, data, predecessor) if predecessor else _scope(s, fid, data)
    if stamp == expected:
        return stamp
    return None


@contextmanager
def use(s, stamp):
    previous = getattr(s, '_review_upgrade', None)
    s._review_upgrade = stamp
    try:
        yield
    finally:
        s._review_upgrade = previous


def _active(s, sid):
    stamp = getattr(s, '_review_upgrade', None)
    return stamp if stamp and stamp['current']['session'] == sid else None


def _owner(s, stamp, nid):
    if nid in (stamp['root'], stamp['parent']):
        return True
    if not nid:
        return False
    import orchestration as machine
    node = machine.node(s, nid)
    return (node.get('kind') == 'branch' and node.get('parent_id') == stamp['root']
            and node.get('binding') == stamp['previous'])


def node_policy(s, node):
    stamp = _active(s, node['session'])
    return bool(stamp and ((node.get('binding') == stamp['previous'] and _owner(s, stamp, node['id']))
                or (node['id'] == stamp.get('active_root') and node.get('binding') == stamp.get('active_previous'))))


def result_policy(s, session, receipt, envelope):
    logic = receipt['logic_hash']
    if logic != envelope['logic_hash']:
        return False
    if logic == s.logic_hash:
        return True
    stamp = _active(s, session)
    if not stamp or logic not in stamp.get('artifact_policies', [stamp['previous']['logic_hash']]) or envelope['role'] not in RESULT_ROLES:
        return False
    owner = (envelope.get('orchestration') or {}).get('node_id')
    if envelope['role'] == 'subagent':
        return owner not in (stamp['root'], stamp['parent']) and _owner(s, stamp, owner)
    return owner == stamp['root']


def repository_policy(s, session, operation):
    if operation['logic_hash'] == s.logic_hash:
        return True
    stamp = _active(s, session)
    if (not stamp or operation['logic_hash'] not in stamp.get('artifact_policies', [stamp['previous']['logic_hash']])
            or operation['kind'] not in REPOSITORY_KINDS):
        return False
    owner = (operation.get('orchestration') or {}).get('node_id')
    return _owner(s, stamp, owner) and (owner != stamp['parent'] or operation['kind'] == 'catalog')


def confirm(s, stamp, data, *, closed=False):
    """Use the existing ledger and fresh validators before adopting old artifacts."""
    import orchestration as machine
    import repository_tools
    from accounting_audit import check_admission
    from execution_sessions import idle
    sid = stamp['current']['session']
    idle(s, stamp['root'])
    check_admission(s)
    if not s.audit_valid(deep=True) or not s.verify_invocations(sid):
        raise InvalidUpdate('Saved review audit or invocation history failed verification')
    repository_tools.records(s, sid)
    for iid in data['child_results']:
        s.load_result(sid, iid)
    checkpoint = data.get('parent_checkpoint') or data.get('claim_review_retry')
    if checkpoint and checkpoint.get('candidate_id'):
        s.load_result(sid, checkpoint['candidate_id'])
    if machine.state(s, stamp['root']) != ('DONE' if closed else 'READY'):
        raise InvalidUpdate('Saved review execution is not ready to resume')


def for_state(s, sid):
    """Find an already recorded handoff for the task display, without admitting work."""
    for (fid,) in s.db.execute(
            "SELECT node_id FROM orchestration_events "
            "WHERE json_extract(record_json,'$.kind') IN ('REVIEW_POLICY_UPGRADED','PARENT_REPORT_CONTINUATION') "
            "AND json_extract(record_json,'$.data.review_policy_upgrade.current.session')=? "
            "ORDER BY rowid DESC", (sid,)):
        stamp = recorded(s, fid)
        if stamp:
            return stamp
    return None
