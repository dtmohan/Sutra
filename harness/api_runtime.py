"""Persisted mapper -> small reviewer -> deterministic publication gate."""
import copy,json,time,uuid
from context_manager import InvalidUpdate,canonical,sha,ContextBudgetExceeded
from invocation_gateway import InvocationGateway
from local_model import LocalModel
from paging import Budget,PageFault
from monitoring import require_resources
import repository_tools as repo
import api_roles as roles
import api_facts as facts

def initialize(s):
 s.db.executescript('''CREATE TABLE IF NOT EXISTS api_reviews(id TEXT PRIMARY KEY,session TEXT,seq INTEGER,record_json TEXT,UNIQUE(session,seq));''')
 for op in ('UPDATE','DELETE'):s.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_api_reviews_{op} BEFORE {op} ON api_reviews BEGIN SELECT RAISE(ABORT,'immutable API review'); END")
 s.db.commit()

def provider_for(root,model,review=False,model_class=LocalModel):
 from vscode_bridge import ollama
 from runtime_memory import fresh_resources
 fresh_resources(root)
 tags=ollama('/api/tags')['models']
 if model not in {x['name'] for x in tags} or 'cloud' in model.lower():raise InvalidUpdate('Choose an installed local model')
 info=ollama('/api/show',{'model':model})
 if info.get('remote_model') or info.get('remote_host'):raise InvalidUpdate('Remote API models are not allowed')
 import os
 from model_activity import thinking_control
 control=thinking_control(info,os.environ.get('SUTRA_THINKING','adaptive'))
 provider=model_class(root/'calls',11434,model,budget=Budget(response_reserve=2048 if review else 1024),thinking=control)
 provider.context_metadata=info
 from decoder_schema import profile
 runtime_version=None
 if (info.get('details')or{}).get('runner')=='mlx':
  runtime_version=ollama('/api/version').get('version')
 provider.decoder_compatibility=profile(info,runtime_version)
 from runtime_memory import configure
 configure(provider,root,info,next(x.get('size') for x in tags if x['name']==model))
 return provider

def invoke(s,session,root,role,payload,provider,real):
 system=roles.MAP_SYSTEM if role=='api_mapper' else roles.REVIEW_SYSTEM;schema=roles.schema(role,payload)
 provider.budget.check(system,dict(payload,repair={'errors':['x'*140]*2}),schema)
 run=s.begin_run(session,payload['request'],[],None,max_calls=3,max_repairs=2)
 for attempt in range(3):
  s.reserve(run,'ATTEMPT',{'attempt':attempt})
  if real:
   h=require_resources(root)
   with s.db:s._audit('RESOURCE_GATE',{'run_id':run,'measurement':h})
  charge=provider.budget.check(system,payload,schema);delivery=s.prepare_delivery(session,payload,charge)
  result=InvocationGateway(s.path,run,provider).invoke(role,uuid.uuid4().hex,system,payload,schema,delivery)
  if result['admission']['admitted']:
   s.event(run,'ALLOW',{'reason':'provisional_api_output','invocation_id':result['invocation_id']});break
  s.event(run,'NACK',{'reason':'api_protocol_rejected','errors':result['admission']['errors']})
  if attempt==2 or result.get('blocked'):s.event(run,'FIN',{'reason':'bounded_api_repair_exhausted'});break
  s.event(run,'BRIDGE',{'reason':'bounded_api_repair','attempt':attempt+1});payload['repair']={'errors':[e[:140] for e in result['admission']['errors'][:2]]}
 result.pop('record',None);result.pop('model_proposal',None);result.update(model=provider.model,run_id=run,review_stage=role)
 return result

def build(s,meta,workspace,root,request,provider=None):
 session=meta['active'];repo.settings(s,session);files=request.get('files',[])
 if not isinstance(files,list) or not 1<=len(files)<=8 or len(set(files))!=len(files):raise InvalidUpdate('Select 1..8 API source files from the review catalog')
 real=provider is None
 if real:provider=provider_for(root,meta.get('model','local-assistant:latest'))
 ids=[]
 for name in files:
  repo.request_check(s,session,'api_index',name)
  cached=next((r for r in reversed(repo.current_records(s,session)) if r['operation']['kind']=='api_index' and r['operation']['target']==name and r['result']['status']=='ok' and r['result']['api']['sha256']==sha(repo.read_bytes(workspace,name).decode())),None)
  r=cached or repo.execute(s,session,workspace,root,'api_index',target=name,resource_check=real)
  if r['result']['status']!='ok':raise InvalidUpdate(r['result'].get('error','API indexing failed'))
  ids.append(r['id'])
 data=facts.indexes(s,session,workspace,ids);sites=facts.inventory(data);offset=request.get('offset',0)
 if type(offset) is not int or not 0<=offset<len(sites):raise InvalidUpdate('API call offset is outside the inspected inventory')
 selected=[c['id'] for c in sites[offset:offset+facts.MAX_SITES]]
 prompt='Map this batch of source-level API calls, preserving the task and all constraints.'
 while selected:
  packet=facts.packet(data,selected);payload={'contract':s.pinned(session),'request':prompt,'workspace':str(workspace),'pages':[],
    'api':{'index_receipts':ids,'site_ids':selected,'packet_hash':sha(canonical(packet))},'packet':packet}
  try:
   # Reserve space for the independent reviewer candidate and bounded repair.
   provider.budget.check(roles.MAP_SYSTEM,dict(payload,reserve='x'*1000),roles.schema('api_mapper',payload))
   probe=dict(payload,candidate_id='x'*32,readable_sites=roles.readable_sites(packet),request='Independently reconstruct each API call from source evidence without seeing the proposed map.',repair={'errors':['x'*140]*2})
   Budget(response_reserve=2048).check(roles.REVIEW_SYSTEM,probe,roles.schema('api_reviewer',probe));break
  except ContextBudgetExceeded:selected=selected[:-1]
 if not selected:raise ContextBudgetExceeded('One API relationship cannot fit this context budget; narrow the file set')
 result=invoke(s,session,root,'api_mapper',payload,provider,real)
 if result['admission']['admitted']:
  meta['api_candidate']=result['invocation_id'];meta.pop('api_review',None)
  meta['api_position']={'files':files,'offset':offset,'count':len(selected),'total':len(sites),'next_offset':offset+len(selected) if offset+len(selected)<len(sites) else None}
 return {'result':result,'api':state(s,meta,workspace)}

def verify(s,meta,workspace,root,request,provider=None):
 session=meta['active'];candidate_id=meta.get('api_candidate')
 if not candidate_id:raise InvalidUpdate('Build an API draft first')
 candidate=s.load_result(session,candidate_id)
 env=json.loads(s.db.execute('select record_json from invocations where id=?',(candidate_id,)).fetchone()[0])
 source=json.loads(s.db.execute('select payload_json from deliveries where id=?',(env['delivery_id'],)).fetchone()[0])
 payload={k:copy.deepcopy(source[k]) for k in ('contract','workspace','pages','api','packet')}
 payload.update(request='Independently reconstruct each API call from source evidence without seeing the proposed map.',candidate_id=candidate_id,readable_sites=roles.readable_sites(source['packet']))
 from model_identity import choose,compare,unknown,enforce
 real=provider is None;strict=request.get('require_distinct_reviewer',False)
 mapper=env.get('model_identity') or unknown(env['model'])
 if real:
  reviewer,separation=choose(mapper,request.get('reviewer_model','auto'))
  enforce(separation,strict)
  provider=provider_for(root,reviewer,review=True);provider.expected_identity=separation['reviewer_identity']
 else:
  from model_identity import observe
  observed=observe(provider.model) if isinstance(provider,LocalModel) else getattr(provider,'model_identity',unknown(provider.model))
  if isinstance(provider,LocalModel):
   if getattr(provider,'expected_identity',observed)!=observed:raise InvalidUpdate('Selected reviewer identity changed')
   provider.expected_identity=observed
  separation=compare(mapper,observed)
  separation.update(selection='injected',requested_model=provider.model);enforce(separation,strict)
 result=invoke(s,session,root,'api_reviewer',payload,provider,real)
 if result['admission']['admitted']:
  initialize(s);data=roles.request_check(s,session,'api_reviewer',payload);host=facts.assess(data,candidate);opinions=facts.compare(candidate,result['accepted_output']);decision=facts.decision(host,opinions)
  previous=s.db.execute('select seq,id from api_reviews where session=? order by seq desc limit 1',(session,)).fetchone() or (0,'GENESIS')
  record={'session':session,'seq':previous[0]+1,'previous_hash':previous[1],'candidate_id':candidate_id,'reviewer_id':result['invocation_id'],'contract_hash':s.origin(session)['contract_hash'],'logic_hash':s.logic_hash,'packet_hash':payload['api']['packet_hash'],'host':host,'reviewer_comparison':opinions,'decision':decision,'independence':separation,'created_at':time.time()}
  raw=canonical(record);rid=sha('api-review-v1:'+raw)
  with s.db:s.db.execute('insert into api_reviews values(?,?,?,?)',(rid,session,record['seq'],raw));s._audit('API_PUBLICATION',dict(id=rid,**record))
  meta['api_review']=rid
 return {'result':result,'api':state(s,meta,workspace)}

def records(s,session):
 if not s.db.execute("select 1 from sqlite_master where name='api_reviews'").fetchone():return []
 rows=[];previous='GENESIS'
 for i,(rid,seq,raw) in enumerate(s.db.execute('select id,seq,record_json from api_reviews where session=? order by seq',(session,)),1):
  r=json.loads(raw)
  if sha('api-review-v1:'+raw)!=rid or r['previous_hash']!=previous or seq!=i:raise InvalidUpdate('API review chain corrupted')
  rows.append(dict(r,id=rid));previous=rid
 return rows

def state(s,meta,workspace):
 result={'status':'not_started','position':meta.get('api_position'),'candidate_id':meta.get('api_candidate'),'review_id':meta.get('api_review'),'report':None}
 if not result['candidate_id']:return result
 try:
  candidate=s.load_result(meta['active'],result['candidate_id']);result['status']='draft_unverified'
  row=next((r for r in records(s,meta['active']) if r['id']==result['review_id']),None)
  if row:
   reviewer=s.load_result(meta['active'],row['reviewer_id'])
   if row['candidate_id']!=result['candidate_id'] or row['logic_hash']!=s.logic_hash:raise InvalidUpdate('API review is stale')
   result.update(status='reviewed' if row['decision']['admitted'] else 'quarantined',decision=row['decision'],host=row['host'],reviewer_comparison=row['reviewer_comparison'],reviewer_id=row['reviewer_id'],independence=row.get('independence'))
   if row['decision']['admitted']:result['report']=render(s,meta['active'],candidate,row,meta.get('api_position'))
 except (InvalidUpdate,PageFault,ValueError) as exc:result.update(status='withheld',error=str(exc))
 return result

def render(s,session,candidate,row,position):
 env=json.loads(s.db.execute('select record_json from invocations where id=?',(row['candidate_id'],)).fetchone()[0]);p=json.loads(s.db.execute('select payload_json from deliveries where id=?',(env['delivery_id'],)).fetchone()[0])
 nodes={n['id']:n for n in p['packet']['nodes']};sites={c['id']:c for c in p['packet']['sites']};approved=row['decision']['verified_edges'];used=set()
 for key in approved:used.update((candidate['claims'][key]['source'],candidate['claims'][key]['target']))
 def safe(x):return ''.join(c if c.isalnum() or c in '/._ -:@' else '_' for c in x)
 sep=row.get('independence')
 from model_identity import label
 note=label(sep) if sep and 'weight_relation' in sep else 'Reviewer weight separation was not established for this legacy review.'
 lines=['# Reviewed API call map','',f'Karma task: {session}',f'Mapper invocation: {row["candidate_id"]}',f'Reviewer invocation: {row["reviewer_id"]}',f'Publication receipt: {row["id"]}',note,'',f'Batch {position["offset"]+1}–{position["offset"]+position["count"]} of {position["total"]} indexed call sites.','Static lexical references; no claim that calls execute at runtime.','']
 if approved:
  lines+=['```mermaid','flowchart LR']
  for key in sorted(used):lines.append('  '+key+'["'+safe(nodes[key]['path']+'::'+nodes[key]['name'])+'"]')
  for key in approved:
   c=candidate['claims'][key];lines.append('  '+c['source']+' -->|calls| '+c['target'])
  lines+=['```','']
 for key in approved:lines.append('- Supported: '+safe(sites[key]['path'])+':'+str(sites[key]['line'])+' — '+safe(sites[key]['expression']))
 for key in row['decision']['unresolved']:lines.append('- Unresolved: '+safe(sites[key]['path'])+':'+str(sites[key]['line'])+' — '+row['host'][key]['reason'])
 lines+=['','Only this batch and indexed files were reviewed. Unknown calls are excluded from solid diagram edges. Source changes invalidate this report.']
 return '\n'.join(lines)+'\n'
