"""Continue a closed, unpublished writer failure without reopening its ledger."""
import json

from context_manager import InvalidUpdate
import orchestration as machine
import review_upgrade


def origin(s, flow, data):
    if machine.state(s, flow['id']) != 'DONE' or not data.get('root_node'):
        return None
    final = machine.events(s, flow['id'])[-1]
    ended = final['data']
    if final['kind'] != 'WORKFLOW_FINISHED' or ended.get('observation_delivery') != 'withheld':
        return None
    iid = ended.get('invocation_id')
    row = s.db.execute(
        'SELECT i.record_json, r.record_json FROM invocations i JOIN invocation_receipts r '
        'ON r.invocation_id=i.id WHERE i.id=?', (iid,)).fetchone()
    if not row:
        return None
    envelope, receipt = map(json.loads, row)
    if (envelope['role'] != 'chat_reviewer' or receipt['status'] != 'quarantined'
            or (envelope.get('orchestration') or {}).get('node_id') != data['root_node']):
        return None
    from remote_profiles import is_remote
    if not is_remote(envelope['model']):
        return None
    if s.db.execute(
            "SELECT 1 FROM orchestration_nodes WHERE "
            "json_extract(record_json,'$.parent_synthesis_predecessor.flow_id')=? LIMIT 1",
            (flow['id'],)).fetchone():
        return None
    link = flow.get('parent_synthesis_predecessor')
    normalize = False
    if link:
        from chat_runtime import repairable_parent_output
        prior = review_upgrade.prior_synthesis(s, flow['id'], data)
        if prior is None:
            prior = review_upgrade.closed_summary_repair(s, flow['id'], data)
            normalize = prior is not None
        if (not prior or envelope['logic_hash'] != prior['current']['logic_hash']
                or not normalize and not repairable_parent_output(receipt)):
            return None
        from orchestration_flow import info
        _, original = info(s, prior['origin_flow_id'])
        stamp = review_upgrade.parent_origin(s, prior['origin_flow_id'], original)
        if normalize:
            saved = data['parent_output_retry']
            failed, repair = saved['failed_invocation'], saved['repair_invocation']
        else:
            failed, repair = link['failed_invocation'], iid
    else:
        stamp = review_upgrade.parent_origin(s, flow['id'], data)
        if not stamp or receipt.get('failure_kind') != 'output_limit' or envelope['logic_hash'] not in stamp['artifact_policies']:
            return None
        failed, repair = iid, None
    if not stamp:
        return None
    return stamp, failed, repair, normalize, iid


def linked_to(s, owner, original_id):
    """Identify an execution descended from its exact original writer owner."""
    previous = owner.get('previous')
    if previous == original_id:
        return True
    if not previous or owner.get('kind') != 'execution':
        return False
    prior = machine.node(s, previous)
    if (prior.get('previous') != original_id or prior.get('kind') != 'execution'
            or prior['session'] != owner['session'] or prior['parent_id'] != owner['parent_id']):
        return False
    from orchestration_flow import info
    _, data = info(s, prior['flow_id'])
    stamp = review_upgrade.prior_synthesis(s, prior['flow_id'], data)
    return (stamp is not None and stamp['root'] == original_id and stamp['active_root'] == previous)


def offer(s, flow, data):
    found = origin(s, flow, data)
    if not found:
        return None
    chat = flow.get('chat_key')
    info = {}
    if chat:
        from orchestration_flow import _latest_ask_flow
        from task_stack import view
        latest = _latest_ask_flow(s, flow['workspace'], chat)
        if not latest or latest['id'] != flow['id']:
            return None
        info['stack_cursor'] = view(s, flow['workspace'], chat)['cursor']
    return dict(info, kind='parent_output_recovery', restart_allowed=True, terminal_review=True,
                message=('Review the saved draft after correcting its host-generated summary. All spending '
                         'and permission limits are retained.' if found[3] else
                         'Continue the saved source-backed report in a new execution with the original '
                         'permissions, remaining attempts and consumed budget.'))


def continue_saved(s, meta, workspace, root, flow, data, request):
    from chat_runtime import parent_output_checkpoint, parent_summary_revalidation
    from execution_sessions import open_session
    from git_provenance import inspect, same_snapshot
    from orchestration_flow import identity, pending_flows, response, save

    approved = offer(s, flow, data)
    if not approved or request.get('confirmed') is not True or not request.get('armed'):
        raise InvalidUpdate('Continuing a withheld report requires its exact saved flow and explicit approval')
    stamp, original_failure, repair, normalize, terminal_invocation = origin(s, flow, data)
    sid = stamp['current']['session']
    if meta.get('active') != sid or request.get('binding') != identity(s, sid):
        raise InvalidUpdate('The saved report task, permissions or calibration changed')
    if not same_snapshot(data['git_provenance'], inspect(workspace)):
        raise InvalidUpdate('The saved report source snapshot changed')
    chat = flow.get('chat_key')
    if chat:
        if request.get('stack_cursor') != approved['stack_cursor']:
            raise InvalidUpdate('The Sutra task stack changed before continuation')
    elif pending_flows(s, workspace):
        raise InvalidUpdate('Another saved workflow owns this workspace')

    with review_upgrade.use(s, stamp):
        review_upgrade.confirm(s, stamp, data, closed=True)
        original_request = {'prompt': flow['prompt'], 'child_results': data['child_results'],
                            'conversation_id': data.get('conversation_id'),
                            'orchestration_parent': stamp['root']}
        if normalize:
            checkpoint = {'parent_summary_revalidation':
                              parent_summary_revalidation(s, sid, original_failure, repair)}
        else:
            options = {'repair_invocation': repair} if repair else {}
            checkpoint = {'parent_output_retry':
                              parent_output_checkpoint(s, sid, original_failure, stamp['root'], original_request, **options)}
        link = {'flow_id': flow['id'], 'execution_id': data['root_node'],
                'failed_invocation': terminal_invocation}
        with s.db:
            new = machine.create(s, 'flow', objective=flow['prompt'], workspace=str(workspace),
                                 prompt=flow['prompt'], conversation_parent=flow.get('conversation_parent'),
                                 chat_key=chat, parent_synthesis_predecessor=link)
            fid = new['id']
            inherited = {key: data[key] for key in
                         ('binding', 'child_results', 'children', 'conversation_id', 'git_provenance',
                          'source_roots', 'permission', 'allocation_version') if key in data}
            machine.transition(s, fid, 'PAUSED', 'PARENT_REPORT_LINKED', inherited)
            execution = open_session(s, sid, fid, previous=link['execution_id'], task_id=stamp['parent'])
            scope = dict(stamp, flow_id=fid, origin_flow_id=stamp['flow_id'], active_root=execution['id'])
            machine.transition(s, fid, 'SYNTHESIS', 'PARENT_REPORT_CONTINUATION', {
                'root_node': execution['id'], 'result_parent': stamp['root'],
                'review_policy_upgrade': scope, **checkpoint,
            })
            if chat:
                from task_stack import select
                select(s, workspace, chat, fid, expected_cursor=approved['stack_cursor'], attach=True)
                meta['_flow'] = fid
        save(root, meta)
        return response(s, fid, progress='Continuing report synthesis from the saved evidence and remaining allocation.')
