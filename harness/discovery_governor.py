"""Bounded structural discovery progress, reconstructed from host-verified steps.

The caller verifies receipts, source currency, task membership and recall addresses.
This pure projection does not establish semantic comprehension or report quality.
Its recommendation is not an admission decision, a FIN or a permission grant.
"""
from bisect import bisect_left
import hashlib
import json


PROFILE = 'discovery-progress-v1'
LIMITS = {'steps': 128, 'excerpts_per_step': 32, 'versions_per_step': 256,
          'text_chars': 262144, 'span_chars': 16384, 'identifier_chars': 256,
          'recent_facts': 8}
DEFAULT_POLICY = {'version': 1, 'yellow_after': 2, 'synthesize_after': 4,
                  'recover_after': 2, 'window': 8}
POLICY_RATIONALE = ('Versioned initial tuning, not universal calibration: two '
                    'non-advancing operations expose a compact host delta; four '
                    'suggest a partial draft. Two advancing operations clear '
                    'the warning. Recall is separate and does not consume this streak.')


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _string(value, name, empty=False):
    if (not isinstance(value, str) or len(value) > LIMITS['identifier_chars']
            or (not value and not empty) or any(ord(c) < 32 for c in value)):
        raise ValueError('Invalid discovery ' + name)
    return value


def _integer(value, name, minimum=0, maximum=10**12):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('Invalid discovery ' + name)
    return value


def _digest(value):
    if (not isinstance(value, str) or len(value) != 64
            or any(c not in '0123456789abcdef' for c in value)):
        raise ValueError('Invalid source version')
    return value


def _position(value):
    if not isinstance(value, dict) or len(value) != 2 or set(value) != {'line', 'column'}:
        raise ValueError('Invalid source position')
    return (_integer(value['line'], 'line', 1), _integer(value['column'], 'column'))


def _policy(value):
    result = dict(DEFAULT_POLICY)
    if value is not None:
        if not isinstance(value, dict) or len(value) > len(result) or set(value) - set(result):
            raise ValueError('Invalid discovery policy')
        result.update(value)
    if type(result['version']) is not int or result['version'] != 1:
        raise ValueError('Unknown discovery policy version')
    for key in ('yellow_after', 'synthesize_after', 'recover_after', 'window'):
        _integer(result[key], 'policy ' + key, 1, 64)
    if not result['yellow_after'] < result['synthesize_after'] <= result['window']:
        raise ValueError('Discovery thresholds must fit the observation window')
    return result


def _end(text, start):
    # Match the repository reader's splitlines(True), including Unicode lines.
    lines = (text + '\0').splitlines(True)
    return (start[0] + len(lines) - 1,
            (start[1] if len(lines) == 1 else 0) + len(lines[-1]) - 1)


def _add_interval(intervals, start, end):
    """Return whether this located span exposes any previously unseen position."""
    if end <= start:
        raise ValueError('Source span does not advance')
    left = bisect_left(intervals, (start, end))
    if left and intervals[left - 1][1] >= start:
        left -= 1
    covered = (left < len(intervals) and intervals[left][0] <= start
               and end <= intervals[left][1])
    right = left; merged_start = start; merged_end = end
    while right < len(intervals) and intervals[right][0] <= merged_end:
        a, b = intervals[right]
        merged_start = min(merged_start, a); merged_end = max(merged_end, b)
        right += 1
    intervals[left:right] = [(merged_start, merged_end)]
    return not covered


def assess(frame):
    """Assess chronological verified repository/recall steps without I/O.

    frame = {format: 1, binding: {session, task, stage[, contract_hash]},
             steps: [{kind: 'repository', record: record_by_id_result},
                     {kind: 'recall', ref: host_validated_address}],
             policy: optional versioned threshold overrides,
             complexity: optional host-measured catalog_files/catalog_bytes/
                         functions/dependency_edges/frontier_files counts}.

    Extra fields inside actual repository records are ignored; no receipt hash
    verification is attempted here. Model notes/remaining gaps are not inputs.
    History is a bounded tail, so uniqueness means within that retained window.
    """
    if (not isinstance(frame, dict) or len(frame) > 5
            or set(frame) - {'format', 'binding', 'steps', 'policy', 'complexity'}):
        raise ValueError('Invalid discovery frame')
    if type(frame.get('format')) is not int or frame['format'] != 1:
        raise ValueError('Unknown discovery frame version')
    binding = frame.get('binding')
    if (not isinstance(binding, dict) or len(binding) > 4
            or set(binding) - {'session', 'task', 'stage', 'contract_hash'}
            or not {'session', 'task', 'stage'} <= set(binding)):
        raise ValueError('Invalid discovery binding')
    binding = {key: _string(value, 'binding ' + key) for key, value in binding.items()}
    steps = frame.get('steps')
    if not isinstance(steps, list):
        raise ValueError('Invalid discovery steps')
    policy = _policy(frame.get('policy'))
    complexity = frame.get('complexity', {})
    allowed_complexity = {'catalog_files', 'catalog_bytes', 'functions', 'dependency_edges', 'frontier_files'}
    if not isinstance(complexity, dict) or len(complexity) > 5 or set(complexity) - allowed_complexity:
        raise ValueError('Invalid host complexity measurements')
    complexity = {k: _integer(v, 'complexity ' + k) for k, v in complexity.items()}
    omitted_steps = max(0, len(steps) - LIMITS['steps'])
    counts = {key: 0 for key in ('repository_operations', 'replayed_receipts', 'failed_operations',
              'progress_operations', 'no_progress_operations', 'unknown_operations',
              'new_source_spans', 'repeated_source_spans', 'new_search_snippets',
              'repeated_search_snippets', 'new_list_paths',
              'cursor_advances', 'empty_search_closures', 'repeated_requests',
              'recalls', 'repeated_recalls', 'omitted_excerpts', 'omitted_versions')}
    seen_receipts = {}; requests = set(); spans = set(); intervals = {}; paths = set()
    read_paths = set(); cursor_heads = {}; empty_searches = set(); recall_refs = set()
    remaining_text = LIMITS['text_chars']; recent = []; recent_outcomes = []
    state = 'green'; no_progress = 0; recovery = 0; observed_text = 0; repeat_streak = 0
    same_request_streak = 0; previous_request = None
    for step in steps[-LIMITS['steps']:]:
        if not isinstance(step, dict):
            raise ValueError('Invalid discovery step')
        kind = step.get('kind')
        if kind == 'recall':
            if len(step) != 2 or set(step) != {'kind', 'ref'}:
                raise ValueError('Invalid recall observation')
            ref = _string(step['ref'], 'recall reference')
            counts['recalls'] += 1
            counts['repeated_recalls'] += int(ref in recall_refs)
            recall_refs.add(ref)
            continue
        if kind != 'repository' or len(step) != 2 or set(step) != {'kind', 'record'}:
            raise ValueError('Invalid repository observation')
        record = step['record']
        if not isinstance(record, dict):
            raise ValueError('Invalid repository record')
        rid = _string(record.get('id'), 'receipt identifier')
        op = record.get('operation'); result = record.get('result')
        if not isinstance(op, dict) or not isinstance(result, dict):
            raise ValueError('Invalid repository record body')
        if op.get('session') != binding['session']:
            raise ValueError('Repository observation belongs to another session')
        if 'contract_hash' in binding and op.get('contract_hash') != binding['contract_hash']:
            raise ValueError('Repository observation belongs to another contract')
        operation = op.get('kind')
        if operation not in ('read', 'search', 'list', 'catalog', 'api_index', 'static_map'):
            raise ValueError('Unsupported observed repository operation')
        target = _string(op.get('target', ''), 'target', empty=True)
        query = _string(op.get('query', ''), 'query', empty=True)
        line = _integer(op.get('line', 1), 'operation line', 1)
        cursor = op.get('cursor')
        cursor_identity = None
        if cursor is not None:
            if not isinstance(cursor, dict) or len(cursor) != 3 or set(cursor) != {'line', 'column', 'sha256'}:
                raise ValueError('Invalid observed read cursor')
            cursor_identity = [_position({k: cursor[k] for k in ('line', 'column')}), _digest(cursor['sha256'])]
        read_profile = op.get('read_profile')
        if read_profile is not None:
            if (not isinstance(read_profile, dict) or len(read_profile) != 3
                    or set(read_profile) != {'version', 'max_bytes', 'max_lines'}
                    or type(read_profile['version']) is not int or read_profile['version'] != 1):
                raise ValueError('Invalid observed read profile')
            read_profile = {k: _integer(v, 'read profile ' + k, 1) for k, v in read_profile.items()}
        versions = result.get('versions', {})
        if not isinstance(versions, dict):
            raise ValueError('Invalid observed source versions')
        versions_complete = len(versions) <= LIMITS['versions_per_step']
        counts['omitted_versions'] += max(0, len(versions) - LIMITS['versions_per_step'])
        # Incomplete maps are not sorted or hashed: that would hide unbounded work.
        version_scope = []
        if versions_complete:
            version_scope = sorted((_string(p, 'version path'), _digest(v)) for p, v in versions.items())
        search_version = op.get('search_version')
        if search_version is not None and (type(search_version) is not int or search_version != 1):
            raise ValueError('Invalid observed search profile')
        status = result.get('status')
        if not isinstance(status, str) or status not in ('ok', 'failed'):
            raise ValueError('Invalid repository observation status')
        request = _hash([binding, operation, target, query, line, cursor_identity,
                         read_profile, search_version, version_scope])
        next_cursor = result.get('next_cursor')
        if next_cursor is not None:
            if not isinstance(next_cursor, dict) or len(next_cursor) != 3 or set(next_cursor) != {'line', 'column', 'sha256'}:
                raise ValueError('Invalid resulting read cursor')
            _position({k: next_cursor[k] for k in ('line', 'column')}); _digest(next_cursor['sha256'])
        signature = _hash([request, status, next_cursor])
        if rid in seen_receipts:
            if seen_receipts[rid] != signature:
                raise ValueError('Receipt identity changed within observation history')
            counts['replayed_receipts'] += 1
            continue
        seen_receipts[rid] = signature
        counts['repository_operations'] += 1
        repeated = versions_complete and request in requests
        counts['repeated_requests'] += int(repeated)
        repeat_streak = repeat_streak + 1 if repeated else 0
        same_request_streak = same_request_streak + 1 if repeated and request == previous_request else 0
        previous_request = request if versions_complete else None
        if versions_complete:
            requests.add(request)
        progress = False; complete = versions_complete
        excerpts = result.get('excerpts', [])
        if not isinstance(excerpts, list):
            raise ValueError('Invalid observed excerpts')
        counts['omitted_excerpts'] += max(0, len(excerpts) - LIMITS['excerpts_per_step'])
        complete = complete and len(excerpts) <= LIMITS['excerpts_per_step']
        if status == 'failed':
            counts['failed_operations'] += 1
        else:
            for entry in excerpts[:LIMITS['excerpts_per_step']]:
                if not isinstance(entry, dict):
                    raise ValueError('Invalid observed excerpt')
                path = _string(entry.get('path'), 'source path'); version = _digest(entry.get('sha256'))
                start = (_integer(entry.get('line'), 'source line', 1), _integer(entry.get('column', 0), 'source column'))
                text = entry.get('text')
                if not isinstance(text, str):
                    raise ValueError('Invalid source text')
                if len(text) > min(LIMITS['span_chars'], remaining_text):
                    counts['omitted_excerpts'] += 1; complete = False; continue
                remaining_text -= len(text); observed_text += len(text)
                if not text:
                    continue
                digest = _hash(text)
                # Search excerpts omit the within-line starting column. They
                # are line-located candidate snippets, never precise ranges.
                located = operation != 'search' or 'column' in entry
                if located:
                    explicit_end = entry.get('end_position')
                    end = _position(explicit_end) if isinstance(explicit_end, dict) else _end(text, start)
                    identity = ('span', path, version, start, end, digest)
                    novel = identity not in spans
                    novel = _add_interval(intervals.setdefault((path, version), []), start, end) and novel
                    counter = 'new_source_spans' if novel else 'repeated_source_spans'
                else:
                    identity = ('search_snippet', path, version, start[0], digest)
                    novel = identity not in spans
                    counter = 'new_search_snippets' if novel else 'repeated_search_snippets'
                spans.add(identity)
                counts[counter] += 1
                progress = progress or novel
                if operation == 'read':
                    read_paths.add(path)
            if operation in ('list', 'catalog'):
                files = result.get('files', [])
                if not isinstance(files, list):
                    raise ValueError('Invalid observed catalog page')
                if len(files) > LIMITS['versions_per_step']:
                    complete = False
                for path in files[:LIMITS['versions_per_step']]:
                    path = _string(path, 'catalog path')
                    if path not in paths:
                        paths.add(path); counts['new_list_paths'] += 1; progress = True
            if operation == 'read' and excerpts:
                if next_cursor is not None:
                    end_cursor = _position({k: next_cursor[k] for k in ('line', 'column')})
                    version = _digest(next_cursor['sha256'])
                    start_cursor = _position({k: cursor[k] for k in ('line', 'column')}) if cursor else (line, 0)
                    prior = cursor_heads.get((target, version), start_cursor)
                    if end_cursor > max(start_cursor, prior) and complete:
                        counts['cursor_advances'] += 1; progress = True
                    cursor_heads[(target, version)] = max(prior, end_cursor)
            if (operation == 'search' and not excerpts and not result.get('truncated', False)
                    and versions_complete and version_scope
                    and type(result.get('scanned_files')) is int and result['scanned_files'] > 0):
                if request not in empty_searches:
                    empty_searches.add(request); counts['empty_search_closures'] += 1; progress = True
        outcome = 'progress' if progress else ('no_progress' if complete else 'unknown')
        counts[outcome + '_operations'] += 1
        recent_outcomes.append(outcome); recent_outcomes = recent_outcomes[-policy['window']:]
        if outcome == 'progress':
            no_progress = 0; recovery += 1
            if recovery >= policy['recover_after']:
                state = 'green'
        elif outcome == 'no_progress':
            recovery = 0; no_progress += 1
            if no_progress >= policy['synthesize_after']:
                state = 'partial_synthesis'
            elif no_progress >= policy['yellow_after']:
                state = 'yellow'
        else:
            # A gap in observer coverage cannot establish a consecutive streak.
            recovery = 0; no_progress = 0
        recent.append({'operation': operation, 'target': target, 'outcome': outcome,
                       'repeated_request': bool(repeated)})
        recent = recent[-LIMITS['recent_facts']:]
    complete = not (omitted_steps or counts['omitted_excerpts'] or counts['omitted_versions'])
    snippets = counts['new_search_snippets'] + counts['repeated_search_snippets']
    located_spans = counts['new_source_spans'] + counts['repeated_source_spans']
    action = {'green': 'continue', 'yellow': 'host_delta', 'partial_synthesis': 'synthesize_partial'}[state]
    delta = None if state == 'green' else {
        'no_progress_operations': no_progress, 'recovery_operations': recovery,
        'repeated_request_streak': repeat_streak,
        'same_request_streak': same_request_streak,
        'recent_outcomes': recent_outcomes, 'recent_operations': recent,
        'meaning': 'Observed source acquisition only; comprehension and unresolved semantic gaps are unverified.'}
    return {'profile': PROFILE, 'format': 1, 'binding': binding, 'policy': policy,
            'status': state, 'action': action, 'host_delta': delta, 'counts': counts,
            'no_progress_streak': no_progress, 'recovery_streak': recovery,
            'repeated_request_streak': repeat_streak,
            'same_request_streak': same_request_streak,
            'frontier': dict(complexity, observed_read_files=len(read_paths),
                             observed_list_paths=len(paths)),
            'coverage': {'complete_input_history': complete, 'omitted_steps': omitted_steps,
                         'observed_text_chars': observed_text,
                         'source_location_coverage': ('partial' if snippets else
                                                      'located' if located_spans else 'unassessed'),
                         'unlocated_search_snippets': snippets,
                         'unlocated_snippets_merged_into_source_ranges': False,
                         'uniqueness_scope': 'retained_history_window',
                         'semantic_progress': 'unassessed', 'receipt_verification': 'caller'},
            'limits': dict(LIMITS), 'authority': 'host_routing_recommendation_only'}
