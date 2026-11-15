"""Bounded structural context from fresh source, never model-created authority."""
import ast
from pathlib import Path
from context_manager import InvalidUpdate, canonical
from paging import Budget

CAPABILITIES = {
    'execution': 'host reads source; this model analyses supplied evidence',
    'source_field': 'excerpts',
    'structure_field': 'workspace_map',
    'pages_field': 'optional Smriti pages, not the source inventory',
    'implementation_present': 'tracks harness-created artifacts, not whether repository source exists',
    'handoff': 'optional earlier reviewer note; null does not disable source analysis',
}


def query(p):
    return p['contract']['goal'] + '\n' + p['request']


def ordered_sources(sources, graph, request):
    from auto_review import ranked
    import re
    names = ranked(sources, request)
    if not re.search(r'architecture|architectural|overview', request, re.I): return names
    counts = {p: 0 for p in names}
    for call in (graph or {}).get('calls', []):
        if call['path'] in counts: counts[call['path']] += 1
    positions = {p: i for i, p in enumerate(names)}
    entry = {'main', 'index', 'app', 'server', 'extension', '__main__'}
    def score(name):
        p = Path(name)
        auxiliary = bool(set(p.parts) & {'test', 'tests', 'examples', 'fixtures'}) or p.stem.startswith(('test_', 'test-'))
        return (name.lower() not in request.lower(), auxiliary, p.stem not in entry, -counts[name], positions[name])
    return sorted(names, key=score)


def context_budget(provider, s, sid, slots=2):
    """Use installed capability within remaining task and ancestor allocations.

    This only proposes a packet size. The gateway still charges every attempt
    and the unchanged kernel must admit it. Reserve one full repair; any later
    attempt still needs fresh admission within the remaining shared budget.
    """
    from model_context import allocation
    import review_budget
    import orchestration as machine
    budget, metadata = allocation(provider.context_metadata)
    available = budget.context_limit * slots
    task = review_budget.usage(s, sid)
    if task:
        available = min(available, max(0, task['limits']['tokens'] - task['used']['tokens']))
    owner = machine.ACTIVE.get() or machine.task(s, sid)['id']
    if owner:
        for frame in machine.gates(s, owner, {}):
            available = min(available, max(0, frame['limits']['tokens'] - frame['used']['tokens']))
    limit = min(budget.context_limit, int(available // slots))
    # A structured architecture report also carries the full constraint echo and
    # source citations. Reserve output within the same per-call/task allocation.
    reserve = max(budget.response_reserve, min(8192, limit // 3))
    if limit <= reserve + 1024:
        # Do not invent extra allocation. The normal budget checks stop dispatch.
        limit = max(1, limit)
    provider.budget = Budget(limit, reserve, budget.template_reserve)
    return dict(metadata, review_context_allocation=limit, attempt_slots_reserved=slots, repair_slots_reserved=1)


def overview(data, limit, request='Architecture overview'):
    if type(limit) is not int or not 512 <= limit <= 65536:
        raise InvalidUpdate('Invalid structural context allocation')
    graph = data['graph']; sources = data['sources']
    paths = sorted(p for p in sources if Path(p).suffix in {'.py', '.js', '.jsx', '.ts', '.tsx'})
    modules = []
    for p in ordered_sources({p:sources[p] for p in paths}, graph, request):
        declarations = [n['name'] for n in graph['nodes'] if n['path'] == p and n['name'] != '<module>']
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
    result = {'source_files': len(paths), 'files': paths, 'modules': modules,
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
    p['workspace_map'] = overview(data, p['map_bytes'], query(p))
    p['sites'] = select_sites(data['graph'], p['excerpts'])
    p['excerpt_locations'] = {}
    for name, excerpt in p['excerpts'].items():
        start = data['sources'][name].find(excerpt)
        line = data['sources'][name][:start].count('\n') + 1
        p['excerpt_locations'][name] = {'start_line': line, 'end_line': line + excerpt.count('\n'), 'partial_file': excerpt != data['sources'][name]}
    p['evidence_coverage'] = {
        'fresh_snapshot_files': p['workspace_map']['source_files'], 'excerpt_files': sorted(p['excerpts']),
        'excerpt_bytes': sum(len(t.encode()) for t in p['excerpts'].values()),
        'child_reports': len(p.get('child_results', [])), 'exhaustive_semantic_review': False,
    }


def check(p, data):
    expected = dict(p); evidence(expected, data)
    for key in ('review_capabilities', 'workspace_map', 'sites', 'evidence_coverage', 'excerpt_locations'):
        if p.get(key) != expected[key]: raise InvalidUpdate('Review evidence context changed: ' + key)
