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


def effective(s, sid, base, *, contract_hash=None, _visited=()):
    """Read without migrations so the monitor can use a read-only connection."""
    origin = contract_hash or s.origin(sid)['contract_hash']
    if origin in _visited:
        raise InvalidUpdate('Allocation carry contains a contract cycle')
    limits = dict(base); version = 0; previous = 'GENESIS'
    from native_access import allocation_carry
    carry = allocation_carry(s, sid, origin)
    if carry:
        saved = carry['carry_allocation']
        inherited = effective(s, sid, base, contract_hash=carry['before'], _visited=(*_visited, origin))
        if (saved['base_limits'] != base or inherited != (saved['limits'], saved['version'], saved['head'])):
            raise InvalidUpdate('Read-scope correction allocation prefix differs from its receipts')
        limits, version, previous = inherited
    if not s.db.execute("SELECT 1 FROM sqlite_master WHERE name='allocation_amendments'").fetchone():
        return limits, version, previous
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


def child_additions(child):
    """Keep historical token proposals unchanged; new dimensions are explicit."""
    if child.get('format') == 2:
        return dict(child['additions'])
    return {'tokens': child['tokens']}


def _quantitative_child_increase(s, data, stop, frame):
    import orchestration as m
    dimension = frame.get('limiting_dimension')
    branch = (stop.get('child_repair') or {}).get('branch_id') or frame.get('node_id')
    if (stop.get('status') != 'task_budget' or frame.get('profile') != 'task-allocations-v1'
            or frame.get('adapter') != 'karma' or dimension not in MAXIMA
            or dimension == 'tokens' or branch not in data.get('children', ())):
        return None
    try:
        cost = frame['reserved_next']
        required = cost.get(dimension, 0)
        check = KarmaAdapter().allocations(frame['used'], frame['limits'], cost)
        if (check['decision'] == 'ALLOW' or check['decision'] != frame.get('decision')
                or check['limiting_dimension'] != dimension or type(required) not in (int, float)
                or not math.isfinite(required) or required < 0):
            raise ValueError('quantitative stop mismatch')
    except (KeyError, TypeError, ValueError, OverflowError):
        raise InvalidUpdate('The child allocation stop differs from its measured quantitative gate') from None
    child = m.checked(s, branch); root = m.checked(s, data['root_node'])
    if (child['kind'] != 'branch' or child['parent_id'] != root['id']
            or m.state(s, child['id']) != 'READY' or root['kind'] != 'execution'
            or m.state(s, root['id']) != 'WAITING_CHILDREN'):
        raise InvalidUpdate('The saved child no longer owns this allocation stop')
    frames = m.gates(s, child['id'], {})
    if frame.get('node_id') not in {item['node_id'] for item in frames}:
        raise InvalidUpdate('The child allocation stop is outside its execution ancestors')
    extra = max(math.ceil(math.floor(item['used'][dimension] + required) + 1 - item['limits'][dimension])
                for item in frames)
    if extra <= 0:
        raise InvalidUpdate('This child already has the measured capacity. Resume the saved review.')
    gates = {}
    for item in frames:
        limits = dict(item['limits']); limits[dimension] += extra
        gate = KarmaAdapter().allocations(item['used'], limits, cost)
        if gate['decision'] != 'ALLOW':
            raise InvalidUpdate('The child allocation cannot cover the measured request without another budget dimension')
        gates[item['node_id']] = gate
    return {'format': 2, 'branch': child['id'], 'execution': root['id'],
            'dimension': dimension, 'additions': {dimension: extra},
            'minimum_cost': dict(cost), 'kernel': gates}


def quantitative_event_additions(s, node, event):
    """Replay only additions bound to the same immutable amendment receipt."""
    data = event['data']; additions = data.get('additions')
    if (type(data.get('version')) is not int or data['version'] != 2
            or not isinstance(additions, dict) or not additions
            or set(additions) - (MAXIMA.keys() & node['limits'].keys())
            or any(type(amount) is not int or amount <= 0 for amount in additions.values())):
        raise InvalidUpdate('Invalid quantitative child allocation event')
    row = s.db.execute('SELECT record_json FROM allocation_amendments WHERE digest=?',
                       (data.get('allocation_receipt'),)).fetchone()
    if not row or sha('allocation-amendment-v1:' + row[0]) != data.get('allocation_receipt'):
        raise InvalidUpdate('Quantitative child allocation receipt is missing or changed')
    record = json.loads(row[0]); proposal = record['proposal']; child = proposal.get('child_allocation') or {}
    if (child.get('format') != 2 or child.get('additions') != additions or proposal['additions'] != additions
            or node['id'] not in (child.get('branch'), child.get('execution'))
            or node['session'] != proposal['session'] or node['binding'] != proposal['binding']
            or proposal.get('proposal_id') != proposal_hash(proposal)
            or event.get('kernel') != child.get('kernel', {}).get(node['id'])):
        raise InvalidUpdate('Quantitative child allocation differs from its approved proposal')
    gate = event['kernel']
    if (gate.get('decision') != 'ALLOW'
            or gate != KarmaAdapter().allocations(gate['used'], gate['limits'], child['minimum_cost'])):
        raise InvalidUpdate('Quantitative child allocation kernel receipt changed')
    return dict(additions)


def exact_context_fit(details, need):
    """A financial reservation may exceed an independently measured token window.

    This validates a host observation already committed in the stop receipt.
    It never grants physical capacity; dispatch still measures the live model.
    """
    if details.get('context_profile') != 2:
        return False
    proof = details.get('physical_fit')
    if not isinstance(proof, dict):
        return False
    try:
        from context_window import validate_window, STRATA_CONTEXT_SLACK
        from smriti_adapter import SmritiAdapter
        window = validate_window(proof['window'])
        basis, slack = 'strata_count_tokens_v1', STRATA_CONTEXT_SLACK
        if window['provider'] == 'sutra_remote':
            from remote_context import BASIS, CONTEXT_SLACK
            basis, slack = BASIS, CONTEXT_SLACK
        cap = need['model_context']; count = proof['prompt_tokens']
        output = proof['output_reserve']
        if (type(cap) is not int or type(count) is not int or type(output) is not int
                or cap != proof['context_tokens'] or cap > window['context_tokens']
                or output != details['output_reserve'] or count <= 0 or output <= 0
                or proof.get('version') != 1 or proof.get('basis') != basis
                or proof.get('context_slack_tokens') != slack):
            return False
        for key in ('wire_sha256', 'packet_sha256'):
            value = proof.get(key)
            if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                return False
        frame = SmritiAdapter().context(count, output + slack, cap)
        return frame['decision'] == 'ALLOW' and frame == proof.get('capacity')
    except (KeyError, TypeError, ValueError, InvalidUpdate):
        return False


def child_increase(s, data):
    """Measure an in-place amendment for the paused, settled child."""
    import orchestration as m
    stop = data.get('workflow_stop') or {}
    if (stop.get('resume_phase') or stop.get('stage')) != 'CHILDREN':
        return None
    frame = stop.get('frame') or {}
    if frame.get('limiting_dimension') in MAXIMA and frame['limiting_dimension'] != 'tokens':
        return _quantitative_child_increase(s, data, stop, frame)
    sizing = stop.get('child_context') or {}
    need = sizing.get('task_allocation_need') or {}
    required = need.get('required_context')
    cost = {'tokens': required}
    branch = sizing.get('branch_id')
    if not sizing:
        # A repair can exhaust its branch share after earlier replies have
        # settled. Retain that branch and its checkpoints when adding capacity.
        frame = stop.get('frame') or {}
        branch = (stop.get('child_repair') or {}).get('branch_id') or frame.get('node_id')
        if (stop.get('status') != 'task_budget' or frame.get('profile') != 'task-allocations-v1'
                or frame.get('adapter') != 'karma' or frame.get('limiting_dimension') != 'tokens'
                or branch not in data.get('children', ())):
            return None
        cost = frame.get('reserved_next') or {}
        required = cost.get('tokens')
        check = KarmaAdapter().allocations(frame['used'], frame['limits'], cost)
        if (check['decision'] == 'ALLOW' or check['decision'] != frame.get('decision')
                or check['limiting_dimension'] != 'tokens' or type(required) is not int or required <= 0):
            raise InvalidUpdate('The child allocation stop differs from its measured token gate')
    elif (need.get('dimension') != 'tokens' or need.get('slots') != 1
          or type(required) is not int or required <= 0
          or not (exact_context_fit(sizing, need) if sizing.get('context_profile') == 2
                  else required <= sizing.get('effective_context_tokens', 0))):
        raise InvalidUpdate('This child stop has no measured task allocation increase. Select a model with a larger context or address the reported limit.')
    child = m.checked(s, branch)
    root = m.checked(s, data['root_node'])
    if (child['id'] not in data.get('children', ()) or child['kind'] != 'branch'
            or child['parent_id'] != root['id'] or m.state(s, child['id']) != 'READY'
            or root['kind'] != 'execution' or m.state(s, root['id']) != 'WAITING_CHILDREN'):
        raise InvalidUpdate('The saved child no longer owns this allocation stop')
    frames = m.gates(s, child['id'], {})
    left = min(frame['limits']['tokens'] - frame['used']['tokens'] for frame in frames)
    extra = math.ceil(required - left) + (0 if sizing else 1)
    if extra <= 0:
        raise InvalidUpdate('This child already has the measured capacity. Resume the saved review.')
    gates = {}
    for frame in frames:
        limit = dict(frame['limits'], tokens=frame['limits']['tokens'] + extra)
        gate = KarmaAdapter().allocations(frame['used'], limit, cost)
        if gate['decision'] != 'ALLOW':
            raise InvalidUpdate('The child allocation cannot cover the measured request')
        gates[frame['node_id']] = gate
    return {'branch': child['id'], 'execution': root['id'], 'tokens': extra,
            'minimum_context': required, 'kernel': gates}


def preview(s, fid, meta, additions=None):
    import orchestration as m
    from review_budget import usage
    n, data, sid = eligible(s, fid, meta)
    budget = usage(s, sid)
    child = child_increase(s, data)
    if child:
        permitted = child_additions(child)
        if additions is not None and additions != permitted:
            raise InvalidUpdate('Only the measured child token increase can be approved for this stop' if child.get('format') != 2
                                else 'Only the measured child quantitative increase can be approved for this stop')
        additions = permitted
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
    if child:
        p['child_allocation'] = child
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
    child = expected.get('child_allocation')
    if child:
        for nid in (child['execution'], child['branch']):
            if child.get('format') == 2:
                m.transition(s, nid, m.state(s, nid), 'CHILD_QUANTITATIVE_ALLOCATION_INCREASED',
                             {'version': 2, 'additions': child_additions(child), 'allocation_receipt': digest},
                             child['kernel'][nid])
            else:
                m.transition(s, nid, m.state(s, nid), 'CHILD_ALLOCATION_INCREASED',
                             {'tokens': child['tokens'], 'allocation_receipt': digest},
                             child['kernel'][nid])
    else:
        renew_after_grant(s, fid)
    evidence = {'allocation_receipt': digest, 'allocation_version': version + 1,
                'allocation_authority': authority}
    if child:
        from orchestration_flow import info
        stop = info(s, fid)[1]['workflow_stop']
        evidence['workflow_stop'] = dict(stop, child_allocation_granted=child_additions(child) if child.get('format') == 2 else child['tokens'])
    m.transition(s, fid, 'PAUSED', 'ALLOCATION_INCREASED', evidence, expected['allocation_gate'])
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
