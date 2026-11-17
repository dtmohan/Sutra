"""Durable focus history for workspace flows in one native chat.

The stack stores flow IDs. Flow receipts, task contracts, memory and execution
charges keep their original owners as focus changes.
"""
from context_manager import InvalidUpdate
from execution_sessions import idle
from karma_adapter import KarmaAdapter
import orchestration as orchestration


_ATTACHED = 'TASK_STACK_ATTACHED'
_SELECTED = 'TASK_STACK_SELECTED'
_CLOSED = 'TASK_STACK_CLOSED'
_BUSY = {'RUNNING', 'EXECUTING_ACTION', 'CANCELLING'}


class TaskStackStop(InvalidUpdate):
    def __init__(self, message, frame):
        super().__init__(message)
        self.frame = frame


def _history(s, workspace, chat_key):
    if not isinstance(chat_key, str) or not chat_key.strip() or len(chat_key) > 512 or not chat_key.isprintable():
        raise InvalidUpdate('Invalid task stack chat identity')
    rows = s.db.execute(
        """SELECT id FROM orchestration_nodes WHERE json_extract(record_json,'$.kind')='task_stack'
           AND json_extract(record_json,'$.workspace')=? AND json_extract(record_json,'$.chat_key')=? LIMIT 2""",
        (str(workspace), chat_key),
    ).fetchall()
    if len(rows) > 1:
        raise InvalidUpdate('Multiple task stacks for this chat')
    if not rows:
        return None, []
    nid = rows[0][0]
    return nid, orchestration.events(s, nid)


def _ordered(history):
    order = []
    for event in history:
        flow_id = event['data']['flow_id']
        if event['kind'] == _ATTACHED and flow_id not in order:
            order.append(flow_id)
        elif event['kind'] == _SELECTED and flow_id in order:
            order.remove(flow_id)
            order.append(flow_id)
        elif event['kind'] == _CLOSED and flow_id in order:
            order.remove(flow_id)
        else:
            raise InvalidUpdate('Invalid task stack history')
    return order


def _flow(s, workspace, flow_id):
    if not isinstance(flow_id, str) or not flow_id:
        raise InvalidUpdate('Invalid task stack flow identity')
    flow = orchestration.node(s, flow_id)
    if flow['kind'] != 'flow' or flow['workspace'] != str(workspace):
        raise InvalidUpdate('Task stack selection requires a flow in this workspace')
    return flow, orchestration.events(s, flow_id)


def _row(s, workspace, flow_id):
    flow, history = _flow(s, workspace, flow_id)
    last = history[-1] if history else None
    return {'flow_id': flow_id, 'prompt': flow['prompt'], 'state': last['to'] if last else flow['initial_state'],
            'cursor': last['receipt'] if last else None}


def _project(s, workspace, history, order):
    rows = [_row(s, workspace, flow_id) for flow_id in reversed(order)]
    open_tasks = [row for row in rows if row['state'] not in orchestration.TERMINAL]
    focused = open_tasks[0]['flow_id'] if open_tasks else None
    for row in open_tasks:
        row['focused'] = row['flow_id'] == focused
    last = history[-1] if history else None
    transition = None
    if last:
        transition = {key: last[key] for key in ('kind', 'at', 'kernel', 'receipt')}
        transition['flow_id'] = last['data']['flow_id']
    return {'focused': focused, 'open_tasks': open_tasks, 'cursor': last['receipt'] if last else None,
            'last_transition': transition}


def view(s, workspace, chat_key):
    _, history = _history(s, workspace, chat_key)
    return _project(s, workspace, history, _ordered(history))


def owner(s, workspace, flow_id):
    """Return the original owning chat, including closed registrations."""
    rows = s.db.execute(
        """SELECT n.id FROM orchestration_nodes n JOIN orchestration_events e ON e.node_id=n.id
           WHERE json_extract(n.record_json,'$.kind')='task_stack'
           AND json_extract(n.record_json,'$.workspace')=?
           AND json_extract(e.record_json,'$.kind')=? AND json_extract(e.record_json,'$.data.flow_id')=?
           LIMIT 2""",
        (str(workspace), _ATTACHED, flow_id),
    ).fetchall()
    if len(rows) > 1:
        raise InvalidUpdate('Task is registered more than once')
    if not rows:
        return None
    nid = rows[0][0]
    orchestration.events(s, nid)
    return orchestration.node(s, nid)['chat_key']


def _checkpoint_clear(s, workspace, flow_id):
    if flow_id is None:
        return True
    flow, history = _flow(s, workspace, flow_id)
    phase = history[-1]['to'] if history else flow['initial_state']
    if phase in _BUSY:
        return False
    root = next((event['data']['root_node'] for event in reversed(history) if 'root_node' in event['data']), None)
    if root:
        try:
            idle(s, root)
        except InvalidUpdate:
            return False
    return True


def _change(s, workspace, chat_key, flow_id, expected_cursor, kind):
    # The caller owns commit/rollback, including any matching workflow close.
    if not s.db.in_transaction:
        s.db.execute('BEGIN IMMEDIATE')
    nid, history = _history(s, workspace, chat_key)
    order = _ordered(history)
    projection = _project(s, workspace, history, order)
    current = projection['focused']
    candidate = _row(s, workspace, flow_id)
    registered = owner(s, workspace, flow_id)
    cursor_matches = expected_cursor == projection['cursor']
    target_matches = (registered is None and candidate['state'] not in orchestration.TERMINAL
                      if kind == _ATTACHED else
                      registered == chat_key and flow_id in order and
                      (kind == _CLOSED or candidate['state'] not in orchestration.TERMINAL))
    checkpoint_clear = (cursor_matches and target_matches
                        and _checkpoint_clear(s, workspace, flow_id)
                        and (kind == _CLOSED or current == flow_id or _checkpoint_clear(s, workspace, current)))
    frame = KarmaAdapter().task_transition(cursor_matches, target_matches, checkpoint_clear)
    if frame['decision'] != 'ALLOW':
        if not cursor_matches:
            reason = 'Task stack changed. Refresh the open tasks and try again.'
        elif not target_matches:
            reason = 'This task is unavailable in the current chat.'
        else:
            reason = 'Wait for active work to settle before switching or closing this task.'
        raise TaskStackStop(reason, frame)
    if kind == _SELECTED and current == flow_id:
        return projection
    if nid is None:
        nid = orchestration.create(s, 'task_stack', workspace=str(workspace), chat_key=chat_key)['id']
    event = orchestration.transition(
        s, nid, 'READY', kind,
        {'flow_id': flow_id, 'previous_focus': current, 'workflow_cursor': candidate['cursor']},
        frame,
    )
    history.append(event)
    return _project(s, workspace, history, _ordered(history))


def select(s, workspace, chat_key, flow_id, expected_cursor, *, attach=False):
    """Focus an indexed flow, or register a host-created flow when attach is true."""
    return _change(s, workspace, chat_key, flow_id, expected_cursor, _ATTACHED if attach else _SELECTED)


def close(s, workspace, chat_key, flow_id, expected_cursor):
    """Remove a flow from the stack. The caller settles its lifecycle in this transaction."""
    return _change(s, workspace, chat_key, flow_id, expected_cursor, _CLOSED)


def assert_focused(s, workspace, chat_key, flow_id):
    projection = view(s, workspace, chat_key)
    if projection['focused'] != flow_id or flow_id is None:
        raise InvalidUpdate('This task is parked. Select it before continuing.')
    return projection
