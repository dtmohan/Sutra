"""Read-only replay of discovery receipts; never dispatches or resumes a task.

Example:
  python3 -B scripts/evaluate-discovery-governor.py --db /absolute/state.sqlite \
    --session review-example --flow FLOW_ID --output /private/tmp/replay.json

SQLite is opened mode=ro/query_only inside one snapshot. Stored invocation and
repository chains are checked without constructing a runtime ledger. Reports
contain counters and hashes, never source bodies or model notes. This verifies
the scoped receipt chains, not the entire protocol or an external audit anchor.
"""
import argparse
from collections import Counter
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import time


MAX_ROWS = 10000
# Historical static-map receipts can exceed 3 MiB. Bounds apply before parsing;
# graph bodies are discarded immediately after their receipt hash is checked.
MAX_ROW_BYTES = 8 * 1024**2
MAX_TOTAL_BYTES = 256 * 1024**2
FORMAT = 'discovery-governor-replay-v1'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def normalized_request(tool):
    """Generated invocation/receipt/evidence IDs cannot make a request novel."""
    if not isinstance(tool, dict):
        return None
    value = {key: tool.get(key, default) for key, default in
             (('kind', ''), ('target', ''), ('query', ''), ('line', 1))}
    if tool.get('cursor') is not None:
        cursor = tool['cursor']
        if not isinstance(cursor, dict):
            raise ValueError('Invalid stored cursor shape')
        value['cursor'] = {key: cursor.get(key) for key in ('line', 'column', 'sha256')}
    return value


class SpanCoverage:
    """Exact UTF-8 gain by (path, source hash, line, character column).

    Only interval endpoints survive ingestion. Text is used to charge newly
    observed substrings exactly, including Unicode and overlaps within a line.
    This measures repository receipt return values. Subsequent model-packet
    delivery, understanding and coverage quality are not established.
    """
    def __init__(self):
        self.lines = {}
        self.spans = set()
        self.source_versions = set()
        self.delivered_bytes = self.unique_bytes = self.excerpt_count = 0
        self.repeated_spans = 0
        self.unlocated_delivered_bytes = self.unlocated_unique_snippet_bytes = 0
        self.unlocated_excerpt_count = 0
        self.unlocated_snippets = set()

    def add(self, excerpt, located=True):
        path, version, text = (excerpt.get(key) for key in ('path', 'sha256', 'text'))
        line, column = excerpt.get('line'), excerpt.get('column', 0)
        if (not all(isinstance(item, str) for item in (path, version, text))
                or type(line) is not int or line < 1
                or type(column) is not int or column < 0):
            raise ValueError('Malformed source excerpt; refusing partial evidence counts')
        identity = (path, version)
        self.source_versions.add(identity)
        self.excerpt_count += 1
        self.delivered_bytes += len(text.encode())
        if not located:
            # Historical search receipts supply the line but omit the column
            # of their query-centred slice. Never invent position zero or merge
            # these snippets into the exact read-position byte union.
            fingerprint = identity + (line, digest(text))
            self.unlocated_delivered_bytes += len(text.encode())
            self.unlocated_excerpt_count += 1
            if fingerprint not in self.unlocated_snippets:
                self.unlocated_unique_snippet_bytes += len(text.encode())
            self.unlocated_snippets.add(fingerprint)
            return 0
        fingerprint = identity + (line, column, digest(text))
        self.repeated_spans += fingerprint in self.spans
        self.spans.add(fingerprint)
        gained = 0
        for piece in text.splitlines(keepends=True):
            start, end = column, column + len(piece)
            key = identity + (line,)
            intervals = self.lines.get(key, [])
            new_bytes = len(piece.encode())
            for left, right in intervals:
                lo, hi = max(start, left), min(end, right)
                if hi > lo:
                    new_bytes -= len(piece[lo-start:hi-start].encode())
            gained += new_bytes
            merged = []
            for left, right in sorted(intervals + [(start, end)]):
                if merged and left <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], right))
                else:
                    merged.append((left, right))
            self.lines[key] = merged
            if len(piece.splitlines()) != 1 or piece[-1:] in '\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029':
                line += 1
                column = 0
            else:
                column = end
        self.unique_bytes += gained
        return gained

    def summary(self):
        located_bytes = self.delivered_bytes-self.unlocated_delivered_bytes
        return {'excerpt_count': self.excerpt_count, 'unique_exact_spans': len(self.spans),
                'repeated_exact_spans': self.repeated_spans,
                'source_versions': len(self.source_versions),
                'source_paths': len({path for path, _ in self.source_versions}),
                'delivered_bytes': self.delivered_bytes, 'unique_bytes': self.unique_bytes,
                'located_delivered_bytes': located_bytes,
                'reread_bytes': located_bytes-self.unique_bytes,
                'reread_fraction': round(1-self.unique_bytes/located_bytes, 6) if located_bytes else 0,
                'unlocated_excerpt_count': self.unlocated_excerpt_count,
                'unlocated_delivered_bytes': self.unlocated_delivered_bytes,
                'unlocated_unique_snippet_bytes': self.unlocated_unique_snippet_bytes,
                'unlocated_snippet_overlap_with_reads': 'unknown; excluded from exact byte union',
                'measurement_scope': 'source returned in repository receipts; downstream model delivery unmeasured'}


class Snapshot:
    def __init__(self, path):
        self.path = Path(path).resolve(strict=True)
        self.db = sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True, timeout=2)
        self.db.execute('PRAGMA query_only=ON')
        self.db.execute('BEGIN')
        self.bytes = 0

    def parse(self, raw):
        if raw is None:
            return None
        size = len(raw.encode())
        self.bytes += size
        if size > MAX_ROW_BYTES or self.bytes > MAX_TOTAL_BYTES:
            raise ValueError('Replay row/total byte bound exceeded')
        return json.loads(raw)

    def models(self, session):
        rows = self.db.execute('''SELECT i.record_json,i.digest,r.seq,r.id,r.record_json,
            d.payload_json,d.payload_hash
            FROM invocations i JOIN deliveries d ON d.id=i.delivery_id
            LEFT JOIN invocation_receipts r ON r.invocation_id=i.id
            WHERE i.session=? ORDER BY i.rowid LIMIT ?''', (session, MAX_ROWS+1))
        result = []
        for index, (raw, ihash, seq, rhash, receipt, draw, phash) in enumerate(rows):
            if index >= MAX_ROWS:
                raise ValueError('Replay model row bound exceeded')
            inv, rec, payload = self.parse(raw), self.parse(receipt), self.parse(draw)
            if digest('invocation-v1:'+raw) != ihash or inv['session'] != session:
                raise ValueError('Invocation digest mismatch')
            if digest(canonical(payload)) != phash or inv['payload_hash'] != phash:
                raise ValueError('Delivery payload binding mismatch')
            if rec is not None and (rec['invocation_hash'] != ihash
                    or rec['payload_hash'] != phash
                    or digest('invocation-receipt-v1:'+receipt) != rhash
                    or digest(canonical(rec['proposal'])) != rec['response_hash']):
                raise ValueError('Invocation receipt digest mismatch')
            result.append({'invocation': inv, 'receipt': rec, 'receipt_hash': rhash,
                           'seq': seq, 'discovery_key': payload.get('discovery_key'),
                           'previous': payload.get('discovery_previous'),
                           'owner': payload.get('discovery_owner')})
        prior = 'GENESIS'
        for sequence, entry in enumerate(sorted((r for r in result if r['receipt']),
                                                 key=lambda row: row['seq']), 1):
            rec = entry['receipt']
            if (entry['seq'] != sequence or rec['seq'] != sequence
                    or rec['session'] != session or rec['previous_hash'] != prior):
                raise ValueError('Invocation receipt chain mismatch')
            prior = entry['receipt_hash']
        return result, prior

    def repositories(self, session):
        rows = self.db.execute('''SELECT o.record_json,o.digest,r.id,r.record_json,r.seq
            FROM repository_ops o LEFT JOIN repository_receipts r ON r.operation_id=o.id
            WHERE o.session=? ORDER BY r.seq,o.rowid LIMIT ?''', (session, MAX_ROWS+1))
        result = []
        prior, sequence = 'GENESIS', 0
        for index, (raw, ohash, rhash, receipt, seq) in enumerate(rows):
            if index >= MAX_ROWS:
                raise ValueError('Replay repository row bound exceeded')
            op, rec = self.parse(raw), self.parse(receipt)
            if digest('repository-op-v1:'+raw) != ohash:
                raise ValueError('Repository operation digest mismatch')
            if rec is not None:
                sequence += 1
                if (rec['seq'] != sequence or seq != sequence
                        or rec['session'] != session or rec['previous_hash'] != prior
                        or rec['operation_hash'] != ohash
                        or digest('repository-receipt-v1:'+receipt) != rhash):
                    raise ValueError('Repository receipt chain mismatch')
                prior = rhash
                rec = dict(rec, result={key: rec['result'][key]
                    for key in ('status', 'excerpts', 'versions', 'files', 'next_cursor',
                                'next_line', 'scanned_files', 'truncated') if key in rec['result']})
            result.append({'operation': op, 'receipt': rec, 'receipt_hash': rhash})
        return result, prior

    def close(self):
        self.db.rollback()
        self.db.close()


def accounting(models):
    settled = pending = 0
    elapsed = 0
    by_model = {}
    for row in models:
        inv, rec = row['invocation'], row['receipt']
        reserve = inv['charge'] + inv['budget_profile']['response_reserve']
        used = (rec.get('accounting_settlement') or {}).get('used_tokens', reserve) if rec else reserve
        if rec:
            settled += used
            elapsed += rec.get('elapsed_seconds', 0)
        else:
            pending += used
        bucket = by_model.setdefault(inv['model'], {'calls': 0, 'settled_units': 0,
                                                   'pending_units': 0})
        bucket['calls'] += 1
        bucket['settled_units' if rec else 'pending_units'] += used
    return {'calls': len(models), 'settled_units': settled, 'pending_units': pending,
            'recorded_model_seconds': round(elapsed, 6), 'by_model': by_model}


def repository_accounting(rows):
    return {'operations': len(rows), 'pending_operations': sum(row['receipt'] is None for row in rows),
            'recorded_source_byte_charges': sum(row['operation'].get('task_cost', {}).get('source_bytes', 0)
                                               for row in rows),
            'recorded_repository_seconds': round(sum((row['receipt'] or {}).get('elapsed_seconds', 0)
                                                     for row in rows), 6)}


def envelope_evidence(invocation, receipt):
    profile = invocation['budget_profile']
    return {'invocation_id': invocation['id'], 'role': invocation.get('role'),
            'model': invocation['model'], 'created_at': invocation['created_at'],
            'input_charge': invocation['charge'],
            'reserved_units': invocation['charge']+profile['response_reserve'],
            'budget_profile': {key: profile[key] for key in ('context_limit', 'context_profile',
                'reservation_limit', 'response_reserve', 'template_reserve') if key in profile},
            'receipt_status': receipt.get('status') if receipt else 'pending',
            'settlement': receipt.get('accounting_settlement') if receipt else None,
            'usage': receipt.get('token_usage') if receipt else None,
            'elapsed_seconds': receipt.get('elapsed_seconds') if receipt else None}


def reference_envelope(snapshot, iid):
    row = snapshot.db.execute('''SELECT i.record_json,i.digest,r.record_json,r.id
        FROM invocations i JOIN invocation_receipts r ON r.invocation_id=i.id
        WHERE i.id=?''', (iid,)).fetchone()
    if row is None:
        raise ValueError('Reference envelope is not a settled invocation')
    raw, ihash, rraw, rhash = row
    invocation, receipt = snapshot.parse(raw), snapshot.parse(rraw)
    if (digest('invocation-v1:'+raw) != ihash or receipt['invocation_hash'] != ihash
            or digest('invocation-receipt-v1:'+rraw) != rhash
            or digest(canonical(receipt['proposal'])) != receipt['response_hash']):
        raise ValueError('Reference envelope/receipt digest mismatch')
    return dict(envelope_evidence(invocation, receipt),
                reference_session=invocation['session'],
                verification='reference record digests only; other session full chain not replayed',
                applicability='historical proxy, not a same-task measured reviewer')


def governor_comparison(rows, repositories, session, assess, policies):
    """Counterfactual recommendations, not saved-task state transitions/savings."""
    keys = list(dict.fromkeys(row['discovery_key'] for row in rows))
    if len(keys) > 1:
        return [comparison for key in keys for comparison in governor_comparison(
            [row for row in rows if row['discovery_key'] == key], repositories, session, assess, policies)]
    effects = {}
    for row in repositories:
        if row['receipt'] is not None:
            effects.setdefault(row['operation'].get('invocation_id'), []).append(row)
    result = []
    for policy in policies:
        history = []
        first_yellow = first_synthesis = None
        statuses = Counter()
        decisions = []
        frame = {'format': 1, 'binding': {'session': session, 'task': session, 'stage': 'discovery'},
                 'steps': history}
        if policy is not None:
            frame['policy'] = policy
        final = assess(frame)
        for index, row in enumerate(rows, 1):
            receipt = row['receipt']
            if receipt is None or receipt['status'] != 'accepted':
                continue
            for entry in effects.get(row['invocation']['id'], []):
                history.append({'kind': 'repository', 'record': {
                    'id': entry['receipt_hash'], 'operation': entry['operation'],
                    'result': entry['receipt']['result']}})
                final = assess(frame)
                status = final['status']
                statuses[status] += 1
                compact = {'selector_attempt': index, 'accepted_repository_steps': len(history),
                           'status': status, 'action': final['action'],
                           'no_progress_streak': final['no_progress_streak'],
                           'complete_input_history': final['coverage']['complete_input_history']}
                decisions.append(compact)
                if status == 'yellow' and first_yellow is None:
                    first_yellow = compact
                if final['action'] == 'synthesize_partial' and first_synthesis is None:
                    first_synthesis = compact
        result.append({'discovery_key': keys[0] if keys else None,
                       'policy': final['policy'], 'first_yellow': first_yellow,
                       'first_partial_synthesis': first_synthesis,
                       'final_status': final['status'], 'final_counts': final['counts'],
                       'final_coverage': final['coverage'], 'prefix_status_counts': dict(statuses),
                       'decisions': decisions,
                       'authority': 'offline_counterfactual; no work stopped; no savings established'})
    return result


def phase_comparison(steps, prior_units, pair_units, task_allocations):
    """Retrospective measured-cost sensitivity; not an online admission policy."""
    def crossing(ceiling):
        used = novel = 0
        if ceiling <= 0:
            return {'after_selector_attempt': 0, 'selected_settled_units': 0,
                    'session_settled_units': prior_units, 'unique_source_bytes': 0}
        for step in steps:
            used += step['settled_units']
            novel += step['new_source_bytes']
            if used >= ceiling:
                return {'after_selector_attempt': step['ordinal'], 'selected_settled_units': used,
                        'session_settled_units': prior_units+used, 'unique_source_bytes': novel}
        return None
    comparisons = []
    for multiple in (1, 2):
        reserve = multiple*pair_units
        comparisons.append({'kind': 'selector_phase_cost_envelope', 'pair_multiple': multiple,
                            'ceiling_units': reserve, 'first_crossing': crossing(reserve)})
        for allocation in task_allocations:
            comparisons.append({'kind': 'task_total_with_downstream_holdback',
                                'pair_multiple': multiple, 'task_allocation_units': allocation,
                                'downstream_holdback_units': reserve,
                                'selector_headroom_at_stage_entry': allocation-prior_units-reserve,
                                'first_crossing': crossing(allocation-prior_units-reserve)})
    return {'synthesis_pair_envelope_units': pair_units, 'session_spending_before_stage': prior_units,
            'comparisons': comparisons,
            'limitations': ['Threshold crossings use actual historical settled costs after a call.',
                            'No claim that earlier synthesis would be correct, faster or cheaper.',
                            'The pair envelope is an explicit experiment input, not a runtime grant.',
                            'Changing the task total is sensitivity analysis; saved allocation stays unchanged.']}


def replay(snapshot, session, flow=None, discovery_key=None, assess=None, policies=None,
           synthesis_pair_units=None, task_allocations=()):
    models, model_head = snapshot.models(session)
    repositories, repository_head = snapshot.repositories(session)
    chosen = []
    owners = {}
    for row in models:
        if not row['discovery_key']:
            continue
        if discovery_key and row['discovery_key'] != discovery_key:
            continue
        if flow:
            owner = row['owner']
            if owner not in owners:
                record = snapshot.db.execute('SELECT record_json,digest FROM orchestration_nodes '
                    'WHERE id=? AND session=?', (owner, session)).fetchone()
                if not record or digest('orchestration-node-v1:'+record[0]) != record[1]:
                    raise ValueError('Discovery owner binding is absent or corrupt')
                owners[owner] = snapshot.parse(record[0])
            if owners[owner].get('flow_id') != flow:
                continue
        chosen.append(row)
    by_invocation = {}
    for row in repositories:
        iid = row['operation'].get('invocation_id')
        if iid:
            by_invocation.setdefault(iid, []).append(row)
    coverage, requests = SpanCoverage(), Counter()
    steps = []
    accepted_previous = {}
    for row in chosen:
        inv, rec = row['invocation'], row['receipt']
        accepted = bool(rec and rec.get('status') == 'accepted')
        key = row['discovery_key']
        if accepted:
            if row['previous'] != accepted_previous.get(key):
                raise ValueError('Accepted discovery checkpoint chain mismatch')
            accepted_previous[key] = inv['id']
        proposal = (rec or {}).get('proposal') or {}
        request = normalized_request(proposal.get('tool'))
        fingerprint = digest(canonical(request)) if request else None
        seen = requests[fingerprint] if fingerprint else 0
        if fingerprint:
            requests[fingerprint] += 1
        effect = by_invocation.get(inv['id'], [])
        new_bytes = delivered = unlocated_bytes = 0
        unlocated_unique_before = coverage.unlocated_unique_snippet_bytes
        excerpt_signatures = []
        versions = set()
        operation_kinds, result_statuses = [], []
        for entry in effect:
            op, receipt = entry['operation'], entry['receipt']
            operation_kinds.append(op['kind'])
            if receipt is None:
                result_statuses.append('pending')
                continue
            result = receipt['result']
            result_statuses.append(result['status'])
            for excerpt in result.get('excerpts', []):
                located = op['kind'] == 'read' or 'column' in excerpt
                new_bytes += coverage.add(excerpt, located=located)
                delivered += len(excerpt['text'].encode())
                if not located:
                    unlocated_bytes += len(excerpt['text'].encode())
                versions.add((excerpt['path'], excerpt['sha256']))
                # Source addresses, not generated receipt or excerpt IDs.
                excerpt_signatures.append({'path': excerpt['path'], 'sha256': excerpt['sha256'],
                    'line': excerpt['line'], 'column': excerpt.get('column', 0) if located else None,
                    'end_position': excerpt.get('end_position'),
                    'text_sha256': digest(excerpt['text']), 'bytes': len(excerpt['text'].encode())})
        reserve = inv['charge'] + inv['budget_profile']['response_reserve']
        step = {'ordinal': len(steps)+1, 'invocation_id': inv['id'], 'created_at': inv['created_at'],
                'discovery_key': key, 'model': inv['model'], 'accepted': accepted,
                'status': (rec or {}).get('status', 'pending'),
                'mode': proposal.get('mode'), 'request_fingerprint': fingerprint,
                'request': request, 'previous_same_request_count': seen,
                'repository_operations': len(effect), 'operation_kinds': operation_kinds,
                'repository_receipts': sum(entry['receipt'] is not None for entry in effect),
                'result_statuses': result_statuses, 'delivered_bytes': delivered,
                'new_source_bytes': new_bytes, 'repeated_source_bytes': delivered-unlocated_bytes-new_bytes,
                'unlocated_source_bytes': unlocated_bytes,
                'new_unlocated_snippet_bytes': coverage.unlocated_unique_snippet_bytes-unlocated_unique_before,
                'source_versions': len(versions), 'excerpts': excerpt_signatures,
                'settled_units': (rec.get('accounting_settlement') or {}).get('used_tokens', reserve)
                if rec else 0, 'elapsed_seconds': (rec or {}).get('elapsed_seconds')}
        steps.append(step)
    accepted_steps = [step for step in steps if step['accepted']]
    tools = [step for step in accepted_steps if step['request'] and step['request']['kind'] in ('read', 'search', 'list')]
    selected_ids = {row['invocation']['id'] for row in chosen}
    selected_repositories = [row for row in repositories
                             if row['operation'].get('invocation_id') in selected_ids]
    summary = {'format': FORMAT, 'authority': 'offline_receipt_observation_only',
        'session': session, 'flow': flow, 'discovery_key_filter': discovery_key,
        'chain_verification': {'invocation_rows': len(models), 'repository_rows': len(repositories),
                               'invocation_head': model_head, 'repository_head': repository_head,
                               'scope': 'scoped immutable record chains; not full protocol/external authenticity'},
        'session_accounting': accounting(models), 'selected_accounting': accounting(chosen),
        'session_repository_accounting': repository_accounting(repositories),
        'selected_repository_accounting': repository_accounting(selected_repositories),
        'recorded_non_discovery_envelopes': [envelope_evidence(row['invocation'], row['receipt'])
                                           for row in models if not row['discovery_key']],
        'selected': {'selector_attempts': len(steps), 'accepted_steps': len(accepted_steps),
                     'accepted_tool_steps': len(tools),
                     'accepted_tool_steps_with_receipt': sum(bool(step['repository_receipts']) for step in tools),
                     'accepted_tool_steps_without_source_gain': sum(
                         step['new_source_bytes'] == step['new_unlocated_snippet_bytes'] == 0 for step in tools),
                     'accepted_tool_steps_without_located_source_gain': sum(
                         step['new_source_bytes'] == 0 for step in tools),
                     'repeated_requests_all_attempts': sum(step['previous_same_request_count'] > 0 for step in steps),
                     'repeated_requests_accepted_tools': sum(step['previous_same_request_count'] > 0 for step in tools),
                     'distinct_request_fingerprints': len(requests),
                     'request_repetition_basis': 'model tool arguments; excludes host read profile/source-version scope',
                     'source': coverage.summary()},
        'limitations': ['Source counters measure repository receipt return values; subsequent model packet delivery is not measured.',
                        'Exact source gain counts located returned characters, not semantic relevance or model comprehension.',
                        'Search receipts without columns are excluded from exact byte union and reported separately.',
                        'A zero-source-gain list or search may still provide useful negative/navigation evidence.',
                        'No task is resumed; no model is called; no historical spending is refunded.',
                        'Current files are not read; source versions and text come only from saved receipts.']}
    trace_hash = digest(canonical(steps))
    summary['trace_sha256'] = trace_hash
    if assess is not None:
        summary['governor_comparisons'] = governor_comparison(chosen, selected_repositories,
                                                             session, assess, policies or [None])
    if synthesis_pair_units is not None:
        if len({row['discovery_key'] for row in chosen}) != 1:
            raise ValueError('Phase-capacity comparison requires exactly one discovery key; filter a flow/key')
        first_at = chosen[0]['invocation']['created_at'] if chosen else float('inf')
        earlier = [row for row in models if row['invocation']['created_at'] < first_at]
        summary['phase_capacity_counterfactuals'] = phase_comparison(steps,
            accounting(earlier)['settled_units'], synthesis_pair_units, task_allocations)
    return summary, steps


def write_new(path, value):
    """Avoid overwriting either evidence or an operator-supplied path."""
    with open(path, 'x', opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
        stream.write(json.dumps(value, indent=2, sort_keys=True)+'\n')


def benchmark_workload(size, pattern):
    if type(size) is not int or not 1 <= size <= 128 or pattern not in ('advancing', 'repeated', 'mixed'):
        raise ValueError('Unknown bounded benchmark workload')
    version = digest('fixed synthetic benchmark source')
    text = 'Synthetic source-acquisition timing fixture; no semantic claim.\n'
    steps = []
    for index in range(size):
        line = index+1 if pattern == 'advancing' else 1 if pattern == 'repeated' else index//4+1
        steps.append({'kind': 'repository', 'record': {'id': 'receipt-'+str(index),
            'operation': {'session': 'benchmark', 'contract_hash': 'benchmark-contract',
                          'kind': 'read', 'target': 'module.py', 'query': '', 'line': line},
            'result': {'status': 'ok', 'versions': {'module.py': version},
                       'excerpts': [{'id': 'volatile-'+str(index), 'path': 'module.py',
                           'sha256': version, 'line': line, 'column': 0, 'text': text}],
                       'truncated': False, 'scanned_files': 1}}})
    return {'format': 1, 'binding': {'session': 'benchmark', 'task': 'benchmark-task',
            'stage': 'discovery', 'contract_hash': 'benchmark-contract'}, 'steps': steps}


def timing_summary(values):
    ordered = sorted(values)
    return {'samples': len(values), 'median_ms': statistics.median(values)/10**6,
            'p95_ms': ordered[math.ceil(.95*len(ordered))-1]/10**6,
            'minimum_ms': ordered[0]/10**6, 'maximum_ms': ordered[-1]/10**6}


def benchmark(assess, adapter, repetitions=100, warmups=10, sizes=(8, 32, 128)):
    """CPU-only component timings. No ledger, runtime or provider is opened."""
    if (type(repetitions) is not int or not 1 <= repetitions <= 2000
            or type(warmups) is not int or not 0 <= warmups <= 25):
        raise ValueError('Benchmark repetitions/warmups exceed bounded experiment limits')
    results = []
    for size in sizes:
        for pattern in ('advancing', 'repeated', 'mixed'):
            frame = benchmark_workload(size, pattern)
            before = digest(canonical(frame))
            for _ in range(warmups):
                adapter.discovery(assess(frame))
            assessment_ns, adapter_ns, combined_ns = [], [], []
            for _ in range(repetitions):
                started = time.perf_counter_ns()
                progress = assess(frame)
                assessment_ns.append(time.perf_counter_ns()-started)
                started = time.perf_counter_ns()
                decision = adapter.discovery(progress)
                adapter_ns.append(time.perf_counter_ns()-started)
                started = time.perf_counter_ns()
                adapter.discovery(assess(frame))
                combined_ns.append(time.perf_counter_ns()-started)
            if digest(canonical(frame)) != before:
                raise ValueError('Benchmark changed its input workload')
            results.append({'receipts': size, 'pattern': pattern, 'workload_sha256': before,
                'canonical_frame_bytes': len(canonical(frame).encode()),
                'source_characters': sum(len(s['record']['result']['excerpts'][0]['text']) for s in frame['steps']),
                'assessment': timing_summary(assessment_ns),
                'adapter_and_canonical_kernel': timing_summary(adapter_ns),
                'combined': timing_summary(combined_ns),
                'status': progress['status'], 'kernel_decision': decision['decision'],
                'coverage': progress['coverage']})
    return {'format': 'discovery-governor-component-timing-v1', 'authority': 'synthetic_observation_only',
            'repetitions_per_component': repetitions, 'warmups_per_workload': warmups,
            'percentile': 'nearest-rank p95; perf_counter_ns; no outlier removal',
            'python': platform.python_version(), 'platform': platform.platform(),
            'workload': 'One fixed located source unit per read; exact character counts reported; fresh receipt IDs; advancing, repeated and four-step mixed ranges.',
            'results': results,
            'limits': ['In-memory synthetic receipts; receipt verification, SQLite, packing and inference are excluded.',
                       'Adapter timing includes the unchanged canonical kernel and its ordinary numeric validation.',
                       'Thresholds2/4 and phase multiplier2 remain initial heuristics, not calibrated quality guarantees.',
                       'The failed historical native review never reached a novelty-only partial-synthesis recommendation.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path)
    parser.add_argument('--session')
    parser.add_argument('--flow')
    parser.add_argument('--discovery-key')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--steps-output', type=Path,
                        help='Optional local metadata trace, including tool queries/paths but no source text')
    parser.add_argument('--compare-governor', action='store_true',
                        help='Replay the pure local governor, without executing its recommendation')
    parser.add_argument('--governor-policy', action='append', metavar='YELLOW:SYNTHESIS',
                        help='Additional initial-threshold sensitivity, e.g. 2:3; never changes runtime settings')
    parser.add_argument('--synthesis-pair-units', type=int,
                        help='Explicit writer+one-review-page envelope for 1x/2x retrospective sensitivity')
    parser.add_argument('--task-allocation-units', type=int, action='append',
                        help='Task-total sensitivity with pair holdback; does not change saved allocation')
    parser.add_argument('--reviewer-reference',
                        help='Optional explicit historical reviewer invocation, labelled proxy if another task')
    parser.add_argument('--benchmark', action='store_true', help='Pure synthetic assessment/adapter component timings; no ledger opened')
    parser.add_argument('--repetitions', type=int, default=100)
    parser.add_argument('--warmups', type=int, default=10)
    args = parser.parse_args(argv)
    if args.benchmark:
        if args.db or args.session or args.flow or args.discovery_key or args.steps_output:
            parser.error('--benchmark is separate from ledger replay')
        harness = Path(__file__).resolve().parents[1]/'harness'
        sys.path.insert(0, str(harness))
        from discovery_governor import assess
        from karma_adapter import KarmaAdapter
        report = benchmark(assess, KarmaAdapter(), args.repetitions, args.warmups)
        if args.output:
            write_new(args.output, report)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    if not args.db or not args.session:
        parser.error('Ledger replay requires --db and --session')
    if (args.synthesis_pair_units is not None and not 1 <= args.synthesis_pair_units <= 10**12
            or any(not 1 <= v <= 10**12 for v in args.task_allocation_units or [])
            or len(args.task_allocation_units or []) > 8 or len(args.governor_policy or []) > 8):
        parser.error('Experiment bounds require positive capacities and at most eight variants')
    if args.task_allocation_units and args.synthesis_pair_units is None:
        parser.error('--task-allocation-units requires --synthesis-pair-units')
    started = time.monotonic()
    snapshot = Snapshot(args.db)
    try:
        assess = None
        policies = [None]
        if args.compare_governor:
            source = Path(__file__).resolve().parents[1]/'harness/discovery_governor.py'
            spec = importlib.util.spec_from_file_location('offline_discovery_governor', source)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            assess = module.assess
            for value in args.governor_policy or []:
                yellow, synthesize = map(int, value.split(':'))
                policies.append({'yellow_after': yellow, 'synthesize_after': synthesize,
                                 'window': max(8, synthesize)})
        elif args.governor_policy:
            raise ValueError('--governor-policy requires --compare-governor')
        summary, steps = replay(snapshot, args.session, args.flow, args.discovery_key, assess, policies,
                                args.synthesis_pair_units, args.task_allocation_units or [])
        if args.reviewer_reference:
            summary['historical_reviewer_reference'] = reference_envelope(snapshot, args.reviewer_reference)
    finally:
        snapshot.close()
    summary['elapsed_seconds'] = round(time.monotonic()-started, 6)
    if args.steps_output:
        write_new(args.steps_output, {'format': FORMAT, 'trace_sha256': summary['trace_sha256'],
                                      'steps': steps})
    if args.output:
        write_new(args.output, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, sqlite3.Error) as error:
        print('Read-only replay failed: '+str(error), file=sys.stderr)
        raise SystemExit(1)
