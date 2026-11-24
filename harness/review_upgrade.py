"""Explicit release handoffs for eligible saved read-only reviews."""
from contextlib import contextmanager
import json
from pathlib import Path

from context_manager import InvalidUpdate, canonical, sha

PREDECESSOR = 'd58edf88efad29d2dfc1d81a9c2cb02260a6b6f22795efeb578f7cdaa2a41c9c'
SYNTHESIS_PREDECESSOR = 'df19c45e777027c01f34d13dbd3c6e46d149bab6a3bbcfb735479f468480299f'
COMPACT_WRITER_PREDECESSOR = 'a57157dbafb20e77c3b4821fc12d53c99611c4e5de6a9c1eb3a76e8146c0a500'
SUMMARY_REPAIR_PREDECESSOR = '1159af17874e347e75644c6d4583dcebb9db212cab69852e009638b3d1e3c43a'
SUMMARY_PROJECTION_PREDECESSOR = 'a0d9d8e3c9660ce67a34333c097795c398591e6ced9ffa1ad4083437aa94b306'
CONTEXT_PROFILE_PREDECESSOR = '71f4db9dbce5d9152ab2800b402754b4b28cce34b871ac5b8a8c4c705b6eba8f'
REVIEWER_ADMISSION_PREDECESSOR = 'dbcab6c395d6849875185d46ee1ac1366ee6b2ca200a7f9260bf01c35e4ed9c5'
REMOTE_CAPACITY_PREDECESSOR = 'c35efdc63a1a8b188c67af4e6384fc7463c0ed7c5df4dea7624e240f4f6f7091'  # 0.57.8
SECTION_SYNTHESIS_PREDECESSOR = '414d200909aabe0c1b1debf9bea80649f15d2b247a6f0c745e96205839e105bb'  # 0.58.1
SECTION_GATE_PREDECESSOR = '046ceb82feb422b203404bfc112da350df6c6814c54f1dfaf49c373b3ff195e3'  # 0.59.2
# Released policies with the original writer and evidence receipts needed to
# recover a closed output-limit result. Other upgrade paths do not use these.
PARENT_OUTPUT_LIMIT_PREDECESSORS = frozenset({
    '082a0b509f9c9ca82724e1eef65b1c82c170958a6a6ff6e16abbacce1ff7278d',  # 0.57.3
    '6b9fea351001160a1676fdf39ac30403f61cc3d29656dc3d5b9be91573b4e91e',  # 0.57.4
})
RESULT_ROLES = {'architect', 'subagent', 'chat_reviewer', 'report_reviewer'}
REPOSITORY_KINDS = {'catalog', 'list', 'read', 'search', 'static_map', 'api_extract'}
_MANIFEST = Path(__file__).with_name('review-upgrade.json')


def release_pair(s):
    """Authorize only the exact packaged successor, never an arbitrary later patch."""
    try:
        value = json.loads(_MANIFEST.read_text())
    except (OSError, ValueError):
        return False
    return value == {'version': 1, 'previous': PREDECESSOR, 'current': s.logic_hash}


def _scope(s, fid, data, predecessor=PREDECESSOR, *, target=None, require_join=True):
    import orchestration as machine
    root = data.get('root_node')
    if not root or (predecessor != s.logic_hash and not release_pair(s)):
        return None
    owner = machine.node(s, root)
    flow = machine.node(s, fid)
    if (owner.get('kind') != 'execution' or owner.get('flow_id') != fid
            or owner.get('previous') or data.get('result_parent') not in (None, root)):
        return None
    sid = owner['session']
    old = owner['binding']
    current = machine.binding(s, sid)
    task = s.snapshot(sid)
    constraints = task['constraints']
    if (old != dict(current, logic_hash=predecessor)
            or data.get('binding') != {key: value for key, value in current.items() if key != 'logic_hash'}
            or not (constraints.get('auto_review') is True and constraints.get('review_only') is True)
            or any(constraints.get(key) is not False for key in (
                'edits_allowed', 'tests_allowed', 'network_allowed', 'dependencies_allowed'))
            or owner['permissions'] != machine.permissions(s, sid)
            or owner['context'].get('request') != flow.get('prompt')):
        return None
    joined = next((event for event in reversed(machine.events(s, root)) if event['kind'] == 'JOIN'), None)
    if require_join:
        if not joined or not data.get('child_results') or joined['data']['invocations'] != data['child_results']:
            return None
    elif joined or data.get('child_results') or data.get('children'):
        return None
    parent = machine.node(s, owner['parent_id'])
    if parent.get('kind') != 'task' or parent.get('binding') != old:
        return None
    return {'version': 1, 'flow_id': fid, 'root': root, 'parent': parent['id'],
            'workspace': flow['workspace'], 'previous': old,
            'current': dict(current, logic_hash=target) if target else current,
            'join_receipt': joined['receipt'] if joined else None}


def parent_origin(s, fid, data, *, target=None):
    """Authorize a closed root's artifacts, including its recorded 0.57.1 handoff."""
    import orchestration as machine
    if (machine.state(s, fid) != 'DONE' or not data.get('root_node')
            or not data.get('git_provenance')):
        return None
    root = machine.node(s, data['root_node'])
    logic = root['binding']['logic_hash']
    if logic not in PARENT_OUTPUT_LIMIT_PREDECESSORS | {s.logic_hash, PREDECESSOR, SYNTHESIS_PREDECESSOR}:
        return None
    stamp = _scope(s, fid, data, logic, target=target)
    if not stamp or machine.state(s, root['id']) != 'DONE':
        return None
    if logic in PARENT_OUTPUT_LIMIT_PREDECESSORS:
        terminal = machine.events(s, fid)[-1]
        ended = terminal['data']
        if terminal['kind'] != 'WORKFLOW_FINISHED' or ended.get('observation_delivery') != 'withheld':
            return None
        invocation_id = ended.get('invocation_id')
        if not invocation_id or s.db.execute(
                'SELECT 1 FROM invocation_quarantine WHERE invocation_id=?', (invocation_id,)).fetchone():
            return None
        from chat_runtime import _parent_output_failure
        try:
            envelope, _, _ = _parent_output_failure(s, root['session'], invocation_id)
        except InvalidUpdate:
            return None
        if (envelope['logic_hash'] != logic
                or (envelope.get('orchestration') or {}).get('node_id') != root['id']):
            return None
    policies = [logic]
    if logic == PREDECESSOR:
        adopted = _scope(s, fid, data, PREDECESSOR, target=SYNTHESIS_PREDECESSOR)
        if data.get('review_policy_upgrade') != adopted:
            return None
        policies.append(SYNTHESIS_PREDECESSOR)
    return dict(stamp, artifact_policies=policies)


def section_origin(s, fid, data, *, target=None):
    """Identify a closed modern writer by its immutable output-limit receipts.

    This handoff opens no allowance and admits no partial answer. The section
    planner revalidates the retained source and creates an immutable plan under
    this narrowly scoped policy authorization before continuation is recorded.
    """
    import orchestration as machine
    from chat_runtime import _check_compact_output, _parent_output_receipt
    root_id = data.get('root_node')
    if (not root_id or machine.state(s, fid) != 'DONE'
            or machine.state(s, root_id) != 'DONE'
            or machine.node(s, fid).get('parent_synthesis_predecessor')):
        return None
    root = machine.node(s, root_id)
    logic = root['binding']['logic_hash']
    if logic not in {s.logic_hash, SECTION_SYNTHESIS_PREDECESSOR}:
        return None
    joined = bool(data.get('child_results'))
    stamp = _scope(s, fid, data, logic, target=target, require_join=joined)
    if not stamp:
        return None
    if joined:
        try:
            children = machine.fork_children(s, root_id)
        except InvalidUpdate:
            return None
        if children != data.get('children') or any(machine.state(s, child) != 'DONE' for child in children):
            return None
    elif s.db.execute('SELECT 1 FROM orchestration_nodes WHERE parent_id=? LIMIT 1', (root_id,)).fetchone():
        return None
    ended = machine.events(s, fid)[-1]
    if (ended['kind'] != 'WORKFLOW_FINISHED'
            or ended['data'].get('observation_delivery') != 'withheld'):
        return None
    invocation_id = ended['data'].get('invocation_id')
    try:
        envelope, packet, receipt, receipt_id = _parent_output_receipt(
            s, root['session'], invocation_id)
        if (packet.get('parent_output_policy_version') != 1
                or receipt.get('failure_kind') != 'output_limit' or receipt.get('proposal') is not None
                or envelope['logic_hash'] != logic
                or (envelope.get('orchestration') or {}).get('node_id') != root_id
                or packet.get('request') != machine.node(s, fid)['prompt']
                or packet.get('workspace') != stamp['workspace']
                or (packet.get('parent_repair') or {}).get('owner') != root_id
                or [item['invocation_id'] for item in packet.get('child_results', ())]
                    != data.get('child_results', [])):
            return None
        original_id = packet.get('parent_output_compact')
        original_receipt = None
        if original_id:
            _check_compact_output(s, root['session'], packet)
            original_env, _, _, original_receipt = _parent_output_receipt(s, root['session'], original_id)
            if original_env['logic_hash'] != logic:
                return None
        for iid in (invocation_id, original_id):
            if iid and s.db.execute('SELECT 1 FROM invocation_quarantine WHERE invocation_id=?', (iid,)).fetchone():
                return None
    except (InvalidUpdate, KeyError, TypeError):
        return None
    return dict(stamp, artifact_policies=[logic], section_output_invocation=invocation_id,
                section_output_receipt=receipt_id, section_original_invocation=original_id,
                section_original_receipt=original_receipt)


def prior_synthesis(s, fid, data):
    """Verify a single, closed continuation under its exact historical policy."""
    import orchestration as machine
    flow = machine.node(s, fid)
    link = flow.get('parent_synthesis_predecessor')
    if (not link or not data.get('root_node') or machine.state(s, fid) != 'DONE'
            or machine.state(s, data['root_node']) != 'DONE'
            or machine.node(s, link['flow_id']).get('parent_synthesis_predecessor')):
        return None
    logic = machine.node(s, data['root_node'])['binding']['logic_hash']
    if logic != s.logic_hash and (logic != COMPACT_WRITER_PREDECESSOR or not release_pair(s)):
        return None
    expected = _continued(s, fid, data, link, target=logic)
    return expected if expected and data.get('review_policy_upgrade') == expected else None


def _continued(s, fid, data, link, *, target=None):
    import orchestration as machine
    from orchestration_flow import info
    old_flow, old_data = info(s, link['flow_id'])
    section = link.get('profile') == 'section_synthesis_v1'
    if section:
        origin = section_origin(s, old_flow['id'], old_data, target=target)
        if (not origin or link.get('failed_invocation') != origin['section_output_invocation']):
            return None
        from section_synthesis import make_plan
        try:
            with use(s, origin):
                expected_plan = make_plan(s, origin['current']['session'], origin['section_output_invocation'],
                                          original_id=origin['section_original_invocation'],
                                          version=(data.get('section_plan') or {}).get('version'))
        except InvalidUpdate:
            return None
        if data.get('section_plan') != expected_plan:
            return None
    elif old_flow.get('parent_synthesis_predecessor'):
        prior = prior_synthesis(s, old_flow['id'], old_data) or closed_summary_repair(s, old_flow['id'], old_data)
        if not prior:
            return None
        _, original = info(s, prior['origin_flow_id'])
        origin = parent_origin(s, prior['origin_flow_id'], original, target=target)
    else:
        origin = parent_origin(s, old_flow['id'], old_data, target=target)
    old_root = old_data.get('root_node')
    if not origin or old_root != link['execution_id'] or not data.get('root_node'):
        return None
    root = machine.node(s, data['root_node'])
    previous = machine.node(s, old_root)
    if (root['flow_id'] != fid or root['previous'] != old_root
            or root['parent_id'] != origin['parent'] or root['binding'] != origin['current']
            or root['permissions'] != previous['permissions'] or root['context'] != previous['context']
            or data.get('result_parent') != origin['root']
            or data.get('binding') != old_data.get('binding')
            or data.get('git_provenance') != old_data.get('git_provenance')
            or data.get('child_results') != old_data.get('child_results')):
        return None
    return dict(origin, flow_id=fid, origin_flow_id=origin['flow_id'], active_root=root['id'])


def _active_continuation(s, fid, data, *, target=None):
    """Verify an exact provisional runtime's link before moving its active writer."""
    import orchestration as machine
    link = machine.node(s, fid).get('parent_synthesis_predecessor')
    if not link or not data.get('root_node') or not release_pair(s):
        return None
    active = machine.node(s, data['root_node'])
    if active['binding']['logic_hash'] != SUMMARY_REPAIR_PREDECESSOR:
        return None
    expected = _continued(s, fid, data, link, target=SUMMARY_REPAIR_PREDECESSOR)
    recorded = next((event['data'].get('review_policy_upgrade') for event in machine.events(s, fid)
                     if event['kind'] == 'PARENT_REPORT_CONTINUATION'), None)
    if not expected or expected != recorded or not (data.get('parent_output_retry') or {}).get('repair_invocation'):
        return None
    current = machine.binding(s, active['session'])
    return dict(expected, active_previous=active['binding'],
                current=dict(current, logic_hash=target) if target else current)


def _section_gate_scope(s, fid, data, *, stop_receipt=None):
    """Resume a linked report stopped before its claim-review reservation."""
    import orchestration as machine
    from section_synthesis import ROLES, _stage_rows
    flow = machine.node(s, fid)
    link = flow.get('parent_synthesis_predecessor') or {}
    plan = data.get('section_plan') or {}
    if (link.get('profile') != 'section_synthesis_v1' or plan.get('version') != 2
            or not data.get('root_node') or not release_pair(s)):
        return None
    active = machine.node(s, data['root_node'])
    if (active['binding']['logic_hash'] != SECTION_GATE_PREDECESSOR
            or active.get('kind') != 'execution'
            or (not stop_receipt and machine.state(s, active['id']) != 'READY')):
        return None
    history = machine.events(s, fid)
    stop = next((event for event in reversed(history) if (
        event['receipt'] == stop_receipt if stop_receipt else
        event['to'] == 'PAUSED' and event['from'] != 'PAUSED')), None)
    if not stop or (stop['kind'], stop['from'], stop['to']) != ('RESOURCE_STOPPED', 'SYNTHESIS', 'PAUSED'):
        return None
    details = stop['data'].get('workflow_stop') or {}
    measurement = details.get('measurement') or {}
    runtime = measurement.get('runtime') or {}
    if ((details.get('status'), details.get('resume_phase'), details.get('blocker'))
            != ('resource_wait', 'SYNTHESIS', 'measurement_unavailable')
            or details.get('automatic_retry') is not False
            or measurement.get('profile') != 'physical-context-v1'
            or measurement.get('provider') != 'sutra_remote'
            or measurement.get('admitted') is not False or measurement.get('available') is not False
            or runtime.get('stage') != 'remote_section_reasoning' or runtime.get('inference_started') is not False
            or runtime.get('kind') != 'remote_observation' or runtime.get('model') != plan.get('model')
            or measurement.get('model') != plan.get('model')):
        return None
    original = next((event['data'].get('review_policy_upgrade') for event in history
                     if event['kind'] == 'PARENT_REPORT_CONTINUATION' and event['at'] <= stop['at']), None)
    expected = _continued(s, fid, data, link, target=SECTION_GATE_PREDECESSOR)
    if (not expected or expected != original or flow['workspace'] != expected['workspace']
            or flow['prompt'] != active['context']['request']
            or flow.get('chat_key') != machine.node(s, link['flow_id']).get('chat_key')):
        return None
    sid = active['session']
    eligible = {}
    try:
        entries = _stage_rows(s, sid, fid, plan)
    except (InvalidUpdate, KeyError, TypeError):
        return None
    accepted_ids = {iid: receipt for stages in entries.values() for iid, receipt, status in stages
                    if status == 'accepted'}
    for iid, raw in s.db.execute(
            "SELECT id,record_json FROM invocations WHERE session=? AND "
            "json_extract(record_json,'$.orchestration.node_id')=? AND "
            "json_extract(record_json,'$.logic_hash')=?", (sid, active['id'], SECTION_GATE_PREDECESSOR)):
        env = json.loads(raw)
        if (iid in accepted_ids and env.get('role') in ROLES and env['created_at'] <= stop['at']
                and not s.db.execute('SELECT 1 FROM invocation_quarantine WHERE invocation_id=?', (iid,)).fetchone()):
            eligible[iid] = accepted_ids[iid]
    if not eligible:
        return None
    return dict(expected, active_previous=active['binding'], current=machine.binding(s, sid),
                section_gate_stop_receipt=stop['receipt'], section_gate_results=eligible)


def closed_summary_repair(s, fid, data):
    """Recover format-only writer work across the previously admitted runtime handoff."""
    import orchestration as machine
    if (not data.get('root_node') or machine.state(s, fid) != 'DONE'
            or machine.state(s, data['root_node']) != 'DONE'):
        return None
    stamp = _active_continuation(s, fid, data, target=SUMMARY_PROJECTION_PREDECESSOR)
    return stamp if stamp and data.get('review_policy_upgrade') == stamp else None


def _context_profile_scope(s, fid, data, *, stop_receipt=None):
    import orchestration as machine
    stop = next((event for event in reversed(machine.events(s, fid))
                 if (event['receipt'] == stop_receipt if stop_receipt else
                     event['to'] == 'PAUSED' and event['from'] != 'PAUSED')), None)
    if not stop or stop['from'] != 'SYNTHESIS' or stop['to'] != 'PAUSED':
        return None
    details = stop['data'].get('workflow_stop') or {}
    context_limit = stop['kind'] == 'CONTEXT_STOPPED' and details.get('status') == 'context_budget'
    retained_limit = (stop['kind'] == 'MEMORY_STOPPED' and details.get('status') == 'memory_fault'
                      and (details.get('memory_fault') or {}).get('code') == 'capacity')
    if not (context_limit or retained_limit):
        return None
    stamp = _scope(s, fid, data, CONTEXT_PROFILE_PREDECESSOR)
    return dict(stamp, context_stop_receipt=stop['receipt']) if stamp else None


def _reviewer_admission_scope(s, fid, data, *, stop_receipt=None):
    """Resume the exact joined draft that waited for its first claim reviewer."""
    import orchestration as machine
    point = data.get('parent_checkpoint') or {}
    candidate = point.get('candidate_id')
    if not data.get('git_provenance') or point.get('version') != 1 or not candidate:
        return None
    stamp = _scope(s, fid, data, REVIEWER_ADMISSION_PREDECESSOR)
    if not stamp:
        return None
    stop = next((event for event in reversed(machine.events(s, fid))
                 if (event['receipt'] == stop_receipt if stop_receipt else
                     event['to'] == 'PAUSED' and event['from'] != 'PAUSED')), None)
    if not stop or (stop['kind'], stop['from'], stop['to']) != ('RESOURCE_STOPPED', 'SYNTHESIS', 'PAUSED'):
        return None
    details = stop['data'].get('workflow_stop') or {}
    if (details.get('status'), details.get('resume_phase'), details.get('blocker')) != (
            'resource_wait', 'SYNTHESIS', 'model_admission'):
        return None
    sid = stamp['current']['session']
    row = s.db.execute("""SELECT i.record_json,r.record_json FROM invocations i
        JOIN invocation_receipts r ON r.invocation_id=i.id
        WHERE i.id=? AND i.session=?""", (candidate, sid)).fetchone()
    if not row or s.db.execute(
            'SELECT 1 FROM invocation_quarantine WHERE invocation_id=?', (candidate,)).fetchone():
        return None
    envelope, receipt = map(json.loads, row)
    if (envelope.get('logic_hash') != REVIEWER_ADMISSION_PREDECESSOR
            or receipt.get('logic_hash') != REVIEWER_ADMISSION_PREDECESSOR
            or envelope.get('role') != 'chat_reviewer'
            or (envelope.get('orchestration') or {}).get('node_id') != stamp['root']
            or envelope.get('payload_hash') != point.get('packet_hash')
            or receipt.get('status') != 'accepted' or receipt.get('response_hash') != point.get('candidate_hash')):
        return None
    if s.db.execute("""SELECT 1 FROM invocations i JOIN deliveries d ON d.id=i.delivery_id
        WHERE i.session=? AND json_extract(i.record_json,'$.role')='report_reviewer'
          AND json_extract(i.record_json,'$.logic_hash')=?
          AND json_extract(d.payload_json,'$.candidate_id')=? LIMIT 1""",
                    (sid, REVIEWER_ADMISSION_PREDECESSOR, candidate)).fetchone():
        return None
    return dict(stamp, claim_wait_stop_receipt=stop['receipt'])


def _remote_capacity_scope(s, fid, data, *, stop_receipt=None, source_receipt=None):
    """Carry a pre-dispatch 0.57.8 review into a fresh provider check."""
    import orchestration as machine
    from remote_profiles import is_remote
    stamp = _scope(s, fid, data, REMOTE_CAPACITY_PREDECESSOR, require_join=False)
    if not stamp:
        return None
    stop = next((event for event in reversed(machine.events(s, fid))
                 if (event['receipt'] == stop_receipt if stop_receipt else
                     event['to'] == 'PAUSED' and event['from'] != 'PAUSED')), None)
    if not stop or (stop['kind'], stop['from'], stop['to']) != ('RESOURCE_STOPPED', 'SYNTHESIS', 'PAUSED'):
        return None
    details = stop['data'].get('workflow_stop') or {}
    state = details.get('measurement') or {}
    runtime = state.get('runtime') or {}
    plan = runtime.get('plan') or {}
    if ((details.get('status'), details.get('resume_phase'), details.get('blocker')) !=
            ('resource_wait', 'SYNTHESIS', 'model_admission')
            or state.get('profile') != 'remote-nvidia-v1' or state.get('provider') != 'remote_llama'
            or state.get('available') is not True or state.get('admitted') is not False
            or state.get('decision') not in ('BRIDGE', 'FIN')
            or runtime.get('kind') != 'model_capacity' or runtime.get('inference_started') is not False
            or runtime.get('stage') not in ('remote_admission', 'remote_dispatch')
            or not is_remote(runtime.get('model')) or plan.get('model') != runtime['model']
            or not plan.get('instance') or plan.get('decision') != state['decision']):
        return None
    root = stamp['root']
    if (s.db.execute('SELECT 1 FROM orchestration_nodes WHERE parent_id=? LIMIT 1', (root,)).fetchone()
            or s.db.execute("SELECT 1 FROM invocations WHERE session=? "
                "AND json_extract(record_json,'$.orchestration.node_id')=? "
                "AND json_extract(record_json,'$.logic_hash')=? LIMIT 1",
                (stamp['current']['session'], root, REMOTE_CAPACITY_PREDECESSOR)).fetchone()):
        return None
    result = dict(stamp, remote_capacity_stop_receipt=stop['receipt'])
    if not data.get('git_provenance') or source_receipt:
        # Some read-only routes did not take a Git snapshot. Their exact paid
        # synthesis preparation supplies the source identity for this handoff.
        node = machine.node(s, root)
        prepared = next((event for event in reversed(machine.events(s, root))
                         if event['kind'] == 'REVIEW_SOURCE_PREPARED' and event['at'] <= stop['at']
                         and (source_receipt is None or event['receipt'] == source_receipt)), None)
        proof = prepared['data'] if prepared else {}
        binding = {'workspace': stamp['workspace'], 'owner': root, 'binding': stamp['previous'],
                   'context_hash': node['context_hash'], 'purpose': 'workspace_synthesis'}
        if any(proof.get(k) != v for k, v in binding.items()) or not proof.get('receipts'):
            return None
        result['remote_capacity_source_receipt'] = prepared['receipt']
    return result


def offer(s, fid, data):
    import orchestration as machine
    stop = data.get('workflow_stop') or {}
    if machine.state(s, fid) != 'PAUSED':
        return None
    if stop.get('status') == 'resource_wait':
        stamp = _section_gate_scope(s, fid, data) or _remote_capacity_scope(s, fid, data)
        if stamp:
            return stamp
    if not data.get('git_provenance'):
        return None
    if stop.get('status') in ('context_budget', 'memory_fault'):
        return _context_profile_scope(s, fid, data)
    if (stop.get('resume_phase') != 'SYNTHESIS'
            or stop.get('status') not in ('resource_wait', 'claim_review_output_limit')):
        return None
    linked = _active_continuation(s, fid, data)
    if linked and (machine.state(s, linked['active_root']) == 'READY'
                   and not s.db.execute('SELECT 1 FROM orchestration_charges WHERE node_id=? LIMIT 1',
                                        (linked['active_root'],)).fetchone()):
        return linked
    return _scope(s, fid, data) or _reviewer_admission_scope(s, fid, data)


def recorded(s, fid, data=None):
    if not fid:
        return None
    if data is None:
        row = s.db.execute(
            "SELECT 1 FROM orchestration_events WHERE node_id=? "
            "AND json_extract(record_json,'$.kind') IN ('REVIEW_POLICY_UPGRADED','PARENT_REPORT_CONTINUATION') LIMIT 1",
            (fid,)).fetchone()
        if not row:
            return None
        from orchestration_flow import info
        data = info(s, fid)[1]
    stamp = data.get('review_policy_upgrade')
    if not stamp:
        return None
    import orchestration as machine
    predecessor = machine.node(s, fid).get('parent_synthesis_predecessor')
    if stamp.get('context_stop_receipt') and not predecessor:
        expected = _context_profile_scope(s, fid, data, stop_receipt=stamp['context_stop_receipt'])
    elif stamp.get('claim_wait_stop_receipt') and not predecessor:
        expected = _reviewer_admission_scope(s, fid, data, stop_receipt=stamp['claim_wait_stop_receipt'])
    elif stamp.get('remote_capacity_stop_receipt') and not predecessor:
        expected = _remote_capacity_scope(s, fid, data, stop_receipt=stamp['remote_capacity_stop_receipt'],
                                          source_receipt=stamp.get('remote_capacity_source_receipt'))
    elif stamp.get('section_gate_stop_receipt') and predecessor:
        expected = _section_gate_scope(s, fid, data, stop_receipt=stamp['section_gate_stop_receipt'])
    elif stamp.get('active_previous'):
        expected = _active_continuation(s, fid, data)
    else:
        expected = _continued(s, fid, data, predecessor) if predecessor else _scope(s, fid, data)
    if stamp == expected:
        return stamp
    return None


@contextmanager
def use(s, stamp):
    previous = getattr(s, '_review_upgrade', None)
    s._review_upgrade = stamp
    try:
        yield
    finally:
        s._review_upgrade = previous


def _active(s, sid):
    stamp = getattr(s, '_review_upgrade', None)
    return stamp if stamp and stamp['current']['session'] == sid else None


def conversation_policy(s, handoff):
    """Keep the original policy and chat axis when rebuilding the saved input."""
    stamp = getattr(s, '_review_upgrade', None)
    if not stamp:
        return None
    policy = stamp['previous']['logic_hash']
    if not (stamp.get('context_stop_receipt') or stamp.get('claim_wait_stop_receipt') or stamp.get('remote_capacity_stop_receipt')
            or stamp.get('section_output_receipt')
            or policy in PARENT_OUTPUT_LIMIT_PREDECESSORS):
        return None
    import orchestration as machine
    owner = machine.node(s, stamp['root'])
    if owner['context'].get('conversation_id') != handoff:
        return None
    from conversation import chat_axis
    flow = machine.node(s, stamp['flow_id'])
    axis = chat_axis(stamp['workspace'], flow.get('chat_key'))
    return (axis, policy) if axis else None


def _owner(s, stamp, nid):
    if nid in (stamp['root'], stamp['parent']):
        return True
    if not nid:
        return False
    import orchestration as machine
    node = machine.node(s, nid)
    return (node.get('kind') == 'branch' and node.get('parent_id') == stamp['root']
            and node.get('binding') == stamp['previous'])


def node_policy(s, node):
    stamp = _active(s, node['session'])
    return bool(stamp and ((node.get('binding') == stamp['previous'] and _owner(s, stamp, node['id']))
                or (node['id'] == stamp.get('active_root') and node.get('binding') == stamp.get('active_previous'))))


def result_policy(s, session, receipt, envelope):
    logic = receipt['logic_hash']
    if logic != envelope['logic_hash']:
        return False
    if logic == s.logic_hash:
        return True
    stamp = _active(s, session)
    if not stamp:
        return False
    owner = (envelope.get('orchestration') or {}).get('node_id')
    if (stamp.get('section_gate_stop_receipt') and logic == stamp['active_previous']['logic_hash']
            and owner == stamp['active_root']):
        from section_synthesis import ROLES
        return (envelope['role'] in ROLES and receipt.get('status') == 'accepted'
                and stamp['section_gate_results'].get(envelope['id'])
                    == sha('invocation-receipt-v1:' + canonical(receipt)))
    if logic not in stamp.get('artifact_policies', [stamp['previous']['logic_hash']]) or envelope['role'] not in RESULT_ROLES:
        return False
    if envelope['role'] == 'subagent':
        return owner not in (stamp['root'], stamp['parent']) and _owner(s, stamp, owner)
    return owner == stamp['root']


def repository_policy(s, session, operation):
    if operation['logic_hash'] == s.logic_hash:
        return True
    stamp = _active(s, session)
    if (not stamp or operation['logic_hash'] not in stamp.get('artifact_policies', [stamp['previous']['logic_hash']])
            or operation['kind'] not in REPOSITORY_KINDS):
        return False
    owner = (operation.get('orchestration') or {}).get('node_id')
    return _owner(s, stamp, owner) and (owner != stamp['parent'] or operation['kind'] == 'catalog')


def confirm(s, stamp, data, *, closed=False):
    """Use the existing ledger and fresh validators before adopting old artifacts."""
    import orchestration as machine
    import repository_tools
    from accounting_audit import check_admission
    from execution_sessions import idle
    sid = stamp['current']['session']
    idle(s, stamp['root'])
    if stamp.get('section_gate_stop_receipt'):
        idle(s, stamp['active_root'])
        if machine.state(s, stamp['active_root']) != 'READY':
            raise InvalidUpdate('Saved report section execution is not ready to resume')
    check_admission(s)
    if not s.audit_valid(deep=True) or not s.verify_invocations(sid):
        raise InvalidUpdate('Saved review audit or invocation history failed verification')
    repository_tools.records(s, sid)
    if stamp.get('section_output_receipt'):
        from section_synthesis import make_plan, validate_plan
        plan = (data['section_plan'] if stamp.get('section_gate_stop_receipt') else
                make_plan(s, sid, stamp['section_output_invocation'], stamp['section_original_invocation']))
        validate_plan(s, sid, plan)
        for iid in stamp.get('section_gate_results', {}):
            s.load_result(sid, iid)
    if stamp.get('remote_capacity_source_receipt'):
        from static_runtime import load_partition
        prepared = next(event['data'] for event in machine.events(s, stamp['root'])
                        if event['receipt'] == stamp['remote_capacity_source_receipt'])
        partition = load_partition(s, sid, Path(stamp['workspace']), prepared['receipts'], stamp['root'])
        if set(partition['sources']) != set(prepared['page_plan']['selected']):
            raise InvalidUpdate('Saved remote review source preparation changed')
    for iid in data.get('child_results', ()):
        s.load_result(sid, iid)
    checkpoint = data.get('parent_checkpoint') or data.get('claim_review_retry')
    if checkpoint and checkpoint.get('candidate_id'):
        s.load_result(sid, checkpoint['candidate_id'])
    if stamp.get('context_stop_receipt') or stamp.get('remote_capacity_stop_receipt') or stamp.get('section_output_receipt'):
        origin = machine.node(s, stamp['root'])['context']
        if origin.get('conversation_id'):
            from conversation import workspace_handoff
            workspace_handoff(s, sid, origin['conversation_id'], origin['request'])
        recall = data.get('context_recall')
        if recall:
            from retained_context import _accepted_request, requests, validate_requests
            if not isinstance(recall, dict) or set(recall) != {'invocation_id', 'refs'}:
                raise InvalidUpdate('Invalid saved context request')
            env, packet, proposal = _accepted_request(s, sid, recall['invocation_id'])
            if (env['role'] != 'chat_reviewer'
                    or (env.get('orchestration') or {}).get('node_id') != stamp['root']
                    or requests(packet, proposal) != recall['refs']):
                raise InvalidUpdate('Saved context request differs from the accepted review')
            validate_requests(s, sid, packet, recall['refs'])
    if machine.state(s, stamp['root']) != ('DONE' if closed else 'READY'):
        raise InvalidUpdate('Saved review execution is not ready to resume')


def for_state(s, sid):
    """Find an already recorded handoff for the task display, without admitting work."""
    for (fid,) in s.db.execute(
            "SELECT node_id FROM orchestration_events "
            "WHERE json_extract(record_json,'$.kind') IN ('REVIEW_POLICY_UPGRADED','PARENT_REPORT_CONTINUATION') "
            "AND json_extract(record_json,'$.data.review_policy_upgrade.current.session')=? "
            "ORDER BY rowid DESC", (sid,)):
        stamp = recorded(s, fid)
        if stamp:
            return stamp
    return None
