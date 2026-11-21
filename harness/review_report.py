"""Structured review presentation; source anchors do not prove model conclusions."""
import ast
import posixpath
import re
from context_manager import InvalidUpdate
from protocol import obj, arr


def brief(p):
    """Choose presentation from the caller's ask, not source or model notes.

    This describes output genre, not difficulty, correctness or permission.
    Keep the old keyword recipe for historical accepted deliveries.
    """
    text = p['contract']['goal'] + '\n' + p['request']
    old = bool(re.search(r'architectur|overview|strengths|shortcomings', text, re.I))
    if 'review_intent_version' not in p:
        return {'structured': old}
    if type(p['review_intent_version']) is not int or p['review_intent_version'] != 1:
        raise InvalidUpdate('Unsupported review presentation intent')
    workspace = bool(re.search(r'\b(?:codebase|repositories|repository|repo|workspace|project|modules?|'
                               r'source\s+code|code\s+(?:logic|flow)|tools?\s+logic)\b', text, re.I))
    analysis = bool(re.search(r'\b(?:review|analy[sz]e|inspect|explain|understand|thoughts?|opinion|'
                              r'walk\s+(?:me\s+)?through|go\s+through)\b', text, re.I))
    diagram = bool(re.search(r'\b(?:diagrams?|flowcharts?|(?:control|request|data|call)[ -]+flow)\b', text, re.I))
    structured = old or workspace and (analysis or diagram)
    return {'structured': bool(structured), 'kind': 'architecture' if structured else 'answer',
            'detail': 'detailed' if re.search(r'\bin\s+detail\b|\bdetailed\b|\bdeep\s+dive\b|\bintern\b', text, re.I) else 'standard',
            'diagram_requested': diagram,
            'purpose': 'Explain how the supplied project works, with source-linked components, control flow, strengths and weaknesses.'
                       if structured else 'Answer the specific question from supplied evidence.'}


def requested(p):
    return brief(p)['structured']


def text(limit):
    return {'type': 'string', 'minLength': 1, 'maxLength': limit}


def schema(p=None):
    from claim_pages import source_paths
    delivered = source_paths(p) if p else []
    recovery = bool(p and 'parent_draft_recovery' in p)
    if recovery:
        paths = dict(arr(dict(text(512), enum=delivered)), minItems=1, maxItems=min(2, len(delivered)))
        finding = obj({'observation': text(200), 'implication': text(200), 'paths': paths})
        return obj({
            'overview': text(500),
            'components': dict(arr(obj({'name': text(80), 'responsibility': text(240), 'paths': paths})), minItems=1, maxItems=4),
            'flow': dict(arr(obj({'description': dict(text(260), minLength=30), 'paths': paths})), minItems=1, maxItems=5),
            'strengths': dict(arr(finding), maxItems=1),
            'risks': dict(arr(finding), maxItems=1),
            'next_steps': dict(arr(dict(text(160), minLength=30)), maxItems=2),
        })
    paths = dict(arr(dict(text(512), enum=delivered) if p else text(512)), minItems=1, maxItems=3)
    finding = obj({'observation': text(600), 'implication': text(400), 'paths': paths})
    result = obj({
        'overview': text(1000),
        'components': dict(arr(obj({'name': text(100), 'responsibility': text(500), 'paths': paths})), minItems=1, maxItems=6),
        'flow': dict(arr(obj({'description': dict(text(600), minLength=30), 'paths': paths})), minItems=1, maxItems=6),
        'strengths': dict(arr(finding), maxItems=4),
        'risks': dict(arr(finding), maxItems=4),
        'next_steps': dict(arr(dict(text(300), minLength=30)), maxItems=4),
    })
    if p and p.get('report_schema_version') == 2:
        # Narrative capacity follows the governed response budget. Source
        # references remain bounded by the actual delivered path choices.
        paths['maxItems'] = len(delivered)
        for section in ('components', 'flow', 'strengths', 'risks', 'next_steps'):
            result['properties'][section].pop('maxItems')
    return result


def validate(p, proposal):
    if not requested(p): return
    report = proposal['report']
    inventory = set(p['workspace_map']['files'])
    from claim_pages import source_paths
    read = set(source_paths(p))
    for section in ('components', 'flow', 'strengths', 'risks'):
        allowed = read
        for item in report[section]:
            if not set(item['paths']) <= allowed:
                raise InvalidUpdate('Review ' + section + ' cites unread files: ' + ', '.join(sorted(set(item['paths']) - allowed)))
    for item in report['flow']:
        if len(re.findall(r'[A-Za-z]{2,}', item['description'])) < 4:
            raise InvalidUpdate('Each request-flow description must explain an operation, not just number a step')
    for step in report['next_steps']:
        if len(step.split()) < 4:
            raise InvalidUpdate('Each next check must describe a concrete action, not a placeholder')
    # A source link is an anchor, never an assertion of semantic correctness.
    if '```' in str(report): raise InvalidUpdate('Review sections are prose; the host renders the source diagram')


def prose(value):
    # Structural Markdown is host-owned. Model content remains inert prose.
    value = ' '.join(value.split())
    parts = re.split(r'(`[^`\n]+`)', value)
    return ''.join(part if part.startswith('`') and part.endswith('`') else
                   re.sub(r'([\\`*_{}\[\]<>#|])', r'\\\1', part) for part in parts)


def references(paths):
    def code(path):
        fence = '`' * (1 + max((len(run) for run in re.findall(r'`+', path)), default=0))
        return fence + ' ' + path + ' ' + fence
    return ' · '.join(code(p) for p in paths)


def markdown(proposal):
    r = proposal.get('report')
    if not r: return proposal.get('answer') or proposal.get('summary', '')
    lines = [prose(r['overview']), '', '## Main components', '']
    for row in r['components']:
        lines += ['- **' + prose(row['name']) + '** — ' + prose(row['responsibility']),
                  '  Source: ' + references(row['paths']), '']
    lines += ['## How a request moves through the code', '']
    for i, row in enumerate(r['flow'], 1):
        lines += [str(i) + '. ' + prose(row['description']) + ' (' + references(row['paths']) + ')', '']
    for field, title in [('strengths', 'Strengths'), ('risks', 'Risks and tradeoffs')]:
        lines += ['## ' + title, '']
        if not r[field]: lines += ['No specific finding supported by the delivered excerpts.', '']
        for row in r[field]:
            lines += ['- ' + prose(row['observation']) + ' **Why it matters:** ' + prose(row['implication']),
                      '  Source: ' + references(row['paths']), '']
    if r['next_steps']:
        lines += ['## Recommended next checks', '']
        lines += [str(i) + '. ' + prose(step) + '\n' for i, step in enumerate(r['next_steps'], 1)]
    if proposal.get('publication_note'):lines += ['', prose(proposal['publication_note'])]
    if proposal.get('claim_evidence'):
        from pathlib import Path
        # The full claim-to-ID mapping is retained in the structured review
        # details. Repeating it as a large table obscures the explanation in chat.
        # Group only identical locations here; never infer semantic support from
        # the existence of an authenticated source address.
        locations = {}
        for entry in proposal['claim_evidence']:
            line = entry['start_line']
            uri = (Path(entry['workspace']) / entry['path']).as_uri() + ('#L' + str(line) if line else '')
            label = prose(entry['path']) + (':' + str(line) if line else '')
            locations[(entry['path'], line)] = '[' + label + '](' + uri + ')'
        lines += ['', '**Source evidence:** ' + ' · '.join(locations.values()), '',
                  'Full claim and evidence-ID mappings are in conversation details. Source links establish provenance; reviewer judgments remain unverified.']
    return '\n'.join(lines).strip()


def diagram(graph, preferred, sources=None):
    """Local import candidates; never promote shared-script globals to edges."""
    sources = sources or {}
    pairs = set()
    def target(source, specifier, python=False, level=0):
        if python:
            base = posixpath.dirname(source)
            for _ in range(max(0, level-1)): base = posixpath.dirname(base)
            part = specifier.replace('.', '/')
            roots = [posixpath.join(base, part)] if level else [part, posixpath.join(base, part)]
            candidates = [p + suffix for p in roots for suffix in ('.py', '/__init__.py')]
        else:
            if not specifier.startswith('.'): return
            base = posixpath.normpath(posixpath.join(posixpath.dirname(source), specifier))
            candidates = [base] + [base + suffix for suffix in ('.ts','.tsx','.js','.jsx','/index.ts','/index.js')]
        found = next((p for p in candidates if p in sources), None)
        if found and found != source: pairs.add((source, found))
    for dep in graph.get('module_dependencies', []): target(dep['source'], dep['specifier'])
    for path, body in sources.items():
        if not path.endswith('.py'): continue
        for n in ast.walk(ast.parse(body)):
            if isinstance(n, ast.Import):
                for alias in n.names: target(path, alias.name, True)
            elif isinstance(n, ast.ImportFrom): target(path, n.module or '', True, n.level)
    paths = list(dict.fromkeys(list(preferred) + [p for pair in sorted(pairs) for p in pair]))[:16]
    if not paths: return None
    ids = {p: 'n' + str(i + 1) for i, p in enumerate(paths)}
    selected = [pair for pair in sorted(pairs) if set(pair) <= set(paths)][:24]
    return {'title': 'Local source dependencies', 'direction': 'LR',
            'note': f'{len(selected)} of {len(pairs)} local import candidates shown. Derived from import/require syntax and existing files; runtime loading and execution order are not proved.',
            'nodes': [{'id': ids[p], 'label': p[:100], 'kind': 'process'} for p in paths],
            'edges': [{'source': ids[a], 'target': ids[b], 'label': 'local import'} for a, b in selected]}


def flow_diagram(proposal):
    """Render the published explanation's sequence without inventing call edges."""
    steps = (proposal.get('report') or {}).get('flow', [])
    if not steps:
        return None
    shown = steps[:16]
    nodes = []
    for index, item in enumerate(shown):
        label = str(index + 1) + '. ' + ' '.join(item['description'].split())
        if len(label) > 100:
            label = label[:97].rsplit(' ', 1)[0] + '…'
        nodes.append({'id': 'n' + str(index + 1), 'label': label,
                      'kind': 'source' if index == 0 else 'output' if index == len(shown)-1 else 'process'})
    return {'title': 'Request flow from the review', 'direction': 'TB',
            'note': (f'First {len(shown)} of {len(steps)} report steps shown. ' if len(shown) < len(steps) else '') + 'An illustration of the report’s ordered explanation. Arrows mean the next described step, not independently measured calls. Full descriptions and source links are in chat.',
            'nodes': nodes, 'edges': [{'source': nodes[i]['id'], 'target': nodes[i+1]['id'],
                                      'label': 'next described step'} for i in range(len(nodes)-1)]}
