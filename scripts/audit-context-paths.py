"""AST inventory of model-call consumers and their intended context authority.

This audits coverage of production origins, not runtime semantic completeness.
Consumer wire/Gateway regressions separately establish retained-page delivery.
No provider, ledger or user workspace is opened.
"""
import argparse
import ast
import json
import os
from pathlib import Path


PROFILES = {
    'architect': ('retained_working_context', 'Bound discovery notes/source lookup; current source required for citations.'),
    'chat_reviewer': ('retained_working_context', 'Writer retains discovery context; notes do not become source authority.'),
    'conversation': ('conversation_directory', 'Bound conversation memory, current user intent and pageable turn directory.'),
    'subagent': ('branch_checkpoint_context', 'Own branch checkpoints and parent request; no foreign task journal.'),
    'report_reviewer': ('independent_claim_sources', 'Independent claim pages and current source; writer notes are not verdict evidence.'),
    'section_writer': ('saved_report_section', 'Original request and bound source receipts for the current report section.'),
    'section_reviewer': ('saved_report_section_claims', 'One admitted claim and its exact cited excerpts for current report plans.'),
    'document_verdict_reviewer': ('independent_document_quotes', 'Bound answer/quotes; no inherited reasoning as evidence.'),
    'review_reader': ('source_navigation', 'Current source inventory and requested evidence; no permission authority from notes.'),
    'document_extract': ('bounded_document_section', 'Exact document section and offsets; other sections remain explicitly outside this call.'),
    'static_explainer': ('bounded_static_source', 'Fresh static graph/source slice; inventory is not runtime behavior.'),
    'api_mapper': ('bounded_api_source', 'Fresh indexed API source; earlier model output is unverified.'),
    'api_reviewer': ('independent_api_sources', 'Bound candidate and source facts; mapper claims alone are not evidence.'),
    'coder': ('paged_task_context', 'Pinned task, selected source pages and scoped prior output.'),
    'reviewer': ('paged_task_context', 'Pinned task, selected source pages and scoped prior output.'),
    'worker': ('paged_task_context', 'Verified selected evidence pages and nested directory.'),
    'selector': ('page_directory_selection', 'Advertised page addresses and catalog cursors, not arbitrary source access.'),
    'parser': ('current_user_text', 'Current explicit user text; older messages cannot create new permission.'),
    'memory_probe': ('independent_calibration_fixture', 'Registered tool-free synthetic fixture; no production Gateway call site.'),
}
REGISTERED_ONLY = {'memory_probe'}


def strings(node, values):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name):
        return set(values.get(node.id, ()))
    if isinstance(node, ast.IfExp):
        return strings(node.body, values) | strings(node.orelse, values)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return set().union(*(strings(item, values) for item in node.elts))
    return set()


def registered_roles(tree, harness):
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'expected_request')
    values = {}
    for node in ast.walk(function):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        module = harness / (node.module + '.py')
        if not module.is_file():
            continue
        exports = {alias.name: alias.asname or alias.name for alias in node.names}
        for statement in ast.parse(module.read_text()).body:
            if isinstance(statement, ast.Assign):
                for target in statement.targets:
                    if isinstance(target, ast.Name) and target.id in exports:
                        values[exports[target.id]] = strings(statement.value, {})
    roles = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == 'role':
            for other in node.comparators:
                roles.update(strings(other, values))
    return roles


def call_name(node):
    if isinstance(node, ast.Name): return node.id
    if isinstance(node, ast.Attribute): return call_name(node.value) + '.' + node.attr
    return '<expression>'


def origins(tree, path):
    globals_ = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name): globals_[target.id] = strings(statement.value, globals_)
    found = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)): continue
        invocations = [n for n in ast.walk(function) if isinstance(n, ast.Call)
                       and isinstance(n.func, ast.Attribute) and n.func.attr == 'invoke' and n.args]
        if not invocations: continue
        values = dict(globals_)
        for node in ast.walk(function):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name): values[target.id] = strings(node.value, values)
        # A module-local helper may accept role as a parameter (api_runtime).
        for index, parameter in enumerate(function.args.args):
            proposals = set()
            for candidate in ast.walk(tree):
                if (isinstance(candidate, ast.Call) and isinstance(candidate.func, ast.Name)
                        and candidate.func.id == function.name and len(candidate.args) > index):
                    proposals.update(strings(candidate.args[index], globals_))
            if proposals: values.setdefault(parameter.arg, proposals)
        for node in invocations:
            found.append({'path': path, 'function': function.name, 'line': node.lineno,
                          'roles': sorted(strings(node.args[0], values)),
                          'role_expression': ast.unparse(node.args[0]),
                          'context_calls': sorted({call_name(n.func) for n in ast.walk(function)
                                                   if isinstance(n, ast.Call)})[:80]})
    # Nested functions are visited through their outer definition too.
    return list({(row['line'], tuple(row['roles'])): row for row in found}.values())


def inventory(root):
    harness = Path(root) / 'harness'
    registry = registered_roles(ast.parse((harness / 'invocation_roles.py').read_text()), harness)
    rows = []
    for file in sorted(harness.glob('*.py')):
        if file.name.startswith('test_'): continue
        rows.extend(origins(ast.parse(file.read_text()), file.relative_to(root).as_posix()))
    observed = {role for row in rows for role in row['roles']}
    errors = []
    if registry - PROFILES.keys(): errors.append('Unclassified registered roles: ' + ', '.join(sorted(registry - PROFILES.keys())))
    if PROFILES.keys() - registry: errors.append('Context profile without registered role: ' + ', '.join(sorted(PROFILES.keys() - registry)))
    if registry - observed - REGISTERED_ONLY: errors.append('Registered roles without a production origin: ' + ', '.join(sorted(registry - observed - REGISTERED_ONLY)))
    if observed - registry: errors.append('Call origin uses unsupported roles: ' + ', '.join(sorted(observed - registry)))
    if any(not row['roles'] for row in rows): errors.append('Unresolved dynamic Gateway call origin')
    return {'format': 'context-origin-inventory-v1', 'authority': 'static_origin_coverage_only',
            'registered_roles': sorted(registry), 'production_call_sites': len(rows),
            'origins': rows, 'profiles': {role: {'profile': PROFILES[role][0], 'scope': PROFILES[role][1],
                'registered_only': role in REGISTERED_ONLY} for role in sorted(registry & PROFILES.keys())},
            'errors': errors, 'runtime_context_delivery_verified': False,
            'limits': ['AST origin coverage is not proof of model delivery, recall or correctness.',
                       'Independent claim/verdict consumers intentionally do not inherit writer notes as evidence.']}


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path); args = parser.parse_args()
    report = inventory(args.root.resolve())
    raw = json.dumps(report, indent=2, sort_keys=True) + '\n'
    if args.output:
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as stream: stream.write(raw)
    else: print(raw, end='')
    return int(bool(report['errors']))


if __name__ == '__main__': raise SystemExit(main())
