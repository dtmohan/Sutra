"""Versioned context packets for fresh model calls. Python standard library only.

Trust boundary: user updates are extracted proposals; tool observations are
supplied by the host. Assistant claims never become authoritative facts.
Quotes establish provenance, not semantic correctness of the extraction.
"""
import hashlib
import json
import pathlib
import sqlite3

class Conflict(Exception): pass
class InvalidUpdate(Exception): pass
class ContextBudgetExceeded(Exception): pass
class IncompleteContext(Exception): pass

BOOL_KEYS = {'network_allowed', 'dependencies_allowed', 'edits_allowed', 'tests_allowed', 'review_only', 'auto_review', 'native_task', 'conversation_only'}
INT_KEYS = {'timeout_ms', 'max_attempts', 'max_rows', 'test_timeout_ms', 'max_actions', 'max_tools', 'review_calls', 'review_tokens', 'review_source_kib', 'review_seconds', 'review_page_kib'}
CONSTRAINT_KEYS = BOOL_KEYS | INT_KEYS | {'allowed_paths', 'test_paths', 'output_format'}
USER_KEYS = CONSTRAINT_KEYS | {'task_id', 'goal', 'selected_option'}

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)

def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()

def decode_value(key, value):
    if not isinstance(value, str): raise InvalidUpdate('Extractor values must be strings')
    if key in BOOL_KEYS:
        if value not in ('true','false'): raise InvalidUpdate('Invalid Boolean')
        return value == 'true'
    if key in INT_KEYS:
        if not value.isdecimal(): raise InvalidUpdate('Invalid positive integer')
        number = int(value)
        if not 0 < number <= 1_000_000: raise InvalidUpdate('Integer out of range')
        return number
    if key in {'allowed_paths', 'test_paths'}:
        paths = [p.strip() for p in value.split(',') if p.strip()]
        if not paths: raise InvalidUpdate('Empty scope')
        for p in paths:
            q = pathlib.PurePosixPath(p)
            if q.is_absolute() or '..' in q.parts or '\\' in p: raise InvalidUpdate('Unsafe scope')
        return sorted(set(paths))
    if not value.strip(): raise InvalidUpdate('Empty value')
    return value.strip()

class ContextStore:
    def __init__(self, path):
        self.path = str(path)
        self.db = sqlite3.connect(self.path)
        from ledger_sqlite import initialize
        try:
            initialize(self)
        except Exception:
            self.db.close()
            raise
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS events(
            session TEXT NOT NULL REFERENCES sessions(id), revision INTEGER NOT NULL,
            source_id TEXT NOT NULL, authority TEXT NOT NULL, kind TEXT NOT NULL,
            raw_content TEXT NOT NULL, payload TEXT NOT NULL, previous_hash TEXT NOT NULL,
            event_hash TEXT NOT NULL, PRIMARY KEY(session,revision), UNIQUE(session,source_id));
          CREATE TRIGGER IF NOT EXISTS no_event_update BEFORE UPDATE ON events
            BEGIN SELECT RAISE(ABORT,'event log is append-only'); END;
          CREATE TRIGGER IF NOT EXISTS no_event_delete BEFORE DELETE ON events
            BEGIN SELECT RAISE(ABORT,'event log is append-only'); END;
        ''')
    def close(self):
        try:
            if getattr(self, '_accounting_pending_finding', None):
                # A failed effect may have rolled back the finding along with
                # its charge. Never commit that partial effect merely to keep
                # the finding: persist quarantine after rolling it back.
                if self.db.in_transaction:
                    self.db.rollback()
                from accounting_batches import persist_pending_finding
                persisted = persist_pending_finding(self)
                if not persisted and not getattr(self, 'accounting_read_only', False):
                    raise InvalidUpdate('Accounting finding could not be persisted before closing')
        finally:
            self.db.close()
    def create(self, session):
        with self.db: self.db.execute('INSERT INTO sessions(id) VALUES(?)',(session,))
    def _append(self, session, source_id, authority, kind, raw, payload, expected_revision):
        owns_transaction = not self.db.in_transaction
        try:
            if owns_transaction: self.db.execute('BEGIN IMMEDIATE')
            row=self.db.execute('SELECT revision FROM sessions WHERE id=?',(session,)).fetchone()
            if row is None: raise InvalidUpdate('Unknown session')
            revision=row[0]
            if revision != expected_revision: raise Conflict(f'Expected revision {expected_revision}, found {revision}')
            prior=self.db.execute('SELECT event_hash FROM events WHERE session=? ORDER BY revision DESC LIMIT 1',(session,)).fetchone()
            previous=prior[0] if prior else 'GENESIS'
            payload_json=canonical(payload)
            envelope=[session,revision+1,source_id,authority,kind,raw,payload_json,previous]
            digest=sha(canonical(envelope))
            self.db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)',(*envelope,digest))
            self.db.execute('UPDATE sessions SET revision=? WHERE id=?',(revision+1,session))
            if owns_transaction: self.db.commit()
            return revision+1
        except Exception:
            if owns_transaction: self.db.rollback()
            raise
    def apply_user_patch(self, session, source_id, message, operations, expected_revision, authority='user'):
        validated = self.validate_user_patch(session, message, operations, authority)
        return self._append(session,source_id,'user','user_patch',message,validated,expected_revision)
    def validate_user_patch(self, session, message, operations, authority='user'):
        if authority != 'user': raise InvalidUpdate('Only user-source updates can change the contract')
        state=self.snapshot(session)
        seen=set(); validated=[]
        if not isinstance(operations,list): raise InvalidUpdate('Operations must be a list')
        for op in operations:
            if set(op) != {'key','value','quote'}: raise InvalidUpdate('Unexpected operation fields')
            key,quote=op['key'],op['quote']
            if key not in USER_KEYS or key in seen: raise InvalidUpdate('Unknown or duplicate field')
            seen.add(key)
            if not isinstance(quote,str) or not quote.strip() or quote not in message:
                raise InvalidUpdate('Evidence quote must occur verbatim in the current user message')
            value=decode_value(key,op['value'])
            if key in {'task_id','goal'} and value not in quote:
                raise InvalidUpdate('Task and goal must use source wording')
            if key in {'allowed_paths','test_paths'} and any(p not in quote for p in value):
                raise InvalidUpdate('All scope paths must appear in source quote')
            if key in INT_KEYS and str(value) not in quote:
                raise InvalidUpdate('Numeric value absent from source quote')
            if key=='selected_option' and value not in state['options']:
                raise InvalidUpdate('Selected option is not an existing candidate')
            validated.append({'key':key,'value':value,'quote':quote})
        return validated
    def add_options(self, session, source_id, options, raw, expected_revision):
        if not options or not all(isinstance(k,str) and isinstance(v,str) for k,v in options.items()):
            raise InvalidUpdate('Options require string IDs and descriptions')
        return self._append(session,source_id,'assistant','options',raw,options,expected_revision)
    def add_claim(self, session, source_id, text, expected_revision):
        return self._append(session,source_id,'assistant','claim',text,{'claim':text},expected_revision)
    def observe_file(self, session, source_id, logical_path, actual_path, expected_revision):
        digest=hashlib.sha256(pathlib.Path(actual_path).read_bytes()).hexdigest()
        return self.observe_digest(session,source_id,logical_path,digest,expected_revision)
    def observe_digest(self, session, source_id, logical_path, digest, expected_revision):
        payload={'path':logical_path,'sha256':digest}
        return self._append(session,source_id,'tool','artifact',canonical(payload),payload,expected_revision)
    def record_test(self, session, source_id, artifact_versions, returncode, description, expected_revision, authority='tool'):
        if authority != 'tool': raise InvalidUpdate('Only host tool observations may verify tests')
        state=self.snapshot(session)
        if not artifact_versions or state['artifacts'] != artifact_versions:
            raise InvalidUpdate('Test provenance must match observed artifact versions')
        payload={'artifact_versions':artifact_versions,'returncode':int(returncode),'description':description}
        return self._append(session,source_id,'tool','test',canonical(payload),payload,expected_revision)
    def snapshot(self, session):
        row=self.db.execute('SELECT revision FROM sessions WHERE id=?',(session,)).fetchone()
        if row is None: raise InvalidUpdate('Unknown session')
        state={'session_id':session,'revision':row[0],'task_id':None,'goal':None,
               'constraints':{},'selected_option':None,'options':{},'artifacts':{},
               'tests':[],'claims':[],'provenance':{},'audit_head':'GENESIS'}
        for revision,sid,kind,payload,digest in self.db.execute('SELECT revision,source_id,kind,payload,event_hash FROM events WHERE session=? ORDER BY revision',(session,)):
            p=json.loads(payload)
            if kind in ('user_patch', 'conversation_contract'):
                for op in p:
                    key=op['key']
                    if key in CONSTRAINT_KEYS: state['constraints'][key]=op['value']
                    else: state[key]=op['value']
                    state['provenance'][key]={'source_id':sid,'revision':revision}
            elif kind=='options': state['options']=p
            elif kind=='claim': state['claims'].append({'source_id':sid,'text':p['claim'],'status':'unverified'})
            elif kind=='artifact': state['artifacts'][p['path']]=p['sha256']
            elif kind=='test': state['tests'].append(dict(p,source_id=sid))
            state['audit_head']=digest
        state['tests_status']='unverified'
        if state['tests']:
            latest=state['tests'][-1]
            if state['artifacts'] != latest['artifact_versions']: state['tests_status']='stale'
            elif latest['returncode']==0: state['tests_status']='passed'
            else: state['tests_status']='failed'
        return state
    def packet(self, session, max_bytes=6000):
        s=self.snapshot(session)
        essential={k:s[k] for k in ['session_id','revision','task_id','goal','constraints','selected_option','options','provenance','audit_head']}
        essential['work_state']={'artifact_versions':s['artifacts'],'tests_status':s['tests_status'],
                                 'implementation_present':bool(s['artifacts']),
                                 'latest_test':s['tests'][-1] if s['tests'] else None}
        # Byte bound is explicit. This is NOT an estimated tokenizer count.
        encoded=canonical(essential)
        if len(encoded.encode())>max_bytes: raise ContextBudgetExceeded('Essential contract and work state exceed packet budget')
        # Optional claims remain labeled. Omission never drops constraints/evidence.
        claims=[]
        for claim in reversed(s['claims'][-3:]):
            candidate=dict(essential,unverified_claims=list(reversed(claims+[claim])))
            if len(canonical(candidate).encode())<=max_bytes: claims.append(claim)
        if claims: essential['unverified_claims']=list(reversed(claims))
        return essential
    def validate_chain(self, session):
        previous='GENESIS'
        for row in self.db.execute('SELECT * FROM events WHERE session=? ORDER BY revision',(session,)):
            if row[7]!=previous or sha(canonical(list(row[:8])))!=row[8]: return False
            previous=row[8]
        return True
    def execution_packet(self, session, max_bytes=4500):
        packet=self.packet(session,max_bytes)
        if not packet['task_id'] or not packet['goal'] or not packet['constraints'].get('allowed_paths'):
            raise IncompleteContext('Worker dispatch requires a task ID, goal, and explicit edit scope')
        return packet
    def admit_proposal(self, session, packet_revision, proposal):
        s=self.snapshot(session)
        errors=[]
        if packet_revision!=s['revision']: errors.append('stale context revision')
        if proposal.get('task_id')!=s['task_id']: errors.append('task identity mismatch')
        if not s['task_id'] or not s['goal'] or not s['constraints'].get('allowed_paths'):
            errors.append('incomplete task context')
        for key in sorted(CONSTRAINT_KEYS):
            value=s['constraints'].get(key)
            actual=proposal.get('constraints',{}).get(key)
            if key=='allowed_paths' and isinstance(actual,list): actual=sorted(actual)
            if actual!=value: errors.append('constraint mismatch: '+key)
        if proposal.get('selected_option')!=s['selected_option']: errors.append('decision mismatch')
        action=proposal.get('next_action',{})
        if action.get('kind') not in ('edit','run_tests','report','clarify'):
            errors.append('unknown operation')
        if action.get('kind') in ('edit','run_tests'):
            if action.get('path') not in s['constraints'].get('allowed_paths',[]): errors.append('target outside scope')
        if proposal.get('complete') and s['tests_status']!='passed': errors.append('completion lacks current test evidence')
        if proposal.get('tests_status')!=s['tests_status']: errors.append('evidence status mismatch')
        return {'admitted':not errors,'errors':errors}
