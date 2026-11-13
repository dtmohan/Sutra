"""Versioned source spans, dependency closure and explicit evidence states.

The host attests structured facts and dependency edges. This module verifies
their provenance and freshness; it does not infer or certify semantic truth.
"""
import json
import pathlib
import time

from context_manager import Conflict, InvalidUpdate, ContextBudgetExceeded, canonical, sha
from paging import PagedContext, PageFault, Budget

CURRENT = {'current', 'aging'}
NEGATIVE = {'missing', 'stale', 'unverified', 'conflicting'}


class EvidenceContext(PagedContext):
    def __init__(self, path):
        super().__init__(path)
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS documents(
            session TEXT NOT NULL REFERENCES sessions(id), key TEXT NOT NULL,
            version INTEGER NOT NULL, text TEXT NOT NULL, digest TEXT NOT NULL,
            authority TEXT NOT NULL, source_json TEXT NOT NULL,
            PRIMARY KEY(session,key,version));
          CREATE TABLE IF NOT EXISTS document_heads(session TEXT,key TEXT,version INTEGER,
            PRIMARY KEY(session,key),
            FOREIGN KEY(session,key,version) REFERENCES documents(session,key,version));
          CREATE TRIGGER IF NOT EXISTS immutable_documents_update BEFORE UPDATE ON documents
            BEGIN SELECT RAISE(ABORT,'immutable document'); END;
          CREATE TRIGGER IF NOT EXISTS immutable_documents_delete BEFORE DELETE ON documents
            BEGIN SELECT RAISE(ABORT,'immutable document'); END;
        ''')

    def document(self, session, key, version=None):
        if version is None:
            row = self.db.execute('SELECT version FROM document_heads WHERE session=? AND key=?', (session, key)).fetchone()
            if not row:
                raise PageFault('Unknown document in this session: ' + key)
            version = row[0]
        row = self.db.execute('SELECT * FROM documents WHERE session=? AND key=? AND version=?',
                              (session, key, version)).fetchone()
        if not row:
            raise PageFault('Missing document version')
        return dict(zip(('session', 'key', 'version', 'text', 'digest', 'authority', 'source_json'), row))

    def put_document(self, session, key, text, *, authority='user', source=None, expected_version=0):
        if authority not in {'user', 'file', 'tool', 'model'} or not isinstance(text, str) or not key:
            raise InvalidUpdate('Invalid document')
        if authority != 'model' and not source:
            raise InvalidUpdate('Host provenance is required')
        if authority == 'file':
            raw = pathlib.Path(source['path']).read_bytes()
            if raw.decode('utf-8') != text or sha(text) != source.get('sha256'):
                raise InvalidUpdate('File document provenance mismatch')
        try:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT version FROM document_heads WHERE session=? AND key=?', (session, key)).fetchone()
            version = row[0] if row else 0
            if version != expected_version:
                raise Conflict('Document version changed')
            record = (session, key, version + 1, text, sha(text), authority, canonical(source or {}))
            self.db.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?)', record)
            self.db.execute('INSERT INTO document_heads VALUES(?,?,?) ON CONFLICT(session,key) DO UPDATE SET version=excluded.version',
                            (session, key, version + 1))
            self._audit('DOCUMENT_COMMIT', {'record': list(record)})
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self.document(session, key, version + 1)

    def _check_edges(self, session, key, dependencies):
        visited = set()
        def visit(dep, version):
            if dep == key:
                raise InvalidUpdate('Dependency cycle across page identities')
            if (dep, version) in visited:
                return
            visited.add((dep, version))
            p = self.page(session, dep, version)
            if self.page(session, dep).version != version:
                raise InvalidUpdate('New edges must reference the current page version')
            for child, child_version in p.dependencies:
                visit(child, child_version)
        for dep, version in dependencies:
            if not isinstance(dep, str) or type(version) is not int or version < 1:
                raise InvalidUpdate('Invalid dependency reference')
            visit(dep, version)

    def put_page(self, session, key, group, subject, text, **kwargs):
        dependencies = tuple(tuple(x) for x in kwargs.get('dependencies', ()))
        if len(set(dependencies)) != len(dependencies):
            raise InvalidUpdate('Duplicate dependency edge')
        self._check_edges(session, key, dependencies)
        kwargs['dependencies'] = dependencies
        return super().put_page(session, key, group, subject, text, **kwargs)

    def put_unit(self, session, key, document_id, start, end, *, document_version=None,
                 dependencies=(), facts=None, subject=None, group='policy', expected_version=0,
                 ttl_s=3600):
        """Create a host-declared unit; offsets count Unicode code points, end exclusive."""
        doc = self.document(session, document_id, document_version)
        if self.document(session, document_id)['version'] != doc['version']:
            raise InvalidUpdate('Cannot create a unit from a superseded document')
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(doc['text']):
            raise InvalidUpdate('Invalid source span')
        text = doc['text'][start:end]
        for k, value in (facts or {}).items():
            if not isinstance(value, str) or not value or value not in text:
                raise InvalidUpdate('Attested fact value is absent from the unit span')
        source = json.loads(doc['source_json'])
        source['span'] = {'document_id': document_id, 'document_version': doc['version'],
                          'document_hash': doc['digest'], 'start': start, 'end': end,
                          'span_hash': sha(text)}
        source['dependency_origin'] = 'host_declared'
        return self.put_page(session, key, group, subject or key, text, authority=doc['authority'],
                             facts=facts, source=source, dependencies=dependencies,
                             expected_version=expected_version, ttl_s=ttl_s)

    def put_claim(self, session, key, text, claimed_keys):
        if not all(isinstance(k, str) and k for k in claimed_keys):
            raise InvalidUpdate('Invalid claimed keys')
        return self.put_page(session, key, 'notes', 'Unverified claim', text, authority='model',
                             source={'claimed_keys': sorted(set(claimed_keys))})

    def status(self, page, now=None, stack=()):
        source = json.loads(page.source_json)
        span = source.get('span')
        if span:
            try:
                doc = self.document(page.session, span['document_id'], span['document_version'])
                if self.document(page.session, span['document_id'])['version'] != doc['version']:
                    return 'superseded_source'
                start, end = span['start'], span['end']
                if (sha(doc['text']) != doc['digest'] or doc['digest'] != span['document_hash']
                    or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(doc['text'])
                    or doc['text'][start:end] != page.payload['text']
                    or sha(page.payload['text']) != span['span_hash'] or doc['authority'] != page.authority):
                    return 'corrupt_span'
            except (PageFault, KeyError, TypeError):
                return 'missing_source'
        return super().status(page, now, stack)

    def _heads(self, session):
        return [self.page(session, k) for (k,) in self.db.execute(
            'SELECT key FROM page_heads WHERE session=? ORDER BY key', (session,))]

    def evidence_index(self, session, keys):
        """States concern exact host-declared fact keys, not open-world knowledge."""
        pages = self._heads(session)
        rows = []
        for key in sorted(set(keys)):
            providers = [p for p in pages if key in p.payload['facts'] or
                         key in json.loads(p.source_json).get('claimed_keys', [])]
            trusted = [p for p in providers if p.authority != 'model']
            fresh = [p for p in trusted if self.status(p) in CURRENT]
            if fresh:
                state = 'supported' if len({p.payload['facts'][key] for p in fresh}) == 1 else 'conflicting'
            elif trusted:
                state = 'stale'
            elif providers:
                state = 'unverified'
            else:
                state = 'missing'
            rows.append({'key': key, 'state': state, 'providers': [
                {'id': p.key, 'version': p.version, 'status': self.status(p)} for p in providers]})
        return rows

    def assemble(self, session, request, requested=(), resident=(), directories=(), *,
               required_facts, system, schema, budget=Budget(), final=False, expand=True, policy_request=None,
               schema_factory=None, recovery=None):
        from protocol import schema_for
        payload = {'contract': self.pinned(session), 'request': request, 'pages': [],
                   'evidence_index': self.evidence_index(session, required_facts), 'unavailable_pages': []}
        if policy_request is not None:
            from policy_check import validate_request
            payload['policy_check'] = json.loads(canonical(validate_request(policy_request)))
        if recovery is not None:
            payload['repair'] = json.loads(canonical(recovery))
        if not final:
            payload['directory'] = [self.catalog(session)] + [self.catalog(session, None if g == 'ROOT' else g, o)
                for g, o in sorted(set(tuple(d) for d in directories))]
        pages, mandatory, faults = {}, set(), []
        def add(key, required, version=None):
            try:
                p = self.read_page(session, key, version)
                if p.key in pages:
                    if pages[p.key].version != p.version:
                        raise PageFault('Conflicting dependency versions')
                    if required:
                        mandatory.add(p.key)
                    return
                if expand:
                    for dep, dver in p.dependencies:
                        add(dep, required, dver)
                pages[p.key] = p
                if required:
                    mandatory.add(p.key)
            except PageFault as exc:
                faults.append({'id': key, 'reason': str(exc)})
        for key in requested:
            add(key, True)
        for key in resident:
            add(key, False)
        payload['unavailable_pages'] = faults
        def render():
            payload['pages'] = []
            for p in pages.values():
                entry = {'id': p.key, 'version': p.version, 'hash': p.content_hash,
                         'authority': p.authority, 'status': self.status(p), **p.payload}
                if p.dependencies:
                    entry['depends_on'] = list(p.dependencies)
                span = json.loads(p.source_json).get('span')
                if span:
                    entry['source_span'] = span
                payload['pages'].append(entry)
        evicted = []
        while True:
            render()
            try:
                charge = budget.check(system, payload, schema_factory(payload) if schema_factory else schema_for(payload, schema))
                break
            except ContextBudgetExceeded:
                optional = [key for key in pages if key not in mandatory]
                if not optional:
                    raise
                dropping = {optional[0]}
                while True:
                    more = {key for key, p in pages.items() if any(dep in dropping for dep, _ in p.dependencies)}
                    if more <= dropping:
                        break
                    dropping |= more
                if dropping & mandatory:
                    raise ContextBudgetExceeded('Required prerequisite cannot be evicted')
                for key in list(pages):
                    if key in dropping:
                        del pages[key]
                        evicted.append(key)
        rid = self.prepare_delivery(session, payload, charge)
        return payload, rid, {'resident': list(pages), 'evicted': evicted, 'faults': faults,
                              'budget_charge': charge, 'dependency_expansion': expand}

    def finish(self, session, rid, proposal, required_facts=()):
        """Bind exact delivered state, then check current provenance and policy.

        Model text is preserved unchanged in proposal; only the host creates the
        task envelope. Existing revision checks reject changes during inference.
        """
        from checked_protocol import attach_contract, worker_schema
        from policy_check import check
        row = self.db.execute('SELECT payload_json,payload_hash FROM deliveries WHERE id=? AND session=?',
                              (rid, session)).fetchone()
        if not row:
            raise ValueError('Missing delivery for response envelope')
        payload = json.loads(row[0])
        answer = attach_contract(proposal, payload['contract'])
        admission = self.admission(session, rid, answer, required_facts)
        permitted = worker_schema(payload)['properties']
        for field in ('action', 'target'):
            if proposal[field] not in permitted[field]['enum']:
                admission['errors'].append('proposal violates host workflow: ' + field)
        admission['admitted'] = not admission['errors']
        verification = check(payload, proposal)
        structural = admission['admitted']
        if not structural and verification['verified']:
            verification.update(status='admission_rejected', verified=False,
                                reason='Calculation matches, but source/action admission failed')
        admission['errors'] = sorted(set(admission['errors'] + verification['errors']))
        admission['admitted'] = not admission['errors']
        admission['interpretation_semantics_verified'] = structural and verification['verified']
        admission['policy_verification'] = verification
        return {'answer': answer, 'model_proposal': proposal, 'admission': admission,
                'contract_origin': {'authority': 'host', 'session': session,
                    'revision': payload['contract']['revision'], 'delivery_id': rid, 'payload_hash': row[1]}}

    def admission(self, session, rid, answer, required_facts=()):
        try:
            if not isinstance(answer, dict):
                raise ValueError('Proposal must be an object')
            for field, keys in [('facts', {'key', 'value', 'page_id'}),
                                ('unavailable_evidence', {'key', 'state'})]:
                if not isinstance(answer.get(field), list) or any(
                    not isinstance(f, dict) or set(f) != keys or not all(isinstance(v, str) for v in f.values())
                    for f in answer[field]):
                    raise ValueError('Malformed ' + field)
            if not isinstance(answer.get('interpretation'), str) or not isinstance(answer.get('interpretation_sources'), list):
                raise ValueError('Malformed interpretation')
            if not all(isinstance(p, str) for p in answer['interpretation_sources']):
                raise ValueError('Malformed interpretation sources')
            base = super().admission(session, rid, answer, required_facts)
        except (ValueError, KeyError, TypeError, InvalidUpdate) as exc:
            return {'admitted': False, 'errors': [str(exc)]}
        row = self.db.execute('SELECT payload_json FROM deliveries WHERE id=? AND session=?', (rid, session)).fetchone()
        if not row:
            return base
        payload = json.loads(row[0])
        errors = base['errors']
        index = self.evidence_index(session, required_facts)
        if payload.get('evidence_index') != index:
            errors.append('evidence catalog changed or wrong requested keys')
        states = {r['key']: r['state'] for r in index}
        fact_keys = [f['key'] for f in answer['facts']]
        missing_keys = [f['key'] for f in answer['unavailable_evidence']]
        if len(set(fact_keys + missing_keys)) != len(fact_keys + missing_keys):
            errors.append('duplicate or contradictory evidence entry')
        if set(fact_keys + missing_keys) != set(required_facts):
            errors.append('evidence coverage must equal requested keys')
        for f in answer['facts']:
            if states.get(f['key']) != 'supported':
                errors.append('unsupported fact asserted: ' + f['key'])
        for f in answer['unavailable_evidence']:
            if f['state'] not in NEGATIVE or f['state'] != states.get(f['key']):
                errors.append('incorrect unavailable state: ' + f['key'])
        if any(state in NEGATIVE for state in states.values()):
            only_tests = all(key == 'test_returncode' for key, state in states.items() if state in NEGATIVE)
            allowed = {'run_tests', 'clarify'} if only_tests and 'conflicting' not in states.values() else {'clarify'}
            if answer.get('action') not in allowed or answer.get('complete') or answer['interpretation']:
                errors.append('unavailable evidence cannot authorize this proposal')
        delivered = {p['id']: p for p in payload.get('pages', [])}
        for pid, entry in delivered.items():
            try:
                page = self.read_page(session, pid, entry['version'])
                expected_span = json.loads(page.source_json).get('span')
                if (entry.get('source_span') != expected_span or entry['hash'] != page.content_hash
                    or entry.get('text') != page.payload['text'] or entry.get('facts') != page.payload['facts']
                    or entry.get('authority') != page.authority
                    or canonical(entry.get('depends_on', [])) != canonical(page.dependencies)):
                    errors.append('delivered source provenance mismatch: ' + pid)
                for dep, version in page.dependencies:
                    if dep not in delivered or delivered[dep]['version'] != version:
                        errors.append('prerequisite not delivered: ' + dep)
            except (PageFault, KeyError) as exc:
                errors.append(str(exc))
        # Requiring every current provider prevents a single selected source from
        # hiding a conflict. This is exact-key consistency, not semantic adjudication.
        for item in index:
            for provider in item['providers']:
                if provider['status'] in CURRENT and provider['id'] not in delivered:
                    errors.append('current evidence provider not delivered: ' + provider['id'])
        if answer['interpretation'] and not answer['interpretation_sources']:
            errors.append('interpretation requires source references')
        for pid in answer['interpretation_sources']:
            if pid not in delivered or delivered[pid]['authority'] == 'model':
                errors.append('interpretation source not delivered or unverified: ' + pid)
        return {'admitted': not errors, 'errors': sorted(set(errors)),
                'provenance': {f['key']: {'page_id': f['page_id'],
                    'version': delivered.get(f['page_id'], {}).get('version'),
                    'source_span': delivered.get(f['page_id'], {}).get('source_span')}
                    for f in answer['facts']},
                'interpretation_semantics_verified': False}
