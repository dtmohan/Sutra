"""Public compatibility API. Parsing and execution both use the gateway."""
import uuid
from invocation_gateway import InvocationGateway, InvocationLedger
from legacy_model import EXTRACTOR, PATCH_SCHEMA, source_units


def ingest(store, session, message, model, call_id=None):
    prefix = call_id or 'user-' + uuid.uuid4().hex
    ledger = InvocationLedger(store.path)
    calls, receipts, error = [], [], None
    run_id = ledger.begin_run(session, message, [], None, purpose='parser', max_repairs=1, max_calls=2)
    gateway = InvocationGateway(store.path, run_id, model, ledger=ledger)
    try:
        for attempt in range(2):
            ledger.reserve(run_id, 'ATTEMPT', {'attempt': attempt})
            prior = ledger.packet(session, max_bytes=100_000)
            payload = {'PRIOR_STATE': prior, 'CURRENT_USER_MESSAGE': message,
                       'SOURCE_UNITS': source_units(message), 'validation_error': error}
            charge = model.budget.check(EXTRACTOR, payload, PATCH_SCHEMA)
            rid = ledger.prepare_delivery(session, payload, charge)
            result = gateway.invoke('parser', prefix + '-' + str(attempt), EXTRACTOR, payload, PATCH_SCHEMA, rid)
            calls.append(result['record']); receipts.append(result['invocation_receipt_id'])
            if result['admission']['admitted']:
                ledger.event(run_id, 'ALLOW', {'reason': 'validated_contract_update'})
                return {'committed': True, 'operations': result['operations'], 'calls': calls,
                        'invocation_receipt_ids': receipts, 'run_id': run_id}
            error = '; '.join(result['admission']['errors'])[:480]
            ledger.event(run_id, 'NACK', {'attempt': attempt, 'errors': result['admission']['errors'], 'invocation_id': result['invocation_id']})
            if result.get('blocked') or any(word in error.lower() for word in ('changed','stale')): break
            if attempt == 0:
                ledger.event(run_id, 'BRIDGE', {'attempt': 1, 'feedback': error})
        ledger.event(run_id, 'RESOURCE_WAIT' if result.get('blocked') else 'FIN', {'reason': 'parser_not_committed', 'errors': [error]})
        return {'committed': False, 'error': error, 'calls': calls,
                'invocation_receipt_ids': receipts, 'run_id': run_id}
    except Exception as exc:
        ledger.event(run_id, 'FIN', {'reason': 'parser_dispatch_rejected', 'errors': [str(exc)]})
        return {'committed': False, 'error': str(exc), 'calls': calls,
                'invocation_receipt_ids': receipts, 'run_id': run_id}
    finally: ledger.close()


def run_paged(*args, **kwargs):
    from dag_runtime import run_paged as governed_run
    return governed_run(*args, **kwargs)
