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
from kernel_boundary import frame

ACTIVE = ContextVar('sutra_orchestration_node', default=None)
TERMINAL = {'DONE', 'CANCELLED', 'QUARANTINED'}
EDGES = {
 'READY': {'RUNNING','WAITING_CHILDREN','DONE','PAUSED','CANCELLED','QUARANTINED'},
 'RUNNING': {'READY','DONE','PAUSED','CANCELLED','QUARANTINED'},
 'WAITING_CHILDREN': {'READY','PAUSED','CANCELLED','QUARANTINED'},
 'PAUSED': {'READY','CLASSIFYING','MAPPING','BRANCHING','CHILDREN','SYNTHESIS','CODING','CANCELLED','QUARANTINED'},
 'CREATED': {'CLASSIFYING','CANCELLED','PAUSED'},
 'CLASSIFYING': {'NEED_ARM','BINDING','DONE','PAUSED','CANCELLED'},
 'NEED_ARM': {'BINDING','CANCELLED','PAUSED'},
 'BINDING': {'NEED_PERMISSION','WORKSPACE','PAUSED','CANCELLED'},
 'NEED_PERMISSION': {'BINDING','CANCELLED','PAUSED'},
 'WORKSPACE': {'MAPPING','BRANCHING','CODING','PAUSED','CANCELLED'},
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

def node(s, nid):
    row=s.db.execute('SELECT record_json,digest FROM orchestration_nodes WHERE id=?',(nid,)).fetchone()
    if not row or sha('orchestration-node-v1:'+row[0])!=row[1]:raise InvalidUpdate('Missing or corrupt orchestration node')
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
    history=events(s,nid)
    return history[-1]['to'] if history else node(s,nid)['initial_state']

def transition(s,nid,target,kind,data=None,kernel=None):
    history=events(s,nid);current=history[-1]['to'] if history else node(s,nid)['initial_state']
    if current in TERMINAL or (target!=current and target not in EDGES.get(current,set())):
        raise InvalidUpdate('Orchestration transition forbidden: '+current+' -> '+target)
    k=kernel or frame('karma','orchestration-transition-v1',0.,0.)
    e={'node_id':nid,'seq':len(history)+1,'from':current,'to':target,'kind':kind,'data':data or {},
       'kernel':k,'previous':history[-1]['receipt'] if history else 'GENESIS','at':time.time()}
    raw=canonical(e);digest=sha('orchestration-event-v1:'+raw)
    s.db.execute('INSERT INTO orchestration_events VALUES(?,?,?,?)',(nid,e['seq'],raw,digest))
    s._audit('ORCHESTRATION_TRANSITION',{'node_id':nid,'receipt':digest,'kind':kind,'state':target,'kernel':k})
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
    n=node(s,nid);used={k:0 for k in n['limits']}
    if n['kind']=='task':
        from review_budget import usage
        old=usage(s,n['session'])
        if old:
            used.update(old['used'])
            if s.db.execute("SELECT 1 FROM sqlite_master WHERE name='host_actions'").fetchone():
                used['actions']=s.db.execute("SELECT COUNT(*) FROM host_actions WHERE session=? AND json_extract(record_json,'$.contract_hash')=?",(n['session'],n['binding']['contract_hash'])).fetchone()[0]
            for child in descendants(s,nid):
                for raw,settled in s.db.execute("SELECT c.cost_json,r.record_json FROM orchestration_charges c LEFT JOIN orchestration_settlements r ON r.charge_id=c.id WHERE c.node_id=? AND c.kind IN ('edit','run_tests')",(child,)):
                    used['seconds']+=(json.loads(settled)['used'] if settled else json.loads(raw)).get('seconds',0)
            return used
    for child in descendants(s,nid):
        for raw,settlement,digest in s.db.execute('SELECT c.cost_json,r.record_json,r.digest FROM orchestration_charges c LEFT JOIN orchestration_settlements r ON r.charge_id=c.id WHERE c.node_id=?',(child,)):
            if settlement and sha('orchestration-settlement-v1:'+settlement)!=digest:raise InvalidUpdate('Corrupt orchestration settlement')
            cost=json.loads(settlement)['used'] if settlement else json.loads(raw)
            for k,v in cost.items():used[k]+=v
    return used

def gates(s,nid,cost):
    n=checked(s,nid);frames=[];excluded=None
    while n:
        used=spent(s,n['id']);reserved={k:0 for k in n['limits']}
        for child, in s.db.execute('SELECT id FROM orchestration_nodes WHERE parent_id=?',(n['id'],)):
            if child==excluded or state(s,child) in TERMINAL:continue
            c=node(s,child);actual=spent(s,child)
            for key in reserved:reserved[key]+=max(0,c['limits'][key]-actual[key])
        effective={k:used[k]+reserved[k] for k in used}
        k=KarmaAdapter().allocations(effective,effective_limits(s,n),cost)
        frames.append(dict(k,node_id=n['id'],consumed=used,reserved_for_other_children=reserved))
        excluded=n['id'];n=checked(s,n['parent_id']) if n['parent_id'] else None
    return frames

def effective_limits(s,n):
    if n['kind'] != 'task': return n['limits']
    from review_budget import usage
    budget = usage(s,n['session'])
    return dict(n['limits'], **budget['limits']) if budget else n['limits']

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
                context=child_context,context_hash=sha(canonical(child_context)),files=p['files'],roles=p.get('roles',['subagent'])))
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
    frames=gates(s,n['id'],cost);allowed(frames)
    if resource and resource.get('decision') and resource['decision']!='ALLOW':raise OrchestrationStop([resource])
    cid=uuid.uuid4().hex
    s.db.execute('INSERT INTO orchestration_charges VALUES(?,?,?,?,?)',(cid,n['id'],kind,resource_id,canonical(cost)))
    transition(s,n['id'],'RUNNING','DISPATCH',{'charge_id':cid,'resource_id':resource_id,'kind':kind,'role':role,'model':model,'frames':frames,'resource':resource},frames[0])
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
    r={'charge_id':owner['charge_id'],'receipt':receipt,'used':cost};raw=canonical(r)
    s.db.execute('INSERT INTO orchestration_settlements VALUES(?,?,?)',(owner['charge_id'],raw,sha('orchestration-settlement-v1:'+raw)))
    pending=s.db.execute('SELECT COUNT(*) FROM orchestration_charges c LEFT JOIN orchestration_settlements r ON r.charge_id=c.id WHERE c.node_id=? AND r.charge_id IS NULL',(owner['node_id'],)).fetchone()[0]
    if state(s,owner['node_id'])=='RUNNING':transition(s,owner['node_id'],'RUNNING' if pending else 'READY','SETTLED',r)

def complete(s,nid,result_id):
    with s.db:
        n=checked(s,nid)
        s.load_result(n['session'],result_id)
        row=s.db.execute('SELECT record_json FROM invocations WHERE id=?',(result_id,)).fetchone()
        env=json.loads(row[0])
        if n['kind']!='branch' or env['role']!='subagent' or env.get('orchestration',{}).get('node_id')!=nid:raise InvalidUpdate('Result does not belong to this child')
        transition(s,nid,'DONE','RESULT',{'invocation_id':result_id})

def join(s,nid):
    with s.db:
        parent=checked(s,nid);results=[]
        forks=[e for e in events(s,nid) if e['kind']=='FORK']
        if state(s,nid)!='WAITING_CHILDREN' or not forks:raise InvalidUpdate('Parent is not awaiting a join')
        for cid in forks[-1]['data']['children']:
            if state(s,cid)!='DONE':raise InvalidUpdate('Child results are not ready to join')
            c=node(s,cid)
            if c['binding']!=binding(s,parent['session']):raise InvalidUpdate('Child context became stale')
            iid=events(s,cid)[-1]['data']['invocation_id'];s.load_result(parent['session'],iid);results.append(iid)
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
        for cost,settled in db.execute('SELECT c.cost_json,r.record_json FROM orchestration_charges c LEFT JOIN orchestration_settlements r ON c.id=r.charge_id WHERE c.node_id=?',(nid,)):
            for k,v in (json.loads(settled)['used'] if settled else json.loads(cost)).items():used[k]=used.get(k,0)+v
        result.append({'id':nid,'parent':n['parent_id'],'kind':n['kind'],'state':e.get('to',n['initial_state']),
                       'objective':n.get('objective',n.get('goal','')),'kernel':last_dispatch.get('kernel'),'limits':n.get('limits'),
                       'permissions':n.get('permissions'),'context_hash':n.get('context_hash'),'last_event':e.get('kind'),
                       'resource_fallback':resource_fallback,
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
