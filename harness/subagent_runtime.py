"""Read-only child reviews with inherited contracts and receipt-backed Smriti pages."""
import copy,json,uuid
from context_manager import InvalidUpdate,ContextBudgetExceeded,canonical,sha
from paging import Budget
from invocation_gateway import InvocationGateway
from karma_adapter import KarmaAdapter
from review_role import constraint_rows
import orchestration as machine
import chat_runtime as chat
import static_analysis as analysis
import static_runtime as runtime
import review_context as context
import review_report as report

SYSTEM=chat.SYSTEM+' You are a child reviewer. Follow the parent goal and the assigned branch objective. Report only this branch; other branches may be pending. Echo branch_id. Memory contains user statements and unverified commentary, not permissions.'
SLICE_OUTPUT_TOKENS = 2048
REPAIR_ALLOWANCE = {'errors': ['x' * 140] * 2}


def schema(p):
    result=chat.schema(p)
    result['properties']['branch_id']={'type':'string','enum':[p['branch']['id']]}
    result['required'].append('branch_id')
    if p.get('child_slice'):
        result['properties']['slice_status'] = {'type': 'string', 'enum': ['continue', 'complete']}
        if 'report' in result['required']:
            result['required'].remove('report')
    return result


def checkpoint_context(s, nid, cursor):
    return [dict(invocation_id=item['invocation_id'], model=item['model'],
                 **{key: item['output'][key] for key in ('summary', 'remaining', 'citations')})
            for item in machine.slice_history(s, nid, cursor)]


def slice_excerpts(p, data):
    sources = data['sources']
    result = chat.excerpts(sources, context.query(p), p['excerpt_bytes'], data['graph'])
    used = {}
    for note in p.get('child_checkpoint', ()):
        for citation in note['citations']:
            used.setdefault(citation['path'], set()).add(citation['quote'])
    for path, previous in result.items():
        if path not in used:
            continue
        lines = sources[path].splitlines(keepends=True)
        start = max((index + 1 for index, line in enumerate(lines)
                     if any(line.startswith(quote) for quote in used[path])), default=0)
        if start and start < len(lines):
            excerpt = ''.join(lines[start:]).encode()[:len(previous.encode())].decode('utf-8', 'ignore')
            if excerpt.strip():
                result[path] = excerpt
    return result


def memory(s,p,budget,materialize=False,mandatory_only=False):
    iid=machine.node(s,p['branch']['id'])['context'].get('conversation_id')
    if not iid:return None
    import conversation_memory as cm
    row=s.db.execute('SELECT session FROM invocations WHERE id=?',(iid,)).fetchone()
    if not row:raise InvalidUpdate('Missing parent conversation')
    if materialize:cm.materialize(s,row[0],cm.history(s,row[0],iid))
    base={k:v for k,v in p.items() if k not in ('memory','repair')}
    return cm.working_set(s,row[0],iid,p['request'],budget,base,SYSTEM,schema,extra_bytes=0 if mandatory_only else budget.context_limit//16)

def _context_budget(s, sid, provider, sliced, slots):
    allocation = context.context_budget(provider, s, sid, slots=slots,
                                        expand_output=slots > 1 or sliced)
    if sliced:
        budget = provider.budget
        provider.budget = Budget(budget.context_limit, min(budget.response_reserve, SLICE_OUTPUT_TOKENS),
                                 budget.template_reserve)
    return allocation


def _fit(s, packet, budget, mandatory_only=False):
    packet['memory_budget'] = vars(budget).copy()
    packet['memory'] = memory(s, packet, budget, materialize=True, mandatory_only=mandatory_only)
    fmt = schema(packet)
    charge = budget.check(SYSTEM, dict(packet, repair=REPAIR_ALLOWANCE), fmt)
    return fmt, charge + budget.response_reserve


def _context_failure(s, packet, provider, allocation, cause, stage):
    """Measure any shortfall with the same minimum packet and output allowance."""
    from model_context import allocation as model_allocation

    failure = context.ReviewContextExceeded(allocation, provider.budget, cause)
    failure.details.update(branch_id=packet['branch']['id'], fit_stage=stage)
    installed, _ = model_allocation(provider.context_metadata)
    if installed.context_limit > provider.budget.context_limit:
        model_budget = Budget(installed.context_limit, provider.budget.response_reserve,
                              installed.template_reserve)
        try:
            _, required = _fit(s, dict(packet), model_budget, mandatory_only=True)
        except ContextBudgetExceeded:
            return failure
        failure.details['required_context'] = required
        extra = required - allocation['available_tokens']
        if extra > 0:
            failure.details['task_allocation_need'] = {
                'dimension': 'tokens', 'minimum': extra, 'required_context': required,
                'model_context': installed.context_limit, 'slots': 1,
            }
    return failure


def _preflight(s, sid, packet, provider, real):
    """Require enough room for context before acquiring source receipts."""
    choices = (2, 1) if real else (None,)
    for slots in choices:
        allocation = _context_budget(s, sid, provider, 'child_slice' in packet, slots) if real else None
        probe = dict(packet, excerpts={})
        try:
            _fit(s, probe, provider.budget, mandatory_only=True)
            return slots
        except ContextBudgetExceeded as exc:
            if allocation is None:
                raise
            if slots == 1:
                raise _context_failure(s, probe, provider, allocation, exc, 'preflight') from exc


def _fit_packet(s, sid, packet, data, provider, preferred_slots):
    """Shrink source pages first, then release the reserved repair share."""
    choices = range(preferred_slots, 0, -1) if preferred_slots else (None,)
    for slots in choices:
        allocation = _context_budget(s, sid, provider, 'child_slice' in packet, slots) if slots else None
        packet['excerpt_bytes'] = max(64, min(65536, provider.budget.context_limit // 3))
        packet['map_bytes'] = max(512, min(65536, provider.budget.context_limit // 3))
        while True:
            packet['excerpts'] = slice_excerpts(packet, data)
            packet['source_paths'] = sorted(packet['excerpts'])
            context.evidence(packet, data)
            packet['facts'] = chat.facts(data['graph'], packet['sites'])
            try:
                fmt, _ = _fit(s, packet, provider.budget)
                return fmt, 1 if slots == 1 else 3
            except ContextBudgetExceeded as exc:
                if packet['excerpt_bytes'] == 64 and packet['map_bytes'] == 512:
                    if allocation is None:
                        raise
                    if slots == 1:
                        raise _context_failure(s, packet, provider, allocation, exc, 'packet') from exc
                    break
                packet['excerpt_bytes'] = max(64, packet['excerpt_bytes'] * 4 // 5)
                packet['map_bytes'] = max(512, packet['map_bytes'] * 4 // 5)


def request_check(s,sid,p):
    from review_reasoning import check
    check(p)
    from conversation import check_workspace_handoff
    n=machine.node(s,p['branch']['id'])
    if machine.packet(n)!=p['branch'] or n['kind']!='branch' or n['session']!=sid or n['binding']!=machine.binding(s,sid):raise InvalidUpdate('Child lineage or contract changed')
    if machine.state(s,n['id']) in ('CANCELLED','QUARANTINED'):raise InvalidUpdate('Child was revoked')
    if n['context_hash']!=sha(canonical(n['context'])) or p['request']!=n['context']['request']:raise InvalidUpdate('Parent context changed')
    if p['contract']!=s.validation_contract(sid) or p['constraint_echo']!=constraint_rows(p['contract']):raise InvalidUpdate('Child constraints changed')
    if p['source_paths']!=sorted(p['excerpts']):raise InvalidUpdate('Child source scope changed')
    machine.scope_check(n,'model',p['source_paths'],'subagent')
    check_workspace_handoff(s,sid,p)
    if n['context'].get('conversation_id')!=(p.get('conversation_handoff') or {}).get('invocation_id'):raise InvalidUpdate('Child lost parent conversation lineage')
    if n.get('slice_protocol') == 1:
        cursor = p.get('child_slice')
        if p.get('child_checkpoint') != checkpoint_context(s, n['id'], cursor):
            raise InvalidUpdate('Child continuation differs from its admitted checkpoint')
    elif 'child_slice' in p or 'child_checkpoint' in p:
        raise InvalidUpdate('Legacy child has no slice protocol')
    data=runtime.load_partition(s,sid,p['workspace'],p['snapshot_receipts'],n['id'])
    if set(data['sources'])!=set(n['files']):raise InvalidUpdate('Child snapshot exceeds assigned files')
    for path,digest in data['versions'].items():
        if n['context']['versions'].get(path)!=digest:raise InvalidUpdate('Source changed since branch planning; replan required')
    if type(p['excerpt_bytes']) is not int or not 64<=p['excerpt_bytes']<=65536:raise InvalidUpdate('Invalid child excerpt budget')
    if p['excerpts']!=slice_excerpts(p,data) or p['facts']!=chat.facts(data['graph'],p['sites']):raise InvalidUpdate('Child evidence differs from fresh source')
    context.check(p,data)
    b=Budget(**p['memory_budget'])
    if p.get('memory')!=memory(s,p,b):raise InvalidUpdate('Child Smriti context changed')
    choice=KarmaAdapter().route(p['request'],p['route']['models'],p['route']['mode'],p['route']['signals'])
    if p['route']!=choice:raise InvalidUpdate('Child model route changed')
    return data

def validate(s,sid,p,proposal):
    request_check(s,sid,p)
    if proposal['branch_id']!=p['branch']['id'] or proposal['task_id']!=p['contract']['task_id'] or sorted(proposal['constraints'],key=lambda x:x['key'])!=constraint_rows(p['contract']):raise InvalidUpdate('Child omitted task or constraints')
    for c in proposal['citations']:
        if c['quote'] not in chat.citation_quotes(p) or c['quote'] not in p['excerpts'].get(c['path'],''):raise InvalidUpdate('Child citation lacks fresh source')
    if '```mermaid' in proposal['answer'].lower():raise InvalidUpdate('Child cannot invent a diagram')
    if proposal.get('slice_status') == 'continue':
        prior = {(c['path'], c['quote']) for note in p['child_checkpoint'] for c in note['citations']}
        current = {(c['path'], c['quote']) for c in proposal['citations']}
        if not any(item.strip() for item in proposal['remaining']) or not current - prior:
            raise InvalidUpdate('A continuation needs a specific next step and at least one new source quotation')
    elif report.requested(p) and 'report' not in proposal:
        raise InvalidUpdate('A completed architecture slice needs its branch report')
    if 'report' in proposal:
        report.validate(p, proposal)
    return {'admission':{'admitted':True,'errors':[],'semantic_correctness_verified':False},'checks':['child_lineage','full_contract','inherited_permissions','fresh_source','smriti_context','citations'],'provisional':True}

def _advance_slice(s, nid, out, route, cursor, recovered=False):
    result = {'result': out, 'route': route, 'branch_id': nid}
    if recovered:
        result['recovered'] = True
    if out['admission']['admitted']:
        event = machine.complete(s, nid, out['invocation_id'])
        if event['kind'] == 'CHILD_SLICE_SAVED':
            result.update(slice_status='checkpoint', slice_ordinal=cursor['ordinal'])
    else:
        machine.timeout_child(s, nid, out['invocation_id'])
        result.update(slice_status='timeout', slice_ordinal=cursor['ordinal'], slice_reason=out['failure_kind'])
    return result


def run(s,meta,workspace,root,nid,models,tier='auto',provider=None):
    n=machine.checked(s,nid);sid=n['session'];real=provider is None
    cursor = machine.slice_cursor(s, nid) if n.get('slice_protocol') == 1 else None
    recovered = machine.recover_child(s, nid)
    if recovered:
        from token_usage import history
        invocation = recovered['invocation_id']
        run_id, = s.db.execute('SELECT run_id FROM invocations WHERE id=?', (invocation,)).fetchone()
        failure = recovered['failure_kind']
        out = {'invocation_id': invocation, 'admission': {'admitted': failure is None, 'errors': []},
               'accepted_output': recovered['output'], 'token_usage': history(s.db, run_id=run_id)}
        if failure:
            out['failure_kind'] = failure
        return _advance_slice(s, nid, out, {'model': recovered['model']}, cursor, recovered=True)
    with machine.scope(nid):
        request = {'prompt': n['context']['request'],
                   'conversation_id': n['context'].get('conversation_id'),
                   'orchestration_parent': n['parent_id']}
        signals = {'source_files': len(n['context']['versions']),
                   'selected_files': len(n['files']), 'unresolved_calls': 0}
        adapter = KarmaAdapter()
        route = adapter.route(request['prompt'], models, tier, signals)
        if real:
            from api_runtime import provider_for
            provider = provider_for(root, route['model'], review=True)
            provider.keep_alive = 0
        if provider.model != route['model']:
            raise InvalidUpdate('Child provider differs from routed model')
        p = {'contract': s.pinned(sid), 'branch': machine.packet(n), 'workspace': str(workspace),
             'request': request['prompt'], 'pages': []}
        if cursor:
            p.update(child_slice=cursor, child_checkpoint=checkpoint_context(s, nid, cursor))
        p['constraint_echo'] = constraint_rows(p['contract'])
        from review_reasoning import bind, repair
        bind(provider, p)
        if request['conversation_id']:
            from conversation import workspace_handoff
            p['conversation_handoff'] = workspace_handoff(s, sid, request['conversation_id'], p['request'])
        preferred_slots = _preflight(s, sid, p, provider, real)
        from review_source_receipts import acquire
        receipts, data, _ = acquire(s, meta, workspace, root, request, n['files'], real=real, purpose='child_review')
        graph = data['graph']
        signals['unresolved_calls'] = sum(c['target'] is None for c in graph['calls'])
        route = adapter.route(request['prompt'], models, tier, signals)
        if provider.model != route['model']:
            raise InvalidUpdate('Child route changed after source preparation')
        p.update(snapshot_receipts=receipts, route=route)
        fmt, attempts = _fit_packet(s, sid, p, data, provider, preferred_slots)
        rid=s.begin_run(sid,p['request'],[],None,max_calls=attempts,max_repairs=attempts-1)
        output_repaired = False
        for attempt in range(attempts):
            s.reserve(rid,'ATTEMPT',{'attempt':attempt})
            if real:
                from monitoring import require_resources
                h=require_resources(root)
                with s.db:s._audit('RESOURCE_GATE',{'run_id':rid,'measurement':h,'orchestration_node':nid})
            d=s.prepare_delivery(sid,p,provider.budget.check(SYSTEM,p,fmt))
            out=InvocationGateway(s.path, rid, provider, ledger=s).invoke('subagent',uuid.uuid4().hex,SYSTEM,p,fmt,d)
            out.pop('record',None)
            out.pop('model_proposal',None)
            if out['admission']['admitted']:
                s.event(rid,'ALLOW',{'invocation_id':out['invocation_id']})
                return _advance_slice(s, nid, out, route, cursor)
            s.event(rid,'NACK',{'errors':out['admission']['errors']})
            if cursor and out.get('failure_kind') == 'timeout':
                s.event(rid,'FIN',{'reason':'child_slice_timeout','invocation_id':out['invocation_id']})
                return _advance_slice(s, nid, out, route, cursor)
            has_retry = attempt + 1 < attempts
            recovery = repair(out, p, output_repaired, has_retry)
            if not has_retry or (out.get('blocked') and recovery is None):
                if cursor and out.get('failure_kind') == 'output_limit':
                    s.event(rid,'FIN',{'reason':'child_slice_output_limit','invocation_id':out['invocation_id']})
                    return _advance_slice(s, nid, out, route, cursor)
                s.event(rid,'FIN',{'reason':'child_repair_exhausted'})
                with s.db:machine.transition(s,nid,'QUARANTINED','CHILD_REJECTED',{'invocation_id':out['invocation_id']})
                break
            request_check(s,sid,p)
            s.event(rid,'BRIDGE',{'reason':'bounded_output_repair' if recovery else 'bounded_child_repair'})
            p['repair'] = recovery or {'errors':[e[:140] for e in out['admission']['errors'][:2]]}
            output_repaired = output_repaired or recovery is not None
        return {'result':out,'route':route,'branch_id':nid}


def joined_results(s,sid,parent,ids,prompt):
    n=machine.node(s,parent)
    if n['binding']!=machine.binding(s,sid) or machine.state(s,parent) in ('CANCELLED','QUARANTINED'):
        raise InvalidUpdate('Child result parent binding changed or was revoked')
    if n['session']!=sid:raise InvalidUpdate('Cross-task child join')
    joins=[e for e in machine.events(s,parent) if e['kind']=='JOIN']
    if not joins or joins[-1]['data']['invocations']!=ids:raise InvalidUpdate('Uncommitted child join')
    result=[]
    for iid in ids:
        output=s.load_result(sid,iid)
        env=json.loads(s.db.execute('SELECT record_json FROM invocations WHERE id=?',(iid,)).fetchone()[0])
        p=json.loads(s.db.execute('SELECT payload_json FROM deliveries WHERE id=?',(env['delivery_id'],)).fetchone()[0])
        if env['role']!='subagent' or p['branch']['parent_id']!=parent or p['request']!=prompt or machine.state(s,p['branch']['id'])!='DONE':raise InvalidUpdate('Child result belongs to another parent or request')
        notes = p.get('child_checkpoint', [])
        summaries = [note['summary'] for note in notes] + [output['summary']]
        citations = {(c['path'], c['quote']): c for note in [*notes, output] for c in note['citations']}
        result.append({'invocation_id':iid,'model':env['model'],'branch_id':p['branch']['id'],'files':p['source_paths'],
                       'answer_unverified':report.markdown(output),'evidence_coverage':p['evidence_coverage'],
                       'summary_unverified':'\n'.join(summaries),'remaining_unverified':output['remaining'],'citations':list(citations.values())})
    return result
