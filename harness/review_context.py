"""Bounded structural context from fresh source, never model-created authority."""
import ast
from pathlib import Path
import posixpath
import re
from context_manager import InvalidUpdate, ContextBudgetExceeded, canonical
from paging import Budget

MAX_REVIEW_OUTPUT_RESERVE = 8192

CAPABILITIES = {
    'execution': 'host reads source; this model analyses supplied evidence',
    'source_field': 'excerpts',
    'structure_field': 'workspace_map',
    'pages_field': 'optional Smriti pages, not the source inventory',
    'implementation_present': 'tracks harness-created artifacts, not whether repository source exists',
    'handoff': 'optional earlier reviewer note; null does not disable source analysis',
}


def query(p):
    query = p['contract']['goal'] + '\n' + p['request']
    if p.get('review_intent_version') == 1:
        from review_report import requested
        if requested(p):
            query += '\nArchitecture overview: components and end-to-end request flow.'
    return query


_WORD = re.compile(r'[a-zA-Z][a-zA-Z0-9]+')
_CAMEL = re.compile(r'(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])')
_NOISE = frozenset({
    'about', 'also', 'and', 'architecture', 'architectural', 'before', 'check', 'checked', 'checks', 'code', 'codebase',
    'each', 'entire', 'explain', 'file', 'flows', 'from', 'give', 'have', 'identify', 'including',
    'into', 'maintainer', 'only', 'overview', 'path', 'please', 'project', 'request', 'review', 'same',
    'show', 'source', 'supplied', 'that', 'the', 'through', 'use', 'what', 'where', 'whole',
    'with', 'work', 'workspace', 'works', 'your',
})
_ALIASES = {
    'published': 'publish', 'publishing': 'publish', 'publication': 'publish',
    'citations': 'citation', 'cite': 'citation',
}
_RELATED = {
    'stp': {'kernel', 'boundary', 'adapter'},
    'checkpoint': {'slice', 'resume', 'join'},
    'branch': {'fork', 'child', 'join', 'subagent', 'orchestration'},
    'publish': {'report', 'claim', 'receipt'},
}


def evidence_words(text):
    words = set()
    for token in _WORD.findall(_CAMEL.sub(' ', text)):
        word = token.lower()
        if word not in _NOISE and len(word) >= 3:
            if word in _ALIASES:
                word = _ALIASES[word]
            elif word.endswith('ies'):
                word = word[:-3] + 'y'
            elif word.endswith('ches'):
                word = word[:-2]
            elif len(word) > 4:
                word = word.removesuffix('s')
            words.add(word)
    return words


class EvidenceFocus:
    """Rank in-scope files and declarations by the question they can answer."""

    def __init__(self, request):
        self.request = request.lower()
        self.words = evidence_words(request)
        self.related = set().union(*(_RELATED.get(word, ()) for word in self.words))
        self.references = set(re.findall(r'\b[a-zA-Z_][\w]*(?:\.[a-zA-Z_][\w]*)+\b', request))
        self.modules = {ref.split('.')[-2].replace('_', '').lower() for ref in self.references}

    def strength(self, label):
        words = evidence_words(label)
        return 3 * len(words & self.words) + len(words & (self.related - self.words))

    def symbol(self, path, name):
        qualified = Path(path).stem + '.' + name
        exact = name in self.references or qualified in self.references
        return (exact, self.strength(name))

    def mentions_module(self, path):
        return Path(path).stem.replace('_', '').lower() in self.modules


def retrieval_hints(notes, sources, *, graph=None):
    """Match unverified source requests only against the caller's source scope."""
    references = re.findall(r'(?<![\w/.-])(?:[a-zA-Z_][\w-]*/)*[a-zA-Z_][\w-]*(?:\.[a-zA-Z_][\w-]*)*',
                            '\n'.join(note for note in notes if isinstance(note, str)))
    preferred, matched = [], []
    paths = sorted(sources)
    for reference in references:
        ref = reference.casefold()
        found = []
        for path in paths:
            if '/' in ref:
                matches = ref == path.casefold()
            else:
                stem = Path(path).stem.replace('_', '').casefold()
                module = str(Path(path).with_suffix('')).replace('/', '.').replace('_', '').casefold()
                name = ref.replace('_', '')
                matches = (ref == Path(path).name.casefold() or name == stem or name.startswith(stem + '.')
                           or name == module or name.startswith(module + '.'))
            if matches:
                found.append(path)
        if found:
            matched.append(reference)
            preferred.extend(path for path in found if path not in preferred)
        # New callers can resolve a bare symbol against declarations in their
        # already authorized partition. Legacy recipes omit graph and preserve
        # their exact historical selection. A requested external path cannot
        # turn its basename into a local-file or symbol match.
        if graph is not None and '/' not in reference:
            for node in graph.get('nodes', []):
                source = node.get('path')
                name = node.get('name', '')
                leaf = name.rsplit('.', 1)[-1]
                if (source in sources and name != '<module>' and
                        reference.casefold() in (name.casefold(), leaf.casefold())):
                    qualified = Path(source).stem + '.' + leaf
                    if qualified not in matched:
                        matched.append(qualified)
                    if source not in preferred:
                        preferred.append(source)
    return preferred, ' '.join(dict.fromkeys(matched))


ENTRY_SYMBOLS = frozenset({'request', 'dispatch', 'handle', 'invoke', 'run', 'activate', 'main',
                           'execute', 'register', 'finalize', 'turn', 'admit_proposal'})


def explicit_symbol(focus, path, name):
    if focus.symbol(path, name)[0]:
        return True
    leaf = name.rsplit('.', 1)[-1]
    references = {reference.casefold() for reference in focus.references}
    if (Path(path).stem + '.' + leaf).casefold() in references:
        return True
    escaped = re.escape(leaf.casefold())
    return bool(re.search(r'`' + escaped + r'`|\b' + escaped + r'\s*\(', focus.request)
                or ('_' in leaf and re.search(r'(?<!\w)' + escaped + r'(?!\w)', focus.request)))


def ranked_declarations(graph, path, request, latest=''):
    """Rank source mechanisms without promoting names to execution evidence."""
    choices = [node for node in (graph or {}).get('nodes', [])
               if node['path'] == path and node['name'] != '<module>']
    focus, hints = EvidenceFocus(request), EvidenceFocus(latest)
    calls = {}
    for call in (graph or {}).get('calls', []):
        calls[call['source']] = calls.get(call['source'], 0) + 1
    def entry(node):
        return node['name'].rsplit('.', 1)[-1].lstrip('_').lower() in ENTRY_SYMBOLS
    def anonymous(node):
        return '<anonymous' in node['name'] or '<lambda' in node['name']
    if any(entry(node) and not anonymous(node) for node in choices):
        choices = [node for node in choices if not anonymous(node)]
    def score(node):
        count = calls.get(node.get('id'), 0)
        return (not explicit_symbol(hints, path, node['name']), not explicit_symbol(focus, path, node['name']),
                not bool(count), not entry(node), -count, -focus.strength(node['name']),
                node['line'], node.get('column', 0), node['name'])
    return sorted(choices, key=score)


def ordered_sources(sources, graph, request, *, version=1, latest=''):
    from auto_review import ranked
    names = ranked(sources, request)
    if not re.search(r'architecture|architectural|overview', request, re.I): return names
    focus = EvidenceFocus(request)
    counts = {p: 0 for p in names}
    for call in (graph or {}).get('calls', []):
        if call['path'] in counts: counts[call['path']] += 1
    symbols = {}
    for node in (graph or {}).get('nodes', []):
        path, name = node['path'], node['name']
        if path in counts and name != '<module>':
            symbols[path] = max(symbols.get(path, (False, 0)), focus.symbol(path, name))
    positions = {p: i for i, p in enumerate(names)}
    entry = {'main', 'index', 'app', 'server', 'extension', '__main__'}
    def score(name):
        p = Path(name)
        auxiliary = bool(set(p.parts) & {'test', 'tests', 'examples', 'fixtures'}) or p.stem.startswith(('test_', 'test-'))
        exact, symbol_strength = symbols.get(name, (False, 0))
        explicit_file = name.lower() in focus.request or focus.mentions_module(name)
        match = 2 * focus.strength(p.as_posix()) + symbol_strength
        return (not (explicit_file or exact), auxiliary, -match, p.stem not in entry, -counts[name], positions[name])
    if version >= 3:
        preferred, _ = retrieval_hints([latest], sources)
        hints = EvidenceFocus(latest)
        def current_score(name):
            nodes = ranked_declarations(graph, name, request, latest)
            top = nodes[0] if nodes else None
            outgoing = sum(call['source'] == top.get('id') for call in (graph or {}).get('calls', [])) if top else 0
            label = top['name'].rsplit('.', 1)[-1].lstrip('_').lower() if top else ''
            return (name not in preferred, not bool(top and explicit_symbol(hints, name, top['name'])),
                    not bool(top and explicit_symbol(focus, name, top['name'])),
                    score(name)[1], not bool(outgoing), label not in ENTRY_SYMBOLS, -outgoing,
                    -focus.strength(name), positions[name])
        return sorted(names, key=current_score)
    return sorted(names, key=score)


class ReviewContextExceeded(ContextBudgetExceeded):
    """Distinguish the task's per-call allocation from model capability."""
    def __init__(self, allocation, budget, cause):
        self.details = dict(allocation, output_reserve=budget.response_reserve,
                            cause=str(cause))
        if budget.context_profile == 2:
            physical = getattr(cause, 'physical_context', None)
            kind = 'physical_context' if physical else ('source_page' if str(cause).startswith('No complete ') else 'reservation_budget')
            self.details.update(failure_basis=kind, physical_context_tokens=budget.context_limit,
                                reservation_limit_units=budget.reservation_limit)
            if physical:
                self.details['physical_context'] = physical
            super().__init__(
                'The review packet exceeds its ' + {'physical_context': 'measured physical token window',
                    'reservation_budget': 'financial reservation allocation', 'source_page': 'source-page allocation'}[kind] +
                f': {budget.context_limit:,} physical context tokens, {budget.response_reserve:,} reserved output tokens, '
                f'and {budget.reservation_limit:,} reservation units per call. ' + str(cause) +
                ' The task contract, pinned statements and spending are retained.')
            return
        super().__init__(
            'The review packet cannot fit the current allocation: '
            f'{budget.context_limit:,} per call, including {budget.response_reserve:,} reserved for output; '
            f'{allocation["available_tokens"]:,} task/parent budget units remain. '
            f'The current host context cap is {allocation["effective_context_tokens"]:,} tokens'
            + (f'; the model advertises {allocation["model_context_tokens"]:,}'
               if allocation.get('model_context_tokens') else '')
            + (f' and is configured for {allocation["configured_context_tokens"]:,}'
               if allocation.get('configured_context_tokens') else '') + '. '
            'Input is reserved conservatively using UTF-8 byte budget units, not measured model tokens; '
            'this stop does not establish an actual token-window overflow. '
            'The task contract, pinned statements and latest turn were retained. '
            'A larger model alone does not increase the task allocation.')


def context_budget(provider, s, sid, slots=2, expand_output=True, *, physical_first=False):
    """Use installed capability within remaining task and ancestor allocations.

    This only proposes a packet size. The gateway still charges every attempt
    and the unchanged kernel must admit it. Optional repair reservations may be
    reduced by the caller when the initial packet cannot fit. Every actual
    attempt still needs fresh admission within the remaining shared budget.
    """
    from model_context import allocation
    import review_budget
    import orchestration as machine
    if getattr(provider, 'context_metadata', {}).get('_sutra_provider') == 'sutra_remote':
        # Opt in after the selected server is available. Discovery failure
        # retains the conservative profile; an established exact profile fails
        # closed on later measurement/identity loss.
        refresh = getattr(provider, 'fresh_context_metadata', None)
        if callable(refresh):
            refresh()
    budget, metadata = allocation(provider.context_metadata)
    writer_limit = getattr(provider, 'review_window_limit', None)
    if writer_limit is not None and writer_limit < budget.context_limit:
        budget = budget.with_limits(context_limit=writer_limit)
        metadata['effective_context_tokens'] = writer_limit
    available = float('inf')
    task = review_budget.usage(s, sid)
    if task:
        available = min(available, max(0, task['limits']['tokens'] - task['used']['tokens']))
    owner = machine.ACTIVE.get() or machine.task(s, sid)['id']
    if owner:
        for frame in machine.gates(s, owner, {}):
            available = min(available, max(0, frame['limits']['tokens'] - frame['used']['tokens']))
    if available == float('inf'):
        available = budget.context_limit * slots
    window = getattr(provider, 'context_metadata', {}).get('_sutra_context_window')
    if window is not None:
        from context_window import validate_window
        validate_window(window)
    financial_limit = max(1, int(available // slots))
    # New parent source recipes fit earned evidence against installed capacity
    # before separately gating their writer/reviewer planning cost. Historical
    # conservative packets still use their original financial context clamp.
    limit = budget.context_limit if window is not None or physical_first else min(budget.context_limit, financial_limit)
    # A structured architecture report needs room for source citations. Reserve output within the same per-call/task allocation.
    reserve_limit = min(limit, financial_limit)
    reserve = max(budget.response_reserve, min(MAX_REVIEW_OUTPUT_RESERVE, reserve_limit // 3)) if expand_output else min(budget.response_reserve, max(512, reserve_limit // 4))
    if limit <= reserve + 1024:
        # Do not invent extra allocation. The normal budget checks stop dispatch.
        limit = max(1, limit)
    provider.budget = (Budget(limit, reserve, budget.template_reserve, context_profile=2,
                             reservation_limit=financial_limit, context_window=window) if window is not None
                       else Budget(limit, reserve, budget.template_reserve))
    return dict(metadata, available_tokens=int(available), review_context_allocation=limit,
                **({'reservation_limit_units': financial_limit, 'context_profile': 2} if window is not None else
                   {'reservation_limit_units': financial_limit, 'parent_source_funding_version': 1} if physical_first else {}),
                attempt_slots_reserved=slots, repair_slots_reserved=max(0, slots-1),
                expanded_output_reserve=expand_output)


def overview(data, limit, request='Architecture overview', *, version=1, latest=''):
    from static_analysis import SOURCE_EXTENSIONS
    if type(limit) is not int or not 512 <= limit <= 65536:
        raise InvalidUpdate('Invalid structural context allocation')
    graph = data['graph']; sources = data['sources']
    paths = sorted(p for p in sources if Path(p).suffix in SOURCE_EXTENSIONS)
    modules = []
    ordered = ordered_sources({p:sources[p] for p in paths}, graph, request, version=version, latest=latest)
    for p in ordered:
        declarations = ([n['name'] for n in ranked_declarations(graph, p, request, latest)] if version >= 3 else
                        [n['name'] for n in graph['nodes'] if n['path'] == p and n['name'] != '<module>'])
        calls = [c for c in graph['calls'] if c['path'] == p]
        imports = [d['specifier'] for d in graph.get('module_dependencies', []) if d['source'] == p]
        if p.endswith('.py'):
            for n in ast.walk(ast.parse(sources[p])):
                if isinstance(n, ast.Import): imports.extend(a.name for a in n.names)
                elif isinstance(n, ast.ImportFrom): imports.append('.' * n.level + (n.module or ''))
        modules.append({'path': p, 'declarations': declarations[:6], 'declaration_count': len(declarations),
                        'imports': sorted(set(imports))[:8], 'import_count': len(set(imports)),
                        'calls': len(calls), 'unresolved': sum(c['target'] is None for c in calls)})
    # Keep all file names when possible; details yield to the breadth inventory.
    result = {'source_files': len(paths), 'files': ordered if version >= 3 else paths, 'modules': modules,
              'static_calls': len(graph['calls']),
              'unresolved_calls': sum(c['target'] is None for c in graph['calls']),
              'diagnostic_count': graph.get('diagnostic_count', len(graph['diagnostics'])),
              'omitted_files': 0, 'omitted_module_details': 0,
              'meaning': 'File/declaration/import inventory, not runtime execution or semantic review. Imports are specifiers, not resolved call edges.'}
    while len(canonical(result).encode()) > limit and result['modules']:
        result['modules'].pop(); result['omitted_module_details'] += 1
    while len(canonical(result).encode()) > limit and result['files']:
        result['files'].pop(); result['omitted_files'] += 1
    return result


def discovery_symbols(data, limit, request, latest=''):
    """Whole declaration addresses from the supplied fresh snapshot only.

    Interleave file and declaration relevance so a large early module cannot
    consume the index before other relevant entry points. The byte allocation
    bounds the address window; it does not cap declarations per file.
    """
    if type(limit) is not int or not 512 <= limit <= 8192:
        raise InvalidUpdate('Invalid discovery symbol-address allocation')
    graph, sources = data['graph'], data['sources']
    choices = []
    for file_rank, path in enumerate(ordered_sources(sources, graph, request, version=3, latest=latest)):
        for rank, node in enumerate(ranked_declarations(graph, path, request, latest)):
            if (type(node.get('line')) is not int or node['line'] < 1
                    or not isinstance(node.get('name'), str) or not node['name']):
                continue
            choices.append((file_rank + rank, file_rank, rank,
                            {'path': path, 'name': node['name'], 'line': node['line']}))
    result = []
    for _, _, _, address in sorted(choices, key=lambda item: item[:3]):
        if address in result:
            continue
        if len(canonical([*result, address]).encode()) > limit:
            break
        result.append(address)
    return result


def discovery_catalog(data, catalog, limit, request, latest=''):
    """Bound a lookup window without changing the committed catalog's scope.

    Imported paths are candidates matched against that catalog, never inferred
    execution edges or permission grants. No filesystem discovery happens here.
    """
    if type(limit) is not int or not 512 <= limit <= 4096:
        raise InvalidUpdate('Invalid discovery catalog-window allocation')
    allowed = set(catalog)
    sources, graph = data['sources'], data['graph']
    selected = [path for path in ordered_sources(sources, graph, request, version=3, latest=latest)
                if path in allowed]
    imports = {path: [] for path in selected}
    def add_import(path, specifier, target=None, python=False):
        if path not in imports:
            return
        candidates = [target] if target in allowed else []
        if isinstance(specifier, str) and specifier:
            if python:
                levels = len(specifier) - len(specifier.lstrip('.'))
                module = specifier[levels:].replace('.', '/')
                parent = posixpath.dirname(path)
                for _ in range(max(0, levels - 1)):
                    parent = posixpath.dirname(parent)
                bases = [posixpath.join(parent, module)]
                if not levels:
                    bases.append(module)
            else:
                bases = [posixpath.join(posixpath.dirname(path), specifier), specifier]
            for base in bases:
                base = posixpath.normpath(base)
                candidates.extend([base, *[base + suffix for suffix in ('.py', '.js', '.ts', '.tsx', '.jsx', '.json')],
                                   base + '/__init__.py', base + '/index.js', base + '/index.ts'])
        for candidate in candidates:
            if candidate in allowed and candidate not in imports[path]:
                imports[path].append(candidate)
    for dependency in graph.get('module_dependencies', []):
        add_import(dependency['source'], dependency.get('specifier'), dependency.get('target'))
    for path in selected:
        if not path.endswith('.py'):
            continue
        for node in ast.walk(ast.parse(sources[path])):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    add_import(path, alias.name, python=True)
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module] if node.module else [alias.name for alias in node.names]
                for module in modules:
                    add_import(path, '.' * node.level + module, python=True)
    recent, _ = retrieval_hints([latest], catalog)
    explicit, _ = retrieval_hints([request], catalog)
    focus = EvidenceFocus(request + '\n' + latest)
    fallback = sorted(catalog, key=lambda path: (-focus.strength(path), path))
    choices = [*recent, *explicit]
    for path in selected:
        choices.extend([path, *imports[path]])
    choices.extend(fallback)
    result = []
    for path in dict.fromkeys(choices):
        if path not in allowed:
            continue
        if len(canonical([*result, path]).encode()) > limit:
            break
        result.append(path)
    return result


def select_sites(graph, excerpts):
    """Prefer resolved relationships and distribute examples across read files."""
    calls = [c for c in graph['calls'] if c['path'] in excerpts]
    if not calls: calls = graph['calls']
    ordered = sorted(calls, key=lambda c: (c['target'] is None, c['path'], c['line'], c['id']))
    result = []; paths = set()
    for c in ordered:
        if c['path'] not in paths:
            result.append(c['id']); paths.add(c['path'])
        if len(result) == 4: break
    for c in ordered:
        if len(result) == 4: break
        if c['id'] not in result: result.append(c['id'])
    return result


def evidence(p, data):
    p['review_capabilities'] = dict(CAPABILITIES)
    version = p.get('child_evidence_version', p.get('parent_context_version', 1))
    notes = p.get('child_checkpoint') or p.get('recovery_checkpoint') or []
    latest = '\n'.join(notes[-1]['remaining']) if notes else ''
    p['workspace_map'] = overview(data, p['map_bytes'], query(p), version=version, latest=latest)
    if version >= 3:
        spans = {}
        if 'child_evidence_version' in p:
            from subagent_runtime import slice_excerpts
            slice_excerpts(p, data, source_spans=spans)
        else:
            from chat_runtime import packet_excerpts
            packet_excerpts(p, data, source_spans=spans)
        p['excerpt_spans'] = spans
    p['sites'] = select_sites(data['graph'], p['excerpts'])
    p['excerpt_locations'] = {}
    followup_files = []
    for name, excerpt in p['excerpts'].items():
        entry = data.get('followups', {}).get(name)
        if entry and entry['text'].startswith(excerpt):
            followup_files.append(name)
            line, partial = entry['line'], True
        elif version >= 3:
            line = p['excerpt_spans'][name]['start_line']
            partial = excerpt != data['sources'].get(name)
        else:
            source = data['sources'][name]
            line, partial = source[:source.find(excerpt)].count('\n') + 1, excerpt != source
        p['excerpt_locations'][name] = {'start_line': line, 'end_line': line + excerpt.count('\n'), 'partial_file': partial}
    p['evidence_coverage'] = {
        'fresh_snapshot_files': p['workspace_map']['source_files'], 'excerpt_files': sorted(p['excerpts']),
        'excerpt_bytes': sum(len(t.encode()) for t in p['excerpts'].values()),
        'child_reports': (p.get('parent_child_note_limit', len(p.get('child_results', [])))
                          if p.get('parent_source_funding_version') == 1 else len(p.get('child_results', []))),
        'exhaustive_semantic_review': False,
    }
    if data.get('followups'):
        p['evidence_coverage']['followup_files'] = sorted(followup_files)


def check(p, data):
    expected = dict(p); evidence(expected, data)
    for key in ('review_capabilities', 'workspace_map', 'sites', 'evidence_coverage', 'excerpt_locations'):
        if p.get(key) != expected[key]: raise InvalidUpdate('Review evidence context changed: ' + key)
    if p.get('child_evidence_version', p.get('parent_context_version', 1)) >= 3:
        if p.get('excerpt_spans') != expected['excerpt_spans']:
            raise InvalidUpdate('Review delivered source spans changed')
