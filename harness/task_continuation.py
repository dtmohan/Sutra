"""Host continuation decisions; readiness observations grant no task authority."""
import copy
import http.client
import json
import re
import time
from types import SimpleNamespace

from context_manager import InvalidUpdate, canonical, sha
from memory_layer import PageFault

VERSION = 1
RUNNABLE = {'CLASSIFYING', 'BINDING', 'WORKSPACE', 'MAPPING', 'BRANCHING',
            'CHILDREN', 'SYNTHESIS', 'CODING'}
APPROVAL = {'NEED_ARM', 'NEED_PERMISSION', 'NEED_TOOL_APPROVAL', 'AWAITING_ACTION'}
INITIAL_EVENTS = {'start', 'start_existing', 'start_review', 'step', 'resume', 'grant',
                  'capability_grant', 'approve_document', 'supply_evidence',
                  'restart_review', 'accept_provenance'}


def _wait_kind(flow):
    stop = flow.get('stop') or {}
    if stop.get('status') != 'resource_wait':
        return None
    state = stop.get('measurement') or {}
    runtime = state.get('runtime') or {}
    if runtime.get('inference_started') is True or runtime.get('kind') in ('interrupted', 'identity_changed'):
        return None
    category = stop.get('blocker')
    if category in ('hardware_pressure', 'measurement_unavailable'):
        return 'hardware'
    if (category == 'model_admission' and runtime.get('kind') == 'model_capacity'
            and runtime.get('inference_started') is False and _model(stop)):
        return 'model_capacity'
    if category == 'runtime_busy' and runtime.get('kind') == 'busy' and _model(stop):
        return 'runtime_busy'
    return None


def describe(flow, readiness=None):
    """Project a disposition without measuring devices or accessing source."""
    state = flow.get('state')
    bound = {key: copy.deepcopy(flow.get(key)) for key in ('flow_id', 'cursor', 'binding')}
    bound['binding'] = copy.deepcopy(flow.get('binding') or flow.get('bound_task'))
    fingerprint = sha(canonical(dict(bound, state=state, stop=flow.get('stop'), recovery=flow.get('recovery'))))
    result = dict(version=VERSION, **bound, fingerprint=fingerprint, next_event=None,
                  disposition='stopped', reason='This workflow needs explicit recovery.')
    if state == 'DONE':
        result.update(disposition='finished', reason='The workflow has a terminal receipt.')
    elif state in ('CANCELLED', 'QUARANTINED', 'CANCELLING', 'EXECUTING_ACTION'):
        result['reason'] = 'Stopped or unresolved work is never replayed automatically.'
    elif flow.get('recovery'):
        result['reason'] = 'Task policy, source custody or binding needs explicit recovery.'
    elif state in RUNNABLE:
        result.update(disposition='continue', next_event='step', reason='The admitted workflow has a runnable next stage.')
    elif state in APPROVAL:
        if state == 'NEED_TOOL_APPROVAL' and flow.get('capability_settled') is True:
            result.update(disposition='continue', next_event='step', reason='The approved tool operation already settled.')
        else:
            result.update(disposition='approval', reason='The next operation requires an explicit authorization decision.')
    elif state == 'PAUSED':
        stop = flow.get('stop') or {}
        if stop.get('status') == 'evidence_bridge':
            result.update(disposition='evidence', reason='The saved request needs additional evidence.')
        elif stop.get('status') == 'provenance_confirmation' or stop.get('allocation_need'):
            result.update(disposition='approval', reason='The saved request needs an explicit scope or allocation decision.')
        elif kind := _wait_kind(flow):
            result.update(disposition='wait', next_event='continuation_probe', wait_kind=kind,
                          reason='A fresh observation of the saved external blocker is required.')
            if readiness is not None:
                expected = dict(bound, fingerprint=fingerprint)
                if (not isinstance(readiness, dict) or any(readiness.get(key) != value for key, value in expected.items())
                        or type(readiness.get('ready')) is not bool):
                    raise InvalidUpdate('Continuation readiness belongs to another workflow checkpoint')
                if readiness['ready']:
                    result.update(disposition='continue', next_event='resume',
                                  reason='The saved external blocker passed a fresh readiness check.')
                elif (readiness.get('observation') or {}).get('blocker') == 'unknown_outcome':
                    result.update(disposition='stopped', next_event=None,
                                  reason='An unresolved execution outcome requires explicit recovery.')
    from karma_adapter import KarmaAdapter
    result['kernel'] = KarmaAdapter().task_continuation({
        'profile': 'task-continuation-v1', 'version': VERSION,
        'disposition': result['disposition'], 'next_event': result['next_event'],
        'binding_current': not bool(flow.get('recovery')),
        'checkpoint_current': bool(result['flow_id'] and result['cursor']),
        'readiness_verified': bool(readiness and readiness.get('ready') is True)})
    return result


def _runner(request):
    value = request.get('task_runner')
    if value is None:
        if 'continuation_receipt' in request:
            raise InvalidUpdate('A continuation receipt requires its originating task runner')
        return None
    if (not isinstance(value, dict) or set(value) != {'version', 'request_id', 'request_digest'}
            or type(value['version']) is not int or value['version'] != VERSION
            or not isinstance(value['request_id'], str)
            or not re.fullmatch('[a-f0-9]{32}', value['request_id'])
            or not isinstance(value['request_digest'], str)
            or not re.fullmatch('[a-f0-9]{64}', value['request_digest'])):
        raise InvalidUpdate('Invalid task-runner identity')
    return value['request_id']


def _latest(s, request_id):
    """Read the latest immutable decision through its bounded per-runner index."""
    if not s.db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='page_audit_runner_continuation'").fetchone():
        with s.db:
            s.db.execute("CREATE INDEX page_audit_runner_continuation ON page_audit("
                         "json_extract(payload_json,'$.runner_request_id'),seq) "
                         "WHERE event='RUNNER_CONTINUATION_DECISION'")
    row = s.db.execute("SELECT seq,payload_json,previous_hash,event_hash FROM page_audit "
                       "WHERE event='RUNNER_CONTINUATION_DECISION' "
                       "AND json_extract(payload_json,'$.runner_request_id')=? ORDER BY seq DESC LIMIT 1",
                       (request_id,)).fetchone()
    if not row:
        return None
    seq, payload, previous_hash, digest = row
    if sha(canonical([seq, 'RUNNER_CONTINUATION_DECISION', payload, previous_hash])) != digest:
        raise InvalidUpdate('Task-continuation receipt differs from its immutable record')
    record = json.loads(payload)
    if record.get('version') != VERSION or record.get('runner_request_id') != request_id:
        raise InvalidUpdate('Task-continuation receipt binding changed')
    return record, {'version': VERSION, 'seq': seq, 'hash': digest}


def authorize(s, request, current=None):
    """Check an automatic intent before execution; a permit grants no resources."""
    request_id = _runner(request)
    if request_id is None:
        return
    previous = _latest(s, request_id)
    if not s.audit_valid():
        raise InvalidUpdate('Task-continuation audit chain is invalid')
    if previous and previous[0].get('request_digest') != request['task_runner']['request_digest']:
        raise InvalidUpdate('Task-runner submission digest changed')
    if previous and previous[0].get('runner_stopped') is True:
        raise InvalidUpdate('Task runner was stopped; its automatic permits are revoked')
    supplied = request.get('continuation_receipt')
    if not previous:
        if supplied is not None:
            raise InvalidUpdate('Unknown task-continuation receipt')
        if request.get('event', 'step') not in INITIAL_EVENTS:
            raise InvalidUpdate('Automatic observation requires a prior task-continuation receipt')
        return
    record, receipt = previous
    if (not isinstance(supplied, dict) or set(supplied) != {'version', 'seq', 'hash'}
            or type(supplied.get('version')) is not int or type(supplied.get('seq')) is not int
            or supplied != receipt):
        raise InvalidUpdate('Task-continuation receipt is missing, stale or replayed')
    if current is None:
        raise InvalidUpdate('Task-continuation requires its saved workflow checkpoint')
    descriptor = describe(current)
    if (request.get('flow_id') != record.get('flow_id')
            or any(descriptor.get(key) != record.get(key) for key in ('flow_id', 'cursor', 'binding', 'fingerprint'))
            or request.get('cursor') != record.get('cursor')
            or request.get('binding') != record.get('binding')):
        raise InvalidUpdate('Task-continuation checkpoint or task binding changed')
    from karma_adapter import KarmaAdapter
    kernel = record.get('kernel') or {}
    try:
        expected = KarmaAdapter().task_continuation(kernel.get('observation'))
    except (ValueError, TypeError, KeyError):
        raise InvalidUpdate('Task-continuation kernel observation is invalid') from None
    if expected != kernel:
        raise InvalidUpdate('Task-continuation kernel decision changed')
    event = request.get('event', 'step')
    if event == 'runner_summary':
        return  # Read-only accounting is valid under terminal FIN as well.
    if (event != record.get('next_event')
            or event in ('step', 'resume') and kernel['decision'] != 'ALLOW'
            or event == 'continuation_probe' and kernel['decision'] != 'BRIDGE'
            or event not in ('step', 'resume', 'continuation_probe')):
        raise InvalidUpdate('Task-continuation intent was not admitted by the kernel')
    if event == 'resume':
        readiness = record.get('readiness')
        if (not isinstance(readiness, dict) or readiness.get('ready') is not True
                or record.get('readiness_hash') != sha(canonical(readiness))
                or describe(current, readiness)['kernel'] != kernel):
            raise InvalidUpdate('Automatic resume has no bound successful readiness observation')


def check_request(s, request):
    """Run before reconciliation, admission or a workflow operation can mutate state."""
    if _runner(request) is None:
        return
    current = None
    if request.get('flow_id'):
        from orchestration_flow import response
        current = response(s, request['flow_id'], _runner_poll=True)
    authorize(s, request, current)


def attach(s, root, request, flow):
    """Anchor a runner decision to the existing audit chain, without moving it."""
    request_id = _runner(request)
    if request_id is None or not flow.get('flow_id'):
        return flow
    descriptor = copy.deepcopy(flow['continuation'])
    descriptor['runner_request_id'] = request_id
    keys = ('flow_id', 'cursor', 'binding', 'fingerprint', 'disposition', 'next_event', 'kernel')
    material = {key: copy.deepcopy(descriptor[key]) for key in keys}
    readiness = flow.get('continuation_probe')
    if descriptor['next_event'] == 'resume':
        if not readiness or readiness.get('ready') is not True:
            raise InvalidUpdate('Automatic resume requires a fresh successful readiness observation')
        material['readiness_hash'] = sha(canonical(readiness))
    previous = _latest(s, request_id)
    if previous and all(previous[0].get(key) == value for key, value in material.items()):
        receipt = previous[1]
    else:
        import orchestration as machine
        owner = machine.node(s, flow['flow_id'])
        record = dict(material, version=VERSION, runner_request_id=request_id,
                      request_digest=request['task_runner']['request_digest'],
                      workspace=owner['workspace'], chat_key=owner.get('chat_key'),
                      at=time.time(), intent=descriptor['next_event'] or descriptor['disposition'],
                      reason=descriptor['reason'],
                      request_event=request.get('event', 'step'), flow_receipt=descriptor['cursor'],
                      previous_decision=previous[1] if previous else None)
        if readiness is not None:
            record['readiness'] = copy.deepcopy(readiness)
        encoded = canonical(record).encode()
        if len(encoded) > 65536:
            raise InvalidUpdate('Task-continuation decision exceeds its audit bound')
        from storage_policy import require_space
        require_space(root, reserve=len(encoded) + 2048)
        with s.db:
            s._audit('RUNNER_CONTINUATION_DECISION', record)
        receipt = _latest(s, request_id)[1]
    descriptor['receipt'] = receipt
    return dict(flow, continuation=descriptor)


def stop(s, root, workspace, request):
    """Revoke only this runner UID; the saved task and every charge remain intact."""
    request_id = _runner(request)
    value = request.get('runner_stop')
    if (request_id is None or not isinstance(value, dict)
            or set(value) != {'version', 'request_digest', 'reason'}
            or type(value['version']) is not int or value['version'] != VERSION
            or value['request_digest'] != request['task_runner']['request_digest']
            or not isinstance(value['reason'], str) or not 1 <= len(value['reason']) <= 500):
        raise InvalidUpdate('Invalid task-runner Stop observation')
    if request.get('chat_key') is not None and (not isinstance(request['chat_key'], str)
            or not re.fullmatch('[a-f0-9]{64}', request['chat_key'])):
        raise InvalidUpdate('Invalid task-runner Stop chat identity')
    previous = _latest(s, request_id)
    if not s.audit_valid():
        raise InvalidUpdate('Task-continuation audit chain is invalid')
    if previous and (previous[0].get('request_digest') != value['request_digest']
            or previous[0].get('workspace') != str(workspace)
            or previous[0].get('chat_key') != request.get('chat_key')):
        raise InvalidUpdate('Task-runner Stop belongs to another submission or chat')
    if previous and previous[0].get('runner_stopped') is True:
        return {'stopped': True, 'runner_request_id': request_id, 'request_digest': value['request_digest'],
                'kernel': previous[0]['kernel'], 'receipt': previous[1]}
    fid = previous[0].get('flow_id') if previous else request.get('flow_id')
    bound = {'flow_id': fid, 'cursor': None, 'binding': None}
    if fid:
        import orchestration as machine
        from orchestration_flow import info
        node, data = info(s, fid)
        if node['workspace'] != str(workspace) or node.get('chat_key') != request.get('chat_key'):
            raise InvalidUpdate('Task-runner Stop belongs to another workflow or chat')
        bound.update(cursor=machine.events(s, fid)[-1]['receipt'], binding=data.get('binding'))
    from karma_adapter import KarmaAdapter
    kernel = KarmaAdapter().task_continuation({
        'profile': 'task-continuation-v1', 'version': VERSION, 'disposition': 'stopped',
        'next_event': None, 'binding_current': True, 'checkpoint_current': bool(bound['cursor']),
        'readiness_verified': False})
    record = dict(bound, version=VERSION, runner_request_id=request_id,
                  request_digest=value['request_digest'], workspace=str(workspace), chat_key=request.get('chat_key'),
                  at=time.time(), intent='stop', disposition='stopped', next_event=None, kernel=kernel,
                  runner_stopped=True, reason=value['reason'], flow_receipt=bound['cursor'],
                  previous_decision=previous[1] if previous else None)
    # The bounded administrative revocation must still be recorded when the
    # workload's storage allowance or authorization has stopped further work.
    with s.db:
        s._audit('RUNNER_CONTINUATION_DECISION', record)
    receipt = _latest(s, request_id)[1]
    return {'stopped': True, 'runner_request_id': request_id, 'request_digest': value['request_digest'],
            'kernel': kernel, 'receipt': receipt}


def _model(stop):
    state = stop.get('measurement') or {}
    runtime = state.get('runtime') or {}
    plan = runtime.get('plan') or state.get('plan') or {}
    return plan.get('model') or runtime.get('model') or state.get('model') or stop.get('model')


def _model_ready(root, stop, hardware):
    """Inspect only; never configure, activate, unload or infer."""
    from runtime_memory import api, estimate, profile_for, running
    model = _model(stop)
    if not isinstance(model, str) or not model:
        return False, {'reason': 'The blocked provider identity was not recorded; explicit inspection is required.'}
    from remote_profiles import is_remote
    if is_remote(model):
        return hardware.get('admitted') is True, {'provider': 'sutra_remote', 'model': model,
                                                  'hardware': hardware}
    from strata_model import is_strata, managed, profile_metadata, status, _configured_int, _identity
    if is_strata(model):
        profile = profile_metadata(model) if managed(model) else None
        port = profile['port'] if profile else _configured_int('SUTRA_STRATA_PORT', 1, 65535)
        state = status(port)
        _identity(state, model)
        if profile and state.get('model') != profile['model']:
            return False, {'reason': 'The running native model differs from the blocked profile.'}
        ready = state.get('loaded') is True and (state.get('activity') or {}).get('in_flight') == 0
        return ready, {'provider': 'strata', 'model': model, 'loaded': state.get('loaded'),
                       'in_flight': (state.get('activity') or {}).get('in_flight')}
    from engine_profiles import is_engine
    if is_engine(model):
        from engine_profiles import load
        from engine_client import status as engine_status
        profile = load(model)
        state = engine_status()
        ready = (state.get('ok') is True and state.get('running') is True and state.get('busy') is False
                 and state.get('profile_hash') == profile['profile_hash'])
        return ready, {'provider': 'sutra_engine', 'model': model, 'status': state}
    tags = api('/api/tags').get('models', [])
    tag = next((row for row in tags if row.get('name') == model), None)
    if not tag:
        return False, {'model': model, 'reason': 'The selected model is not in the current local inventory.'}
    metadata = api('/api/show', {'model': model})
    if metadata.get('remote_model') or metadata.get('remote_host'):
        return False, {'model': model, 'reason': 'The selected provider is not a local model.'}
    if stop.get('blocker') == 'runtime_busy':
        return True, {'provider': 'ollama', 'model': model, 'slot_available': True,
                      'meaning': 'The Sutra OS slot is free; model capacity and dispatch remain separately gated.'}
    runtime = (stop.get('measurement') or {}).get('runtime') or {}
    plan = runtime.get('plan') or (stop.get('measurement') or {}).get('plan') or {}
    context = plan.get('context_tokens')
    if type(context) is not int or context <= 0:
        return False, {'model': model, 'reason': 'The blocked model allocation was not recorded.'}
    profile, _ = profile_for(SimpleNamespace(model=model, port=11434), metadata, tag)
    fresh = estimate(model, metadata, tag.get('size'), running(), context, hardware, profile, tag.get('digest'))
    return fresh['decision'] == 'ALLOW', {'provider': 'ollama', 'model': model, 'plan': fresh}


def _owned_call(s, fid, call_id):
    if not call_id:
        return False
    row = s.db.execute('SELECT record_json FROM invocations WHERE call_id=?', (call_id,)).fetchone()
    if not row:
        return False
    import orchestration as machine
    owner = (json.loads(row[0]).get('orchestration') or {}).get('node_id')
    while owner:
        node = machine.node(s, owner)
        if node.get('flow_id') == fid or owner == fid:
            return True
        owner = node.get('parent_id')
    return False


def _pending_call(s, fid):
    for iid, call_id in s.db.execute('SELECT i.id,i.call_id FROM invocations i LEFT JOIN invocation_receipts r '
                                     'ON r.invocation_id=i.id WHERE r.invocation_id IS NULL'):
        if _owned_call(s, fid, call_id):
            return iid
    return None


def _material(observed, kind):
    """Only changed readiness outcomes append history, not clock or RAM noise."""
    value = observed.get('observation') or {}
    hardware = value.get('hardware') or value.get('blocking_measurement') or {}
    provider = value.get('provider') or {}
    keep = lambda source, keys: {key: source[key] for key in keys if key in source}
    return {'ready': observed['ready'], 'wait_kind': kind, 'reason': value.get('reason'),
        'hardware': keep(hardware, ('admitted', 'available', 'decision', 'reason', 'thermal_label')),
        'pressure': (hardware.get('sample') or {}).get('memory_pressure_raw'),
        'provider': keep(provider, ('provider', 'model', 'loaded', 'in_flight', 'slot_available', 'reason')),
        'plan': keep(provider.get('plan') or {}, ('decision', 'model', 'context_tokens', 'resident',
                                                'estimated_increment_bytes', 'model_file_bytes')),
        'status': keep(provider.get('status') or {}, ('ok', 'running', 'busy', 'phase', 'profile_hash', 'instance_id'))}


def _record(s, root, observed, kind):
    """One bounded diagnostic head per flow, anchored to immutable observations.

    The mutable head only avoids duplicate logging. Fresh readiness is always
    measured again; neither this row nor an old ALLOW grants execution.
    """
    material_hash = sha(canonical(_material(observed, kind)))
    snapshot = canonical(observed)
    if len(snapshot.encode()) > 65536:
        raise InvalidUpdate('Readiness observation exceeds its diagnostic bound')
    with s.db:
        s.db.execute('CREATE TABLE IF NOT EXISTS task_readiness_heads('
                     'flow_id TEXT PRIMARY KEY,fingerprint TEXT,material_hash TEXT,'
                     'audit_seq INTEGER,audit_hash TEXT,observation_json TEXT)')
        previous = s.db.execute('SELECT fingerprint,material_hash,audit_seq,audit_hash FROM task_readiness_heads '
                                'WHERE flow_id=?', (observed['flow_id'],)).fetchone()
        if previous:
            fingerprint, material, seq, digest = previous
            row = s.db.execute('SELECT event,payload_json,previous_hash,event_hash FROM page_audit WHERE seq=?', (seq,)).fetchone()
            if (not row or row[0] != 'TASK_READINESS_PROBED' or row[3] != digest
                    or sha(canonical([seq, row[0], row[1], row[2]])) != digest):
                raise InvalidUpdate('Readiness diagnostic head differs from its immutable observation')
            prior = json.loads(row[1])
            if (prior.get('flow_id') != observed['flow_id'] or prior.get('fingerprint') != fingerprint
                    or prior.get('condition_hash') != material):
                raise InvalidUpdate('Readiness diagnostic head binding changed')
        if not previous or previous[:2] != (observed['fingerprint'], material_hash):
            from storage_policy import require_space
            require_space(root, reserve=len(snapshot.encode()) + 2048)
            s._audit('TASK_READINESS_PROBED', dict(observed, condition_hash=material_hash))
            seq, digest = s.db.execute('SELECT seq,event_hash FROM page_audit ORDER BY seq DESC LIMIT 1').fetchone()
        else:
            seq, digest = previous[2:]
        s.db.execute('INSERT OR REPLACE INTO task_readiness_heads VALUES(?,?,?,?,?,?)',
                     (observed['flow_id'], observed['fingerprint'], material_hash, seq, digest, snapshot))


def probe(s, root, flow):
    """Refresh one explicitly eligible blocker; model execution is impossible here."""
    descriptor = describe(flow)
    bound = {key: copy.deepcopy(descriptor[key]) for key in ('flow_id', 'cursor', 'binding', 'fingerprint')}
    observed = dict(version=VERSION, **bound, ready=False, observed_at=time.time(),
                    observation={'reason': descriptor['reason']})
    if descriptor['disposition'] != 'wait':
        return observed
    # A missing receipt is an unknown outcome, never proof that a retry is safe.
    pending = _pending_call(s, flow['flow_id'])
    if pending:
        observed['observation'] = {'blocker': 'unknown_outcome',
            'reason': 'An invocation has no settled receipt; automatic continuation is blocked.'}
        return observed
    try:
        from provider_resources import require_resources
        model = _model(flow.get('stop') or {})
        hardware = require_resources(root,model=model)
        observation = {'hardware': hardware, 'inference_started': False}
        kind = descriptor['wait_kind']
        from remote_profiles import is_remote
        if is_remote(model):
            # A remote observation includes the exact resident GPU/model and
            # idle slot; local Ollama ownership cannot establish remote liveness.
            observed['ready'], observation['provider'] = _model_ready(root, flow['stop'], hardware)
        elif kind == 'hardware':
            observed['ready'] = True
        else:
            from runtime_memory import enabled, lease, read_state
            if not enabled():
                observation['reason'] = 'Shared inference-slot coordination is unavailable.'
            else:
                with lease():
                    current = read_state()
                    active = current.get('active') or {}
                    interrupted = current.get('interrupted') or {}
                    if (_owned_call(s, flow['flow_id'], active.get('call_id'))
                            or _owned_call(s, flow['flow_id'], interrupted.get('latest_call_id'))):
                        observation.update(blocker='unknown_outcome',
                                           reason='Runtime ownership or an interrupted request is unresolved.')
                    elif kind == 'model_capacity' or _model(flow.get('stop') or {}):
                        observed['ready'], observation['provider'] = _model_ready(root, flow['stop'], hardware)
                    else:
                        # The unbound busy observation may refer to an external
                        # provider rather than this OS lease. Do not guess.
                        observation['reason'] = 'The busy provider identity is unavailable; explicit inspection is required.'
        observed['observation'] = observation
    except (InvalidUpdate, OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as error:
        observed['observation'] = {'reason': str(error), 'inference_started': False}
        if getattr(error, 'state', None):
            observed['observation']['blocking_measurement'] = error.state
    _record(s, root, observed, descriptor['wait_kind'])
    return observed


def summary(s, fid, flow):
    """Aggregate once at a runner boundary, including the classifier's run."""
    from orchestration_flow import info, workflow_usage
    import orchestration as machine
    from token_usage import history, summarize
    _, data = info(s, fid)
    usage = workflow_usage(s, fid)
    calls = {row['invocation_id']: row for row in usage['calls']}
    conversation = None
    referenced = {data['conversation_id']} if data.get('conversation_id') else set()
    for event in machine.events(s, fid):
        record = event['data']
        referenced.update(record[key] for key in ('conversation_id', 'routing_continuation', 'document_candidate_id')
                          if isinstance(record.get(key), str))
        failed = (record.get('workflow_stop') or {}).get('failed_invocation_id')
        if isinstance(failed, str):
            referenced.add(failed)
    for iid in referenced:
        row = s.db.execute('SELECT record_json FROM invocations WHERE id=?', (iid,)).fetchone()
        if row:
            env = json.loads(row[0])
            calls.update({item['invocation_id']: item for item in history(s.db, run_id=env['run_id'])['calls']})
    if data.get('conversation_id'):
        try:
            from conversation import read
            conversation = read(s, data['conversation_id'])
        except (InvalidUpdate, PageFault):
            pass
    ordered = sorted(calls.values(), key=lambda row: (row['created_at'], row['invocation_id']))
    return {'runner_usage': {'calls': ordered, 'totals': summarize(ordered), 'total_calls': len(ordered), 'limit': None},
            **({'conversation': conversation} if conversation else {})}
