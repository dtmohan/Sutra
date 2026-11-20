"""Retry exact review evidence in its original, bounded execution."""
from pathlib import Path

from context_manager import InvalidUpdate, canonical, sha
import orchestration as machine
from source_paging import select
from static_analysis import StaticSourceChanged
import static_runtime as runtime


_PREPARED = 'REVIEW_SOURCE_PREPARED'


def _scope(s, sid, workspace, request, purpose):
    owner = machine.ACTIVE.get()
    node = machine.checked(s, owner) if owner else machine.task(s, sid)
    if node['session'] != sid:
        raise InvalidUpdate('Source preparation belongs to another task')
    if purpose not in ('workspace_synthesis', 'child_review'):
        raise InvalidUpdate('Unsupported review source purpose')
    if purpose == 'child_review' and node['kind'] != 'branch':
        raise InvalidUpdate('Child source preparation requires an active branch')
    # Direct, unrelated asks always compile their own snapshots.
    if not owner or node['kind'] == 'task':
        return node, None

    context = node['context']
    if sha(canonical(context)) != node['context_hash']:
        raise InvalidUpdate('Source preparation has a corrupt execution context')
    if (context['request'] != request.get('prompt') or
            context.get('conversation_id') != request.get('conversation_id')):
        raise InvalidUpdate('Source preparation differs from the committed review context')
    if node['kind'] != ('branch' if purpose == 'child_review' else 'execution'):
        raise InvalidUpdate('Source preparation differs from the committed execution role')
    if machine.state(s, owner) != 'READY':
        raise InvalidUpdate('Source preparation requires a ready execution node')
    lineage = {
        'request': request['prompt'], 'conversation_id': request.get('conversation_id'),
        'child_results': request.get('child_results') or [],
        'orchestration_parent': request.get('orchestration_parent'),
    }
    identity = {
        'workspace': str(Path(workspace).resolve()), 'owner': owner,
        'binding': node['binding'], 'context_hash': node['context_hash'],
        'lineage_hash': sha(canonical(lineage)), 'purpose': purpose,
    }
    return node, identity


def _audit(s, event, record, *, commit_node=False):
    with s.db:
        if commit_node:
            machine.transition(s, record['owner'], 'READY', event, record)
        s._audit(event, record)


def _preparation(s, node, purpose):
    """A budget renewal may retain receipts of its own settled predecessor."""
    current = node
    seen = set()
    while current['id'] not in seen:
        seen.add(current['id'])
        saved = next((e['data'] for e in reversed(machine.events(s, current['id']))
                      if e['kind'] == _PREPARED and e['data']['purpose'] == purpose), None)
        if saved:
            if saved.get('owner') != current['id']:
                raise InvalidUpdate('Prepared source receipt owner differs from its execution')
            return saved
        previous = current.get('previous')
        if not previous or node['kind'] != 'execution':
            return None
        current = machine.node(s, previous)
        keys = ('kind', 'session', 'flow_id', 'binding', 'context_hash', 'permissions')
        if any(current[key] != node[key] for key in keys) or machine.state(s, previous) != 'DONE':
            raise InvalidUpdate('Source receipt predecessor is outside this execution lineage')
    raise InvalidUpdate('Source receipt execution lineage contains a cycle')


def acquire(s, meta, workspace, root, request, candidates, *, real,
            before_read=None, purpose='workspace_synthesis', cached_only=False):
    """Select and read new evidence, or re-verify an exact prior preparation.

    A prepared snapshot is not an independent grant. Its owner, task binding,
    request, and selected sources must still be valid. Byte verification uses
    the same scoped reads as normal packet validation without a second compiler
    dispatch. Model and reviewer calls retain their own admission checks.
    """
    sid = meta['active']
    node, identity = _scope(s, sid, workspace, request, purpose)
    owner = node['id']
    candidates = list(candidates)
    if purpose == 'child_review' and candidates != node['files']:
        raise InvalidUpdate('Child source preparation differs from its assigned files')
    machine.scope_check(node, 'repository', candidates)
    # The task-only path can create the underlying node before repository
    # dispatch starts its own transaction.
    s.db.commit()

    if identity:
        # A sizing-only lookup can re-verify previously paid evidence without
        # reserving fresh work. New acquisition and every model dispatch keep
        # their normal quantitative admission. It never builds a new receipt.
        if not cached_only:
            machine.allowed(machine.gates(s, owner, {}))
        preparation = _preparation(s, node, purpose)
        if preparation and all(preparation.get(k) == v for k, v in identity.items() if k != 'owner'):
            if preparation['candidates'] == candidates:
                plan = preparation['page_plan']
                selected = plan['selected']
                if (not selected or len(set(selected)) != len(selected) or
                        not set(selected).issubset(candidates) or
                        set(plan['omitted']) != set(candidates).difference(selected)):
                    raise InvalidUpdate('Prepared review page differs from the candidate inventory')
                machine.scope_check(node, 'repository', selected)
                if before_read:
                    before_read(selected)
                try:
                    data = runtime.load_partition(s, sid, workspace, preparation['receipts'], preparation['owner'])
                except StaticSourceChanged:
                    _audit(s, 'REVIEW_SOURCE_CHANGED', preparation)
                    if purpose == 'child_review':
                        raise
                else:
                    if set(data['sources']) != set(selected):
                        raise InvalidUpdate('Prepared review paths differ from their source receipts')
                    _audit(s, 'REVIEW_SOURCE_REUSED', dict(preparation, reused_by=owner))
                    return preparation['receipts'], data, dict(plan, snapshot_owner=preparation['owner'])

    if cached_only:
        return None

    if purpose == 'child_review':
        files = candidates
        plan = {'selected': files, 'omitted': []}
    else:
        files, plan = select(s, sid, workspace, candidates, purpose)
    if before_read:
        before_read(files)
    receipts = runtime.build_partition(s, meta, workspace, root, files, real=real)
    data = runtime.load_partition(s, sid, workspace, receipts, owner)
    if set(data['sources']) != set(files):
        raise InvalidUpdate('Prepared review paths differ from their source receipts')
    if purpose == 'child_review':
        for path, digest in data['versions'].items():
            if node['context']['versions'].get(path) != digest:
                raise StaticSourceChanged('Source changed since branch planning: ' + path)
    if identity:
        record = dict(identity, candidates=candidates, receipts=receipts, page_plan=plan)
        _audit(s, _PREPARED, record, commit_node=True)
    return receipts, data, dict(plan, snapshot_owner=owner)


def reuse(s, meta, workspace, root, request, candidates, *, real,
          purpose='workspace_synthesis'):
    """Plan from exact current paid receipts; never acquire new source.

    Binding, inherited read scope, source identity and receipt checks are the
    same as ordinary reuse. Financial admission remains mandatory for execution.
    """
    return acquire(s, meta, workspace, root, request, candidates, real=real,
                   purpose=purpose, cached_only=True)
