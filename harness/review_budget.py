"""Task allocations measured by the adapter; decisions from the canonical kernel."""
import json
from context_manager import InvalidUpdate
from karma_adapter import KarmaAdapter

DEFAULTS={'max_tools':1000,'review_calls':40,'review_tokens':250000,'review_source_kib':65536,'review_seconds':1800,'review_page_kib':512}

def allocations(c):
 if not (c.get('auto_review') or c.get('native_task') or c.get('conversation_only')):return None
 for k in DEFAULTS:
  if type(c.get(k)) is not int or not 0<c[k]<=1000000:raise InvalidUpdate('Missing positive task allocation: '+k)
 return {'operations':c['max_tools'],'calls':c['review_calls'],'tokens':c['review_tokens'],'source_bytes':c['review_source_kib']*1024,'seconds':c['review_seconds']}

def usage(s,session):
 c=s.snapshot(session)['constraints'];limits=allocations(c)
 if not limits:return None
 used={k:0 for k in limits};digest=s.origin(session)['contract_hash']
 from native_access import carry_usage
 used.update(carry_usage(s,session,digest))
 from capabilities import usage as capability_usage
 for key,value in capability_usage(s,session,digest).items():used[key]+=value
 if c.get('conversation_only'):
  from document_review import READ_SECONDS, RESERVED
  count,total=s.db.execute(
   """SELECT COUNT(*), COALESCE(SUM(json_extract(payload_json,'$.bytes')),0)
      FROM page_audit WHERE event=? AND json_extract(payload_json,'$.session')=?""",(RESERVED,session)).fetchone()
  used['operations']+=count;used['source_bytes']+=total;used['seconds']+=count*READ_SECONDS
 if s.db.execute("SELECT 1 FROM sqlite_master WHERE name='repository_ops'").fetchone():
  for raw,elapsed in s.db.execute("SELECT o.record_json,json_extract(r.record_json,'$.elapsed_seconds') FROM repository_ops o LEFT JOIN repository_receipts r ON r.operation_id=o.id WHERE o.session=?",(session,)):
   op=json.loads(raw)
   if op['contract_hash']!=digest:continue
   used['operations']+=1;cost=op.get('task_cost',{});used['source_bytes']+=cost.get('source_bytes',0);used['seconds']+=elapsed if elapsed is not None else cost.get('seconds',20)
 for raw,receipt_raw,payload in s.db.execute("SELECT i.record_json,r.record_json,d.payload_json FROM invocations i LEFT JOIN invocation_receipts r ON r.invocation_id=i.id JOIN deliveries d ON d.id=i.delivery_id WHERE i.session=?",(session,)):
  inv=json.loads(raw)
  if inv['contract_hash']!=digest:continue
  if c.get('native_task'):
   used['operations']+=1;used['source_bytes']+=sum(len(p.get('text','').encode()) for p in json.loads(payload).get('pages',[]))
  from budget_accounting import used as settled_tokens
  receipt=json.loads(receipt_raw) if receipt_raw else {}
  from invocation_timing import reserved_seconds
  used['calls']+=1;used['tokens']+=settled_tokens(inv,receipt);used['seconds']+=receipt.get('elapsed_seconds',reserved_seconds(inv))
 from allocation_recovery import effective
 amended,version,_=effective(s,session,limits)
 return {'limits':amended,'used':used,'base_limits':limits,'allocation_version':version}

def decision(s,session,cost=None):
 data=usage(s,session)
 if data is None:return None
 try:return dict(KarmaAdapter().allocations(data['used'],data['limits'],cost),base_limits=data['base_limits'],allocation_version=data['allocation_version'])
 except ValueError as exc:raise InvalidUpdate(str(exc)) from None

class BudgetStop(InvalidUpdate):
 def __init__(self,frame):self.frame=frame;super().__init__('Task budget '+frame['decision']+': '+frame['limiting_dimension'])

def require(s,session,cost):
 frame=decision(s,session,cost)
 if frame and frame['decision']!='ALLOW':raise BudgetStop(frame)
 return frame
