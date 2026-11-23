"""Bounded host fallback for capacity stops before workspace inference.

Only the central workflow calls this adapter. Candidate selection is not an
admission: the next step must page context and pass all ordinary dispatch gates.
"""
import copy
import re

from context_manager import InvalidUpdate
from karma_adapter import KarmaAdapter
from model_routing import TIERS
import orchestration as machine


def remote_capacity_plan(state, *, historical=False):
    """Verify a retained pre-inference capacity refusal, without remote calls.

    Historical verification identifies a saved stop at its observation time; it
    never proves present capacity or permits dispatch. Normal fallback requires
    an observation that is still fresh and a currently assigned descriptor.
    """
    from remote_profiles import is_remote, load, require_assignment, identity
    from remote_memory import evaluate
    try:
        runtime = state.get('runtime') or {}
        plan = runtime.get('plan') or {}
        name = plan.get('model')
        if (runtime.get('kind') != 'model_capacity' or
                runtime.get('stage') not in ('remote_admission', 'remote_dispatch') or
                runtime.get('inference_started') is not False or
                runtime.get('model') != name or not is_remote(name) or
                state.get('available') is not True or state.get('admitted') is not False or
                state.get('provider') != 'remote_llama' or state.get('blocker') != 'model_admission'):
            return None
        profile = load(name)
        require_assignment(profile)
        sample = plan['hardware']['sample']
        verified = evaluate(profile, sample, plan['context_tokens'],
                            now=sample['wall_time'] if historical else None)
        hardware = {key: value for key, value in state.items()
                    if key not in ('runtime', 'runtime_recovery')}
        if (verified.get('available') is not True or verified.get('admitted') is not False or
                verified.get('hardware', {}).get('blocker') != 'model_admission' or
                any(plan.get(key) != value for key, value in verified.items()) or
                hardware != verified['hardware']):
            return None
        return {'profile': copy.deepcopy(profile), 'plan': copy.deepcopy(verified),
                'identity': identity(name, profile), 'model_bytes': sample['model']['bytes']}
    except (OSError, InvalidUpdate, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return None


def _local_candidate(row):
    """Use installed identities or approved descriptors, never activate a model.

    A bound native cold-memory estimate is a conservative size ordering, not an
    admission. Strata currently exposes no bound size, so those inventory rows
    cannot establish that a switch reduces the requested footprint.
    """
    from runtime_memory import positive
    from model_identity import weights
    from engine_profiles import is_engine
    from remote_profiles import is_remote
    from strata_model import is_strata
    name = row.get('name')
    if (not isinstance(name, str) or row.get('eligible') is not True or
            not positive(row.get('context_tokens')) or row['context_tokens'] < 4096 or
            is_remote(name) or is_strata(name) or 'cloud' in name.lower()):
        return None
    if is_engine(name):
        import engine_profiles
        if name not in engine_profiles.assigned_models():
            return None
        profile = engine_profiles.load(name)
        engine_profiles.require_approval(profile)
        size = profile.get('cold_memory_bytes')
        if (not positive(size) or row.get('provider') != 'sutra_engine' or
                row.get('profile_hash') != profile['profile_hash'] or
                row['context_tokens'] != profile['context_tokens']):
            return None
        return {'candidate_size_bytes': size, 'candidate_manifest': profile['profile_hash'],
                'candidate_size_basis': 'approved_engine_cold_memory_estimate',
                'candidate_identity': engine_profiles.identity(name, profile),
                'candidate_context_tokens': row['context_tokens']}
    # Reserved provider namespaces cannot fall through to an Ollama identity.
    if name.startswith(('sutra:', 'strata:')):
        return None
    digest = row.get('digest')
    if (row.get('provider', 'ollama') != 'ollama' or not positive(row.get('size')) or
            not isinstance(digest, str) or re.fullmatch(r'(?:sha256:)?[a-f0-9]{64}', digest) is None):
        return None
    return {'candidate_size_bytes': row['size'], 'candidate_manifest': digest,
            'candidate_size_basis': 'installed_ollama_model_bytes',
            'candidate_identity': {'name': name, 'manifest_digest': digest,
                                   'weights_sha256': weights(digest),
                                   'basis': 'installed_ollama_manifest', 'server_attested': False},
            'candidate_context_tokens': row['context_tokens']}


def _remote_candidate(models, failed, history, proof):
    from model_catalog import combined
    from runtime_memory import api
    rows = combined(api).get('models')
    if not isinstance(rows, list):
        return None
    by_name = {row['name']: row for row in rows if isinstance(row, dict) and isinstance(row.get('name'), str)}
    attempted = {failed} | {event['data']['resource_fallback']['model'] for event in history}
    ceiling = min(TIERS.index(tier) for tier in TIERS if models[tier] == failed)
    for tier in reversed(TIERS[:ceiling]):
        name = models[tier]
        if name in attempted:
            continue
        try:
            candidate = _local_candidate(by_name.get(name, {}))
        except (OSError, InvalidUpdate, ValueError, KeyError, TypeError):
            # A changed/unapproved candidate does not authorize it or hide a
            # separately configured, verifiable lower tier.
            continue
        if not candidate or candidate['candidate_size_bytes'] >= proof['model_bytes']:
            continue
        candidate_weights = candidate['candidate_identity'].get('weights_sha256')
        if candidate_weights and candidate_weights == proof['identity'].get('weights_sha256'):
            continue
        return dict(candidate, tier=tier, model=name,
                    failed_identity=proof['identity'], failed_size_bytes=proof['model_bytes'],
                    failed_size_basis='verified_remote_GGUF_bytes')
    return None


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
            or runtime.get('inference_started') is not False or
            runtime.get('stage') not in ('configure','dispatch','remote_admission','remote_dispatch')):
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
        if runtime['stage'] in ('remote_admission', 'remote_dispatch'):
            proof = remote_capacity_plan(state)
            if proof is None:
                return None
            candidate = _remote_candidate(models, failed, history, proof)
            if candidate is None:
                return None
            return dict(candidate, models=dict(models), from_model=failed, phase=phase,
                        owner=owner, reason='verified remote capacity refused before inference',
                        failed_plan=copy.deepcopy(plan), bounded_switch=bound,
                        budget_frames=frames, inference_started=False)
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
