"""Bounded host fallback for capacity stops before workspace inference.

Only the central workflow calls this adapter. Candidate selection is not an
admission: the next step must page context and pass all ordinary dispatch gates.
"""
from context_manager import InvalidUpdate
from karma_adapter import KarmaAdapter
from model_routing import TIERS
import orchestration as machine


def effective(data, models, requested):
    saved=data.get('resource_fallback') or {}
    if requested=='auto' and saved.get('models')==models:
        return 'resource-'+saved['tier']
    return requested


def propose(s, fid, data, request, state, phase):
    if request.get('tier','auto')!='auto' or not request.get('armed'):
        return None
    runtime=state.get('runtime') or {}
    if (phase not in ('CHILDREN','SYNTHESIS','CODING') or runtime.get('kind')!='model_capacity'
            or runtime.get('inference_started') is not False or runtime.get('stage') not in ('configure','dispatch')):
        return None
    models=request.get('models') or {};plan=runtime.get('plan') or {};failed=plan.get('model')
    if set(models)!=set(TIERS) or failed not in models.values():return None
    history=[e for e in machine.events(s,fid) if e['kind']=='RESOURCE_MODEL_FALLBACK']
    bound=KarmaAdapter().counted_slots(len(history),len(TIERS)-1)
    if bound['decision']!='ALLOW':return None
    owner=data.get('root_node')
    if phase=='CHILDREN':
        owner=next((i for i in data.get('children',[]) if machine.state(s,i)!='DONE'),None)
    if not owner:return None
    # Check remaining task/child allocations without reserving or refunding work.
    frames=machine.gates(s,owner,{'calls':1})
    if any(f['decision']!='ALLOW' for f in frames):return None
    from runtime_memory import api, positive
    from model_identity import weights
    try:
        tags=api('/api/tags').get('models',[])
        if not isinstance(tags,list):return None
        by_name={t['name']:t for t in tags if isinstance(t,dict) and isinstance(t.get('name'),str)}
        old=by_name.get(failed,{})
        if not positive(old.get('size')) or not old.get('digest'):return None
        old_weights=weights(old['digest'])
        attempted={failed}|{e['data']['resource_fallback']['model'] for e in history}
        ceiling=min(TIERS.index(t) for t in TIERS if models[t]==failed)
        for tier in reversed(TIERS[:ceiling]):
            name=models[tier];tag=by_name.get(name,{})
            if (name in attempted or 'cloud' in name.lower() or not positive(tag.get('size'))
                    or tag['size']>=old['size'] or not tag.get('digest') or tag['digest']==old['digest']):continue
            candidate_weights=weights(tag['digest'])
            if old_weights and candidate_weights==old_weights:continue
            return {'tier':tier,'model':name,'models':dict(models),'from_model':failed,'phase':phase,
                    'owner':owner,'reason':'preferred model capacity refused before inference',
                    'failed_plan':plan,'candidate_size_bytes':tag['size'],'candidate_manifest':tag['digest'],
                    'bounded_switch':bound,'budget_frames':frames,'inference_started':False}
    except (OSError,InvalidUpdate,ValueError,KeyError,TypeError):
        # Inventory failures never turn an existing resource stop into dispatch.
        return None
    return None
