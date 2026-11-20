"""Karma + Smriti: durable pages, bounded packets, explicit delivery receipts.

Authority is assigned by the trusted host adapters, never by model output.
Provenance and freshness are structural properties, not semantic confidence.
"""
from dataclasses import InitVar, dataclass, field
import hashlib, json, math, pathlib, sqlite3, time, uuid
from context_manager import ContextStore, Conflict, InvalidUpdate, ContextBudgetExceeded, canonical, sha
from smriti_adapter import SmritiAdapter
from memory_layer import PageFault, eviction_closure
from ram_compartments import decode as decode_json

@dataclass(frozen=True)
class Page:
    session: str
    key: str
    version: int
    group: str
    subject: str
    authority: str
    payload_json: str
    content_hash: str
    committed_at: float
    ttl_s: float
    dependencies_json: str
    source_json: str
    cache_scope: object = field(default=None, compare=False, repr=False)

    @property
    def payload(self): return decode_json(('page_payload', self.cache_scope), self.payload_json)
    @property
    def dependencies(self):
        return tuple(tuple(x) for x in decode_json(('page_dependencies', self.cache_scope), self.dependencies_json))

@dataclass(frozen=True)
class Budget:
    context_limit: int = 8192
    response_reserve: int = 1024
    template_reserve: int = 256
    context_profile: InitVar[int] = 1
    reservation_limit: InitVar[object] = None
    context_window: InitVar[object] = None

    def __post_init__(self, context_profile, reservation_limit, context_window):
        if type(context_profile) is not int or context_profile not in (1, 2):
            raise InvalidUpdate('Unsupported context budget profile')
        if context_profile == 1:
            if reservation_limit is not None or context_window is not None:
                raise InvalidUpdate('Legacy context budget cannot carry an exact window')
            return
        from context_window import validate_window
        validate_window(context_window)
        if (type(reservation_limit) is not int or not 1 <= reservation_limit <= 2 ** 53 - 1
                or type(self.context_limit) is not int or not 1 <= self.context_limit <= context_window['context_tokens']):
            raise InvalidUpdate('Invalid exact context reservation allocation')
        object.__setattr__(self, 'context_profile', context_profile)
        object.__setattr__(self, 'reservation_limit', reservation_limit)
        object.__setattr__(self, 'context_window', json.loads(canonical(context_window)))

    @property
    def input_limit(self):
        return self.reservation_limit if self.context_profile == 2 else self.context_limit

    def with_limits(self, *, context_limit=None, response_reserve=None, template_reserve=None, reservation_limit=None):
        values = dict(vars(self))
        for key, value in (('context_limit', context_limit), ('response_reserve', response_reserve),
                           ('template_reserve', template_reserve), ('reservation_limit', reservation_limit)):
            if value is not None:
                values[key] = value
        return Budget(**values)

    def charge(self, system, payload, schema):
        # Charge the same task-facing packet used by the local transport. Full
        # governance records remain in the ledger, outside the model context.
        from model_packet import build, descriptor
        packet = build(system, payload, schema)
        if packet is not None:
            return descriptor(packet, self.template_reserve)['input_reservation']
        return sum(len(s.encode('utf-8')) for s in (system, canonical(payload), canonical(schema))) + self.template_reserve

    def check(self, system, payload, schema):
        charge=self.charge(system,payload,schema)
        measured=SmritiAdapter().context(charge,self.response_reserve,self.input_limit)
        if charge+self.response_reserve>self.input_limit or measured['decision']!='ALLOW':
            raise ContextBudgetExceeded(f'Conservative input charge {charge} + output reserve {self.response_reserve} exceeds {self.input_limit}')
        return charge

class PagedContext(ContextStore):
    def __init__(self, path):
        super().__init__(path)
        self.adapter=SmritiAdapter()
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS pages(
            session TEXT NOT NULL REFERENCES sessions(id), key TEXT NOT NULL, version INTEGER NOT NULL,
            page_group TEXT NOT NULL, subject TEXT NOT NULL, authority TEXT NOT NULL,
            payload_json TEXT NOT NULL, content_hash TEXT NOT NULL, committed_at REAL NOT NULL,
            ttl_s REAL NOT NULL, dependencies_json TEXT NOT NULL, source_json TEXT NOT NULL,
            PRIMARY KEY(session,key,version));
          CREATE TABLE IF NOT EXISTS page_heads(session TEXT,key TEXT,version INTEGER,
            PRIMARY KEY(session,key), FOREIGN KEY(session,key,version) REFERENCES pages(session,key,version));
          CREATE TABLE IF NOT EXISTS page_audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL,payload_json TEXT NOT NULL,previous_hash TEXT NOT NULL,event_hash TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,session TEXT NOT NULL,revision INTEGER NOT NULL,
            payload_json TEXT NOT NULL,payload_hash TEXT NOT NULL,refs_json TEXT NOT NULL,budget_charge INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS delivery_success(id TEXT PRIMARY KEY REFERENCES deliveries(id));
        ''')
        for table in ('pages','page_audit','deliveries','delivery_success'):
            for op in ('UPDATE','DELETE'):
                self.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{op} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'immutable record'); END")
        self.db.commit()
        from memory_layer import initialize
        initialize(self.db)
        self._audit_verified = None
        self._audit_prefix = None
        self._audit_guard_epoch = 0
        self._audit_guard_installed = True
        # This is an invalidation sensor, not authorization. Keep raw SQL usable
        # for the explicit verifier and fault-injection tests, but never reuse a
        # prefix after this connection could have changed its immutable guards.
        self.db.set_authorizer(self._audit_authorizer)
        self._audit_file_identity = self._audit_identity()

    def _audit_authorizer(self, action, first, second, database, trigger):
        ddl = {sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_CREATE_INDEX,
               sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_TRIGGER,
               sqlite3.SQLITE_CREATE_VIEW, sqlite3.SQLITE_DROP_INDEX,
               sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_TRIGGER,
               sqlite3.SQLITE_DROP_VIEW, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}
        if (action in ddl or
                action in (sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE) and first == 'page_audit' or
                action == sqlite3.SQLITE_PRAGMA and first.lower() in ('schema_version', 'writable_schema')
                and second is not None):
            self._audit_guard_epoch += 1
        return sqlite3.SQLITE_OK

    def _audit_guards_present(self):
        rows = dict(self.db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                                    "AND name IN ('immutable_page_audit_UPDATE','immutable_page_audit_DELETE')"))
        for op in ('UPDATE', 'DELETE'):
            name = 'immutable_page_audit_' + op
            expected = ("CREATE TRIGGER " + name + " BEFORE " + op + " ON page_audit "
                        "BEGIN SELECT RAISE(ABORT,'immutable record'); END")
            if ' '.join((rows.get(name) or '').split()).lower() != expected.lower():
                return False
        return True

    def _audit_identity(self):
        if self.path == ':memory:':
            return None
        st = pathlib.Path(self.path).stat()
        return st.st_dev, st.st_ino

    def _audit_stamp(self):
        return (self.db.total_changes,
                self.db.execute('PRAGMA data_version').fetchone()[0],
                self.db.execute('PRAGMA schema_version').fetchone()[0],
                self.db.execute('SELECT seq,event_hash FROM page_audit ORDER BY seq DESC LIMIT 1').fetchone())

    def snapshot(self, session):
        state=super().snapshot(session)
        if state['tests_status']=='passed':
            sid=state['tests'][-1]['source_id']
            proofs=[self.page(session,key) for (key,) in self.db.execute('SELECT key FROM page_heads WHERE session=?',(session,))]
            proofs=[p for p in proofs if p.authority=='tool' and json.loads(p.source_json).get('test_source_id')==sid]
            if not any(self.status(p) in ('current','aging') for p in proofs): state['tests_status']='stale'
        return state

    def _audit(self, event, payload):
        row=self.db.execute('SELECT event_hash FROM page_audit ORDER BY seq DESC LIMIT 1').fetchone()
        prev=row[0] if row else 'GENESIS'; data=canonical(payload)
        seq=self.db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM page_audit').fetchone()[0]
        digest=sha(canonical([seq,event,data,prev]))
        self.db.execute('INSERT INTO page_audit VALUES(?,?,?,?,?)',(seq,event,data,prev,digest))

    def audit_valid(self, deep=False):
        # Unknown external commits retain strict replay by default. A host may
        # explicitly select the SAME externally authenticated V2 accounting seal
        # as its page-audit prefix; that seal commits its original audit cursor.
        # Later DB seals and previously replayed tails never promote that trust.
        # No profile or self-trusted marker is persisted in the database.
        from accounting_metrics import record
        started = time.perf_counter_ns()
        stage, scanned, reason, ok = 'page_audit_full', 0, 'cold', False
        previous_seq, head_seq = 0, 0
        trusted, trust = None, None
        owns_transaction = not self.db.in_transaction
        try:
            if self._audit_identity() != self._audit_file_identity:
                reason = 'file_identity'
                return False
            if owns_transaction:
                self.db.execute('BEGIN')
            stamp = self._audit_stamp()
            head_seq = stamp[3][0] if stamp[3] else 0
            epoch = getattr(self, '_audit_guard_epoch', None)
            if not deep and getattr(self, '_accounting_audit_prefix_enabled', False):
                from accounting_batches import trusted_audit_prefix
                trusted = trusted_audit_prefix(self)
                trust = trusted['binding']
            if (not deep and stamp == self._audit_verified and
                    getattr(self, '_audit_verified_epoch', None) == epoch and
                    getattr(self, '_audit_verified_trust', None) == trust):
                stage, reason, ok = 'page_audit_cache', 'unchanged', True
                return True
            self._audit_verified = None
            prefix = getattr(self, '_audit_prefix', None)
            prev = 'GENESIS'
            expected = 1
            guards = self._audit_guards_present()
            can_reuse = (not deep and prefix is not None and guards and
                         getattr(self, '_audit_guard_installed', False) and
                         prefix.get('trust') == trust and
                         prefix['data_version'] == stamp[1] and prefix['schema'] == stamp[2] and
                         prefix['guard_epoch'] == epoch)
            if can_reuse:
                # Every position in the verified positive prefix is occupied,
                # so a raw INSERT can only append or add a nonpositive row.
                # MAX/head checks alone miss the latter. MIN uses the integer
                # primary-key B-tree and does not scan historical records.
                minimum = self.db.execute('SELECT MIN(seq) FROM page_audit').fetchone()[0]
                if minimum != (1 if stamp[3] else None):
                    self._audit_prefix = None
                    stage, reason = 'page_audit_tail', 'nonpositive_position'
                    return False
                position = prefix['head']
                observed = self.db.execute('SELECT seq,event_hash FROM page_audit WHERE seq=?',
                                           (position[0],)).fetchone() if position else None
                if observed == position and head_seq >= (position[0] if position else 0):
                    stage, reason = 'page_audit_tail', 'verified_immutable_prefix'
                    if position:
                        previous_seq, prev = position
                        expected = previous_seq + 1
                else:
                    reason = 'prefix_mismatch_or_rollback'
            elif deep:
                reason = 'explicit_deep'
            elif prefix is not None:
                reason = ('external_commit' if prefix['data_version'] != stamp[1] else
                          'schema_or_guard_change')
            unsafe_local_change = prefix is not None and prefix['guard_epoch'] != epoch
            if stage == 'page_audit_full' and trusted is not None and guards and not unsafe_local_change:
                # Re-check every canonical row after the ORIGINAL trusted seal
                # on an external commit. A later verified suffix is not a new
                # trust root, and clear/change-anchor invalidates this proof.
                minimum = self.db.execute('SELECT MIN(seq) FROM page_audit').fetchone()[0]
                if minimum != 1:
                    self._audit_prefix = None
                    reason = 'nonpositive_position'
                    return False
                previous_seq, prev = trusted['head']
                if head_seq < previous_seq:
                    self._audit_prefix = None
                    reason = 'trusted_prefix_missing'
                    return False
                expected = previous_seq + 1
                stage, reason = 'page_audit_anchored_tail', 'caller_trusted_seal'
            rows = (self.db.execute('SELECT * FROM page_audit WHERE seq>? ORDER BY seq', (previous_seq,))
                    if stage in ('page_audit_tail', 'page_audit_anchored_tail') else
                    self.db.execute('SELECT * FROM page_audit ORDER BY seq'))
            for seq,event,data,old,digest in rows:
                scanned += 1
                if seq != expected or old != prev or sha(canonical([seq,event,data,old])) != digest:
                    self._audit_prefix = None
                    reason = 'malformed_chain'
                    return False
                expected += 1
                prev = digest
            if self._audit_stamp() == stamp:
                self._audit_verified = stamp
                self._audit_verified_epoch = epoch
                self._audit_verified_trust = trust
                self._audit_prefix = {'head': stamp[3], 'schema': stamp[2],
                                      'data_version': stamp[1], 'guard_epoch': epoch,
                                      'trust': trust} if guards else None
            ok = True
            return True
        except (OSError, sqlite3.DatabaseError, InvalidUpdate):
            self._audit_verified = None
            self._audit_prefix = None
            reason = 'sqlite_or_file_error'
            return False
        finally:
            if owns_transaction and self.db.in_transaction:
                # SQL ROLLBACK avoids invalidating active outer cursors on the
                # Apple Python 3.9 sqlite wrapper's Connection.rollback path.
                self.db.execute('ROLLBACK')
            record(self, stage, time.perf_counter_ns() - started, rows_scanned=scanned,
                   previous_seq=previous_seq, head_seq=head_seq, reason=reason, ok=ok,
                   trusted_prefix_seq=trusted['head'][0] if trusted is not None else None)

    def page(self, session, key, version=None):
        if version is None:
            row=self.db.execute('SELECT version FROM page_heads WHERE session=? AND key=?',(session,key)).fetchone()
            if row is None: raise PageFault('Unknown page in this session: '+key, code='missing', key=key)
            version=row[0]
        row=self.db.execute('SELECT * FROM pages WHERE session=? AND key=? AND version=?',(session,key,version)).fetchone()
        if row is None: raise PageFault('Missing page version: '+key, code='missing', key=key)
        database = ('memory', id(self.db)) if self.path == ':memory:' else (
            str(pathlib.Path(self.path).resolve()), self._audit_file_identity)
        scope = (database, session, key, version, getattr(self, 'logic_hash', None))
        return Page(*row, cache_scope=scope)

    def put_page(self, session, key, group, subject, text, *, authority='model', facts=None,
                 dependencies=(), ttl_s=3600, source=None, expected_version=0):
        if authority not in ('user','file','tool','model'): raise InvalidUpdate('Unknown authority')
        if not key or not group or len(key)>160 or len(subject)>180 or len(group)>40:
            raise InvalidUpdate('Invalid page metadata size')
        if not math.isfinite(ttl_s) or ttl_s<=0: raise InvalidUpdate('Invalid expiry interval')
        if authority=='model' and facts: raise InvalidUpdate('Model text cannot establish authoritative facts')
        if facts and not all(isinstance(k,str) and isinstance(v,str) for k,v in facts.items()):
            raise InvalidUpdate('Structured facts require string keys and values')
        if authority!='model' and not source: raise InvalidUpdate('Host source provenance required')
        for dep,version in dependencies:
            if dep==key: raise InvalidUpdate('Self dependency is prohibited')
            self.page(session,dep,version)
        payload=canonical({'text':text,'facts':facts or {}})
        now=time.time()
        try:
            self.db.execute('BEGIN IMMEDIATE')
            head=self.db.execute('SELECT version FROM page_heads WHERE session=? AND key=?',(session,key)).fetchone()
            version=head[0] if head else 0
            if version!=expected_version: raise Conflict('Page version changed')
            row=(session,key,version+1,group,subject,authority,payload,sha(payload),now,ttl_s,canonical(dependencies),canonical(source or {}))
            self.db.execute('INSERT INTO pages VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',row)
            self.db.execute('INSERT INTO page_heads VALUES(?,?,?) ON CONFLICT(session,key) DO UPDATE SET version=excluded.version',(session,key,version+1))
            self._audit('PAGE_COMMIT',{'session':session,'key':key,'version':version+1,'record':list(row)})
            self.db.commit()
        except Exception:
            self.db.rollback(); raise
        return self.page(session,key,version+1)

    def import_file(self, session, logical, path, *, facts=None, expected_version=0, group='implementation', subject=None):
        raw=pathlib.Path(path).read_bytes(); text=raw.decode()
        if any(value not in text for value in (facts or {}).values()):
            raise InvalidUpdate('Host-attested file fact value is absent from the source')
        actual=pathlib.Path(path).resolve(); relative=False
        try: stored=str(actual.relative_to(pathlib.Path(self.path).resolve().parent)); relative=True
        except ValueError: stored=str(actual)
        return self.put_page(session,logical,group,subject or logical,text,authority='file',facts=facts,
                             source={'path':stored,'relative_to_db':relative,'sha256':hashlib.sha256(raw).hexdigest()},expected_version=expected_version)

    def status(self, page, now=None, stack=()):
        now=time.time() if now is None else now
        marker=(page.key,page.version)
        if marker in stack: return 'dependency_cycle'
        if sha(page.payload_json)!=page.content_hash: return 'corrupt'
        if self.page(page.session,page.key).version!=page.version: return 'superseded'
        age=max(0,now-page.committed_at)
        # Normalize time to Smriti's two freshness bands; no lexical sigma.
        frame=self.adapter.freshness(age,page.ttl_s);decision=frame['decision']
        if frame['expired']: return 'expired'
        if page.authority=='file':
            source=json.loads(page.source_json)
            try:
                source_path=pathlib.Path(source['path'])
                if source.get('relative_to_db'): source_path=pathlib.Path(self.path).resolve().parent/source_path
                observed=hashlib.sha256(source_path.read_bytes()).hexdigest()
            except (OSError,KeyError): return 'missing_source'
            if observed!=source.get('sha256'): return 'source_changed'
        for key,version in page.dependencies:
            try: dep=self.page(page.session,key,version)
            except PageFault: return 'missing_dependency'
            if self.status(dep,now,stack+(marker,)) not in ('current','aging','unverified'):
                return 'stale_dependency'
            if dep.authority=='model': return 'unverified_dependency'
        if page.authority=='model': return 'unverified'
        return 'aging' if decision=='BRIDGE' else 'current'

    def read_page(self, session, key, version=None, now=None):
        page=self.page(session,key,version); status=self.status(page,now)
        if status not in ('current','aging','unverified'): raise PageFault(key+': '+status, code=status, key=key)
        return page

    def catalog(self, session, group=None, offset=0, limit=6):
        if offset<0 or not 1<=limit<=8: raise InvalidUpdate('Invalid catalog bounds')
        if group is None:
            rows=self.db.execute('SELECT p.page_group,COUNT(*) FROM pages p JOIN page_heads h USING(session,key,version) WHERE p.session=? GROUP BY p.page_group ORDER BY p.page_group LIMIT ? OFFSET ?',(session,limit+1,offset)).fetchall()
            items=[{'group':g,'pages':n} for g,n in rows[:limit]]
        else:
            rows=self.db.execute('SELECT p.key FROM pages p JOIN page_heads h USING(session,key,version) WHERE p.session=? AND p.page_group=? ORDER BY p.key LIMIT ? OFFSET ?',(session,group,limit+1,offset)).fetchall()
            items=[]
            for (key,) in rows[:limit]:
                p=self.page(session,key)
                items.append({'id':p.key,'version':p.version,'subject':p.subject,'authority':p.authority,
                              'status':self.status(p),'bytes':len(p.payload_json.encode()),'fact_keys':sorted(p.payload['facts'])})
        return {'group':group or 'ROOT','offset':offset,'items':items,'next_offset':offset+limit if len(rows)>limit else None}

    def report(self, session):
        # Diagnostics have no delivery, access-count, or trust side effects.
        rows=self.db.execute('SELECT key FROM page_heads WHERE session=? ORDER BY key',(session,)).fetchall()
        return {key:self.status(self.page(session,key)) for (key,) in rows}

    def pinned(self, session):
        self.execution_packet(session,max_bytes=100_000)
        state=self.snapshot(session)
        result={k:state[k] for k in ('session_id','revision','task_id','goal','constraints','selected_option','tests_status')}
        result['implementation_present']=bool(state['artifacts'])
        return result

    def make_packet(self, session, request, requested=(), resident=(), directories=(), *, system, schema, budget=Budget(), mode='paging'):
        payload={'contract':self.pinned(session),'request':request,'pages':[],'unavailable_pages':[]}
        if mode=='paging':
            payload['directory']=[self.catalog(session)]+[self.catalog(session,None if g=='ROOT' else g,o) for g,o in sorted(set(tuple(d) for d in directories))]
        pages={}; required=set(); faults=[]
        def add(key, mandatory, version=None):
            try:
                p=self.read_page(session,key,version)
                if p.key in pages:
                    if mandatory: required.add(p.key)
                    return
                for dep,dver in p.dependencies: add(dep,mandatory,dver)
                pages[p.key]=p
                if mandatory: required.add(p.key)
            except PageFault as e:
                faults.append({'id':key,'reason':str(e)})
        for key in requested: add(key,True)
        for key in resident: add(key,False)
        payload['unavailable_pages']=faults
        def render():
            payload['pages']=[{'id':p.key,'version':p.version,'hash':p.content_hash,
                               'authority':p.authority,'status':self.status(p),**p.payload} for p in pages.values()]
        render(); evicted=[]
        while True:
            from protocol import schema_for
            try: charge=budget.check(system,payload,schema_for(payload,schema)); break
            except ContextBudgetExceeded:
                dropping=eviction_closure(pages,required)
                for key in list(pages):
                    if key in dropping: del pages[key]; evicted.append(key)
                render()
        receipt=self.prepare_delivery(session,payload,charge)
        return payload,receipt,{'resident':list(pages),'evicted':evicted,'faults':faults,'budget_charge':charge}

    def prepare_delivery(self, session, payload, charge):
        refs=[(p['id'],p['version'],p['hash']) for p in payload.get('pages',[])]
        data=canonical(payload); rid=uuid.uuid4().hex
        try:
            self.db.execute('BEGIN IMMEDIATE')
            revision=self.snapshot(session)['revision']
            if 'contract' in payload and payload['contract']['revision']!=revision:
                raise Conflict('Contract changed while building packet')
            self.db.execute('INSERT INTO deliveries VALUES(?,?,?,?,?,?,?)',(rid,session,revision,data,sha(data),canonical(refs),charge))
            self._audit('PACKET_PREPARED',{'id':rid,'session':session,'revision':revision,'payload_hash':sha(data),'refs':refs})
            self.db.commit()
        except Exception:
            self.db.rollback(); raise
        return rid

    def confirm_delivery(self, rid):
        with self.db:
            self.db.execute('INSERT INTO delivery_success VALUES(?)',(rid,))
            self._audit('PACKET_DELIVERED',{'id':rid})
            from memory_layer import observe_delivery
            observe_delivery(self.db,rid)

    def admission(self, session, rid, answer, required_facts=()):
        row=self.db.execute('SELECT session,revision,payload_json,refs_json FROM deliveries WHERE id=?',(rid,)).fetchone()
        if row is None or row[0]!=session: return {'admitted':False,'errors':['unknown or wrong-session delivery']}
        if not self.db.execute('SELECT 1 FROM delivery_success WHERE id=?',(rid,)).fetchone():
            return {'admitted':False,'errors':['packet delivery not confirmed']}
        from protocol import normalize_answer
        try: proposal=normalize_answer(answer)
        except InvalidUpdate as e: return {'admitted':False,'errors':[str(e)]}
        result=self.admit_proposal(session,row[1],proposal); errors=result['errors']
        state=self.snapshot(session)
        if str(answer.get('goal','')).strip().rstrip('.') != str(state['goal']).strip().rstrip('.'):
            errors.append('goal wording mismatch')
        refs={key:(version,digest) for key,version,digest in json.loads(row[3])}
        seen=set()
        for fact in answer.get('facts',[]):
            key=fact['key']; pid=fact['page_id']; seen.add(key)
            if pid not in refs: errors.append('fact page was not delivered: '+pid); continue
            version,digest=refs[pid]
            try: page=self.read_page(session,pid,version)
            except PageFault as exc: errors.append(str(exc)); continue
            if page.authority=='model': errors.append('model claim has no fact authority'); continue
            if page.content_hash!=digest or page.payload['facts'].get(key)!=fact['value']:
                errors.append('fact mismatch: '+key)
        if len(seen)!=len(answer.get('facts',[])): errors.append('duplicate fact')
        missing=set(required_facts)-seen
        if missing and answer.get('action') not in ('clarify','run_tests'):
            errors.append('required evidence missing: '+','.join(sorted(missing)))
        if answer.get('action') in ('edit','run_tests') and answer.get('target'):
            try:
                current=self.page(session,answer['target'])
                if answer['target'] not in refs or refs[answer['target']][0]!=current.version:
                    errors.append('target page not delivered at current version')
            except PageFault:
                errors.append('target page unavailable')
        if answer.get('complete'):
            verified_test=False
            for key,(version,_) in refs.items():
                try: page=self.read_page(session,key,version)
                except PageFault: continue
                if page.authority=='tool' and page.payload['facts'].get('test_returncode')=='0':
                    covered={dep for dep,_ in page.dependencies}
                    if set(state['artifacts']) <= covered: verified_test=True
            if not verified_test: errors.append('current test page not delivered')
        return {'admitted':not errors,'errors':errors}
