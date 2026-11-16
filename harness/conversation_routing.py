"""Receipt-bound conversation routing. Model assessments are proposals, not proof.

Context requests precede escalation. Every resulting call still passes the
invocation gateway and the same session's kernel-governed allocations.
"""
import json
from context_manager import InvalidUpdate
from model_routing import choose,TIERS

SIGNALS={'source_files':0,'selected_files':0,'unresolved_calls':0}

class RoutingBlocked(InvalidUpdate):
    pass


def next_tier(route,tried):
    for tier in TIERS[TIERS.index(route['tier'])+1:]:
        if route['models'][tier] not in tried:
            return tier
    return None


def followup(proposal,p):
    state=p['routing_state']
    requested=proposal.get('page_requests',[])
    if requested:
        if not set(requested)-set(state['requested_pages']):
            return {'kind':'blocked','reason':'Context paging made no progress. No draft was published.'}
        return {'kind':'page_in','reason':'Load requested receipt-backed context before considering a larger model.'}
    if proposal['intent']=='workspace':
        # The first stage has no source. Fresh scoped reads must precede the
        # workspace adapter's model decision, regardless of the model's opinion.
        return None
    reason=proposal.get('escalation_reason','none')
    if reason=='missing_context':
        return None  # Ask the user for the missing evidence; a bigger model cannot supply it.
    if reason!='reasoning_capacity':
        return None
    from reasoning_policy import next_control
    control=next_control(p.get('reasoning'))
    if control is not None:
        return {'kind':'reasoning','control':control,
                'reason':'The model requested more reasoning. Check a supported higher effort on this model against the remaining allocation.'}
    if p['route']['mode']!='auto':
        return {'kind':'blocked','reason':'The selected model requested escalation. Choose Auto or another tier to continue; explicit model selections stay fixed.'}
    tier=next_tier(p['route'],state['models_tried'])
    if tier is None:
        return {'kind':'blocked','reason':'No untried higher-tier model is configured. No draft was published; context and consumed budget are retained.'}
    return {'kind':'escalate','tier':tier,'reason':'The model requested stronger reasoning after available context selection. This assessment is unverified.'}


def prepare(s,sid,request):
    route=choose(request['prompt'],request['models'],request.get('tier','auto'),SIGNALS)
    previous=request.get('routing_continuation')
    state={'previous':previous,'steps':0,'page_passes':0,'models_tried':[],
           'requested_pages':[],'parent':request.get('parent')}
    if previous:
        row=s.db.execute('SELECT record_json,delivery_id FROM invocations WHERE id=? AND session=?',(previous,sid)).fetchone()
        if not row or json.loads(row[0])['role']!='conversation':
            raise InvalidUpdate('Routing continuation needs a conversation receipt in this session')
        proposal=s.load_result(sid,previous)
        prior=json.loads(s.db.execute('SELECT payload_json FROM deliveries WHERE id=?',(row[1],)).fetchone()[0])
        old=prior.get('routing_state')
        if not old or prior['request']!=request['prompt'] or old['parent']!=request.get('parent'):
            raise InvalidUpdate('Routing continuation changed the original request or conversation parent')
        step=followup(proposal,prior)
        if not step:
            raise InvalidUpdate('A completed answer cannot authorize another routing call')
        state.update(steps=old['steps']+1,page_passes=old['page_passes'],
                     models_tried=list(old['models_tried']),requested_pages=list(old['requested_pages']))
        requested=proposal.get('page_requests',[])
        if requested:
            if not set(requested)-set(state['requested_pages']):
                raise RoutingBlocked('Context paging made no progress; continuation cannot reset it')
            state['requested_pages']=list(dict.fromkeys(state['requested_pages']+requested))
            state['page_passes']+=1
        if route['mode']=='auto':
            old_route=dict(prior['route'],models=dict(route['models']))
            same_model=bool(requested) or step['kind']=='reasoning'
            tier=old_route['tier'] if same_model else next_tier(old_route,state['models_tried'])
            if tier is None:
                raise RoutingBlocked('No untried higher-tier model is configured')
            route.update(tier=tier,model=route['models'][tier],
                         reason=('requested context paged before escalation' if requested else
                                 'supported reasoning retry before model escalation' if same_model else
                                 'receipt-backed model escalation'))
            if same_model and route['model']!=prior['route']['model']:
                raise RoutingBlocked('The saved routing tier now names a different model. Restore it or explicitly select a tier; budget is retained.')
        else:
            route['reason']='explicit user override during saved routing'
        route.update(evidence_invocation=previous,assessment_authority='model_unverified')
    if route['model'] not in state['models_tried']:
        state['models_tried'].append(route['model'])
    return route,state


def check(s,sid,p):
    state=p['routing_state']
    if (p.get('memory') or {}).get('invocation_id')!=state['parent']:
        raise InvalidUpdate('Routing lost its original conversation parent')
    route,expected=prepare(s,sid,{'prompt':p['request'],'parent':state['parent'],
        'models':p['route']['models'],'tier':p['route']['mode'],'routing_continuation':state['previous']})
    if p['route']!=route or state!=expected or p.get('requested_memory_pages',[])!=state['requested_pages']:
        raise InvalidUpdate('Routing decision or requested context differs from its receipts')
    if state['previous']:
        row=s.db.execute('SELECT d.payload_json FROM invocations i JOIN deliveries d ON d.id=i.delivery_id WHERE i.id=?',(state['previous'],)).fetchone()
        prior=json.loads(row[0])
        if any(p[k]!=prior[k] for k in ('contract','workspace_context','context_binding')):
            raise InvalidUpdate('Contract or workspace permissions changed during routing')
