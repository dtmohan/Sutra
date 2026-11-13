"""Versioned, host-selected uncertain-domain profiles. Metadata is not truth proof.

No fitting, automatic promotion, or model-authored policy changes. Exact checks
and deterministic resource allocations never read these optional profiles.
"""
import json,re,time
from context_manager import InvalidUpdate,Conflict,canonical,sha
from kernel_boundary import KernelBoundary,measurement

EMPTY_HASH=sha('calibration-bindings-v1:'+canonical([]))

def initialize(s):
    s.db.executescript('''
      CREATE TABLE IF NOT EXISTS calibration_profiles(profile_id TEXT,version INTEGER,digest TEXT UNIQUE,record_json TEXT NOT NULL,PRIMARY KEY(profile_id,version));
      CREATE TABLE IF NOT EXISTS calibration_bindings(session TEXT,seq INTEGER,digest TEXT UNIQUE,record_json TEXT NOT NULL,PRIMARY KEY(session,seq));
      CREATE TABLE IF NOT EXISTS calibration_revocations(digest TEXT PRIMARY KEY,reason TEXT NOT NULL,created_at REAL);
      CREATE TABLE IF NOT EXISTS calibration_receipts(session TEXT,seq INTEGER,digest TEXT UNIQUE,record_json TEXT NOT NULL,PRIMARY KEY(session,seq));
    ''')
    for table in ('calibration_profiles','calibration_bindings','calibration_revocations','calibration_receipts'):
        for op in ('UPDATE','DELETE'):
            s.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{op} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'immutable calibration record'); END")
    s.db.commit()

def fields(value,required,label):
    if not isinstance(value,dict) or set(value)!=set(required.split()):raise InvalidUpdate(label+' fields differ from schema')

def text(value,label):
    if not isinstance(value,str) or not value.strip() or len(value)>1000:raise InvalidUpdate('Invalid '+label)

def digest(value,label):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):raise InvalidUpdate('Invalid '+label+' SHA-256')

def validate_profile(p):
    fields(p,'schema_version profile_id version adapter domain metric unit measurement_method producers normalization kernel valid_from valid_until max_sample_age_seconds evidence limitations','Profile')
    if type(p['schema_version']) is not int or p['schema_version']!=1:raise InvalidUpdate('Unsupported calibration schema')
    if not isinstance(p['profile_id'],str) or not re.fullmatch('[a-z][a-z0-9_.-]{0,95}',p['profile_id']):raise InvalidUpdate('Invalid profile ID')
    if type(p['version']) is not int or p['version']<1:raise InvalidUpdate('Profile version must be a positive integer')
    if p['adapter'] not in ('smriti','karma'):raise InvalidUpdate('Unknown adapter')
    for key in ('domain','metric','unit','measurement_method','limitations'):text(p[key],key)
    producers=p['producers']
    if not isinstance(producers,list) or not 1<=len(producers)<=16:raise InvalidUpdate('Declare 1..16 calibrated producers')
    for producer in producers:text(producer,'producer')
    if len(set(producers))!=len(producers):raise InvalidUpdate('Duplicate producer')
    n=p['normalization'];fields(n,'method offset scale input_min input_max sigma_definition','Normalization')
    if n['method']!='linear_deviation':raise InvalidUpdate('Only declared linear deviation normalization is supported')
    for key in ('offset','scale','input_min','input_max'):measurement(n[key],key)
    if n['scale']<=0 or not n['offset']<=n['input_min']<n['input_max']:raise InvalidUpdate('Invalid normalization bounds')
    measurement((n['input_max']-n['offset'])/n['scale'],'normalized upper bound',nonnegative=True)
    text(n['sigma_definition'],'sigma definition')
    fields(p['kernel'],'k sigma_clause','Kernel parameters');KernelBoundary(**p['kernel'])
    start=measurement(p['valid_from'],'valid_from',nonnegative=True);end=measurement(p['valid_until'],'valid_until',positive=True)
    if end<=start:raise InvalidUpdate('Calibration validity interval is empty')
    if type(p['max_sample_age_seconds']) is not int or not 1<=p['max_sample_age_seconds']<=86400:raise InvalidUpdate('Invalid sample lifetime')
    e=p['evidence'];fields(e,'kind dataset_sha256 report_sha256 calibration_samples validation_samples procedure objective evaluator','Calibration evidence')
    if e['kind'] not in ('empirical','synthetic'):raise InvalidUpdate('Unknown calibration evidence kind')
    for key in ('dataset_sha256','report_sha256'):digest(e[key],key)
    for key in ('calibration_samples','validation_samples'):
        if type(e[key]) is not int or e[key]<=0:raise InvalidUpdate('Declare positive calibration and validation sample counts')
    for key in ('procedure','objective','evaluator'):text(e[key],key)
    if len(canonical(p).encode())>16000:raise InvalidUpdate('Calibration profile exceeds 16 KB')

def register(s,p):
    try:validate_profile(p)
    except (ValueError,TypeError,OverflowError) as exc:raise InvalidUpdate(str(exc)) from None
    raw=canonical(p);h=sha('calibration-profile-v1:'+raw)
    existing=s.db.execute('SELECT digest FROM calibration_profiles WHERE profile_id=? AND version=?',(p['profile_id'],p['version'])).fetchone()
    if existing:
        if existing[0]!=h:raise Conflict('Profile version already committed with different contents')
        return {'profile_id':p['profile_id'],'version':p['version'],'hash':h}
    latest=s.db.execute('SELECT MAX(version) FROM calibration_profiles WHERE profile_id=?',(p['profile_id'],)).fetchone()[0] or 0
    if p['version']!=latest+1:raise InvalidUpdate('Profile versions must advance by one; old versions remain immutable')
    with s.db:
        s.db.execute('INSERT INTO calibration_profiles VALUES(?,?,?,?)',(p['profile_id'],p['version'],h,raw))
        s._audit('CALIBRATION_PROFILE_REGISTERED',{'profile_hash':h,'evidence_status':'declared; not independently verified'})
    return {'profile_id':p['profile_id'],'version':p['version'],'hash':h}

def load(s,ref,now=None,usable=True):
    fields(ref,'profile_id version hash','Profile reference')
    if not isinstance(ref['profile_id'],str) or type(ref['version']) is not int or ref['version']<1:raise InvalidUpdate('Invalid profile reference identity')
    digest(ref['hash'],'profile reference')
    row=s.db.execute('SELECT record_json,digest FROM calibration_profiles WHERE profile_id=? AND version=?',(ref['profile_id'],ref['version'])).fetchone()
    if not row or row[1]!=ref['hash'] or sha('calibration-profile-v1:'+row[0])!=row[1]:raise InvalidUpdate('Calibration profile missing or hash mismatch')
    p=json.loads(row[0])
    if usable:
        now=time.time() if now is None else measurement(now,'now')
        if s.db.execute('SELECT 1 FROM calibration_revocations WHERE digest=?',(ref['hash'],)).fetchone():raise InvalidUpdate('Calibration profile revoked')
        if not p['valid_from']<=now<p['valid_until']:raise InvalidUpdate('Calibration profile expired or not yet valid')
    return p

def current(s,session,usable=False):
    if not session or not s.db.execute("SELECT 1 FROM sqlite_master WHERE name='calibration_bindings'").fetchone():return {'hash':EMPTY_HASH,'profiles':[]}
    previous=EMPTY_HASH;refs=[];expected_seq=0
    for seq,h,raw in s.db.execute('SELECT seq,digest,record_json FROM calibration_bindings WHERE session=? ORDER BY seq',(session,)):
        rec=json.loads(raw)
        expected_seq+=1
        if rec['session']!=session or seq!=expected_seq or rec['previous_hash']!=previous or rec['seq']!=seq or sha('calibration-binding-v1:'+raw)!=h:raise InvalidUpdate('Calibration binding chain corrupted')
        previous=h;refs=rec['profiles']
    for ref in refs:load(s,ref,usable=usable)
    return {'hash':previous,'profiles':refs}

def bind(s,session,refs,expected_hash):
    s.current_contract(session)
    if not isinstance(refs,list) or len(refs)>16:raise InvalidUpdate('Choose up to 16 profiles')
    old=current(s,session)
    if expected_hash!=old['hash']:raise Conflict('Calibration selection changed; refresh before selecting')
    keys=[]
    for ref in refs:
        p=load(s,ref);keys.append(tuple(p[k] for k in ('adapter','domain','metric','measurement_method')))
    if len(set(keys))!=len(keys):raise InvalidUpdate('Only one version per adapter/domain/metric/method may be selected')
    refs=sorted(refs,key=lambda r:(r['profile_id'],r['version']))
    if refs==old['profiles']:return old
    seq=s.db.execute('SELECT COUNT(*) FROM calibration_bindings WHERE session=?',(session,)).fetchone()[0]+1
    record={'session':session,'seq':seq,'previous_hash':old['hash'],'profiles':refs,'selected_at':time.time(),'contract_hash':s.origin(session)['contract_hash']}
    raw=canonical(record);h=sha('calibration-binding-v1:'+raw)
    with s.db:
        s.db.execute('INSERT INTO calibration_bindings VALUES(?,?,?,?)',(session,seq,h,raw))
        s._audit('CALIBRATION_SELECTION',{'session':session,'hash':h,'profiles':refs})
    return {'hash':h,'profiles':refs}

def revoke(s,ref,reason):
    load(s,ref,usable=False);text(reason,'revocation reason')
    with s.db:
        s.db.execute('INSERT OR IGNORE INTO calibration_revocations VALUES(?,?,?)',(ref['hash'],reason,time.time()))
        s._audit('CALIBRATION_REVOKED',{'profile':ref,'reason':reason})

def assert_binding(s,session,expected):
    actual=current(s,session,usable=True)
    if actual!=expected:raise InvalidUpdate('Calibration selection changed since this run; fresh invocation required')
    return actual

def evaluate(s,session,adapter,ref,sample):
    binding=current(s,session,usable=True)
    if ref not in binding['profiles']:raise InvalidUpdate('Uncertain evaluation requires an explicitly selected profile version')
    p=load(s,ref)
    if p['adapter']!=adapter:raise InvalidUpdate('Calibration profile belongs to another adapter')
    fields(sample,'domain metric unit measurement_method producer value sigma observed_at source evidence_sha256','Measurement')
    for key in ('domain','metric','unit','measurement_method'):
        if sample[key]!=p[key]:raise InvalidUpdate('Measurement '+key+' differs from calibrated profile')
    if sample['producer'] not in p['producers']:raise InvalidUpdate('Measurement producer is outside calibration coverage')
    if sample['source'] not in ('host_observation','model_estimate','synthetic'):raise InvalidUpdate('Unknown measurement source declaration')
    digest(sample['evidence_sha256'],'measurement evidence')
    try:
        value=measurement(sample['value'],'value');sigma=measurement(sample['sigma'],'sigma')
        observed=measurement(sample['observed_at'],'observed_at');age=time.time()-observed
        if not 0<=age<=p['max_sample_age_seconds']:raise InvalidUpdate('Measurement is stale or future-dated')
        n=p['normalization']
        if not n['input_min']<=value<=n['input_max']:raise InvalidUpdate('Measurement outside calibrated range; extrapolation prohibited')
        omega=(value-n['offset'])/n['scale'];v=KernelBoundary(**p['kernel']).evaluate(omega,sigma)
    except (ValueError,OverflowError,TypeError) as exc:raise InvalidUpdate(str(exc)) from None
    if not verify_receipts(s,session):raise InvalidUpdate('Calibration receipt chain corrupted')
    seq,previous=s.db.execute('SELECT seq,digest FROM calibration_receipts WHERE session=? ORDER BY seq DESC LIMIT 1',(session,)).fetchone() or (0,'GENESIS')
    record={'session':session,'seq':seq+1,'previous_hash':previous,'contract_hash':s.origin(session)['contract_hash'],
        'logic_hash':s.logic_hash,'binding':binding,'adapter':adapter,'profile':ref,'sample':sample,
        'omega':v.omega,'sigma':v.sigma,'k':v.k,'sigma_clause':p['kernel']['sigma_clause'],'decision':v.decision.value,
        'evaluated_at':time.time(),'evidence_kind':p['evidence']['kind'],'evidence_status':'declared; not independently verified',
        'semantic_correctness_verified':False,'grants_tool_permission':False}
    raw=canonical(record);h=sha('calibration-receipt-v1:'+raw)
    with s.db:
        s.db.execute('INSERT INTO calibration_receipts VALUES(?,?,?,?)',(session,seq+1,h,raw))
        s._audit('CALIBRATED_MEASUREMENT',{'session':session,'receipt':h,'profile':ref,'decision':v.decision.value})
    return dict(record,receipt_id=h)

def verify_receipts(s,session):
    """Verify historical integrity, not present-day validity or semantic accuracy."""
    previous='GENESIS';expected=0
    try:
        for seq,h,raw in s.db.execute('SELECT seq,digest,record_json FROM calibration_receipts WHERE session=? ORDER BY seq',(session,)):
            rec=json.loads(raw);expected+=1
            if seq!=expected or rec['seq']!=seq or rec['session']!=session or rec['previous_hash']!=previous or sha('calibration-receipt-v1:'+raw)!=h:return False
            p=load(s,rec['profile'],usable=False)
            if rec['adapter']!=p['adapter'] or rec['profile'] not in rec['binding']['profiles']:return False
            bound=s.db.execute('SELECT record_json FROM calibration_bindings WHERE session=? AND digest=?',(session,rec['binding']['hash'])).fetchone()
            if not bound or sha('calibration-binding-v1:'+bound[0])!=rec['binding']['hash'] or json.loads(bound[0])['profiles']!=rec['binding']['profiles']:return False
            v=KernelBoundary(**p['kernel']).evaluate(rec['omega'],rec['sigma'])
            if rec['omega']!=(rec['sample']['value']-p['normalization']['offset'])/p['normalization']['scale'] or rec['sigma']!=rec['sample']['sigma']:return False
            if v.decision.value!=rec['decision'] or rec['k']!=p['kernel']['k'] or rec['sigma_clause']!=p['kernel']['sigma_clause']:return False
            if rec['grants_tool_permission'] is not False or rec['semantic_correctness_verified'] is not False:return False
            previous=h
        return True
    except (InvalidUpdate,ValueError,TypeError,KeyError):return False

def overview(s,session=None):
    if not s.db.execute("SELECT 1 FROM sqlite_master WHERE name='calibration_profiles'").fetchone():return {'binding':{'hash':EMPTY_HASH,'profiles':[]},'profiles':[]}
    entries=[]
    for pid,version,h,raw in s.db.execute('SELECT profile_id,version,digest,record_json FROM calibration_profiles ORDER BY profile_id,version'):
        ref={'profile_id':pid,'version':version,'hash':h};p=json.loads(raw)
        try:load(s,ref);status='available'
        except InvalidUpdate as exc:status=str(exc)
        entries.append(dict(ref,adapter=p['adapter'],domain=p['domain'],metric=p['metric'],valid_until=p['valid_until'],
                            evidence_kind=p['evidence']['kind'],evidence_status='declared; not independently verified',status=status))
    latest=s.db.execute('SELECT digest,record_json FROM calibration_receipts WHERE session=? ORDER BY seq DESC LIMIT 1',(session,)).fetchone() if session else None
    return {'binding':current(s,session),'profiles':entries,'receipts_valid':verify_receipts(s,session) if session else True,
            'last_evaluation':dict(json.loads(latest[1]),receipt_id=latest[0]) if latest else None}
