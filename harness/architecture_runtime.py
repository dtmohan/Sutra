"""One governed model step, followed by at most one governed repository tool."""
import json,uuid
from context_manager import InvalidUpdate,ContextBudgetExceeded
from invocation_gateway import InvocationGateway
from paging import PageFault
from review_role import compact,findings
from monitoring import ResourceWait,require_resources
from review_reasoning import bind
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
  except PageFault as exc:warning='Architecture checkpoint withheld: '+str(exc);checkpoint=None
 last=s.db.execute("""SELECT record_json FROM protocol_events WHERE run_id=(
   SELECT id FROM protocol_runs WHERE session=? ORDER BY rowid DESC LIMIT 1)
   ORDER BY seq DESC LIMIT 1""",(session,)).fetchone()
 stop=json.loads(last[0])['data'].get('architecture_stop') if last else None
 return {'session':session,'workspace':str(workspace),'model':meta.get('model','local-assistant:latest'),
  'contract':s.current_contract(session),'checkpoint':checkpoint,'last_result':proposal if checkpoint else None,'sources':[],
  'api':__import__('api_runtime').state(s,meta,workspace),'static':__import__('static_runtime').state(s,meta,workspace),'warning':warning,'ledger_valid':s.verify_invocations(session,complete=False),'architecture':summary,'architecture_stop':stop,'routing':routing if checkpoint else None,'report':report if checkpoint else None}

def packet_evidence(s,session,workspace,phase):
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
 if phase=='report':
  # Prior model citations are optional. Successful reads are already evidence.
  by_path={e['path']:e for e in chosen if e['text'].strip()}
  for e in reversed(list(available.values())):
   if len(by_path)>=2:break
   if e['text'].strip():by_path.setdefault(e['path'],e)
  chosen=list(by_path.values())
 return [dict(e,text=e['text'].encode()[:700].decode('utf-8','ignore')) for e in chosen]

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
 prompt=request.get('prompt','')
 if not isinstance(prompt,str) or not 1<=len(prompt.strip())<=1800:raise InvalidUpdate('Architecture request must be 1..1800 characters')
 prompt=prompt.strip()
 model=request.get('model') or meta.get('model','local-assistant:latest')
 names=repo.catalog(s,session);parent=st['checkpoint']
 phase=request.get('mode','explore')
 if phase not in ('explore','report'):raise InvalidUpdate('Unknown architecture mode')
 # Commit the attempt before evidence or capacity preflight. A stopped
 # preflight has a receipt and consumes no model or repository allocation.
 run=s.begin_run(session,prompt,[],None,max_calls=3,max_repairs=2)
 tools=repo.usage(s,session)
 if tools['decision']!='ALLOW':phase='report'
 evidence=packet_evidence(s,session,workspace,phase)
 minimum=min(2,len(names));covered=len({e['path'] for e in evidence})
 if not names or phase=='report' and covered<minimum:
  terminal=not names or tools['decision']!='ALLOW'
  if not names:
   reason='empty_catalog'
   error='This review has no eligible source files. Start a new architecture review with eligible files.'
  else:
   reason='insufficient_evidence'
   error=f'The architecture report needs current excerpts from {minimum} distinct catalog files. Sutra has {covered}.'
   if terminal:
    reason='evidence_budget_exhausted'
    error+=' The repository-operation budget is exhausted. Start a new architecture review to inspect more source.'
  stop={'reason':reason,'terminal':terminal,'run_id':run,'message':error,
        'phase':phase,'covered_files':covered,'required_files':minimum,'repository_budget':tools}
  s.event(run,'FIN',{'reason':reason,'architecture_stop':stop})
  return {'result':{'admission':{'admitted':False,'errors':[error],'semantic_correctness_verified':False},
                    'accepted_output':None,'run_id':run,'model':model,
                    'review_stage':'architecture_stopped' if terminal else 'architecture_evidence'},
          'tool_receipt':None,'routing':None,'state':dict(st,architecture_stop=stop)}
 payload={'phase':phase,'contract':s.pinned(session),'request':prompt,'workspace':str(workspace),'pages':[],
  'derived_from':[parent] if parent else [],'handoff':compact(st['last_result']) if parent else None,
  'repository':{'catalog':names[:20],'total_files':len(names),'budget':tools,
                'operations_remaining':max(0,tools['limit']-tools['used']),
                'scope':'committed catalog; source bodies require read/search receipts','last_tool':repo.feedback(s,session)},
  'evidence':evidence}
 real=provider is None
 try:
  if real:
   from api_runtime import provider_for
   from review_context import context_budget
   provider=provider_for(root,model,review=True)
   context_budget(provider,s,session,slots=3)
   bind(provider,payload)
  schema=fit_packet(payload,provider.budget)
 except ResourceWait as exc:
  s.event(run,'RESOURCE_WAIT',{'reason':str(exc),'measurement':exc.state})
  raise
 except (InvalidUpdate,ContextBudgetExceeded,PageFault) as exc:
  s.event(run,'FIN',{'reason':'architecture_preflight','error':str(exc)})
  raise
 meta['model']=model;last=None
 output_repaired=False
 for attempt in range(3):
  s.reserve(run,'ATTEMPT',{'attempt':attempt})
  if real:
   try:hardware=require_resources(root)
   except ResourceWait as exc:
    s.event(run,'RESOURCE_WAIT',{'reason':str(exc),'measurement':exc.state});raise
   with s.db:s._audit('RESOURCE_GATE',{'run_id':run,'measurement':hardware})
  charge=provider.budget.check(role.SYSTEM,payload,schema);delivery=s.prepare_delivery(session,payload,charge)
  last=InvocationGateway(s.path,run,provider).invoke('architect',uuid.uuid4().hex,role.SYSTEM,payload,schema,delivery)
  if last['admission']['admitted']:
   s.event(run,'ALLOW',{'reason':'validated_architecture','invocation_id':last['invocation_id']});meta['checkpoint']=last['invocation_id'];break
  s.event(run,'NACK',{'reason':'invalid_architecture','errors':last['admission']['errors']})
  length_retry=last.get('failure_kind')=='output_limit' and not output_repaired
  if attempt==2 or (last.get('blocked') and not length_retry):
   s.event(run,'FIN',{'reason':'repair_exhausted_or_transport'})
   break
  if length_retry:
   output_repaired=True
   bind(provider,payload,preference='off')
   payload['repair']={'errors':['The previous generation reached its output limit. Send compact, complete JSON with all required fields and evidence.']}
  else:
   payload['repair']={'errors':[e[:140] for e in last['admission']['errors'][:2]]}
  s.event(run,'BRIDGE',{'reason':'bounded_output_repair' if length_retry else 'bounded_repair','attempt':attempt+1})
 last.pop('record',None);last.pop('model_proposal',None);last.update(model=model,run_id=run)
 tool=None
 if last['admission']['admitted']:
  t=last['accepted_output']['tool']
  if t['kind'] in ('read','search','list'):
   tool=repo.execute(s,session,workspace,root,t['kind'],target=t['target'],query=t['query'],line=t['line'],cursor=t.get('cursor'),
    invocation_id=last['invocation_id'],request_id=last['invocation_id'],resource_check=real)
 return {'result':last,'tool_receipt':tool,'routing':route(s,session,last['invocation_id'],model),'state':state(s,meta,workspace)}
