"""Host-owned discovery feedback and a finite, uncharged stage allowance.

Progress is structural, not a semantic verdict. Every continued model call and
tool effect still uses the existing gateway, permissions and task allocations.
The initial planning allowance is anchored in the first invocation payload;
reloads, model changes and later grants cannot replenish it.
"""
import copy
import json

from context_manager import InvalidUpdate, canonical, sha
from discovery_governor import assess
from karma_adapter import KarmaAdapter

VERSION = 1
PHASE_MULTIPLIER = 2
ENVELOPE_VERSION = 2


def observe(sid, parent, key, receipts, catalog_files):
    return assess({'format': 1, 'binding': {
        'session': sid, 'task': parent['contract']['task_id'], 'stage': 'discovery',
        'contract_hash': parent['contract']['contract_hash']},
        'steps': [{'kind': 'repository', 'record': record} for record in receipts],
        'complexity': {'catalog_files': catalog_files}})


def feedback(observation, accepted):
    """One factual delta on entering yellow, without repeating policy prose."""
    previous = ((accepted[-1][1].get('discovery_control') or {}).get('progress') or {}) if accepted else {}
    if observation['status'] != 'yellow' or previous.get('status', 'green') != 'green':
        return None
    return {'source_acquisition': {
        'non_advancing_operations': observation['no_progress_streak'],
        'repeated_requests': observation['repeated_request_streak'],
        'recent_operations': observation['host_delta']['recent_operations']},
        'meaning': 'These are source-acquisition observations, not a judgment of understanding.',
        'next_step': 'Use the supplied bodies to prepare a supported draft, or identify the specific missing evidence needed. Preserve unresolved gaps.'}


def attach(sid, parent, packet, key, receipts, accepted):
    observation = observe(sid, parent, key, receipts, packet['repository']['total_files'])
    packet['discovery_control_version'] = VERSION
    packet['discovery_control'] = {'key': key, 'progress': observation,
        'kernel': KarmaAdapter().discovery(observation)}
    delta = feedback(observation, accepted)
    if delta is not None:
        packet['host_feedback'] = delta
    return observation


def check(sid, packet, key, receipts, accepted):
    if 'discovery_control_version' not in packet:
        if any(name in packet for name in ('discovery_control', 'host_feedback', 'discovery_envelope')):
            raise InvalidUpdate('Discovery feedback requires its versioned host profile')
        return
    if (type(packet['discovery_control_version']) is not int or packet['discovery_control_version'] != VERSION
            or packet.get('discovery_selection_version') != 3):
        raise InvalidUpdate('Unsupported discovery control profile')
    expected = observe(sid, packet, key, receipts, packet['repository']['total_files'])
    wanted = {'key': key, 'progress': expected, 'kernel': KarmaAdapter().discovery(expected)}
    if packet.get('discovery_control') != wanted or packet.get('host_feedback') != feedback(expected, accepted):
        raise InvalidUpdate('Discovery host feedback changed or belongs to another checkpoint')


def _envelope_record(sid, key, plan, capacity_anchor):
    basis = plan.get('stage_allowance_cost', plan.get('holdback'))
    if not basis:
        return None
    record = {'version': ENVELOPE_VERSION, 'session': sid, 'key': key,
        'capacity_digest': plan['digest'], 'multiplier': PHASE_MULTIPLIER,
        'capacity_plan_anchor': capacity_anchor,
        'token_ceiling': PHASE_MULTIPLIER * basis['tokens'],
        'basis': 'initial_writer_and_declared_reviewer_allowance',
        'durable_reservation': False, 'semantic_completion': False}
    record['digest'] = sha('discovery-envelope-v1:' + canonical(record))
    return record


def envelope(s, sid, key, records, plan):
    for _, packet, _ in records:
        if packet.get('discovery_control_version') == VERSION and packet.get('discovery_envelope'):
            value = copy.deepcopy(packet['discovery_envelope'])
            validate_envelope(value, sid, key, s=s)
            return value
    if plan.get('holdback') is not None:
        from discovery_capacity import validate
        validate(plan)
        with s.db:
            s._audit('DISCOVERY_CAPACITY_PLAN', {'session': sid, 'key': key, 'plan': plan})
            seq, digest = s.db.execute('SELECT seq,event_hash FROM page_audit ORDER BY seq DESC LIMIT 1').fetchone()
            value = _envelope_record(sid, key, plan, {'seq': seq, 'hash': digest})
            s._audit('DISCOVERY_STAGE_ALLOWANCE', value)
            seq, digest = s.db.execute('SELECT seq,event_hash FROM page_audit ORDER BY seq DESC LIMIT 1').fetchone()
        value.update(anchor_seq=seq, anchor_hash=digest)
        return value
    return None


def validate_envelope(value, sid, key, *, s=None):
    if (not isinstance(value, dict) or value.get('version') not in (1, ENVELOPE_VERSION)
            or type(value.get('version')) is not int or value.get('session') != sid or value.get('key') != key
            or value.get('multiplier') != PHASE_MULTIPLIER
            or type(value.get('token_ceiling')) is not int or not 0 < value['token_ceiling'] <= 2**53-1
            or value.get('durable_reservation') is not False or value.get('semantic_completion') is not False):
        raise InvalidUpdate('Invalid discovery stage allowance')
    raw = {key: data for key, data in value.items() if key not in ('digest', 'anchor_seq', 'anchor_hash')}
    if value.get('digest') != sha('discovery-envelope-v1:' + canonical(raw)):
        raise InvalidUpdate('Discovery stage allowance changed')
    if s is not None:
        if type(value.get('anchor_seq')) is not int or value['anchor_seq'] < 1:
            raise InvalidUpdate('Discovery allowance needs its original audit anchor')
        row = s.db.execute('SELECT event,payload_json,previous_hash,event_hash FROM page_audit WHERE seq=?',
                           (value['anchor_seq'],)).fetchone()
        record = {key: data for key, data in value.items() if key not in ('anchor_seq', 'anchor_hash')}
        if (not row or row[0] != 'DISCOVERY_STAGE_ALLOWANCE' or row[1] != canonical(record)
                or row[3] != value.get('anchor_hash')
                or sha(canonical([value['anchor_seq'], row[0], row[1], row[2]])) != row[3]):
            raise InvalidUpdate('Discovery allowance differs from its original audit anchor')
        if value['version'] == ENVELOPE_VERSION:
            _validate_capacity_anchor(s, sid, value)
    return value


def _validate_capacity_anchor(s, sid, value):
    from discovery_capacity import MAX_PLAN_BYTES, validate
    anchor = value.get('capacity_plan_anchor')
    if (not isinstance(anchor, dict) or set(anchor) != {'seq', 'hash'}
            or type(anchor['seq']) is not int or not 0 < anchor['seq'] < value['anchor_seq']):
        raise InvalidUpdate('Discovery allowance needs its initial capacity observation')
    row = s.db.execute('SELECT event,payload_json,previous_hash,event_hash FROM page_audit WHERE seq=?',
                       (anchor['seq'],)).fetchone()
    if (not row or row[0] != 'DISCOVERY_CAPACITY_PLAN' or len(row[1].encode()) > MAX_PLAN_BYTES + 1024
            or row[3] != anchor['hash'] or sha(canonical([anchor['seq'], row[0], row[1], row[2]])) != row[3]):
        raise InvalidUpdate('Initial discovery capacity observation changed')
    try:
        record = json.loads(row[1])
    except ValueError as exc:
        raise InvalidUpdate('Invalid initial discovery capacity record') from exc
    if (not isinstance(record, dict) or set(record) != {'session', 'key', 'plan'}
            or record.get('session') != sid or record.get('key') != value['key']):
        raise InvalidUpdate('Initial discovery capacity observation belongs to another task')
    plan = validate(record.get('plan'))
    if (plan.get('binding') or {}).get('session') != sid:
        raise InvalidUpdate('Initial discovery capacity plan belongs to another session')
    basis = plan.get('stage_allowance_cost', plan.get('holdback'))
    if (plan.get('digest') != value.get('capacity_digest') or not basis
            or value['token_ceiling'] != PHASE_MULTIPLIER * basis['tokens']):
        raise InvalidUpdate('Discovery allowance differs from its initial capacity measurements')


def spending(s, sid, records):
    """Use actual immutable settlements, including rejected model attempts."""
    from budget_accounting import used
    tokens = 0
    for iid, _, receipt in records:
        row = s.db.execute('SELECT i.record_json,i.digest,r.id,r.record_json FROM invocations i '
            'JOIN invocation_receipts r ON r.invocation_id=i.id AND r.session=i.session '
            'WHERE i.id=? AND i.session=?', (iid, sid)).fetchone()
        if not row or sha('invocation-v1:' + row[0]) != row[1] or receipt['invocation_hash'] != row[1]:
            raise InvalidUpdate('Discovery spending envelope changed')
        if (row[3] != canonical(receipt) or sha('invocation-receipt-v1:' + row[3]) != row[2]
                or receipt.get('invocation_id') != iid or receipt.get('session') != sid):
            raise InvalidUpdate('Discovery spending receipt changed')
        tokens += used(json.loads(row[0]), receipt)
    return {'calls': len(records), 'tokens': tokens}


def stage_gate(s, sid, owner, value, records, next_cost):
    if value is None:
        return None
    validate_envelope(value, sid, value['key'], s=s)
    spent = spending(s, sid, records)
    # The first bounded call may exceed the planning allowance. In that case
    # dispatching it is not required merely to establish a model checkpoint.
    frame = KarmaAdapter().allocations({'tokens': spent['tokens']},
        {'tokens': value['token_ceiling']}, {'tokens': next_cost['tokens']})
    return {'profile': 'discovery-stage-capacity-v1', 'used': spent,
        'token_ceiling': value['token_ceiling'], 'proposed_tokens': next_cost['tokens'],
        'kernel': frame, 'fits': frame['decision'] == 'ALLOW',
        'meaning': 'Finite discovery planning allowance; all original task spending remains charged.'}
