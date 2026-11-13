"""A persisted, source-verified review queue. Model changes never own the queue."""
import json,re,time
from pathlib import Path
from context_manager import InvalidUpdate,ContextBudgetExceeded,sha,canonical
from paging import PageFault
import repository_tools as repo
import static_analysis as analysis
import static_runtime as runtime
import review_budget as budget

CODE={'.py','.js','.jsx','.ts','.tsx'}

def failure_version(workspace,name,cap):
 try:
  st=(workspace/name).stat()
  return {'sha256':sha(repo.read_bytes(workspace,name,limit=cap).decode())} if st.st_size<=cap else {'size':st.st_size,'mtime_ns':st.st_mtime_ns}
 except (OSError,InvalidUpdate):return {'unavailable':True}

def fail_page(a,workspace,name,message,cap):
 a.setdefault('failures',{})[name]=message;a.setdefault('failure_versions',{})[name]=failure_version(workspace,name,cap)

def ranked(names,goal,preferred=()):
 words=set(re.findall(r'[a-z][a-z0-9]+',goal.lower()))-{'review','codebase','whole','code','explain','architecture','with','the','and'}
 def score(p):
  parts=set(re.findall(r'[a-z][a-z0-9]+',p.lower()));base=Path(p).stem
  return (30 if p in preferred else 0)+10*len(parts&words)+(8 if base in ('index','main','app','routes','server','router') else 0)+(3 if p.startswith(('server/','client/src/')) else 0)
 return sorted(names,key=lambda p:(-score(p),p))

def current_pages(s,meta,workspace):
 valid=[];stale=[];cache={}
 for page in meta.get('auto',{}).get('pages',[]):
  if page.get('superseded'):continue
  try:
   row=repo.record_by_id(s,meta['active'],page['receipt'])
   if row['result']['versions']!=page['versions']:raise InvalidUpdate('Page version commitment differs')
   for p,h in page['versions'].items():
    repo.request_check(s,meta['active'],'read',p)
    if p not in cache:cache[p]=sha(repo.read_bytes(workspace,p,limit=page['read_limit']).decode())
    if cache[p]!=h:raise InvalidUpdate('Source changed')
   valid.append(page)
  except (InvalidUpdate,OSError,ValueError):stale.append(page['receipt'])
 return valid,stale

def status(s,meta,workspace,verify=True):
 sid=meta['active'];c=s.snapshot(sid)['constraints']
 # Coverage validation must notice newly added files even when merely opening
 # a saved report. Actual source dispatch still requires the committed catalog.
 repo.catalog(s,sid);names=repo.inventory(Path(workspace),c['allowed_paths'],automatic=True)
 sources=[p for p in names if Path(p).suffix in CODE];a=meta.setdefault('auto',{'pages':[],'failures':{}})
 valid,stale=current_pages(s,meta,workspace) if verify else (a['pages'],[])
 mapped=set(p for page in valid for p in page['primary_files']);mapped.intersection_update(sources)
 pending=ranked(set(sources)-mapped,s.snapshot(sid)['goal'],a.get('preferred',[]));notes=[]
 for page in valid:
  if page.get('invocation_id'):
   try:
    out=s.load_result(sid,page['invocation_id']);notes.append({'receipt':page['receipt'],'invocation_id':page['invocation_id'],'model':page['model'],'summary':out['summary'],'sites':page.get('explained_sites',[])})
   except (InvalidUpdate,PageFault,ValueError):pass
 frame=budget.decision(s,sid)
 return {'status':a.get('status','ready'),'catalog_files':len(names),'source_files':len(sources),'mapped_files':len(mapped),'pending_files':len(pending),'next_files':pending[:12],'metadata_only_files':len(names)-len(sources),'pages':len(valid),'stale_pages':stale,'notes':notes,'explained_sites':sum(len(n['sites']) for n in notes),'failures':a.get('failures',{}),'budget':frame,'last_page':a.get('last_page'),'structural_pass_complete':not pending,'semantic_review_complete':False,'scope_notice':'Counts describe supported source mapping. Notes explain sampled call sites, not every function. Non-code files are cataloged only.'}

def select_files(s,meta,workspace,st):
 names=repo.catalog(s,meta['active']);c=s.snapshot(meta['active'])['constraints'];cap=c['review_page_kib']*1024;a=meta['auto']
 valid,_=current_pages(s,meta,workspace);done={p for page in valid for p in page['primary_files']}
 candidates=ranked([p for p in names if Path(p).suffix in CODE and p not in done and p not in a.get('failures',{})],s.snapshot(meta['active'])['goal'],a.get('preferred',[]))
 if not candidates:return [],[]
 config=['tsconfig.json'] if 'tsconfig.json' in names and any(Path(p).suffix!='.py' for p in candidates) else []
 selected=list(config);size=sum((workspace/p).stat().st_size for p in config);primary=[]
 # Byte allocation, rather than file count, sets the page. No source is executed.
 for p in candidates:
  length=(workspace/p).stat().st_size
  if size+length>cap:
   if not primary:fail_page(a,workspace,p,'File exceeds this page byte allocation; increase review_page_kib in a new contract.',cap)
   continue
  selected.append(p);primary.append(p);size+=length
 return selected,primary

def dependency_hints(graph,names):
 # Retrieval hints only. Only the compiler may produce actual graph edges.
 found=[];known=set(names)
 for d in graph['module_dependencies']:
  spec=d['specifier'];base=None
  if spec.startswith('.'):base=Path(d['source']).parent/spec
  elif spec.startswith('@shared/'):base=Path('shared')/spec[len('@shared/'):]
  elif spec.startswith('@/'):base=Path('client/src')/spec[2:]
  if base:
   import posixpath
   name=posixpath.normpath(base.as_posix())
   for q in [name,*[name+ext for ext in ('.ts','.tsx','.js','.jsx','.py')],name+'/index.ts',name+'/index.tsx']:
    if q in known:found.append(q)
 return sorted(set(found))

def step(s,meta,workspace,root,request,provider=None):
 c=s.snapshot(meta['active'])['constraints']
 if not c.get('auto_review'):raise InvalidUpdate('Start an automatic codebase review first')
 a=meta.setdefault('auto',{'pages':[],'failures':{}});a['status']='running';a.pop('error',None);a.pop('stop_frame',None);result=None
 try:
  # Refresh the directory TOC on every page; source bodies are never cached.
  catalog=repo.execute(s,meta['active'],workspace,root,'catalog',resource_check=provider is None)
  if catalog['result']['status']!='ok':raise InvalidUpdate(catalog['result']['error'])
  for p in list(a['failures']):
   if p not in catalog['result']['files'] or a.get('failure_versions',{}).get(p)!=failure_version(workspace,p,c['review_page_kib']*1024):a['failures'].pop(p);a.get('failure_versions',{}).pop(p,None)
  st=status(s,meta,workspace);valid,_=current_pages(s,meta,workspace)
  retry=next((p for p in reversed(valid) if p.get('explanation_status')=='pending'),None) if request.get('explain',True) else None
  files,primary=(retry['files'],retry['primary_files']) if retry else select_files(s,meta,workspace,st)
  if not primary:
   a['status']='structural_pass_complete' if st['pending_files']==0 else 'needs_attention'
   return {'result':None,'auto':status(s,meta,workspace)}
  # A parser work-unit overflow bridges to a smaller page, not task completion.
  while primary:
   try:row=runtime.build(s,meta,workspace,root,files,real=provider is None);break
   except budget.BudgetStop:raise
   except InvalidUpdate as exc:
    with s.db:s._audit('AUTO_PAGE_SPLIT',{'session':meta['active'],'files':files,'reason':str(exc)})
    if len(primary)==1:
     fail_page(a,workspace,primary[0],str(exc),c['review_page_kib']*1024);a['status']='page_rejected';return {'result':None,'auto':status(s,meta,workspace)}
    primary=primary[:max(1,len(primary)//2)];files=[p for p in files if Path(p).name=='tsconfig.json' or p in primary]
  data=row['result'];graph=data['graph'];page={'receipt':row['id'],'versions':data['versions'],'read_limit':data['read_limit'],'primary_files':primary,'files':files,'call_count':len(graph['calls']),'resolved':sum(bool(c['target']) for c in graph['calls']),'invocation_id':None,'model':None,'explained_sites':[]}
  if retry:retry['superseded']=True
  page['explanation_status']='pending' if request.get('explain',True) and graph['calls'] else 'not_requested'
  a['pages'].append(page);a['last_page']={'receipt':row['id'],'files':primary,'calls':page['call_count'],'resolved':page['resolved']};a['preferred']=dependency_hints(graph,repo.catalog(s,meta['active']))
  for p in primary:a['failures'].pop(p,None)
  if request.get('explain',True) and graph['calls']:
   model=provider.model if provider else request.get('model') or meta.get('model') or 'qwen3.6:35b'
   notes=st['notes'];cursor={'session':meta['active'],'contract_hash':s.origin(meta['active'])['contract_hash'],'mapped_before_this_page':st['mapped_files'],'source_files':st['source_files'],'current_files_sample':primary[:4],'current_file_count':len(primary),'remaining_after_this_page':max(0,st['pending_files']-len(primary)),'prior_model':notes[-1]['model'] if notes else None,'prior_invocation':notes[-1]['invocation_id'] if notes else None,'meaning':'Host-maintained coverage; earlier model commentary is retained in reports, not source authority.'}
   with s.db:s._audit('AUTO_PROGRESS',cursor)
   progress={'receipt':s.db.execute('SELECT event_hash FROM page_audit ORDER BY seq DESC LIMIT 1').fetchone()[0],'progress':cursor}
   nodes={n['id']:n for n in graph['nodes']};calls=sorted(graph['calls'],key=lambda call:(0 if call['target'] and nodes[call['target']]['path']!=call['path'] else 1 if call['target'] is None else 2,call['path'],call['line']))
   sites=[call['id'] for call in calls[:4]]
   while sites:
    try:result=runtime.explain(s,meta,workspace,root,row['id'],model,sites=sites,provider=provider,progress=progress);break
    except ContextBudgetExceeded:sites=sites[:-1]
    except budget.BudgetStop as exc:
     if exc.frame['decision']=='BRIDGE' and exc.frame['limiting_dimension']=='tokens' and len(sites)>1:sites=sites[:-1]
     else:raise
   if not sites:raise InvalidUpdate('Complete contract and one call cannot fit the model context')
   result.pop('record',None);result.pop('model_proposal',None);result.update(model=model,review_stage='static_explainer')
   if result['admission']['admitted']:
    page.update(invocation_id=result['invocation_id'],model=model,explained_sites=sites,explanation_status='accepted');meta['model']=model
   else:a['status']='model_quarantined';page['explanation_status']='quarantined'
  a['status']='ready' if a['status']=='running' else a['status']
  if status(s,meta,workspace)['pending_files']==0 and a['status']=='ready':a['status']='structural_pass_complete'
 except budget.BudgetStop as exc:
  a['status']='budget_stopped';a['stop_frame']=exc.frame
  with s.db:s._audit('AUTO_BUDGET_STOP',{'session':meta['active'],'kernel':exc.frame})
 except (InvalidUpdate,PageFault) as exc:
  a['status']='resource_wait' if 'RESOURCE_WAIT' in str(exc) else 'needs_attention';a['error']=str(exc)
 return {'result':result,'auto':dict(status(s,meta,workspace),stop_frame=a.get('stop_frame'),error=a.get('error'))}

def report(s,meta,workspace):
 st=status(s,meta,workspace);valid,_=current_pages(s,meta,workspace);edges=set();notes=st['notes'];diagnostics=0;calls=0
 for p in valid:
  g=analysis.load(s,meta['active'],workspace,p['receipt'])['graph'];diagnostics+=len(g['diagnostics']);calls+=len(g['calls'])
  for d in g['module_dependencies']:
   if d['target']:edges.add((d['source'],d['target']))
 labels=sorted({p for edge in edges for p in edge});ids={p:'m'+str(i) for i,p in enumerate(labels)}
 lines=['# Automatic codebase review','',s.snapshot(meta['active'])['goal'],'',f"Mapped {st['mapped_files']} of {st['source_files']} supported source files; {st['pending_files']} pending. {st['metadata_only_files']} non-code files cataloged only.",f"Kernel: {st['budget']['decision']}. Review status: {st['status']}. Semantic review complete: false.",'','Diagram: compiler-resolved module references in fresh supplied pages. Cross-page or dynamic relationships may remain unresolved.','','```mermaid','flowchart LR']
 for p in labels:lines.append(ids[p]+'["'+p.replace('"','_')+'"]')
 for a,b in sorted(edges):lines.append(ids[a]+' --> '+ids[b])
 lines+=['```','','## Page commentary (not independently verified)','']
 for n in notes:lines+=['- '+n['model']+': '+n['summary']+' (invocation '+n['invocation_id']+')']
 lines+=['','## Remaining work','',*['- '+p for p in st['next_files']],'',canonical({'failures':st['failures'],'budget':st['budget'],'scope_notice':st['scope_notice']})]
 return '\n'.join(lines)+'\n'
