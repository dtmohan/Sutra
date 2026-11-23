"""Test-only transport: real orchestration and receipts, deterministic models.

Invoked by a private executable wrapper. This is not a shipped provider or a
production injection interface. All workspaces, controls and ledgers are temporary.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / 'harness'))

from context_manager import canonical, sha
from invocation_gateway import InvocationLedger
from karma_adapter import KarmaAdapter
import monitoring
import orchestration as machine
import orchestration_flow as flow
import review_budget
import runtime_memory
import task_continuation
import vscode_bridge as bridge
from test_model_packet import WireConversation, WireModel


MODELS = {'low': 'fixture-small', 'mid': 'fixture-mid', 'high': 'fixture-large'}
CHAT = 'a' * 64
PROMPT = ('Explain this workspace architecture and request flow. Remember Cedar CED724; '
          'read source only, preserve the public API, and use no network or new dependencies.')


def controls(root):
    try:
        return json.loads((root / 'runner-controls.json').read_text())
    except FileNotFoundError:
        return {}


def log(root, kind, **fields):
    with (root / 'runner-events.ndjson').open('a') as stream:
        stream.write(canonical(dict(kind=kind, at=time.monotonic(), **fields)) + '\n')
        stream.flush()


def wait_control(root, key):
    while controls(root).get(key):
        time.sleep(.02)


def observe(storage):
    """Copy the fixture database read-only before opening its verification API."""
    path = storage / 'state.sqlite'
    if not path.exists():
        return {'flows': [], 'invocations': [], 'receipts': [], 'verified': True}
    with tempfile.TemporaryDirectory(prefix='sutra-runner-observation-') as directory:
        target = Path(directory) / 'state.sqlite'
        source = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        store = InvocationLedger(target)
        try:
            flows = []
            for nid, raw in store.db.execute('SELECT id,record_json FROM orchestration_nodes ORDER BY rowid'):
                node = json.loads(raw)
                if node['kind'] != 'flow':
                    continue
                _, data = flow.info(store, nid)
                session = (data.get('binding') or {}).get('session')
                children = []
                for child in data.get('children') or []:
                    cursor = machine.slice_cursor(store, child)
                    children.append({'id': child, 'state': machine.state(store, child),
                                     'cursor': cursor, 'node': machine.node(store, child)})
                history = machine.events(store, nid)
                flows.append({'id': nid, 'state': machine.state(store, nid),
                              'cursor': history[-1]['receipt'], 'binding': data.get('binding'),
                              'session': session, 'children': children,
                              'contract': store.pinned(session) if session else None,
                              'usage': review_budget.usage(store, session) if session else None,
                              'stop': data.get('workflow_stop'),
                              'events': [{'seq': event['seq'], 'kind': event['kind']}
                                         for event in machine.events(store, nid)]})
            invocations = []
            for raw, receipt in store.db.execute(
                    'SELECT i.record_json,r.record_json FROM invocations i '
                    'LEFT JOIN invocation_receipts r ON r.invocation_id=i.id ORDER BY i.rowid'):
                envelope = json.loads(raw)
                settled = json.loads(receipt) if receipt else None
                invocations.append({'id': envelope['id'], 'session': envelope['session'],
                                    'role': envelope['role'], 'model': envelope['model'],
                                    'status': settled['status'] if settled else 'pending',
                                    'charge': envelope['charge'], 'budget_profile': envelope['budget_profile'],
                                    'settlement': settled.get('accounting_settlement') if settled else None,
                                    'token_usage': settled.get('token_usage') if settled else None})
            sessions = [row[0] for row in store.db.execute('SELECT id FROM sessions')]
            decisions = [{'seq': seq, 'hash': digest, 'previous_hash': previous,
                          'record': json.loads(raw)} for seq, raw, previous, digest in store.db.execute(
                              "SELECT seq,payload_json,previous_hash,event_hash FROM page_audit "
                              "WHERE event='RUNNER_CONTINUATION_DECISION' ORDER BY seq")]
            return {'flows': flows, 'invocations': invocations,
                    'receipts': [[iid, sha(raw)] for iid, raw in store.db.execute(
                        'SELECT invocation_id,record_json FROM invocation_receipts ORDER BY rowid')],
                    'runner_decisions': decisions,
                    'verified': all(store.verify_invocations(sid) for sid in sessions),
                    'audit_valid': store.audit_valid()}
        finally:
            store.close()


def install_fixture(workspace, storage):
    original_handle = flow.handle
    original_bridge = bridge.handle
    original_pending = task_continuation._pending_call
    original_model_ready = task_continuation._model_ready

    def pending(store, fid):
        # Controlled, read-only observation for the transport's unknown-outcome
        # branch. Production lineage classification has its own ledger tests.
        if controls(storage).get('owned_unknown'):
            return 'controlled-owned-unsettled-invocation'
        return original_pending(store, fid)

    def hardware(root, now=None, **kwargs):
        ready = controls(storage).get('hardware_ready', True)
        sampled_at = time.time() if now is None else now
        sample = {'wall_time': sampled_at, 'thermal_state': 0, 'memory_pressure_raw': 1 if ready else 2,
                  'free_bytes': 32 * 1024 ** 3, 'inactive_bytes': 0}
        log(storage, 'hardware_probe', ready=ready)
        return dict(KarmaAdapter().hardware(sample, sampled_at), sample_source='controlled-fixture')

    def recover(root, audit=None):
        state = hardware(root)
        return {'admitted': state['admitted'], 'after': state,
                'action': 'controlled observation only', 'automatic_inference': False}

    def model_ready(root, stop, state):
        if stop.get('blocker') == 'runtime_busy':
            ready = not controls(storage).get('runtime_busy')
            return ready, {'provider': 'ollama', 'model': stop['measurement']['runtime']['model'],
                           'slot_available': ready}
        return original_model_ready(root, stop, state)

    def with_models(store, meta, ws, root, request, providers=None):
        def provider(stage):
            config = controls(storage)
            if config.get('hardware_stage') == stage:
                monitoring.require_resources(storage)
            models = request.get('models', MODELS)
            tier = request.get('tier', 'low')
            name = models[tier if tier in MODELS else 'low']
            if config.get('busy_stage') == stage and config.get('runtime_busy'):
                runtime_memory.denied('The fixture inference slot is busy.', kind='busy', stage=stage,
                                      model=name, inference_started=False)
            if stage == 'conversation':
                model = WireConversation(name, lambda value, packet: value.update(
                    intent='workspace', facts=[],
                    workspace_request={'operation': 'read', 'scope': 'workspace', 'paths': []}))
            else:
                model = WireModel(name)
                if stage == 'subagent':
                    def checkpoint(raw, packet):
                        continuing = packet['child_slice']['ordinal'] == 1
                        raw['slice_status'] = 'continue' if continuing else 'complete'
                        raw['remaining'] = (['Inspect the remaining source in ' + next(iter(packet['excerpts']))]
                                            if continuing else [])
                    model.after = checkpoint
            original_call = model.call

            def call(cid, system, packet, schema):
                log(storage, 'model_started', stage=stage, model=model.model, call_id=cid,
                    branch=(packet.get('branch') or {}).get('id'),
                    thinking=os.environ.get('SUTRA_THINKING'), child_thinking=os.environ.get('SUTRA_CHILD_THINKING'),
                    goal=packet['contract']['goal'], constraints=packet['contract']['constraints'],
                    parent_notes=len(packet.get('child_results', [])),
                    child_notes=len(packet.get('child_checkpoint', [])),
                    memory_has_pin='CED724' in canonical(packet.get('memory')))
                while controls(storage).get('block_stage') == stage:
                    time.sleep(.02)
                result, record = original_call(cid, system, packet, schema)
                log(storage, 'model_returned', stage=stage, model=model.model, call_id=cid)
                return result, record

            model.call = call
            return model

        return original_handle(store, meta, ws, root, request, providers=provider)

    def logged_handle(root, ws, request):
        log(storage, 'bridge_started', request=copy.deepcopy(request))
        wait_control(storage, 'hold_bridge')
        result = original_bridge(root, ws, request)
        if controls(storage).get('corrupt_continuation') and result.get('continuation'):
            result = copy.deepcopy(result)
            result['continuation']['binding'] = dict(result.get('binding') or {}, contract_hash='0' * 64)
        log(storage, 'bridge_returned', event=request.get('event'), state=result.get('state'),
            flow_id=result.get('flow_id'), cursor=result.get('cursor'),
            continuation=result.get('continuation'))
        return result

    flow.handle = with_models
    bridge.handle = logged_handle
    monitoring.resource_state = hardware
    runtime_memory.recover = recover
    task_continuation._pending_call = pending
    task_continuation._model_ready = model_ready


def seed(workspace, storage):
    workspace.mkdir(parents=True, exist_ok=True)
    storage.mkdir(parents=True, exist_ok=True)
    for index in range(4):
        (workspace / f'part{index}.py').write_text(
            f'def part{index}():\n' +
            ''.join(f'    # Cedar CED724 source {line:03}: retain each authorized source continuation.\n'
                    for line in range(80)) + f'    return {index}\n')
    request = {'op': 'orchestrate', 'event': 'start', 'prompt': PROMPT,
               'models': MODELS, 'tier': 'low', 'armed': True, 'chat_key': CHAT}
    for _ in range(30):
        result = bridge.handle(storage, workspace, request)
        if result['state'] == 'CHILDREN':
            break
        request = {'op': 'orchestrate', 'event': 'step', 'flow_id': result['flow_id'],
                   'cursor': result['cursor'], 'binding': result.get('binding'),
                   'models': MODELS, 'tier': 'low', 'armed': True, 'chat_key': CHAT}
    else:
        raise RuntimeError('Controlled fixture did not reach child review')
    request.update(flow_id=result['flow_id'], cursor=result['cursor'], binding=result.get('binding'), event='step')
    (storage / 'runner-events.ndjson').write_text('')
    return {'request': request, 'flow': result, 'observation': observe(storage)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--storage', type=Path, required=True)
    parser.add_argument('--request-stream', action='store_true')
    parser.add_argument('--seed', action='store_true')
    parser.add_argument('--observe', action='store_true')
    args = parser.parse_args()
    workspace, storage = args.workspace.resolve(), args.storage.resolve()
    if args.observe:
        print(json.dumps(observe(storage)))
        return
    install_fixture(workspace, storage)
    if args.seed:
        print(json.dumps(seed(workspace, storage)))
        return
    bridge.request_stream(storage, workspace)


if __name__ == '__main__':
    main()
