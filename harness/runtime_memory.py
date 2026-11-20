"""Host-owned runtime allocation and bounded recovery; no task-memory mutation.

The shared-server default never sends an explicit unload. Exclusive ownership
is a user setting, not something /api/ps can establish. Estimates are planning
signals, not a proof of backend peak RAM or semantic sufficiency.
"""
import contextlib
from datetime import datetime
import http.client
import json
import math
import os
from pathlib import Path
import time

from context_manager import InvalidUpdate, canonical
from karma_adapter import KarmaAdapter
from platform_support import lock_file

PROFILE = 'runtime-memory-v1'
GIB = 1024 ** 3
HEADROOM = 4 * GIB
MAX_RECOVERY_SECONDS = 3


def enabled():
    return bool(os.environ.get('SUTRA_RUNTIME_DIR'))


def ownership():
    # Unknown settings never authorize unloading on a shared server.
    return 'exclusive' if os.environ.get('SUTRA_MEMORY_OWNERSHIP') == 'exclusive' else 'shared'


def directory():
    path = Path(os.environ['SUTRA_RUNTIME_DIR']).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def api(path, body=None):
    from lifecycle import timeout
    conn = http.client.HTTPConnection('127.0.0.1', 11434, timeout=timeout(2))
    try:
        conn.request('POST' if body is not None else 'GET', path,
                     None if body is None else canonical(body), {'Content-Type':'application/json'})
        response = conn.getresponse()
        raw = response.read(1024 * 1024 + 1)
        if response.status != 200 or len(raw) > 1024 * 1024:
            raise InvalidUpdate('Ollama memory inventory is unavailable')
        return json.loads(raw)
    except (OSError, ValueError, http.client.HTTPException, InvalidUpdate) as exc:
        denied('Ollama memory inventory or release is unavailable: '+str(exc)[:160])
    finally:
        conn.close()


def running():
    response=api('/api/ps')
    result = response.get('models') if isinstance(response,dict) else None
    if not isinstance(result, list) or len(result) > 100:
        denied('Invalid Ollama memory inventory')
    rows = []
    for r in result:
        if not isinstance(r, dict) or not isinstance(r.get('name'), str) or not isinstance(r.get('digest'), str):
            denied('Model memory identity unavailable')
        row={k:r.get(k) for k in ('name','digest','size','size_vram','context_length','runner','expires_at')}
        row['runner']=row['runner'] or (r.get('details') or {}).get('runner')
        rows.append(row)
    return rows


def read_state():
    try:
        p = directory() / 'state.json'
        if p.stat().st_size > 1024 * 1024:
            return {}
        value=json.loads(p.read_text())
        return value if isinstance(value,dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(data):
    data = dict(data, profile=PROFILE, ownership=ownership(), updated_at=time.time())
    p = directory() / 'state.json'
    temp = p.with_suffix('.tmp')
    temp.write_text(canonical(data)); temp.replace(p)


def note(data, event, **fields):
    data.setdefault('events', []).append(dict(event=event, at=time.time(), **fields))
    data['events'] = data['events'][-24:]
    write_state(data)


def denied(reason, **extra):
    from monitoring import ResourceWait
    state = extra.get('plan') or KarmaAdapter().frame(PROFILE, 2 * KarmaAdapter().boundary.k, 0.)
    raise ResourceWait(dict(state, available=False, admitted=False, reason=reason, runtime=extra))


@contextlib.contextmanager
def lease():
    manager = lock_file(directory() / 'ollama-11434.lock')
    try:
        manager.__enter__()
    except InvalidUpdate:
        denied('Another Sutra call or recovery owns the local model slot; resume after it finishes.', kind='busy')
    try:
        yield
    finally:
        manager.__exit__(None, None, None)


def positive(value):
    return type(value) is int and value > 0


def kv_bytes_per_token(metadata):
    """Conventional attention estimate only; hybrid/MLX layouts remain unknown."""
    info = metadata.get('model_info') or {}
    arch = info.get('general.architecture', '')
    # Hybrid/recurrent architectures and non-GGUF backends need measured profiles.
    if (metadata.get('details') or {}).get('format') != 'gguf' or arch not in ('llama','qwen2','qwen3','gemma','gemma2'):
        return None
    values = [info.get(arch + suffix) for suffix in
              ('.block_count','.attention.head_count_kv','.embedding_length','.attention.head_count')]
    if not all(positive(v) for v in values):
        return None
    layers, kv_heads, embedding, heads = values
    if embedding % heads:
        return None
    key = info.get(arch+'.attention.key_length', embedding // heads)
    value = info.get(arch+'.attention.value_length', embedding // heads)
    if not positive(key) or not positive(value):
        return None
    # Two bytes per K/V element; backend workspace and concurrency are separate.
    return layers * kv_heads * (key + value) * 2


def resident_credit(model, metadata, weights, models, context_tokens, digest, now=None):
    """Estimate reusable weights, never credit KV, workspace, or reclaimable RAM.

    Inventory is read under the dispatch lease again immediately before use.
    Matching an alias by name alone is insufficient. This remains a planning
    estimate: external clients can change a shared server after observation.
    """
    now=time.time() if now is None else now
    rows=[r for r in models if r.get('name')==model]
    if len(rows)!=1:return 0,'No unique resident model'
    row=rows[0];backend=(metadata.get('details') or {}).get('runner')
    if not digest or row.get('digest')!=digest:return 0,'Resident manifest does not match installed model'
    if not backend or row.get('runner')!=backend:return 0,'Resident backend is unverified'
    try:
        expiry=datetime.fromisoformat(row['expires_at'].replace('Z','+00:00'))
        if expiry.tzinfo is None or expiry.timestamp()<=now:return 0,'Resident inventory entry expired'
    except (KeyError,TypeError,ValueError,AttributeError,OverflowError):return 0,'Resident expiry is unavailable'
    if not positive(row.get('context_length')) or row['context_length']<context_tokens:
        return 0,'Requested context exceeds resident context; plan a cold allocation'
    bounds=[weights,row.get('size')]
    if backend=='mlx':bounds.append(row.get('size_vram'))
    if not all(positive(n) for n in bounds):return 0,'Resident size is unavailable'
    return min(bounds),'Exact resident name, manifest, backend and sufficient context; weights only'


def estimate(model, metadata, weights, models, context_tokens, hardware, calibration=None, manifest_digest=None):
    current = next((r for r in models if r['name'] == model), None)
    kv = kv_bytes_per_token(metadata)
    cold = weights + (kv * context_tokens if kv else 0)
    calibrated=None
    if calibration:
        from memory_profiles import estimate as calibrated_estimate
        calibrated=calibrated_estimate(calibration,context_tokens,weights)
        if calibrated is None:
            denied('Requested context exceeds measured MLX calibration coverage; re-page or recalibrate.')
        cold=calibrated['bytes']
        manifest_digest=manifest_digest or calibration['identity']['manifest_digest']
    credit,basis=resident_credit(model,metadata,weights,models,context_tokens,manifest_digest)
    incremental=max(0,cold-credit)
    sample = hardware.get('sample') or {}
    available = sample.get('available_bytes') if sample.get('profile') == 'portable-memory-v1' else sample.get('free_bytes', 0) + sample.get('inactive_bytes', 0)
    frame = KarmaAdapter().memory_allocation(incremental, available, HEADROOM)
    return dict(frame, model=model, context_tokens=context_tokens, estimated_increment_bytes=incremental,
                model_file_bytes=weights, kv_bytes_per_token=kv, resident=bool(current),
                cold_estimate_bytes=cold,resident_weight_credit_bytes=credit,resident_credit_basis=basis,
                calibration=calibrated,
                estimate_scope=(('measured MLX footprint envelope plus full margin' if calibrated else
                                'weights and conventional KV' if kv else 'weights only; KV/backend workspace unknown')+
                                ('; matched resident weights deducted; KV/workspace still reserved' if credit else '; cold allocation, no resident credit')),
                peak_memory_verified=False, source='local metadata and fresh Ollama inventory')


def profile_for(provider, metadata, tag=None):
    if getattr(provider,'memory_probe',False):return None,'Calibration probe; existing estimates not used'
    if (metadata.get('details')or{}).get('runner')!='mlx':return None,'Not an MLX backend'
    import subprocess
    from memory_profiles import identity,select
    from memory_observation import environment
    try:
        if tag is None:tag=next(t for t in api('/api/tags')['models'] if t['name']==provider.model)
        key=identity(provider.model,tag,metadata,environment(api('/api/version')['version']))
        return select(key)
    except (OSError,ValueError,KeyError,StopIteration,subprocess.SubprocessError) as exc:
        return None,'Calibration identity unavailable: '+str(exc)[:140]


def fresh_resources(root):
    """After our unload, wait briefly for telemetry to observe released memory.

    This is measurement refresh, not inference retry or permission to ignore
    pressure. An unchanged old sample must never grant the next dispatch.
    """
    from monitoring import require_resources,ResourceWait
    if not enabled():return require_resources(root)
    releases=[e['at'] for e in read_state().get('events',[])
              if e.get('event')=='release_observed' and e.get('status')=='unloaded']
    released=max(releases,default=0)
    if time.time()-released>8:return require_resources(root)
    deadline=time.monotonic()+MAX_RECOVERY_SECONDS
    while True:
        try:state=require_resources(root,newer_than=released)
        except ResourceWait as exc:state=exc.state
        stamp=(state.get('sample')or{}).get('wall_time',0)
        if stamp>=released:
            if not state['admitted']:raise ResourceWait(state)
            return state
        if time.monotonic()>=deadline:
            denied('Waiting for a fresh memory measurement after the previous model unloaded.',kind='post_unload_sample')
        time.sleep(.1)


def configure(provider, root, metadata, weights):
    """Propose a context cap before Smriti selects its optional pages."""
    if not enabled() or provider.port != 11434:
        return
    from model_context import allocation
    from monitoring import require_resources
    if not positive(weights):
        denied('Installed model size is unavailable; cannot plan its memory allocation.')
    hardware = fresh_resources(root)
    models = running()
    budget, _ = allocation(metadata)
    low, high = 4096, budget.context_limit
    calibration,status=profile_for(provider,metadata)
    provider.memory_profile=calibration;provider.memory_profile_status=status
    if calibration:high=min(high,calibration['points'][-1]['covered_tokens'])
    digest=getattr(provider,'manifest_digest',None)
    minimum = estimate(provider.model, metadata, weights, models, low, hardware,calibration,digest)
    if minimum['decision'] != 'ALLOW':
        minimum['calibration_status']=status
        with lease():
            data=read_state();data.update(plan=minimum,resident_models=models)
            note(data,'allocation_paused',model=provider.model,decision=minimum['decision'])
        denied('The selected model requires an estimated %.1f GiB plus %.1f GiB system reserve; %.1f GiB is available.' %
               (minimum['estimated_increment_bytes']/GIB,HEADROOM/GIB,minimum['available_bytes']/GIB),
               plan=minimum,kind='model_capacity',stage='configure',inference_started=False)
    while low < high:
        mid = (low + high + 1) // 2
        if estimate(provider.model, metadata, weights, models, mid, hardware,calibration,digest)['decision'] == 'ALLOW': low = mid
        else: high = mid - 1
    provider.memory_weights = weights
    provider.context_metadata = dict(metadata, _sutra_runtime_cap=low)
    # A packet can stop before inference. Keep the actual context allocation
    # visible rather than leaving the last unrelated model's plan in Monitor.
    plan = estimate(provider.model, metadata, weights, models, low, hardware, calibration, digest)
    plan['calibration_status'] = status
    provider.memory_cap_plan = plan
    with lease():
        data = read_state(); data.update(plan=plan, resident_models=models)
        note(data, 'context_planned', model=provider.model, context_tokens=low,
             decision=plan['decision'], inference_started=False)


def refit_required_context(provider, root, observation):
    """One fresh preflight for a mandatory packet above a hardware-derived cap.

    This never invokes a model, changes task allocation, discards memory or
    unloads a shared server. A larger context is used only if current measured
    hardware and the unchanged metadata/calibration admit its memory plan.
    """
    metadata = getattr(provider, 'context_metadata', {})
    cap = metadata.get('_sutra_runtime_cap')
    if not enabled() or getattr(provider, 'port', None) != 11434 or not positive(cap):
        return None
    from model_context import allocation
    from paging import Budget
    original = {key: value for key, value in metadata.items() if key != '_sutra_runtime_cap'}
    supported = allocation(original)[0].context_limit
    required = observation['required_context']
    if cap >= supported or required > supported:
        return None
    context = min(supported, math.ceil((required + 256) / 1024) * 1024)
    weights = getattr(provider, 'memory_weights', None)
    if not positive(weights):
        denied('Installed model size is unavailable for mandatory context preflight.')
    with lease():
        hardware = fresh_resources(root)
        models = running()
        calibration, status = profile_for(provider, original)
        previous = getattr(provider, 'memory_profile', None)
        if previous and (not calibration or previous['sha256'] != calibration['sha256']):
            denied('MLX calibration changed before mandatory context preflight.')
        plan = estimate(provider.model, original, weights, models, context, hardware,
                        calibration, getattr(provider, 'manifest_digest', None))
        plan.update(calibration_status=status, context_observation=observation,
                    model_context_limit=supported)
        data = read_state(); data.update(plan=plan, resident_models=models)
        admitted = plan['decision'] == 'ALLOW'
        note(data, 'mandatory_context_planned' if admitted else 'mandatory_context_paused',
             model=provider.model, context_tokens=context, decision=plan['decision'],
             inference_started=False)
        if not admitted:
            denied('The mandatory conversation packet needs at least %s context units '
                   '(%s conservative input + %s output reserve); its fresh memory plan '
                   'requires %.2f GiB additional memory plus %.1f GiB system reserve, '
                   'with %.2f GiB available. History, constraints and spending are retained.' %
                   (required, observation['required_input'], observation['output_reserve'],
                    plan['estimated_increment_bytes'] / GIB, HEADROOM / GIB,
                    plan['available_bytes'] / GIB), plan=plan, kind='model_capacity',
                   stage='context_preflight', inference_started=False)
        provider.context_metadata = dict(original, _sutra_runtime_cap=context)
        provider.memory_cap_plan = plan
        old = provider.budget
        provider.budget = Budget(context, old.response_reserve, old.template_reserve)
        _, capacity = allocation(provider.context_metadata)
        capacity['output_reserve_tokens'] = old.response_reserve
        return capacity


def dispatch_context(provider, system, payload, schema):
    charge = provider.budget.check(system, payload, schema)
    from context_window import check as check_window, exact, runtime_context
    if exact(provider.budget):
        proof = check_window(provider.budget, system, payload, schema, thinking=getattr(provider, 'thinking', None))
        provider.context_measurement = proof
        return runtime_context(provider.budget, proof)
    # Existing byte-based charge includes template reserve. Add a small runtime
    # margin, never enlarge the admitted allocation or remove payload content.
    required = charge + provider.budget.response_reserve
    return min(provider.budget.context_limit, int(math.ceil((required + 256) / 1024)) * 1024)


@contextlib.contextmanager
def dispatch(provider, root, call_id, system, payload, schema):
    provider.runtime_context_tokens = dispatch_context(provider, system, payload, schema)
    if not enabled() or provider.port != 11434:
        yield None
        return
    from monitoring import require_resources
    with lease():
        data = read_state()
        hardware = fresh_resources(root)
        models = running()
        interrupted=data.get('active')
        if interrupted and any(m['name']==interrupted.get('model') for m in models):
            denied('An interrupted Sutra call may still be running in Ollama. Wait for its model to unload before resuming.', kind='interrupted')
        weights = getattr(provider, 'memory_weights', None)
        if not positive(weights):
            tags = api('/api/tags').get('models', [])
            tag = next((m for m in tags if m.get('name') == provider.model), {})
            weights = tag.get('size')
            provider.manifest_digest=tag.get('digest')
        if not positive(weights): denied('Installed model size is unavailable at dispatch.')
        expected_digest=getattr(provider,'manifest_digest',None)
        if expected_digest:
            tag=next((m for m in api('/api/tags').get('models',[]) if m.get('name')==provider.model),None)
            if not tag or tag.get('digest')!=expected_digest or tag.get('size')!=weights:
                denied('Installed model changed after context selection; retry with fresh metadata.',kind='identity_changed')
        metadata = getattr(provider, 'context_metadata', {})
        if not metadata:
            metadata=api('/api/show',{'model':provider.model})
            provider.context_metadata=metadata
        calibration,status=profile_for(provider,metadata)
        previous=getattr(provider,'memory_profile',None)
        if previous and (not calibration or previous['sha256']!=calibration['sha256']):
            denied('MLX calibration changed after context selection; retry with a fresh allocation.')
        plan = estimate(provider.model, metadata, weights, models, provider.runtime_context_tokens, hardware,calibration,
                        getattr(provider,'manifest_digest',None))
        plan['calibration_status']=status
        data.update(plan=plan, resident_models=models)
        if plan['decision'] != 'ALLOW':
            note(data, 'allocation_paused', model=provider.model, decision=plan['decision'])
            denied('Runtime memory allocation requires recovery before loading this model.', plan=plan,
                   kind='model_capacity',stage='dispatch',inference_started=False)
        # An active marker surviving process death never grants permission to
        # unload: the server might still be finishing the interrupted request.
        data.update(active={'call_id':call_id,'model':provider.model,'pid':os.getpid()}, owned=None)
        note(data, 'dispatch', call_id=call_id, model=provider.model, context_tokens=provider.runtime_context_tokens,
             calibration_sha256=calibration['sha256'] if calibration else None)
        provider.keep_alive = 0
        provider.runtime_completed = False
        try:
            yield plan
        finally:
            data['active'] = None
            try:
                rows = running(); data['resident_models'] = rows
                resident = next((m for m in rows if m['name'] == provider.model), None)
                complete = getattr(provider, 'runtime_completed', False)
                data['owned'] = ({'name':resident['name'],'digest':resident['digest'],'completed_at':time.time()}
                                 if resident and complete and ownership() == 'exclusive' else None)
                release = 'still_resident' if resident else 'unloaded'
                if resident and not complete:
                    release = 'interrupted_ownership_unknown'
                    data['active']={'call_id':call_id,'model':provider.model,'uncertain':True}
            except Exception:
                data['owned'] = None; release = 'verification_unavailable'
                if not getattr(provider,'runtime_completed',False):
                    data['active']={'call_id':call_id,'model':provider.model,'uncertain':True}
            # Failure to collect diagnostics must not turn a settled model result
            # into a new request error. The OS lease still releases in all cases.
            try: note(data, 'release_observed', call_id=call_id, status=release)
            except OSError: pass


def stable_resources(root):
    from monitoring import resource_state
    deadline = time.monotonic() + MAX_RECOVERY_SECONDS
    previous = None
    while True:
        state = resource_state(root, refresh=True, newer_than=previous)
        stamp = (state.get('sample') or {}).get('wall_time')
        if not state['admitted']: return state
        if previous is not None and stamp is not None and stamp > previous: return state
        if time.monotonic() >= deadline:
            frame=KarmaAdapter().frame('runtime-recovery-stability-v1',2*KarmaAdapter().boundary.k,0.)
            return dict(state, **frame, admitted=False, reason='Waiting for two distinct healthy memory samples before resuming.')
        previous = stamp
        time.sleep(.25)


def recover(root, audit=None):
    """One idle release at most, then a bounded stability check. Never infer."""
    from monitoring import resource_state, ResourceWait
    if not enabled(): return None
    with lease():
        data = read_state(); before = resource_state(root, refresh=True)
        if os.environ.get('SUTRA_STRATA_ONLY') == '1' or os.environ.get('SUTRA_ENGINE_ONLY') == '1':
            after = stable_resources(root)
            result = {'profile': PROFILE, 'action': 'observe_native_runtime',
                      'before': before, 'after': after, 'admitted': after['admitted'],
                      'max_release_actions': 0, 'automatic_inference': False,
                      'release_kernel': None}
            if audit:
                audit('MEMORY_RECOVERY_OBSERVED', result)
            note(data, 'recovery', action=result['action'], admitted=after['admitted'])
            return result
        rows = running(); data['resident_models'] = rows
        owned = data.get('owned')
        candidate = next((m for m in rows if owned and m['name']==owned.get('name') and m['digest']==owned.get('digest')), None)
        action = 'observe_only_shared_server' if ownership() == 'shared' else 'no_confirmed_idle_owned_model'
        kernel=None
        if ownership() == 'exclusive' and not data.get('active') and candidate:
            # The explicit exclusive-server grant is required in addition to
            # completed-call ownership and a fresh matching manifest identity.
            kernel = KarmaAdapter().counted_slots(0, 1)
            if kernel['decision']!='ALLOW':denied('Idle-model recovery allocation is exhausted.')
            if audit:audit('MEMORY_RELEASE_PLANNED',{'model':candidate['name'],'digest':candidate['digest'],'kernel':kernel})
            data['owned'] = None
            note(data, 'release_requested', model=candidate['name'], kernel=kernel)
            api('/api/generate', {'model':candidate['name'],'keep_alive':0,'stream':False})
            rows = running(); data['resident_models'] = rows
            action = 'release_unconfirmed' if any(m['name']==candidate['name'] for m in rows) else 'released_owned_model'
        after = stable_resources(root)
        result = {'profile':PROFILE, 'action':action, 'before':before, 'after':after,
                  'admitted':after['admitted'], 'max_release_actions':1, 'automatic_inference':False,'release_kernel':kernel}
        if audit:audit('MEMORY_RECOVERY_OBSERVED',result)
        note(data, 'recovery', action=action, admitted=after['admitted'])
        return result


def snapshot():
    if not enabled(): return {'enabled':False}
    data = read_state()
    result = {k:data.get(k) for k in ('profile','updated_at','active','plan','events')}
    result.update(enabled=True, ownership=ownership())
    if os.environ.get('SUTRA_ENGINE_ONLY') == '1':
        import engine_client
        result.update(backend='sutra_engine', ownership='sutra_owned')
        if (result.get('plan') or {}).get('provider') != 'sutra_engine':
            result['plan'] = None
        try:
            worker = engine_client.status()
            row = worker.get('profile')
            result.update(resident_models=[{'name': row, 'size': None,
                'context_length': worker['context_tokens'], 'runner': 'sutra_engine'}] if row else [],
                inventory_available=True, inventory_at=time.time(), engine_worker=worker)
        except Exception:
            result.update(resident_models=[], inventory_available=False)
        return result
    if os.environ.get('SUTRA_STRATA_ONLY') == '1':
        result.update(backend='strata', ownership='native_owner')
        if (result.get('plan') or {}).get('provider') != 'strata':
            result['plan'] = None
        try:
            result.update(resident_models=native_inventory(), inventory_available=True,
                          inventory_at=time.time())
        except (OSError, ValueError, InvalidUpdate, http.client.HTTPException):
            result.update(resident_models=[], inventory_available=False)
        return result
    try: result.update(resident_models=running(), inventory_available=True, inventory_at=time.time())
    except Exception: result.update(resident_models=[], inventory_available=False)
    return result


def native_inventory():
    """Observe the active local endpoint without spawning native catalog scans."""
    import re
    path = Path.home() / 'Library/Application Support/Sutra/Strata/startup.json'
    with path.open('rb') as stream:
        raw = stream.read(8193)
    if len(raw) > 8192:
        raise InvalidUpdate('Native runtime record exceeds the observation bound')
    record = json.loads(raw)
    if not isinstance(record, dict):
        raise InvalidUpdate('Invalid native runtime record')
    port, model, profile = record.get('port'), record.get('model'), record.get('profile')
    if (type(port) is not int or not 1 <= port <= 65535 or not isinstance(model, str)
            or not isinstance(profile, str) or not re.fullmatch('[a-z][a-z0-9_-]{0,63}', profile)):
        raise InvalidUpdate('Native runtime observation needs a configured local profile')
    from strata_model import _headers
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
    response = None
    try:
        connection.request('GET', '/v1/status', headers=_headers())
        response = connection.getresponse()
        body = response.read(65537)
        if response.status != 200 or len(body) > 65536:
            raise InvalidUpdate('Native runtime status is unavailable')
        status = json.loads(body)
    except ConnectionRefusedError:
        return []
    finally:
        if response is not None:
            response.close()
        connection.close()
    if not isinstance(status, dict) or status.get('service') != 'strata' or status.get('model') != model:
        raise InvalidUpdate('Native runtime status differs from the active profile')
    if status.get('loaded') is not True:
        return []
    context = (status.get('context') or {}).get('max_positions')
    if type(context) is not int or context < 1:
        raise InvalidUpdate('Native runtime context is unavailable')
    return [{'name': 'strata:profile/' + profile, 'size': None,
             'context_length': context, 'runner': 'strata'}]


def note_native_plan(plan):
    if enabled():
        data = read_state()
        data['plan'] = dict(plan, provider=plan.get('provider', 'strata'))
        note(data, 'native_dispatch', model=plan['model'])
