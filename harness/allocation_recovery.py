"""Host allocation amendments; proposals never grant themselves.

The task contract and usage ledger stay immutable. A confirmed amendment adds
capacity in a separately versioned, hash-chained record. No model permissions,
context-window capability or hardware limits are changed here.
"""
import json
import math
import time
from context_manager import InvalidUpdate, canonical, sha
from karma_adapter import KarmaAdapter

MAXIMA = {'operations': 1000000, 'calls': 1000000, 'tokens': 1000000,
          'source_bytes': 1024000000, 'seconds': 1000000}


def initialize(s):
    s.db.execute('''CREATE TABLE IF NOT EXISTS allocation_amendments(
        session TEXT, contract_hash TEXT, version INTEGER, record_json TEXT, digest TEXT,
        PRIMARY KEY(session,contract_hash,version))''')
    for op in ('UPDATE', 'DELETE'):
        s.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_allocation_{op} BEFORE {op} ON allocation_amendments BEGIN SELECT RAISE(ABORT,'immutable allocation amendment'); END")


def effective(s, sid, base):
    """Read without migrations so the monitor can use a read-only connection."""
    limits = dict(base); version = 0; previous = 'GENESIS'
    if not s.db.execute("SELECT 1 FROM sqlite_master WHERE name='allocation_amendments'").fetchone():
        return limits, version, previous
    origin = s.origin(sid)['contract_hash']
    for number, raw, digest in s.db.execute('SELECT version,record_json,digest FROM allocation_amendments WHERE session=? AND contract_hash=? ORDER BY version', (sid, origin)):
        r = json.loads(raw)
        p = r['proposal']
        if (number != version + 1 or r['version'] != number or r['previous'] != previous
                or sha('allocation-amendment-v1:' + raw) != digest
                or r['authority'] not in ('host_user_confirmation', 'host_workspace_policy')
                or p['session'] != sid or p['binding']['contract_hash'] != origin
                or p['before'] != limits or p['version'] != version
                or p['proposal_id'] != proposal_hash(p)):
            raise InvalidUpdate('Allocation amendment chain is corrupt')
        if r['authority'] == 'host_workspace_policy':
            from workspace_allocation import verified_evidence
            if not verified_evidence(r):
                raise InvalidUpdate('Workspace allocation admission evidence is corrupt')
        after = increased(limits, p['additions'])
        if p['after'] != after:
            raise InvalidUpdate('Allocation amendment totals differ')
        limits = after; version = number; previous = digest
    return limits, version, previous


def increased(limits, additions):
    if not isinstance(additions, dict) or not additions or set(additions) - set(limits):
        raise InvalidUpdate('Only existing task budget dimensions can be increased')
    result = dict(limits)
    for key, value in additions.items():
        if key not in MAXIMA or type(value) is not int or value <= 0:
            raise InvalidUpdate('Allocation additions must be positive integers')
        result[key] += value
        if result[key] > MAXIMA[key]:
            raise InvalidUpdate('Allocation exceeds supported ceiling: ' + key)
    return result


def proposal_hash(p):
    return sha('allocation-proposal-v1:' + canonical({k: v for k, v in p.items() if k != 'proposal_id'}))


def eligible(s, fid, meta):
    import orchestration as m
    from orchestration_flow import info, identity, recovery
    n, data = info(s, fid)
    if m.state(s, fid) != 'PAUSED' or not data.get('root_node'):
        raise InvalidUpdate('Allocation recovery requires a paused workspace task')
    sid = m.node(s, data['root_node'])['session']
    if sid != meta.get('active') or data.get('binding') != identity(s, sid):
        raise InvalidUpdate('Allocation recovery task or permissions changed')
    incompatibility = recovery(s, fid)
    if incompatibility and not incompatibility['restart_allowed']:
        raise InvalidUpdate('Recover the changed task binding before increasing allocation')
    stop = data.get('workflow_stop') or data.get('mapping_stop') or {}
    if stop.get('status') not in ('context_budget', 'task_budget', 'budget_stopped') and not (
            stop.get('status') == 'memory_fault' and stop.get('memory_fault', {}).get('code') == 'capacity'):
        raise InvalidUpdate('This stop does not request task allocation; inspect its resource or source error')
    # A UI click in another window must not amend a task with work in flight.
    ids = m.descendants(s, data['root_node'])
    for nid in ids:
        if m.state(s, nid) in ('RUNNING', 'EXECUTING_ACTION', 'CANCELLING'):
            raise InvalidUpdate('Wait for active task work before changing allocation')
        if s.db.execute('SELECT 1 FROM orchestration_charges c LEFT JOIN orchestration_settlements r ON r.charge_id=c.id WHERE c.node_id=? AND r.charge_id IS NULL', (nid,)).fetchone():
            raise InvalidUpdate('Recover interrupted work before changing allocation')
    return n, data, sid


def preview(s, fid, meta, additions=None):
    import orchestration as m
    from review_budget import usage
    n, data, sid = eligible(s, fid, meta)
    budget = usage(s, sid)
    # Offer enough *additional* headroom to reach the initial allocation's
    # remaining capacity again. Usage stays charged; totals visibly increase.
    if additions is None:
        additions = {k: min(math.ceil(v), MAXIMA[k] - budget['limits'][k])
                     for k, v in budget['used'].items()
                     if v > 0 and budget['limits'][k] < MAXIMA[k]}
        if not additions:
            raise InvalidUpdate('The task is already at the supported allocation ceiling')
    after = increased(budget['limits'], additions)
    p = {'format': 1, 'session': sid, 'flow_id': fid,
         'cursor': m.events(s, fid)[-1]['receipt'], 'binding': m.binding(s, sid),
         'version': budget['allocation_version'], 'before': budget['limits'],
         'used': budget['used'], 'additions': additions, 'after': after,
         'remaining_after': {k: after[k] - budget['used'][k] for k in after},
         'goal': s.snapshot(sid)['goal'], 'request': n['prompt'],
         'permissions': m.permissions(s, sid),
         'allocation_gate': KarmaAdapter().allocations(budget['used'], after),
         'context_policy': 'Fresh model capability, packet sizing and memory admission still apply. No inference is started by approval.'}
    p['proposal_id'] = proposal_hash(p)
    return p


def _append(s, fid, expected, workspace_policy=None):
    """Persist a freshly admitted host proposal inside its write transaction."""
    import orchestration as m
    from execution_sessions import renew_after_grant
    from review_budget import allocations

    if expected['allocation_gate']['decision'] != 'ALLOW':
        raise InvalidUpdate('Increased allocation still cannot admit the task')
    base = allocations(s.snapshot(expected['session'])['constraints'])
    _, version, previous = effective(s, expected['session'], base)
    authority = 'host_user_confirmation' if workspace_policy is None else 'host_workspace_policy'
    record = {'version': version + 1, 'previous': previous, 'authority': authority,
              'proposal': expected, 'at': time.time()}
    if workspace_policy is not None:
        from workspace_allocation import verified_evidence
        record['workspace_policy'] = workspace_policy
        if not verified_evidence(record):
            raise InvalidUpdate('Workspace policy cannot authorize the proposed allocation')
    raw = canonical(record)
    digest = sha('allocation-amendment-v1:' + raw)
    s.db.execute('INSERT INTO allocation_amendments VALUES(?,?,?,?,?)',
                 (expected['session'], expected['binding']['contract_hash'], version + 1, raw, digest))
    renew_after_grant(s, fid)
    m.transition(s, fid, 'PAUSED', 'ALLOCATION_INCREASED',
                 {'allocation_receipt': digest, 'allocation_version': version + 1,
                  'allocation_authority': authority}, expected['allocation_gate'])
    s._audit('ALLOCATION_INCREASED', {'session': expected['session'], 'receipt': digest,
                                     'version': version + 1, 'proposal_id': expected['proposal_id'],
                                     'authority': authority})
    result = {'receipt': digest, 'version': version + 1, 'limits': expected['after'], 'used': expected['used']}
    if workspace_policy is not None:
        result.update(authority=authority, dimension=workspace_policy['dimension'],
                      additions=expected['additions'], workspace_limit=workspace_policy['ceilings'])
    return result


def grant(s, fid, meta, proposed):
    """Called only by the private host UI after an explicit confirmation."""
    if not isinstance(proposed, dict):
        raise InvalidUpdate('A reviewed allocation proposal is required')
    with s.db:
        s.db.execute('BEGIN IMMEDIATE')
        expected = preview(s, fid, meta, proposed.get('additions'))
        if proposed != expected:
            raise InvalidUpdate('Allocation proposal is stale or changed; review a fresh proposal')
        return _append(s, fid, expected)
