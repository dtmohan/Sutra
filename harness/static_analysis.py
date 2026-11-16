from lifecycle import timeout as operation_timeout
"""Fresh, governed snapshots; deterministic graphs are independent of model prose."""
import json,os,platform,subprocess,time
from pathlib import Path
from context_manager import InvalidUpdate,canonical,sha
import repository_tools as repo

class StaticSourceChanged(InvalidUpdate):
 """The committed source version differs from the current scoped file."""

def analyze(texts,item_budget=1024):
 if type(item_budget) is not int or not 1<=item_budget<=16384:raise InvalidUpdate('Invalid parser work-unit allocation')
 started=time.monotonic();nodes=[];calls=[];diagnostics=[];dependencies=[];profiles=[];toolchain={};diagnostic_count=0
 js={p:t for p,t in texts.items() if Path(p).suffix in ('.js','.jsx','.ts','.tsx')}
 py={p:t for p,t in texts.items() if Path(p).suffix=='.py'}
 configs={p:t for p,t in texts.items() if Path(p).name=='tsconfig.json'}
 if len(configs)>1 or len(js)+len(py)+len(configs)!=len(texts) or not (js or py):raise InvalidUpdate('Static graph supports Python, JS/TS and one explicit tsconfig.json')
 if js:
  node=os.environ.get('KARMA_NODE');compiler=os.environ.get('KARMA_TYPESCRIPT')
  if not node or not compiler:raise InvalidUpdate('Static TypeScript graph requires trusted host parser paths')
  run=subprocess.run([node,str(Path(__file__).with_name('static_typescript.js'))],input=canonical({'files':js,'configs':configs,'typescript':compiler,'item_budget':item_budget}),text=True,capture_output=True,timeout=operation_timeout(20),env=dict(os.environ,ELECTRON_RUN_AS_NODE='1'))
  if run.returncode:raise InvalidUpdate('TypeScript compiler rejected snapshot: '+run.stderr[:400])
  data=json.loads(run.stdout);nodes+=data['nodes'];calls+=data['calls'];diagnostics+=data['diagnostics'];dependencies+=data['module_dependencies'];profiles.append(data['profile'])
  diagnostic_count+=data['diagnostic_count']
  toolchain={'typescript':data['compiler'],'typescript_sha256':sha(Path(compiler).read_bytes().decode()),'library_hashes':data['library_hashes'],'options':data['compiler_options']}
 if py:
  import api_extract,api_facts
  parsed={};binding_count=0;node_count=len(nodes);call_count=len(calls)
  for p,t in py.items():
   d=api_extract.extract(p,t,item_budget=item_budget);parsed[p]=d;binding_count+=len(d['bindings']);node_count+=len(d['symbols']);call_count+=len(d['calls'])
   if node_count>item_budget or call_count>item_budget or binding_count>4*item_budget:
    raise InvalidUpdate(f'Static graph work-unit allocation exceeded ({item_budget} nodes/calls; {4*item_budget} bindings)')
  for p,d in parsed.items():nodes += [{'id':n['id'],'name':n['name'],'kind':n['kind'],'path':p,'line':n['line'],'column':n['column'],'text':n['quote']} for n in d['symbols']]
  for c in api_facts.inventory(parsed):calls.append({'id':c['id'],'source':c['caller'],'target':c['target'],'expression':c['expression'],'relation':'static_call_reference' if c['target'] else 'unresolved','reason':c['reason'],'path':c['path'],'line':c['line'],'column':c['column'],'text':c['quote']})
  profiles.append('python-lexical-v1');toolchain['python']=platform.python_version()
 return {'profiles':profiles,'toolchain':toolchain,'versions':{p:sha(t) for p,t in texts.items()},'nodes':nodes,'calls':calls,'diagnostics':diagnostics,'diagnostic_count':diagnostic_count,'diagnostics_truncated':diagnostic_count>len(diagnostics),'module_dependencies':dependencies,'analysis_ms':round((time.monotonic()-started)*1000,3),'fresh_source_read':True,'cache_used':False}

def snapshot(workspace,files,max_total=None):
 texts={};remaining=max_total
 for p in files:
  raw=repo.read_bytes(workspace,p,limit=max(1,remaining) if remaining is not None else repo.MAX_FILE)
  if remaining is not None and len(raw)>remaining:raise InvalidUpdate('Source grew beyond the remaining page allocation: '+p)
  texts[p]=raw.decode()
  if remaining is not None:remaining-=len(raw)
 item_budget=max(1024,min(16384,max_total//8)) if max_total else 1024
 graph=analyze(texts,item_budget=item_budget);graph['item_budget']=item_budget
 # Guard both analysis completion and every later use. History is not a cache.
 for p,t in texts.items():
  if sha(repo.read_bytes(workspace,p,limit=max_total or repo.MAX_FILE).decode())!=sha(t):raise InvalidUpdate('Source changed during static analysis')
 return {'status':'ok','graph':graph,'sources':texts,'versions':graph['versions'],'excerpts':[],'read_limit':max_total or repo.MAX_FILE}

def load(s,session,workspace,receipt):
 row=repo.record_by_id(s,session,receipt)
 if not row or row['operation']['kind']!='static_map' or row['result']['status']!='ok':raise InvalidUpdate('Static snapshot receipt absent or stale')
 for p,h in row['result']['versions'].items():
  repo.request_check(s,session,'read',p)
  try:
   current=repo.read_bytes(workspace,p,limit=row['result'].get('read_limit',repo.MAX_FILE))
  except repo.SourceReadLimitExceeded as exc:
   raise StaticSourceChanged('Static snapshot source grew: '+p) from exc
  if sha(current.decode())!=h:raise StaticSourceChanged('Static snapshot source changed: '+p)
 chain=row['result']['graph']['toolchain']
 if 'python' in chain and platform.python_version()!=chain['python']:
  raise InvalidUpdate('Static Python compiler version changed')
 if 'typescript' in chain:
  compiler=Path(os.environ.get('KARMA_TYPESCRIPT','missing-compiler'))
  if not compiler.is_file() or sha(compiler.read_bytes().decode())!=chain['typescript_sha256']:raise InvalidUpdate('Static compiler version changed')
  for name,h in chain['library_hashes'].items():
   if Path(name).name!=name or sha((compiler.parent/name).read_bytes().decode())!=h:raise InvalidUpdate('Static compiler library changed')
 return row['result']

def compact(graph,sites):
 if not sites or len(sites)>8 or len(set(sites))!=len(sites):raise InvalidUpdate('Select 1..8 distinct call sites')
 byid={c['id']:c for c in graph['calls']}
 if not set(sites)<=set(byid):raise InvalidUpdate('Unknown static call site')
 rows=[byid[k] for k in sites];used={c['source'] for c in rows}|{c['target'] for c in rows if c['target']}
 return {'nodes':[{k:n[k] for k in ('id','name','path','line')} for n in graph['nodes'] if n['id'] in used],
         'calls':[{k:c[k] for k in ('id','source','target','expression','relation','path','line','text')} for c in rows],
         'diagnostics':[dict(d,message=d['message'][:120]) for d in graph['diagnostics'][:3]],'diagnostic_count':graph.get('diagnostic_count',len(graph['diagnostics'])),'meaning':'Static references only; dynamic dispatch and runtime execution are not proved. Diagnostic text is a bounded sample; total count includes omitted diagnostics.'}

def render(graph):
 nodes={n['id']:n for n in graph['nodes']};edges=[c for c in graph['calls'] if c['target']];used={c[k] for c in edges for k in ('source','target')}
 def safe(t):return ''.join(c if c.isalnum() or c in '/._ -:@' else '_' for c in t)
 lines=['# Deterministic source map','','Fresh source snapshot; no LLM generated these edges.','Static declaration references; runtime execution is not established.','','```mermaid','flowchart LR']
 for k in sorted(used):lines.append(k+'["'+safe(nodes[k]['path']+'::'+nodes[k]['name'])+'"]')
 for c in edges:lines.append(c['source']+' -->|references| '+c['target'])
 lines+=['```',''] if edges else ['```','','No resolved project edges.']
 for c in graph['calls']:lines.append(('- Reference: ' if c['target'] else '- Unresolved: ')+c['path']+':'+str(c['line'])+' '+c['expression']+' — '+c['reason'])
 for d in graph['diagnostics']:lines.append('- Diagnostic: '+d['path']+':'+str(d['line'])+' TS'+str(d['code'])+' '+d['message'])
 if graph.get('diagnostics_truncated'):lines.append('Diagnostic text truncated: showing '+str(len(graph['diagnostics']))+' of '+str(graph['diagnostic_count'])+'.')
 return '\n'.join(lines)+'\n'
