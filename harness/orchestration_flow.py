"""Host-owned native workflow. The UI renders states and supplies user grants."""
import copy,json,math
from pathlib import Path
from context_manager import InvalidUpdate,ContextBudgetExceeded,canonical,sha
import orchestration as m
from memory_layer import PageFault, fault_details, observe_fault
from review_budget import BudgetStop


def save(root,meta):
    path=Path(root)/'workspace.json';temp=path.with_suffix('.tmp');temp.write_text(canonical(meta));temp.replace(path)

def info(s,fid):
    n=m.node(s,fid)
    if n['kind']!='flow':raise InvalidUpdate('Expected a workflow identity')
    history=m.events(s,fid);data={}
    for e in history:data.update(e['data'])
    return n,data

def move(s,fid,target,kind,**data):
    with s.db:
        result = m.transition(s,fid,target,kind,data)
        if target == 'DONE':
            from execution_sessions import finish
            finish(s, fid)
        return result

def pending_flows(s,workspace,exclude=None):
    return [nid for nid, in s.db.execute(
        "SELECT id FROM orchestration_nodes WHERE json_extract(record_json,'$.kind')='flow' ORDER BY rowid DESC")
        if nid!=exclude and m.node(s,nid).get('workspace')==str(workspace)
        and m.state(s,nid) not in m.TERMINAL]

def response(s,fid,**extra):
    n,data=info(s,fid);history=m.events(s,fid)
    if m.state(s,fid)=='NEED_TOOL_APPROVAL':
        import capabilities
        p=data.get('capability_proposal')
        extra.setdefault('capability_proposal',p)
        extra['capability_settled']=bool(p and capabilities.completed(s,p['id']))
    if m.state(s,fid)=='DONE' and data.get('capability_result'):extra.setdefault('capability_result',data['capability_result'])
    from task_observations import summary
    outcome = extra.get('data', {}).get('result')
    observations = summary(s, data.get('conversation_id'), data.get('invocation_id'),
        ('awaiting_action' if m.state(s,fid)=='AWAITING_ACTION' else 'admitted' if outcome['admission']['admitted'] else 'withheld') if outcome else data.get('observation_delivery'))
    return dict(task_observations=observations,flow_id=fid,state=m.state(s,fid),cursor=history[-1]['receipt'] if history else None,
                conversation_id=data.get('conversation_id'),binding=data.get('binding'),
                git_provenance=data.get('git_provenance'),
                resource_fallback=data.get('resource_fallback'),
                stop=(data.get('workflow_stop') or data.get('mapping_stop')) if m.state(s,fid)=='PAUSED' else None,
                recovery=recovery(s,fid),allocation_version=data.get('allocation_version',0),
                execution_id=data.get('root_node') if data.get('root_node') and m.node(s,data['root_node'])['kind']=='execution' else None,
                children=[{'id':i,'state':m.state(s,i),'files':m.node(s,i)['files']} for i in data.get('children',[])],**extra)

def recovery(s,fid):
    """Inspect incompatibility without accepting or rewriting old commitments."""
    n,data=info(s,fid)
    if m.state(s,fid) in m.TERMINAL or not data.get('root_node'):return None
    parent=m.node(s,data['root_node']);old=parent['binding']
    try:current=m.binding(s,parent['session'])
    except InvalidUpdate:
        return {'kind':'binding_changed','restart_allowed':False,
                'message':'The saved task calibration is no longer usable. Discard this request and establish a valid task before continuing.'}
    changed=[key for key in old if old[key]!=current.get(key)]
    if not changed:
        if (data.get('workflow_stop') or {}).get('status') != 'provenance_changed':
            return None
        return {'kind': 'provenance_changed', 'restart_allowed': True,
                'message': 'Source changed during this review. Restart with this workspace to check fresh source and retain the request, constraints and consumed budget.'}
    policy_only=changed==['logic_hash']
    return {'kind':'policy_changed' if policy_only else 'binding_changed',
            'restart_allowed':policy_only and bool(s.snapshot(parent['session'])['constraints'].get('auto_review')),
            'message':('Sutra was updated after this review started. Its previous worker results cannot be resumed under the new policy. '
                       'Restart review keeps the original request, task constraints and consumed budget, releases unused child reservations, and reads sources again.'
                       if policy_only else 'The task contract or calibration changed. Discard this saved request and submit a new request under the current task.'),
            'changed':changed}

def identity(s,sid):
    return {k:v for k,v in m.binding(s,sid).items() if k!='logic_hash'}

def plan(s,meta,workspace,prompt,conversation_id,parent_id=None):
    """Measured partition proposal; every allocation still needs kernel ALLOW.

    Reserve worst-case repair capacity for each child and parent synthesis.
    File count is a workload signal, not a claim of semantic complexity.
    """
    import auto_review as auto
    import static_analysis as analysis
    parent=m.checked(s,parent_id) if parent_id else m.task(s,meta['active']);s.db.commit()
    if m.state(s,parent['id'])=='WAITING_CHILDREN':
        fork=[e for e in m.events(s,parent['id']) if e['kind']=='FORK'][-1]
        children=[m.node(s,i) for i in fork['data']['children']]
        if all(c['context']['request']==prompt and c['context'].get('conversation_id')==conversation_id for c in children):return parent,children,fork['data']['signals']
        raise InvalidUpdate('Another parent request owns the outstanding children')
    pages,_=auto.current_pages(s,meta,workspace);versions={};calls=0;unresolved=0
    for page in pages:
        data=analysis.load(s,meta['active'],workspace,page['receipt'])
        versions.update(data['versions']);calls+=len(data['graph']['calls']);unresolved+=sum(c['target'] is None for c in data['graph']['calls'])
    s.db.commit();used=m.spent(s,parent['id'])
    left={k:max(0,v-used[k]) for k,v in m.effective_limits(s,parent).items()}
    # One fresh map plus up to three calls per participant. Actual use is charged.
    minimum={'calls':3,'tokens':3*16384,'seconds':470,'operations':1}
    affordable=min(int(left[k]//v) for k,v in minimum.items())-1
    names=auto.ranked([p for p in versions if Path(p).suffix in auto.CODE],prompt)
    configs=[p for p in versions if Path(p).name=='tsconfig.json']
    count=min(len(names),max(0,affordable))
    signals={'source_files':len(names),'static_calls':calls,'unresolved_calls':unresolved,'affordable_children':max(0,affordable),
             'policy':'source-partition-v1','semantic_complexity_verified':False}
    if count<2:return parent,[],signals
    groups=[configs+names[i::count] for i in range(count)]
    proposals=[]
    for i,files in enumerate(groups):
        # Preserve a parent share for synthesis and fresh source reads.
        limits={k:v/(count+1) for k,v in left.items()}
        if any(v<=0 for v in limits.values()):return parent,[],signals
        permissions=copy.deepcopy(parent['permissions']);permissions.update(read_paths=files,write_paths=[],test_paths=[],network_allowed=False,dependencies_allowed=False)
        proposals.append({'objective':'Review source partition '+str(i+1)+' of '+str(count)+' for: '+prompt,
                          'files':files,'permissions':permissions,'limits':limits})
    context={'request':prompt,'conversation_id':conversation_id,'versions':versions,'goal':parent['goal']}
    # Persist the full source version commitment once; each child gets its subset.
    return parent,m.fork(s,parent['id'],proposals,signals,context),signals

def permission(s,sid,requested):
    from native_access import read_scope
    from repository_tools import scoped
    c=s.snapshot(sid)['constraints'];operation=requested.get('operation','read');paths=requested.get('paths',[])
    roots=read_scope(s,sid)
    if any(not scoped(p,roots) for p in paths):raise InvalidUpdate('Requested file is outside the armed read scope')
    needed=(operation=='edit' and not c.get('edits_allowed')) or (operation=='test' and not c.get('tests_allowed'))
    if operation in ('edit','test') and c.get('native_task') and any(p not in c['allowed_paths'] for p in paths):needed=True
    return {'operation':operation,'paths':list(dict.fromkeys((c['allowed_paths'] if c.get('native_task') else [])+paths)),
            'read_scope':roots,'goal':s.snapshot(sid)['goal']} if needed else None

def budget_pause(s, meta, root, req, fid, phase, stop, kind='CONTEXT_STOPPED', **details):
    """Record the failed gate, then consider a separately admitted reserve draw."""
    move(s, fid, 'PAUSED', kind, workflow_stop=stop, **details)
    save(root, meta)
    if not req.get('armed'):
        return response(s, fid)

    from workspace_allocation import try_amend
    outcome = try_amend(s, fid, meta)
    if not outcome:
        return response(s, fid)
    if outcome['status'] != 'amended':
        stop = dict(stop, automatic_allocation=outcome)
        move(s, fid, 'PAUSED', 'WORKSPACE_RESERVE_STOPPED', workflow_stop=stop)
        return response(s, fid)

    amendment = outcome['amendment']
    dimension = outcome['dimension']
    move(s, fid, phase, 'WORKSPACE_RESERVE_RESUMED', workflow_stop=None, mapping_stop=None)
    return response(s, fid, allocation_amendment=amendment,
                    progress='Sutra added '+dimension.replace('_', ' ')+' from the workspace reserve. Continuing with prior spending retained.')


def handle(s,meta,workspace,root,req,providers=None):
    """Translate measured resource stops at the shared orchestration boundary."""
    from monitoring import ResourceWait
    req=dict(req)
    req.pop('_created_flow_id',None)
    try:
        return _handle(s,meta,workspace,root,req,providers)
    except (BudgetStop,m.OrchestrationStop) as exc:
        fid=req.get('flow_id')
        if not fid or m.state(s,fid) not in ('BINDING','WORKSPACE','NEED_TOOL_APPROVAL','MAPPING','BRANCHING','CHILDREN'):raise
        phase=m.state(s,fid)
        return budget_pause(s, meta, root, req, fid, phase, {
            'status': 'task_budget', 'stage': phase, 'error': str(exc),
            'frame': exc.frame, 'automatic_retry': False})
    except (PageFault, ContextBudgetExceeded) as exc:
        fid=req.get('_created_flow_id') or req.get('flow_id')
        if not fid:raise
        _,data=info(s,fid);phase=m.state(s,fid)
        target=(data.get('workflow_stop') or {}).get('resume_phase') if phase=='PAUSED' else phase
        if target not in ('CLASSIFYING','MAPPING','BRANCHING','CHILDREN','SYNTHESIS','CODING'):raise
        detail=fault_details(exc)
        stop={'status':'memory_fault','stage':target,'resume_phase':target,'error':str(exc),
              'memory_fault':detail,'automatic_retry':False}
        observe_fault(s,(data.get('binding') or {}).get('session',''),exc)
        kind = 'MEMORY_STOPPED'
        if target == 'SYNTHESIS' and detail['code'] == 'source_changed':
            from review_followup import FollowupSourceChanged
            if isinstance(exc, FollowupSourceChanged):
                stop['followup_source_changed'] = detail['page']
                if data.get('resumed_followup_source') == detail['page']:
                    stop['status'] = 'provenance_changed'
                    kind = 'GIT_SNAPSHOT_CHANGED'
        move(s,fid,'PAUSED',kind,workflow_stop=stop)
        save(root,meta)
        return response(s,fid)
    except ResourceWait as exc:
        fid=req.get('_created_flow_id') or req.get('flow_id')
        if not fid:
            # A start can stop during classification after its durable creation.
            # The private bridge serializes requests; match this workspace/request.
            pending=[nid for nid, in s.db.execute("SELECT id FROM orchestration_nodes WHERE json_extract(record_json,'$.kind')='flow' ORDER BY rowid DESC")
                     if m.node(s,nid).get('workspace')==str(workspace) and m.node(s,nid).get('prompt')==req.get('prompt') and m.state(s,nid) not in m.TERMINAL]
            if len(pending)!=1:raise
            fid=pending[0]
        _,data=info(s,fid);phase=m.state(s,fid)
        target=(data.get('workflow_stop') or {}).get('resume_phase') if phase=='PAUSED' else phase
        if target not in ('CLASSIFYING','MAPPING','CHILDREN','SYNTHESIS','CODING','WORKSPACE','NEED_TOOL_APPROVAL'):raise
        from resource_routing import propose
        fallback=propose(s,fid,data,req,exc.state,target) if target not in ('WORKSPACE','NEED_TOOL_APPROVAL') else None
        if fallback:
            with s.db:
                m.transition(s,fid,target,'RESOURCE_MODEL_FALLBACK',
                             {'resource_fallback':fallback,'workflow_stop':None},fallback['bounded_switch'])
            save(root,meta)
            return response(s,fid,progress='Memory admission blocked '+fallback['from_model']+'. Auto will try '+
                fallback['model']+' with fresh paging and the same task, permissions and remaining budget.')
        stop={'status':'resource_wait','stage':target,'resume_phase':target,'error':str(exc),
              'measurement':exc.state,'automatic_retry':False}
        # Bounded host recovery never retries inference or changes task grants.
        # Persist the original stop before any optional idle-model release.
        move(s,fid,'PAUSED','RESOURCE_STOPPED',workflow_stop=stop)
        save(root,meta)
        try:
            from runtime_memory import recover
            def audit(kind,value):
                with s.db:s._audit(kind,dict(flow_id=fid,**value))
            recovery_result=exc.state.get('runtime_recovery') or recover(root,audit)
            if recovery_result:
                stop['recovery']=recovery_result
                move(s,fid,'PAUSED','RESOURCE_RECOVERY_OBSERVED',workflow_stop=stop)
        except ResourceWait as recovery_error:
            stop['recovery']={'admitted':False,'error':str(recovery_error),'automatic_inference':False}
            move(s,fid,'PAUSED','RESOURCE_RECOVERY_OBSERVED',workflow_stop=stop)
        return response(s,fid)


def review_provenance(s, meta, workspace, fid, data, prompt):
    """Capture a read-only architecture review's custody before source work."""
    from git_provenance import inspect, summary, upstream_requested
    from review_report import requested
    active = meta.get('active')
    task = s.snapshot(active) if active else {}
    constraints = task.get('constraints', {})
    if (data.get('permission', {}).get('operation', 'read') != 'read' or
            constraints.get('native_task') or not requested({'contract': {'goal': task.get('goal') or ''},
                                                             'request': prompt})):
        return None, None
    if upstream_requested(prompt):
        pending = inspect(workspace, include_dirty=False)
        stop = {'status': 'provenance_confirmation', 'resume_phase': 'BINDING',
                'error': summary(pending) + ' Open the intended checkout, or confirm review of the open snapshot.'}
        move(s, fid, 'PAUSED', 'PROVENANCE_CONFIRMATION_REQUIRED', workflow_stop=stop,
             git_provenance=pending)
        return None, response(s, fid)
    snapshot = inspect(workspace)
    move(s, fid, 'NEED_ARM', 'GIT_SNAPSHOT_RECORDED', git_provenance=snapshot)
    return snapshot, None


def provenance_changed(s, workspace, fid, data):
    from git_provenance import inspect, same_snapshot, summary
    before = data.get('git_provenance')
    if not before:
        return False
    current = inspect(workspace)
    if not same_snapshot(before, current):
        stop = {'status': 'provenance_changed', 'error': 'Source snapshot changed. ' + summary(current)}
        move(s, fid, 'PAUSED', 'GIT_SNAPSHOT_CHANGED', workflow_stop=stop)
        return True
    return False


def with_provenance(out, snapshot):
    if snapshot is None:
        return out
    from git_provenance import summary
    from review_report import references
    result = dict(out, git_provenance=snapshot)
    if result.get('report_markdown'):
        result['report_markdown'] = ('**Source snapshot** · ' + summary(snapshot) + '\n\n'
            + references([snapshot['workspace_root']]) + '\n\n' + result['report_markdown'])
    return result


def _handle(s,meta,workspace,root,req,providers=None):
    # Only the extension's private bridge can supply arming/permission events.
    # Model proposals cannot invoke this endpoint or set permissions.
    from conversation import answer as converse
    event=req.get('event','step');models=req.get('models');tier=req.get('tier','auto')
    def provider(stage):return providers(stage) if providers else None
    if event in ('pending','start','start_existing','start_review'):
        pending=pending_flows(s,workspace)
        if event=='pending':return response(s,pending[0]) if pending else {'state':'DONE','flow_id':None}
        # A saved workspace task must not intercept unrelated conversation.
        # Classify a new request through the gateway before asking to replace work.
        if pending and event!='start':raise InvalidUpdate('A saved workflow exists; explicitly resume or discard it before starting another request')
    if event in ('start','start_existing','start_review'):
        prompt=req.get('prompt','')
        if not isinstance(prompt,str) or not prompt.strip():raise InvalidUpdate('Empty workflow request')
        if event=='start_existing':
            from conversation import workspace_handoff,read
            workspace_handoff(s,meta.get('active'),req.get('conversation_id'),prompt)
            proposal=read(s,req['conversation_id'])['result']['accepted_output']
            if proposal['intent']!='workspace':raise InvalidUpdate('Continuation requires an admitted workspace intent')
        with s.db:
            f=m.create(s,'flow',objective=prompt,workspace=str(workspace),prompt=prompt,conversation_parent=req.get('parent'))
            m.transition(s,f['id'],'CLASSIFYING','REQUEST')
        fid=f['id']
        req['_created_flow_id']=fid
        if event=='start_review':
            sid=meta.get('active')
            if not req.get('armed') or not sid or req.get('binding')!=identity(s,sid) or not s.snapshot(sid)['constraints'].get('auto_review'):raise InvalidUpdate('Explicit review requires a user-created read-only task')
            move(s,fid,'NEED_ARM','USER_REVIEW',conversation_id=req.get('conversation_id'),permission={'operation':'read','paths':[]})
            return response(s,fid)
        if event=='start_existing':
            move(s,fid,'NEED_ARM','ACTION_CONTINUATION',conversation_id=req['conversation_id'],
                 permission=proposal.get('workspace_request') or {'operation':'read','paths':[]})
            return response(s,fid)
    else:
        fid=req.get('flow_id');f,d=info(s,fid)
        if f['workspace']!=str(workspace):raise InvalidUpdate('Workflow belongs to another workspace')
        if event=='inspect':
            extra={'prompt':f['prompt']}
            if m.state(s,fid)=='NEED_TOOL_APPROVAL':extra['capability_proposal']=d.get('capability_proposal')
            if m.state(s,fid)=='NEED_PERMISSION' and not recovery(s,fid):extra.update(permission=permission(s,meta['active'],d['permission']),bound_task=identity(s,meta['active']))
            return response(s,fid,**extra)
        if req.get('cursor')!=response(s,fid)['cursor']:raise InvalidUpdate('Workflow event is stale or replayed')
    f,d=info(s,fid);phase=m.state(s,fid);prompt=f['prompt']
    if event in ('allocation_preview','allocation_grant'):
        from allocation_recovery import preview,grant
        if event=='allocation_preview':
            return response(s,fid,allocation_proposal=preview(s,fid,meta,req.get('additions')))
        if req.get('confirmed') is not True:
            raise InvalidUpdate('Allocation increase requires explicit user confirmation')
        amended=grant(s,fid,meta,req.get('proposal'))
        return response(s,fid,allocation_amendment=amended)
    if event=='cancel':
        if phase in ('EXECUTING_ACTION','CANCELLING'):
            m.cancel(s,fid);return response(s,fid)
        if d.get('root_node'):
            from execution_sessions import finish
            finish(s, fid, cancelled=True)
            m.abandon_children(s,d['root_node'],'Workflow cancelled')
        m.cancel(s,fid);return response(s,fid)
    incompatibility=recovery(s,fid)
    if event=='restart_review':
        if not incompatibility or not incompatibility['restart_allowed']:raise InvalidUpdate('This saved request cannot restart as a policy-only or source-snapshot review')
        parent=m.node(s,d['root_node']);sid=parent['session']
        if not req.get('armed') or meta.get('active')!=sid or req.get('binding')!=identity(s,sid):
            raise InvalidUpdate('Restart requires the same user-armed task and current binding')
        conversation_id = d.get('conversation_id')
        if conversation_id:
            from conversation import workspace_handoff
            workspace_handoff(s,sid,conversation_id,prompt)
        # This is an explicit host UI event. Keep the task and all consumed use;
        # do not transfer old model results or authorize any new file/action scope.
        with s.db:
            from execution_sessions import finish
            finish(s, fid, cancelled=True)
            m.abandon_children(s,parent['id'],'User restarted review after a policy or source change')
            m.cancel(s,fid)
            new=m.create(s,'flow',objective=prompt,workspace=str(workspace),prompt=prompt,
                         conversation_parent=conversation_id,restarted_from=fid)
            restart_kind = 'SOURCE_RESTART' if incompatibility['kind']=='provenance_changed' else 'POLICY_RESTART'
            m.transition(s,new['id'],'CLASSIFYING',restart_kind,{'previous_flow':fid})
            m.transition(s,new['id'],'NEED_ARM','USER_REVIEW',{'conversation_id':conversation_id,
                         'permission':d.get('permission') or {'operation':'read','paths':[]}})
        return response(s,new['id'],progress=('Review restarted with the current source snapshot; task constraints and consumed budget retained.'
                        if incompatibility['kind']=='provenance_changed' else
                        'Review restarted under the current policy; task constraints and consumed budget retained.'))
    if incompatibility:return response(s,fid)
    if phase=='PAUSED':
        stop = d.get('workflow_stop') or {}
        if stop.get('status') == 'provenance_confirmation':
            if event == 'resume':
                return response(s, fid)
            if event != 'accept_provenance' or req.get('confirmed') is not True or not req.get('armed'):
                raise InvalidUpdate('Source snapshot selection requires explicit confirmation in this workspace')
            from git_provenance import checkout, inspect, summary
            snapshot = inspect(workspace, include_dirty=False)
            if checkout(snapshot) == checkout(d['git_provenance']):
                snapshot = inspect(workspace)
            if checkout(snapshot) != checkout(d['git_provenance']):
                stop = dict(stop, error='The checkout changed before confirmation. ' + summary(snapshot))
                move(s, fid, 'PAUSED', 'PROVENANCE_CONFIRMATION_REQUIRED', workflow_stop=stop,
                     git_provenance=snapshot)
                return response(s, fid)
            move(s, fid, 'BINDING', 'GIT_SNAPSHOT_CONFIRMED', workflow_stop=None, git_provenance=snapshot)
            return response(s, fid, progress=summary(snapshot))
        if event!='resume':raise InvalidUpdate('Workflow paused. Choose Resume or Discard; allocations remain unchanged.')
        stopped=[e for e in m.events(s,fid) if e['kind'] in ('MAPPING_STOPPED','CONTEXT_STOPPED','RESOURCE_STOPPED','ROUTING_STOPPED','MEMORY_STOPPED')]
        if not stopped:raise InvalidUpdate('Paused workflow requires inspection before recovery')
        last=stopped[-1]
        target=(last['data']['workflow_stop']['resume_phase'] if last['kind'] in ('RESOURCE_STOPPED','ROUTING_STOPPED','MEMORY_STOPPED')
                else last['from'] if last['kind']=='CONTEXT_STOPPED' else 'MAPPING')
        if target!='CLASSIFYING':
            if not req.get('armed'):raise InvalidUpdate('Workspace workflow requires a user-armed harness')
            if d.get('binding')!=identity(s,meta.get('active')) or req.get('binding')!=d['binding']:
                raise InvalidUpdate('Workflow task, calibration or permissions changed')
        if target != 'CLASSIFYING' and provenance_changed(s, workspace, fid, d):
            return response(s, fid)
        if last['kind']=='RESOURCE_STOPPED':
            from monitoring import require_resources
            from runtime_memory import recover
            def audit(kind,value):
                with s.db:s._audit(kind,dict(flow_id=fid,**value))
            recovered=recover(root,audit)
            if recovered and not recovered['admitted']:
                from monitoring import ResourceWait
                raise ResourceWait(dict(recovered['after'],runtime_recovery=recovered))
            require_resources(root)
        move(s,fid,target,'RESUME_REQUESTED',workflow_stop=None,
             resumed_followup_source=stop.get('followup_source_changed'),
             reason='User resumed with existing contract and allocation')
        return response(s,fid)
    if phase=='CLASSIFYING':
        from conversation import session_for
        from conversation_routing import RoutingBlocked
        request={'prompt':prompt,'parent':f.get('conversation_parent'),'models':models,'tier':tier,
                 'conversation_session':d.get('conversation_session'),'routing_continuation':d.get('routing_continuation'),'one_step':True}
        request['conversation_session']=session_for(s,request)
        if not d.get('conversation_session'):
            move(s,fid,'CLASSIFYING','CONVERSATION_BOUND',conversation_session=request['conversation_session'])
        try:
            chat=converse(s,meta,root,request,provider=provider('conversation'))
        except (ContextBudgetExceeded,BudgetStop,m.OrchestrationStop,RoutingBlocked) as exc:
            if isinstance(exc,ContextBudgetExceeded):observe_fault(s,request['conversation_session'],exc)
            stop={'status':'routing_budget','resume_phase':'CLASSIFYING','error':str(exc),
                  'budget':getattr(exc,'frame',None),'automatic_retry':False}
            move(s,fid,'PAUSED','ROUTING_STOPPED',workflow_stop=stop)
            return response(s,fid)
        r=chat['result']
        if not r['admission']['admitted']:
            move(s,fid,'DONE','REJECTED',conversation_id=r['invocation_id']);return response(s,fid,conversation=chat)
        next_step=chat.get('routing_followup')
        if next_step:
            move(s,fid,'CLASSIFYING','ROUTING_CHECKPOINT',routing_continuation=r['invocation_id'],next_routing=next_step)
            if next_step['kind']=='blocked':
                stop={'status':'routing_choice','resume_phase':'CLASSIFYING','error':next_step['reason'],'automatic_retry':False}
                move(s,fid,'PAUSED','ROUTING_STOPPED',workflow_stop=stop)
            return response(s,fid,routing_step=chat,progress=next_step['reason'])
        p=r['accepted_output'];target='NEED_ARM' if p['intent']=='workspace' else 'DONE'
        other=pending_flows(s,workspace,exclude=fid)
        if target=='NEED_ARM' and other:
            # Only the host can resolve this conflict. No source reads or grants
            # are made, and the existing task/allocations remain untouched.
            move(s,fid,'DONE','WORKSPACE_DEFERRED',conversation_id=r['invocation_id'],saved_flow_id=other[0])
            return response(s,fid,conversation=chat,deferred_flow_id=other[0])
        move(s,fid,target,'CLASSIFIED',conversation_id=r['invocation_id'],permission=p.get('workspace_request') or {'operation':'read','paths':[]})
        return response(s,fid,conversation=chat)
    if phase in m.TERMINAL:return response(s,fid)
    if not req.get('armed'):raise InvalidUpdate('Workspace workflow requires a user-armed harness')
    if phase=='NEED_ARM':
        snapshot, pending = review_provenance(s, meta, workspace, fid, d, prompt)
        if pending:
            return pending
        move(s,fid,'BINDING','ARM_CONFIRMED');phase='BINDING'
    else:
        snapshot = None
    if phase in ('BINDING','NEED_PERMISSION'):
        from vscode_bridge import _handle
        sid=meta.get('active')
        if sid and req.get('binding') and req['binding']!=identity(s,sid):raise InvalidUpdate('Task changed during arming')
        if not sid or not any(s.snapshot(sid)['constraints'].get(k) for k in ('native_task','auto_review')):
            _handle(Path(root),Path(workspace),{'op':'auto_new','goal':prompt,'roots':['.'],'conversation_id':d['conversation_id']})
            meta.clear();meta.update(json.loads((Path(root)/'workspace.json').read_text()));sid=meta['active']
        wanted=permission(s,sid,d['permission'])
        if phase=='NEED_PERMISSION':
            if d.get('permission_binding')!=identity(s,sid):raise InvalidUpdate('Task changed while asking for permission')
            if event!='grant':raise InvalidUpdate('Explicit permission grant required')
            grant=req.get('grant') or {}
            if grant.get('operation')!=d['permission']['operation']:raise InvalidUpdate('Grant operation differs from pending request')
            from native_access import grant as grant_access
            grant_access(s,meta,workspace,dict(grant,**identity(s,sid),conversation_id=d['conversation_id']));save(root,meta)
            move(s,fid,'BINDING','PERMISSION_GRANTED');wanted=None
        if wanted:
            move(s,fid,'NEED_PERMISSION','PERMISSION_REQUIRED',permission_binding=identity(s,sid))
            return response(s,fid,permission=wanted,bound_task=identity(s,sid))
        from conversation import workspace_handoff
        if d['conversation_id']:workspace_handoff(s,sid,d['conversation_id'],prompt)
        from execution_sessions import open_session
        task=m.task(s,sid);s.db.commit()
        move(s,fid,'BINDING','EXECUTION_PENDING',binding=identity(s,sid),root_node=task['id'])
        with s.db:
            parent=open_session(s,sid,fid)
            m.transition(s,fid,'WORKSPACE','TASK_BOUND',{'binding':identity(s,sid),'root_node':parent['id']})
        from git_provenance import summary
        return response(s,fid,**({'progress': summary(snapshot)} if snapshot else {}))
    if d.get('binding')!=identity(s,meta.get('active')) or req.get('binding')!=d['binding']:raise InvalidUpdate('Workflow task, calibration or permissions changed')
    parent=m.checked(s,d['root_node']);sid=parent['session']
    if phase=='NEED_TOOL_APPROVAL':
        import capabilities
        p=d['capability_proposal']
        receipt=capabilities.completed(s,p['id'])
        if receipt:
            capabilities.materialize(s,receipt,receipt['receipt'])
        else:
            if event!='capability_grant':raise InvalidUpdate('Tool approval required')
            with m.scope(parent['id']):
                receipt=capabilities.execute(s,sid,workspace,root,p['id'],req.get('authorization'))
        if receipt['result']['status']=='installed' and d['permission'].get('capability_action')=='check':
            move(s,fid,'WORKSPACE','TOOL_INSTALLED',capability_proposal=None)
            return response(s,fid,progress='Approved tool installed; preparing its separate source-check permission.',capability_result=receipt)
        move(s,fid,'DONE','CAPABILITY_FINISHED',capability_result=receipt)
        return response(s,fid,capability_result=receipt)
    if phase=='WORKSPACE':
        if d['permission'].get('operation')=='capability':
            import capabilities
            wanted=d['permission'];name=wanted['capability'];action=wanted['capability_action']
            if action=='check' and not capabilities.installed(s,root,name):action='install'
            with m.scope(parent['id']):
                p=capabilities.preview(s,sid,workspace,root,name,action,wanted['paths'] if action=='check' else [])
            move(s,fid,'NEED_TOOL_APPROVAL','TOOL_PROPOSED',capability_proposal=p)
            return response(s,fid,capability_proposal=p)
        c=s.snapshot(sid)['constraints']
        move(s,fid,'CODING' if c.get('native_task') else 'MAPPING','WORK_SELECTED')
        return response(s,fid)
    if phase=='MAPPING':
        import auto_review as auto
        with m.scope(parent['id']):out=auto.step(s,meta,workspace,root,{'explain':False},provider=provider('mapping'))
        save(root,meta)
        status=out['auto']['status'] if 'auto' in out else out.get('status')
        if status=='resource_wait' and out.get('auto',out).get('resource_stop'):
            from monitoring import ResourceWait
            raise ResourceWait(out.get('auto',out)['resource_stop'])
        if status=='structural_pass_complete':
            move(s,fid,'WORKSPACE','SOURCE_MAPPED')
            move(s,fid,'BRANCHING','PLAN_CHILDREN')
        elif status=='bounded_source_pass':
            move(s,fid,'WORKSPACE','SOURCE_BREADTH_DEFERRED')
            move(s,fid,'BRANCHING','PLAN_BOUNDED_SYNTHESIS')
            move(s,fid,'SYNTHESIS','PAGING_BEFORE_INCREASE',children=[],signals={'policy':'bounded-source-pages','pending_files':out['auto']['pending_files']})
        elif status not in ('ready','page_rejected'):
            detail=out.get('auto',out);failures=detail.get('failures') or {}
            stop={'status':status,'error':detail.get('error'),'failure_count':len(failures),
                  'failures':[{'path':p,'error':str(e)[:500]} for p,e in sorted(failures.items())[:5]],
                  'mapped_files':detail.get('mapped_files'),'source_files':detail.get('source_files'),
                  'budget':detail.get('stop_frame') or detail.get('budget')}
            if status == 'budget_stopped' and detail.get('stop_frame'):
                stop['frame'] = detail['stop_frame']
                return budget_pause(s, meta, root, req, fid, 'MAPPING', stop, kind='MAPPING_STOPPED',
                                    reason=status, mapping_stop=stop)
            move(s,fid,'PAUSED','MAPPING_STOPPED',reason=status,mapping_stop=stop,workflow_stop=stop)
        return response(s,fid,progress='Fresh source mapping: '+str(status),mapping=out)
    if phase=='BRANCHING':
        parent,children,signals=plan(s,meta,workspace,prompt,d['conversation_id'],parent['id'])
        move(s,fid,'CHILDREN' if children else 'SYNTHESIS','BRANCH_PLAN',children=[c['id'] for c in children],signals=signals)
        return response(s,fid,progress=('Reviewing '+str(len(children))+' source partitions' if children else 'Preparing a bounded overview directly; no child review was allocated'))
    if phase=='CHILDREN':
        from subagent_runtime import run
        from resource_routing import effective
        pending=[i for i in d['children'] if m.state(s,i)!='DONE']
        if pending:
            if m.state(s,pending[0]) in m.TERMINAL:raise InvalidUpdate('Child is quarantined or cancelled; result cannot be joined')
            out=run(s,meta,workspace,root,pending[0],models,effective(d,models,tier),provider('subagent'))
            move(s,fid,'CHILDREN','CHILD_FINISHED',last_child=pending[0],last_model=out['route']['model'])
            return response(s,fid,child=out,progress='Child review '+m.state(s,pending[0]))
        if m.state(s,parent['id'])=='READY':
            joined=[e for e in m.events(s,parent['id']) if e['kind']=='JOIN']
            ids=joined[-1]['data']['invocations'] if joined else []
            if len(ids)!=len(d['children']) or any(m.events(s,i)[-1]['data'].get('invocation_id')!=iid for i,iid in zip(d['children'],ids)):raise InvalidUpdate('Recovered join does not match children')
        else:ids=m.join(s,parent['id'])
        move(s,fid,'SYNTHESIS','CHILDREN_JOINED',child_results=ids)
        return response(s,fid)
    if phase in ('SYNTHESIS','CODING'):
        from resource_routing import effective
        if phase == 'SYNTHESIS' and provenance_changed(s, workspace, fid, d):
            return response(s, fid)
        request={'prompt':prompt,'models':models,'tier':effective(d,models,tier),'conversation_id':d['conversation_id'],**d['binding'],'child_results':d.get('child_results',[]),'orchestration_parent':d.get('result_parent',parent['id'])}
        try:
            with m.scope(parent['id']):
                if phase=='CODING':
                    from native_runtime import turn
                    out=turn(s,meta,workspace,root,request,provider=provider('coder'))
                else:
                    from chat_runtime import answer
                    out=answer(s,meta,workspace,root,request,provider=provider('synthesis'))
        except (ContextBudgetExceeded, m.OrchestrationStop, BudgetStop) as exc:
            observe_fault(s,sid,exc)
            import review_budget
            allocation = getattr(exc, 'details', None)
            stop={'status':'context_budget' if isinstance(exc,ContextBudgetExceeded) else 'task_budget','stage':phase,'error':str(exc),
                  'allocation':allocation,'budget':review_budget.usage(s,sid)}
            if isinstance(exc, (m.OrchestrationStop, BudgetStop)):
                stop['frame'] = exc.frame
            if allocation and allocation.get('task_allocation_need'):
                stop['allocation_need'] = allocation['task_allocation_need']
            elif isinstance(exc, ContextBudgetExceeded) and allocation:
                stop['automatic_allocation'] = {'status': 'model_context',
                    'reason': 'The host could not show that more task allocation would fit this packet in the selected model.'}
            return budget_pause(s, meta, root, req, fid, phase, stop)
        if phase == 'SYNTHESIS':
            if provenance_changed(s, workspace, fid, d):
                return response(s, fid)
            out = with_provenance(out, d.get('git_provenance'))
        save(root,meta)
        action=(out['result'].get('accepted_output') or {}).get('action',{}).get('kind')
        awaiting=phase=='CODING' and action in ('edit','run_tests')
        move(s,fid,'AWAITING_ACTION' if awaiting else 'DONE','ACTION_PROPOSED' if awaiting else 'WORKFLOW_FINISHED',invocation_id=out['result']['invocation_id'],
             observation_delivery='awaiting_action' if awaiting else 'admitted' if out['result']['admission']['admitted'] else 'withheld')
        return response(s,fid,data=out)
    raise InvalidUpdate('Workflow needs explicit recovery: '+phase)
