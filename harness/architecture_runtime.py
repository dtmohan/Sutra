"""One governed model step, followed by at most one governed repository tool."""
import json,uuid
from pathlib import Path
from context_manager import InvalidUpdate,canonical,ContextBudgetExceeded
from invocation_gateway import InvocationGateway
from local_model import LocalModel
from review_role import compact,findings
from monitoring import require_resources
import repository_tools as repo
import architecture_role as role

def route(s,session,iid,model):
 receipt=s.db.execute('SELECT id FROM invocation_receipts WHERE session=? AND invocation_id=?',(session,iid)).fetchone()
 return {'via':'Karma InvocationGateway','session':session,'model':model,'invocation_id':iid,'receipt_id':receipt[0] if receipt else None,'contract_hash':s.origin(session)['contract_hash']}

def state(s,meta,workspace):
 session=meta['active'];summary=repo.status(s,session);checkpoint=meta.get('checkpoint');proposal=None;warning=None;report=None;routing=None
 if checkpoint:
  try:
   proposal=s.load_result(session,checkpoint)
   env=json.loads(s.db.execute('SELECT record_json FROM invocations WHERE id=?',(checkpoint,)).fetchone()[0])
   routing=route(s,session,checkpoint,env['model'])
   if proposal.get('tool',{}).get('kind')=='report':
    payload=json.loads(s.db.execute('SELECT payload_json FROM deliveries WHERE id=?',(env['delivery_id'],)).fetchone()[0]);report=role.markdown(proposal,payload['evidence'],routing)
  except (ValueError,InvalidUpdate) as exc:warning='Architecture checkpoint withheld: '+str(exc);checkpoint=None
  except Exception as exc:
   from paging import PageFault
   if not isinstance(exc,PageFault):raise
   warning='Architecture checkpoint withheld: '+str(exc);checkpoint=None
 return {'session':session,'workspace':str(workspace),'model':meta.get('model','local-assistant:latest'),
  'contract':s.current_contract(session),'checkpoint':checkpoint,'last_result':proposal if checkpoint else None,'sources':[],
  'api':__import__('api_runtime').state(s,meta,workspace),'static':__import__('static_runtime').state(s,meta,workspace),'warning':warning,'ledger_valid':s.verify_invocations(session,complete=False),'architecture':summary,'routing':routing if checkpoint else None,'report':report if checkpoint else None}

def packet_evidence(s,session,workspace,prior):
 available=repo.evidence(s,session,workspace);pins={}
 # Select source quotations, not conversation messages or inherited model prose.
 # Older current quotations survive omissions in the latest model's summary.
 for iid,raw in s.db.execute('SELECT invocation_id,record_json FROM invocation_receipts WHERE session=? ORDER BY seq',(session,)):
  receipt=json.loads(raw)
  if receipt['role']!='architect' or receipt['status']!='accepted':continue
  try:s.load_result(session,iid)
  except (InvalidUpdate,ValueError):continue
  except Exception as exc:
   from paging import PageFault
   if isinstance(exc,PageFault):continue
   raise
  for f in findings(receipt['proposal']):
   e=available.get(f['page_id'])
   if e and f['quote'] and f['quote'] in e['text']:
    old=pins.get(e['id']);quote=f['quote']
    if old:
     start=min(e['text'].index(old['text']),e['text'].index(quote));end=max(e['text'].index(old['text'])+len(old['text']),e['text'].index(quote)+len(quote));quote=e['text'][start:end]
    pins[e['id']]=dict(e,text=quote)
 newest=list(available.values())[-1:]  # latest tool excerpt, plus retained quotes
 chosen=[e for e in pins.values() if not any(n['path']==e['path'] and e['text'] in n['text'] for n in newest)][-3:]+newest
 for e in chosen:e['text']=e['text'].encode()[:700].decode('utf-8','ignore')
 return chosen

def fit_packet(payload,budget):
 # Preserve the contract and minimum source coverage before model-authored notes.
 while True:
  schema=role.schema(payload)
  try:
   budget.check(role.SYSTEM,dict(payload,repair={'errors':['x'*140,'x'*140]}),schema)
   return schema
  except ContextBudgetExceeded:
   handoff=payload.get('handoff')
   if handoff:
    level=payload.get('handoff_level',0)+1
    payload['handoff']=role.reduce_handoff(handoff,level);payload['handoff_level']=level
    payload['handoff_window_limited']=True
    continue
   items=payload['evidence']
   if not items:raise
   longest=max(items,key=lambda e:len(e['text']))
   if len(longest['text'])>240:longest['text']=longest['text'][:240]
   elif len({e['path'] for e in items})>min(2,payload['repository']['total_files']):items.pop(0)
   else:raise ContextBudgetExceeded('Contract and minimum source evidence cannot fit; shorten the request or narrow the review scope.')
   payload['evidence_window_limited']=True

def dispatch(s,meta,workspace,root,request,provider=None):
 st=state(s,meta,workspace);session=st['session'];repo.settings(s,session)
 prompt=request.get('prompt','').strip()
 if not prompt or len(prompt)>1800:raise InvalidUpdate('Architecture request must be 1..1800 characters')
 model=request.get('model') or meta.get('model','local-assistant:latest')
 real=provider is None
 if real:
  from vscode_bridge import ollama
  require_resources(root)
  if model not in {m['name'] for m in ollama('/api/tags')['models']} or 'cloud' in model.lower():raise InvalidUpdate('Choose an installed local model')
  info=ollama('/api/show',{'model':model})
  if info.get('remote_model') or info.get('remote_host'):raise InvalidUpdate('Remote model outside local adapter')
  provider=LocalModel(root/'calls',11434,model)
 names=repo.catalog(s,session);parent=st['checkpoint']
 phase=request.get('mode','explore')
 if phase not in ('explore','report'):raise InvalidUpdate('Unknown architecture mode')
 if repo.usage(s,session)['decision']!='ALLOW':phase='report'
 payload={'phase':phase,'contract':s.pinned(session),'request':prompt,'workspace':str(workspace),'pages':[],
  'derived_from':[parent] if parent else [],'handoff':compact(st['last_result']) if parent else None,
  'repository':{'catalog':names[:20],'total_files':len(names),'budget':repo.usage(s,session),'scope':'committed catalog; source bodies require read/search receipts','last_tool':repo.feedback(s,session)},
  'evidence':packet_evidence(s,session,workspace,st['last_result'])}
 schema=fit_packet(payload,provider.budget)
 run=s.begin_run(session,prompt,[],None,max_calls=3,max_repairs=2);meta['model']=model;last=None
 for attempt in range(3):
  s.reserve(run,'ATTEMPT',{'attempt':attempt})
  if real:
   try:hardware=require_resources(root)
   except InvalidUpdate as exc:
    s.event(run,'RESOURCE_WAIT',{'reason':str(exc)});raise
   with s.db:s._audit('RESOURCE_GATE',{'run_id':run,'measurement':hardware})
  charge=provider.budget.check(role.SYSTEM,payload,schema);delivery=s.prepare_delivery(session,payload,charge)
  last=InvocationGateway(s.path,run,provider).invoke('architect',uuid.uuid4().hex,role.SYSTEM,payload,schema,delivery)
  if last['admission']['admitted']:
   s.event(run,'ALLOW',{'reason':'validated_architecture','invocation_id':last['invocation_id']});meta['checkpoint']=last['invocation_id'];break
  s.event(run,'NACK',{'reason':'invalid_architecture','errors':last['admission']['errors']})
  if attempt==2 or last.get('blocked'):s.event(run,'FIN',{'reason':'repair_exhausted_or_transport'});break
  s.event(run,'BRIDGE',{'reason':'bounded_repair','attempt':attempt+1});payload['repair']={'errors':[e[:140] for e in last['admission']['errors'][:2]]}
 last.pop('record',None);last.pop('model_proposal',None);last.update(model=model,run_id=run)
 tool=None
 if last['admission']['admitted']:
  t=last['accepted_output']['tool']
  if t['kind'] in ('read','search','list'):
   tool=repo.execute(s,session,workspace,root,t['kind'],target=t['target'],query=t['query'],line=t['line'],
    invocation_id=last['invocation_id'],request_id=last['invocation_id'],resource_check=real)
 return {'result':last,'tool_receipt':tool,'routing':route(s,session,last['invocation_id'],model),'state':state(s,meta,workspace)}
