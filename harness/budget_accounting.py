"""Host reservation and settlement. Historical calls keep their original charges."""
from context_manager import InvalidUpdate
from token_usage import count


def reserved(envelope):
    return envelope['charge'] + envelope['budget_profile']['response_reserve']


def settlement(envelope, record):
    if not envelope.get('model_interface'): return None
    reservation = reserved(envelope)
    response = record.get('response') if isinstance(record, dict) else None
    response = response if isinstance(response, dict) else {}
    inp, out = count(response.get('prompt_eval_count')), count(response.get('eval_count'))
    drained = (isinstance(record, dict) and record.get('provider') == 'sutra_engine' and response.get('done') is False and
               response.get('usage_finalized') is True)
    measured = (response.get('done') is True or drained) and inp is not None and out is not None
    actual = inp + out if measured else reservation
    return {'version': 1, 'basis': 'runtime_measured' if measured else 'reserved_unmeasured',
            'reserved_tokens': reservation, 'used_tokens': actual,
            'released_unused_reservation': max(0, reservation - actual),
            'overrun_tokens': max(0, actual - reservation)}


def used(envelope, receipt):
    r = (receipt or {}).get('accounting_settlement')
    if not envelope.get('model_interface') or not r: return reserved(envelope)
    if (r.get('version') != 1 or r.get('reserved_tokens') != reserved(envelope)
            or count(r.get('used_tokens')) is None):
        raise InvalidUpdate('Invalid invocation accounting settlement')
    if r['basis'] == 'runtime_measured':
        usage = receipt.get('token_usage') or {}
        inp, out = count(usage.get('input_tokens')), count(usage.get('output_tokens'))
        if inp is None or out is None or r['used_tokens'] != inp + out:
            raise InvalidUpdate('Measured settlement differs from runtime counts')
    elif r['basis'] != 'reserved_unmeasured' or r['used_tokens'] != reserved(envelope):
        raise InvalidUpdate('Unmeasured call must retain its reservation')
    if r.get('released_unused_reservation') != max(0, reserved(envelope)-r['used_tokens']) or r.get('overrun_tokens') != max(0, r['used_tokens']-reserved(envelope)):
        raise InvalidUpdate('Invalid reservation reconciliation')
    return r['used_tokens']
