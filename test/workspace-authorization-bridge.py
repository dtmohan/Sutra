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
                          workspace_request={'operation': 'read', 'paths': []})
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
            review.budget = Budget(1400, 512, 256) if tier == 'low' else Budget(32768, 4096, 256)
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
        packets = []
        rows = ledger.db.execute(
            'SELECT i.record_json,d.payload_json FROM invocations i '
            'JOIN deliveries d ON i.delivery_id=d.id WHERE i.session=? ORDER BY i.rowid', (sid,))
        for raw, payload in rows:
            record = json.loads(raw)
            packets.append(dict(role=record['role'], model=record['model'], packet=json.loads(payload)))
        return {'session': sid, 'contract': ledger.pinned(sid), 'budget': usage(ledger, sid),
                'flows': [orchestration_flow.response(ledger, fid) for fid in flows],
                'packets': packets, 'valid': ledger.verify_invocations(sid) and ledger.audit_valid(),
                'executions': [dict(id=nid, state=orchestration.state(ledger, nid)) for nid, in ledger.db.execute(
                    "SELECT id FROM orchestration_nodes WHERE json_extract(record_json,'$.kind')='execution' ORDER BY rowid")]}
    finally:
        ledger.close()


if __name__ == '__main__':
    # Only the model provider is substituted. The bridge retains validation,
    # locking, recovery, file reads and the real budget/permission state machine.
    real = orchestration_flow.handle

    def with_fixture(ledger, meta, workspace, root, request):
        tier = request.get('tier', 'low') or 'low'
        return real(ledger, meta, workspace, root, request, providers(tier))

    os.environ.pop('SUTRA_RUNTIME_DIR', None)
    if sys.argv[1] == 'snapshot':
        print(json.dumps(snapshot(Path(sys.argv[2]))))
    else:
        with patch('orchestration_flow.handle', side_effect=with_fixture):
            vscode_bridge.main()
