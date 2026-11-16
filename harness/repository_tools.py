"""Scoped repository reads/search with durable commitments and host receipts."""
import hashlib,json,os,stat,time,uuid
from pathlib import Path,PurePosixPath
from context_manager import InvalidUpdate,canonical,sha
from karma_adapter import budget_decision
from monitoring import require_resources

EXTENSIONS={'.py','.js','.ts','.tsx','.jsx','.json','.md','.txt','.toml','.yaml','.yml','.html','.css','.go','.rs','.java','.c','.h','.cpp','.m','.swift','.sql'}
EXCLUDE={'node_modules','vendor','dist','build','__pycache__','venv','target','coverage','secrets.json','secrets.yaml','credentials.json'}
MAX_FILES=128;MAX_FILE=64000

def initialize(s):
 s.db.executescript('''CREATE TABLE IF NOT EXISTS repository_ops(id TEXT PRIMARY KEY,session TEXT,request_id TEXT UNIQUE,record_json TEXT,digest TEXT);
 CREATE TABLE IF NOT EXISTS repository_receipts(id TEXT PRIMARY KEY,session TEXT,seq INTEGER,operation_id TEXT UNIQUE,record_json TEXT,UNIQUE(session,seq));''')
 for table in ('repository_ops','repository_receipts'):
  for op in ('UPDATE','DELETE'):s.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{op} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'immutable repository record'); END")
 s.db.commit()

def normalized(name,root=False):
 if not isinstance(name,str) or not name or len(name)>180 or '\\' in name or ',' in name:raise InvalidUpdate('Invalid repository path')
 p=PurePosixPath(name)
 if name=='.' and root:return name
 if ':' in name or p.is_absolute() or '..' in p.parts or str(p)!=name or any(x.startswith('.') or x in EXCLUDE or x.endswith((' ','.')) or x.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]} for x in p.parts):raise InvalidUpdate('Repository path is outside permitted code scope')
 if any(ord(c)<32 for c in name):raise InvalidUpdate('Control characters in path')
 return name

def settings(s,session):
 c=s.snapshot(session)['constraints']
 if c.get('review_only') is not True or c.get('edits_allowed') is not False or c.get('tests_allowed') is not False:raise InvalidUpdate('Create a read-only architecture contract first')
 if c.get('auto_review'):
  from review_budget import allocations
  allocations(c)
 elif type(c.get('max_tools')) is not int or not 1<=c['max_tools']<=32:raise InvalidUpdate('Repository tool budget must be 1..32')
 return c

def scoped(name,roots):return any(r=='.' or name==r or name.startswith(r+'/') for r in roots)

def read_bytes(workspace,name,limit=MAX_FILE):
 if type(limit) is not int or limit<=0:raise InvalidUpdate('Invalid source byte allocation')
 if os.name=='nt':
  from platform_support import windows_read
  normalized(name)
  try:return windows_read(workspace,name,limit)
  except (OSError,UnicodeError) as exc:raise InvalidUpdate('Repository read rejected: '+str(exc)) from None
 normalized(name);parts=Path(name).parts;fd=os.open(str(Path(workspace).resolve()),os.O_RDONLY|os.O_DIRECTORY)
 try:
  for part in parts[:-1]:
   nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=nxt
  f=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
  with os.fdopen(f,'rb') as stream:
   before=os.fstat(stream.fileno())
   if not stat.S_ISREG(before.st_mode):raise InvalidUpdate('Source is not a regular file: '+name)
   if before.st_nlink!=1:raise InvalidUpdate('Source has multiple hard links: '+name)
   if before.st_size>limit:raise InvalidUpdate(f'Source {name} needs {before.st_size} bytes; remaining page allocation is {limit} bytes')
   raw=stream.read(limit+1);after=os.fstat(stream.fileno())
   if len(raw)>limit or (before.st_mtime_ns,before.st_size)!=(after.st_mtime_ns,after.st_size):raise InvalidUpdate('File changed while reading')
  if b'\0' in raw:raise InvalidUpdate('Binary file rejected')
  raw.decode('utf-8');return raw
 except (OSError,UnicodeError) as exc:raise InvalidUpdate('Repository read rejected: '+str(exc)) from None
 finally:os.close(fd)

def inventory(workspace,roots,automatic=False):
 result=[];visited=0;started=time.monotonic()
 for folder,dirs,files in os.walk(workspace,followlinks=False):
  visited+=1
  if visited>5000 or time.monotonic()-started>2:raise InvalidUpdate('Catalog scan bound reached; narrow the review roots')
  relative=Path(folder).relative_to(workspace).as_posix()
  dirs[:]=sorted(d for d in dirs if not d.startswith('.') and d not in EXCLUDE and not (Path(folder)/d).is_symlink() and
    (scoped((Path(relative)/d).as_posix(),roots) or any(r.startswith((Path(relative)/d).as_posix()+'/') for r in roots)))
  for name in sorted(files):
   visited+=1
   if visited>5000 or time.monotonic()-started>2:raise InvalidUpdate('Catalog scan bound reached; narrow the review roots')
   logical=(Path(relative)/name).as_posix()
   try:normalized(logical)
   except InvalidUpdate:continue
   if not scoped(logical,roots) or Path(name).suffix.lower() not in EXTENSIONS:continue
   p=Path(folder)/name;st=p.lstat()
   if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1 or (not automatic and st.st_size>MAX_FILE):continue
   result.append(logical)
   if not automatic and len(result)>MAX_FILES:raise InvalidUpdate('More than 128 code files; narrow the review roots')
 if not result:raise InvalidUpdate('No supported code files in the review scope')
 return sorted(result)

def records(s,session):
 if not s.db.execute("SELECT 1 FROM sqlite_master WHERE name='repository_ops'").fetchone():return []
 result=[];previous='GENESIS';seq=0
 for raw,digest,rid,rraw in s.db.execute('SELECT o.record_json,o.digest,r.id,r.record_json FROM repository_ops o LEFT JOIN repository_receipts r ON r.operation_id=o.id WHERE o.session=? ORDER BY r.seq',(session,)):
  op=json.loads(raw)
  if sha('repository-op-v1:'+raw)!=digest:raise InvalidUpdate('Repository commitment corrupted')
  if not rraw:raise InvalidUpdate('Repository operation pending or interrupted; inspect before continuing')
  r=json.loads(rraw);seq+=1
  if r['seq']!=seq or r['previous_hash']!=previous or r['operation_hash']!=digest or sha('repository-receipt-v1:'+rraw)!=rid:raise InvalidUpdate('Repository receipt chain corrupted')
  previous=rid;result.append({'id':rid,'operation':op,'result':r['result']})
 return result

def current_records(s,session):
 origin=s.origin(session)
 return [r for r in records(s,session) if r['operation']['contract_hash']==origin['contract_hash'] and r['operation']['logic_hash']==s.logic_hash]

def catalog(s,session):
 if s.snapshot(session)['constraints'].get('auto_review'):
  row=s.db.execute("SELECT r.id FROM repository_ops o JOIN repository_receipts r ON r.operation_id=o.id WHERE o.session=? AND json_extract(o.record_json,'$.kind')='catalog' ORDER BY r.seq DESC LIMIT 1",(session,)).fetchone()
  if not row:raise InvalidUpdate('Automatic catalog absent')
  record=record_by_id(s,session,row[0])
  if record['result']['status']!='ok':raise InvalidUpdate('Latest catalog refresh failed')
  return record['result']['files']
 rows=[r for r in current_records(s,session) if r['operation']['kind']=='catalog' and r['result']['status']=='ok']
 if not rows:raise InvalidUpdate('Review catalog absent or stale; start a new architecture review')
 return rows[-1]['result']['files']

def usage(s,session):
 c=settings(s,session);origin=s.origin(session)
 used=sum(json.loads(raw)['contract_hash']==origin['contract_hash'] for raw, in s.db.execute('SELECT record_json FROM repository_ops WHERE session=?',(session,))) if c.get('auto_review') else sum(r['operation']['contract_hash']==origin['contract_hash'] for r in records(s,session))
 from review_budget import usage as task_usage
 allocation=task_usage(s,session)
 return budget_decision(used,allocation['limits']['operations'] if allocation else c['max_tools'])

def request_check(s,session,kind,target='',query='',line=1,cursor=None):
 c=settings(s,session)
 if kind not in ('catalog','read','search','list','api_index','static_map'):raise InvalidUpdate('Unsupported repository operation')
 if type(line) is not int or line<1:raise InvalidUpdate('Line/offset must be positive')
 if cursor is not None:
  if kind!='read' or not isinstance(cursor,dict) or set(cursor)!={'line','column','sha256'}:raise InvalidUpdate('Invalid read continuation')
  if type(cursor['line']) is not int or cursor['line']!=line or type(cursor['column']) is not int or cursor['column']<0:raise InvalidUpdate('Invalid read continuation position')
  if not isinstance(cursor['sha256'],str) or len(cursor['sha256'])!=64 or any(c not in '0123456789abcdef' for c in cursor['sha256']):raise InvalidUpdate('Invalid read continuation version')
 if kind=='catalog':
  if target or query or line!=1:raise InvalidUpdate('Unexpected catalog arguments')
  return
 names=catalog(s,session)
 if kind=='static_map':
  files=target.split('\n') if isinstance(target,str) else []
  if not files or (not c.get('auto_review') and len(files)>8) or len(set(files))!=len(files) or query or line!=1:raise InvalidUpdate('Choose unique static-map files within the task scope')
  for name in files:
   normalized(name)
   if name not in names or not scoped(name,c['allowed_paths']):raise InvalidUpdate('Static source outside committed scope')
 elif kind in ('read','api_index'):
  normalized(target)
  if target not in names or not scoped(target,c['allowed_paths']):raise InvalidUpdate('Read target outside committed catalog')
  if query:raise InvalidUpdate('Read cannot contain a query')
 elif kind=='search':
  if target or not isinstance(query,str) or not 1<=len(query)<=80 or '\n' in query or line!=1:raise InvalidUpdate('Search requires one bounded literal query')
 elif target or query or line>len(names):raise InvalidUpdate('Invalid catalog page')

def read_excerpt(text,line,column=0):
 lines=text.splitlines(True)
 if line>len(lines) or column>=len(lines[line-1]):raise InvalidUpdate('Requested position is beyond source')
 body=''.join(lines[line-1:line+29])[column:].encode()[:1200].decode('utf-8','ignore')
 index=line-1;position=column+len(body)
 while index<len(lines) and position>=len(lines[index]):
  position-=len(lines[index]);index+=1
 return body,({'line':index+1,'column':position} if index<len(lines) else None)

def execute(s,session,workspace,root,kind,*,target='',query='',line=1,cursor=None,invocation_id=None,request_id=None,resource_check=True):
 from lifecycle import timeout
 timeout(20)
 initialize(s);c=settings(s,session);automatic=c.get('auto_review',False);rows=[] if automatic else records(s,session)
 request_id=request_id or uuid.uuid4().hex
 if automatic:
  prior=s.db.execute('SELECT r.id FROM repository_ops o LEFT JOIN repository_receipts r ON r.operation_id=o.id WHERE o.request_id=?',(request_id,)).fetchone()
  if prior:
   if not prior[0]:raise InvalidUpdate('Repository operation pending')
   rows=[record_by_id(s,session,prior[0])]
 for row in rows:
  if row['operation']['request_id']==request_id:
   op=row['operation']
   if (op['kind'],op['target'],op['query'],op['line'])!=(kind,target,query,line):raise InvalidUpdate('Replayed request has changed arguments')
   if op.get('cursor')!=cursor:raise InvalidUpdate('Replayed request has changed continuation')
   if op['contract_hash']!=s.origin(session)['contract_hash'] or op['logic_hash']!=s.logic_hash:raise InvalidUpdate('Replayed tool receipt is stale')
   return dict(row,replayed=True)
 request_check(s,session,kind,target,query,line,cursor)
 if invocation_id:
  p=s.load_result(session,invocation_id)
  expected={'kind':kind,'target':target,'query':query,'line':line}
  if cursor is not None:expected['cursor']=cursor
  if p.get('tool')!=expected:raise InvalidUpdate('Tool request differs from accepted model proposal')
 from review_budget import require
 source_charge=sum((Path(workspace)/p).stat().st_size for p in target.split('\n')) if kind=='static_map' else 0 if kind in ('catalog','list') else MAX_FILE
 from storage_policy import require_space
 require_space(root,max(16*1024**2,source_charge*32))
 cost={'operations':1,'source_bytes':source_charge,'seconds':20}
 task_frame=require(s,session,cost)
 budget=usage(s,session)
 if budget['decision']!='ALLOW':
  with s.db:s._audit('REPOSITORY_DENIED',{'session':session,'kernel':budget})
  raise InvalidUpdate('Repository budget exhausted: '+budget['decision'])
 hardware=require_resources(root) if resource_check else {'profile':'bounded-metadata-scan' if kind=='catalog' else 'unit-test-fixture'}
 op={'id':uuid.uuid4().hex,'session':session,'request_id':request_id,'invocation_id':invocation_id,
     'actor':'governed_model' if invocation_id else 'host_tool','kind':kind,'target':target,'query':query,'line':line,
     'contract_hash':s.origin(session)['contract_hash'],'logic_hash':s.logic_hash,'kernel':budget,'resource':hardware,'started_at':time.time(),'task_cost':cost if automatic else {},'task_budget_kernel':task_frame}
 if cursor is not None:op['cursor']=cursor
 with s.db:
  s.db.execute('BEGIN IMMEDIATE')
  from orchestration import admit
  from orchestration import ACTIVE,node
  if ACTIVE.get() and node(s,ACTIVE.get())['kind']=='branch' and kind not in ('read','api_index','static_map'):
   raise InvalidUpdate('Child repository operation requires an exact scoped target')
  op['orchestration']=admit(s,session,'repository',op['id'],cost,paths=target.split('\n') if target else [],resource=hardware)
  digest=sha('repository-op-v1:'+canonical(op))
  s.db.execute('INSERT INTO repository_ops VALUES(?,?,?,?,?)',(op['id'],session,request_id,canonical(op),digest));s._audit('REPOSITORY_DISPATCH',op)
 started_op=time.monotonic()
 try:
  if kind=='catalog':result={'status':'ok','files':inventory(Path(workspace),c['allowed_paths'],automatic=automatic),'excerpts':[]}
  elif kind=='list':
   names=catalog(s,session) if automatic else next(r['result']['files'] for r in reversed(rows) if r['operation']['kind']=='catalog' and r['operation']['contract_hash']==op['contract_hash'] and r['result']['status']=='ok')
   result={'status':'ok','files':names[line-1:line+19],'next_line':line+20 if line+19<len(names) else None,'excerpts':[]}
  elif kind=='static_map':
   from static_analysis import snapshot
   result=snapshot(workspace,target.split('\n'),max_total=max(1,min(c['review_page_kib']*1024,source_charge)) if automatic else None)
  elif kind=='api_index':
   from api_extract import extract
   raw=read_bytes(workspace,target);facts=extract(target,raw.decode('utf-8'))
   result={'status':'ok','api':facts,'versions':{target:facts['sha256']},'excerpts':[]}
  else:
   names=catalog(s,session) if automatic else next(r['result']['files'] for r in reversed(rows) if r['operation']['kind']=='catalog' and r['operation']['contract_hash']==op['contract_hash'] and r['result']['status']=='ok')
   selected=[target] if kind=='read' else names;excerpts=[];versions={};started=time.monotonic();truncated=False;next_position=None
   for name in selected:
    if time.monotonic()-started>3:truncated=True;break
    raw=read_bytes(workspace,name);text=raw.decode();version=hashlib.sha256(raw).hexdigest();versions[name]=version;lines=text.splitlines(True)
    if kind=='read':
     if cursor and cursor['sha256']!=version:raise InvalidUpdate('Source changed since read continuation; restart the read')
     column=cursor['column'] if cursor else 0
     body,next_position=read_excerpt(text,line,column)
     excerpts.append({'path':name,'line':line,'column':column,'text':body,'sha256':version});truncated=next_position is not None
    else:
     for n,body in enumerate(lines,1):
      if query in body:
       start=max(0,body.index(query)-60);snippet=body[start:start+240]
       excerpts.append({'path':name,'line':n,'text':snippet,'sha256':version})
       if len(excerpts)>=5:truncated=True;break
     if len(excerpts)>=5:break
   result={'status':'ok','excerpts':excerpts,'versions':versions,'truncated':truncated,'scanned_files':len(versions)}
   if kind=='read':
    result['next_line']=next_position['line'] if next_position else None
    result['next_cursor']=dict(next_position,sha256=version) if next_position else None
 except Exception as exc:result={'status':'failed','error':type(exc).__name__+': '+str(exc),'excerpts':[]}
 for i,e in enumerate(result.get('excerpts',[])):e['id']='e'+op['id'][:10]+'_'+str(i)
 return finish(s,op,digest,result,time.monotonic()-started_op)

def finish(s,op,digest,result,elapsed_seconds):
 session=op['session']
 prior=s.db.execute('SELECT seq,id FROM repository_receipts WHERE session=? ORDER BY seq DESC LIMIT 1',(session,)).fetchone() or (0,'GENESIS')
 previous=prior[1]
 receipt={'operation_id':op['id'],'operation_hash':digest,'session':session,'seq':prior[0]+1,'previous_hash':previous,'result':result,'finished_at':time.time(),'elapsed_seconds':elapsed_seconds}
 rid=sha('repository-receipt-v1:'+canonical(receipt))
 with s.db:
  s.db.execute('INSERT INTO repository_receipts VALUES(?,?,?,?,?)',(rid,session,prior[0]+1,op['id'],canonical(receipt)));s._audit('REPOSITORY_RECEIPT',{'id':rid,'record':receipt})
  from orchestration import settle
  settle(s,op.get('orchestration'),rid,elapsed_seconds)
 return {'id':rid,'operation':op,'result':result}

def record_by_id(s,session,rid):
 row=s.db.execute('SELECT o.record_json,o.digest,r.record_json FROM repository_ops o JOIN repository_receipts r ON r.operation_id=o.id WHERE r.id=? AND r.session=?',(rid,session)).fetchone()
 if not row:raise InvalidUpdate('Repository receipt absent')
 raw,digest,receipt=row;op=json.loads(raw);r=json.loads(receipt);origin=s.origin(session)
 if sha('repository-op-v1:'+raw)!=digest or sha('repository-receipt-v1:'+receipt)!=rid or r['operation_hash']!=digest or r['operation_id']!=op['id']:raise InvalidUpdate('Repository receipt corrupted')
 if op['contract_hash']!=origin['contract_hash'] or op['logic_hash']!=s.logic_hash:raise InvalidUpdate('Repository receipt stale')
 return {'id':rid,'operation':op,'result':r['result']}

def evidence(s,session,workspace):
 result={};cache={}
 for row in current_records(s,session):
  if row['result']['status']!='ok':continue
  for e in row['result'].get('excerpts',[]):
   try:
    if e['path'] not in cache:cache[e['path']]=hashlib.sha256(read_bytes(workspace,e['path'])).hexdigest()
   except InvalidUpdate:continue
   if cache[e['path']]==e['sha256']:result[e['id']]=dict(e,receipt=row['id'])
 return result

def validate_evidence(s,session,workspace,items):
 available=evidence(s,session,workspace)
 for item in items:
  original=available.get(item['id'])
  if not original or any(item[k]!=original[k] for k in ('path','line','sha256','receipt')) or not item['text'] or item['text'] not in original['text']:
   raise InvalidUpdate('Repository evidence is stale, fabricated or outside a receipt')
 return available

def status(s,session):
 rows=current_records(s,session);names=catalog(s,session)
 return {'catalog_files':len(names),'catalog':names,'budget':usage(s,session),'operations':[{'id':r['id'],'kind':r['operation']['kind'],'target':r['operation']['target'],'query':r['operation']['query'],'status':r['result']['status']} for r in rows],
         'inspected_files':sorted({e['path'] for r in rows for e in r['result'].get('excerpts',[])})}


def feedback(s,session):
 rows=current_records(s,session)
 if not rows:return None
 row=rows[-1];op=row['operation'];result=row['result']
 item={'receipt':row['id'],'kind':op['kind'],'target':op['target'],'query':op['query'],'status':result['status'],
       'matches_or_excerpts':len(result.get('excerpts',[])),'truncated':result.get('truncated',False)}
 for key in ('error','scanned_files','next_line','next_cursor'):
  if key in result:item[key]=result[key]
 if op['kind']=='list':item['files']=result.get('files',[])
 return item
