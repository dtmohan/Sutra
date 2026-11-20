"""Resumable parent discovery using the existing architect/tool protocol.

The ledger owns every model decision and repository effect. Discovery notes are
unverified; only current repository receipts supply source text to synthesis.
"""
import copy
import json
import re
import uuid
from pathlib import Path

from context_manager import ContextBudgetExceeded, InvalidUpdate, canonical, sha
from paging import PageFault
from paging import Budget
import architecture_role as role
import orchestration as machine
import repository_tools as repo

VERSION = 4
SELECTION_VERSION = 3
SEARCH_VERSION = 1
MEMORY_VERSION = 1
REASONING_VERSION = 1
SOURCE_RECALL_VERSION = 1


def _lineage(s, owner):
    result = []; seen = set(); current = owner
    keys = ('kind', 'session', 'flow_id', 'binding', 'context_hash', 'permissions')
    while current['id'] not in seen:
        seen.add(current['id']); result.append(current)
        previous = current.get('previous')
        if not previous: return result
        current = machine.node(s, previous)
        if (any(current.get(k) != owner.get(k) for k in keys)
                or machine.state(s, previous) != 'DONE'):
            raise InvalidUpdate('Evidence discovery execution predecessor changed')
    raise InvalidUpdate('Evidence discovery execution lineage contains a cycle')


def scope(s, sid, p):
    owner = machine.node(s, p.get('discovery_owner', p['snapshot_owner']))
    handoff = p.get('conversation_handoff') or {}
    if (owner['kind'] != 'execution' or owner['session'] != sid
            or owner['context']['request'] != p['request']
            or owner['context'].get('conversation_id') != handoff.get('invocation_id')
            or owner['binding'] != machine.binding(s, sid)):
        raise InvalidUpdate('Evidence discovery lost its execution binding')
    lineage = _lineage(s, owner)
    if p['snapshot_owner'] not in {n['id'] for n in lineage}:
        raise InvalidUpdate('Evidence discovery source owner is outside its execution lineage')
    value = {'owner': lineage[-1]['id'], 'context_hash': owner['context_hash'],
             'workspace': p['workspace'], 'request': p['request'],
             'conversation_id': handoff.get('invocation_id'), 'binding': owner['binding'],
             'snapshots': p['snapshot_receipts'], 'logic_hash': s.logic_hash}
    return owner, sha(canonical(value))


def history(s, sid, key):
    result = []
    rows = s.db.execute('''SELECT i.id,d.payload_json,r.record_json FROM invocations i
        JOIN deliveries d ON d.id=i.delivery_id JOIN invocation_receipts r ON r.invocation_id=i.id
        WHERE i.session=? AND json_extract(i.record_json,'$.role')='architect'
        AND json_extract(d.payload_json,'$.discovery_key')=? ORDER BY i.rowid''', (sid, key))
    for iid, raw, receipt in rows:
        result.append((iid, json.loads(raw), json.loads(receipt)))
    return result


def _accepted(s, sid, records):
    accepted = []; previous = None
    for iid, packet, receipt in records:
        if receipt['status'] != 'accepted': continue
        if packet.get('discovery_previous') != previous:
            raise InvalidUpdate('Evidence discovery checkpoint chain changed')
        accepted.append((iid, packet))
        previous = iid
    proposals = s.load_architect_results(sid, [iid for iid, _ in accepted])
    return [(iid, packet, proposal) for (iid, packet), proposal in zip(accepted, proposals)]


def _request_id(iid):
    return sha(canonical(['parent-evidence-discovery', VERSION, iid]))


def authorize_tool(s, sid, packet):
    decision_owner, _ = scope(s, sid, packet)
    active = machine.checked(s, machine.ACTIVE.get())
    if decision_owner['id'] not in {n['id'] for n in _lineage(s, active)}:
        raise InvalidUpdate('Evidence discovery tool belongs to another execution')


def _tool_receipts(s, sid, accepted):
    receipts = []
    for iid, packet, proposal in accepted:
        tool = proposal['tool']
        if tool['kind'] not in ('read', 'search', 'list'): continue
        row = s.db.execute('SELECT r.id FROM repository_ops o JOIN repository_receipts r '
                           'ON r.operation_id=o.id WHERE o.session=? AND o.request_id=?',
                           (sid, _request_id(iid))).fetchone()
        if not row: continue
        record = repo.record_by_id(s, sid, row[0])
        op = record['operation']
        expected = (tool['kind'], tool['target'], tool['query'], tool['line'], tool.get('cursor'))
        if ((op['kind'], op['target'], op['query'], op['line'], op.get('cursor')) != expected
                or op.get('invocation_id') != iid):
            raise InvalidUpdate('Evidence discovery tool differs from its accepted proposal')
        read_profile = packet.get('read_profile') if tool['kind'] == 'read' else None
        if op.get('read_profile') != read_profile:
            raise InvalidUpdate('Evidence discovery read page differs from its admitted profile')
        search_version = packet.get('discovery_search_version') if tool['kind'] == 'search' else None
        if op.get('search_version') != search_version:
            raise InvalidUpdate('Evidence discovery search differs from its admitted profile')
        effect_owner = machine.node(s, op['orchestration']['node_id'])
        if packet.get('discovery_owner', packet['snapshot_owner']) not in {n['id'] for n in _lineage(s, effect_owner)}:
            raise InvalidUpdate('Evidence discovery tool belongs to another execution')
        receipts.append(record)
    return receipts


def _notes(accepted):
    return [{'summary': proposal['summary'], 'remaining': copy.deepcopy(proposal['remaining']),
             'citation_paths': sorted({entry['path'] for entry in packet['evidence']
                if any(f['page_id'] == entry['id'] for f in __import__('review_role').findings(proposal))})}
            for _, packet, proposal in accepted]


def memory(s, p, budget, *, materialize=False):
    iid = (p.get('conversation_handoff') or {}).get('invocation_id')
    if not iid: return None
    import conversation_memory as cm
    row = s.db.execute('SELECT session FROM invocations WHERE id=?', (iid,)).fetchone()
    if not row: raise InvalidUpdate('Missing discovery conversation receipt')
    if materialize: cm.materialize(s, row[0], cm.history(s, row[0], iid))
    base = {k: v for k, v in p.items() if k not in ('memory', 'repair')}
    return cm.working_set(s, row[0], iid, p['request'], budget, base, role.SYSTEM, role.schema, extra_bytes=0)


def _protected_cost(budget, packet, schema):
    return {'calls': 3, 'tokens': 3*(budget.charge(role.SYSTEM, packet, schema)+budget.response_reserve),
            'operations': 1}


def _request_funding(s, sid, owner, budget, packet, schema, cause, *, provider=None):
    """Expose a real financial denial only after the same physical packet fits.

    This is a measurement, not a changed dispatch allocation. The existing
    working-set shortage supplies its byte charge when mandatory pins/latest
    turn cannot yet be materialized under the current financial share.
    """
    if packet.get('discovery_selection_version') == 3:
        return _fund_active_source(s, sid, owner, budget, packet, schema, provider)
    if packet.get('discovery_selection_version') not in (1, 2) or budget.context_profile != 2:
        return
    # The existing memory planner also reserves this repair allowance. Use
    # that same packet for the physical proof and financial gate so a minimal
    # grant can reconstruct its mandatory working set on the next fit.
    probe = dict(packet, repair={'errors': ['x'*140]*2})
    measured = budget
    shortage = cause
    for _ in range(4):
        try:
            probe['memory'] = memory(s, probe, measured, materialize=True)
            break
        except ContextBudgetExceeded as exc:
            shortage = exc
            required = getattr(shortage, 'budget', {}).get('required_input')
            if type(required) is not int or required < 0:
                return
            limit = required + budget.response_reserve
            if limit <= measured.input_limit:
                return
            measured = budget.with_limits(reservation_limit=limit)
    else:
        return
    from context_window import check
    try:
        actual = dict(probe)
        if 'repair' in packet: actual['repair'] = packet['repair']
        else: actual.pop('repair', None)
        check(budget, role.SYSTEM, actual, schema, planning=True)
        check(budget, role.SYSTEM, probe, schema, planning=True)
    except ContextBudgetExceeded:
        return  # More task units cannot repair a physical window overflow.
    machine.allowed(machine.gates(s, owner['id'], _protected_cost(budget, probe, schema)))
    from review_budget import require
    require(s, sid, _protected_cost(budget, probe, schema))


def _fund_active_source(s, sid, owner, budget, packet, schema, provider):
    """Distinguish financial shortage from physical overflow for a new page.

    The current provider's trusted metadata supplies a conservative physical
    bound for Ollama. Exact-window providers retain their existing counter.
    Neither path changes a dispatch budget or grants capacity directly.
    """
    probe = dict(packet, repair={'errors': ['x'*140]*2})
    if budget.context_profile == 2:
        physical = budget
        for _ in range(4):
            try:
                probe['memory'] = memory(s, probe, physical, materialize=True)
                break
            except ContextBudgetExceeded as exc:
                required = getattr(exc, 'budget', {}).get('required_input')
                if type(required) is not int or required < 0:
                    return False
                limit = required + budget.response_reserve
                if limit <= physical.input_limit:
                    return False
                physical = physical.with_limits(reservation_limit=limit)
        else:
            return False
        from context_window import check
        try:
            actual = dict(probe)
            if 'repair' in packet: actual['repair'] = packet['repair']
            else: actual.pop('repair', None)
            check(physical, role.SYSTEM, actual, schema, planning=True)
            proof = check(physical, role.SYSTEM, probe, schema, planning=True)
        except ContextBudgetExceeded:
            return False
        capacity = proof['capacity']
        basis = proof['basis']
    else:
        metadata = getattr(provider, 'context_metadata', None)
        if not isinstance(metadata, dict):
            return False
        from model_context import allocation
        installed, _ = allocation(metadata)
        physical = Budget(installed.context_limit, budget.response_reserve, budget.template_reserve)
        try:
            probe['memory'] = memory(s, probe, physical, materialize=True)
            actual = dict(probe)
            if 'repair' in packet: actual['repair'] = packet['repair']
            else: actual.pop('repair', None)
            physical.check(role.SYSTEM, actual, schema)
            charge = physical.check(role.SYSTEM, probe, schema)
        except ContextBudgetExceeded:
            return False
        from smriti_adapter import SmritiAdapter
        capacity = SmritiAdapter().context(charge, budget.response_reserve, physical.context_limit)
        basis = 'conservative_utf8_upper_bound'
    with s.db:
        s._audit('DISCOVERY_ACTIVE_PAGE_FIT', {'session': sid, 'owner': owner['id'],
            'model': getattr(provider, 'model', None), 'basis': basis,
            'capacity': capacity, 'context_limit': physical.context_limit,
            'source_receipt': (packet.get('active_source_page') or {}).get('receipt')})
    cost = _protected_cost(budget, probe, schema)
    machine.allowed(machine.gates(s, owner['id'], cost))
    from review_budget import require
    require(s, sid, cost)
    return True


def _state(key, accepted, receipts, *, complete=False, reason=None):
    return {'version': VERSION, 'key': key, 'invocations': [iid for iid, _, _ in accepted],
            'receipts': [r['id'] for r in receipts], 'complete': complete, 'stop_reason': reason}


def _entries(s, sid, workspace, receipts):
    available = repo.evidence(s, sid, workspace)
    result = []
    for record in receipts:
        for entry in record['result'].get('excerpts', []):
            actual = available.get(entry['id'])
            if not actual:
                raise PageFault('Discovery source changed: ' + entry['path'], code='source_changed', key=entry['path'])
            result.append(actual)
    return result


def _page_working_set(s, sid, workspace, receipts, accepted):
    """Latest requested page wins; accepted deliveries retire only exact units."""
    entries = _entries(s, sid, workspace, receipts)
    reads = [r for r in receipts if r['operation']['kind'] == 'read' and r['result']['status'] == 'ok']
    by_id = {entry['id']: entry for entry in entries}
    seen = {entry['id'] for _, packet, _ in accepted for entry in packet['evidence']
            if entry['id'] in by_id and canonical(entry) == canonical(by_id[entry['id']])}
    pages = [(record, [by_id[e['id']] for e in record['result'].get('excerpts', [])
                       if e['id'] not in seen]) for record in reads]
    if not pages or not pages[-1][1]:
        return entries, None, [], pages
    record, unread = pages[-1]
    # Earlier accepted windows can leave a gap. Each delivered prefix remains
    # contiguous, rather than joining unseen text around that gap.
    units = record['result']['excerpts']; first = next(i for i, e in enumerate(units) if e['id'] == unread[0]['id'])
    prefix = []
    for entry in units[first:]:
        if entry['id'] in seen: break
        prefix.append(by_id[entry['id']])
    older = [entry for entry in entries if entry['receipt'] != record['id']]
    return older, (record, unread), prefix, pages


def _page_view(active, prefix):
    if active is None: return None
    record, unread = active
    if not prefix: raise ContextBudgetExceeded('No coherent unread source unit fits the discovery context')
    final = prefix[-1]
    end = final.get('end_position')
    if 'end_position' not in final:
        cursor = record['result'].get('next_cursor')
        end = {key: cursor[key] for key in ('line', 'column')} if cursor else None
    remaining = [entry for entry in unread if entry['id'] not in {item['id'] for item in prefix}]
    return {'version': 1, 'receipt': record['id'], 'path': prefix[0]['path'],
            'unit_ids': [e['id'] for e in unread], 'prefix': [e['id'] for e in prefix],
            'total_units': len(record['result']['excerpts']), 'start_line': prefix[0]['line'],
            'column': prefix[0].get('column', 0), 'end_position': end,
            'partial_page': bool(remaining), 'next_fresh_position':
                {'line': remaining[0]['line'], 'column': remaining[0].get('column', 0)} if remaining else None}


def _page_toc(pages, limit):
    result = []
    for _, unread in reversed(pages[:-1]):
        if not unread: continue
        first = unread[0]
        item = {'path': first['path'], 'start_line': first['line'],
                'column': first.get('column', 0), 'unread_units': len(unread)}
        if len(canonical(result + [item]).encode()) <= limit: result.append(item)
    return result


def _supplied_progress_rows(s, sid, workspace, receipts, accepted):
    """Positions actually supplied in accepted calls, never review claims."""
    available = {entry['id']: entry for entry in _entries(s, sid, workspace, receipts)}
    supplied = {}
    for ordinal, (_, packet, _) in enumerate(accepted):
        for entry in packet['evidence']:
            actual = available.get(entry['id'])
            # Older recipes could supply only part of a unit. Do not promote
            # that substring to a claim that the whole unit was delivered.
            if actual and canonical(entry) == canonical(actual):
                supplied[entry['id']] = (actual, ordinal)
    paths = {}
    for entry, ordinal in supplied.values():
        row = paths.setdefault(entry['path'], {'path': entry['path'], 'units': [], 'recent': ordinal})
        start = (entry['line'], entry.get('column', 0))
        supplied_end = entry.get('end_position')
        if isinstance(supplied_end, dict):
            # A unit may end between CR and LF, so the whole-source reader's
            # receipt position takes precedence over splitting this substring.
            end = (supplied_end['line'], supplied_end['column'])
        else:
            # Match repository paging's splitlines(True), including CR and
            # Unicode separators. The sentinel preserves a trailing empty line
            # for coordinate arithmetic only; it never becomes source evidence.
            lines = (entry['text'] + '\0').splitlines(True)
            end = (start[0] + len(lines) - 1,
                   (start[1] if len(lines) == 1 else 0) + len(lines[-1]) - 1)
        row['units'].append((start, end, entry.get('end_position', False) is None,
                             ordinal, entry['receipt']))
        row['recent'] = max(row['recent'], ordinal)
    result = []
    for row in paths.values():
        spans = []
        for start, end, eof, recent, _ in sorted(row['units']):
            if spans and start <= spans[-1][1]:
                previous = spans[-1]
                spans[-1] = (previous[0], max(previous[1], end), previous[2] or eof,
                             max(previous[3], recent))
            else:
                spans.append((start, end, eof, recent))
        prior = []; overlaps = 0; reads = 0
        for record in receipts:
            if record['operation']['kind'] != 'read': continue
            positions = [(start, end) for start, end, _, _, rid in row['units'] if rid == record['id']]
            if not positions: continue
            reads += 1
            overlaps += any(a < d and c < b for a, b in positions for c, d in prior)
            prior.extend(positions)
        result.append({'path': row['path'], 'spans': spans, 'recent': row['recent'],
                       'read_pages': reads, 'overlapping_read_pages': overlaps})
    return sorted(result, key=lambda row: (-row['recent'], row['path']))


def _progress_view(rows, limit):
    if type(limit) is not int or not 512 <= limit <= 2048:
        raise InvalidUpdate('Invalid supplied source progress allocation')
    result = {'meaning': 'Previously supplied source; not reviewed or understood.',
              'total_paths': len(rows), 'total_spans': sum(len(row['spans']) for row in rows),
              'read_pages': sum(row['read_pages'] for row in rows),
              'overlapping_read_pages': sum(row['overlapping_read_pages'] for row in rows),
              'omitted_paths': len(rows), 'omitted_spans': sum(len(row['spans']) for row in rows),
              'paths': []}
    for source in rows:
        row = {'path': source['path'], 'spans': [], 'read_pages': source['read_pages'],
               'overlapping_read_pages': source['overlapping_read_pages']}
        candidate = copy.deepcopy(result)
        candidate['paths'].append(row); candidate['omitted_paths'] -= 1
        if len(canonical(candidate).encode()) > limit: continue
        for start, end, eof, _ in sorted(source['spans'], key=lambda span: (-span[3], span[0])):
            span = {'start_line': start[0], 'column': start[1],
                    'end_position': None if eof else {'line': end[0], 'column': end[1]}}
            trial = copy.deepcopy(candidate)
            trial['paths'][-1]['spans'].append(span)
            trial['paths'][-1]['spans'].sort(key=lambda item: (item['start_line'], item['column']))
            trial['omitted_spans'] -= 1
            if len(canonical(trial).encode()) <= limit: candidate = trial
        result = candidate
    return result


def supplied_source_progress(s, sid, workspace, receipts, accepted, limit):
    return _progress_view(_supplied_progress_rows(s, sid, workspace, receipts, accepted), limit)


def _memory_graph(sid, key, accepted, receipts):
    """Address the verified journal, without turning its notes into authority."""
    pages, note_refs, source_refs, parents = {}, {}, {}, {}
    work = {'pages_built': 0, 'index_pages_built': 0, 'historical_roots_checked': 0}
    def put(body):
        ref = sha('discovery-memory-v1:' + canonical([sid, key, body]))
        pages[ref] = dict(ref=ref, **copy.deepcopy(body))
        work['pages_built'] += 1
        work['index_pages_built'] += body['kind'] in ('index', 'working_memory_root')
        if body['kind'] in ('index', 'working_memory_root') and len(canonical(pages[ref]).encode()) > 512:
            raise ContextBudgetExceeded('A mandatory working-memory index page cannot fit')
        for link in body.get('links', []): parents[link['ref']] = ref
        return ref
    def label(text):
        if len(canonical(text).encode()) <= 48: return text
        text = text[:45]  # JSON encoding never uses fewer bytes than characters.
        while len(canonical(text + '...').encode()) > 48:
            text = text[:-1]
        return text + '...'
    def index(name, links):
        # Bounded nested groups keep the mandatory root small at any journal
        # size. The full path/text lives in a leaf, never in an abbreviated label.
        def fits(items):
            return len(canonical({'ref': '0'*64, 'kind': 'index', 'label': name, 'links': items}).encode()) <= 512
        level = links
        while not fits(level):
            groups, current = [], []
            for link in level:
                if current and not fits(current + [link]):
                    groups.append(current); current = []
                if not fits([link]):
                    raise ContextBudgetExceeded('A mandatory working-memory index address cannot fit')
                current.append(link)
            if current: groups.append(current)
            if len(groups) >= len(level):
                raise ContextBudgetExceeded('The working-memory directory cannot be nested within its page bound')
            level = [{'label': name + ' ' + str(n + 1),
                      'ref': put({'kind': 'index', 'label': name, 'links': group})}
                     for n, group in enumerate(groups)]
        return put({'kind': 'index', 'label': name, 'links': level})
    notes = _notes(accepted)
    note_links = []
    for ordinal, ((iid, packet, proposal), note) in enumerate(zip(accepted, notes)):
        ref = put({'kind': 'working_note_unverified', 'checkpoint': iid,
                   'ordinal': ordinal + 1, 'note': copy.deepcopy(note)})
        note_refs[iid] = ref
        note_links.append({'label': label(str(ordinal + 1) + ': ' + note['summary']), 'ref': ref})
    source_links = []
    for record in receipts:
        if record['operation'].get('invocation_id') not in note_refs:
            raise InvalidUpdate('Working-memory source receipt is outside its accepted journal')
        groups = {}
        for entry in record['result'].get('excerpts', []):
            groups.setdefault(entry['path'], []).append(entry)
        for path, entries in sorted(groups.items()):
            ref = put({'kind': 'source_page_index', 'path': path,
                       'receipt': record['id'], 'operation': record['operation']['kind'],
                       'meaning': 'Saved source lookup; not reviewed or understood.',
                       'positions': [{'line': e['line'], 'column': e.get('column', 0)} for e in entries],
                       'units': len(entries)})
            source_refs.setdefault(path, []).append(ref)
            source_links.append({'label': label(path), 'ref': ref})
    # Keep historical *addresses* from their accepted deliveries. Missing
    # historical subtrees are reconstructed only when a known ref is requested,
    # rather than rebuilding every accepted prefix during every model step.
    old_roots = []
    for ordinal, (_, packet, _) in enumerate(accepted):
        if packet.get('discovery_memory_version') != 1: continue
        work['historical_roots_checked'] += 1
        saved = packet.get('discovery_memory_root') or {}
        body = {k: v for k, v in saved.items() if k != 'ref'}
        ref = sha('discovery-memory-v1:' + canonical([sid, key, body]))
        if saved.get('ref') != ref or saved.get('kind') != 'working_memory_root':
            raise InvalidUpdate('Historical working-memory root belongs to another journal')
        pages[ref] = copy.deepcopy(saved)
        old_roots.append({'label': str(ordinal), 'ref': ref})
    note_index = index('notes', note_links)
    source_index = index('sources', source_links)
    history_index = index('history', old_roots)
    root = put({'kind': 'working_memory_root', 'notes': len(notes), 'source_pages': len(source_links),
                'latest_note': note_refs[accepted[-1][0]] if accepted else None,
                'links': [{'label': 'notes', 'ref': note_index},
                          {'label': 'sources', 'ref': source_index},
                          {'label': 'history', 'ref': history_index}]})
    if len(canonical(pages[root]).encode()) > 512:
        raise ContextBudgetExceeded('The mandatory working-memory root cannot fit its page')
    return {'root': root, 'pages': pages, 'parents': parents, 'note_refs': note_refs,
            'source_refs': source_refs, 'work': work, 'resolved_prefixes': {}}


def _memory_resolve(graph, ref, sid, key, accepted, receipts):
    if ref in graph['pages']: return
    for count, (_, packet, _) in enumerate(accepted):
        if packet.get('discovery_memory_version') != 1: continue
        delivered = [packet['discovery_memory_root'], *packet.get('discovery_memory_pages', [])]
        known = {page['ref'] for page in delivered}
        known.update(link['ref'] for page in delivered for link in page.get('links', []))
        if ref not in known: continue
        if count not in graph['resolved_prefixes']:
            ids = {iid for iid, _, _ in accepted[:count]}
            prior = _memory_graph(sid, key, accepted[:count],
                [r for r in receipts if r['operation'].get('invocation_id') in ids])
            if prior['root'] != packet['discovery_memory_root']['ref']:
                raise InvalidUpdate('Historical working-memory directory changed')
            graph['resolved_prefixes'][count] = prior
        prior = graph['resolved_prefixes'][count]
        if ref not in prior['pages']: continue
        for address, page in prior['pages'].items():
            if address in graph['pages'] and canonical(graph['pages'][address]) != canonical(page):
                raise InvalidUpdate('Working-memory content address changed')
            graph['pages'][address] = copy.deepcopy(page)
        for address, parent in prior['parents'].items(): graph['parents'].setdefault(address, parent)
        return
    raise InvalidUpdate('Working-memory page was not advertised by this accepted journal')


def _memory_selection(packet, accepted, graph, sid, key, receipts):
    """Recall exact older notes from committed topic hints, rather than reread."""
    from review_context import retrieval_hints
    latest = accepted[-1][2] if accepted else {}
    last_tool = packet['repository'].get('last_tool') or {}
    active = packet.get('active_source_page') or {}
    text = '\n'.join([packet['request'], *latest.get('remaining', []),
                      canonical(last_tool), active.get('path', '')])
    paths, _ = retrieval_hints([text], graph['source_refs'])
    paths = list(dict.fromkeys([*( [active['path']] if active.get('path') else []), *paths]))
    note_ids = [accepted[-1][0]] if accepted else []
    refs = []
    # Earlier accepted deliveries that actually carried this file take
    # precedence over a note merely requesting its first read.
    for path in paths[:2]:
        candidates = [iid for iid, p, _ in accepted[:-1]
                      if any(e['path'] == path for e in p['evidence'])]
        if not candidates:
            candidates = [iid for iid, _, proposal in accepted[:-1]
                          if proposal['tool'].get('target') == path]
        if candidates: note_ids.append(candidates[0])
        saved = graph['source_refs'].get(path, [])
        refs.extend(saved[:1] + saved[-1:])
    # Existing remaining text can name a known page address. It never grants
    # access to a foreign journal, scope or file.
    advertised = set(graph['pages'])
    for _, old, _ in accepted:
        if old.get('discovery_memory_version') != 1: continue
        delivered = [old['discovery_memory_root'], *old.get('discovery_memory_pages', [])]
        advertised.update(page['ref'] for page in delivered)
        advertised.update(link['ref'] for page in delivered for link in page.get('links', []))
    for ref in sorted(advertised):
        if ref in text:
            _memory_resolve(graph, ref, sid, key, accepted, receipts)
            refs.append(ref)
    wanted = set(refs) | {graph['note_refs'][iid] for iid in note_ids}
    selected = {graph['root']}
    for ref in wanted:
        while ref not in selected:
            selected.add(ref); ref = graph['parents'][ref]
    note_ids = [iid for iid, _, _ in accepted if iid in set(note_ids)]
    def depth(ref):
        level = 0
        while ref != graph['root']:
            level += 1; ref = graph['parents'][ref]
        return level
    return note_ids, sorted(selected, key=lambda ref: (depth(ref), ref))


def _bind_memory(packet, sid, key, accepted, receipts):
    graph = _memory_graph(sid, key, accepted, receipts)
    note_ids, selected = _memory_selection(packet, accepted, graph, sid, key, receipts)
    packet['discovery_memory_root'] = copy.deepcopy(graph['pages'][graph['root']])
    packet['discovery_memory_pages'] = [copy.deepcopy(graph['pages'][ref]) for ref in selected
                                         if ref != graph['root']]
    packet['discovery_memory_note_ids'] = note_ids
    packet['discovery_notes_unverified'] = [note for (iid, _, _), note in zip(accepted, _notes(accepted))
                                             if iid in set(note_ids)]
    packet['discovery_notes_start'] = 0


def _check_memory(s, sid, packet, accepted):
    receipts = _tool_receipts(s, sid, accepted)
    # Even an evicted page's index must refer to current in-scope receipts.
    # Lookup addresses never bypass repository freshness or task permissions.
    _entries(s, sid, packet['workspace'], receipts)
    expected = copy.deepcopy(packet)
    _bind_memory(expected, sid, packet['discovery_key'], accepted, receipts)
    for name in ('discovery_memory_root', 'discovery_memory_pages', 'discovery_memory_note_ids',
                 'discovery_notes_unverified', 'discovery_notes_start'):
        if canonical(packet.get(name)) != canonical(expected.get(name)):
            raise InvalidUpdate('Discovery working-memory journal changed: ' + name)


def _source_recall(packet, accepted, entries, fresh_units=()):
    """Choose whole verified source units, never a semantic-completion claim."""
    from review_context import EvidenceFocus, retrieval_hints
    latest = '\n'.join(accepted[-1][2]['remaining']) if accepted else ''
    recent, user = EvidenceFocus(latest), EvidenceFocus(packet['request'])
    fresh_ids = {entry['id'] for entry in fresh_units}
    unread = set((packet.get('active_source_page') or {}).get('unit_ids', []))
    leaves = [page for page in packet['discovery_memory_pages'] if page['kind'] == 'source_page_index']
    by_path = {}
    for page in leaves:
        for entry in entries:
            if (entry['receipt'] == page['receipt'] and entry['path'] == page['path']
                    and (entry['id'] not in unread or entry['id'] in fresh_ids)):
                by_path.setdefault(entry['path'], {})[entry['id']] = entry
    preferred, _ = retrieval_hints([latest, packet['request']], by_path)
    paths = preferred + sorted(set(by_path) - set(preferred))
    required, focused = [], []
    for path in paths:
        addresses = [page for page in leaves if page['path'] == path]
        explicit_receipts = {page['receipt'] for page in addresses if page['ref'] in latest}
        names = '|'.join(re.escape(name) for name in {path, Path(path).name})
        lines = {int(n) for n in re.findall('(?:' + names + r')(?:#L|:|\s+line\s+)(\d+)', latest, re.I)}
        def score(entry):
            end = entry.get('end_position')
            if not isinstance(end, dict):
                parts = (entry['text'] + '\0').splitlines(True)
                end = {'line': entry['line'] + len(parts) - 1,
                       'column': (entry.get('column', 0) if len(parts) == 1 else 0) + len(parts[-1]) - 1}
            last_line = end['line'] if end['column'] else end['line'] - 1
            exact_line = any(entry['line'] <= line <= last_line for line in lines)
            return (not exact_line, entry['receipt'] not in explicit_receipts,
                    -recent.strength(entry['text']), -user.strength(entry['text']),
                    entry['id'] not in fresh_ids, entry['line'], entry.get('column', 0), entry['id'])
        ordered = sorted(by_path[path].values(), key=score)
        required.append(ordered[0])
        focused.extend(entry['id'] for entry in ordered)
    descriptor = {'required_unit_ids': [entry['id'] for entry in required],
                  'focused_unit_ids': focused,
                  'source_refs': sorted({page['ref'] for page in leaves})}
    return descriptor, required


def _recall_order(older, required, focused, prefix):
    protected = {entry['id'] for entry in required + prefix}
    available = {entry['id']: entry for entry in older if entry['id'] not in protected}
    promoted = [available[eid] for eid in focused if eid in available]
    focused_ids = set(focused)
    optional = [entry for entry in older if entry['id'] in available and entry['id'] not in focused_ids]
    prefix_ids = {entry['id'] for entry in prefix}
    return optional + promoted + [entry for entry in required if entry['id'] not in prefix_ids] + prefix


def _check_source_recall(s, sid, packet, accepted, receipts, older, prefix, fresh_units):
    entries = _entries(s, sid, packet['workspace'], receipts)
    expected, required = _source_recall(packet, accepted, entries, fresh_units)
    if canonical(packet.get('discovery_source_recall')) != canonical(expected):
        raise InvalidUpdate('Discovery recalled source selection changed')
    fresh_minimum = max([1] + [n + 1 for n, entry in enumerate(fresh_units)
                              if entry['id'] in set(expected['required_unit_ids'])]) if fresh_units else 0
    if len(prefix) < fresh_minimum:
        raise InvalidUpdate('Discovery fresh prefix omits a required source focus unit')
    ordered = _recall_order(older, required, expected['focused_unit_ids'], prefix)
    floor = _recall_order([], required, [], prefix)
    supplied = packet['evidence']
    suffix = ordered[len(ordered)-len(supplied):] if supplied else []
    if len(supplied) < len(floor) or canonical(supplied) != canonical(suffix):
        raise InvalidUpdate('Discovery recalled source floor or ordering changed')


def _shrink_optional(packet, data, request, latest, active, prefix, pages, progress_rows=None):
    """Retire reconstructible context before any newly earned source unit."""
    from chat_runtime import excerpts
    from review_context import discovery_catalog, discovery_symbols, overview
    if packet['discovery_notes_unverified'] and packet.get('discovery_memory_version') != 1:
        packet['discovery_notes_unverified'].pop(0); packet['discovery_notes_start'] += 1
    elif packet['discovery_catalog_bytes'] > 512:
        packet['discovery_catalog_bytes'] = max(512, packet['discovery_catalog_bytes'] * 4 // 5)
        packet['catalog_window'] = discovery_catalog(data, packet['repository']['catalog'],
            packet['discovery_catalog_bytes'], request, latest)
    elif packet['discovery_map_bytes'] > 512:
        packet['discovery_map_bytes'] = max(512, packet['discovery_map_bytes'] * 4 // 5)
        packet['workspace_map'] = overview(data, packet['discovery_map_bytes'], request, version=3)
    elif packet['discovery_symbol_bytes'] > 512:
        packet['discovery_symbol_bytes'] = max(512, packet['discovery_symbol_bytes'] * 4 // 5)
        packet['readable_symbols'] = discovery_symbols(data, packet['discovery_symbol_bytes'], request, latest)
    elif packet['discovery_page_toc_bytes'] > 512:
        packet['discovery_page_toc_bytes'] = max(512, packet['discovery_page_toc_bytes'] * 4 // 5)
        packet['source_page_toc'] = _page_toc(pages, packet['discovery_page_toc_bytes'])
    elif packet.get('discovery_progress_version') == 1 and packet['discovery_progress_bytes'] > 512:
        packet['discovery_progress_bytes'] = max(512, packet['discovery_progress_bytes'] * 4 // 5)
        packet['supplied_source_progress'] = _progress_view(progress_rows, packet['discovery_progress_bytes'])
    elif packet['discovery_child_start'] < len(packet.get('child_results', [])):
        packet['discovery_child_start'] += 1
    elif packet['initial_excerpt_bytes'] > 64:
        packet['initial_excerpt_bytes'] = max(64, packet['initial_excerpt_bytes'] * 4 // 5)
        packet['initial_excerpts'] = excerpts(data['sources'], request, packet['initial_excerpt_bytes'],
                                               data['graph'], whole_lines=True, recipe_version=3)
    elif packet.get('discovery_source_recall_version') == 1:
        protected = set(packet['discovery_source_recall']['required_unit_ids']) | {entry['id'] for entry in prefix}
        if not packet['evidence'] or packet['evidence'][0]['id'] in protected: return False
        packet['evidence'].pop(0)
    elif len(packet['evidence']) > len(prefix):
        packet['evidence'].pop(0)
    else:
        return False
    return True


def read_paths(s, sid, p):
    # Readiness uses successful, current read receipts. Search hits and model
    # notes cannot become inspected-file coverage.
    _, key = scope(s, sid, p)
    accepted = _accepted(s, sid, history(s, sid, key))
    receipts = [record for record in _tool_receipts(s, sid, accepted)
                if record['operation']['kind'] == 'read' and record['result']['status'] == 'ok']
    return sorted({entry['path'] for entry in _entries(s, sid, p['workspace'], receipts)
                   if entry['text'].strip()})


def _coverage(s, sid, p, accepted, receipts, reason):
    result = {'stop_reason': reason, 'model_steps': len(accepted),
              'tool_results': len(receipts), 'exhaustive_semantic_review': False}
    if p.get('discovery_selection_version') in (1, 2, 3):
        result['read_files'] = read_paths(s, sid, p)
    return result


def _finish(s, sid, p, key, accepted, receipts, reason):
    checkpoint = _state(key, accepted, receipts, complete=True, reason=reason)
    p['evidence_discovery'] = checkpoint
    p['discovery_notes_unverified'] = _notes(accepted)
    p['discovery_coverage'] = _coverage(s, sid, p, accepted, receipts, reason)
    record = {'session': sid, 'owner': p['snapshot_owner'], 'checkpoint': checkpoint}
    if not s.db.execute("SELECT 1 FROM page_audit WHERE event='PARENT_DISCOVERY_CHECKPOINT' AND payload_json=?",
                        (canonical(record),)).fetchone():
        with s.db: s._audit('PARENT_DISCOVERY_CHECKPOINT', record)
    return {'complete': True, 'checkpoint': checkpoint, 'followups': load(s, sid, p),
            'token_usage': None}


def load(s, sid, p):
    checkpoint = p.get('evidence_discovery')
    if not checkpoint: return {}
    _, key = scope(s, sid, p)
    records = history(s, sid, key)
    accepted = _accepted(s, sid, records)
    receipts = _tool_receipts(s, sid, accepted)
    expected = _state(key, accepted, receipts, complete=checkpoint.get('complete'), reason=checkpoint.get('stop_reason'))
    if checkpoint != expected or not checkpoint['complete']:
        raise InvalidUpdate('Evidence discovery checkpoint changed')
    audit = s.db.execute("SELECT 1 FROM page_audit WHERE event='PARENT_DISCOVERY_CHECKPOINT' "
                         "AND payload_json=?", (canonical({'session': sid, 'owner': p['snapshot_owner'],
                                                          'checkpoint': checkpoint}),)).fetchone()
    if not audit: raise InvalidUpdate('Evidence discovery checkpoint was not committed')
    if (p.get('discovery_notes_unverified') != _notes(accepted)
            or p.get('discovery_coverage') != _coverage(s, sid, p, accepted, receipts, checkpoint['stop_reason'])):
        raise InvalidUpdate('Evidence discovery notes or coverage changed')
    result = {}
    # Keep a real contiguous excerpt. Nonadjacent spans never become one fake
    # source location; earlier tool results remain in the immutable receipts.
    reads = [record for record in receipts if record['operation']['kind'] == 'read']
    for entry in _entries(s, sid, p['workspace'], reads):
        if not entry['text'].strip(): continue
        current = dict(entry)
        record = next(r for r in receipts if r['id'] == entry['receipt'])
        cursor = record['result'].get('next_cursor')
        current['line_complete'] = (entry['line_complete'] if record['operation'].get('read_profile')
                                    else cursor is None or cursor['column'] == 0)
        old = result.get(entry['path'])
        joined_units = False
        if (record['operation'].get('read_profile') and old
                and old['sha256'] == current['sha256']
                and old.get('end_position') == {'line': current['line'], 'column': current.get('column', 0)}):
            # Verified adjacent units can reconstruct a long source line.
            # Nonadjacent windows never acquire an invented contiguous span.
            current['text'] = old['text'] + current['text']
            current['line'] = old['line']; current['column'] = old.get('column', 0)
            joined_units = True
        if current.get('column', 0):
            newline = current['text'].find('\n')
            if newline < 0: continue
            current['text'] = current['text'][newline+1:]
            current['line'] += 1; current['column'] = 0
            if not current['text'].strip(): continue
        if (old and not joined_units
                and old['sha256'] == current['sha256'] and old.get('column', 0) == 0
                and current.get('column', 0) == 0
                and old['line'] + old['text'].count('\n') == current['line']
                and old['text'].endswith('\n')):
            current['text'] = old['text'] + current['text']; current['line'] = old['line']
        result[entry['path']] = current
    return result


def check_request(s, sid, p):
    owner, key = scope(s, sid, p)
    if p.get('discovery_key') != key or p.get('evidence_discovery_version') != VERSION:
        raise InvalidUpdate('Evidence discovery request key changed')
    names = repo.catalog(s, sid)
    machine.scope_check(owner, 'repository', names)
    if (p['repository']['catalog'] != names or p['repository']['total_files'] != len(names)
            or p['repository']['budget'] != repo.usage(s, sid)
            or p['repository'].get('last_tool') != repo.feedback(s, sid)):
        raise InvalidUpdate('Evidence discovery catalog or tool feedback changed')
    if p['repository']['operations_remaining'] != max(0, repo.usage(s, sid)['limit'] - repo.usage(s, sid)['used']):
        raise InvalidUpdate('Evidence discovery operation allocation changed')
    accepted = _accepted(s, sid, history(s, sid, key))
    previous = accepted[-1][0] if accepted else None
    if p.get('discovery_previous') != previous or p.get('derived_from') != ([previous] if previous else []):
        raise InvalidUpdate('Evidence discovery previous checkpoint changed')
    if 'discovery_memory_version' in p:
        if (type(p['discovery_memory_version']) is not int or p['discovery_memory_version'] != 1
                or type(p.get('discovery_selection_version')) is not int
                or p['discovery_selection_version'] != 3):
            raise InvalidUpdate('Unsupported discovery working-memory profile')
        _check_memory(s, sid, p, accepted)
    else:
        if any(name in p for name in ('discovery_memory_root', 'discovery_memory_pages', 'discovery_memory_note_ids')):
            raise InvalidUpdate('Discovery working-memory journal requires its profile')
        start = p.get('discovery_notes_start', 0)
        if type(start) is not int or not 0 <= start <= len(accepted): raise InvalidUpdate('Invalid discovery note window')
        if p.get('discovery_notes_unverified') != _notes(accepted)[start:]:
            raise InvalidUpdate('Evidence discovery working notes changed')
    if p.get('source_paths') != sorted({e['path'] for e in p['evidence']}):
        raise InvalidUpdate('Evidence discovery source scope changed')
    machine.scope_check(owner, 'repository', p['source_paths'])
    repo.validate_evidence(s, sid, Path(p['workspace']), p['evidence'])
    if 'discovery_source_recall_version' in p:
        if (type(p['discovery_source_recall_version']) is not int or p['discovery_source_recall_version'] != 1
                or type(p.get('discovery_memory_version')) is not int or p['discovery_memory_version'] != 1
                or type(p.get('discovery_selection_version')) is not int or p['discovery_selection_version'] != 3):
            raise InvalidUpdate('Unsupported discovery source recall profile')
    elif 'discovery_source_recall' in p:
        raise InvalidUpdate('Discovery source recall needs its versioned profile')
    from static_runtime import load_partition
    from chat_runtime import excerpts
    initial = p.get('initial_excerpt_bytes')
    if type(initial) is not int or not 64 <= initial <= 65536:
        raise InvalidUpdate('Invalid discovery initial source allocation')
    data = load_partition(s, sid, p['workspace'], p['snapshot_receipts'], p['snapshot_owner'])
    if p.get('initial_excerpts') != excerpts(data['sources'], p['request'], initial,
                                           data['graph'], whole_lines=True, recipe_version=3):
        raise InvalidUpdate('Discovery initial source snapshot changed')
    from review_context import overview
    map_bytes = p.get('discovery_map_bytes')
    if type(map_bytes) is not int or not 512 <= map_bytes <= 8192:
        raise InvalidUpdate('Invalid discovery symbol-index allocation')
    if p.get('workspace_map') != overview(data, map_bytes, p['request'], version=3):
        raise InvalidUpdate('Discovery source index changed')
    selection_version = p.get('discovery_selection_version')
    for marker, expected in (('discovery_note_version', 2), ('discovery_progress_version', 1),
                             ('discovery_search_version', 1), ('discovery_reasoning_version', 1)):
        if marker in p and (type(p[marker]) is not int or p[marker] != expected
                            or type(selection_version) is not int or selection_version != 3):
            raise InvalidUpdate('Unsupported discovery note or progress profile')
    if p.get('discovery_progress_version') == 1:
        progress_bytes = p.get('discovery_progress_bytes')
        if type(progress_bytes) is not int or not 512 <= progress_bytes <= 2048:
            raise InvalidUpdate('Invalid supplied source progress allocation')
        expected = supplied_source_progress(s, sid, p['workspace'],
            _tool_receipts(s, sid, accepted), accepted, progress_bytes)
        if p.get('supplied_source_progress') != expected:
            raise InvalidUpdate('Previously supplied source progress changed')
    elif any(key in p for key in ('discovery_progress_bytes', 'supplied_source_progress')):
        raise InvalidUpdate('Supplied source progress needs its versioned profile')
    if selection_version in (2, 3):
        if p.get('read_profile') != repo.discovery_read_profile(p['memory_budget']):
            raise InvalidUpdate('Discovery read page allocation changed')
        symbol_bytes = p.get('discovery_symbol_bytes')
        if type(symbol_bytes) is not int or not 512 <= symbol_bytes <= 8192:
            raise InvalidUpdate('Invalid discovery symbol address allocation')
        from review_context import discovery_symbols
        latest = '\n'.join(accepted[-1][2]['remaining']) if accepted else ''
        if p.get('readable_symbols') != discovery_symbols(data, symbol_bytes, p['request'], latest):
            raise InvalidUpdate('Discovery source symbol addresses changed')
        if selection_version == 3:
            from review_context import discovery_catalog
            catalog_bytes = p.get('discovery_catalog_bytes')
            if type(catalog_bytes) is not int or not 512 <= catalog_bytes <= 4096:
                raise InvalidUpdate('Invalid discovery catalog window allocation')
            if p.get('catalog_window') != discovery_catalog(data, names, catalog_bytes, p['request'], latest):
                raise InvalidUpdate('Discovery catalog window changed')
            toc_bytes = p.get('discovery_page_toc_bytes')
            if type(toc_bytes) is not int or not 512 <= toc_bytes <= 2048:
                raise InvalidUpdate('Invalid discovery prior source page allocation')
            current_receipts = _tool_receipts(s, sid, accepted)
            older, active, units, pages = _page_working_set(s, sid, p['workspace'], current_receipts, accepted)
            saved = p.get('active_source_page')
            if active is None:
                if saved is not None: raise InvalidUpdate('Discovery active source page changed')
            else:
                count = len(saved.get('prefix', [])) if isinstance(saved, dict) else 0
                if (not 1 <= count <= len(units) or saved != _page_view(active, units[:count])
                        or p['evidence'][-count:] != units[:count]):
                    raise InvalidUpdate('Discovery active source prefix changed')
            if (len({e['id'] for e in p['evidence']}) != len(p['evidence'])
                    or p.get('source_page_toc') != _page_toc(pages, toc_bytes)):
                raise InvalidUpdate('Discovery source page working set changed')
            if p.get('discovery_source_recall_version') == 1:
                _check_source_recall(s, sid, p, accepted, current_receipts, older,
                                     units[:len(saved['prefix'])] if active else [], units)
    elif selection_version != 1:
        raise InvalidUpdate('Unsupported discovery selection profile')
    child_start = p.get('discovery_child_start', 0)
    if type(child_start) is not int or not 0 <= child_start <= len(p.get('child_results', [])):
        raise InvalidUpdate('Invalid discovery child-note window')
    if p.get('child_results'):
        from subagent_runtime import joined_results
        expected = joined_results(s, sid, p['orchestration_parent'],
            [item['invocation_id'] for item in p['child_results']], p['request'], note_version=3)
        if p['child_results'] != expected:
            raise InvalidUpdate('Discovery child retrieval context changed')
    if p.get('memory') != memory(s, p, Budget(**p['memory_budget'])):
        raise InvalidUpdate('Discovery pinned conversation context changed')


def advance(s, sid, p, data, root, provider, real):
    p['discovery_owner'] = machine.ACTIVE.get()
    p['discovery_selection_version'] = SELECTION_VERSION
    machine.checked(s, p['discovery_owner'])
    owner, key = scope(s, sid, p)
    if machine.ACTIVE.get() != owner['id']: raise InvalidUpdate('Evidence discovery requires its parent execution')
    accepted = _accepted(s, sid, history(s, sid, key))
    receipts = _tool_receipts(s, sid, accepted)
    terminal = s.db.execute("SELECT payload_json FROM page_audit WHERE event='PARENT_DISCOVERY_CHECKPOINT' "
        "AND json_extract(payload_json,'$.checkpoint.key')=? ORDER BY seq DESC LIMIT 1", (key,)).fetchone()
    if terminal:
        checkpoint = json.loads(terminal[0])['checkpoint']
        if checkpoint != _state(key, accepted, receipts, complete=True, reason=checkpoint['stop_reason']):
            raise InvalidUpdate('Completed discovery history changed')
        return _finish(s, sid, p, key, accepted, receipts, checkpoint['stop_reason'])
    # Recover the accepted-decision -> repository-effect gap before any new call.
    missing = [(iid, packet, proposal) for iid, packet, proposal in accepted
               if proposal['tool']['kind'] in ('read', 'search', 'list')
               and not any(r['operation']['invocation_id'] == iid for r in receipts)]
    if missing:
        iid, decision, proposal = missing[0]
        tool = proposal['tool']
        from review_budget import BudgetStop
        try:
            receipt = repo.execute(s, sid, p['workspace'], root, tool['kind'], target=tool['target'],
                query=tool['query'], line=tool['line'], cursor=tool.get('cursor'), invocation_id=iid,
                request_id=_request_id(iid), resource_check=real,
                read_profile=decision.get('read_profile') if tool['kind'] == 'read' else None)
        except (BudgetStop, machine.OrchestrationStop):
            if decision.get('discovery_selection_version') in (1, 2, 3): raise
            return _finish(s, sid, p, key, accepted, receipts, 'budget_boundary')
        except repo.SourceReadLimitExceeded:
            return _finish(s, sid, p, key, accepted, receipts, 'operation_failed')
        receipts = _tool_receipts(s, sid, accepted)
        if receipt['result']['status'] != 'ok':
            return _finish(s, sid, p, key, accepted, receipts, 'operation_failed')
        return {'complete': False, 'checkpoint': _state(key, accepted, receipts), 'token_usage': None, 'result': None}
    if accepted and accepted[-1][2]['tool']['kind'] in ('report', 'clarify'):
        return _finish(s, sid, p, key, accepted, receipts,
                       'evidence_complete' if accepted[-1][2]['tool']['kind'] == 'report' else 'clarification')
    previous = accepted[-1][0] if accepted else None
    failed = [r for r in history(s, sid, key) if r[2]['status'] != 'accepted'
              and r[1].get('discovery_previous') == previous]
    if len(failed) >= 3:
        return _finish(s, sid, p, key, accepted, receipts, 'proposal_rejected')
    remaining_ops = repo.usage(s, sid)
    if remaining_ops['decision'] != 'ALLOW': return _finish(s, sid, p, key, accepted, receipts, 'budget_boundary')
    old_budget, old_thinking = provider.budget, getattr(provider, 'thinking', None)
    try:
        if real:
            from review_context import context_budget
            context_budget(provider, s, sid, slots=3, expand_output=False)
            provider.budget = provider.budget.with_limits(response_reserve=min(2048, provider.budget.response_reserve))
        from review_context import overview, discovery_catalog
        from chat_runtime import excerpts
        packet = {k: copy.deepcopy(p[k]) for k in ('contract', 'workspace', 'request', 'snapshot_owner', 'snapshot_receipts', 'discovery_owner')}
        if p.get('conversation_handoff'): packet['conversation_handoff'] = copy.deepcopy(p['conversation_handoff'])
        packet.update(evidence_discovery_version=VERSION, discovery_selection_version=SELECTION_VERSION,
            discovery_key=key, discovery_previous=previous,
            phase='explore', pages=[], derived_from=[previous] if previous else [],
            handoff=__import__('review_role').compact(accepted[-1][2]) if accepted else None,
            repository={'catalog': repo.catalog(s, sid), 'total_files': len(repo.catalog(s, sid)),
                        'budget': remaining_ops, 'operations_remaining': max(0, remaining_ops['limit']-remaining_ops['used']),
                        'last_tool': repo.feedback(s, sid)},
            workspace_map=overview(data, min(8192, max(512, provider.budget.input_limit // 8)), p['request'], version=3),
            evidence=_entries(s, sid, p['workspace'], receipts), discovery_notes_unverified=_notes(accepted),
            discovery_notes_start=0)
        if SELECTION_VERSION == 3:
            packet.update(discovery_note_version=2, discovery_progress_version=1,
                          discovery_progress_bytes=1024)
            if MEMORY_VERSION == 1:
                packet['discovery_memory_version'] = 1
                if SOURCE_RECALL_VERSION == 1: packet['discovery_source_recall_version'] = 1
            if SEARCH_VERSION == 1:
                packet['discovery_search_version'] = 1
            if REASONING_VERSION == 1:
                packet['discovery_reasoning_version'] = 1
        packet['initial_excerpt_bytes'] = max(64, min(65536, provider.budget.input_limit // 4))
        packet['initial_excerpts'] = excerpts(data['sources'], p['request'], packet['initial_excerpt_bytes'],
                                               data['graph'], whole_lines=True, recipe_version=3)
        if p.get('child_results'):
            packet.update(child_results=copy.deepcopy(p['child_results']), parent_context_version=3,
                          orchestration_parent=p['orchestration_parent'])
        packet['discovery_map_bytes'] = min(8192, max(512, provider.budget.input_limit // 8))
        packet['read_profile'] = repo.discovery_read_profile(vars(provider.budget))
        packet['discovery_symbol_bytes'] = min(4096, max(512, provider.budget.input_limit // 16))
        from review_context import discovery_symbols
        latest = '\n'.join(accepted[-1][2]['remaining']) if accepted else ''
        packet['readable_symbols'] = discovery_symbols(data, packet['discovery_symbol_bytes'], p['request'], latest)
        if SELECTION_VERSION == 3:
            progress_rows = _supplied_progress_rows(s, sid, p['workspace'], receipts, accepted)
            packet['supplied_source_progress'] = _progress_view(progress_rows, packet['discovery_progress_bytes'])
            older, active, prefix, pages = _page_working_set(s, sid, p['workspace'], receipts, accepted)
            packet['evidence'] = older + prefix
            packet['active_source_page'] = _page_view(active, prefix)
            packet['discovery_catalog_bytes'] = min(4096, max(512, provider.budget.input_limit // 16))
            packet['catalog_window'] = discovery_catalog(data, packet['repository']['catalog'],
                packet['discovery_catalog_bytes'], p['request'], latest)
            packet['discovery_page_toc_bytes'] = 2048
            packet['source_page_toc'] = _page_toc(pages, packet['discovery_page_toc_bytes'])
            if packet.get('discovery_memory_version') == 1:
                _bind_memory(packet, sid, key, accepted, receipts)
            if packet.get('discovery_source_recall_version') == 1:
                packet['discovery_source_recall'], recalled = _source_recall(packet, accepted,
                    _entries(s, sid, p['workspace'], receipts), prefix)
                packet['evidence'] = _recall_order(older, recalled,
                    packet['discovery_source_recall']['focused_unit_ids'], prefix)
        packet['discovery_child_start'] = 0
        if failed: packet['repair'] = {'errors': failed[-1][2]['admission']['errors'][:2]}
        from review_reasoning import bind
        bind(provider, packet, bounded_selection=True)
        while True:
            if packet.get('discovery_reasoning_version') == 1:
                bind(provider, packet, bounded_selection=True)
            packet['source_paths'] = sorted({e['path'] for e in packet['evidence']})
            packet['memory_budget'] = vars(provider.budget).copy()
            fmt = role.schema(packet)
            try:
                packet['memory'] = memory(s, packet, provider.budget, materialize=True)
                charge = provider.budget.check(role.SYSTEM, packet, fmt)
                from context_window import check
                check(provider.budget, role.SYSTEM, packet, fmt, planning=True)
                break
            except ContextBudgetExceeded as exc:
                if packet['discovery_selection_version'] == 3:
                    if _shrink_optional(packet, data, p['request'], latest, active, prefix, pages, progress_rows):
                        continue
                    physical_fit = _request_funding(s, sid, owner, provider.budget, packet, fmt, exc,
                                                    provider=provider)
                    minimum_prefix = 1
                    if packet.get('discovery_source_recall_version') == 1:
                        required_ids = set(packet['discovery_source_recall']['required_unit_ids'])
                        minimum_prefix = max([1] + [n + 1 for n, entry in enumerate(prefix)
                                                   if entry['id'] in required_ids])
                    if physical_fit or not active or len(prefix) <= minimum_prefix:
                        raise
                    # Financial shortage never retires newly earned source.
                    # A smaller physical window may receive only a coherent
                    # prefix, with its actual next unread position explicit.
                    prefix.pop()
                    packet['evidence'] = (_recall_order([], recalled, [], prefix)
                        if packet.get('discovery_source_recall_version') == 1 else prefix.copy())
                    packet['active_source_page'] = _page_view(active, prefix)
                elif packet['discovery_notes_unverified']:
                    packet['discovery_notes_unverified'].pop(0); packet['discovery_notes_start'] += 1
                elif packet['initial_excerpt_bytes'] > 64:
                    packet['initial_excerpt_bytes'] = max(64, packet['initial_excerpt_bytes'] * 4 // 5)
                    packet['initial_excerpts'] = excerpts(data['sources'], p['request'], packet['initial_excerpt_bytes'],
                                                           data['graph'], whole_lines=True, recipe_version=3)
                elif packet['discovery_map_bytes'] > 512:
                    packet['discovery_map_bytes'] = max(512, packet['discovery_map_bytes'] * 4 // 5)
                    packet['workspace_map'] = overview(data, packet['discovery_map_bytes'], p['request'], version=3)
                elif packet['discovery_symbol_bytes'] > 512:
                    packet['discovery_symbol_bytes'] = max(512, packet['discovery_symbol_bytes'] * 4 // 5)
                    packet['readable_symbols'] = discovery_symbols(data, packet['discovery_symbol_bytes'], p['request'], latest)
                elif packet['discovery_child_start'] < len(packet.get('child_results', [])):
                    packet['discovery_child_start'] += 1
                elif len(packet['evidence']) > 1: packet['evidence'].pop(0)
                else:
                    _request_funding(s, sid, owner, provider.budget, packet, fmt, exc)
                    raise
        # Leave financial/call capacity for the writer and its claim reviewer.
        frames = machine.gates(s, owner['id'], _protected_cost(provider.budget, packet, fmt))
        if any(frame['decision'] != 'ALLOW' for frame in frames):
            if packet.get('discovery_selection_version') == 3 or (packet.get('discovery_selection_version') in (1, 2) and provider.budget.context_profile == 2):
                machine.allowed(frames)
            return _finish(s, sid, p, key, accepted, receipts, 'budget_boundary')
        from review_budget import require, BudgetStop
        try:
            require(s, sid, _protected_cost(provider.budget, packet, fmt))
        except BudgetStop:
            if packet.get('discovery_selection_version') == 3 or (packet.get('discovery_selection_version') in (1, 2) and provider.budget.context_profile == 2): raise
            return _finish(s, sid, p, key, accepted, receipts, 'budget_boundary')
        from invocation_gateway import InvocationGateway
        run = s.begin_run(sid, p['request'], [], None, max_calls=1, max_repairs=0)
        s.reserve(run, 'ATTEMPT', {'attempt': len(failed)})
        delivery = s.prepare_delivery(sid, packet, charge)
        result = InvocationGateway(s.path, run, provider, ledger=s).invoke('architect', uuid.uuid4().hex,
            role.SYSTEM, packet, fmt, delivery)
        usage = result.get('token_usage')
        if not result['admission']['admitted']:
            s.event(run, 'FIN', {'reason': 'discovery_proposal_withheld'})
            return {'complete': False, 'checkpoint': _state(key, accepted, receipts), 'token_usage': usage, 'result': result}
        s.event(run, 'ALLOW', {'reason': 'discovery_decision', 'invocation_id': result['invocation_id']})
        # A durable break after the decision prevents multiple expensive calls
        # in one bridge request. The next step recovers/executes this exact tool.
        accepted = _accepted(s, sid, history(s, sid, key))
        tool = result['accepted_output']['tool']
        if tool['kind'] in ('read', 'search', 'list'):
            try:
                receipt = repo.execute(s, sid, p['workspace'], root, tool['kind'], target=tool['target'],
                    query=tool['query'], line=tool['line'], cursor=tool.get('cursor'), invocation_id=result['invocation_id'],
                    request_id=_request_id(result['invocation_id']), resource_check=real,
                    read_profile=packet.get('read_profile') if tool['kind'] == 'read' else None)
            except (BudgetStop, machine.OrchestrationStop):
                if packet.get('discovery_selection_version') in (1, 2, 3): raise
                finished = _finish(s, sid, p, key, accepted, receipts, 'budget_boundary')
                finished.update(token_usage=usage, result=result); return finished
            except repo.SourceReadLimitExceeded:
                finished = _finish(s, sid, p, key, accepted, receipts, 'operation_failed')
                finished.update(token_usage=usage, result=result); return finished
            receipts = _tool_receipts(s, sid, accepted)
            if receipt['result']['status'] != 'ok':
                finished = _finish(s, sid, p, key, accepted, receipts, 'operation_failed')
                finished.update(token_usage=usage, result=result); return finished
            return {'complete': False, 'checkpoint': _state(key, accepted, receipts), 'token_usage': usage, 'result': result}
        finished = _finish(s, sid, p, key, accepted, receipts,
            'evidence_complete' if tool['kind'] == 'report' else 'clarification')
        finished.update(token_usage=usage, result=result); return finished
    except ContextBudgetExceeded:
        if p.get('discovery_selection_version') == 3: raise
        return _finish(s, sid, p, key, accepted, receipts, 'budget_boundary')
    finally:
        provider.budget = old_budget
        provider.thinking = old_thinking
