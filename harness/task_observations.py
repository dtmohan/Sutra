"""Model task commitments and self-assessments. Observations, never authority.

Captured in the existing immutable invocation receipts. No extra inference,
kernel signals, repairs, grants, refunds or task-completion decisions originate
here. A commitment is made by the router before workspace execution; workers
inherit it even when the model changes. Direct calls without a router have no
precommitment, rather than manufacturing one after the work.
"""
import copy
import json
from context_manager import canonical, sha, InvalidUpdate
from protocol import obj

FIELD = 'task_observation'
PLACEMENT = '_host_placement'
REPORT_PLACEMENT = 'report.task_observation'
ROLES = {'conversation', 'chat_reviewer', 'subagent', 'coder'}
NOTICE = 'Observe only. Model claims are unverified; the original request and host contract remain authoritative.'
ASSESSMENT_GUIDE = ' Assess saved task_commitment criteria in task_observation. Claims only; pending actions are not completed.'



def text(n):
    return {'type': 'string', 'minLength': 1, 'maxLength': n}


def array(item, maximum, minimum=0):
    return {'type': 'array', 'items': item, 'minItems': minimum, 'maxItems': maximum}


def observation_schema(kind):
    if kind == 'commitment':
        return obj({'understanding': text(400),
                    'criteria': array(obj({'id': text(24), 'deliverable': text(200), 'done_when': text(240)}), 6, 1),
                    'assumptions': array(text(160), 3)})
    return obj({'judgement': dict(text(20), enum=['complete', 'partial', 'blocked']),
                'delivered': text(600), 'remaining': array(text(200), 6),
                'items': array(obj({'criterion_id': text(24),
                                   'status': dict(text(20), enum=['met', 'partial', 'unmet', 'unknown']),
                                   'evidence': text(300)}), 6, 1)})


def add_schema(schema, kind, payload=None):
    # Ask workers to emit the diagnostic, but never require it for decoding or admission.
    # Gateway extracts it before authoritative answer checks.
    if kind == 'assessment' and not (payload or {}).get('conversation_handoff', {}).get('task_commitment'):
        return schema
    schema['properties'][FIELD] = observation_schema(kind)
    return schema


def decoder_schema(schema):
    # Native decoders run before the gateway can split diagnostics from answers.
    # Constraining this optional value there would let an invalid observation
    # withhold valid work. capture() alone validates its recorded shape and size.
    result = copy.deepcopy(schema)
    if FIELD in result.get('properties', {}):
        result['properties'][FIELD] = {}
    if 'required' in result:
        result['required'] = [key for key in result['required'] if key != FIELD]
    return result


def answer_schema(schema):
    # A worker may emit an assessment. Validation of the actual
    # answer must not depend on that diagnostic field being present or correct.
    result = copy.deepcopy(schema)
    result.get('properties', {}).pop(FIELD, None)
    if 'required' in result:
        result['required'] = [key for key in result['required'] if key != FIELD]
    return result


def split(role, proposal):
    if role not in ROLES or not isinstance(proposal, dict):
        return proposal, {}
    if FIELD in proposal:
        # An ambiguous top-level plus nested diagnostic is NOT repaired: the
        # nested unknown report field remains subject to strict answer checks.
        clean = dict(proposal)
        return clean, {FIELD: clean.pop(FIELD)}
    report = proposal.get('report')
    if role == 'chat_reviewer' and isinstance(report, dict) and FIELD in report:
        clean = dict(proposal); clean['report'] = dict(report)
        return clean, {FIELD: clean['report'].pop(FIELD), PLACEMENT: REPORT_PLACEMENT}
    return proposal, {}


def restore(role, proposal, raw_fields):
    """Reconstruct the exact raw response for hashing, never answer admission."""
    if (role not in ROLES or not isinstance(proposal, dict) or FIELD in proposal
            or not isinstance(raw_fields, dict)):
        raise InvalidUpdate('Invalid observation reconstruction')
    if set(raw_fields) == {FIELD}:
        original = dict(proposal); original[FIELD] = raw_fields[FIELD]
        return original  # Historical top-level receipt representation.
    if (set(raw_fields) != {FIELD, PLACEMENT} or role != 'chat_reviewer'
            or raw_fields[PLACEMENT] != REPORT_PLACEMENT
            or not isinstance(proposal.get('report'), dict) or FIELD in proposal['report']):
        raise InvalidUpdate('Invalid nested observation placement')
    original = dict(proposal); original['report'] = dict(proposal['report'])
    original['report'][FIELD] = raw_fields[FIELD]
    return original


def receipt(s, iid):
    row = s.db.execute('SELECT id,record_json FROM invocation_receipts WHERE invocation_id=?', (iid,)).fetchone()
    if not row:
        return None
    data = json.loads(row[1])
    if sha('invocation-receipt-v1:' + row[1]) != row[0]:
        return None
    return data


def commitment(s, iid):
    """Restore data only, without loading executable results or widening scope."""
    rec = receipt(s, iid)
    if not rec or rec['role'] != 'conversation' or rec['status'] != 'accepted':
        return None
    obs = rec.get('task_observation')
    return copy.deepcopy(obs) if obs and obs['kind'] == 'commitment' else None


def context(record):
    if not record or record['status'] != 'recorded':
        return None
    # Avoid replaying the router's tool-free contract in a worker. The current
    # pinned workspace contract already supplies all permissions and constraints.
    return {key: copy.deepcopy(record[key]) for key in
            ('mode', 'authority', 'invocation_id', 'version', 'request', 'status', 'value')}


def capture(s, envelope, payload, proposal, raw_fields):
    role = envelope['role']
    if role not in ROLES:
        return None
    kind = 'commitment' if role == 'conversation' else 'assessment'
    ref = (payload.get('conversation_handoff') or {}).get('task_commitment')
    if kind == 'commitment' and (not isinstance(proposal, dict) or proposal.get('intent') != 'workspace'):
        return None  # Greetings and ordinary conversation have no task ceremony.
    if kind == 'assessment' and not ref and not raw_fields:
        return None
    previous = None
    if kind == 'commitment':
        previous = commitment(s, payload.get('routing_state', {}).get('previous'))
    result = {'schema_version': 1, 'mode': 'observe_only', 'kind': kind,
              'authority': 'model_unverified', 'notice': NOTICE,
              'invocation_id': envelope['id'], 'model': envelope['model'],
              'request': payload['request'], 'contract_hash': envelope['contract_hash'],
              'constraints': copy.deepcopy(payload['contract']['constraints']),
              'status': 'missing', 'issues': [], 'value': None}
    if kind == 'commitment':
        result.update(version=1 + (previous or {}).get('version', 0),
                      previous_version=(previous or {}).get('invocation_id'),
                      parent_conversation=payload.get('routing_state', {}).get('parent'))
    else:
        result.update(commitment_id=(ref or {}).get('invocation_id'),
                      commitment_version=(ref or {}).get('version'),
                      scope='branch' if role == 'subagent' else 'worker_response')
    if FIELD not in raw_fields:
        return result
    value = raw_fields[FIELD]
    try:
        from invocation_roles import validate_shape
        if len(canonical(value).encode()) > 8192:
            raise ValueError('Observation exceeds diagnostic allocation')
        validate_shape(value, observation_schema(kind))
        if kind == 'commitment':
            ids = [c['id'] for c in value['criteria']]
        else:
            if not ref or ref['status'] != 'recorded':
                raise ValueError('No recorded precommitment; retrospective claims are not precommitments')
            ids = [c['criterion_id'] for c in value['items']]
            expected = {c['id'] for c in ref['value']['criteria']}
            if set(ids) != expected:
                result['issues'].append('Assessment omits committed criteria or names unknown criteria')
            if value['judgement'] == 'complete' and (value['remaining'] or any(c['status'] != 'met' for c in value['items'])):
                result['issues'].append('Complete judgement conflicts with remaining work or unmet criteria')
        if len(ids) != len(set(ids)):
            result['issues'].append('Duplicate criterion identifiers')
        result.update(status='recorded', value=copy.deepcopy(value))
    except (InvalidUpdate, ValueError, TypeError, KeyError) as exc:
        result.update(status='malformed', issues=[str(exc)[:300]])
    return result


def summary(s, conversation_id=None, result_id=None, delivery_status=None):
    rec = receipt(s, result_id) if result_id else None
    return {'mode': 'observe_only', 'notice': NOTICE,
            'commitment': commitment(s, conversation_id),
            'assessment': (rec or {}).get('task_observation'),
            'delivery_status': delivery_status}


def latest(s, limit=12):
    rows = s.db.execute("SELECT record_json FROM invocation_receipts WHERE json_type(record_json,'$.task_observation')='object' ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
    return [{'observation': json.loads(raw)['task_observation'],
             'gateway_status': json.loads(raw)['status']} for raw, in rows]
