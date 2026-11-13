"""Fresh selector/worker calls; exact receipts and independently reopened state."""
from evidence import CURRENT
from governance import GovernedContext as EvidenceContext
from paging import PageFault
from protocol import SELECT_SCHEMA, SELECT_SYSTEM, schema_for
from checked_protocol import SCHEMA, SYSTEM, worker_schema


def run_paged(*args, **kwargs):
    from governed_runtime import run
    return run(*args, **kwargs)


def run_once(db, session, request, required_facts, model, call_prefix,
              max_rounds=6, initial_resident=(), expand=True, policy_request=None, recovery=None, run_id=None):
    if run_id is None: raise ValueError("Use run_paged: an active governed run is required")
    from invocation_gateway import InvocationGateway
    gateway = InvocationGateway(db, run_id, model)
    directories, requested, resident = [], [], list(initial_resident)
    records, turns = [], []
    for n in range(max_rounds):
        store = EvidenceContext(db)
        try:
            contract = store.pinned(session)
            tests = [p for p in contract['constraints']['allowed_paths'] if p.startswith('tests/')]
            payload, rid, meta = store.assemble(session, request, list(dict.fromkeys(requested + tests)),
                resident, directories, required_facts=required_facts, system=SELECT_SYSTEM,
                schema=SELECT_SCHEMA, budget=model.budget, expand=expand, policy_request=policy_request, recovery=recovery)
            loaded = {p['id'] for p in payload['pages']}
            needed = {p['id'] for item in payload['evidence_index'] for p in item['providers'] if p['status'] in CURRENT}
            ready = needed <= loaded
            if ready:
                payload, rid, meta = store.assemble(session, request, meta['resident'], required_facts=required_facts,
                    system=SYSTEM, schema=SCHEMA, budget=model.budget, final=True, expand=expand,
                    policy_request=policy_request, schema_factory=worker_schema, recovery=recovery)
                system, schema = SYSTEM, worker_schema(payload)
            else:
                system, schema = SELECT_SYSTEM, schema_for(payload, SELECT_SCHEMA)
            gated = gateway.invoke('worker' if ready else 'selector', call_prefix + '-' + str(n), system, payload, schema, rid)
            records.append(gated.pop('record'))
            turns.append({'delivery_id': rid, 'stage': 'worker' if ready else 'selector', 'metadata': meta, 'invocation_id': gated['invocation_id']})
            answer = gated['accepted_output']
            if not gated['admission']['admitted']:
                gated.setdefault('answer', {})
                gated.update(calls=records, turns=turns)
                return gated
            resident = meta['resident']
            if ready:
                gated.update(calls=records, turns=turns)
                return gated
            advertised_groups = {'ROOT'} | {i['group'] for d in payload.get('directory', []) if d['group'] == 'ROOT' for i in d['items']}
            advertised_pages = {i['id'] for d in payload.get('directory', []) if d['group'] != 'ROOT' for i in d['items']}
            advertised_pages |= set(contract['constraints']['allowed_paths'])
            additions = []
            for b in answer.get('browse', []):
                g, offset = b['group'], b['offset']
                permitted = {0} | {d['next_offset'] for d in payload.get('directory', []) if d['group'] == g and d['next_offset'] is not None}
                if g not in advertised_groups or offset not in permitted:
                    raise PageFault('Invalid catalog cursor')
                if g != 'ROOT' or offset:
                    additions.append((g, offset))
            if any(k not in advertised_pages for k in answer.get('read_pages', [])):
                raise PageFault('Page absent from advertised catalog')
            if not additions and not answer.get('read_pages'):
                raise PageFault('Empty context selection')
            directories = list(dict.fromkeys(directories + additions))
            requested = answer.get('read_pages', [])
        except Exception as exc:
            return {'answer': {}, 'admission': {'admitted': False, 'errors': [str(exc)]},
                    'blocked': type(exc).__name__ + ': ' + str(exc), 'calls': records, 'turns': turns}
        finally:
            store.close()
    return {'answer': {}, 'admission': {'admitted': False, 'errors': ['paging round limit']},
            'blocked': 'paging round limit', 'calls': records, 'turns': turns}
