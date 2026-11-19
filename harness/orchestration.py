"""One durable state machine for workflow, branch and dispatch ownership.

Model interpretations are proposals. Only this host boundary commits transitions,
allocates child budgets, or admits effects. The canonical kernel is untouched.
"""
import copy
from contextlib import contextmanager
from contextvars import ContextVar
import json
import time
import uuid
from context_manager import InvalidUpdate, canonical, sha
from karma_adapter import KarmaAdapter

ACTIVE = ContextVar('sutra_orchestration_node', default=None)
TERMINAL = {'DONE', 'CANCELLED', 'QUARANTINED'}
SLICE_PROTOCOL = 1
SLICE_STOPS = {'CHILD_SLICE_TIMED_OUT', 'CHILD_SLICE_OUTPUT_LIMIT'}
EDGES = {
 'READY': {'RUNNING','WAITING_CHILDREN','DONE','PAUSED','CANCELLED','QUARANTINED'},
 'RUNNING': {'READY','DONE','PAUSED','CANCELLED','QUARANTINED'},
 'WAITING_CHILDREN': {'READY','PAUSED','CANCELLED','QUARANTINED'},
 'PAUSED': {'READY','CLASSIFYING','NEED_ARM','BINDING','MAPPING','BRANCHING','CHILDREN','SYNTHESIS','CODING','WORKSPACE','NEED_TOOL_APPROVAL','DONE','CANCELLED','QUARANTINED'},
 'CREATED': {'CLASSIFYING','CANCELLED','PAUSED'},
 'CLASSIFYING': {'NEED_ARM','BINDING','DONE','PAUSED','CANCELLED'},
 'NEED_ARM': {'BINDING','CANCELLED','PAUSED'},
 'BINDING': {'NEED_PERMISSION','WORKSPACE','PAUSED','CANCELLED'},
 'NEED_PERMISSION': {'BINDING','CANCELLED','PAUSED'},
 'WORKSPACE': {'MAPPING','BRANCHING','CODING','NEED_TOOL_APPROVAL','PAUSED','CANCELLED'},
 'NEED_TOOL_APPROVAL': {'WORKSPACE','DONE','PAUSED','CANCELLED'},
 'MAPPING': {'WORKSPACE','PAUSED','CANCELLED'},
 'BRANCHING': {'CHILDREN','SYNTHESIS','PAUSED','CANCELLED'},
 'CHILDREN': {'CHILDREN','SYNTHESIS','PAUSED','CANCELLED'},
 'SYNTHESIS': {'DONE','PAUSED','CANCELLED'},
 'CODING': {'DONE','AWAITING_ACTION','PAUSED','CANCELLED'},
 'AWAITING_ACTION': {'EXECUTING_ACTION','CANCELLED'},
 'EXECUTING_ACTION': {'DONE','CANCELLING'},
 'CANCELLING': {'DONE'},
}

def initialize(s):
    from allocation_recovery import initialize as initialize_allocations
    initialize_allocations(s)
    s.db.executescript('''
      CREATE TABLE IF NOT EXISTS orchestration_nodes(id TEXT PRIMARY KEY,parent_id TEXT,session TEXT,record_json TEXT,digest TEXT);
      CREATE TABLE IF NOT EXISTS orchestration_events(node_id TEXT,seq INTEGER,record_json TEXT,digest TEXT,PRIMARY KEY(node_id,seq));
      CREATE TABLE IF NOT EXISTS orchestration_charges(id TEXT PRIMARY KEY,node_id TEXT,kind TEXT,resource_id TEXT UNIQUE,cost_json TEXT);
      CREATE TABLE IF NOT EXISTS orchestration_settlements(charge_id TEXT PRIMARY KEY,record_json TEXT,digest TEXT);
    ''')
    for table in ('orchestration_nodes','orchestration_events','orchestration_charges','orchestration_settlements'):
        for op in ('UPDATE','DELETE'):
            s.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{op} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'immutable orchestration record'); END")
    s.db.commit()
    from accounting_batches import initialize as initialize_accounting
    initialize_accounting(s)

def node(s, nid):
    row=s.db.execute('SELECT record_json,digest FROM orchestration_nodes WHERE id=?',(nid,)).fetchone()
    if not row or sha('orchestration-node-v1:'+row[0])!=row[1]:raise InvalidUpdate('Missing or corrupt orchestration node')
    from accounting_batches import assert_node_digest
    assert_node_digest(s,nid,row[1])
    return json.loads(row[0])

def events(s,nid):
    n=node(s,nid);state=n['initial_state'];previous='GENESIS';result=[]
    for seq,raw,digest in s.db.execute('SELECT seq,record_json,digest FROM orchestration_events WHERE node_id=? ORDER BY seq',(nid,)):
        e=json.loads(raw)
        if seq!=len(result)+1 or e['previous']!=previous or e['from']!=state or sha('orchestration-event-v1:'+raw)!=digest:
            raise InvalidUpdate('Orchestration transition chain corrupted')
        if e['to']!=state and e['to'] not in EDGES.get(state,set()):raise InvalidUpdate('Invalid recorded orchestration transition')
        state=e['to'];previous=digest;result.append(dict(e,receipt=digest))
    return result

def state(s,nid):
    from accounting_batches import current_state
    return current_state(s,nid)

def transition(s,nid,target,kind,data=None,kernel=None):
    from accounting_batches import event_head
    current,seq,previous=event_head(s,nid)
    if current in TERMINAL or (target!=current and target not in EDGES.get(current,set())):
        raise InvalidUpdate('Orchestration transition forbidden: '+current+' -> '+target)
    e={'node_id':nid,'seq':seq+1,'from':current,'to':target,'kind':kind,'data':data or {},
       'kernel':kernel,'previous':previous,'at':time.time()}
    raw=canonical(e);digest=sha('orchestration-event-v1:'+raw)
    s.db.execute('INSERT INTO orchestration_events VALUES(?,?,?,?)',(nid,e['seq'],raw,digest))
    s._audit('ORCHESTRATION_TRANSITION',{'node_id':nid,'receipt':digest,'kind':kind,'state':target,'kernel':kernel,'at':e['at']})
    return dict(e,receipt=digest)

def create(s,kind,session=None,parent=None,**data):
    n=dict(data,id=uuid.uuid4().hex,kind=kind,session=session,parent_id=parent,
           initial_state='CREATED' if kind=='flow' else 'READY',created_at=time.time())
    raw=canonical(n);s.db.execute('INSERT INTO orchestration_nodes VALUES(?,?,?,?,?)',(n['id'],parent,session,raw,sha('orchestration-node-v1:'+raw)))
    s._audit('ORCHESTRATION_CREATE',{'node_id':n['id'],'kind':kind,'session':session,'parent':parent})
    return n

def binding(s,sid):
    from calibration import current
    return {'session':sid,'contract_hash':s.origin(sid,bootstrap=True)['contract_hash'],
            'calibration_hash':current(s,sid,usable=True)['hash'],'logic_hash':s.logic_hash}

def permissions(s,sid):
    from native_access import read_scope
    c=s.snapshot(sid)['constraints'];paths=c.get('allowed_paths',['.'])
    return {'read_paths':read_scope(s,sid) if 'allowed_paths' in c else ['.'],
            'write_paths':paths if c.get('edits_allowed') else [],
            'test_paths':c.get('test_paths',[]) if c.get('tests_allowed') else [],
            'network_allowed':c.get('network_allowed',False),'dependencies_allowed':c.get('dependencies_allowed',False)}

def task(s,sid):
    b=binding(s,sid)
    for nid, in s.db.execute("SELECT id FROM orchestration_nodes WHERE session=? AND json_extract(record_json,'$.kind')='task' ORDER BY rowid DESC",(sid,)):
        n=node(s,nid)
        if n['binding']==b:return n
    from review_budget import allocations
    c=s.snapshot(sid)['constraints'];limits=allocations(c) or {'operations':1000000,'calls':1000000,'tokens':10**12,'source_bytes':10**12,'seconds':10**9}
    limits=dict(limits,actions=c.get('max_actions',1000000))
    return create(s,'task',sid,binding=b,permissions=permissions(s,sid),limits=limits,goal=s.snapshot(sid)['goal'],context=None)

def checked(s,nid):
    from accounting_audit import check_admission
    check_admission(s)
    n=node(s,nid)
    if n['kind']=='flow':return n
    if n['binding']!=binding(s,n['session']):raise InvalidUpdate('Orchestration contract, calibration or policy changed')
    if state(s,nid) in TERMINAL:raise InvalidUpdate('Orchestration node is terminal')
    if n['parent_id']:checked(s,n['parent_id'])
    return n

def descendants(s,nid):
    result=[nid]
    for child, in s.db.execute('SELECT id FROM orchestration_nodes WHERE parent_id=?',(nid,)):
        result.extend(descendants(s,child))
    return result

def spent(s,nid):
    from accounting_batches import summary
    n=node(s,nid);used={k:0 for k in n['limits']}
    if n['kind']=='task':
        from review_budget import usage
        old=usage(s,n['session'])
        if old:
            used.update(old['used'])
            if s.db.execute("SELECT 1 FROM sqlite_master WHERE name='host_actions'").fetchone():
                used['actions']=s.db.execute("SELECT COUNT(*) FROM host_actions WHERE session=? AND json_extract(record_json,'$.contract_hash')=?",(n['session'],n['binding']['contract_hash'])).fetchone()[0]
            used['seconds']+=summary(s,nid)['action_seconds']
            return used
    return summary(s,nid)['used']

def active_children(s,nid):
    from accounting_batches import summary
    return [node(s,child) for child in summary(s,nid)['active_children']]

def _gates(s,nid,cost):
    n=checked(s,nid);frames=[];excluded=None
    while n:
        used=spent(s,n['id']);reserved={k:0 for k in n['limits']}
        for c in active_children(s,n['id']):
            child=c['id']
            if child==excluded:continue
            actual=spent(s,child)
            child_limits=effective_limits(s,c)
            for key in reserved:reserved[key]+=max(0,child_limits[key]-actual[key])
        effective={k:used[k]+reserved[k] for k in used}
        k=KarmaAdapter().allocations(effective,effective_limits(s,n),cost)
        frames.append(dict(k,node_id=n['id'],consumed=used,reserved_for_other_children=reserved))
        excluded=n['id'];n=checked(s,n['parent_id']) if n['parent_id'] else None
    return frames

def gates(s,nid,cost):
    from accounting_metrics import measure
    with measure(s,'budget_gate',node_id=nid):
        return _gates(s,nid,cost)

def effective_limits(s,n):
    if n['kind'] == 'task':
        from review_budget import usage
        budget = usage(s,n['session'])
        return dict(n['limits'], **budget['limits']) if budget else n['limits']
    if n['kind'] in ('execution', 'branch'):
        amendments = [event['data']['tokens'] for event in events(s, n['id'])
                      if event['kind'] == 'CHILD_ALLOCATION_INCREASED']
        if amendments:
            return dict(n['limits'], tokens=n['limits']['tokens'] + sum(amendments))
    return n['limits']

class OrchestrationStop(InvalidUpdate):
    def __init__(self,frames):
        self.frames=frames;self.frame=next(f for f in frames if f['decision']!='ALLOW')
        super().__init__('Orchestration '+self.frame['decision']+': '+self.frame.get('limiting_dimension','resource'))

def allowed(frames):
    if any(f['decision']!='ALLOW' for f in frames):raise OrchestrationStop(frames)

def subset(child,parent):
    from repository_tools import normalized,scoped
    if set(child)!=set(parent):raise InvalidUpdate('Child permission dimensions differ')
    for key in ('read_paths','write_paths','test_paths'):
        if not isinstance(child[key],list):raise InvalidUpdate('Invalid child scope')
        for name in child[key]:
            normalized(name,root=True)
            if not scoped(name,parent[key]):raise InvalidUpdate('Child cannot widen '+key)
    for key in ('write_paths','test_paths'):
        if any(not scoped(name,child['read_paths']) for name in child[key]):raise InvalidUpdate('Child action scope exceeds its read scope')
    for key in ('network_allowed','dependencies_allowed'):
        if type(child[key]) is not bool or child[key] and not parent[key]:raise InvalidUpdate('Child cannot grant '+key)

def fork(s,nid,proposals,signals,context):
    if not proposals:raise InvalidUpdate('Empty branch proposal')
    with s.db:
        s.db.execute('BEGIN IMMEDIATE');parent=checked(s,nid)
        if state(s,nid)!='READY':raise InvalidUpdate('Parent is not ready to branch')
        cost={k:0 for k in parent['limits']}
        for p in proposals:
            subset(p['permissions'],parent['permissions'])
            from repository_tools import scoped,normalized
            if not p['files'] or len(set(p['files']))!=len(p['files']):raise InvalidUpdate('Invalid child source assignment')
            for path in p['files']:
                normalized(path)
                if not scoped(path,p['permissions']['read_paths']):raise InvalidUpdate('Child file is outside its scope')
            if p.get('roles',['subagent'])!=['subagent']:raise InvalidUpdate('Only read-only review children are implemented')
            if set(p['limits'])!=set(cost):raise InvalidUpdate('Child allocation dimensions differ')
            for key,value in p['limits'].items():
                if type(value) not in (int,float) or value<=0:raise InvalidUpdate('Child allocations must be positive')
                cost[key]+=value
        frames=gates(s,nid,cost);allowed(frames)
        children=[]
        for p in proposals:
            child_context=copy.deepcopy(context)
            if 'versions' in child_context:child_context['versions']={name:context['versions'][name] for name in p['files']}
            children.append(create(s,'branch',parent['session'],nid,binding=parent['binding'],
                goal=parent['goal'],objective=p['objective'],permissions=p['permissions'],limits=p['limits'],
                context=child_context,context_hash=sha(canonical(child_context)),files=p['files'],
                roles=p.get('roles',['subagent']),slice_protocol=SLICE_PROTOCOL))
        transition(s,nid,'WAITING_CHILDREN','FORK',{'children':[c['id'] for c in children],'signals':signals,'frames':frames},frames[0])
        return children

@contextmanager
def scope(nid):
    token=ACTIVE.set(nid)
    try:yield
    finally:ACTIVE.reset(token)

def packet(n):
    # Large file manifests stay in the ledger; the model receives a bound handle.
    return {'id':n['id'],'parent_id':n['parent_id'],'context_hash':n['context_hash'],
            'objective':n['objective'],'capabilities':{'read':True,'edit':False,'tests':False,'network':False,'dependencies':False}}

def scope_check(n,kind,paths,role=None):
    from repository_tools import scoped
    if n['kind']=='branch' and kind=='model' and role not in n['roles']:raise InvalidUpdate('Child role is not permitted')
    key='write_paths' if kind=='edit' else 'test_paths' if kind=='run_tests' else 'read_paths'
    if kind in ('edit','run_tests') and not paths:raise InvalidUpdate('Action target required')
    if any(not scoped(p,n['permissions'][key]) for p in paths):raise InvalidUpdate('Operation exceeds orchestration '+key)

def admit(s,sid,kind,resource_id,cost,paths=(),role=None,payload=None,model=None,resource=None):
    # Called inside the same IMMEDIATE transaction that commits the host effect.
    n=checked(s,ACTIVE.get()) if ACTIVE.get() else task(s,sid)
    if n['session']!=sid:raise InvalidUpdate('Cross-task orchestration dispatch')
    if state(s,n['id']) not in ({'READY','RUNNING'} if n['kind']=='task' else {'READY'}):raise InvalidUpdate('Orchestration dispatch requires a ready node')
    scope_check(n,kind,paths,role)
    if n['kind']=='branch' and kind=='model':
        if not payload or payload.get('branch')!=packet(n):raise InvalidUpdate('Child model context differs from committed branch')
        if n.get('slice_protocol') or 'child_slice' in payload:
            check_slice(s,n['id'],payload.get('child_slice'),current=True)
            if recover_child(s,n['id']):
                raise InvalidUpdate('Recover the settled child slice before dispatching again')
    frames=gates(s,n['id'],cost);allowed(frames)
    if resource and resource.get('decision') and resource['decision']!='ALLOW':raise OrchestrationStop([resource])
    cid=uuid.uuid4().hex
    s.db.execute('INSERT INTO orchestration_charges VALUES(?,?,?,?,?)',(cid,n['id'],kind,resource_id,canonical(cost)))
    transition(s,n['id'],'RUNNING','DISPATCH',{'charge_id':cid,'resource_id':resource_id,'kind':kind,'role':role,'model':model,'frames':frames,'resource':resource},frames[0])
    from accounting_batches import rotate
    rotate(s)
    return {'node_id':n['id'],'charge_id':cid,'frames':frames}

def settle(s,owner,receipt,elapsed=None):
    if not owner:return
    if s.db.execute('SELECT 1 FROM orchestration_settlements WHERE charge_id=?',(owner['charge_id'],)).fetchone():return
    row=s.db.execute('SELECT cost_json,node_id,kind,resource_id FROM orchestration_charges WHERE id=?',(owner['charge_id'],)).fetchone()
    if not row or row[1]!=owner['node_id']:raise InvalidUpdate('Invalid orchestration settlement')
    cost=json.loads(row[0])
    if elapsed is not None and 'seconds' in cost:cost['seconds']=max(0,elapsed)
    if row[2] == 'model':
        call = s.db.execute('SELECT i.record_json,r.record_json FROM invocations i JOIN invocation_receipts r ON r.invocation_id=i.id WHERE i.id=? AND r.id=?', (row[3], receipt)).fetchone()
        if call:
            from budget_accounting import used
            envelope, evidence = (json.loads(raw) for raw in call)
            if envelope.get('orchestration') != owner:
                raise InvalidUpdate('Invocation settlement owner changed')
            cost['tokens'] = used(envelope, evidence)
    r={'charge_id':owner['charge_id'],'receipt':receipt,'used':cost,'at':time.time()};raw=canonical(r)
    s.db.execute('INSERT INTO orchestration_settlements VALUES(?,?,?)',(owner['charge_id'],raw,sha('orchestration-settlement-v1:'+raw)))
    pending=s.db.execute('SELECT COUNT(*) FROM orchestration_charges c LEFT JOIN orchestration_settlements r ON r.charge_id=c.id WHERE c.node_id=? AND r.charge_id IS NULL',(owner['node_id'],)).fetchone()[0]
    if state(s,owner['node_id'])=='RUNNING':transition(s,owner['node_id'],'RUNNING' if pending else 'READY','SETTLED',r)
    from accounting_batches import rotate
    rotate(s)

def _child_receipt(s,n,iid):
    row=s.db.execute("""
        SELECT i.record_json,i.digest,r.record_json,r.id,d.payload_json,d.payload_hash,
               c.id,c.node_id,t.record_json,t.digest
        FROM invocations i JOIN invocation_receipts r ON r.invocation_id=i.id AND r.session=i.session
        JOIN deliveries d ON d.id=i.delivery_id AND d.session=i.session
        JOIN orchestration_charges c ON c.resource_id=i.id AND c.kind='model'
        JOIN orchestration_settlements t ON t.charge_id=c.id
        WHERE i.id=? AND i.session=?
          AND NOT EXISTS (SELECT 1 FROM invocation_quarantine q WHERE q.invocation_id=i.id)
    """,(iid,n['session'])).fetchone()
    if not row or n['kind']!='branch' or row[7]!=n['id']:
        raise InvalidUpdate('Settled result does not belong to this child or was quarantined')
    raw_envelope,envelope_hash,raw_receipt,receipt_id,raw_packet,packet_hash,charge_id,_,raw_settlement,settlement_hash=row
    env,receipt,payload,settlement=map(json.loads,(raw_envelope,raw_receipt,raw_packet,raw_settlement))
    owner=env.get('orchestration') or {}
    if (env['role']!='subagent' or owner.get('node_id')!=n['id'] or owner.get('charge_id')!=charge_id
            or payload.get('branch')!=packet(n) or env['contract_hash']!=n['binding']['contract_hash']
            or env['logic_hash']!=n['binding']['logic_hash']
            or sha('invocation-v1:'+raw_envelope)!=envelope_hash or receipt['invocation_hash']!=envelope_hash
            or sha('invocation-receipt-v1:'+raw_receipt)!=receipt_id or receipt['invocation_id']!=iid
            or sha(raw_packet)!=packet_hash or env['payload_hash']!=packet_hash or receipt['payload_hash']!=packet_hash
            or sha(canonical(receipt['proposal']))!=receipt['response_hash']
            or sha('orchestration-settlement-v1:'+raw_settlement)!=settlement_hash
            or settlement['charge_id']!=charge_id or settlement['receipt']!=receipt_id):
        raise InvalidUpdate('Child invocation, receipt or settlement failed integrity validation')
    return env,receipt,payload


def _cursor(previous):
    return {'version':SLICE_PROTOCOL,'ordinal':len(previous)+1,
            'predecessor':previous[-1]['invocation_id'] if previous else None}


def _slice_history(s,nid):
    n=node(s,nid);saved=[]
    if n['kind']!='branch' or n.get('slice_protocol',SLICE_PROTOCOL)!=SLICE_PROTOCOL:
        raise InvalidUpdate('Unsupported child slice protocol')
    for event in events(s,nid):
        if event['kind']!='CHILD_SLICE_SAVED':continue
        data=event['data'];iid=data['invocation_id']
        env,receipt,payload=_child_receipt(s,n,iid)
        output=receipt['proposal']
        if (data['child_slice']!=_cursor(saved) or payload.get('child_slice')!=data['child_slice']
                or receipt['status']!='accepted' or output.get('slice_status')!='continue'):
            raise InvalidUpdate('Child slice history does not match its accepted predecessor')
        saved.append({'invocation_id':iid,'model':env['model'],'output':output})
    return saved


def slice_cursor(s,nid):
    return _cursor(_slice_history(s,nid))


def slice_history(s,nid,cursor):
    saved=_slice_history(s,nid)
    ordinal=cursor.get('ordinal') if isinstance(cursor,dict) else None
    if (type(ordinal) is not int or not 1<=ordinal<=len(saved)+1
            or type(cursor.get('version')) is not int or cursor!=_cursor(saved[:ordinal-1])):
        raise InvalidUpdate('Child slice cursor does not match its checkpoint history')
    return saved[:ordinal-1]


def check_slice(s,nid,cursor,current=False):
    if current:
        expected=slice_cursor(s,nid)
        if (not isinstance(cursor,dict) or type(cursor.get('version')) is not int
                or type(cursor.get('ordinal')) is not int or cursor!=expected):
            raise InvalidUpdate('Child slice cursor is stale or differs from its predecessor')
    else:
        slice_history(s,nid,cursor)


def _resumable_failure(s,env,receipt):
    if receipt['status']!='quarantined':return None
    kind=receipt.get('failure_kind')
    if kind=='timeout':return kind
    if kind=='output_limit':
        last=s.run(env['run_id'])['events'][-1]
        if (last['kind']=='FIN' and last['data'].get('reason')=='child_slice_output_limit'
                and last['data'].get('invocation_id')==env['id']):
            return kind
    return None


def recover_child(s,nid):
    n=checked(s,nid)
    cursor=slice_cursor(s,nid)
    stopped={event['data']['invocation_id'] for event in events(s,nid) if event['kind'] in SLICE_STOPS}
    for iid, in s.db.execute("""
        SELECT i.id FROM invocations i JOIN invocation_receipts r ON r.invocation_id=i.id
        WHERE i.session=? AND json_extract(i.record_json,'$.orchestration.node_id')=?
          AND (json_extract(r.record_json,'$.status')='accepted'
               OR json_extract(r.record_json,'$.failure_kind') IN ('timeout','output_limit'))
        ORDER BY i.rowid DESC
    """,(n['session'],nid)):
        if iid in stopped:continue
        env,receipt,payload=_child_receipt(s,n,iid)
        delivered=payload.get('child_slice')
        if delivered!=cursor and not (delivered is None and not n.get('slice_protocol') and cursor['ordinal']==1):
            continue
        failure=_resumable_failure(s,env,receipt)
        if failure or receipt['status']=='accepted':
            return {'invocation_id':iid,'model':env['model'],'output':receipt['proposal'],
                    'failure_kind':failure}
    return None


def timeout_child(s,nid,result_id):
    with s.db:
        n=checked(s,nid)
        env,receipt,payload=_child_receipt(s,n,result_id)
        cursor=payload.get('child_slice')
        check_slice(s,nid,cursor,current=True)
        kind=_resumable_failure(s,env,receipt)
        if not kind or state(s,nid)!='READY':
            raise InvalidUpdate('Child slice did not settle as a recoverable stop')
        for event in events(s,nid):
            if event['kind'] in SLICE_STOPS and event['data']['invocation_id']==result_id:return event
        label='CHILD_SLICE_TIMED_OUT' if kind=='timeout' else 'CHILD_SLICE_OUTPUT_LIMIT'
        return transition(s,nid,'READY',label,{'invocation_id':result_id,'child_slice':cursor,'failure_kind':kind})


def complete(s,nid,result_id):
    with s.db:
        n=checked(s,nid)
        _,receipt,payload=_child_receipt(s,n,result_id)
        cursor=payload.get('child_slice')
        if n.get('slice_protocol') or cursor is not None:
            check_slice(s,nid,cursor,current=True)
        if state(s,nid)!='READY' or receipt['status']!='accepted':
            raise InvalidUpdate('Child result was not accepted and settled')
        output=s.load_result(n['session'],result_id)
        status=output.get('slice_status','complete')
        data={'invocation_id':result_id}
        if cursor is not None:data['child_slice']=cursor
        if status=='continue' and cursor is not None:
            return transition(s,nid,'READY','CHILD_SLICE_SAVED',data)
        if status!='complete':raise InvalidUpdate('Invalid child slice outcome')
        return transition(s,nid,'DONE','RESULT',data)

def join(s,nid):
    with s.db:
        parent=checked(s,nid);results=[]
        forks=[e for e in events(s,nid) if e['kind']=='FORK']
        if state(s,nid)!='WAITING_CHILDREN' or not forks:raise InvalidUpdate('Parent is not awaiting a join')
        for cid in forks[-1]['data']['children']:
            if state(s,cid)!='DONE':raise InvalidUpdate('Child results are not ready to join')
            c=node(s,cid)
            if c['binding']!=binding(s,parent['session']):raise InvalidUpdate('Child context became stale')
            final=events(s,cid)[-1]
            if final['kind']!='RESULT':raise InvalidUpdate('Child has no completed slice to join')
            iid=final['data']['invocation_id']
            _,receipt,payload=_child_receipt(s,c,iid)
            if c.get('slice_protocol') or 'child_slice' in payload:
                check_slice(s,cid,payload.get('child_slice'),current=True)
            if receipt['status']!='accepted' or receipt['proposal'].get('slice_status','complete')!='complete':
                raise InvalidUpdate('Child result is not a completed, accepted slice')
            s.load_result(parent['session'],iid);results.append(iid)
        if not results:raise InvalidUpdate('Join requires child results')
        transition(s,nid,'READY','JOIN',{'invocations':results})
        return results

def overview(db,sid=None):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='orchestration_nodes'").fetchone():return {'nodes':[],'scope':'not_started'}
    rows=db.execute('SELECT id,record_json FROM orchestration_nodes '+('WHERE session=? ' if sid else '')+'ORDER BY rowid DESC LIMIT 40',(sid,) if sid else ()).fetchall()
    result=[]
    for nid,raw in rows:
        n=json.loads(raw);last=db.execute('SELECT record_json FROM orchestration_events WHERE node_id=? ORDER BY seq DESC LIMIT 1',(nid,)).fetchone();e=json.loads(last[0]) if last else {}
        dispatch=db.execute("SELECT record_json FROM orchestration_events WHERE node_id=? AND json_extract(record_json,'$.kind') IN ('DISPATCH','FORK') ORDER BY seq DESC LIMIT 1",(nid,)).fetchone()
        last_dispatch=json.loads(dispatch[0]) if dispatch else {}
        fallback=db.execute("SELECT record_json FROM orchestration_events WHERE node_id=? AND json_extract(record_json,'$.kind')='RESOURCE_MODEL_FALLBACK' ORDER BY seq DESC LIMIT 1",(nid,)).fetchone()
        resource_fallback=json.loads(fallback[0])['data']['resource_fallback'] if fallback else None
        used={k:0 for k in n.get('limits',{})}
        for cost,settled in db.execute('WITH RECURSIVE owned(id) AS (SELECT ? UNION ALL SELECT n.id FROM orchestration_nodes n JOIN owned p ON n.parent_id=p.id) SELECT c.cost_json,r.record_json FROM orchestration_charges c JOIN owned ON owned.id=c.node_id LEFT JOIN orchestration_settlements r ON c.id=r.charge_id',(nid,)):
            for k,v in (json.loads(settled)['used'] if settled else json.loads(cost)).items():used[k]=used.get(k,0)+v
        page = db.execute("SELECT payload_json FROM page_audit WHERE event='SOURCE_PAGE_PLAN' AND json_extract(payload_json,'$.owner')=? ORDER BY seq DESC LIMIT 1", (nid,)).fetchone()
        result.append({'id':nid,'parent':n['parent_id'],'kind':n['kind'],'state':e.get('to',n['initial_state']),
                       'objective':n.get('objective',n.get('goal','')),'kernel':e.get('kernel') or last_dispatch.get('kernel'),'limits':n.get('limits'),
                       'permissions':n.get('permissions'),'context_hash':n.get('context_hash'),'last_event':e.get('kind'),
                       'resource_fallback':resource_fallback,
                       'source_paging':json.loads(page[0]) if page else None,
                       'stop':(e.get('data',{}).get('workflow_stop') or e.get('data',{}).get('mapping_stop')) if e.get('to')=='PAUSED' else None,
                       'model':last_dispatch.get('data',{}).get('model'),'used':used,'files':n.get('files',[])})
    return {'nodes':result,'execution':'serial local dispatch','kernel':'unchanged STP','scope':'latest 40 orchestration nodes'}


def cancel(s,nid,reason='User cancelled'):
    with s.db:
        s.db.execute('BEGIN IMMEDIATE')
        for cid in reversed(descendants(s,nid)):
            if state(s,cid) in ('EXECUTING_ACTION','CANCELLING'):
                # An action remains owned and charged until process cleanup and receipt settlement.
                transition(s,cid,'CANCELLING','CANCEL_REQUESTED',{'reason':reason})
            elif state(s,cid) not in TERMINAL:
                transition(s,cid,'CANCELLED','CANCELLED',{'reason':reason})

def abandon_children(s,nid,reason):
    # Stop a failed batch without discarding any consumed allocation.
    with s.db:
        for cid, in s.db.execute('SELECT id FROM orchestration_nodes WHERE parent_id=?',(nid,)).fetchall():
            if state(s,cid) not in TERMINAL:transition(s,cid,'CANCELLED','BATCH_STOPPED',{'reason':reason})
        if state(s,nid)=='WAITING_CHILDREN':transition(s,nid,'READY','BATCH_STOPPED',{'reason':reason})


def action_flow(s,iid):
    row=s.db.execute("SELECT node_id FROM orchestration_events WHERE json_extract(record_json,'$.kind')='ACTION_PROPOSED' AND json_extract(record_json,'$.data.invocation_id')=? ORDER BY rowid DESC LIMIT 1",(iid,)).fetchone()
    if not row:return None
    if state(s,row[0])!='AWAITING_ACTION':raise InvalidUpdate('Workflow action is no longer pending')
    return row[0]
