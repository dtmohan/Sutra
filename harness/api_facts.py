"""Host evidence and conservative lexical resolution for API map claims."""
import copy,json,posixpath
from pathlib import Path
from context_manager import InvalidUpdate,canonical,sha
import repository_tools as repo
from api_extract import PROFILE

MAX_SITES=3

def indexes(s,session,workspace,ids):
 if not isinstance(ids,list) or not 1<=len(ids)<=8 or len(set(ids))!=len(ids):raise InvalidUpdate('Choose 1..8 unique API indexes')
 rows={r['id']:r for r in repo.current_records(s,session)};result={}
 for rid in ids:
  r=rows.get(rid)
  if not r or r['operation']['kind']!='api_index' or r['result']['status']!='ok':raise InvalidUpdate('Missing or stale API index receipt')
  data=r['result']['api'];path=data['path']
  if path in result:raise InvalidUpdate('Duplicate file index')
  if data['profile']!=PROFILE or sha(repo.read_bytes(workspace,path).decode())!=data['sha256']:raise InvalidUpdate('API source version changed')
  result[path]=dict(data,receipt=rid)
 return result

def module_path(data,binding,all_data):
 module=binding['module'];path=data['path'];suffix=Path(path).suffix
 if suffix=='.py':
  base=module.replace('.','/')
  if binding.get('level'):
   parent=posixpath.dirname(path)
   for _ in range(binding['level']-1):parent=posixpath.dirname(parent)
   base=posixpath.join(parent,base)
  options=[base+'.py',base+'/__init__.py']
 else:
  if not module.startswith('.'):return None
  base=posixpath.normpath(posixpath.join(posixpath.dirname(path),module))
  options=[base,base+'.ts',base+'.tsx',base+'.js',base+'.jsx',base+'/index.ts',base+'/index.js']
 found=[x for x in options if x in all_data]
 return found[0] if len(found)==1 else None

def resolve(data,call,all_data):
 symbols={n['id']:n for n in data['symbols']};scope=call['scope'];base=call['base'];attr=call['attr'];visited=set();evidence=[call['evidence_id']]
 if not base:return None,evidence,'Dynamic callee expression'
 while scope is not None:
  if scope=='opaque' or scope in visited:return None,evidence,'Dynamic or nonlocal binding'
  visited.add(scope)
  if symbols.get(scope,{}).get('kind')=='class' and scope!=call['scope']:
   scope=data['scopes'].get(scope);continue
  matches=[b for b in data['bindings'] if b['scope']==scope and b['name']==base]
  if matches:
   evidence += [b['evidence_id'] for b in matches]
   if len(matches)!=1:return None,evidence,'Rebound or ambiguous lexical name'
   b=matches[0]
   if b['kind']=='symbol' and not attr:
    n=symbols[b['target']];return n['id'],evidence+[n['evidence_id']],'Direct lexical function reference'
   if b['kind']!='import':return None,evidence,'Parameter, assignment, method or unresolved binding'
   destination=module_path(data,b,all_data)
   if not destination:return None,evidence,'Imported module is external, ambiguous or not indexed'
   target=all_data[destination]
   name=b['symbol'] if b['symbol'] and not attr else (attr if not b['symbol'] else '')
   roots=[n['id'] for n in target['symbols'] if n['kind']=='module']
   matches=[b for b in target['bindings'] if b['scope'] in roots and b['name']==name]
   evidence += [b['evidence_id'] for b in matches]
   if len(matches)!=1 or matches[0]['kind']!='symbol':return None,evidence,'Target definition is missing, rebound or re-exported'
   node=next(n for n in target['symbols'] if n['id']==matches[0]['target'])
   if Path(destination).suffix!='.py' and not node.get('exported'):return None,evidence,'Imported JavaScript symbol is not explicitly exported'
   return node['id'],evidence+[node['evidence_id']],'Imported lexical function reference'
  scope=data['scopes'].get(scope)
 return None,evidence,'Name is outside the supported lexical profile'

def inventory(all_data):
 rows=[]
 for path,data in sorted(all_data.items()):
  for c in data['calls']:
   target,evidence,why=resolve(data,c,all_data)
   rows.append(dict(c,path=path,target=target,evidence=list(dict.fromkeys(evidence)),reason=why))
 return sorted(rows,key=lambda c:(c['path'],c['line'],c['column']))

def packet(all_data,site_ids):
 if not isinstance(site_ids,list) or not 1<=len(site_ids)<=MAX_SITES or len(set(site_ids))!=len(site_ids):raise InvalidUpdate('API batch requires 1..3 distinct call sites')
 all_sites={c['id']:c for c in inventory(all_data)}
 if not set(site_ids)<=set(all_sites):raise InvalidUpdate('Unknown API call site')
 sites=[all_sites[i] for i in site_ids];nodes={n['id']:dict(n,path=path) for path,d in all_data.items() for n in d['symbols']}
 selected={c['caller'] for c in sites}|{c['target'] for c in sites if c['target']}
 for n in nodes.values():
  if len(selected)>=8:break
  if n['kind']=='function':selected.add(n['id'])
 evidence={x['evidence_id']:dict(x,path=path) for path,d in all_data.items() for group in ('symbols','bindings','calls') for x in d[group]}
 binding_rows={}
 for c in sites:
  d=all_data[c['path']];chain=[];scope=c['scope']
  while scope is not None and scope!='opaque' and scope not in chain:
   chain.append(scope);scope=d['scopes'].get(scope)
  for binding in d['bindings']:
   if binding['scope'] in chain and binding['name']==c['base']:
    binding_rows[binding['evidence_id']]=dict(binding,path=c['path'])
 needed={eid for c in sites for eid in c['evidence']}|{nodes[n]['evidence_id'] for n in selected}|set(binding_rows)
 # Target resolutions are host-only. The model sees syntax, bindings, definitions.
 return {'bindings':[{'name':b['name'],'kind':b['kind'],'scope':b['path']+'::'+nodes[b['scope']]['name'],'target':b.get('target',''),'module':b.get('module',''),'imported_name':b.get('symbol',''),'evidence':eid} for eid,b in sorted(binding_rows.items())],
         'nodes':[{'id':n,'name':nodes[n]['name'],'path':nodes[n]['path'],'line':nodes[n]['line'],'kind':nodes[n]['kind']} for n in sorted(selected)],
         'sites':[{'id':c['id'],'caller':c['caller'],'expression':c['expression'],'path':c['path'],'line':c['line'],'evidence':c['evidence_id']} for c in sites],
         'evidence':[{'id':eid,'path':evidence[eid]['path'],'line':evidence[eid]['line'],'text':evidence[eid]['quote']} for eid in sorted(needed)]}

def assess(all_data,proposal):
 expected={c['id']:c for c in inventory(all_data)};results={}
 for key,c in proposal['claims'].items():
  actual=expected.get(key)
  if not actual:results[key]={'verdict':'contradicted','reason':'No such call site'};continue
  if c['source']!=actual['caller'] or c['relation']!='calls':verdict='contradicted';why='Caller or relationship differs from syntax'
  elif actual['target'] is None:
   verdict='insufficient_evidence';why=actual['reason']
  elif c['target']==actual['target']:verdict='supported';why=actual['reason']
  else:verdict='contradicted';why='Target differs from the resolved lexical binding'
  results[key]={'verdict':verdict,'reason':why,'expected_target':actual['target'],'evidence':actual['evidence']}
 return results

def compare(candidate,review):
 verdicts={}
 for site,observed in review['observations'].items():
  proposed=candidate['claims'][site]
  if not observed['target']:verdict='insufficient_evidence'
  elif observed['source']==proposed['source'] and observed['target']==proposed['target']:verdict='supported'
  else:verdict='contradicted'
  verdicts[site]={'verdict':verdict,'evidence':observed['evidence'],'reason':observed['reason']}
 return {'verdicts':verdicts}

def decision(host,review):
 issues=[]
 for site,h in host.items():
  opinion=review['verdicts'][site]['verdict']
  if h['verdict']=='contradicted':issues.append(site+': host rejected relationship')
  elif opinion!=h['verdict']:issues.append(site+': reviewer/host disagreement')
 return {'admitted':not issues,'errors':issues,'verified_edges':[s for s,h in host.items() if h['verdict']=='supported' and review['verdicts'][s]['verdict']=='supported'],
         'unresolved':[s for s,h in host.items() if h['verdict']=='insufficient_evidence'],'meaning':'Static lexical references only; no runtime-execution guarantee'}
