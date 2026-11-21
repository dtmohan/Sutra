"""One governed architecture step with at most one repository operation."""
import json,uuid
from context_manager import InvalidUpdate,ContextBudgetExceeded,canonical,sha
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
   payload=json.loads(s.db.execute('SELECT payload_json FROM deliveries WHERE id=?',(env['delivery_id'],)).fetchone()[0])
   from retained_context import requests as context_requests
   if proposal.get('tool',{}).get('kind')=='report' and not context_requests(payload, proposal):
    report=role.markdown(proposal,payload['evidence'],routing)
  except (ValueError,InvalidUpdate) as exc:warning='Architecture checkpoint withheld: '+str(exc);checkpoint=None
  except PageFault as exc:warning='Architecture checkpoint withheld: '+str(exc);checkpoint=None
 last=s.db.execute("""SELECT record_json FROM protocol_events WHERE run_id=(
   SELECT id FROM protocol_runs WHERE session=? ORDER BY rowid DESC LIMIT 1)
   ORDER BY seq DESC LIMIT 1""",(session,)).fetchone()
 stop=json.loads(last[0])['data'].get('architecture_stop') if last else None
 return {'session':session,'workspace':str(workspace),'model':meta.get('model','local-assistant:latest'),
  'contract':s.current_contract(session),'checkpoint':checkpoint,'last_result':proposal if checkpoint else None,'sources':[],
  'api':__import__('api_runtime').state(s,meta,workspace),'static':__import__('static_runtime').state(s,meta,workspace),'warning':warning,'ledger_valid':s.verify_invocations(session,complete=False),'architecture':summary,'architecture_stop':stop,'routing':routing if checkpoint else None,'report':report if checkpoint else None}

def packet_evidence(s,session,available,phase):
 pins={}
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

def recovery_prompt(s,session,stop,expected_run):
 if not stop or stop['terminal'] or stop['reason']!='insufficient_evidence':
  raise InvalidUpdate('This review is not waiting for another source read.')
 if expected_run!=stop['run_id']:
  raise InvalidUpdate('The saved review changed. Reopen its current details before retrying.')
 run=s.run(stop['run_id'])
 if run['session']!=session or run['config']['contract_hash']!=s.origin(session)['contract_hash']:
  raise InvalidUpdate('Saved architecture request belongs to another task or contract.')
 data=run['events'][-1]['data']
 if 'request' not in data:return s.pinned(session)['goal']
 prompt=data['request']
 if run['config']['request_hash']!=sha(canonical([prompt,[],None])):
  raise InvalidUpdate('Saved architecture request failed verification.')
 return prompt

def recovery_target(s,session,names,available):
 # Reuse valid receipts. An empty excerpt cannot support a citation, and
 # another read of the same current version would return the same excerpt.
 inspected={e['path'] for e in available.values()}
 options=[name for name in names if name not in inspected]
 if not options:return None
 prior=repo.current_records(s,session)
 return min(options,key=lambda name:sum(row['operation']['kind']=='read'
            and row['operation']['target']==name for row in prior))

def fit_packet(payload,budget,s=None,session=None):
 # Preserve the contract and minimum source coverage before model-authored notes.
 if s is not None:
  from chat_runtime import expand_retained_context
  expand_retained_context(s, session, payload, budget)
 while True:
  try:
   if payload.get('retained_conversation_version') == 1:
    from retained_context import conversation_view
    payload['memory_budget'] = vars(budget).copy()
    payload['memory'] = conversation_view(s, payload, budget, system=role.SYSTEM, schema_for=role.schema)
   schema=role.schema(payload)
   budget.check(role.SYSTEM,dict(payload,repair={'errors':['x'*140,'x'*140]}),schema)
   return schema
  except ContextBudgetExceeded:
   if payload.get('retained_context_version') == 1 and s is not None:
    from chat_runtime import trim_retained_context
    if trim_retained_context(s, session, payload):
     from retained_context import source_units
     payload['evidence'] = source_units(payload['retained_context'])
     continue
   handoff=payload.get('handoff')
   if handoff:
    level=payload.get('handoff_level',0)+1
    payload['handoff']=role.reduce_handoff(handoff,level);payload['handoff_level']=level
    payload['handoff_window_limited']=True
    continue
   # A bound view only changes through the resolver. Do not silently slice its
   # source bodies into a second, inconsistent memory window.
   if payload.get('retained_context_version') == 1:
    raise ContextBudgetExceeded('Contract and retained-memory directory cannot fit this allocation; original context remains saved.')
   items=payload['evidence']
   if not items:raise
   longest=max(items,key=lambda e:len(e['text']))
   if len(longest['text'])>240:longest['text']=longest['text'][:240]
   elif len({e['path'] for e in items})>min(2,payload['repository']['total_files']):items.pop(0)
   else:raise ContextBudgetExceeded('Contract and minimum source evidence cannot fit; shorten the request or narrow the review scope.')
   payload['evidence_window_limited']=True

def dispatch(s,meta,workspace,root,request,provider=None):
 st=state(s,meta,workspace);session=st['session'];repo.settings(s,session)
 mode=request.get('mode','explore')
 if mode not in ('explore','report','guided','recover_evidence'):raise InvalidUpdate('Unknown architecture mode')
 previous_stop=st['architecture_stop'] if mode=='recover_evidence' else None
 prompt=recovery_prompt(s,session,previous_stop,request.get('run_id')) if mode=='recover_evidence' else request.get('prompt','')
 if not isinstance(prompt,str) or not 1<=len(prompt.strip())<=1800:raise InvalidUpdate('Architecture request must be 1..1800 characters')
 prompt=prompt.strip()
 model=request.get('model') or meta.get('model','local-assistant:latest')
 names=st['architecture']['catalog'];parent=st['checkpoint'];real=provider is None
 # Commit the attempt before evidence or capacity preflight. A stopped
 # preflight has a receipt and consumes no model or repository allocation.
 run=s.begin_run(session,prompt,[],None,max_calls=3,max_repairs=2)
 tools=repo.usage(s,session)
 available=repo.evidence(s,session,workspace)
 minimum=min(2,len(names))
 covered=len({e['path'] for e in available.values() if e['text'].strip()})
 phase='explore' if mode in ('explore','guided') else 'report'
 if tools['decision']!='ALLOW' or (mode=='guided' and covered>=minimum):phase='report'
 tool=None;no_target=False
 if mode=='recover_evidence' and covered<minimum and tools['decision']=='ALLOW':
  target=recovery_target(s,session,names,available)
  no_target=target is None
  if target:
   try:
    tool=repo.execute(s,session,workspace,root,'read',target=target,request_id=run,resource_check=real)
   except ResourceWait as exc:
    s.event(run,'RESOURCE_WAIT',{'reason':str(exc),'measurement':exc.state,'architecture_stop':previous_stop})
    raise
   except InvalidUpdate as exc:
    s.event(run,'FIN',{'reason':'architecture_read_preflight','error':str(exc),'architecture_stop':previous_stop})
    raise
   tools=repo.usage(s,session)
   available=repo.evidence(s,session,workspace)
   st=state(s,meta,workspace)
   parent=st['checkpoint']
 evidence=packet_evidence(s,session,available,phase)
 covered=len({e['path'] for e in evidence if e['text'].strip()})
 if not names or phase=='report' and covered<minimum:
  terminal=not names or no_target or tools['decision']!='ALLOW'
  if not names:
   reason='empty_catalog'
   error='This review has no eligible source files. Start a new architecture review with eligible files.'
   omitted=st['architecture']['excluded']
   if omitted:
    examples=', '.join(f'{name} ({cause})' for name,cause in sorted(omitted.items())[:3])
    error+=f' The catalog excluded {len(omitted)} files or directories, including {examples}.'
  else:
   reason='insufficient_evidence'
   error=f'The architecture report needs current excerpts from {minimum} distinct catalog files. Sutra has {covered}.'
   if tool and tool['result']['status']!='ok':
    error+=f" {tool['operation']['target']} could not be read. {tool['result']['error']}"
   if tools['decision']!='ALLOW':
    reason='evidence_budget_exhausted'
    error+=' The repository-operation budget is exhausted. Start a new architecture review to inspect more source.'
   elif no_target:
    reason='no_usable_evidence'
    error+=' The remaining files have no usable excerpts. Start a review with source files that contain code or documentation.'
   else:error+=' Choose Read another file and retry to inspect one more file in scope.'
  stop={'reason':reason,'terminal':terminal,'run_id':run,'message':error,
        'phase':phase,'covered_files':covered,'required_files':minimum,'repository_budget':tools}
  s.event(run,'FIN',{'reason':reason,'architecture_stop':stop,'request':prompt})
  return {'result':{'admission':{'admitted':False,'errors':[error],'semantic_correctness_verified':False},
                    'accepted_output':None,'run_id':run,'model':model,
                    'review_stage':'architecture_stopped' if terminal else 'architecture_evidence'},
          'tool_receipt':tool,'routing':None,'state':dict(st,architecture_stop=stop)}
 payload={'phase':phase,'contract':s.pinned(session),'request':prompt,'workspace':str(workspace),'pages':[],
  'derived_from':[parent] if parent else [],'handoff':compact(st['last_result']) if parent else None,
  'repository':{'catalog':names[:20],'total_files':len(names),'budget':tools,
                'operations_remaining':max(0,tools['limit']-tools['used']),
                'scope':'committed catalog; source bodies require read/search receipts','last_tool':repo.feedback(s,session)},
  'evidence':evidence}
 try:
  if request.get('conversation_id'):
   from conversation import workspace_handoff
   payload['conversation_handoff'] = workspace_handoff(s, session, request['conversation_id'], prompt)
   payload['retained_conversation_version'] = 1
  import orchestration as machine
  import retained_context
  owner = machine.ACTIVE.get() or machine.task(s, session)['id']
  prior_refs = request.get('context_refs', [])
  if not isinstance(prior_refs, list):raise InvalidUpdate('Context references must be a list')
  retained_context.bind(s, session, payload, owner=owner, limit=retained_context.MAX_LIMIT, required_refs=prior_refs)
  payload['retained_context_owner'] = owner
  if phase == 'report':
   # A report cannot make progress from a directory alone. Preserve the same
   # minimum distinct-file evidence required by the authoritative validator.
   chosen = {}
   for page in payload['retained_context']['pages']:
    if page['kind'] == 'retained_source_unit':
     chosen.setdefault(page['evidence']['path'], page['ref'])
   if len(chosen) < minimum:
    raise ContextBudgetExceeded('Minimum whole source evidence does not fit the retained-memory window; original receipts remain saved.')
   retained_context.refit(s, session, payload, payload['retained_context']['limit_bytes'],
                          required_refs=list(chosen.values())[:minimum])
  payload['evidence'] = retained_context.source_units(payload['retained_context'])
  if real:
   from api_runtime import provider_for
   from review_context import context_budget
   provider=provider_for(root,model,review=True)
   context_budget(provider,s,session,slots=3)
   bind(provider,payload)
  if request.get('context_recall'):
   from chat_runtime import expand_retained_context
   expand_retained_context(s, session, payload, provider.budget)
   retained_context.apply_recall(s, session, payload, request['context_recall'])
  schema=fit_packet(payload,provider.budget,s,session)
 except ResourceWait as exc:
  s.event(run,'RESOURCE_WAIT',{'reason':str(exc),'measurement':exc.state,'architecture_stop':previous_stop})
  raise
 except (InvalidUpdate,ContextBudgetExceeded,PageFault) as exc:
  s.event(run,'FIN',{'reason':'architecture_preflight','error':str(exc)})
  raise
 meta['model']=model;last=None
 output_repaired=False
 for attempt in range(3):
  s.reserve(run,'ATTEMPT',{'attempt':attempt})
  if real:
   try:
    from provider_resources import require_resources as provider_resources
    hardware=provider_resources(root,provider=provider)
   except ResourceWait as exc:
    s.event(run,'RESOURCE_WAIT',{'reason':str(exc),'measurement':exc.state,'architecture_stop':previous_stop});raise
   with s.db:s._audit('RESOURCE_GATE',{'run_id':run,'measurement':hardware})
  charge=provider.budget.check(role.SYSTEM,payload,schema);delivery=s.prepare_delivery(session,payload,charge)
  last=InvocationGateway(s.path, run, provider, ledger=s).invoke('architect',uuid.uuid4().hex,role.SYSTEM,payload,schema,delivery)
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
 if last['admission']['admitted']:
  requested = retained_context.requests(payload, last['accepted_output'])
  if requested:
   retained_context.validate_requests(s,session,payload,requested)
   last['retained_context_pending'] = {'invocation_id': last['invocation_id'], 'refs': requested}
  t=last['accepted_output']['tool']
  if t['kind'] in ('read','search','list'):
   tool=repo.execute(s,session,workspace,root,t['kind'],target=t['target'],query=t['query'],line=t['line'],cursor=t.get('cursor'),
    invocation_id=last['invocation_id'],request_id=last['invocation_id'],resource_check=real)
 return {'result':last,'tool_receipt':tool,'routing':route(s,session,last['invocation_id'],model),'state':state(s,meta,workspace)}
