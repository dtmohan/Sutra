import uuid,json
from pathlib import Path
from context_manager import InvalidUpdate
from invocation_gateway import InvocationGateway
from monitoring import require_resources
from paging import Budget
from local_model import LocalModel
import repository_tools as repo
import static_analysis as analysis
import static_role as role

def build(s,meta,workspace,root,files,real=True):
 # New operation and compiler Program on every ask, even for unchanged source.
 row=repo.execute(s,meta['active'],workspace,root,'static_map',target='\n'.join(files),resource_check=real)
 if row['result']['status']!='ok':raise InvalidUpdate(row['result'].get('error','Static analysis failed'))
 return row

def build_partition(s,meta,workspace,root,files,real=True):
 """Fresh pages inside one child allocation; never enlarge the source-page cap."""
 cap=s.snapshot(meta['active'])['constraints']['review_page_kib']*1024
 configs=[p for p in files if Path(p).name=='tsconfig.json']
 config_bytes=sum((Path(workspace)/p).stat().st_size for p in configs)
 pages=[];page=list(configs);size=config_bytes
 for name in files:
  if name in configs:continue
  length=(Path(workspace)/name).stat().st_size
  if length+config_bytes>cap:
   raise InvalidUpdate(f'Source page cannot fit {name}: {length+config_bytes} bytes including config, allocation {cap}')
  if size+length>cap:
   pages.append(page);page=list(configs);size=config_bytes
  page.append(name);size+=length
 if len(page)>len(configs):pages.append(page)
 receipts=[]
 while pages:
  current=pages.pop(0)
  try:row=build(s,meta,workspace,root,current,real=real)
  except InvalidUpdate as exc:
   # Only parser work-unit overflow is repairable by dividing this source page.
   code=[p for p in current if p not in configs]
   if len(code)<2 or not any(t in str(exc) for t in ('work-unit allocation exceeded','API index bound exceeded')):raise
   middle=len(code)//2
   pages[:0]=[configs+code[:middle],configs+code[middle:]]
   continue
  receipts.append(row['id'])
 return receipts

def load_partition(s,sid,workspace,receipts,owner):
 """Validate every page again; merging does not infer cross-page relationships."""
 if not isinstance(receipts,list) or not receipts or len(set(receipts))!=len(receipts):
  raise InvalidUpdate('Invalid child snapshot receipts')
 sources={};versions={};graph={'nodes':[],'calls':[],'profiles':[],'diagnostics':[],'diagnostic_count':0,'module_dependencies':[]}
 for rid in receipts:
  row=repo.record_by_id(s,sid,rid)
  if (row['operation'].get('orchestration') or {}).get('node_id')!=owner:
   raise InvalidUpdate('Child source page belongs to another orchestration node')
  data=analysis.load(s,sid,workspace,rid)
  for name,text in data['sources'].items():
   if name in sources and (Path(name).name!='tsconfig.json' or sources[name]!=text):
    raise InvalidUpdate('Duplicate or conflicting child source page')
   sources[name]=text;versions[name]=data['versions'][name]
  for key in ('nodes','calls','module_dependencies'):graph[key].extend(data['graph'].get(key,[]))
  graph['diagnostics'].extend(data['graph']['diagnostics'][:max(0,3-len(graph['diagnostics']))])
  graph['diagnostic_count']+=data['graph'].get('diagnostic_count',len(data['graph']['diagnostics']))
  graph['profiles']=sorted(set(graph['profiles']+data['graph']['profiles']))
 return {'sources':sources,'versions':versions,'graph':graph}

def explain(s,meta,workspace,root,receipt,model,sites=None,mode='compact',provider=None,handoff=None,progress=None):
 sid=meta['active'];result=analysis.load(s,sid,workspace,receipt)
 sites=sites or [c['id'] for c in result['graph']['calls'][:8]]
 payload={'_request_nonce':uuid.uuid4().hex,'contract':s.pinned(sid),'workspace':str(workspace),'pages':[],'snapshot_receipt':receipt,
  'request':'Explain the current source relationships and identify unresolved calls.','sites':sites,'facts':analysis.compact(result['graph'],sites),'source_mode':mode,'source_text':result['sources'] if mode=='full_source' else {}}
 if handoff:payload['handoff']=handoff
 if progress:payload['review_progress']=progress
 payload['constraint_echo']=role.constraint_rows(payload['contract'])
 real=provider is None
 if real:
  from api_runtime import provider_for
  provider=provider_for(root,model)
  if payload['contract']['constraints'].get('auto_review'):provider.keep_alive=0
  # Full-source comparison is isolated to this experimental role; normal paths
  # remain 8192. Both branches reserve identical output and check actual usage.
  from model_context import allocation
  capacity=allocation(provider.context_metadata)[0].context_limit
  provider.budget=Budget(context_limit=min(capacity,32768 if mode=='full_source' else 8192),response_reserve=1024 if payload['contract']['constraints'].get('auto_review') else 768)
 schema=role.schema(payload);provider.budget.check(role.SYSTEM,dict(payload,repair={'errors':['x'*120]*2}),schema)
 run=s.begin_run(sid,payload['request'],[],None,max_calls=3,max_repairs=2)
 for attempt in range(3):
  s.reserve(run,'ATTEMPT',{'attempt':attempt})
  if real:
   from provider_resources import require_resources as provider_resources
   h=provider_resources(root,provider=provider)
   with s.db:s._audit('RESOURCE_GATE',{'run_id':run,'measurement':h})
  charge=provider.budget.check(role.SYSTEM,payload,schema);delivery=s.prepare_delivery(sid,payload,charge)
  from review_budget import BudgetStop
  try:out=InvocationGateway(s.path, run, provider, ledger=s).invoke('static_explainer',uuid.uuid4().hex,role.SYSTEM,payload,schema,delivery)
  except BudgetStop as exc:
   s.event(run,'FIN',{'reason':'task_allocation_stopped_dispatch','kernel':exc.frame});raise
  if out['admission']['admitted']:
   s.event(run,'ALLOW',{'reason':'static_explanation_is_commentary','invocation_id':out['invocation_id']});break
  s.event(run,'NACK',{'reason':'static_explanation_rejected','errors':out['admission']['errors']})
  if attempt==2 or out.get('blocked'):s.event(run,'FIN',{'reason':'bounded_repair_exhausted'});break
  try:role.request_check(s,sid,payload)
  except InvalidUpdate as exc:
   s.event(run,'FIN',{'reason':'source_or_contract_changed','error':str(exc)});break
  s.event(run,'BRIDGE',{'reason':'bounded_protocol_repair'});payload['repair']={'errors':[e[:120] for e in out['admission']['errors'][:2]]}
 out['run_id']=run
 return out

def state(s,meta,workspace):
 current=meta.get('static_map')
 if not current:return {'status':'not_started','report':None}
 out=dict(current,report=None)
 try:
  data=analysis.load(s,meta['active'],workspace,current['receipt']);graph=data['graph']
  out.update(status='fresh',profiles=graph['profiles'],nodes=len(graph['nodes']),calls=len(graph['calls']),resolved=sum(c['target'] is not None for c in graph['calls']),diagnostics=graph.get('diagnostic_count',len(graph['diagnostics'])),versions=data['versions'],cache_used=False)
  out['report']=analysis.render(graph)+'\nTask: '+meta['active']+'\nContract: '+s.origin(meta['active'])['contract_hash']+'\nSnapshot receipt: '+current['receipt']+'\n\nSource versions:\n'+''.join('- '+p+': '+h+'\n' for p,h in data['versions'].items())+'\nExplanation coverage: '+str(len(current.get('covered_sites',[])))+'/'+str(len(graph['calls']))+' call sites in the supplied files.\n'
  if current.get('invocation_id'):
   proposal=s.load_result(meta['active'],current['invocation_id'])
   out['commentary']=proposal['summary']
   out['report']+='\n## Model commentary (not independently verified)\n\n'+proposal['summary']+'\n\nModel: '+current['model']+'\nInvocation: '+current['invocation_id']+'\n'
 except (InvalidUpdate,ValueError) as exc:out.update(status='withheld',error=str(exc),report=None)
 except Exception as exc:
  from paging import PageFault
  if not isinstance(exc,PageFault):raise
  out.update(status='withheld',error=str(exc),report=None)
 return out

def dispatch(s,meta,workspace,root,request,provider=None):
 old=meta.get('static_map',{});files=request.get('files',old.get('files',[]))
 if not isinstance(files,list) or not files or any(not isinstance(p,str) for p in files):raise InvalidUpdate('Select explicit source files first')
 # Always compile a new snapshot, including for explanation and model handoff.
 row=build(s,meta,workspace,root,files,real=provider is None);data=row['result'];graph=data['graph']
 same=old.get('versions')==data['versions']
 coverage=old.get('covered_sites',[]) if same else []
 prior=old.get('last_accepted') if same else None
 handoff=None
 if prior:
  try:
   previous=s.load_result(meta['active'],prior['invocation_id'])
   handoff={'invocation_id':prior['invocation_id'],'model':prior['model'],'summary_unverified':previous['summary']}
  except (ValueError,InvalidUpdate):pass
 offset=request.get('offset',old.get('next_offset',0) if same else 0)
 if offset is None:offset=0
 if type(offset) is not int or offset<0 or (graph['calls'] and offset>=len(graph['calls'])):raise InvalidUpdate('Call offset outside current source inventory')
 current={'receipt':row['id'],'files':files,'versions':data['versions'],'offset':offset,'count':0,'total':len(graph['calls']),'next_offset':offset if graph['calls'] else None,'covered_sites':coverage,'invocation_id':None,'model':None,'explanation_status':'not_requested','source_changed':bool(old and not same)}
 current['last_accepted']=prior if handoff else None
 meta['static_map']=current
 result=None
 if request['op']=='static_explain':
  if not graph['calls']:raise InvalidUpdate('No call sites to explain; the deterministic map is available')
  model=provider.model if provider is not None else request.get('model') or meta.get('model') or 'qwen3.6:35b'
  sites=[c['id'] for c in graph['calls'][offset:offset+4]]
  # Keep the entire contract. Reduce only source batch size if context is tight.
  from context_manager import ContextBudgetExceeded
  while sites:
   try:result=explain(s,meta,workspace,root,row['id'],model,sites=sites,provider=provider,handoff=handoff);break
   except ContextBudgetExceeded:sites=sites[:-1]
  if not sites:raise ContextBudgetExceeded('One relationship and the complete contract do not fit; narrow the task')
  result.pop('record',None);result.pop('model_proposal',None);result.update(model=model,review_stage='static_explainer')
  current.update(count=len(sites),model=model,explanation_status='accepted_commentary' if result['admission']['admitted'] else 'quarantined',next_offset=offset+len(sites) if offset+len(sites)<len(graph['calls']) else None)
  if result['admission']['admitted']:
   current['invocation_id']=result['invocation_id'];current['covered_sites']=sorted(set(coverage)|set(sites));meta['model']=model
   current['last_accepted']={'invocation_id':result['invocation_id'],'model':model}
  else:current['next_offset']=offset
 return {'result':result,'static':state(s,meta,workspace),'snapshot_receipt':row['id']}
