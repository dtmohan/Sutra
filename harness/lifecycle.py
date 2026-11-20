"""Bridge-owned deadlines and append-only reconciliation under the workspace lock."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path
import signal
import time
import threading
from context_manager import InvalidUpdate, canonical, sha

_deadline = ContextVar('karma_deadline', default=None)

class Interrupted(BaseException):
    pass

def remaining():
    bound = _deadline.get()
    return None if bound is None else max(0, bound-time.monotonic())

def hard_deadline_supported():
    return hasattr(signal, 'SIGALRM') and threading.current_thread() is threading.main_thread()

@contextmanager
def call_deadline(seconds):
    """Bound a new timed call while restoring the enclosing bridge alarm."""
    if not hard_deadline_supported():
        raise InvalidUpdate('This provider cannot enforce the selected call deadline')
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    outer_first = previous_timer[0] > 0 and previous_timer[0] <= seconds
    duration = min(seconds, previous_timer[0]) if outer_first else seconds
    def expired(signum, frame):
        if outer_first and callable(previous_handler): previous_handler(signum, frame)
        raise TimeoutError('Model transport reached its admitted deadline')
    try:
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, duration)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, max(.000001, previous_timer[0]-(time.monotonic()-started)), previous_timer[1])

def timeout(cap):
    deadline = _deadline.get()
    remaining = cap if deadline is None else min(cap, deadline-time.monotonic())
    if remaining <= 0: raise Interrupted('Bridge operation deadline reached')
    return remaining

@contextmanager
def deadline(seconds=450):
    token = _deadline.set(time.monotonic()+seconds)
    old = {}
    def stop(signum, frame): raise Interrupted('Bridge cancelled or deadline reached')
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            old[sig] = signal.signal(sig, stop)
        if hasattr(signal, 'SIGALRM'):
            old[signal.SIGALRM] = signal.signal(signal.SIGALRM, stop)
            signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        if hasattr(signal, 'SIGALRM'):signal.setitimer(signal.ITIMER_REAL, 0)
        for sig, handler in old.items():signal.signal(sig, handler)
        _deadline.reset(token)

def reconcile(s, workspace):
    """Caller MUST own bridge.lock. Never replay work or refund a reservation."""
    import repository_tools as repo
    import host_actions as actions
    repo.initialize(s);actions.initialize(s)
    events=[]
    from capabilities import recover as recover_capabilities
    events.extend({'kind':'capability','id':r['operation_id'],'status':'interrupted'} for r in recover_capabilities(s))
    pending=s.db.execute('SELECT o.record_json,o.digest FROM repository_ops o LEFT JOIN repository_receipts r ON r.operation_id=o.id WHERE r.id IS NULL ORDER BY o.rowid').fetchall()
    for raw,digest in pending:
        if sha('repository-op-v1:'+raw)!=digest:raise InvalidUpdate('Interrupted repository commitment corrupted')
        op=json.loads(raw)
        result={'status':'interrupted','error':'Prior bridge ended without a receipt; read-only operation was not replayed','excerpts':[]}
        repo.finish(s,op,digest,result,op.get('task_cost',{}).get('seconds',20))
        events.append({'kind':'repository','id':op['id'],'status':'interrupted'})
    pending=s.db.execute('SELECT a.record_json,a.digest FROM host_actions a LEFT JOIN host_action_receipts r ON r.action_id=a.id WHERE r.id IS NULL ORDER BY a.rowid').fetchall()
    for raw,digest in pending:
        if sha('action-v1:'+raw)!=digest:raise InvalidUpdate('Interrupted action commitment corrupted')
        action=json.loads(raw);result={'status':'interrupted','assertions_verified':False,'replayed':False}
        if action['kind']=='edit':
            try:
                data=repo.read_bytes(workspace,action['target'])
                observed=hashlib.sha256(data).hexdigest();result['observed_hash']=observed
                result['status']='reconciled_after' if observed==action['proposed_after_hash'] else 'reconciled_before' if observed==action['before_hash'] else 'interrupted_unknown'
            except (OSError,ValueError,InvalidUpdate):result['status']='interrupted_unknown'
            result['interpretation']='Byte comparison only; no assertion about who changed the file'
        actions.receipt(s,action,result)
        if result['status']=='interrupted_unknown':
            s.quarantine_invocation(action['session'],action['invocation_id'],'Interrupted edit has unknown outcome; use fresh source for a new proposal')
        events.append({'kind':'action','id':action['id'],'status':result['status']})
    pending=s.db.execute('SELECT i.record_json,i.digest,d.payload_json FROM invocations i LEFT JOIN invocation_receipts r ON r.invocation_id=i.id JOIN deliveries d ON d.id=i.delivery_id WHERE r.id IS NULL ORDER BY i.rowid').fetchall()
    for raw,digest,payload in pending:
        if sha('invocation-v1:'+raw)!=digest:raise InvalidUpdate('Interrupted invocation commitment corrupted')
        env=json.loads(raw)
        from invocation_timing import reserved_seconds
        s.finalize(env,json.loads(payload),None,'Prior bridge ended without a model receipt; result withheld',elapsed_seconds=reserved_seconds(env))
        events.append({'kind':'invocation','id':env['id'],'status':'quarantined'})
    for rid, in s.db.execute('SELECT id FROM protocol_runs').fetchall():
        if not any(e['kind'] in ('ALLOW','FIN','RESOURCE_WAIT') for e in s.run(rid)['events']):
            s.event(rid,'FIN',{'reason':'Prior bridge ended before run settlement','interrupted':True})
            events.append({'kind':'run','id':rid,'status':'interrupted'})
    if events:
        with s.db:s._audit('INTERRUPTION_RECONCILED',{'events':events,'reservation_refunds':False})
    return events
