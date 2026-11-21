"""Bounded workspace executions inside an unchanged, cumulative task ceiling.

An execution reserves remaining task capacity once. Reload, retry and model
selection never open a new allowance. Memory belongs to the task/conversation;
charges belong to immutable execution nodes and remain in the parent ledger.
"""
from context_manager import InvalidUpdate, canonical, sha
import orchestration as m


def open_session(s, sid, flow_id, previous=None, *, task_id=None):
    from orchestration_flow import info
    flow, data = info(s, flow_id)
    parent = m.checked(s, task_id) if task_id else m.task(s, sid)
    if parent['kind'] != 'task' or parent['session'] != sid:
        raise InvalidUpdate('Execution requires the same task and session')
    frames = m.gates(s, parent['id'], {})
    m.allowed(frames)
    left = {k: frames[0]['limits'][k] - frames[0]['used'][k]
            for k in frames[0]['limits']}
    # Reserve capacity in the existing task; this is not a budget refill.
    admitted = m.gates(s, parent['id'], left)
    m.allowed(admitted)
    context = {'request': flow['prompt'], 'conversation_id': data.get('conversation_id'),
               'contract_hash': parent['binding']['contract_hash']}
    n = m.create(s, 'execution', sid, parent['id'], binding=m.binding(s, sid),
                 permissions=parent['permissions'], goal=parent['goal'],
                 objective=flow['prompt'], limits=left, context=context,
                 context_hash=sha(canonical(context)), flow_id=flow_id, previous=previous)
    m.transition(s, n['id'], 'READY', 'EXECUTION_OPENED',
                 {'parent_spent': m.spent(s, parent['id']), 'allowance': left}, admitted[0])
    return n


def idle(s, nid):
    for child in m.descendants(s, nid):
        if m.state(s, child) in ('RUNNING', 'EXECUTING_ACTION', 'CANCELLING'):
            raise InvalidUpdate('Wait for active execution work before closing its allowance')
        if s.db.execute('SELECT 1 FROM orchestration_charges c LEFT JOIN orchestration_settlements r ON r.charge_id=c.id WHERE c.node_id=? AND r.charge_id IS NULL', (child,)).fetchone():
            raise InvalidUpdate('Recover interrupted execution work before closing its allowance')


def finish(s, fid, cancelled=False):
    from orchestration_flow import info
    _, data = info(s, fid)
    nid = data.get('root_node')
    if not nid or m.node(s, nid)['kind'] != 'execution' or m.state(s, nid) in m.TERMINAL:
        return
    idle(s, nid)
    for child in reversed(m.descendants(s, nid)[1:]):
        if m.state(s, child) not in m.TERMINAL:
            m.transition(s, child, 'CANCELLED', 'EXECUTION_ENDED', {'spending_retained': True})
    if m.state(s, nid) == 'WAITING_CHILDREN':
        m.transition(s, nid, 'READY', 'EXECUTION_JOIN_RELEASED')
    m.transition(s, nid, 'CANCELLED' if cancelled else 'DONE', 'EXECUTION_CLOSED',
                 {'spent': m.spent(s, nid), 'reason': 'cancelled' if cancelled else 'request finished'})


def renew_after_grant(s, fid):
    """Explicit allocation amendment ends one bounded session and opens another.

    Previous child notes stay historical evidence. No child permission or budget
    is enlarged; active child work must finish/recover before renewal.
    """
    from orchestration_flow import info
    _, data = info(s, fid)
    old = m.node(s, data['root_node'])
    if old['kind'] != 'execution':
        return  # Legacy roots already use the amended task ceiling.
    if old['binding'] != m.binding(s, old['session']):
        return  # Approval cannot silently repair policy or permission bindings.
    idle(s, old['id'])
    if any(m.state(s, c) not in m.TERMINAL for c in m.descendants(s, old['id'])[1:]):
        raise InvalidUpdate('Finish or discard pending child work before renewing execution capacity')
    m.transition(s, old['id'], 'DONE', 'EXECUTION_CLOSED',
                 {'spent': m.spent(s, old['id']), 'reason': 'explicit allocation amendment'})
    new = open_session(s, old['session'], fid, previous=old['id'])
    m.transition(s, fid, 'PAUSED', 'EXECUTION_RENEWED',
                 {'root_node': new['id'], 'result_parent': data.get('result_parent', old['id'])})
    return new
