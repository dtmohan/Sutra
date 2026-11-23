"""Host policy for elastic review allocations beneath a workspace ceiling.

Only committed host observations are inputs. The workspace policy gives Sutra
permission to amend a task's working allowance. It is not a dispatch permit.
"""
import json
import math
import os

from allocation_recovery import MAXIMA
from context_manager import InvalidUpdate
from karma_adapter import KarmaAdapter

ENVIRONMENT_KEY = 'SUTRA_WORKSPACE_ALLOCATION'
PROFILE = 'workspace-review-allocation-v1'
DEFAULT_LIMITS = {'operations': 2000, 'calls': 80, 'tokens': 500000,
                  'source_bytes': 128 * 1024**2, 'seconds': 3600}
MINIMUM_INCREASE = {'operations': 4, 'calls': 2, 'tokens': 4096,
                    'source_bytes': 4096, 'seconds': 30}
MAX_AUTOMATIC_AMENDMENTS = 12


def valid_limits(limits):
    return (isinstance(limits, dict) and limits.keys() == MAXIMA.keys()
            and all(type(value) is int and 0 < value <= MAXIMA[key]
                    for key, value in limits.items())
            and limits['source_bytes'] >= 1024)


def policy():
    """Read a host-supplied policy. Invalid configuration disables auto grants."""
    try:
        supplied = json.loads(os.environ.get(ENVIRONMENT_KEY, '{}'))
    except (TypeError, ValueError):
        return None
    if (not isinstance(supplied, dict) or set(supplied) - (MAXIMA.keys() | {'enabled'})
            or supplied.get('enabled', True) is not True):
        return None
    limits = dict(DEFAULT_LIMITS, **{k: v for k, v in supplied.items() if k != 'enabled'})
    return limits if valid_limits(limits) else None


def _need(stop):
    """Return (dimension, measured minimum increase), or no justified grant."""
    status = stop.get('status')
    measured = stop.get('allocation_need')
    if status not in ('task_budget', 'budget_stopped', 'context_budget'):
        return None
    if isinstance(measured, dict):
        dimension, minimum = measured.get('dimension'), measured.get('minimum')
        if (isinstance(dimension, str) and dimension in MAXIMA
                and type(minimum) is int and minimum > 0):
            if status != 'context_budget':
                return dimension, minimum
            # A real model / runtime context stop cannot be fixed by authorizing
            # more task tokens. The host must supply both measured bounds.
            details = stop.get('allocation')
            if not isinstance(details, dict):
                return None
            required, model, slots = (measured.get(key) for key in
                                      ('required_context', 'model_context', 'slots'))
            available = details.get('available_tokens')
            from allocation_recovery import exact_context_fit
            fits_model = (type(required) is int and type(model) is int and
                          (exact_context_fit(details, measured) if details.get('context_profile') == 2
                           else required <= model))
            if (dimension == 'tokens' and type(required) is int and type(model) is int
                    and type(slots) is int and slots in (1, 2)
                    and type(available) is int and available >= 0
                    and details.get('effective_context_tokens') == model
                    and details.get('attempt_slots_reserved') == slots
                    and available // slots < required and fits_model
                    and slots * required - available == minimum):
                return dimension, minimum
    if status == 'context_budget':
        return None
    frame = stop.get('frame') or stop.get('budget')
    if (not isinstance(frame, dict) or frame.get('profile') != 'task-allocations-v1'
            or frame.get('adapter') != 'karma' or frame.get('decision') == 'ALLOW'):
        return None
    try:
        used, limits, cost = (frame[key] for key in ('used', 'limits', 'reserved_next'))
        check = KarmaAdapter().allocations(used, limits, cost)
        dimension = check['limiting_dimension']
        if (check['decision'] != frame['decision'] or dimension != frame['limiting_dimension']
                or dimension not in MAXIMA):
            return None
        # Keep enough room for the denied operation and a positive remainder
        # for the next kernel check (including zero-cost checks at exact exhaustion).
        minimum = math.floor(used[dimension] + cost.get(dimension, 0)) + 1 - limits[dimension]
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if minimum > 0:
        return dimension, math.ceil(minimum)


def _workspace_gate(dimension, before, ceilings, extra):
    return KarmaAdapter().allocations({dimension: before[dimension]},
                                      {dimension: ceilings[dimension]},
                                      {dimension: extra})


def verified_evidence(record):
    """Check the recorded policy and kernel admission while replaying the ledger."""
    evidence = record.get('workspace_policy')
    p = record['proposal']
    if not isinstance(evidence, dict) or evidence.get('profile') != PROFILE:
        return False
    ceilings = evidence.get('ceilings')
    dimension = evidence.get('dimension')
    minimum = evidence.get('minimum')
    if (not valid_limits(ceilings) or not isinstance(dimension, str) or dimension not in MAXIMA
            or type(minimum) is not int or minimum <= 0
            or not isinstance(evidence.get('workspace'), str) or not evidence['workspace']
            or not isinstance(evidence.get('stop_receipt'), str) or not evidence['stop_receipt']
            or set(p['additions']) != {dimension}
            or type(p['additions'][dimension]) is not int or p['additions'][dimension] < minimum
            or p['before'].keys() != ceilings.keys() or p['after'].keys() != ceilings.keys()
            or any(p['after'][key] > ceilings[key] for key in ceilings)):
        return False
    gate = _workspace_gate(dimension, p['before'], ceilings, p['additions'][dimension])
    return gate['decision'] == 'ALLOW' and gate == evidence.get('kernel')


def try_amend(s, fid, meta):
    """Apply at most one grant for a committed pause, without dispatching work.

    The orchestrator must first check the live workspace grant and task binding.
    A missing measurement, another kind of stop or non-review flow yields None.
    """
    import orchestration as m
    from allocation_recovery import _append, child_increase, child_additions, preview
    from orchestration_flow import info, identity, recovery
    from review_budget import usage

    with s.db:
        s.db.execute('BEGIN IMMEDIATE')
        flow, data = info(s, fid)
        if m.state(s, fid) != 'PAUSED' or not data.get('root_node'):
            return None
        stop = data.get('workflow_stop') or data.get('mapping_stop') or {}
        need = _need(stop)
        if need is None:
            return None
        dimension, minimum = need
        result = {'status': 'denied', 'dimension': dimension, 'minimum': minimum}
        root = m.node(s, data['root_node'])
        sid = root['session']
        if (sid != meta.get('active') or data.get('binding') != identity(s, sid)
                or recovery(s, fid) or (data.get('permission') or {}).get('operation', 'read') != 'read'):
            return dict(result, reason='task_binding')
        constraints = s.snapshot(sid)['constraints']
        if not constraints.get('auto_review') or not constraints.get('review_only'):
            return None
        if constraints.get('edits_allowed') or constraints.get('tests_allowed'):
            return None
        ceilings = policy()
        if ceilings is None:
            return dict(result, reason='policy_disabled')
        events = m.events(s, fid)
        stop_event = next((e for e in reversed(events) if e['to'] == 'PAUSED'
                           and (e['data'].get('workflow_stop') or e['data'].get('mapping_stop')) == stop), None)
        if stop_event is None:
            return None
        budget = usage(s, sid)
        before = budget['limits']
        room = ceilings[dimension] - before[dimension]
        if room < minimum or any(before[key] > limit for key, limit in ceilings.items()):
            return dict(result, reason='workspace_limit', limit=ceilings[dimension],
                        remaining=max(room, 0))

        past = [
            json.loads(raw) for raw, in s.db.execute(
                "SELECT record_json FROM allocation_amendments WHERE session=? AND contract_hash=? "
                "AND json_extract(record_json,'$.authority')='host_workspace_policy' ORDER BY version DESC",
                (sid, root['binding']['contract_hash']),
            )
        ]
        if len(past) >= MAX_AUTOMATIC_AMENDMENTS:
            return dict(result, reason='retry_limit')
        for record in past:
            evidence = record['workspace_policy']
            if evidence['stop_receipt'] == stop_event['receipt']:
                return dict(result, reason='already_amended')
            if (evidence['dimension'] == dimension and record['proposal']['flow_id'] == fid
                    and record['proposal']['used'][dimension] == budget['used'][dimension]):
                return dict(result, reason='no_progress')

        for nid in m.descendants(s, root['id']):
            state = m.state(s, nid)
            if (state in ('RUNNING', 'EXECUTING_ACTION', 'CANCELLING')
                    or s.db.execute(
                        'SELECT 1 FROM orchestration_charges c LEFT JOIN orchestration_settlements r '
                        'ON r.charge_id=c.id WHERE c.node_id=? AND r.charge_id IS NULL', (nid,),
                    ).fetchone()):
                return dict(result, reason='pending_execution')
        child = child_increase(s, data)
        # Settled quantitative child stops amend the existing execution and branch.
        # Other stops still require an idle execution before its renewal.
        if not child and (m.state(s, root['id']) != 'READY'
                          or any(m.state(s, nid) not in m.TERMINAL
                                 for nid in m.descendants(s, root['id'])[1:])):
            return dict(result, reason='pending_execution')
        step = max(MINIMUM_INCREASE[dimension], math.ceil(budget['base_limits'][dimension] / 4))
        extra = child_additions(child)[dimension] if child else min(room, max(minimum, step))
        if extra > room:
            return dict(result, reason='workspace_limit', limit=ceilings[dimension], remaining=max(room, 0))
        gate = _workspace_gate(dimension, before, ceilings, extra)
        if gate['decision'] != 'ALLOW':
            return dict(result, reason='workspace_limit', limit=ceilings[dimension],
                        remaining=max(room, 0))
        proposal = preview(s, fid, meta, {dimension: extra})
        if proposal['allocation_gate']['decision'] != 'ALLOW':
            return dict(result, reason='another_budget_dimension')
        evidence = {'profile': PROFILE, 'workspace': flow['workspace'],
                    'ceilings': ceilings, 'stop_receipt': stop_event['receipt'],
                    'dimension': dimension, 'minimum': minimum, 'kernel': gate}
        amendment = _append(s, fid, proposal, workspace_policy=evidence)
        return {'status': 'amended', 'dimension': dimension, 'amendment': amendment}
