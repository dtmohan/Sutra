"""One request per fresh process; governed models, edits, tests and diagnostics."""
import argparse
import http.client
import json
from pathlib import Path
import sys
import uuid
from context_manager import InvalidUpdate, canonical, sha, CONSTRAINT_KEYS
from invocation_gateway import InvocationLedger, InvocationGateway
from local_model import LocalModel
from paging import PageFault, Budget
from review_role import SYSTEM, compact, review_schema


def ollama(path, body=None):
    c=http.client.HTTPConnection('127.0.0.1',11434,timeout=10)
    try:
        c.request('POST' if body is not None else 'GET',path,
                  json.dumps(body) if body is not None else None, {'Content-Type':'application/json'})
        r=c.getresponse(); raw=r.read()
        if r.status!=200: raise InvalidUpdate('Local Ollama: HTTP '+str(r.status))
        return json.loads(raw)
    except OSError as exc:
        raise InvalidUpdate('Cannot reach Ollama at 127.0.0.1:11434. Start the Ollama app or run ollama serve, then choose Retry model discovery in Sutra Settings.') from exc
    finally: c.close()


def safe_path(workspace, name):
    workspace=workspace.resolve()
    if not isinstance(name,str) or not name or Path(name).is_absolute() or '..' in Path(name).parts or '\\' in name:
        raise InvalidUpdate('Use a relative workspace path')
    if str(Path(name))!=name:raise InvalidUpdate('Use a canonical relative path without dot or repeated-slash aliases')
    target=(workspace/name).resolve()
    if workspace not in target.parents: raise InvalidUpdate('Path escapes workspace')
    return target


def save_contract(s, session, data, workspace):
    goal=data.get('goal','').strip(); constraints=data.get('constraints',{})
    if not goal or len(goal)>1800: raise InvalidUpdate('Goal must be 1..1800 characters')
    if not isinstance(constraints,dict) or set(constraints)-CONSTRAINT_KEYS: raise InvalidUpdate('Unsupported constraint')
    paths=constraints.get('allowed_paths',[])
    if not isinstance(paths,list) or not 1<=len(paths)<=8: raise InvalidUpdate('Choose 1..8 exact workspace paths')
    if constraints.get('review_only') is True:
        from repository_tools import normalized
        for name in paths: normalized(name,root=True)
        if any(constraints.get(k) is not False for k in ('edits_allowed','tests_allowed','network_allowed','dependencies_allowed')):
            raise InvalidUpdate('Architecture review requires explicit read-only permissions')
        if constraints.get('auto_review'):
            from review_budget import allocations
            allocations(constraints)
        elif type(constraints.get('max_tools')) is not int or not 1<=constraints['max_tools']<=32:
            raise InvalidUpdate('Repository tool budget must be 1..32')
    else:
        for name in paths: safe_path(workspace,name)
    if type(constraints.get('network_allowed')) is not bool or type(constraints.get('dependencies_allowed')) is not bool:
        raise InvalidUpdate('Explicit network and dependency permissions are required')
    if constraints.get('native_task'):
        from review_budget import allocations
        allocations(constraints)
        if constraints.get('review_only') or constraints.get('auto_review') or constraints['network_allowed'] or constraints['dependencies_allowed']:
            raise InvalidUpdate('Native coding scope forbids network, dependencies and review-mode flags')
    for name in ('edits_allowed','tests_allowed'):
        if name in constraints and type(constraints[name]) is not bool:raise InvalidUpdate('Action permissions must be Boolean')
    if constraints.get('edits_allowed') or constraints.get('tests_allowed'):
        if type(constraints.get('max_actions')) is not int or not 1<=constraints['max_actions']<=32:
            raise InvalidUpdate('Action budget must be 1..32')
    if constraints.get('tests_allowed'):
        tests=constraints.get('test_paths',[])
        if not isinstance(tests,list) or not tests or not set(tests)<=set(paths) or any(not p.endswith('.py') for p in tests):
            raise InvalidUpdate('Test paths must be in-scope Python unittest files')
        if type(constraints.get('test_timeout_ms')) is not int or not 100<=constraints['test_timeout_ms']<=120000:
            raise InvalidUpdate('Test timeout must be 100..120000 ms')
    values={'task_id':session,'goal':goal,**constraints};ops=[];lines=[]
    for key,value in values.items():
        if type(value) is bool: value=str(value).lower()
        elif isinstance(value,list): value=','.join(value)
        else: value=str(value)
        quote=key+': '+value;lines.append(quote);ops.append({'key':key,'value':value,'quote':quote})
    # This structured form is a direct user instruction, not model extraction.
    # Existing constraints cannot disappear merely because a UI omits a field.
    old=s.snapshot(session)['constraints']
    if set(old)-set(constraints): raise InvalidUpdate('Preserve existing constraint fields when updating the contract')
    s.apply_user_patch(session,uuid.uuid4().hex,'\n'.join(lines),ops,s.snapshot(session)['revision'])
    return s.current_contract(session)


def sync_sources(s,session,workspace):
    if s.snapshot(session)['constraints'].get('review_only'):return []
    rows=[]
    for name in s.snapshot(session)['constraints']['allowed_paths']:
        path=safe_path(workspace,name)
        if not path.is_file():
            rows.append({'path':name,'status':'missing','pages':[]});continue
        raw=path.read_bytes()
        if len(raw)>64000: raise InvalidUpdate(name+': file exceeds 64 KB prototype limit; select smaller files')
        text=raw.decode('utf-8');digest=sha(text)
        try:doc=s.document(session,name)
        except PageFault:doc=None
        # Refresh expired pages explicitly; changed documents create new versions.
        changed=not doc or doc['digest']!=digest
        if changed:
            doc=s.put_document(session,name,text,authority='file',source={'path':str(path),'sha256':digest},
                               expected_version=doc['version'] if doc else 0)
        pages=[];start=0;number=0
        while start<len(text):
            end=min(start+900,len(text))
            if end<len(text):
                newline=text.rfind('\n',start,end)
                if newline>start:end=newline+1
            key=name+'#'+str(number+1)
            try:old=s.page(session,key)
            except PageFault:old=None
            if changed or not old or s.status(old) not in ('current','aging'):
                p=s.put_unit(session,key,name,start,end,document_version=doc['version'],group='code',
                    expected_version=old.version if old else 0,subject=name,ttl_s=86400)
            else:p=old
            pages.append({'id':key,'version':p.version,'path':name,
                          'lines':[text.count('\n',0,start)+1,text.count('\n',0,end)+1]})
            start=end;number+=1
        rows.append({'path':name,'status':'current','pages':pages})
    return rows


def state(s,meta,workspace):
    session=meta.get('active')
    if not session:return {'session':None,'workspace':str(workspace),'model':meta.get('model','local-assistant:latest')}
    if s.snapshot(session)['constraints'].get('review_only'):
        import repository_tools as repo
        try:repo.catalog(s,session)
        except InvalidUpdate as exc:
            return {'session':session,'workspace':str(workspace),'model':meta.get('model','local-assistant:latest'),
                    'contract':s.current_contract(session),'sources':[],'checkpoint':None,'last_result':None,
                    'architecture':None,'warning':'Catalog unavailable: '+str(exc)+'. Continue after recovery or allocation review; the contract and used budget are preserved.'}
    if s.snapshot(session)['constraints'].get('auto_review'):
        import auto_review,repository_tools as repo
        return {'session':session,'workspace':str(workspace),'model':meta.get('model','qwen3.6:35b'),'contract':s.current_contract(session),'sources':[],'checkpoint':None,'last_result':None,'auto':auto_review.status(s,meta,workspace),'architecture':{'catalog_files':len(repo.catalog(s,session)),'catalog':repo.catalog(s,session),'budget':repo.usage(s,session),'inspected_files':[]},'ledger_valid':s.verify_invocations(session,complete=False)}
    if s.snapshot(session)['constraints'].get('review_only'):
        from architecture_runtime import state as architecture_state
        import repository_tools as repo
        # Upgrade history remains evidence, but old adapter receipts cannot
        # authorize a new mapper. Keep the task visible so the UI is usable.
        rows=repo.records(s,session)
        if rows and not any(r['operation']['kind']=='catalog' and r['operation']['logic_hash']==s.logic_hash and r['operation']['contract_hash']==s.origin(session)['contract_hash'] for r in rows):
            return {'session':session,'workspace':str(workspace),'model':meta.get('model','local-assistant:latest'),'contract':s.current_contract(session),'sources':[],'checkpoint':None,'last_result':None,'architecture':None,'warning':'Adapter version changed. Historical receipts are preserved but withheld. Start an architecture task to create a fresh catalog under this version.'}
        return architecture_state(s,meta,workspace)
    sources=sync_sources(s,session,workspace)
    checkpoint=meta.get('checkpoint');note=None;warning=None
    if checkpoint:
        try:note=s.load_result(session,checkpoint)
        except (PageFault,InvalidUpdate) as exc:warning='Previous working note withheld: '+str(exc);checkpoint=None
    from host_actions import execution_context
    from native_access import read_scope
    return {'session':session,'workspace':str(workspace),'model':meta.get('model','local-assistant:latest'),
            'contract':s.current_contract(session),'sources':sources,'checkpoint':checkpoint,
            'last_result':note,'warning':warning,'ledger_valid':s.verify_invocations(session,complete=False),
            'execution':execution_context(s,session,workspace),'read_scope':read_scope(s,session)}


def dispatch(s,meta,workspace,root,request,provider=None):
    st=state(s,meta,workspace);session=st['session']
    if not session:raise InvalidUpdate('Create a task first')
    if st.get('architecture'):raise InvalidUpdate('Use the Architecture review entry point for this read-only task')
    prompt=request.get('prompt','')
    if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>1800:raise InvalidUpdate('Request must be 1..1800 characters')
    chosen=request.get('pages',[])
    catalog=[p for row in st['sources'] for p in row['pages']]
    ids={p['id'] for p in catalog}
    if not isinstance(chosen,list) or not 1<=len(chosen)<=4 or len(set(chosen))!=len(chosen) or not set(chosen)<=ids:
        raise InvalidUpdate('Select 1..4 current source pages from the table of contents')
    model_name=request.get('model',meta.get('model','local-assistant:latest'))
    if request.get('op')=='work' and request.get('mode') in ('edit','test'):
        if st['execution'].get('pending_actions'):raise InvalidUpdate('An earlier action is pending; inspect it before continuing')
        from karma_adapter import budget_decision
        c=st['contract']['contract']['constraints'];permission='edits_allowed' if request['mode']=='edit' else 'tests_allowed'
        if c.get(permission) is not True:raise InvalidUpdate('Requested action is not permitted by the contract')
        measured=budget_decision(st['execution']['actions_used'],c['max_actions'])
        if measured['decision']!='ALLOW':
            with s.db:s._audit('ACTION_PLAN_DENIED',{'session':session,'budget_kernel':measured})
            raise InvalidUpdate('Action budget exhausted: '+measured['decision'])
    if provider is None:
        from monitoring import require_resources
        hardware=require_resources(root)
        names={m['name'] for m in ollama('/api/tags')['models']}
        if model_name not in names or 'cloud' in model_name.lower():raise InvalidUpdate('Choose an installed local model')
        show=ollama('/api/show',{'model':model_name})
        if show.get('remote_model') or show.get('remote_host'):raise InvalidUpdate('Remote models are outside this local adapter')
        provider=LocalModel(root/'calls',11434,model_name)
        if request.get('native_route'):
            from paging import Budget
            provider.budget=Budget(16384,2048,256);provider.keep_alive=0
    parent=st['checkpoint'];pages=[]
    for key in chosen:
        p=s.read_page(session,key)
        page={'id':key,'version':p.version,'hash':p.content_hash,'authority':p.authority,**p.payload}
        if request.get('op')=='work':page['path']=next(row['path'] for row in st['sources'] if any(item['id']==key for item in row['pages']))
        pages.append(page)
    # TOC is metadata only, bounded to the first 24 current pages. The UI exposes
    # every page and lets the user explicitly page in additional bodies.
    payload={'contract':s.pinned(session),'request':prompt,'pages':pages,
             'toc':[{'id':p['id'],'lines':p['lines']} for p in catalog[:24]],
             'toc_total':len(catalog),'derived_from':[parent] if parent else [],
             'handoff':compact(st['last_result']) if parent else None}
    if request.get('native_route'):payload['native_route']=request['native_route']
    if request.get('conversation_id'):
        from conversation import workspace_handoff
        payload['conversation_handoff']=workspace_handoff(s,session,request['conversation_id'],prompt)
    role='coder' if request.get('op')=='work' else 'reviewer';system=SYSTEM
    if role=='coder':
        from coding_role import SYSTEM as CODER_SYSTEM, coding_schema
        from host_actions import files_at
        system=CODER_SYSTEM;payload.update(mode=request.get('mode','review'),workspace=str(workspace),execution=st['execution'],
            source_versions={k:v['sha256'] for k,v in files_at(s,session,workspace).items()})
        schema=coding_schema(payload)
    else:schema=review_schema(payload)
    provider.budget.check(system,payload,schema)
    run=s.begin_run(session,prompt,[],None,max_calls=3,max_repairs=2)
    meta['model']=model_name;last=None
    for attempt in range(3):
        s.reserve(run,'ATTEMPT',{'attempt':attempt})
        if provider.__class__ is LocalModel:
            from monitoring import require_resources
            try:
                hardware=require_resources(root)
                with s.db:s._audit('RESOURCE_GATE',{'run_id':run,'measurement':hardware})
            except InvalidUpdate as exc:
                s.event(run,'RESOURCE_WAIT',{'reason':str(exc)})
                raise
        charge=provider.budget.check(system,payload,schema)
        delivery=s.prepare_delivery(session,payload,charge)
        last=InvocationGateway(s.path,run,provider).invoke(role,uuid.uuid4().hex,system,payload,schema,delivery)
        if last['admission']['admitted']:
            s.event(run,'ALLOW',{'reason':'validated_review','invocation_id':last['invocation_id']})
            meta['checkpoint']=last['invocation_id'];break
        s.event(run,'NACK',{'reason':'invalid_review','invocation_id':last['invocation_id'],'errors':last['admission']['errors']})
        if attempt==2 or last.get('blocked'):
            s.event(run,'FIN',{'reason':'repair_exhausted_or_transport','errors':last['admission']['errors']});break
        s.event(run,'BRIDGE',{'reason':'bounded_repair','attempt':attempt+1})
        payload['repair']={'errors':[e[:140] for e in last['admission']['errors'][:2]]}
    last.pop('record',None);last.pop('model_proposal',None)
    last['run_id']=run;last['model']=model_name;last['handoff_from']=parent
    return {'result':last,'state':state(s,meta,workspace)}


def handle(root,workspace,request):
    from platform_support import lock_file
    from lifecycle import reconcile
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True)
    with lock_file(root/'bridge.lock'):
        metadata=root/'workspace.json'
        if metadata.exists() and json.loads(metadata.read_text())['workspace']!=str(Path(workspace).resolve()):
            raise InvalidUpdate('Storage belongs to a different workspace')
        if request.get('op')=='models':
            from model_catalog import catalog
            return catalog(ollama)
        s=InvocationLedger(root/'state.sqlite')
        try:
            if request.get('op')=='orchestrate':
                from orchestration_flow import handle as orchestrate
                from storage_policy import prune,require_space
                prune(root);require_space(root)
                meta=json.loads(metadata.read_text()) if metadata.exists() else {'workspace':str(Path(workspace).resolve())}
                if not metadata.exists():metadata.write_text(canonical(meta))
                # Classification is tool-free; later events reconcile committed work.
                if request.get('event')!='start':reconcile(s,workspace)
                try:return orchestrate(s,meta,Path(workspace).resolve(),root,request)
                except BaseException:
                    if request.get('event')!='start':reconcile(s,workspace)
                    raise
            if request.get('op')=='conversation_read':
                from conversation import read
                return read(s,request.get('invocation_id'))
            if request.get('op')=='conversation':
                # No workspace reconciliation, source refresh or report loading.
                from conversation import answer
                from storage_policy import prune,require_space
                prune(root);require_space(root)
                meta=json.loads(metadata.read_text()) if metadata.exists() else {'workspace':str(Path(workspace).resolve())}
                if not metadata.exists():metadata.write_text(json.dumps(meta))
                return answer(s,meta,root,request)
            events=reconcile(s,workspace)
            from storage_policy import prune,require_space
            prune(root,s)
            if request.get('op') not in ('state','models','quarantine','preview','auto_report','switch'):require_space(root)
        finally:s.close()
        try:
            result=_handle(root,workspace,request)
            from calibration import overview
            s=InvocationLedger(root/'state.sqlite')
            try:
                meta=json.loads((root/'workspace.json').read_text()) if (root/'workspace.json').exists() else {}
                snapshot=overview(s,meta.get('active'))
                result['calibration']=snapshot
                if 'state' in result:result['state']['calibration']=snapshot
                if meta.get('chat'):
                    try:
                        last=s.load_result(meta['active'],meta['chat']['checkpoint'])
                        if meta['chat'].get('reviewer_id'):
                            from report_review import published
                            last,_=published(s,meta['active'],meta['chat']['checkpoint'],meta['chat']['reviewer_id'])
                        chat={'answer':last,'route':meta['chat']['route'],'checkpoint':meta['chat']['checkpoint']}
                    except (InvalidUpdate,PageFault):chat={'answer':None,'warning':'Previous chat answer is withheld; source, contract or calibration changed.'}
                    if 'state' in result:result['state']['chat']=chat
                    else:result['chat']=chat
            finally:s.close()
            if events:result['recovery']=events
            return result
        except BaseException:
            s=InvocationLedger(root/'state.sqlite')
            try:reconcile(s,workspace)
            finally:s.close()
            raise


def _handle(root,workspace,request):
    root=root.resolve();workspace=workspace.resolve()
    root.mkdir(parents=True,exist_ok=True);metadata=root/'workspace.json'
    meta=json.loads(metadata.read_text()) if metadata.exists() else {'workspace':str(workspace)}
    if meta['workspace']!=str(workspace):raise InvalidUpdate('Storage belongs to a different workspace')
    s=InvocationLedger(root/'state.sqlite')
    try:
        from host_actions import initialize
        initialize(s)
        op=request.get('op')
        if meta.get('active') and not str(op).startswith('calibration_') and op not in ('new','review_new','auto_new','models') and s.snapshot(meta['active'])['constraints'].get('review_only'):
            import repository_tools as repo
            try:repo.catalog(s,meta['active'])
            except InvalidUpdate:
                # Rebuild missing/interrupted or old-adapter catalogs under the
                # SAME contract and allocations. Never reset usage on recovery.
                try:repo.execute(s,meta['active'],workspace,root,'catalog',resource_check=False)
                except InvalidUpdate:
                    if op!='state':raise
        if op=='models':
            from model_catalog import catalog
            return catalog(ollama)
        if str(op).startswith('calibration_'):
            import calibration
            if op=='calibration_register':result={'registered':calibration.register(s,request['profile'])}
            elif op=='calibration_list':result=calibration.overview(s,meta.get('active'))
            elif op=='calibration_revoke':
                calibration.revoke(s,request['profile_ref'],request['reason']);result={'revoked':request['profile_ref']}
            elif op=='calibration_bind':
                if not meta.get('active'):raise InvalidUpdate('Create a task before selecting calibration profiles')
                result={'binding':calibration.bind(s,meta['active'],request['profiles'],request['expected_hash'])}
            elif op=='calibration_evaluate':
                if not meta.get('active'):raise InvalidUpdate('Create a task before evaluating a calibration sample')
                from smriti_adapter import SmritiAdapter
                from karma_adapter import KarmaAdapter
                profile=calibration.load(s,request['profile_ref'])
                adapter=SmritiAdapter() if profile['adapter']=='smriti' else KarmaAdapter()
                result={'evaluation':adapter.uncertain(s,meta['active'],request['profile_ref'],request['sample'])}
            else:raise InvalidUpdate('Unknown calibration operation')
        elif op in ('review_new','auto_new'):
            # Only the direct user form/command may establish scope; external agent tools cannot.
            for key in ('api_candidate','api_review','api_position','static_map','auto','chat'):meta.pop(key,None)
            meta['active']='review-'+uuid.uuid4().hex[:12];s.create(meta['active']);meta.pop('checkpoint',None)
            data={'goal':request.get('goal',''),'constraints':{'allowed_paths':request.get('roots',['.']),
                'review_only':True,'network_allowed':False,'dependencies_allowed':False,'edits_allowed':False,
                'tests_allowed':False,'max_tools':request.get('max_tools',12),'output_format':'architecture with diagram'}}
            if op=='auto_new':
                from review_budget import DEFAULTS
                data['constraints'].update(auto_review=True,**{key:request.get(key,value) for key,value in DEFAULTS.items()})
                meta['auto']={'pages':[],'failures':{},'status':'ready'}
            save_contract(s,meta['active'],data,workspace)
            # Persist identity before the first interruptible repository action.
            temp=metadata.with_suffix('.tmp');temp.write_text(canonical(meta));temp.replace(metadata)
            from repository_tools import execute
            execute(s,meta['active'],workspace,root,'catalog',resource_check=False)
            if request.get('conversation_id'):
                from native_access import bind
                bind(s,meta['active'],request['conversation_id'])
            result=state(s,meta,workspace)
        elif op=='native_permissions':
            from native_access import grant
            grant(s,meta,workspace,request)
            result=state(s,meta,workspace)
        elif op=='chat_answer':
            if not meta.get('active') or not s.snapshot(meta['active'])['constraints'].get('auto_review'):raise InvalidUpdate('Start a Karma chat review task first')
            from chat_runtime import answer
            result=answer(s,meta,workspace,root,request);result['state']=state(s,meta,workspace)
        elif op=='native_work':
            from native_runtime import turn
            result=turn(s,meta,workspace,root,request)
        elif op in ('native_preview','native_execute'):
            from native_runtime import binding
            sid=binding(s,meta,request)
            from host_actions import preview,execute
            result=preview(s,sid,request['invocation_id'],workspace) if op=='native_preview' else {'action_receipt':execute(s,sid,request['invocation_id'],workspace,root),'state':state(s,meta,workspace)}
        elif op=='auto_step':
            from auto_review import step
            result=step(s,meta,workspace,root,request);result['state']=state(s,meta,workspace)
        elif op=='auto_report':
            from auto_review import report
            result={'report':report(s,meta,workspace),'state':state(s,meta,workspace)}
        elif op in ('static_build','static_explain'):
            if not meta.get('active'):raise InvalidUpdate('Create a read-only architecture task first')
            from static_runtime import dispatch as static_dispatch
            result=static_dispatch(s,meta,workspace,root,request)
            result['state']=state(s,meta,workspace)
        elif op in ('api_build','api_verify'):
            if not meta.get('active'):raise InvalidUpdate('Create a read-only architecture task first')
            from api_runtime import build,verify
            result=(build if op=='api_build' else verify)(s,meta,workspace,root,request)
            result['state']=state(s,meta,workspace)
        elif op=='architecture':
            if not meta.get('active'):raise InvalidUpdate('Create an architecture review in Karma first')
            from architecture_runtime import dispatch as architecture_dispatch
            result=architecture_dispatch(s,meta,workspace,root,request)
        elif op=='repository_tool':
            if not meta.get('active'):raise InvalidUpdate('Create an architecture review in Karma first')
            from repository_tools import execute
            result={'tool_receipt':execute(s,meta['active'],workspace,root,request['kind'],target=request.get('target',''),query=request.get('query',''),line=request.get('line',1),cursor=request.get('cursor')),'state':state(s,meta,workspace)}
        elif op in ('new','save'):
            if op=='save' and meta.get('active') and s.snapshot(meta['active'])['constraints'].get('review_only'):
                raise InvalidUpdate('Start a new architecture review to change its scope or budget')
            if op=='new' or not meta.get('active'):
                for key in ('api_candidate','api_review','api_position','static_map','auto','chat'):meta.pop(key,None)
                meta['active']='task-'+uuid.uuid4().hex[:12];s.create(meta['active']);meta.pop('checkpoint',None)
            save_contract(s,meta['active'],request,workspace)
            result=state(s,meta,workspace)
        elif op=='state':result=state(s,meta,workspace)
        elif op=='switch':
            model=request['model']
            if model not in {m['name'] for m in ollama('/api/tags')['models']} or 'cloud' in model.lower():raise InvalidUpdate('Unknown local model')
            with s.db:s._audit('MODEL_SWITCH',{'session':meta.get('active'),'from':meta.get('model'),'to':model})
            meta['model']=model;result=state(s,meta,workspace)
        elif op in ('review','work'):result=dispatch(s,meta,workspace,root,request)
        elif op=='preview':
            from host_actions import preview
            result=preview(s,meta['active'],request['invocation_id'],workspace)
        elif op=='execute':
            from host_actions import execute
            result={'action_receipt':execute(s,meta['active'],request['invocation_id'],workspace,root),
                    'state':state(s,meta,workspace)}
        elif op=='quarantine':
            s.quarantine_invocation(meta['active'],request['invocation_id'],'Explicit host quarantine')
            result=state(s,meta,workspace)
        else:raise InvalidUpdate('Unknown operation')
        temp=metadata.with_suffix('.tmp');temp.write_text(canonical(meta));temp.replace(metadata)
        return result
    finally:s.close()


def main():
    p=argparse.ArgumentParser();p.add_argument('--storage',required=True);p.add_argument('--workspace',required=True);p.add_argument('--dashboard-stream',action='store_true');args=p.parse_args()
    root=Path(args.storage).resolve();workspace=Path(args.workspace).resolve();root.mkdir(parents=True,exist_ok=True)
    if args.dashboard_stream:
        # Persistent diagnostics worker: no model, source reads, mutation or
        # recovery. Each sample opens/closes a read-only SQLite connection.
        from monitoring import dashboard
        while True:
            raw=sys.stdin.readline(128)
            if not raw:return
            try:
                if raw.strip()!='{}':raise InvalidUpdate('Dashboard accepts empty diagnostic requests only')
                answer={'ok':True,'data':dashboard(root)}
            except Exception as exc:answer={'ok':False,'error':str(exc)}
            print(json.dumps(answer),flush=True)
        return
    try:
        if not workspace.is_dir():raise InvalidUpdate('Open a local workspace folder')
        raw=sys.stdin.read(200001)
        if len(raw)>200000:raise InvalidUpdate('Request too large')
        request=json.loads(raw)
        if request.get('op')=='dashboard':
            from monitoring import dashboard
            print(json.dumps({'ok':True,'data':dashboard(root)}));return
        from lifecycle import deadline
        with deadline():
            result=handle(root,workspace,request)
        print(json.dumps({'ok':True,'data':result}))
    except (Exception,KeyboardInterrupt) as exc:
        print(json.dumps({'ok':False,'error':type(exc).__name__+': '+str(exc)}));sys.exit(1)


if __name__=='__main__':
    from lifecycle import Interrupted
    try:main()
    except Interrupted as exc:
        print(json.dumps({'ok':False,'error':str(exc)+'; pending work reconciled on recovery'}));sys.exit(1)
