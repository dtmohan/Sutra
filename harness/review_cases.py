"""Host projection of explicitly enumerated questions and source defaults."""
import ast
import re

from context_manager import InvalidUpdate
from protocol import obj


_NUMBER = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'
_TUPLE = rf'\(\s*{_NUMBER}(?:\s*,\s*{_NUMBER})+\s*\)'
_TUPLES = re.compile(rf'{_TUPLE}(?:(?:\s*[,;]\s*(?:(?:and|or)\s+)?|\s+(?:and|or)\s+){_TUPLE})+')
_ITEM = re.compile(r'(?m)^\s*(?:\d+[.)]|[-*])\s+(\S[^\n]*)')
_SENTENCE = re.compile(r'(?<=[.?!])\s+(?=\S)')
_FOLLOWUP = re.compile(r'^(?:answer|calculate|compare|describe|determine|explain|identify|state|show)\b', re.I)
_DETAIL = re.compile(r'^(?:give|show|include)\s+(?:the|your)\s+(?:calculation|reasoning|working|inequality)\b', re.I)


def requested(prompt):
    """Find explicit lists and adjacent literal tuples in the latest user ask."""
    if not isinstance(prompt, str):
        return None
    rows = _TUPLES.search(prompt)
    if rows:
        before = _SENTENCE.split(prompt[:rows.start()].strip())
        instruction = before[-1] if before else ''
        items = re.findall(_TUPLE, rows[0])
        tuple_count = len(items)
        rest = prompt[rows.end():].lstrip(' .;:\n')
        for sentence in _SENTENCE.split(rest):
            if len(items) > tuple_count and _DETAIL.match(sentence):
                items[-1] += ' ' + sentence
            elif sentence.endswith('?') or _FOLLOWUP.match(sentence):
                if sentence:
                    items.append(sentence)
    else:
        numbered = list(_ITEM.finditer(prompt))
        if len(numbered) < 2:
            return None
        instruction = prompt[:numbered[0].start()].strip()
        items = [row[1].strip() for row in numbered]
    return {'version': 1, 'instruction': instruction,
            'items': {str(index): item for index, item in enumerate(items, 1)}}


def add_schema(schema, cases):
    if cases:
        schema['properties']['answer']['maxLength'] = 240
        schema['properties']['case_answers'] = obj({
            key: {'type': 'string', 'minLength': 1, 'maxLength': 360}
            for key in cases['items']})
        schema['required'].append('case_answers')
    return schema


def check(cases, result):
    answers = result.get('case_answers', {})
    if set(answers) != set(cases['items']) or any(not answer.strip() for answer in answers.values()):
        raise InvalidUpdate('Answer every numbered case with its decision and reason, or identify its evidence gap')


def source_details(prompt, sources, excerpts):
    """Copy defaults and explicitly requested methods from admitted source."""
    reference = set(re.findall(r'\b[A-Za-z_]\w*\b', prompt))
    result = {}
    for path in excerpts:
        if not path.endswith('.py') or path not in sources:
            continue
        text = sources[path]
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        lines = text.splitlines(keepends=True)
        records = []
        for owner in ast.walk(tree):
            if not isinstance(owner, ast.ClassDef) or owner.name not in reference:
                continue
            for node in owner.body:
                if (isinstance(node, ast.FunctionDef) and node.name == '__init__'
                        and (node.args.defaults or any(node.args.kw_defaults))):
                    stop = min(node.end_lineno, node.lineno + 6)
                elif isinstance(node, ast.FunctionDef) and node.name in reference:
                    stop = node.end_lineno
                elif (isinstance(node, (ast.Assign, ast.AnnAssign))
                        and isinstance(node.value, (ast.Constant, ast.UnaryOp))):
                    stop = node.end_lineno
                else:
                    continue
                declaration = ''.join(lines[node.lineno-1:stop])
                if declaration and len(declaration.encode()) <= 900 and declaration not in excerpts[path]:
                    records.append({'owner': owner.name, 'line': node.lineno, 'text': declaration})
        if records:
            result[path] = records
    return result


def markdown(cases, proposal):
    from review_report import prose
    body = proposal['answer'].strip()
    rows = [key + '. **' + prose(cases['items'][key]) + '** — ' + prose(proposal['case_answers'][key])
            for key in sorted(cases['items'], key=int)]
    return body + '\n\n' + '\n'.join(rows)
