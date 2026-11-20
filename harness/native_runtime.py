"""Native chat adapter: host-selected pages/model, existing coder/action gateway."""
import json,re
from context_manager import InvalidUpdate,canonical,sha
from karma_adapter import KarmaAdapter

def binding(s,meta,request):
    sid=meta.get('active')
    if sid!=request.get('session') or not sid:raise InvalidUpdate('Native task changed; arm the current task again')
    if s.origin(sid)['contract_hash']!=request.get('contract_hash'):raise InvalidUpdate('Native contract changed; re-arm required')
    from calibration import current as calibration_binding
    if calibration_binding(s,sid,usable=True)['hash']!=request.get('calibration_hash'):raise InvalidUpdate('Native calibration changed; re-arm required')
    return sid

def check_route(s,sid,p):
    from review_reasoning import check
    check(p)
    route=p['native_route'];record=route['record']
    row=s.db.execute('SELECT seq,event,payload_json,previous_hash,event_hash FROM page_audit WHERE event_hash=?',(route['receipt'],)).fetchone()
    if not row or row[1]!='NATIVE_ROUTE' or sha(canonical(list(row[:4])))!=row[4] or json.loads(row[2])!=record:raise InvalidUpdate('Native route receipt mismatch')
    if record['session']!=sid or record['contract_hash']!=p['contract']['contract_hash'] or record['request_hash']!=sha(p['request']):raise InvalidUpdate('Native routing identity changed')
    c=record['choice'];expected=KarmaAdapter().route(p['request'],c['models'],c['mode'],c['signals'])
    if c!=expected:raise InvalidUpdate('Incorrect native model routing decision')

def turn(s,meta,workspace,root,request,provider=None):
    sid=binding(s,meta,request)
    if not s.snapshot(sid)['constraints'].get('native_task'):raise InvalidUpdate('Arm a native coding task first')
    from vscode_bridge import state,dispatch
    st=state(s,meta,workspace);prompt=request.get('prompt','')
    words=set(re.findall(r'[A-Za-z_][A-Za-z_0-9]{2,}',prompt.lower()))
    pages=[p for f in st['sources'] for p in f['pages']]
    def score(p):
        text=s.read_page(sid,p['id']).payload['text'].lower()
        return (-sum(w in (p['path'].lower()+' '+text) for w in words),p['path'],p['id'])
    requested=request.get('pages',[])
    by_id={p['id']:p for p in pages}
    if (not isinstance(requested,list) or any(not isinstance(pid,str) for pid in requested)
            or len(set(requested))!=len(requested) or not set(requested)<=set(by_id)):
        raise InvalidUpdate('Requested source pages are outside the current native scope')
    # Explicit page addresses and exact file names are required evidence, not
    # competitors in a four-page keyword sample. The actual context gate decides
    # whether the complete requested set fits; no pins or permissions are shed.
    named={p['path'] for p in pages if re.search(
        r'(?<![A-Za-z_0-9./-])'+re.escape(p['path'])+r'(?![A-Za-z_0-9/-]|\.[A-Za-z_0-9])',prompt)}
    required=[by_id[pid] for pid in requested]
    required.extend(p for p in pages if p['path'] in named and p['id'] not in requested)
    required_ids={p['id'] for p in required}
    ranked=sorted((p for p in pages if p['id'] not in required_ids),key=score)
    pages=required+ranked[:max(0,4-len(required))]
    if not pages:raise InvalidUpdate('No current source pages in the armed scope')
    choice=KarmaAdapter().route(prompt,request['models'],request.get('tier','auto'),{'source_files':len(st['sources']),'selected_files':len({p['path'] for p in pages}),'unresolved_calls':0})
    if provider is not None and provider.model!=choice['model']:raise InvalidUpdate('Provider differs from native route')
    record={'session':sid,'contract_hash':s.origin(sid)['contract_hash'],'request_hash':sha(prompt),'choice':choice}
    with s.db:s._audit('NATIVE_ROUTE',record)
    route={'record':record,'receipt':s.db.execute('SELECT event_hash FROM page_audit ORDER BY seq DESC LIMIT 1').fetchone()[0]}
    result=dispatch(s,meta,workspace,root,{'op':'work','mode':'auto','prompt':prompt,'model':choice['model'],'pages':[p['id'] for p in pages],'native_route':route,'conversation_id':request.get('conversation_id'),'toc_offset':request.get('toc_offset',0)},provider=provider)
    from review_budget import decision
    result.update(route=choice,native_budget=decision(s,sid));return result
