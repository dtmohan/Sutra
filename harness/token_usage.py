"""Host-observed token usage. Completed counts can settle a prior reservation.

Provider counts describe whole model calls, including structured output. They do
not attribute tokens to individual prompt components or visible answer text.
Older receipts are intentionally not backfilled from prunable trace files.
"""
import json

MAX_SAFE_INTEGER = 2 ** 53 - 1


def count(value):
    return value if type(value) is int and 0 <= value <= MAX_SAFE_INTEGER else None


def measured(record):
    response = record.get('response') if isinstance(record, dict) else None
    response = response if isinstance(response, dict) else {}
    provider = record.get('provider') if isinstance(record, dict) else None
    source = {'strata': 'strata_response', 'sutra_engine': 'sutra_engine_response'}.get(provider, 'ollama_response')
    return {'source': source if response else 'unavailable',
            'input_tokens': count(response.get('prompt_eval_count')),
            'output_tokens': count(response.get('eval_count'))}


def summarize(calls):
    result = {'calls': len(calls)}
    for field in ('input_tokens', 'output_tokens'):
        known = [c[field] for c in calls if count(c.get(field)) is not None]
        result[field] = sum(known) if known else None
        result['missing_' + field] = len(calls) - len(known)
    return result


def history(db, run_id=None, limit=250):
    """Read immutable receipts; dashboard rows are a disclosed bounded window.

    A run query is complete (bounded by the protocol's invocation allocation).
    No source text, prompts, or model responses are included in this view.
    """
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'invocations', 'invocation_receipts'}.issubset(tables):
        return {'calls': [], 'totals': summarize([]), 'total_calls': 0, 'limit': limit}
    where, args = ('WHERE i.run_id=?', [run_id]) if run_id else ('', [])
    total = db.execute('SELECT COUNT(*) FROM invocations i ' + where, args).fetchone()[0]
    query = ('SELECT i.record_json,r.record_json FROM invocations i '
             'LEFT JOIN invocation_receipts r ON r.invocation_id=i.id ' + where + ' ORDER BY i.rowid DESC')
    if not run_id:
        query += ' LIMIT ?'
        args.append(limit)
    calls = []
    for raw, receipt_raw in db.execute(query, args):
        env = json.loads(raw)
        receipt = json.loads(receipt_raw) if receipt_raw else {}
        usage = receipt.get('token_usage') or {}
        budget = env.get('budget_profile', {})
        calls.append({'invocation_id': env['id'], 'run_id': env['run_id'],
                      'session': env['session'], 'stage': env['role'], 'model': env['model'],
                      'created_at': env['created_at'], 'status': receipt.get('status', 'pending'),
                      'input_tokens': count(usage.get('input_tokens')),
                      'output_tokens': count(usage.get('output_tokens')),
                      'source': usage.get('source', 'unavailable'),
                      'conservative_input_charge': env['charge'],
                      'output_reserve': budget.get('response_reserve'),
                      'context_limit': budget.get('context_limit'),
                      'model_packet': env.get('model_interface'),
                      'accounting_settlement': receipt.get('accounting_settlement')})
    return {'calls': calls, 'totals': summarize(calls), 'total_calls': total,
            'limit': None if run_id else limit}
