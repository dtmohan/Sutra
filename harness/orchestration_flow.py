"""Host-owned native workflow. The UI renders states and supplies user grants."""
import copy,json,math,re
from pathlib import Path
from context_manager import InvalidUpdate,ContextBudgetExceeded,canonical,sha
import orchestration as m
from memory_layer import PageFault, fault_details, observe_fault
from review_budget import BudgetStop
from review_targets import (
    DOCUMENT_TEXT_REQUIRED, document_file_target, document_input_notice, document_text, has_document_text,
    may_use_retained_document, refers_to_previous_review_target, refers_to_retained_document,
)
from evidence_admission import measure as evidence_measure, record_document, workspace_roots, workspace_target


def document_workspace_notice(prompt):
    notice=document_input_notice(prompt)
    if not notice and has_document_text(prompt):
        return 'Open Sutra Auto in VS Code Local Chat to review the pasted document text.'
    return notice

def latest_chat_flow(s,workspace,chat):
    previous=s.db.execute(
            """SELECT id FROM orchestration_nodes
               WHERE json_extract(record_json,'$.kind')='flow'
               AND json_extract(record_json,'$.workspace')=? AND json_extract(record_json,'$.chat_key')=?
               ORDER BY rowid DESC LIMIT 1""",(str(workspace),chat)).fetchone()
    return info(s,previous[0]) if previous else None


def _original_ask(s, turn):
    """A restart's creation time does not make its original ask newer."""
    current = turn; seen = set()
    while current.get('restarted_from'):
        if current['id'] in seen: raise InvalidUpdate('Review restart lineage contains a cycle')
        seen.add(current['id'])
        prior = m.node(s, current['restarted_from'])
        if (prior.get('kind') != 'flow' or any(prior.get(key) != turn.get(key)
                for key in ('workspace', 'chat_key', 'prompt'))):
            raise InvalidUpdate('Review restart lineage changed its original request or chat')
        current = prior
    return current['id']


def _latest_ask_flow(s, workspace, chat):
    original = s.db.execute("""SELECT id FROM orchestration_nodes
        WHERE json_extract(record_json,'$.kind')='flow'
        AND json_extract(record_json,'$.workspace')=? AND json_extract(record_json,'$.chat_key')=?
        AND json_extract(record_json,'$.restarted_from') IS NULL ORDER BY rowid DESC LIMIT 1""",
        (str(workspace), chat)).fetchone()
    if not original: return None
    for raw, in s.db.execute("""SELECT record_json FROM orchestration_nodes
        WHERE json_extract(record_json,'$.kind')='flow'
        AND json_extract(record_json,'$.workspace')=? AND json_extract(record_json,'$.chat_key')=?
        ORDER BY rowid DESC""", (str(workspace), chat)):
        turn = json.loads(raw)
        if _original_ask(s, turn) == original[0]: return turn
    return None


def referenced_document_notice(s,workspace,prompt,chat,parent,prior_prompt=None):
    if not refers_to_previous_review_target(prompt):
        return None
    if chat:
        previous=latest_chat_flow(s,workspace,chat)
        if previous:
            turn,data=previous
            if (data.get('workflow_stop') or {}).get('status')=='evidence_bridge':
                bridge=data['workflow_stop']['evidence_bridge']
                if bridge['target_kind']=='document':return data['workflow_stop']['error']
            if data.get('request_notice') and m.events(s,turn['id'])[-1]['kind']=='DOCUMENT_INPUT_REQUIRED':
                return data['request_notice']
            notice=document_input_notice(turn['prompt'])
            if notice:return notice
            if data.get('conversation_id')!=parent:return None
    if not isinstance(parent,str) or not re.fullmatch('[a-f0-9]{32}',parent):
        # A chat can omit both a stable ID and a model receipt after host guidance.
        # This hint can only request document input. It never binds or grants work.
        return document_input_notice(prior_prompt)
    row=s.db.execute('SELECT session FROM invocations WHERE id=?',(parent,)).fetchone()
    if row:
        from conversation_memory import history
        previous=history(s,row[0],parent)[0]
        if not has_document_text(previous['user']):
            return document_input_notice(previous['user']) or (
                DOCUMENT_TEXT_REQUIRED if previous['intent']=='document' else None)
    return None


def retained_chat_document(s, workspace, chat, parent, prompt, *, implicit=False):
    eligible = may_use_retained_document if implicit else refers_to_retained_document
    if not chat or not parent or not eligible(prompt):
        return None
    latest = latest_chat_flow(s, workspace, chat)
    if not latest:
        return None
    turn, data = latest
    last = m.events(s, turn['id'])[-1]
    phase = last['to']
    # A withheld reply is never published as the native conversation parent.
    retry = (last['kind'] == 'REJECTED' and turn.get('conversation_parent') == parent
             and (data.get('document_evidence') or {}).get('retained_from') == parent)
    if data.get('conversation_id') != parent and not retry:
        return None
    # A legacy router can misdirect a document follow-up before arming. Its
    # accepted conversation receipt is usable while no workspace task is bound.
    if phase != 'DONE' and (phase != 'NEED_ARM' or data.get('binding') or data.get('root_node')):
        return None
    from conversation import retained_document_reference
    session = data.get('conversation_session')
    if not session:
        return None
    ref = retained_document_reference(s, session, parent, unique=implicit)
    if not ref:
        return None
    source_id = ref['key'].removeprefix('document-bridge:')
    source = m.node(s, source_id)
    if source.get('chat_key') != chat or source.get('workspace') != str(workspace):
        return None
    return session, dict(ref, retained_from=parent)


def repage_document(s, fid, session, ref):
    frame,_=evidence_measure(s,session,'resolved','available',False)
    if frame['decision']!='ALLOW':
        move(s,fid,'DONE','EVIDENCE_FIN',kernel=frame,request_notice='The task budget is exhausted.')
        return False
    move(s,fid,'CLASSIFYING','EVIDENCE_REPAGED',kernel=frame,
         conversation_session=session,document_evidence=ref,routing_continuation=None)
    return True


def saved_document_notice(s,workspace,flow,phase,data):
    if phase in m.TERMINAL|{'CLASSIFYING','EXECUTING_ACTION','CANCELLING'}:
        return None
    if phase=='PAUSED' and (data.get('workflow_stop') or {}).get('resume_phase')=='CLASSIFYING':
        return None
    return document_workspace_notice(flow['prompt']) or referenced_document_notice(
        s,workspace,flow['prompt'],None,flow.get('conversation_parent'))


def metadata_path(root, flow_id=None):
    if flow_id is None:
        return Path(root)/'workspace.json'
    if not isinstance(flow_id,str) or not re.fullmatch('[a-f0-9]{32}',flow_id):
        raise InvalidUpdate('Invalid workspace workflow identity')
    return Path(root)/'workflows'/(flow_id+'.json')

def flow_metadata(root,workspace,fid):
    path=metadata_path(root,fid)
    meta=json.loads(path.read_text()) if path.exists() else {'workspace':str(workspace),'_flow':fid}
    if meta.get('workspace')!=str(workspace) or meta.get('_flow')!=fid:
        raise InvalidUpdate('Workspace checkpoint belongs to another workflow')
    return meta

def save(root,meta):
    path=metadata_path(root,meta.get('_flow'))
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp');temp.write_text(canonical(meta));temp.replace(path)

def info(s,fid):
    n=m.node(s,fid)
    if n['kind']!='flow':raise InvalidUpdate('Expected a workflow identity')
    history=m.events(s,fid);data={}
    for e in history:data.update(e['data'])
    return n,data

def move(s,fid,target,kind,*,kernel=None,**data):
    with s.db:
        result = m.transition(s,fid,target,kind,data,kernel)
        if target == 'DONE':
            from execution_sessions import finish
            finish(s, fid)
            from task_stack import close, owner, view
            workspace=m.node(s,fid)['workspace']
            chat=owner(s,workspace,fid)
            if chat:
                close(s,workspace,chat,fid,expected_cursor=view(s,workspace,chat)['cursor'])
        return result

def pending_flows(s,workspace,exclude=None):
    return [nid for nid, in s.db.execute(
        "SELECT id FROM orchestration_nodes WHERE json_extract(record_json,'$.kind')='flow' ORDER BY rowid DESC")
        if nid!=exclude and m.node(s,nid).get('workspace')==str(workspace)
        and not m.node(s,nid).get('chat_key')
        and m.state(s,nid) not in m.TERMINAL]

def response(s,fid,**extra):
    n,data=info(s,fid);history=m.events(s,fid)
    if m.state(s,fid)=='NEED_TOOL_APPROVAL':
        import capabilities
        p=data.get('capability_proposal')
        extra.setdefault('capability_proposal',p)
        extra['capability_settled']=bool(p and capabilities.completed(s,p['id']))
    if m.state(s,fid)=='DONE':
        for key in ('capability_result','request_notice'):
            if key in data:extra.setdefault(key,data[key])
    if m.state(s,fid)=='AWAITING_ACTION':
        extra.setdefault('pending_action',data.get('invocation_id'))
    from task_observations import summary
    outcome = extra.get('data', {}).get('result')
    observations = summary(s, data.get('conversation_id'), data.get('invocation_id'),
        ('awaiting_action' if m.state(s,fid)=='AWAITING_ACTION' else 'admitted' if outcome['admission']['admitted'] else 'withheld') if outcome else data.get('observation_delivery'))
    from coherence_observations import read_observation
    publication = extra.get('data', {}).get('report_review') or {}
    coherence_id = (publication.get('reviewer_id') or (outcome or {}).get('invocation_id')
                    or data.get('invocation_id') or data.get('conversation_id'))
    coherence = read_observation(s, coherence_id)
    if m.state(s, fid) == 'PAUSED' and data.get('binding'):
        extra.setdefault('claim_review_usage', workflow_usage(s, fid))
    return dict(task_observations=observations,coherence_observation=coherence,
                flow_id=fid,state=m.state(s,fid),cursor=history[-1]['receipt'] if history else None,
                conversation_id=data.get('conversation_id'),binding=data.get('binding'),
                git_provenance=data.get('git_provenance'),
                resource_fallback=data.get('resource_fallback'),
                stop=(data.get('workflow_stop') or data.get('mapping_stop')) if m.state(s,fid)=='PAUSED' else None,
                recovery=recovery(s,fid),allocation_version=data.get('allocation_version',0),
                execution_id=data.get('root_node') if data.get('root_node') and m.node(s,data['root_node'])['kind']=='execution' else None,
                children=[{'id':i,'state':m.state(s,i),'files':m.node(s,i)['files']} for i in data.get('children',[])],**extra)

def _terminal_review(s, flow, data):
    """Only the latest closed, unpublished workspace draft can be restarted.

    Historical output is inspected as an immutable receipt, never admitted as
    fresh evidence under a new policy. A restart still reads the source again.
    """
    chat = flow.get('chat_key')
    if not chat or m.state(s, flow['id']) != 'DONE': return False
    last = m.events(s, flow['id'])[-1]
    if last['kind'] != 'WORKFLOW_FINISHED' or last['data'].get('observation_delivery') != 'withheld': return False
    from task_stack import owner
    if owner(s, flow['workspace'], flow['id']) != chat: return False
    latest = _latest_ask_flow(s, flow['workspace'], chat)
    if not latest or latest['id'] != flow['id']: return False
    checkpoint = data.get('parent_checkpoint') or {}
    cid = checkpoint.get('candidate_id')
    if not cid or checkpoint.get('version') != 1 or last['data'].get('invocation_id') != cid: return False
    row = s.db.execute('SELECT i.record_json,i.digest,d.payload_json,d.payload_hash,r.record_json,r.id '
        'FROM invocations i JOIN deliveries d ON d.id=i.delivery_id '
        'JOIN invocation_receipts r ON r.invocation_id=i.id WHERE i.id=?', (cid,)).fetchone()
    if not row: return False
    raw, digest, payload, payload_hash, receipt, rid = row
    env, source, accepted = json.loads(raw), json.loads(payload), json.loads(receipt)
    parent = m.node(s, data['root_node'])
    actor_id = (env.get('orchestration') or {}).get('node_id')
    if not actor_id: return False
    actor = m.node(s, actor_id)
    return (sha('invocation-v1:' + raw) == digest and sha('invocation-receipt-v1:' + receipt) == rid
        and accepted.get('invocation_hash') == digest and accepted.get('status') == 'accepted'
        and sha(canonical(accepted.get('proposal'))) == accepted.get('response_hash')
        and 'report' in (accepted.get('proposal') or {}) and env.get('role') == 'chat_reviewer'
        and env.get('session') == parent['session'] and actor.get('flow_id') == flow['id']
        and sha(payload) == payload_hash == env.get('payload_hash') == checkpoint.get('packet_hash')
        and sha(canonical(accepted['proposal'])) == checkpoint.get('candidate_hash')
        and source.get('workspace') == flow['workspace'] and source.get('request') == flow['prompt']
        and not s.db.execute('SELECT 1 FROM invocation_quarantine WHERE invocation_id=?', (cid,)).fetchone())


def recovery(s,fid):
    """Inspect incompatibility without accepting or rewriting old commitments."""
    n,data=info(s,fid)
    terminal = m.state(s,fid) in m.TERMINAL
    if not data.get('root_node') or terminal and not _terminal_review(s, n, data): return None
    parent=m.node(s,data['root_node']);old=parent['binding']
    try:current=m.binding(s,parent['session'])
    except InvalidUpdate:
        return {'kind':'binding_changed','restart_allowed':False,
                'message':'The saved task calibration is no longer usable. Discard this request and establish a valid task before continuing.'}
    changed=[key for key in old if old[key]!=current.get(key)]
    if terminal:
        if changed != ['logic_hash'] or not s.snapshot(parent['session'])['constraints'].get('auto_review'): return None
        from task_stack import view
        stack = view(s, n['workspace'], n['chat_key'])
        return {'kind': 'policy_changed', 'restart_allowed': True, 'terminal_review': True,
                'stack_cursor': stack['cursor'], 'parked_flow_id': stack['focused'],
                'message': 'This unpublished review ended under an older Sutra policy. Restart review keeps the '
                           'original task, constraints and consumed spending, and reads fresh source under the current policy.'
                           + (' The older open task will remain saved and be parked while this review continues.'
                              if stack['focused'] else '')}
    if set(changed) <= {'logic_hash', 'contract_hash'}:
        from native_access import read_scope_correction
        correction = read_scope_correction(s, n, data, old)
        if correction:
            return {'kind': 'source_scope_correction', 'restart_allowed': True,
                    'binding': {key: value for key, value in current.items() if key != 'logic_hash'},
                    'source_roots': correction['read_scope'],
                    'message': 'The original request asked for this workspace, but the router committed suggested '
                               'filenames as its scope. Restart review corrects that read-only target to the workspace '
                               'in a new contract version. The original request, other constraints, consumed spending '
                               'and total allocation remain; no edits, tests or network access are granted.'}
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
        children=[m.node(s,i) for i in m.fork_children(s,parent['id'])]
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


def task_projection(s,workspace,chat_key):
    from task_stack import view
    stack=view(s,workspace,chat_key)
    fid=stack['focused']
    focus=response(s,fid) if fid else None
    if focus and focus['binding']:
        from review_budget import usage
        focus['task_budget']=usage(s,focus['binding']['session'])
    return {'task_stack':stack,'focused_flow':focus}

def handle(s,meta,workspace,root,req,providers=None):
    req=dict(req)
    chat=req.get('chat_key')
    event=req.get('event','step')
    fid=req.get('flow_id')
    if chat is None:
        if fid and m.node(s,fid).get('chat_key'):
            raise InvalidUpdate('This workflow requires its originating Sutra chat')
        return run(s,meta,workspace,root,req,providers)
    if not isinstance(chat,str) or not re.fullmatch('[a-f0-9]{64}',chat):
        raise InvalidUpdate('Invalid Sutra chat identity')
    if fid is not None and event in ('start','start_existing','start_review'):
        raise InvalidUpdate('A new request needs a new workflow identity')
    from task_stack import assert_focused, close, owner, select
    if event in ('task_list','pending','recoverable_review'):
        projection=task_projection(s,workspace,chat)
        if event=='task_list':
            return projection
        if event=='recoverable_review':
            latest = _latest_ask_flow(s, workspace, chat)
            if latest:
                saved = response(s, latest['id'], prompt=latest['prompt'])
                if (saved.get('recovery') or {}).get('terminal_review'):
                    return dict(saved, **projection)
        return dict(projection['focused_flow'] or {'state':'DONE','flow_id':None},**projection)
    if fid:
        n=m.node(s,fid)
        if n.get('workspace')!=str(workspace) or owner(s,workspace,fid)!=chat:
            raise InvalidUpdate('Workflow belongs to another Sutra chat')
        meta=flow_metadata(root,workspace,fid)
        if event in ('task_focus','task_close','cancel'):
            if req.get('cursor')!=response(s,fid)['cursor']:
                raise InvalidUpdate('Workflow event is stale or replayed')
            if event=='cancel':
                assert_focused(s,workspace,chat,fid)
            expected=req.get('stack_cursor') if event!='cancel' else task_projection(s,workspace,chat)['task_stack']['cursor']
            with s.db:
                if event=='task_focus':
                    select(s,workspace,chat,fid,expected_cursor=expected)
                else:
                    close(s,workspace,chat,fid,expected_cursor=expected)
                    if m.state(s,fid) not in m.TERMINAL:
                        from execution_sessions import finish
                        finish(s,fid,cancelled=True)
                        m.transition(s,fid,'CANCELLED','CANCELLED',{'spending_retained':True})
            projection=task_projection(s,workspace,chat)
            return dict(response(s,fid),**projection) if event=='cancel' else projection
        if event!='inspect' and not (event == 'restart_review' and (recovery(s,fid) or {}).get('terminal_review')):
            assert_focused(s,workspace,chat,fid)
    else:
        meta={'workspace':str(workspace)}
    out=run(s,meta,workspace,root,req,providers)
    return dict(out,**task_projection(s,workspace,chat))

def workflow_usage(s, fid):
    """Complete receipt-backed usage for this flow, including interrupted calls."""
    from token_usage import history, summarize
    _, data = info(s, fid)
    sid = (data.get('binding') or {}).get('session')
    call_ids = set(); runs = set()
    if sid:
        for raw, in s.db.execute('SELECT record_json FROM invocations WHERE session=? ORDER BY rowid', (sid,)):
            env = json.loads(raw)
            owner = (env.get('orchestration') or {}).get('node_id')
            while owner:
                node = m.node(s, owner)
                if node.get('flow_id') == fid:
                    call_ids.add(env['id']); runs.add(env['run_id']); break
                owner = node.get('parent_id')
    calls = [call for run in runs for call in history(s.db, run_id=run)['calls']
             if call['invocation_id'] in call_ids]
    calls.sort(key=lambda call: (call['created_at'], call['invocation_id']))
    return {'calls': calls, 'totals': summarize(calls), 'total_calls': len(calls), 'limit': None}


def run(s,meta,workspace,root,req,providers=None):
    """Translate measured resource stops at the shared orchestration boundary."""
    from monitoring import ResourceWait
    from lifecycle import Interrupted
    req=dict(req)
    req.pop('_created_flow_id',None)
    try:
        return _handle(s,meta,workspace,root,req,providers)
    except Interrupted as exc:
        fid = req.get('_created_flow_id') or req.get('flow_id')
        if not fid or m.state(s, fid) in m.TERMINAL:
            raise
        # The bridge owns the workspace lock here. Settle durable commitments
        # before returning normal saved-pause guidance; never replay or refund.
        from lifecycle import reconcile
        reconcile(s, workspace)
        _, data = info(s, fid)
        phase = m.state(s, fid)
        target = (data.get('workflow_stop') or {}).get('resume_phase') if phase == 'PAUSED' else phase
        stop = {'status': 'interrupted', 'resume_phase': target, 'stage': target,
                'error': str(exc), 'automatic_retry': False}
        move(s, fid, 'PAUSED', 'BRIDGE_INTERRUPTED', workflow_stop=stop)
        save(root, meta)
        return response(s, fid, claim_review_usage=workflow_usage(s, fid))
    except (BudgetStop,m.OrchestrationStop) as exc:
        fid=req.get('flow_id')
        if not fid or m.state(s,fid) not in ('BINDING','WORKSPACE','NEED_TOOL_APPROVAL','MAPPING','BRANCHING','CHILDREN'):raise
        phase=m.state(s,fid)
        stop = {
            'status': 'task_budget', 'stage': phase, 'error': str(exc),
            'resume_phase': phase, 'frame': exc.frame, 'automatic_retry': False}
        if getattr(exc, 'child_repair', None):
            stop['child_repair'] = exc.child_repair
        return budget_pause(s, meta, root, req, fid, phase, stop)
    except (PageFault, ContextBudgetExceeded) as exc:
        fid=req.get('_created_flow_id') or req.get('flow_id')
        if not fid:raise
        _,data=info(s,fid);phase=m.state(s,fid)
        target=(data.get('workflow_stop') or {}).get('resume_phase') if phase=='PAUSED' else phase
        if target not in ('CLASSIFYING','MAPPING','BRANCHING','CHILDREN','SYNTHESIS','CODING'):raise
        detail=fault_details(exc)
        stop={'status':'memory_fault','stage':target,'resume_phase':target,'error':str(exc),
              'memory_fault':detail,'automatic_retry':False}
        if target == 'CHILDREN' and isinstance(exc, ContextBudgetExceeded):
            sizing = getattr(exc, 'details', {})
            if sizing.get('branch_id') in (data.get('children') or []):
                stop['child_context'] = sizing
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
        category=exc.category
        hardware_wait=category in ('hardware_pressure','model_admission')
        fallback=propose(s,fid,data,req,exc.state,target) if hardware_wait and target not in ('WORKSPACE','NEED_TOOL_APPROVAL') else None
        if fallback:
            with s.db:
                m.transition(s,fid,target,'RESOURCE_MODEL_FALLBACK',
                             {'resource_fallback':fallback,'workflow_stop':None},fallback['bounded_switch'])
            save(root,meta)
            return response(s,fid,progress='Memory admission blocked '+fallback['from_model']+'. Auto will try '+
                fallback['model']+' with fresh paging and the same task, permissions and remaining budget.')
        stop={'status':'resource_wait','stage':target,'resume_phase':target,'error':str(exc),
              'blocker':category,'measurement':exc.state,'automatic_retry':False}
        # Bounded host recovery never retries inference or changes task grants.
        # Persist the original stop before any optional idle-model release.
        move(s,fid,'PAUSED','RESOURCE_STOPPED',workflow_stop=stop)
        save(root,meta)
        if not hardware_wait:
            return response(s,fid)
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


def child_rejection(s, fid, child):
    """Save a rejected worker as an inspectable stop, never a join candidate."""
    node = m.node(s, child)
    final = m.events(s, child)[-1]
    iid = final['data'].get('invocation_id')
    errors, model, receipt_id = [], None, None
    if iid:
        row = s.db.execute(
            'SELECT i.record_json,r.record_json,r.id FROM invocations i '
            'JOIN invocation_receipts r ON r.invocation_id=i.id AND r.session=i.session '
            'WHERE i.id=? AND i.session=?', (iid, node['session'])).fetchone()
        if not row or not s.verify_invocations(node['session']):
            raise InvalidUpdate('Rejected child receipt is absent or corrupt')
        envelope, receipt = json.loads(row[0]), json.loads(row[1])
        if ((envelope.get('orchestration') or {}).get('node_id') != child
                or receipt['status'] != 'quarantined'):
            raise InvalidUpdate('Rejected child receipt does not match its owner')
        errors = [str(error)[:500] for error in receipt['admission'].get('errors', [])[:4]]
        model, receipt_id = envelope['model'], row[2]
    eligible = final['kind'] == 'CHILD_REJECTED' and not node.get('replacement_of')
    used, limits = m.spent(s, child), m.effective_limits(s, node)
    eligible = eligible and all(used[key] < limits[key] for key in limits)
    stop = {'status': 'child_review_rejected', 'resume_phase': 'CHILDREN',
            'branch_id': child, 'invocation_id': iid, 'receipt': receipt_id,
            'model': model, 'errors': errors, 'retryable': eligible,
            'error': ('The child review failed its output checks. Its rejected output cannot be published or joined. '
                      + ('Resume may allocate one replacement from this branch’s remaining allowance.' if eligible else
                         'This branch has no further bounded replacement; its results remain quarantined.')),
            'automatic_retry': False}
    move(s, fid, 'PAUSED', 'CHILD_REVIEW_STOPPED', workflow_stop=stop)
    return response(s, fid)


def retry_child(s, fid, data, models, tier):
    stop = data['workflow_stop']
    if not stop.get('retryable'):
        return response(s, fid)
    child = stop['branch_id']
    retry_route = None
    if tier == 'auto':
        from model_routing import TIERS
        choices = [candidate for candidate in reversed(TIERS)
                   if models.get(candidate) and models[candidate] != stop.get('model')]
        if not choices:
            updated = dict(stop, error=stop['error'] + ' Auto has no alternate configured model. Choose another model before resuming.')
            move(s, fid, 'PAUSED', 'CHILD_REVIEW_STOPPED', workflow_stop=updated)
            return response(s, fid)
        retry_route = {'tier': choices[0], 'model': models[choices[0]], 'models': dict(models),
                       'reason': 'one bounded alternate-model review after rejected structured output'}
    try:
        replacement = m.replace_child(s, data['root_node'], child, retry_route)
    except m.OrchestrationStop as exc:
        updated = dict(stop, retryable=False, frame=exc.frame,
                       error='The remaining child/parent allocation cannot admit a fresh replacement. Spending is retained.')
        move(s, fid, 'PAUSED', 'CHILD_REVIEW_STOPPED', workflow_stop=updated)
        return response(s, fid)
    children = list(data['children'])
    children[children.index(child)] = replacement['id']
    move(s, fid, 'CHILDREN', 'CHILD_REPAIR_ALLOCATED', children=children, workflow_stop=None,
         previous_rejected_child=child, replacement_child=replacement['id'], resource_fallback=None)
    return response(s, fid, progress='Preparing one fresh child review with remaining allocation; rejected output stays quarantined.')


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


def evidence_gap(s,f,session,kind,notice,*,recoverable=True,target_status=None,conversation_id=None):
    target_status=target_status or ('resolved' if kind=='document' else 'missing')
    frame,budget=evidence_measure(s,session,target_status,'missing',recoverable)
    fid=f['id']
    data={'conversation_session':session}
    if conversation_id:
        data['conversation_id']=conversation_id
    if frame['decision']!='BRIDGE':
        message='The task budget is exhausted.' if frame['budget_exhausted'] else notice
        move(s,fid,'DONE','EVIDENCE_FIN',kernel=frame,request_notice=message,**data)
        return
    target='Requested document' if kind=='document' else 'Workspace scope'
    required='document_text' if kind=='document' else 'workspace_selection'
    file_target=document_file_target(f['prompt'], classified=bool(conversation_id)) if kind=='document' else None
    if file_target:
        target,required=file_target,'document_file'
        notice='Sutra can look for the named DOCX in this workspace and request permission to read it.'
    stop={'status':'evidence_bridge','resume_phase':'CLASSIFYING','error':notice,'budget':budget,
          'automatic_retry':False,'evidence_bridge':{'target_kind':kind,'target':target,
                                                       'required':required,'decision':frame['decision']}}
    move(s,fid,'PAUSED','EVIDENCE_BRIDGE',kernel=frame,workflow_stop=stop,**data)


def supply_evidence(s,f,d,req):
    bridge=d['workflow_stop']['evidence_bridge']
    supplied=req.get('evidence_text')
    if not isinstance(supplied,str) or not supplied.strip() or len(supplied)>1800:
        raise InvalidUpdate('Supply the requested evidence in a message of 1–1800 characters')
    session=d['conversation_session']
    frame,_=evidence_measure(s,session,'resolved','available',False)
    if frame['decision']!='ALLOW':
        move(s,f['id'],'DONE','EVIDENCE_FIN',kernel=frame,request_notice='The task budget is exhausted.')
        return response(s,f['id'])
    if bridge['target_kind']=='document':
        text=document_text(supplied,leading=True)
        if not text:raise InvalidUpdate('Reply with the document text below a Document text: heading')
        ref=record_document(s,session,f,text)
        move(s,f['id'],'CLASSIFYING','EVIDENCE_SUPPLIED',kernel=frame,workflow_stop=None,
             document_evidence=ref,routing_continuation=None)
    else:
        if re.fullmatch(r'use this workspace[.!?]*',supplied.strip(),re.IGNORECASE):
            work={'operation':'read','scope':'workspace','paths':[]}
        else:
            header,_,body=supplied.strip().partition('\n')
            paths=[line.strip() for line in body.splitlines() if line.strip()]
            if header.strip().lower()!='workspace files:' or not 1<=len(paths)<=8:
                raise InvalidUpdate('Reply Use this workspace or list up to eight paths below Workspace files:')
            work={'operation':'read','paths':paths}
        status,roots=workspace_roots(work)
        if status!='resolved':
            raise InvalidUpdate('Select paths within the workspace or reply Use this workspace')
        move(s,f['id'],'NEED_ARM','EVIDENCE_SUPPLIED',kernel=frame,workflow_stop=None,
             permission=work,source_roots=roots)
    return response(s,f['id'])


def workspace_document_evidence(s,workspace,f,d,req):
    from document_source import locate_document
    from document_review import approve

    stop=d['workflow_stop']
    bridge=stop['evidence_bridge']
    session=d['conversation_session']
    filename=document_file_target(f['prompt'], classified=bool(d.get('conversation_id')))
    if not filename:
        return response(s,f['id'])
    if req['event']=='locate_document':
        frame,_=evidence_measure(s,session,'resolved','missing',True)
        if frame['decision']!='BRIDGE':
            move(s,f['id'],'DONE','EVIDENCE_FIN',kernel=frame,request_notice='The task budget is exhausted.')
            return response(s,f['id'])
        lookup=locate_document(workspace,filename)
        messages={'found':'Confirm a one-time read of the named DOCX to continue.',
                  'ambiguous':'Choose which matching DOCX to read.',
                  'missing':'The named DOCX was not found in this workspace.',
                  'blocked':'The named DOCX cannot be read within this workspace scope.',
                  'scan_limit':'Sutra reached the lookup limit. Use the workspace-relative path in a new request.'}
        updated=dict(stop,error=messages.get(lookup['status'],'Sutra could not locate the named DOCX.'),
                     evidence_bridge=dict(bridge,target=filename,required='document_file'),document_lookup=lookup)
        move(s,f['id'],'PAUSED','DOCUMENT_LOCATED',kernel=frame,workflow_stop=updated)
        return response(s,f['id'])
    lookup=stop.get('document_lookup') or {}
    path=req.get('document_path')
    if (req.get('confirmed') is not True or not isinstance(path,str)
            or lookup.get('status') not in ('found','ambiguous') or path not in lookup['sizes']):
        raise InvalidUpdate('Document read requires confirmation of a located file.')
    try:
        ref=approve(s,session,workspace,f,path,lookup['sizes'][path])
    except InvalidUpdate as exc:
        _,budget=evidence_measure(s,session,'resolved','missing',True)
        updated=dict(stop,error=str(exc),budget=budget,document_lookup={'status':'read_failed'})
        move(s,f['id'],'PAUSED','DOCUMENT_READ_STOPPED',kernel=getattr(exc,'frame',None),workflow_stop=updated)
        return response(s,f['id'])
    frame,_=evidence_measure(s,session,'resolved','available',False)
    if frame['decision']!='ALLOW':
        move(s,f['id'],'DONE','EVIDENCE_FIN',kernel=frame,document_evidence=ref,
             request_notice='The task budget is exhausted.')
        return response(s,f['id'])
    move(s,f['id'],'CLASSIFYING','EVIDENCE_SUPPLIED',kernel=frame,workflow_stop=None,
         document_evidence=ref,routing_continuation=None)
    return response(s,f['id'])


def advance_document_stage(s, root, flow, data, request, provider):
    from document_stages import extract, plan, synthesis_packet
    from review_budget import require

    sid = request['conversation_session']
    fid = flow['id']
    staged = data.get('document_stage_plan')
    if staged is None:
        staged = plan(s, sid, flow['prompt'], request['document_evidence'], request['parent'],
                      request['tier'], request['models'])
        if staged is None:
            return None
        final_calls = 2 if staged['version'] == 2 else 1
        frame = require(s, sid, {'calls': len(staged['sections']) + final_calls})
        move(s, fid, 'CLASSIFYING', 'DOCUMENT_STAGE_PLAN', kernel=frame, document_stage_plan=staged)
        synthesis = (f"Evidence review starts after a first answer from {staged['synthesis_model']}."
                     if staged['version'] == 2 else f"Synthesis is assigned to {staged['synthesis_model']}.")
        progress = f"Reading {len(staged['sections'])} document sections with {staged['extract_model']}. {synthesis}"
        return response(s, fid, document_stage_progress=progress)
    receipts = data.get('document_stage_receipts', [])
    count = len(staged['sections'])
    if len(receipts) == count:
        if not synthesis_packet(s, sid, fid)['evidence']:
            move(s, fid, 'DONE', 'DOCUMENT_STAGE_EMPTY', request_notice=(
                'Sutra found no supporting quotations for this request in the approved document. '
                'Ask about a specific section or provide more source material.'))
            return response(s, fid)
        request['document_stage_flow'] = fid
        return None
    result = extract(s, sid, root, fid, staged, receipts, provider=provider('document_extract'))
    if not result['admission']['admitted']:
        stop_document_stage(s, fid, result['admission']['errors'], 'Document evidence extraction stopped.')
        progress = f"Evidence extraction stopped at section {len(receipts) + 1} of {count}."
    else:
        move(s, fid, 'CLASSIFYING', 'DOCUMENT_STAGE_EXTRACTED',
             document_stage_receipts=[*receipts, result['invocation_id']])
        progress = f"Recorded evidence for section {len(receipts) + 1} of {count}."
    step = {'result': result, 'route': result['route'],
            'routing_followup': {'kind': 'document_extract', 'reason': progress}}
    return response(s, fid, routing_step=step, progress=progress)


def stop_document_stage(s, fid, errors, title):
    stop = {'status': 'document_stage', 'resume_phase': 'CLASSIFYING',
            'error': title + ' ' + ' '.join(errors), 'automatic_retry': False}
    move(s, fid, 'PAUSED', 'DOCUMENT_STAGE_STOPPED', workflow_stop=stop)


def advance_document_verdict(s, meta, root, fid, data, request, axis, provider):
    from conversation import answer, read
    from conversation_routing import next_tier
    from document_stages import (CANDIDATE_EVENT, ESCALATION_EVENT, PUBLICATION_EVENT,
                                recover_candidate, synthesis_route, verification_candidate)
    from document_verdict_review import ROLE, review
    from model_routing import TIERS
    from review_budget import require

    sid = request['conversation_session']
    plan = data['document_stage_plan']
    route = synthesis_route(s, fid, plan)
    invocation = data.get('document_candidate_id')
    if not invocation:
        candidate = recover_candidate(s, sid, fid, route)
        if candidate is None:
            require(s, sid, {'calls': 2})
            candidate = answer(s, meta, root, request, provider=provider('conversation'),
                               axis=axis, orchestrated=True)
        result = candidate['result']
        if result['admission']['admitted']:
            move(s, fid, 'CLASSIFYING', CANDIDATE_EVENT, document_candidate_id=result['invocation_id'])
            progress = f"Checking the draft from {route['model']} against the document quotations."
        else:
            progress = 'Document synthesis stopped.'
            stop_document_stage(s, fid, result['admission']['errors'], progress)
        step = {'result': {key: value for key, value in result.items() if key != 'accepted_output'},
                'route': route, 'routing_followup': {'kind': 'document_synthesis', 'reason': progress}}
        return response(s, fid, routing_step=step, progress=progress)

    candidate = verification_candidate(s, sid, fid)
    result = review(s, sid, root, fid, provider=provider(ROLE))
    review_route = result['route']
    assessed = None
    if not result['admission']['admitted']:
        progress = 'Document evidence review stopped.'
        stop_document_stage(s, fid, result['admission']['errors'], progress)
    else:
        verdict = result['accepted_output']
        assessed = {**verdict, 'model': review_route['model'], 'synthesis_model': candidate['model'],
                    'invocation_id': result['invocation_id'], 'authority': 'model_assessment'}
        if verdict['verdict'] == 'supported':
            move(s, fid, 'DONE', PUBLICATION_EVENT, conversation_id=invocation,
                 document_verification=assessed)
            chat = read(s, invocation)
            chat.update(route=route, memory_used=bool(request['parent']))
            progress = 'Document evidence review complete.'
        elif verdict['verdict'] == 'insufficient_evidence':
            progress = 'The evidence review found a gap in the supplied quotations.'
            move(s, fid, 'DONE', 'DOCUMENT_EVIDENCE_INSUFFICIENT', document_verification=assessed,
                 request_notice='Sutra could not establish the conclusion from the selected quotations. '
                                'Point to a specific section or provide the missing source to continue.')
        else:
            tried = [plan['models'][rank] for rank in TIERS[:TIERS.index(route['tier']) + 1]]
            tier = next_tier(route, tried)
            if tier is None:
                progress = 'Document evidence review did not support the conclusion.'
                move(s, fid, 'DONE', 'DOCUMENT_SYNTHESIS_UNSUPPORTED', document_verification=assessed,
                     request_notice='Sutra withheld the verdict after the evidence review found an unsupported conclusion.')
            else:
                try:
                    frame = require(s, sid, {'calls': 2})
                except BudgetStop as exc:
                    progress = 'Evidence review found a problem in the draft. The next stage exceeds the remaining allocation.'
                    stop = {'status': 'routing_budget', 'resume_phase': 'CLASSIFYING', 'error': str(exc),
                            'budget': exc.frame, 'automatic_retry': False}
                    move(s, fid, 'PAUSED', 'ROUTING_STOPPED', kernel=exc.frame, workflow_stop=stop,
                         document_verification=assessed)
                else:
                    progress = f"Evidence review found a problem in the draft. Trying {plan['models'][tier]} with the same quotations."
                    move(s, fid, 'CLASSIFYING', ESCALATION_EVENT, kernel=frame, document_synthesis_tier=tier,
                         document_candidate_id=None, document_verification=assessed)

    if assessed and assessed['verdict'] == 'supported':
        # The candidate's own call was already reported on the preceding step.
        chat['result']['token_usage'] = result['token_usage']
        return response(s, fid, conversation=chat)
    step = {'result': {key: value for key, value in result.items() if key != 'accepted_output'},
            'route': review_route, 'routing_followup': {'kind': ROLE, 'reason': progress}}
    return response(s, fid, routing_step=step, progress=progress)


def _handle(s,meta,workspace,root,req,providers=None):
    # Only the extension's private bridge can supply arming/permission events.
    # Model proposals cannot invoke this endpoint or set permissions.
    from conversation import answer as converse, chat_axis, session_for
    event=req.get('event','step');models=req.get('models');tier=req.get('tier','auto')
    def provider(stage):return providers(stage) if providers else None
    if event=='pending':
        pending=pending_flows(s,workspace)
        return response(s,pending[0]) if pending else {'state':'DONE','flow_id':None}
    if event in ('start','start_existing','start_review'):
        prompt=req.get('prompt','')
        if not isinstance(prompt,str) or not prompt.strip():raise InvalidUpdate('Empty workflow request')
        chat=req.get('chat_key')
        axis=chat_axis(workspace,chat)
        if event=='start' and req.get('parent'):
            session_for(s,{'prompt':prompt,'parent':req['parent']},axis=axis)
        retained=retained_chat_document(s,workspace,chat,req.get('parent'),prompt) if event=='start' else None
        notice=None if retained else (document_input_notice(prompt) if event=='start' else document_workspace_notice(prompt))
        if not retained and not notice and event=='start':
            notice=referenced_document_notice(s,workspace,prompt,chat,req.get('parent'),req.get('prior_prompt'))
            if notice and chat:
                from task_stack import view
                previous=view(s,workspace,chat)['focused']
                if previous and m.state(s,previous)=='PAUSED':
                    _,data=info(s,previous)
                    if (data.get('workflow_stop') or {}).get('evidence_bridge',{}).get('target_kind')=='document':
                        latest=latest_chat_flow(s,workspace,chat)
                        if latest and latest[0]['id']==previous:return response(s,previous)
        offer = (retained_chat_document(s,workspace,chat,req.get('parent'),prompt,implicit=True)
                 if event=='start' and not retained and not notice else None)
        # Classify ordinary chat before asking the user to replace saved work.
        if not notice and not chat and event!='start' and pending_flows(s,workspace):
            raise InvalidUpdate('A saved workflow exists; explicitly resume or discard it before starting another request')
        if not notice and event=='start_existing':
            from conversation import workspace_handoff,read
            if chat:
                from task_stack import owner
                previous=req.get('action_flow_id')
                if not previous or owner(s,workspace,previous)!=chat:
                    raise InvalidUpdate('Continuation requires the original Sutra action workflow')
                _,prior=info(s,previous)
                settled=m.events(s,previous)[-1]
                if (settled['to']!='DONE' or settled['kind']!='ACTION_SETTLED'
                        or settled['data'].get('status') not in ('applied','process_exited_zero','process_failed')
                        or prior.get('conversation_id')!=req.get('conversation_id')):
                    raise InvalidUpdate('Continuation requires a settled Sutra action')
                if s.db.execute("SELECT 1 FROM orchestration_nodes WHERE json_extract(record_json,'$.action_flow_id')=?",
                                (previous,)).fetchone():
                    raise InvalidUpdate('This action has already been continued')
                meta.update(flow_metadata(root,workspace,previous))
            workspace_handoff(s,meta.get('active'),req.get('conversation_id'),prompt)
            proposal=read(s,req['conversation_id'])['result']['accepted_output']
            if proposal['intent']!='workspace':raise InvalidUpdate('Continuation requires an admitted workspace intent')
        with s.db:
            f=m.create(s,'flow',objective=prompt,workspace=str(workspace),prompt=prompt,
                       conversation_parent=req.get('parent'),chat_key=chat,
                       action_flow_id=req.get('action_flow_id') if chat and event=='start_existing' else None)
            m.transition(s,f['id'],'CLASSIFYING','REQUEST')
            if chat and (event=='start' or not notice):
                from task_stack import select,view
                select(s,workspace,chat,f['id'],expected_cursor=view(s,workspace,chat)['cursor'],attach=True)
                meta['_flow']=f['id']
                save(root,meta)
        fid=f['id']
        req['_created_flow_id']=fid
        if retained:
            session,ref=retained
            from conversation import document_evidence
            session_for(s,{'prompt':prompt,'parent':req['parent'],'conversation_session':session},axis=axis)
            document_evidence(s,session,ref,prompt,req['parent'])
            if not repage_document(s,fid,session,ref):
                return response(s,fid)
        elif offer:
            session,ref=offer
            move(s,fid,'CLASSIFYING','DOCUMENT_OFFERED',conversation_session=session,document_offer=ref)
        if notice:
            if event=='start':
                session=session_for(s,{'prompt':prompt,'parent':req.get('parent')},axis=axis)
                evidence_gap(s,f,session,'document',notice)
            else:
                move(s,fid,'DONE','DOCUMENT_INPUT_REQUIRED',request_notice=notice)
            return response(s,fid)
        if event=='start_review':
            sid=meta.get('active')
            if not req.get('armed') or not sid or req.get('binding')!=identity(s,sid) or not s.snapshot(sid)['constraints'].get('auto_review'):raise InvalidUpdate('Explicit review requires a user-created read-only task')
            roots=s.snapshot(sid)['constraints']['allowed_paths']
            frame,_=evidence_measure(s,sid,'resolved','available',False)
            if frame['decision']!='ALLOW':
                move(s,fid,'DONE','EVIDENCE_FIN',kernel=frame,request_notice='The task budget is exhausted.')
            else:
                move(s,fid,'NEED_ARM','USER_REVIEW',kernel=frame,conversation_id=req.get('conversation_id'),
                     permission={'operation':'read','paths':[]},source_roots=roots)
            return response(s,fid)
        if event=='start_existing':
            work=proposal.get('workspace_request') or {'operation':'read','paths':[]}
            work,status,roots=workspace_target(work,prompt)
            if status!='resolved':
                raise InvalidUpdate('Continuation needs the original admitted workspace target')
            move(s,fid,'NEED_ARM','ACTION_CONTINUATION',conversation_id=req['conversation_id'],
                 permission=work,source_roots=roots)
            return response(s,fid)
    else:
        fid=req.get('flow_id');f,d=info(s,fid)
        if f['workspace']!=str(workspace):raise InvalidUpdate('Workflow belongs to another workspace')
        if event=='inspect':
            extra={'prompt':f['prompt']}
            notice=saved_document_notice(s,workspace,f,m.state(s,fid),d)
            if notice:return response(s,fid,request_notice=notice,**extra)
            if (m.state(s,fid) == 'DONE' and d.get('conversation_id')
                    and (d.get('document_verification') or {}).get('verdict') == 'supported'):
                from conversation import read
                extra['conversation'] = read(s, d['conversation_id'])
            if m.state(s,fid)=='NEED_TOOL_APPROVAL':extra['capability_proposal']=d.get('capability_proposal')
            if m.state(s,fid)=='NEED_PERMISSION' and not recovery(s,fid):extra.update(permission=permission(s,meta['active'],d['permission']),bound_task=identity(s,meta['active']))
            return response(s,fid,**extra)
        if req.get('cursor')!=response(s,fid)['cursor']:raise InvalidUpdate('Workflow event is stale or replayed')
    f,d=info(s,fid);phase=m.state(s,fid);prompt=f['prompt']
    if event in ('supply_evidence','locate_document','approve_document') and phase=='PAUSED' and d.get('conversation_session'):
        session_for(s,{'prompt':prompt,'parent':f.get('conversation_parent'),
                       'conversation_session':d['conversation_session']},
                    axis=chat_axis(workspace,f.get('chat_key')))
    if event=='supply_evidence':
        if phase!='PAUSED' or (d.get('workflow_stop') or {}).get('status')!='evidence_bridge':
            raise InvalidUpdate('This workflow is not waiting for evidence')
        return supply_evidence(s,f,d,req)
    if event in ('locate_document','approve_document'):
        stop=d.get('workflow_stop') or {}
        if (phase!='PAUSED' or stop.get('status')!='evidence_bridge'
                or stop['evidence_bridge']['target_kind']!='document'):
            raise InvalidUpdate('This workflow is not waiting for document evidence.')
        return workspace_document_evidence(s,workspace,f,d,req)
    if event!='cancel':
        notice=saved_document_notice(s,workspace,f,phase,d)
        if notice:return response(s,fid,request_notice=notice)
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
        terminal_restart = incompatibility.get('terminal_review') is True
        if terminal_restart and req.get('confirmed') is not True:
            raise InvalidUpdate('Restarting an unpublished completed review requires explicit confirmation')
        if terminal_restart:
            from task_stack import view
            if req.get('stack_cursor') != view(s, workspace, f['chat_key'])['cursor']:
                raise InvalidUpdate('Task stack changed before review restart; inspect its current state')
        if not req.get('armed') or meta.get('active')!=sid or req.get('binding')!=identity(s,sid):
            raise InvalidUpdate('Restart requires the same user-armed task and current binding')
        correction = None
        if incompatibility['kind'] == 'source_scope_correction':
            if req.get('confirmed') is not True:
                raise InvalidUpdate('Read-scope correction requires explicit user confirmation')
            from native_access import read_scope_correction
            correction = read_scope_correction(s, f, d, parent['binding'])
            if not correction:
                raise InvalidUpdate('The confirmed read-scope correction changed')
        conversation_id = d.get('conversation_id')
        if conversation_id and not correction:
            from conversation import workspace_handoff
            session_for(s,{'prompt':prompt,'parent':conversation_id},axis=chat_axis(workspace,f.get('chat_key')))
            workspace_handoff(s,sid,conversation_id,prompt)
        # This is an explicit host UI event. Keep the task and all consumed use.
        # Policy/source restarts retain scope; a separately confirmed router
        # target correction versions only read scope. Old model results never
        # become new evidence or action authority.
        chat=f.get('chat_key')
        if correction:
            from execution_sessions import finish, idle
            idle(s, parent['id'])
            finish(s, fid, cancelled=True)
            if parent['kind'] != 'execution':
                with s.db:
                    m.abandon_children(s, parent['id'], 'Read-only source target corrected after confirmation')
            from native_access import correct_read_scope
            correct_read_scope(s, meta, workspace, correction)
            save(root, meta)
        with s.db:
            from execution_sessions import finish
            if not terminal_restart: finish(s, fid, cancelled=True)
            if chat and not terminal_restart:
                from task_stack import close, select, view
                close(s,workspace,chat,fid,expected_cursor=view(s,workspace,chat)['cursor'])
                m.transition(s,fid,'CANCELLED','CANCELLED',{'spending_retained':True})
            elif not terminal_restart:
                m.abandon_children(s,parent['id'],'User restarted review after a policy or source change')
                m.cancel(s,fid)
            new=m.create(s,'flow',objective=prompt,workspace=str(workspace),prompt=prompt,
                         conversation_parent=conversation_id,restarted_from=fid,chat_key=chat)
            restart_kind = ('SOURCE_SCOPE_RESTART' if correction else
                            'SOURCE_RESTART' if incompatibility['kind']=='provenance_changed' else 'POLICY_RESTART')
            m.transition(s,new['id'],'CLASSIFYING',restart_kind,{'previous_flow':fid})
            m.transition(s,new['id'],'NEED_ARM','USER_REVIEW',{'conversation_id':conversation_id,
                         'permission':correction['permission'] if correction else d.get('permission') or {'operation':'read','paths':[]},
                         **({'source_roots': correction['read_scope']} if correction else {})})
            if chat:
                from task_stack import select, view
                select(s,workspace,chat,new['id'],expected_cursor=(req['stack_cursor'] if terminal_restart
                    else view(s,workspace,chat)['cursor']),attach=True)
                meta['_flow']=new['id']
                save(root,meta)
        return response(s,new['id'],progress=('Review restarted with the original workspace target; other constraints, consumed spending and total allocation retained.'
                        if correction else 'Review restarted with the current source snapshot; task constraints and consumed budget retained.'
                        if incompatibility['kind']=='provenance_changed' else
                        'Review restarted under the current policy; task constraints and consumed budget retained.'))
    if incompatibility:return response(s,fid)
    if phase=='PAUSED':
        stop = d.get('workflow_stop') or {}
        if stop.get('status')=='evidence_bridge':
            return response(s,fid)
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
        saved_phase_stops = ('RESOURCE_STOPPED', 'ROUTING_STOPPED', 'MEMORY_STOPPED',
                             'CLAIM_REVIEW_STOPPED', 'REVIEWER_UNAVAILABLE', 'BRIDGE_INTERRUPTED', 'DOCUMENT_STAGE_STOPPED', 'CHILD_SLICE_STOPPED', 'CHILD_REVIEW_STOPPED')
        stopped = [e for e in m.events(s, fid)
                   if e['kind'] in (*saved_phase_stops, 'MAPPING_STOPPED', 'CONTEXT_STOPPED')]
        if not stopped:raise InvalidUpdate('Paused workflow requires inspection before recovery')
        last=stopped[-1]
        target=(last['data']['workflow_stop']['resume_phase'] if last['kind'] in saved_phase_stops
                else last['from'] if last['kind']=='CONTEXT_STOPPED' else 'MAPPING')
        if target!='CLASSIFYING':
            if not req.get('armed'):raise InvalidUpdate('Workspace workflow requires a user-armed harness')
            if d.get('binding')!=identity(s,meta.get('active')) or req.get('binding')!=d['binding']:
                raise InvalidUpdate('Workflow task, calibration or permissions changed')
        if target != 'CLASSIFYING' and provenance_changed(s, workspace, fid, d):
            return response(s, fid)
        if last['kind'] == 'CHILD_REVIEW_STOPPED':
            return retry_child(s, fid, d, models, tier)
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
        axis=chat_axis(workspace,f.get('chat_key'))
        notice=None if d.get('document_evidence') else document_input_notice(prompt)
        if notice:
            session=session_for(s,{'prompt':prompt,'parent':f.get('conversation_parent'),
                                   'conversation_session':d.get('conversation_session')},axis=axis)
            evidence_gap(s,f,session,'document',notice)
            return response(s,fid)
        from conversation_routing import RoutingBlocked
        request={'prompt':prompt,'parent':f.get('conversation_parent'),'models':models,'tier':tier,
                 'conversation_session':d.get('conversation_session'),'routing_continuation':d.get('routing_continuation'),'one_step':True}
        if d.get('document_evidence'):
            request['document_evidence']=d['document_evidence']
        elif d.get('document_offer'):
            request['document_offer_flow']=fid
        request['conversation_session']=session_for(s,request,axis=axis)
        if not d.get('conversation_session'):
            move(s,fid,'CLASSIFYING','CONVERSATION_BOUND',conversation_session=request['conversation_session'])
        try:
            if 'document_evidence' in request:
                staged = advance_document_stage(s, root, f, d, request, provider)
                if staged is not None:
                    return staged
                if d.get('document_stage_plan', {}).get('version') == 2:
                    return advance_document_verdict(s, meta, root, fid, d, request, axis, provider)
            chat=converse(s,meta,root,request,provider=provider('conversation'),axis=axis,orchestrated=True)
        except (ContextBudgetExceeded,BudgetStop,m.OrchestrationStop,RoutingBlocked) as exc:
            if isinstance(exc,ContextBudgetExceeded):observe_fault(s,request['conversation_session'],exc)
            stop={'status':'routing_budget','resume_phase':'CLASSIFYING','error':str(exc),
                  'budget':getattr(exc,'frame',None),'automatic_retry':False}
            move(s,fid,'PAUSED','ROUTING_STOPPED',workflow_stop=stop)
            return response(s,fid)
        r=chat['result']
        if not r['admission']['admitted']:
            if request.get('document_stage_flow'):
                progress = 'Document synthesis stopped.'
                stop_document_stage(s, fid, r['admission']['errors'], progress)
                chat['routing_followup'] = {'kind': 'document_synthesis', 'reason': progress}
                return response(s, fid, routing_step=chat, progress=progress)
            move(s,fid,'DONE','REJECTED',conversation_id=r['invocation_id']);return response(s,fid,conversation=chat)
        next_step=chat.get('routing_followup')
        if next_step:
            move(s,fid,'CLASSIFYING','ROUTING_CHECKPOINT',routing_continuation=r['invocation_id'],next_routing=next_step)
            if next_step['kind']=='blocked':
                stop={'status':'routing_choice','resume_phase':'CLASSIFYING','error':next_step['reason'],'automatic_retry':False}
                move(s,fid,'PAUSED','ROUTING_STOPPED',workflow_stop=stop)
            return response(s,fid,routing_step=chat,progress=next_step['reason'])
        p=r['accepted_output']
        work=p.get('workspace_request') or {'operation':'read','paths':[]}
        # A catalog review cannot read DOCX contents. Route an admitted read
        # of exactly the document named by the user through scoped approval.
        named_doc = (document_file_target(prompt, classified=True)
                     if p['intent']=='workspace' and work['operation']=='read' and not work.get('scope') else None)
        if p['intent']=='document' or (named_doc and work['paths']==[named_doc]):
            session=request['conversation_session']
            if p.get('document_request')=='previous_snapshot':
                from conversation import document_evidence
                ref=dict(d['document_offer'], selected_by=r['invocation_id'])
                document_evidence(s,session,ref,prompt,request['parent'])
                if not repage_document(s,fid,session,ref):
                    return response(s,fid,conversation=chat)
                progress='Loading the approved document snapshot from this chat.'
                chat['routing_followup']={'kind':'document_page_in','reason':progress}
                return response(s,fid,routing_step=chat,progress=progress)
            evidence_gap(s,f,session,'document',p.get('context_question') or DOCUMENT_TEXT_REQUIRED,
                         conversation_id=r['invocation_id'])
            return response(s,fid,conversation=chat)
        if p['intent']!='workspace':
            move(s,fid,'DONE','CLASSIFIED',conversation_id=r['invocation_id'])
            return response(s,fid,conversation=chat)
        work,status,roots=workspace_target(work,prompt)
        if status!='resolved':
            recoverable=status=='missing' and work['operation']=='read'
            guidance=('Reply **Use this workspace**, or list up to eight relative paths below a **Workspace files:** heading.'
                      if recoverable else 'Name the authorized workspace files for this operation.')
            evidence_gap(s,f,request['conversation_session'],'workspace',guidance,recoverable=recoverable,target_status=status,
                         conversation_id=r['invocation_id'])
            return response(s,fid,conversation=chat)
        frame,_=evidence_measure(s,request['conversation_session'],'resolved','available',False)
        if frame['decision']!='ALLOW':
            move(s,fid,'DONE','EVIDENCE_FIN',kernel=frame,conversation_id=r['invocation_id'],
                 request_notice='The task budget is exhausted.')
            return response(s,fid,conversation=chat)
        target='NEED_ARM' if p['intent']=='workspace' else 'DONE'
        other=[] if f.get('chat_key') else pending_flows(s,workspace,exclude=fid)
        if target=='NEED_ARM' and other:
            # Only the host can resolve this conflict. No source reads or grants
            # are made, and the existing task/allocations remain untouched.
            move(s,fid,'DONE','WORKSPACE_DEFERRED',kernel=frame,conversation_id=r['invocation_id'],saved_flow_id=other[0])
            return response(s,fid,conversation=chat,deferred_flow_id=other[0])
        move(s,fid,target,'CLASSIFIED',kernel=frame,conversation_id=r['invocation_id'],permission=work,source_roots=roots)
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
            path=metadata_path(root,meta.get('_flow'))
            roots=d.get('source_roots')
            if not roots:raise InvalidUpdate('A workspace review requires an admitted source target')
            work=d['permission']
            catalog=not (work['operation']=='capability' and work.get('capability_action')=='install')
            _handle(Path(root),Path(workspace),{'op':'auto_new','goal':prompt,'roots':roots,'conversation_id':d['conversation_id']},
                    metadata=path,prepare_catalog=catalog)
            meta.clear();meta.update(json.loads(path.read_text()));sid=meta['active']
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
            child = pending[0]
            if m.state(s,child) in m.TERMINAL:
                return child_rejection(s, fid, child)
            retry = m.node(s, child).get('retry_route') or {}
            mode = effective(d,models,tier)
            if mode == 'auto' and retry.get('models') == models:
                mode = retry['tier']
            out=run(s,meta,workspace,root,child,models,mode,provider('subagent'))
            latest = {'last_child': child, 'last_model': out['route']['model']}
            if m.state(s, child) in ('QUARANTINED', 'CANCELLED'):
                return child_rejection(s, fid, child)
            if out.get('slice_status') == 'timeout':
                reason = out.get('slice_reason', 'timeout')
                message = {
                    'output_limit': 'The child review reached its output allowance before returning a checkpoint.',
                    'interrupted': 'The child review was interrupted before returning a checkpoint.',
                }.get(reason, 'The child review reached its time limit before returning a checkpoint.')
                stop = {'status': 'child_slice_timeout', 'resume_phase': 'CHILDREN',
                        'error': message, 'slice_reason': reason, 'slice_ordinal': out['slice_ordinal'],
                        'branch_id': child, 'automatic_retry': False}
                move(s, fid, 'PAUSED', 'CHILD_SLICE_STOPPED', workflow_stop=stop, **latest)
                return response(s, fid, child=out)
            if out.get('slice_status') == 'checkpoint':
                ordinal = out['slice_ordinal']
                move(s, fid, 'CHILDREN', 'CHILD_SLICE_SAVED', slice_ordinal=ordinal, **latest)
                return response(s, fid, child=out,
                                progress=f'Child review saved slice {ordinal}. Continuing the partition.')
            move(s, fid, 'CHILDREN', 'CHILD_FINISHED', **latest)
            return response(s, fid, child=out, progress='Child review '+m.state(s, child))
        if m.state(s,parent['id'])=='READY':
            joined=[e for e in m.events(s,parent['id']) if e['kind']=='JOIN']
            ids=joined[-1]['data']['invocations'] if joined else []
            if len(ids)!=len(d['children']) or any(m.events(s,i)[-1]['data'].get('invocation_id')!=iid for i,iid in zip(d['children'],ids)):raise InvalidUpdate('Recovered join does not match children')
        else:ids=m.join(s,parent['id'])
        move(s,fid,'SYNTHESIS','CHILDREN_JOINED',child_results=ids)
        return response(s,fid)
    if phase in ('SYNTHESIS','CODING'):
        from invocation_timing import BridgeStepRequired
        from resource_routing import effective
        from static_analysis import StaticSourceChanged
        from review_followup import FollowupSourceChanged
        if phase == 'SYNTHESIS' and provenance_changed(s, workspace, fid, d):
            return response(s, fid)
        request={'prompt':prompt,'models':models,'tier':effective(d,models,tier),'conversation_id':d['conversation_id'],**d['binding'],'child_results':d.get('child_results',[]),'orchestration_parent':d.get('result_parent',parent['id']), '_parent_step': phase == 'SYNTHESIS'}
        claim_retry = d.get('claim_review_retry') if phase == 'SYNTHESIS' else None
        try:
            with m.scope(parent['id']):
                if phase=='CODING':
                    from native_runtime import turn
                    out=turn(s,meta,workspace,root,request,provider=provider('coder'))
                elif claim_retry:
                    from chat_runtime import resume_claim_review
                    out=resume_claim_review(s,meta,workspace,root,request,claim_retry,
                                            review_provider=provider('synthesis'))
                else:
                    from chat_runtime import answer, recover_parent_checkpoint, resume_parent_review
                    checkpoint = d.get('parent_checkpoint')
                    if phase == 'SYNTHESIS' and not checkpoint:
                        checkpoint = recover_parent_checkpoint(s, sid, workspace, request)
                        if checkpoint:
                            move(s, fid, 'SYNTHESIS', 'PARENT_CANDIDATE_RECOVERED', parent_checkpoint=checkpoint)
                            save(root, meta)
                            return response(s, fid, progress='The saved draft is ready for claim review.',
                                            claim_review_usage=workflow_usage(s, fid))
                    out = (resume_parent_review(s, meta, workspace, root, request, checkpoint,
                                                review_provider=provider('synthesis')) if checkpoint else
                           answer(s,meta,workspace,root,request,provider=provider('synthesis')))
        except BridgeStepRequired:
            # No model dispatch or charge occurred. Keep the same saved work
            # and let the next bridge step admit a complete bounded slice.
            move(s, fid, phase, 'MODEL_SLICE_DEFERRED')
            save(root, meta)
            return response(s, fid, progress='Continuing the saved review in a fresh execution slice.',
                            claim_review_usage=workflow_usage(s, fid))
        except (StaticSourceChanged, FollowupSourceChanged) as exc:
            if not claim_retry: raise
            stop = {'status': 'provenance_changed', 'error': str(exc)}
            move(s, fid, 'PAUSED', 'GIT_SNAPSHOT_CHANGED', workflow_stop=stop)
            save(root,meta)
            return response(s,fid)
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
            if out.get('evidence_discovery_pending'):
                move(s, fid, 'SYNTHESIS', 'PARENT_EVIDENCE_CHECKPOINT',
                     parent_evidence=out['evidence_discovery_pending'])
                save(root, meta)
                return response(s, fid, progress='Gathering source evidence for the review.',
                                claim_review_usage=workflow_usage(s, fid))
            if out.get('parent_generation_pending'):
                move(s, fid, 'SYNTHESIS', 'PARENT_GENERATION_CHECKPOINT',
                     parent_generation=out['parent_generation_pending'])
                save(root, meta)
                return response(s, fid, progress='Review attempt saved. Continuing its remaining repair allowance.',
                                claim_review_usage=workflow_usage(s, fid))
            if out.get('parent_checkpoint'):
                move(s, fid, 'SYNTHESIS', 'PARENT_REVIEW_CHECKPOINT',
                     parent_checkpoint=out['parent_checkpoint'], claim_review_retry=None,
                     parent_review_progress=out.get('claim_review_pending'))
                save(root, meta)
                return response(s, fid, progress='Draft saved. Continuing the claim review.',
                                claim_review_usage=workflow_usage(s, fid))
            claim_stop = out.get('claim_review_stop')
            if claim_stop:
                if claim_stop.get('status') == 'reviewer_unavailable':
                    from chat_runtime import parent_checkpoint
                    checkpoint = d.get('parent_checkpoint') or parent_checkpoint(s, sid, claim_stop['candidate_id'])
                    move(s, fid, 'PAUSED', 'REVIEWER_UNAVAILABLE', workflow_stop=claim_stop,
                         parent_checkpoint=checkpoint, parent_review_progress=claim_stop)
                    save(root, meta)
                    return response(s, fid, claim_review_usage=workflow_usage(s, fid))
                if claim_stop.get('status') == 'claim_review_timeout' and claim_stop.get('automatic_retry') is True:
                    move(s, fid, 'SYNTHESIS', 'CLAIM_REVIEW_CHECKPOINT', claim_review_retry=claim_stop,
                         parent_review_progress=claim_stop)
                    save(root, meta)
                    return response(s, fid, progress='Claim review saved. Continuing its remaining attempt with thinking off.',
                                    claim_review_usage=workflow_usage(s, fid))
                move(s, fid, 'PAUSED', 'CLAIM_REVIEW_STOPPED', workflow_stop=claim_stop,
                     claim_review_retry=claim_stop)
                save(root,meta)
                return response(s,fid,claim_review_usage=out['result']['token_usage'])
            out = with_provenance(out, d.get('git_provenance'))
        save(root,meta)
        action=(out['result'].get('accepted_output') or {}).get('action',{}).get('kind')
        awaiting=phase=='CODING' and action in ('edit','run_tests')
        move(s,fid,'AWAITING_ACTION' if awaiting else 'DONE','ACTION_PROPOSED' if awaiting else 'WORKFLOW_FINISHED',invocation_id=out['result']['invocation_id'],
             observation_delivery='awaiting_action' if awaiting else 'admitted' if out['result']['admission']['admitted'] else 'withheld')
        return response(s,fid,data=out)
    raise InvalidUpdate('Workflow needs explicit recovery: '+phase)
