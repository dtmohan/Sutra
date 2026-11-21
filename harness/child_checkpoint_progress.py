"""Observe changes across accepted checkpoints within a source partition."""
import json
import math
import sqlite3

from context_manager import sha
from token_usage import count

PROFILE = 'child-checkpoint-progress-v1'
MAX_RECEIPT_BYTES = 2 * 1024 * 1024
MAX_SCAN = 32
MAX_RECENT = 12


def _citations(note):
    return {(item['path'], item['quote']) for item in note['citations']}


def _checks(note):
    return {wording for item in note['remaining'] if (wording := ' '.join(item.split()))}


def _withheld(observation):
    return dict(observation, gateway_status='quarantined', checkpoint='withheld',
                evidence=None, next_checks=None)


def capture(envelope, payload, receipt, *, elapsed_seconds=None):
    """Compare the committed checkpoint history with an admitted proposal."""
    if envelope['role'] != 'subagent' or not isinstance(payload.get('child_slice'), dict):
        return None
    history = {}
    for field in ('recovery_checkpoint', 'child_checkpoint'):
        for note in payload.get(field, ()):
            history[note['invocation_id']] = note
    usage = receipt['token_usage']
    elapsed = (elapsed_seconds if type(elapsed_seconds) in (int, float)
               and math.isfinite(elapsed_seconds) and elapsed_seconds >= 0 else None)
    observation = {
        'profile': PROFILE, 'mode': 'observe_only', 'invocation_id': envelope['id'],
        'model': envelope['model'][:160], 'gateway_status': receipt['status'],
        'prior_checkpoints': len(history), 'semantic_assessment': 'unassessed',
        'cost': {'input_tokens': count(usage.get('input_tokens')),
                 'output_tokens': count(usage.get('output_tokens')), 'elapsed_seconds': elapsed},
    }
    if receipt['status'] != 'accepted':
        return _withheld(observation)

    previous_citations = set()
    previous_checks = set()
    last_checks = set()
    for note in history.values():
        previous_citations.update(_citations(note))
        last_checks = _checks(note)
        previous_checks.update(last_checks)
    proposal = receipt['proposal']
    citations = _citations(proposal)
    checks = _checks(proposal)
    return dict(observation, checkpoint='continued' if proposal.get('slice_status') == 'continue' else 'completed',
                evidence={'new_citations': len(citations - previous_citations),
                          'retained_citations': len(citations & previous_citations),
                          'citations': len(citations)},
                next_checks={'new': len(checks - previous_checks),
                             'repeated': len(checks & previous_checks),
                             'retired': len(last_checks - checks)})


def latest(db):
    """Read a bounded window of digest-checked observations without replay."""
    try:
        rows = db.execute(
            'SELECT r.invocation_id,r.id,'
            'CASE WHEN length(CAST(r.record_json AS BLOB))<=? THEN r.record_json END,'
            'EXISTS (SELECT 1 FROM invocation_quarantine q WHERE q.invocation_id=r.invocation_id) '
            'FROM invocation_receipts r ORDER BY r.rowid DESC LIMIT ?',
            (MAX_RECEIPT_BYTES, MAX_SCAN))
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc):
            return []
        raise
    result = []
    for invocation_id, digest, raw, revoked in rows:
        if raw is None or sha('invocation-receipt-v1:' + raw) != digest:
            continue
        try:
            record = json.loads(raw)
            observation = record.get('child_checkpoint_progress')
            if (isinstance(observation, dict)
                    and record['role'] == 'subagent' and record['invocation_id'] == invocation_id
                    and observation['profile'] == PROFILE and observation['mode'] == 'observe_only'
                    and observation['invocation_id'] == invocation_id
                    and observation['gateway_status'] == record['status']):
                result.append(_withheld(observation) if revoked else observation)
                if len(result) == MAX_RECENT:
                    break
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            continue
    return result
