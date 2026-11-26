"""Select a small set of source locations for a grounded architecture report."""
import re
from collections import Counter
from pathlib import PurePosixPath

from review_context import EvidenceFocus, explicit_symbol, ordered_sources, ranked_declarations


PURPOSE = ('readme', 'overview', 'architecture', 'design', 'about')
VALIDATION = ('validation', 'known-limitations', 'limitations', 'known-issues', 'testing', 'benchmarks')
REGISTRATION = {'requestanimationframe', 'addeventlistener', 'addroute', 'register', 'listen', 'serve'}


def _document(catalog, stems, entry, focus):
    matches = [PurePosixPath(name) for name in catalog]
    matches = [path for path in matches if path.suffix.lower() in ('.md', '.rst', '.txt', '.adoc')
               and path.stem.lower().replace('_', '-') in stems]
    if matches:
        return str(min(matches, key=lambda path: (
            not focus.mentions_module(str(path)), path.parent != PurePosixPath(entry).parent,
            len(path.parts), stems.index(path.stem.lower().replace('_', '-')), str(path))))
    return None


def select(data, catalog, request):
    """Map task focus and graph references to read addresses, never source claims."""
    graph = data['graph']
    catalog = set(catalog)
    sources = {path: value for path, value in data['sources'].items() if path in catalog}
    paths = ordered_sources(sources, graph, request, version=3)
    if not paths:
        return []
    focus = EvidenceFocus(request)
    priority = {path: index for index, path in enumerate(paths)}
    nodes = {node['id']: node for node in graph['nodes'] if node['path'] in priority}
    roots = [call for call in graph['calls'] if call['path'] in priority
             and nodes.get(call['source'], {}).get('name') == '<module>']

    def expression(call):
        return call['expression'].rsplit('.', 1)[-1].lower()

    def registration_priority(call):
        name = expression(call)
        return 0 if name == 'requestanimationframe' else 1 if name in REGISTRATION else 2

    entry_calls = [call for call in roots if call.get('target') in nodes or expression(call) in REGISTRATION]
    entry = min(entry_calls, default=None, key=lambda call: (
        not focus.mentions_module(call['path']), registration_priority(call),
        priority[call['path']], call['line'], call['id']))
    entry_path = entry['path'] if entry else paths[0]
    targets = []

    def add(role, path, line, lines):
        if path and not any(row['path'] == path and abs(row['line'] - line) < 12 for row in targets):
            targets.append({'role': role, 'path': path, 'line': line,
                            'profile': {'version': 1, 'max_bytes': 16384, 'max_lines': lines}})

    purpose = _document(catalog, PURPOSE, entry_path, focus)
    add('purpose', purpose, 1, 256)
    add('entry', entry_path, max(1, entry['line'] - 8) if entry else 1, 32)
    declarations = ranked_declarations(graph, entry_path, request)
    callee = nodes.get(entry.get('target')) if entry else None
    if entry and expression(entry) == 'requestanimationframe':
        callback = re.search(r'\brequestAnimationFrame\(\s*([A-Za-z_$][\w$]*)\s*\)', entry['text'])
        if callback:
            callee = next((node for node in declarations if node['name'] == callback[1]), callee)
    explicit = next((node for node in declarations if explicit_symbol(focus, entry_path, node['name'])), None)
    callee = explicit or callee or (declarations[0] if declarations else None)
    if callee:
        add('implementation', callee['path'], callee['line'], 64)
    add('validation', _document(catalog, VALIDATION, entry_path, focus), 1, 256)

    # Prefer a real, independent callback with local work over adjacent text.
    callbacks = {(node['path'], node['line']): node['id'] for node in nodes.values()
                 if '<anonymous' in node['name']}
    outgoing = Counter(call['source'] for call in graph['calls'] if call.get('target') in nodes)
    event_calls = [call for call in roots if call is not entry and expression(call) == 'addeventlistener'
                   and outgoing[callbacks.get((call['path'], call['line']))]]
    if event_calls:
        event = min(event_calls, key=lambda call: (-outgoing[callbacks[(call['path'], call['line'])]],
                    priority[call['path']], call['line'], call['id']))
        add('event_path', event['path'], event['line'], 64)
    else:
        related = next((nodes[call['target']] for node in [callee] if node for call in graph['calls']
                        if call['source'] == node['id'] and call.get('target') in nodes
                        and nodes[call['target']]['path'] != node['path']), None)
        if related:
            add('dependency', related['path'], related['line'], 64)
        elif len(paths) > 1:
            add('alternate_source', next(path for path in paths if path != entry_path), 1, 64)
    return targets
