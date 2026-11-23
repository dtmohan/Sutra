"""Scoped, receipt-backed continuity views shared by workspace model roles.

Working notes are model claims, never source authority. Whole source units keep
all their receipt/span identities. A small window defers content behind a
nested directory; it never silently converts a partial prefix into all memory.
"""
import copy
import json
import re

from context_manager import ContextBudgetExceeded, InvalidUpdate, canonical, sha
from paging import PageFault
import orchestration as machine
import repository_tools as repo

VERSION = 1
MAX_PAGES = 32768
MAX_LIMIT = 16 * 1024 * 1024
CONTENTS_FIELDS = ('root', 'pages', 'totals', 'deferred', 'requested_refs')


def contents_bytes(view):
    """Conservative content-window cost, excluding anchored host-only metadata."""
    return len(canonical({key: view[key] for key in CONTENTS_FIELDS}).encode())


def minimum_bytes(view):
    """Exact semantic-window floor with only explicitly requested whole pages."""
    refs = set(view['requested_refs']); root_ref = view['root']['ref']
    pages = [page for page in view['pages'] if page['ref'] in refs]
    if refs - {root_ref} - {page['ref'] for page in pages}:
        raise InvalidUpdate('A mandatory retained-context page is missing')
    minimum = {key: view[key] for key in CONTENTS_FIELDS}
    minimum['pages'] = pages
    minimum['deferred'] = {
        'notes': view['totals']['notes'] - sum(page['kind'] == 'working_note_unverified' for page in pages),
        'source_units': view['totals']['source_units'] - sum(page['kind'] == 'retained_source_unit' for page in pages),
        'pages': view['totals']['pages'] - len(pages)}
    return contents_bytes(minimum)


def _binding(s, sid, p, owner):
    owner = owner or p.get('retained_context_owner') or p.get('discovery_owner') or p.get('snapshot_owner')
    if isinstance(owner, dict):
        owner = owner['id']
    if not owner:
        raise InvalidUpdate('Retained context requires an explicit orchestration owner')
    node = machine.node(s, owner)
    if (node.get('session') != sid or node.get('kind') not in ('task', 'execution')
            or not machine.binding_matches(s, node)
            or node.get('permissions') != machine.permissions(s, sid)
            or machine.state(s, owner) in ('CANCELLED', 'QUARANTINED')):
        raise InvalidUpdate('Retained context owner has a foreign or stale binding')
    if node['kind'] == 'execution':
        import review_discovery as discovery
        lineage = discovery._lineage(s, node)
        handoff = (p.get('conversation_handoff') or {}).get('invocation_id')
        if (node['context'].get('request') != p['request']
                or node['context'].get('conversation_id') != handoff):
            raise InvalidUpdate('Retained context request differs from execution context')
    else:
        lineage = [node]
    return dict(node['binding'], owner=owner, lineage=[n['id'] for n in lineage],
                workspace=p['workspace'], request_hash=sha(p['request']),
                conversation_id=(p.get('conversation_handoff') or {}).get('invocation_id'),
                parents=list(p.get('derived_from', [])) if node['kind'] == 'task' else None)


def _journal(s, sid, binding, journal=None, seeds=()):
    """Only exact owner/compatible execution predecessors, never SID-wide memory."""
    allowed = set(binding['lineage'])
    rows = s.db.execute('''SELECT i.id,i.record_json,i.digest,d.payload_json,d.payload_hash,
        r.record_json,r.id FROM invocations i JOIN deliveries d ON d.id=i.delivery_id
        JOIN invocation_receipts r ON r.invocation_id=i.id WHERE i.session=?
        AND json_extract(i.record_json,'$.role')='architect' ORDER BY i.rowid''', (sid,))
    found = {}
    for iid, env_raw, env_digest, raw, payload_hash, receipt_raw, receipt_id in rows:
        env, packet, receipt = json.loads(env_raw), json.loads(raw), json.loads(receipt_raw)
        if ((env.get('orchestration') or {}).get('node_id') not in allowed
                or packet.get('workspace') != binding['workspace']
                or receipt.get('status') != 'accepted'):
            continue
        if (sha('invocation-v1:' + env_raw) != env_digest
                or sha('invocation-receipt-v1:' + receipt_raw) != receipt_id
                or sha(raw) != payload_hash or env['payload_hash'] != payload_hash
                or receipt['payload_hash'] != payload_hash):
            raise PageFault('Retained architect receipt or delivery is corrupt', code='corrupt')
        found[iid] = packet
    if journal is None and binding['parents'] is not None:
        # Manual architecture requests share a task node. Only explicit parent
        # ancestry belongs to this request; equal SID/workspace is insufficient.
        wanted_ids = set(); pending = list(binding['parents'])
        while pending:
            iid = pending.pop()
            if iid in wanted_ids:
                continue
            if iid not in found:
                raise PageFault('Retained parent is outside its task ancestry', code='missing')
            wanted_ids.add(iid)
            pending.extend(found[iid].get('derived_from', []))
        ids = [iid for iid in found if iid in wanted_ids]
    else:
        ids = list(found) if journal is None else journal['invocations']
    if len(ids) != len(set(ids)) or any(iid not in found for iid in ids):
        raise PageFault('Retained architect journal is missing or foreign', code='missing')
    proposals = s.load_architect_results(sid, ids)
    accepted = [(iid, found[iid], proposal) for iid, proposal in zip(ids, proposals)]
    wanted = None if journal is None else set(journal['receipts'])
    records = []
    # Iterate bounded metadata and load only this owner's addressed receipts.
    rows = s.db.execute('''SELECT r.id,o.record_json FROM repository_ops o
        JOIN repository_receipts r ON r.operation_id=o.id WHERE o.session=? ORDER BY r.seq''', (sid,))
    for rid, raw in rows:
        op = json.loads(raw)
        if ((op.get('orchestration') or {}).get('node_id') not in allowed
                or op['kind'] not in ('read', 'search')
                or (op.get('invocation_id') and op['invocation_id'] not in ids)):
            continue
        if wanted is not None and rid not in wanted:
            continue
        if (wanted is None and binding['parents'] is not None
                and not op.get('invocation_id') and rid not in seeds):
            continue
        record = repo.record_by_id(s, sid, rid)
        if record['result'].get('status') == 'ok':
            records.append(record)
    actual = {'invocations': ids, 'receipts': [r['id'] for r in records]}
    if journal is not None and journal != actual:
        raise PageFault('Retained source journal is missing or outside its owner', code='missing')
    if len(ids) + sum(len(r['result'].get('excerpts', [])) for r in records) > MAX_PAGES:
        raise ContextBudgetExceeded('Retained memory directory exceeds its explicit page bound; original receipts remain saved')
    return accepted, records, actual


def _graph(s, sid, binding, journal=None, seeds=()):
    import review_discovery as discovery
    accepted, records, journal = _journal(s, sid, binding, journal, seeds)
    key = sha('retained-context-v1:' + canonical(binding))
    directory = discovery.memory_directory(sid, key)
    put, index, label = (directory[k] for k in ('put', 'index', 'label'))
    note_refs = []
    for ordinal, ((iid, _, _), note) in enumerate(zip(accepted, discovery._notes(accepted))):
        ref = put({'kind': 'working_note_unverified', 'checkpoint': iid,
                   'ordinal': ordinal + 1, 'note': note})
        note_refs.append(ref)
    # Verify current bytes under the existing source-receipt API. This performs
    # no new tool operation, permission grant, reservation or source charge.
    available = repo.evidence(s, sid, binding['workspace'])
    source_refs = []; seen = set()
    for record in records:
        for original in record['result'].get('excerpts', []):
            entry = available.get(original['id'])
            expected = dict(original, receipt=record['id'])
            if entry != expected:
                raise PageFault('Retained source is stale or changed: ' + original['path'],
                                code='source_changed', key=original['path'])
            identity = canonical(entry)
            if identity in seen:
                continue
            seen.add(identity)
            source_refs.append(put({'kind': 'retained_source_unit', 'evidence': entry}))
    pages = directory['pages']
    notes = index('notes', [{'label': str(n + 1), 'ref': ref} for n, ref in enumerate(note_refs)])
    sources = index('sources', [{'label': label(pages[ref]['evidence']['path'] + ':' +
                    str(pages[ref]['evidence']['line'])), 'ref': ref} for ref in source_refs])
    root = put({'kind': 'retained_memory_root', 'notes': len(note_refs), 'source_units': len(source_refs),
                'links': [{'label': 'notes', 'ref': notes}, {'label': 'sources', 'ref': sources}]})
    return dict(directory, root=root, note_refs=note_refs, source_refs=source_refs, journal=journal)


def _anchor(binding, graph):
    return {'version': VERSION, 'binding': binding, 'journal': graph['journal'], 'root': graph['root']}


def _pack(binding, graph, limit, required_refs):
    if type(limit) is not int or not 0 < limit <= MAX_LIMIT:
        raise InvalidUpdate('Invalid retained-context byte limit')
    required = list(dict.fromkeys(required_refs))
    if len(required) > 128 or any(not isinstance(ref, str) or len(ref) != 64 for ref in required):
        raise InvalidUpdate('Invalid retained-context requested refs')
    if any(ref not in graph['pages'] for ref in required):
        raise PageFault('Retained-context lookup is foreign or missing', code='missing')
    root = graph['root']; selected = set(); pages = graph['pages']
    base = {'version': VERSION, 'binding': binding, 'journal': graph['journal'],
            'root': copy.deepcopy(pages[root]), 'pages': [],
            'totals': {'notes': len(graph['note_refs']), 'source_units': len(graph['source_refs']),
                       'pages': len(pages) - 1},
            'deferred': {}, 'requested_refs': required, 'limit_bytes': limit}
    note_keys, source_keys = set(graph['note_refs']), set(graph['source_refs'])
    selected_notes = selected_sources = selected_bytes = 0
    fixed_cost = contents_bytes(base)
    def deferred():
        return {'notes': len(note_keys) - selected_notes,
                'source_units': len(source_keys) - selected_sources,
                'pages': len(pages) - 1 - len(selected)}
    def size():
        # Serialize each content unit once; do not repeatedly serialize all
        # earlier source text for every candidate as the journal grows.
        return (fixed_cost + len(canonical(deferred()).encode()) - 2 + selected_bytes
                + max(0, len(selected) - 1))
    def value():
        base['pages'] = [copy.deepcopy(v) for ref, v in pages.items() if ref in selected]
        base['deferred'] = deferred()
        return base
    def add(ref, mandatory=False):
        nonlocal selected_notes, selected_sources, selected_bytes
        if ref == root or ref in selected:
            return True
        page_bytes = len(canonical(pages[ref]).encode())
        selected.add(ref); selected_bytes += page_bytes
        selected_notes += ref in note_keys; selected_sources += ref in source_keys
        if size() <= limit:
            return True
        selected.remove(ref); selected_bytes -= page_bytes
        selected_notes -= ref in note_keys; selected_sources -= ref in source_keys
        if mandatory:
            raise ContextBudgetExceeded('A requested whole retained-memory page cannot fit; its directory and receipts remain saved')
        return False
    if size() > limit:
        raise ContextBudgetExceeded('The retained-memory root cannot fit; original notes and sources remain saved')
    for ref in required:
        add(ref, True)
    # Keep directories before optional content. The root always resolves even
    # if only its immediate references fit; no fixed-count content tail exists.
    for ref, page in pages.items():
        if page['kind'] == 'index':
            add(ref)
    # Whole notes first, then whole source units; fit each without dropping an
    # older nonadjacent span just because another span from its path is newer.
    for ref in reversed(graph['note_refs']):
        add(ref)
    for ref in graph['source_refs']:
        add(ref)
    return copy.deepcopy(value())


def build(s, sid, p, *, owner=None, limit=32768, required_refs=()):
    binding = _binding(s, sid, p, owner)
    if type(limit) is not int or not 0 < limit <= MAX_LIMIT:
        raise InvalidUpdate('Invalid retained-context byte limit')
    seeds = {entry['receipt'] for entry in p.get('evidence', []) if 'receipt' in entry}
    graph = _graph(s, sid, binding, seeds=seeds)
    anchor = _anchor(binding, graph)
    raw = canonical(anchor)
    if not s.db.execute("SELECT 1 FROM page_audit WHERE event='RETAINED_CONTEXT_ROOT' AND payload_json=?", (raw,)).fetchone():
        with s.db:
            s._audit('RETAINED_CONTEXT_ROOT', anchor)
    return _pack(binding, graph, limit, required_refs)


def bind(s, sid, p, *, owner=None, limit=32768, required_refs=()):
    view = build(s, sid, p, owner=owner, limit=limit, required_refs=required_refs)
    p['retained_context_version'] = VERSION
    p['retained_context'] = view
    return view


def _bound_graph(s, sid, p, owner):
    view = p.get('retained_context')
    if p.get('retained_context_version') != VERSION or not isinstance(view, dict):
        raise InvalidUpdate('Missing retained-context profile')
    journal = view.get('journal')
    if (view.get('version') != VERSION or not isinstance(journal, dict)
            or set(journal) != {'invocations', 'receipts'}
            or any(not isinstance(journal[key], list) or len(journal[key]) > MAX_PAGES
                   or any(not isinstance(value, str) or not 1 <= len(value) <= 128
                          for value in journal[key]) for key in journal)):
        raise InvalidUpdate('Malformed retained-context journal')
    binding = _binding(s, sid, p, owner)
    if view.get('binding') != binding:
        raise InvalidUpdate('Retained-context binding changed')
    graph = _graph(s, sid, binding, view.get('journal'))
    anchor = _anchor(binding, graph)
    if not s.db.execute("SELECT 1 FROM page_audit WHERE event='RETAINED_CONTEXT_ROOT' AND payload_json=?",
                        (canonical(anchor),)).fetchone():
        raise InvalidUpdate('Retained-context directory was not committed')
    if view != _pack(binding, graph, view.get('limit_bytes'), view.get('requested_refs', ())):
        raise InvalidUpdate('Retained-context view differs from its verified directory')
    return view, binding, graph


def check(s, sid, p, *, owner=None):
    view, binding, graph = _bound_graph(s, sid, p, owner)
    return copy.deepcopy(view)


def refit(s, sid, p, limit, *, owner=None, required_refs=()):
    view, binding, graph = _bound_graph(s, sid, p, owner)
    # A caller cannot discard an already requested mandatory page by refitting.
    refs = list(dict.fromkeys(view.get('requested_refs', []) + list(required_refs)))
    result = _pack(binding, graph, limit, refs)
    p['retained_context'] = result
    return result


def lookup(s, sid, p, ref, *, owner=None):
    view, binding, graph = _bound_graph(s, sid, p, owner)
    if not isinstance(ref, str) or ref not in graph['pages']:
        raise PageFault('Retained-context page is missing or foreign', code='missing')
    return copy.deepcopy(graph['pages'][ref])


def recall(s, sid, p, prior_packet, refs, *, owner=None, prior_owner=None):
    """Map a previously advertised subtree to current whole-unit addresses.

    Roots/indexes expand to their leaves. The old page remains independently
    resolvable, while current delivery uses its own committed namespace.
    """
    _, binding, graph = _bound_graph(s, sid, p, owner)
    _, old_binding, old_graph = _bound_graph(s, sid, prior_packet, prior_owner)
    if (old_binding['owner'] not in binding['lineage']
            or any(old_binding[key] != binding[key]
                   for key in ('session', 'workspace', 'contract_hash', 'calibration_hash',
                               'logic_hash', 'conversation_id'))):
        raise InvalidUpdate('Retained-context recall belongs to another task lineage')
    refs = list(refs)
    if len(refs) > 128 or any(not isinstance(ref, str) or ref not in old_graph['pages'] for ref in refs):
        raise PageFault('Retained-context recall ref is missing or foreign', code='missing')
    def signature(page):
        return canonical({key: val for key, val in page.items() if key != 'ref'})
    current = {signature(page): ref for ref, page in graph['pages'].items()
               if page['kind'] in ('working_note_unverified', 'retained_source_unit')}
    result = []; seen = set(); pending = list(reversed(refs))
    while pending:
        ref = pending.pop()
        if ref in seen:
            continue
        seen.add(ref); page = old_graph['pages'][ref]
        if page['kind'] in ('index', 'retained_memory_root'):
            pending.extend(link['ref'] for link in reversed(page.get('links', [])))
            continue
        mapped = current.get(signature(page))
        if mapped is None:
            raise PageFault('Previously retained page is outside the current task journal', code='missing')
        if mapped not in result:
            result.append(mapped)
        if len(result) > 128:
            raise ContextBudgetExceeded('Requested retained subtree needs more than 128 whole pages; request a smaller directory branch')
    return result


MAX_RECALL_REFS = 16
_RECALL = re.compile(r'Recall context: ([0-9a-f]{64})\Z')


def requests(payload, proposal):
    """Read the existing bounded remaining channel, without a new model field."""
    if payload.get('retained_context_version') != VERSION:
        return []
    remaining = proposal.get('remaining', [])
    # Ordinary notes remain governed by their role schema/aggregate allowance.
    # Only the explicit recall channel gains this parser's format/count bounds.
    if not isinstance(remaining, list) or any(not isinstance(line, str) for line in remaining):
        raise InvalidUpdate('Malformed retained-context remaining channel')
    view = payload.get('retained_context')
    if not isinstance(view, dict):
        raise InvalidUpdate('Missing retained-context request directory')
    nodes = [view['root']] + view['pages']
    known = {node['ref'] for node in nodes}
    known.update(link['ref'] for node in nodes for link in node.get('links', []))
    visible = {node['ref'] for node in nodes
               if node['kind'] in ('working_note_unverified', 'retained_source_unit')}
    supplied = {node['ref']: node for node in nodes}
    def fully_supplied(ref):
        pending = [ref]; seen = set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            node = supplied.get(current)
            if node is None:
                return False
            pending.extend(link['ref'] for link in node.get('links', []))
        return True
    result = []
    for line in remaining:
        if not line.startswith('Recall context:'):
            continue
        match = _RECALL.fullmatch(line)
        if not match:
            raise InvalidUpdate('Malformed retained-context recall request')
        ref = match.group(1)
        if ref not in known:
            raise InvalidUpdate('Retained-context recall ref was not advertised')
        if ref in visible:
            raise InvalidUpdate('Retained-context requested body was already visible')
        if fully_supplied(ref):
            raise InvalidUpdate('Retained-context request adds no previously deferred body')
        if ref not in result:
            result.append(ref)
        if len(result) > MAX_RECALL_REFS:
            raise InvalidUpdate('Too many retained-context recall requests')
    return result


def validate_requests(s, sid, p, refs):
    """Host preflight: requested supplied refs must expose a deferred body."""
    if (not isinstance(refs, list) or not 1 <= len(refs) <= MAX_RECALL_REFS
            or any(not isinstance(ref, str) or not re.fullmatch('[0-9a-f]{64}', ref) for ref in refs)):
        raise InvalidUpdate('Malformed retained-context requested refs')
    view, binding, graph = _bound_graph(s, sid, p, None)
    # Reuse the same exact channel parser so arbitrary hidden graph refs cannot
    # be introduced by a forged host IPC record.
    expected = requests(p, {'remaining': ['Recall context: ' + ref for ref in refs]})
    if list(refs) != expected:
        raise InvalidUpdate('Malformed retained-context requested refs')
    visible = {node['ref'] for node in p['retained_context']['pages']
               if node['kind'] in ('working_note_unverified', 'retained_source_unit')}
    pending = list(refs); deferred = []; seen = set()
    while pending:
        ref = pending.pop()
        if ref in seen:
            continue
        seen.add(ref); page = graph['pages'][ref]
        if page['kind'] in ('index', 'retained_memory_root'):
            pending.extend(link['ref'] for link in page.get('links', []))
        elif ref not in visible:
            deferred.append(ref)
            if len(deferred) > 128:
                raise InvalidUpdate('Retained context needs more than 128 whole pages; request a smaller directory branch')
    if not deferred:
        raise InvalidUpdate('Retained-context request adds no previously deferred body')
    required = list(dict.fromkeys(view['requested_refs'] + deferred))
    if len(required) > 128:
        raise InvalidUpdate('Retained context needs more than 128 mandatory pages; request a smaller directory branch')
    try:
        _pack(binding, graph, view['limit_bytes'], required)
    except ContextBudgetExceeded:
        limit = p.get('expanded_context_recall_bytes')
        if type(limit) is not int or not view['limit_bytes'] < limit <= 65536:
            raise InvalidUpdate('Retained context cannot fit this fixed window; request a smaller directory branch') from None
        try:
            _pack(binding, graph, limit, required)
        except ContextBudgetExceeded:
            raise InvalidUpdate('Retained context cannot fit the expanded view; request a smaller directory branch') from None
    return deferred


def _accepted_request(s, sid, iid):
    """Validate receipt/delivery identity before interpreting a model IPC note."""
    row = s.db.execute("""SELECT i.record_json,i.digest,d.payload_json,d.payload_hash,
        r.record_json,r.id FROM invocations i JOIN deliveries d ON d.id=i.delivery_id
        JOIN invocation_receipts r ON r.invocation_id=i.id WHERE i.id=? AND i.session=?""",
        (iid, sid)).fetchone()
    if not row:
        raise PageFault('Saved retained-context request is missing or foreign', code='missing')
    env_raw, digest, raw, payload_hash, receipt_raw, rid = row
    env, prior, receipt = json.loads(env_raw), json.loads(raw), json.loads(receipt_raw)
    if (sha('invocation-v1:' + env_raw) != digest
            or sha('invocation-receipt-v1:' + receipt_raw) != rid
            or sha(raw) != payload_hash or env['payload_hash'] != payload_hash
            or receipt['payload_hash'] != payload_hash
            or receipt['status'] != 'accepted'
            or env['role'] not in ('architect', 'chat_reviewer', 'reviewer', 'coder', 'subagent')):
        raise InvalidUpdate('Saved retained-context request is unaccepted or corrupt')
    return env, prior, s.load_result(sid, iid)


def apply_recall(s, sid, p, requestrecord, *, owner=None, limit=None):
    """Apply an accepted scoped model request to this next delivery, whole-unit.

    The draft is still unverified model work. This helper authorizes no source
    read, model call, permission change, task completion or allocation increase.
    Reapplying the same saved request is idempotent; a new model call asking for
    a body it already saw is rejected by requests()/the prior-view check.
    """
    if (not isinstance(requestrecord, dict) or set(requestrecord) != {'invocation_id', 'refs'}
            or not isinstance(requestrecord['invocation_id'], str)
            or not 1 <= len(requestrecord['invocation_id']) <= 128
            or not isinstance(requestrecord['refs'], list)
            or not 1 <= len(requestrecord['refs']) <= MAX_RECALL_REFS
            or any(not isinstance(ref, str) or not re.fullmatch('[0-9a-f]{64}', ref)
                   for ref in requestrecord['refs'])
            or len(set(requestrecord['refs'])) != len(requestrecord['refs'])):
        raise InvalidUpdate('Malformed saved retained-context recall')
    iid = requestrecord['invocation_id']
    _, prior, proposal = _accepted_request(s, sid, iid)
    expected = requests(prior, proposal)
    if requestrecord['refs'] != expected:
        raise InvalidUpdate('Saved recall differs from the accepted model request')
    deferred_leaves = validate_requests(s, sid, prior, expected)
    mapped = recall(s, sid, p, prior, deferred_leaves, owner=owner)
    return refit(s, sid, p, p['retained_context']['limit_bytes'] if limit is None else limit,
                 owner=owner, required_refs=mapped)


def recover_requests(s, sid, owner, request, *, parent_output_failure=None):
    """Recover receipt-only IPC after a crash, without replaying an older draft.

    Only the latest accepted writer in this compatible execution lineage is
    considered. A later ordinary accepted result supersedes earlier requests.
    The returned checkpoint grants no scope, capacity, source read or model call.
    """
    if not isinstance(request, dict) or not isinstance(request.get('prompt'), str):
        raise InvalidUpdate('Malformed retained-context recovery request')
    owner = machine.node(s, owner['id'] if isinstance(owner, dict) else owner)
    if owner.get('kind') != 'execution':
        return None
    if (owner.get('session') != sid or not machine.binding_matches(s, owner)
            or owner.get('permissions') != machine.permissions(s, sid)):
        raise InvalidUpdate('Retained-context recovery owner has a foreign or stale binding')
    if (machine.state(s, owner['id']) in machine.TERMINAL
            or owner.get('context', {}).get('request') != request['prompt']
            or owner['context'].get('conversation_id') != request.get('conversation_id')):
        return None
    import review_discovery as discovery
    allowed = {node['id'] for node in discovery._lineage(s, owner)}
    rows = s.db.execute("""SELECT i.id,i.record_json,d.payload_json FROM invocations i
        JOIN deliveries d ON d.id=i.delivery_id JOIN invocation_receipts r ON r.invocation_id=i.id
        WHERE i.session=? AND json_extract(i.record_json,'$.role')='chat_reviewer'
        AND json_extract(r.record_json,'$.status')='accepted' ORDER BY i.rowid DESC""", (sid,))
    for iid, env_raw, raw in rows:
        env, packet = json.loads(env_raw), json.loads(raw)
        if ((env.get('orchestration') or {}).get('node_id') not in allowed
                or packet.get('request') != request['prompt']
                or (packet.get('conversation_handoff') or {}).get('invocation_id') != request.get('conversation_id')):
            continue
        if parent_output_failure is not None and (
                (env.get('orchestration') or {}).get('node_id') != owner['id']
                or (packet.get('parent_draft_recovery') or {}).get('failed_invocation') != parent_output_failure):
            return None
        # Never bypass a newer accepted variant to resurrect an older request.
        if ([item['invocation_id'] for item in packet.get('child_results', [])] != request.get('child_results', [])
                or (packet.get('child_results') and
                    packet.get('orchestration_parent') != request.get('orchestration_parent'))):
            return None
        _, packet, proposal = _accepted_request(s, sid, iid)
        refs = requests(packet, proposal)
        if not refs:
            return None
        validate_requests(s, sid, packet, refs)
        return {'invocation_id': iid, 'refs': refs}
    return None


def source_units(view):
    return [copy.deepcopy(page['evidence']) for page in view['pages']
            if page['kind'] == 'retained_source_unit']


def note_pages(view):
    return [copy.deepcopy(page) for page in view['pages'] if page['kind'] == 'working_note_unverified']


def conversation_view(s, p, budget, *, system, schema_for, materialize=False,
                      extra_bytes=0, parent=None):
    """Reuse the conversation pager at a host-selected, verified parent only."""
    handoff = p.get('conversation_handoff') or {}
    parent = parent or handoff.get('invocation_id') or p.get('retained_conversation_parent')
    if not parent:
        return None
    row = s.db.execute('SELECT session FROM invocations WHERE id=?', (parent,)).fetchone()
    if not row:
        raise InvalidUpdate('Missing retained conversation receipt')
    import conversation_memory as cm
    # The workspace handoff validates request/workspace identity across the
    # conversation and workspace sessions. Document callers stay in one SID.
    if handoff:
        from conversation import check_workspace_handoff
        sid = p['contract']['task_id']
        check_workspace_handoff(s, sid, p)
    elif p.get('contract', {}).get('task_id') != row[0]:
        raise InvalidUpdate('Retained conversation parent belongs to another task')
    rows = cm.history(s, row[0], parent)
    if materialize:
        cm.materialize(s, row[0], rows)
    base = {key: val for key, val in p.items() if key not in ('memory', 'repair')}
    return cm.working_set(s, row[0], parent, p['request'], budget, base,
                          system, schema_for, extra_bytes=extra_bytes)
