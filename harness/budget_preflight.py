"""Offline packet-size/closure preflight; host-seeded contract, no model calls."""
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from checked_protocol import SCHEMA, SYSTEM, worker_schema
from governance import GovernedContext as EvidenceContext
from validate_dag import build_new, new_definitions, POLICY_REQUEST


def host_seed(store, session, text, model, source):
    assert text == POLICY_REQUEST
    fields = [
        ('task_id', 'POLICY-31', 'Ticket POLICY-31.'),
        ('goal', 'Evaluate deployment policy.', 'Goal: Evaluate deployment policy.'),
        ('allowed_paths', 'src/policy.py,tests/test_policy.py', 'Edit only src/policy.py and tests/test_policy.py.'),
        ('network_allowed', 'false', 'No network access.'),
        ('dependencies_allowed', 'false', 'No new dependencies.')]
    store.apply_user_patch(session, source, text,
        [{'key': key, 'value': value, 'quote': quote} for key, value, quote in fields], 0)
    return {'calls': []}


def main():
    rows = []
    with tempfile.TemporaryDirectory() as directory, patch('experiment.ingest', host_seed):
        for definition in new_definitions():
            case = build_new(definition, Path(directory) / definition['id'], None)
            store = EvidenceContext(case['db'])
            try:
                for expand in (True, False):
                    payload, _, meta = store.assemble(case['session'], case['request'],
                        ['tests/test_policy.py'] + case['seed_pages'], required_facts=case['required_facts'],
                        system=SYSTEM, schema=SCHEMA, final=True, expand=expand, policy_request=case.get('policy_request'),
                        schema_factory=worker_schema)
                    delivered = {page['id']: page for page in payload['pages']}
                    closure = all(dep in delivered and delivered[dep]['version'] == version
                        for page in delivered.values() for dep, version in page.get('depends_on', []))
                    assert not expand or closure
                    assert 'history' not in payload and 'expected' not in payload
                    rows.append({'case': case['id'], 'expand': expand,
                        'charged_input': meta['budget_charge'], 'total_with_output': meta['budget_charge'] + 1024,
                        'pages': list(delivered), 'dependency_closure_complete': closure,
                        'states': payload['evidence_index']})
            finally:
                store.close()
    print(json.dumps({'model_calls': 0, 'host_seeded_contract': True, 'conditions': rows}, indent=2))


if __name__ == '__main__': main()
