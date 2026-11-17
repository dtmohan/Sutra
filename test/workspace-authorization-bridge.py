"""Run the real bridge with deterministic inference for the authorization journey."""
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'harness'))
from invocation_gateway import InvocationLedger
import orchestration
import orchestration_flow
import repository_tools
from paging import Budget
from review_budget import usage
from test_chat import Fake as Review
from test_conversation import Fake as Conversation, MODELS
from test_native_runtime import NativeModel
import vscode_bridge


def providers(tier):
    model = MODELS[tier]

    def classification(result, packet):
        if packet['request'].startswith('Review'):
            result.update(intent='workspace', facts=[packet['request'].split('. ', 1)[0] + '.'],
                          workspace_request={'operation': 'read', 'scope': 'workspace', 'paths': []})
        if packet['request'].startswith('Fix'):
            result.update(intent='workspace', workspace_request={
                'operation': 'edit', 'paths': ['src/retry.py']})

    def create(stage):
        if stage == 'conversation':
            return Conversation(model, classification)
        if stage == 'coder':
            return NativeModel(model, 'edit')
        if stage not in ('mapping', 'synthesis'):
            raise AssertionError('Unexpected fixture stage: ' + stage)
        review = Review(model)
        if stage == 'synthesis':
            review.budget = {
                'low': Budget(1400, 512, 256),
                'mid': Budget(6500, 768, 256),
                'high': Budget(32768, 4096, 256),
            }[tier]
        return review

    return create


def snapshot(root):
    """Observe the durable state without calling the extension or reading source."""
    ledger = InvocationLedger(root / 'state.sqlite')
    try:
        meta = json.loads((root / 'workspace.json').read_text())
        sid = meta['active']
        flows = [fid for fid, in ledger.db.execute(
            "SELECT id FROM orchestration_nodes WHERE json_extract(record_json,'$.kind')='flow' ORDER BY rowid")]
        flow_states = [orchestration_flow.response(ledger, fid) for fid in flows]
        packets = []
        rows = ledger.db.execute(
            'SELECT i.record_json,d.payload_json FROM invocations i '
            'JOIN deliveries d ON i.delivery_id=d.id WHERE i.session=? ORDER BY i.rowid', (sid,))
        for raw, payload in rows:
            record = json.loads(raw)
            packets.append(dict(role=record['role'], model=record['model'], packet=json.loads(payload)))
        # Distinguish work already spent on the structural inventory from
        # source work started by synthesis or by any subsequent recovery.
        # An unrelated conversation can complete while a bound review remains
        # paused. Its newer flow must not hide that review's source receipts.
        active_flow = next((flow['flow_id'] for flow in reversed(flow_states) if flow['binding']), None)
        synthesis_started = False
        synthesis_receipts = []
        source_events = []
        events = ledger.db.execute(
            "SELECT event,payload_json FROM page_audit WHERE event IN "
            "('ORCHESTRATION_TRANSITION','REPOSITORY_RECEIPT','SOURCE_PAGE_PLAN',"
            "'REVIEW_SOURCE_PREPARED','REVIEW_SOURCE_REUSED','REVIEW_SOURCE_CHANGED') ORDER BY seq"
        )
        for event, raw in events:
            record = json.loads(raw)
            if event == 'ORCHESTRATION_TRANSITION':
                if record['node_id'] == active_flow and record['state'] == 'SYNTHESIS':
                    synthesis_started = True
            elif event == 'REPOSITORY_RECEIPT' and synthesis_started:
                outcome = record['record']['result']
                if 'versions' in outcome:
                    synthesis_receipts.append({'id': record['id'], 'versions': outcome['versions']})
            elif event.startswith('REVIEW_SOURCE_') or event == 'SOURCE_PAGE_PLAN':
                source_events.append(event)
        return {'session': sid, 'contract': ledger.pinned(sid), 'budget': usage(ledger, sid),
                'synthesis_receipts': synthesis_receipts, 'source_events': source_events,
                'flows': flow_states,
                'packets': packets, 'valid': ledger.verify_invocations(sid) and ledger.audit_valid(),
                'amendments': [dict(receipt=digest, **json.loads(record)) for record, digest in ledger.db.execute(
                    'SELECT record_json,digest FROM allocation_amendments WHERE session=? ORDER BY version', (sid,))],
                'executions': [dict(id=nid, state=orchestration.state(ledger, nid),
                                    previous=json.loads(record).get('previous'),
                                    permissions=json.loads(record)['permissions'])
                               for nid, record in ledger.db.execute(
                                   "SELECT id,record_json FROM orchestration_nodes WHERE "
                                   "json_extract(record_json,'$.kind')='execution' ORDER BY rowid")]}
    finally:
        ledger.close()


if __name__ == '__main__':
    # Only the model provider is substituted. The bridge retains validation,
    # locking, recovery, file reads and the real budget/permission state machine.
    real = orchestration_flow.handle

    def with_fixture(ledger, meta, workspace, root, request):
        tier = request.get('tier', 'low') or 'low'
        flow_id = request.get('flow_id')
        synthesis = (request.get('event') == 'step' and flow_id
                     and orchestration.state(ledger, flow_id) == 'SYNTHESIS')
        if not synthesis:
            return real(ledger, meta, workspace, root, request, providers(tier))
        # Count actual scoped reads, including freshness checks that correctly
        # have no repository-operation charge.
        with patch.object(repository_tools, 'read_bytes', wraps=repository_tools.read_bytes) as reads:
            result = real(ledger, meta, workspace, root, request, providers(tier))
            result['_fixture_synthesis_reads'] = reads.call_count
        return result

    os.environ.pop('SUTRA_RUNTIME_DIR', None)
    if sys.argv[1] == 'snapshot':
        print(json.dumps(snapshot(Path(sys.argv[2]))))
    else:
        with patch('orchestration_flow.handle', side_effect=with_fixture):
            vscode_bridge.main()
