"""Bounded NACK/repair around fresh page-in calls. No action executor."""
import copy
import time

from governance import GovernedContext


def repairable(result):
    if result.get('blocked'):
        return result['blocked'].startswith('ValueError:')
    verification = result['admission'].get('policy_verification', {})
    if verification.get('status') in ('unverifiable', 'unconfigured'): return False
    permanent = ('changed', 'stale', 'superseded', 'not delivered', 'unavailable', 'wrong-session', 'quarantined')
    return not any(word in error.lower() for error in result['admission']['errors'] for word in permanent)


def run(db, session, request, required_facts, model, call_prefix, max_rounds=6,
        initial_resident=(), expand=True, policy_request=None, max_repairs=2,
        max_calls=12, max_seconds=600, checkpoint_ids=(), run_id=None):
    from dag_runtime import run_once
    store = GovernedContext(db)
    try:
        run_id = store.begin_run(session, request, required_facts, policy_request,
            max_repairs=max_repairs, max_calls=max_calls, max_seconds=max_seconds,
            parents=checkpoint_ids, run_id=run_id)
    finally: store.close()
    resident = list(initial_resident) + ['checkpoint-' + cid for cid in checkpoint_ids]
    records, turns, attempts, recovery = [], [], [], None

    result = {'answer': {}, 'admission': {'admitted': False, 'errors': []}}
    for attempt in range(max_repairs + 1):
        ledger = GovernedContext(db)
        try:
            ledger.reserve(run_id, 'ATTEMPT', {'attempt': attempt})
        except Exception as exc:
            result = {'answer': {}, 'admission': {'admitted': False, 'errors': [str(exc)]}, 'blocked': str(exc)}
            ledger.close(); break
        ledger.close()
        if attempt: time.sleep(min(0.25 * (2 ** (attempt - 1)), 1.0))
        result = run_once(db, session, request, required_facts, model,
            call_prefix + '-attempt' + str(attempt), max_rounds=max_rounds,
            initial_resident=resident, expand=expand, policy_request=policy_request, recovery=recovery, run_id=run_id)
        ledger = GovernedContext(db)
        try:
            checkpoint = ledger.record_result(run_id, attempt, result, getattr(model, 'model', 'test-adapter'))
            records.extend(result.get('calls', [])); turns.extend(result.get('turns', []))
            attempts.append({k: copy.deepcopy(v) for k, v in result.items() if k not in ('calls', 'turns')})
            if result['admission']['admitted']:
                state = 'FIN' if result['answer']['action'] == 'clarify' else 'ALLOW'
                reason = 'missing_inputs' if state == 'FIN' else 'validated'
                ledger.event(run_id, state, {'reason': reason, 'checkpoint_id': checkpoint})
                break
            errors = result['admission']['errors']
            ledger.event(run_id, 'NACK', {'attempt': attempt, 'errors': errors, 'checkpoint_id': checkpoint})
            blocked = result.get('blocked', '').lower()
            if any(word in blocked for word in ('thermal', 'memory', 'telemetry', 'timed out', 'remotedisconnected', 'connection')):
                state, reason = 'RESOURCE_WAIT', 'resource_or_transport_interruption'
                ledger.event(run_id, state, {'reason': reason}); break
            if not repairable(result) or attempt == max_repairs:
                state, reason = 'FIN', 'repair_exhausted' if attempt == max_repairs and repairable(result) else 'missing_or_changed_evidence'
                ledger.event(run_id, state, {'reason': reason, 'errors': errors}); break
            # Only bounded host rejection messages go into the next fresh call.
            # No rejected answer, computed policy answer or conversation is replayed.
            recovery = {'attempt': attempt + 1, 'errors': [e[:160] for e in errors[:3]]}
            ledger.event(run_id, 'BRIDGE', {'attempt': attempt + 1, 'repair_of': checkpoint, 'feedback': recovery})
        finally: ledger.close()
    else:
        state, reason = 'FIN', 'repair_exhausted'
    ledger = GovernedContext(db)
    try:
        run_state = ledger.run(run_id)
        terminal = [e for e in run_state['events'] if e['kind'] in ('ALLOW', 'FIN', 'RESOURCE_WAIT')]
        if not terminal:
            state, reason = 'FIN', 'protocol_budget_or_epoch_change'
            ledger.event(run_id, state, {'reason': reason, 'errors': result['admission']['errors']})
        else:
            state = terminal[-1]['kind']; reason = terminal[-1]['data']['reason']
        missing = list(result['admission']['errors'])
        missing += [item['key'] + ': ' + item['state'] for item in result.get('answer', {}).get('unavailable_evidence', [])]
        result.update(calls=records, turns=turns, attempts=attempts,
            released_answer=result['answer'] if result['admission']['admitted'] else None,
            protocol={'run_id': run_id, 'state': state, 'reason': reason,
                      'epoch': run_state['config']['epoch'], 'contract_hash': run_state['config']['contract_hash'],
                      'logic_hash': run_state['config']['logic_hash'], 'attempts_used': len(attempts),
                      'max_repairs': max_repairs, 'missing_inputs': missing if state != 'ALLOW' else [],
                      'safe_next_steps': [] if state == 'ALLOW' else ['Wait for resource recovery, then start a new bounded run.'] if state == 'RESOURCE_WAIT'
                      else ['Provide current evidence or an authorized contract update before starting a new run.']})
        return result
    finally: ledger.close()
