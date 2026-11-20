import ast
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('audit_context', Path(__file__).with_name('audit-context-paths.py'))
audit = importlib.util.module_from_spec(spec); spec.loader.exec_module(audit)


class OriginCoverageTests(unittest.TestCase):
    def test_production_registry_and_all_gateway_origins_are_classified(self):
        report = audit.inventory(Path(__file__).resolve().parents[1])
        self.assertEqual(report['errors'], [])
        self.assertGreater(report['production_call_sites'], 10)
        self.assertFalse(report['runtime_context_delivery_verified'])
        self.assertEqual(report['profiles']['report_reviewer']['profile'], 'independent_claim_sources')

    def test_conditional_constants_and_forwarded_role_parameters_resolve(self):
        source = '''ROLE = 'document_extract'
def explicit(gateway):
    gateway.invoke(ROLE, 'id')
    role = 'coder' if edit else 'reviewer'
    gateway.invoke(role, 'id')
def forwarded(store, role):
    gateway.invoke(role, 'id')
def route():
    forwarded(store, 'api_mapper')
    forwarded(store, 'api_reviewer')
def paged():
    gateway.invoke('worker' if ready else 'selector', 'id')
'''
        rows = audit.origins(ast.parse(source), 'fixture.py')
        self.assertEqual({role for row in rows for role in row['roles']},
                         {'document_extract', 'coder', 'reviewer', 'api_mapper', 'api_reviewer', 'worker', 'selector'})
        self.assertFalse(any(not row['roles'] for row in rows))

    def test_unknown_dynamic_origin_remains_visible_instead_of_assuming_context(self):
        rows = audit.origins(ast.parse('def dispatch(role):\n    gateway.invoke(role, "id")\n'), 'new.py')
        self.assertEqual(rows[0]['roles'], [])
        self.assertEqual(rows[0]['role_expression'], 'role')


if __name__ == '__main__': unittest.main()
