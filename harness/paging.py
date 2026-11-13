"""Karma + Smriti: durable pages, bounded packets, explicit delivery receipts.

Authority is assigned by the trusted host adapters, never by model output.
Provenance and freshness are structural properties, not semantic confidence.
"""
from dataclasses import dataclass
import hashlib, json, math, pathlib, sqlite3, time, uuid
from context_manager import ContextStore, Conflict, InvalidUpdate, ContextBudgetExceeded, canonical, sha
from smriti_adapter import SmritiAdapter

class PageFault(Exception): pass

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

    @property
    def payload(self): return json.loads(self.payload_json)
    @property
    def dependencies(self): return tuple(tuple(x) for x in json.loads(self.dependencies_json))

@dataclass(frozen=True)
class Budget:
    context_limit: int = 8192
    response_reserve: int = 1024
    template_reserve: int = 256

    def charge(self, system, payload, schema):
        # Conservative UTF-8 byte upper bound for the tested Qwen byte-level BPE.
        # Include schema bytes even though Ollama may use them only as a grammar.
        # Not an exact tokenizer; other tokenizer families need their own adapter.
        return sum(len(s.encode('utf-8')) for s in (system,canonical(payload),canonical(schema))) + self.template_reserve

    def check(self, system, payload, schema):
        charge=self.charge(system,payload,schema)
        measured=SmritiAdapter().context(charge,self.response_reserve,self.context_limit)
        if charge+self.response_reserve>self.context_limit or measured['decision']!='ALLOW':
            raise ContextBudgetExceeded(f'Conservative input charge {charge} + output reserve {self.response_reserve} exceeds {self.context_limit}')
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

    def audit_valid(self):
        prev='GENESIS'; expected=1
        for seq,event,data,old,digest in self.db.execute('SELECT * FROM page_audit ORDER BY seq'):
            if seq!=expected or old!=prev or sha(canonical([seq,event,data,old]))!=digest: return False
            expected+=1; prev=digest
        return True

    def page(self, session, key, version=None):
        if version is None:
            row=self.db.execute('SELECT version FROM page_heads WHERE session=? AND key=?',(session,key)).fetchone()
            if row is None: raise PageFault('Unknown page in this session: '+key)
            version=row[0]
        row=self.db.execute('SELECT * FROM pages WHERE session=? AND key=? AND version=?',(session,key,version)).fetchone()
        if row is None: raise PageFault('Missing page version: '+key)
        return Page(*row)

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
        if status not in ('current','aging','unverified'): raise PageFault(key+': '+status)
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
                optional=[key for key in pages if key not in required]
                if not optional: raise
                # resident is ordered by previous delivery; evict its oldest optional page.
                dropping={optional[0]}
                while True:
                    more={key for key,p in pages.items() if any(dep in dropping for dep,_ in p.dependencies)}
                    if more<=dropping: break
                    dropping|=more
                if dropping & required: raise ContextBudgetExceeded('Required dependency cannot be evicted')
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
