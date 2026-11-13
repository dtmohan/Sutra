"""Offline audit of exact payloads, spans, dependency closure and private scores."""
import argparse
import hashlib
import json
from pathlib import Path

from context_manager import canonical, sha
from governance import GovernedContext as EvidenceContext
from validate_dag import score_new
from checked_protocol import attach_contract, HOST_FIELDS, worker_schema
from policy_check import check as check_policy


def audit(root, allow_incomplete=False):
    plan = json.loads((root / 'plan.json').read_text())
    results = json.loads((root / 'results.json').read_text())
    planned = {(d['id'], 'dag') for d in plan['regression_cases']}
    planned |= {(d['id'], mode) for d in plan['new_cases'] for mode in ('dag', 'leaf_only')}
    observed = [(r['case'], r['mode']) for r in results]
    assert len(set(observed)) == len(observed), 'Duplicate condition result'
    assert set(observed) <= planned, 'Unexpected condition result'
    complete = len(results) == plan['planned_conditions'] == len(planned)
    assert complete or allow_incomplete, 'Incomplete run; use --allow-incomplete to audit recorded evidence only'
    calls = {p.stem: json.loads(p.read_text()) for p in (root / 'calls').glob('*.json')}
    cases = {p.parent.name: json.loads(p.read_text()) for p in (root / 'cases').glob('*/case.json')}
    receipts, spans, details = 0, 0, []
    for result in results:
        case = cases[result['case']]
        assert result['score'] == score_new(result['answer'], case['expected'])
        store = EvidenceContext(root / 'cases' / result['case'] / 'state.sqlite')
        assert store.validate_chain(case['session']) and store.audit_valid()
        assert store.verify_protocol(case['session'])
        final_payload = {}
        for call_id, turn in zip(result['call_ids'], result['turns']):
            record = calls[call_id]
            actual = json.loads(record['request']['messages'][1]['content'])
            row = store.db.execute('SELECT payload_json,payload_hash FROM deliveries WHERE id=?', (turn['delivery_id'],)).fetchone()
            assert row and row[0] == canonical(actual) and row[1] == sha(row[0])
            assert store.db.execute('SELECT 1 FROM delivery_success WHERE id=?', (turn['delivery_id'],)).fetchone()
            receipts += 1
            for page in actual.get('pages', []):
                stored = store.page(case['session'], page['id'], page['version'])
                assert page['text'] == stored.payload['text'] and page['facts'] == stored.payload['facts']
                assert page['hash'] == stored.content_hash
                if 'source_span' in page:
                    span = page['source_span']
                    doc = store.document(case['session'], span['document_id'], span['document_version'])
                    assert doc['text'][span['start']:span['end']] == page['text']
                    assert sha(doc['text']) == span['document_hash'] and sha(page['text']) == span['span_hash']
                    spans += 1
            if turn['stage'] == 'worker':
                final_payload = actual
                candidate = next((a for a in result.get('attempts', []) if a.get('last_delivery_id') == turn['delivery_id']), result)
                assert record['request']['format'] == worker_schema(actual)
                assert candidate['model_proposal'] == record['answer']
                assert not HOST_FIELDS & set(record['answer'])
                assert candidate['answer'] == attach_contract(record['answer'], actual['contract'])
                assert candidate['contract_origin']['delivery_id'] == turn['delivery_id']
                assert candidate['contract_origin']['payload_hash'] == row[1]
                verification = check_policy(actual, record['answer'])
                logged = candidate['admission']['policy_verification']
                if logged['status'] == 'admission_rejected':
                    assert verification['verified'] and not candidate['admission']['admitted']
                    assert not logged['verified']
                else:
                    assert logged == verification
                if candidate['admission']['admitted'] and record['answer']['interpretation']:
                    assert verification['verified']
                    assert candidate['admission']['interpretation_semantics_verified']
        delivered = {p['id']: p for p in final_payload.get('pages', [])}
        dependencies = case.get('required_dependencies', [])
        supported_interpretation = bool(case['expected']['interpretation'])
        closure = all(dep in delivered and delivered[dep]['version'] == version
                      for page in delivered.values() for dep, version in page.get('depends_on', []))
        prefix = result['case'] + '-' + result['mode'] + '-'
        attempts = [v for k, v in calls.items() if k.startswith(prefix)]
        detail = dict(result)
        detail.update(
            failed_checks=[k for k, v in result['score']['checks'].items() if not v],
            interpretation_case=supported_interpretation,
            dependencies_present=bool(dependencies) and all(d in delivered for d in dependencies),
            dependency_closure_complete=closure,
            model_calls=len(attempts), failed_calls=sum(not x.get('ok') for x in attempts),
            prompt_tokens=sum(x.get('response', {}).get('prompt_eval_count', 0) for x in attempts),
            output_tokens=sum(x.get('response', {}).get('eval_count', 0) for x in attempts),
            elapsed_s=sum(x.get('elapsed_s', 0) for x in attempts))
        details.append(detail)
        store.close()
    for record in calls.values():
        request = record['request']; messages = request['messages']
        assert len(messages) == 2 and [m['role'] for m in messages] == ['system', 'user']
        payload = json.loads(messages[1]['content'])
        assert not {'expected', 'history', 'conversation'} & set(payload)
        assert record['conservative_input_charge'] + 1024 <= 8192
        if record.get('ok'):
            assert record['response']['prompt_eval_count'] + 1024 <= 8192
    groups = {}
    for cohort, mode in [('regression', 'dag'), ('paired', 'dag'), ('paired', 'leaf_only')]:
        rows = [r for r in details if r['cohort'] == cohort and r['mode'] == mode]
        groups[cohort + '/' + mode] = {
            'cases': len(rows), 'contract_preserved': sum(r['score']['contract_preserved'] for r in rows),
            'strict_passed': sum(r['score']['passed'] for r in rows),
            'evidence_states_correct': sum(r['score']['checks']['explicit_evidence_states'] for r in rows),
            'facts_correct': sum(r['score']['checks']['facts'] for r in rows),
            'interpretation_cases': sum(r['interpretation_case'] for r in rows),
            'interpretations_correct': sum(r['interpretation_case'] and r['score']['checks']['interpretation'] for r in rows),
            'interpretation_prerequisites_present': sum(r['interpretation_case'] and r['dependencies_present'] for r in rows),
            'admitted': sum(r['admission']['admitted'] for r in rows),
            'admitted_but_strict_failed': sum(r['admission']['admitted'] and not r['score']['passed'] for r in rows),
            'policy_mismatches_rejected': sum(r['admission'].get('policy_verification', {}).get('status') == 'mismatch'
                                             and not r['admission']['admitted'] for r in rows),
            'semantics_verified': sum(r['admission'].get('interpretation_semantics_verified', False) for r in rows),
            'repair_attempts': sum(max(0,len(r.get('attempts', []))-1) for r in rows),
            'protocol_states': {state: sum(r.get('protocol', {}).get('state') == state for r in rows) for state in ('ALLOW','FIN','RESOURCE_WAIT')},
            'blocked': sum(bool(r.get('blocked')) for r in rows),
            **{key: sum(r[key] for r in rows) for key in ('model_calls', 'failed_calls', 'prompt_tokens', 'output_tokens', 'elapsed_s')}}
    ingestion = [v for k, v in calls.items() if '-ingest-' in k]
    health = [json.loads(line) for line in (root / 'health.jsonl').read_text().splitlines()]
    before, after = [json.loads((root / (name + '.json')).read_text()) for name in ('before', 'after')]
    frozen = json.loads((root / 'source-hashes.json').read_text())
    changes = [name for name, digest in frozen.items() if hashlib.sha256((root.parent / name).read_bytes()).hexdigest() != digest]
    summary = {'groups': groups, 'conditions': len(results), 'expected_conditions': plan['planned_conditions'],
        'run_complete': complete,
        'conditions_without_result': [{'case': case, 'mode': mode} for case, mode in sorted(planned - set(observed))],
        'call_status': {'succeeded': sum(c.get('ok') is True for c in calls.values()),
                        'failed': sum(c.get('ok') is False for c in calls.values()),
                        'interrupted_without_response': sum('ok' not in c for c in calls.values())},
        'audit': {'call_logs': len(calls), 'verified_receipts': receipts, 'verified_delivered_spans': spans,
                  'fresh_two_message_calls': True, 'history_replay': False, 'all_budgets_fit': True,
                  'contract_fields_owned_by_host': True, 'policy_checks_reproduced': True,
                  'max_prompt_tokens': max(c.get('response', {}).get('prompt_eval_count', 0) for c in calls.values()),
                  'max_charged_input_plus_output': max(c['conservative_input_charge'] + 1024 for c in calls.values()),
                  'frozen_source_changes': changes,
                  'thinking_responses': sum(bool(c.get('response', {}).get('message', {}).get('thinking')) for c in calls.values())},
        'ingestion': {'model_calls': len(ingestion),
                      'prompt_tokens': sum(c.get('response', {}).get('prompt_eval_count', 0) for c in ingestion),
                      'output_tokens': sum(c.get('response', {}).get('eval_count', 0) for c in ingestion),
                      'elapsed_s': sum(c['elapsed_s'] for c in ingestion)},
        'model_parameters_unchanged': before['model']['parameters'] == after['model']['parameters'],
        'resident_context_before': [m.get('context_length') for m in before['resident']['models'] if m['name'] == plan['model']],
        'resident_context_after': [m.get('context_length') for m in after['resident']['models'] if m['name'] == plan['model']],
        'health': {'samples': len(health), 'min_free_plus_inactive_gib': min(h['free_bytes'] + h['inactive_bytes'] for h in health) / 1024**3,
                   'max_thermal_state': max(h['thermal_state'] for h in health),
                   'max_memory_pressure': max(h['memory_pressure_raw'] for h in health),
                   'swapout_pages_delta': health[-1]['swapouts_pages'] - health[0]['swapouts_pages']}}
    assert not changes, 'Inference code changed after freeze'
    output = {'summary': summary, 'details': details}
    (root / 'analysis.json').write_text(json.dumps(output, indent=2))
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('directory', nargs='?', default=str(Path(__file__).parent / 'validation'))
    parser.add_argument('--allow-incomplete', action='store_true', help='Audit available records without claiming the experiment finished')
    args = parser.parse_args()
    print(json.dumps(audit(Path(args.directory).resolve(), args.allow_incomplete), indent=2))
