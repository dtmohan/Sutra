"""Offline verification of the live protocol ledger and labelled fault injection."""
import argparse
import hashlib
import json
from pathlib import Path
from checked_protocol import attach_contract, worker_schema
from context_manager import canonical, sha
from governance import GovernedContext


def audit(root):
    plan=json.loads((root/'plan.json').read_text());rows=json.loads((root/'results.json').read_text())
    assert [r['case'] for r in rows]==plan['cases'], 'Incomplete or reordered protocol suite'
    frozen=json.loads((root/'source-hashes.json').read_text())
    assert all(hashlib.sha256((root.parent/name).read_bytes()).hexdigest()==digest for name,digest in frozen.items())
    calls={p.stem:json.loads(p.read_text()) for p in (root/'calls').glob('*.json')}
    injections=0
    for call in calls.values():
        assert call['ok']
        messages=call['request']['messages'];assert len(messages)==2
        assert [m['role'] for m in messages]==['system','user']
        payload=json.loads(messages[1]['content'])
        assert not {'history','expected','conversation'} & set(payload)
        assert call['conservative_input_charge']+1024<=8192
        assert call['response']['prompt_eval_count']+1024<=8192
        original=json.loads(call['response']['message']['content'])
        if call.get('fault_injection'):
            injections+=1
            assert call['model_answer_before_injection']==original
            expected=dict(original,interpretation='DENY');assert call['answer']==expected
        else:assert call['answer']==original
    receipts=0;checkpoints=0;quarantined=0;states={}
    for row in rows:
        assert row['passed']==all(row['checks'].values())
        store=GovernedContext(root/'cases'/row['case']/'state.sqlite')
        assert store.verify_protocol('active')
        for result in row['results']:
            run=store.run(result['protocol']['run_id'])
            assert sum(e['kind']=='DISPATCH' for e in run['events'])==len(result['call_ids'])
            states[result['protocol']['state']]=states.get(result['protocol']['state'],0)+1
            for call_id,turn in zip(result['call_ids'],result['turns']):
                record=calls[call_id];payload=json.loads(record['request']['messages'][1]['content'])
                delivery=store.db.execute('SELECT payload_json,payload_hash FROM deliveries WHERE id=?',(turn['delivery_id'],)).fetchone()
                assert delivery==(canonical(payload),sha(canonical(payload)))
                assert store.db.execute('SELECT 1 FROM delivery_success WHERE id=?',(turn['delivery_id'],)).fetchone()
                if turn['stage']!='worker':continue
                attempt=next(a for a in result['attempts'] if a['last_delivery_id']==turn['delivery_id'])
                assert record['request']['format']==worker_schema(payload)
                assert attempt['model_proposal']==record['answer']
                assert attempt['answer']==attach_contract(record['answer'],payload['contract'])
                for receipt_id in attempt['receipt_ids']:
                    raw=store.db.execute('SELECT record_json FROM assertion_receipts WHERE id=?',(receipt_id,)).fetchone()[0]
                    receipt=json.loads(raw);receipts+=1
                    assert receipt['response_hash']==sha(canonical(record['answer']))
                    assert receipt['payload_hash']==delivery[1]
                    assert receipt['status']==('accepted' if attempt['admission']['admitted'] else 'rejected')
                    assert receipt['contract_hash']==run['config']['contract_hash']
                    expected_refs=[{'id':p['id'],'version':p['version'],'hash':p['hash'],'source_span':p.get('source_span')}
                                   for p in payload['pages'] if p['authority']!='model']
                    assert receipt['source_pages']==expected_refs
                if 'repair' in payload:
                    assert set(payload['repair'])=={'attempt','errors'}
                    assert len(payload['repair']['errors'])<=3
                if not attempt['admission']['admitted']:
                    assert store.checkpoint_status('active',attempt['checkpoint_id']).startswith('quarantined')
            assert (result['released_answer'] is not None)==result['admission']['admitted']
        for cid,raw,digest in store.db.execute('SELECT id,record_json,digest FROM checkpoints'):
            checkpoints+=1;assert sha('checkpoint-v1:'+raw)==digest
            if store.checkpoint_status('active',cid).startswith('quarantined'):quarantined+=1
        store.close()
    health=[json.loads(line) for line in (root/'health.jsonl').read_text().splitlines()]
    summary={'cases':len(rows),'passed':sum(r['passed'] for r in rows),'model_calls':len(calls),
             'fault_injected_responses':injections,'verified_assertion_receipts':receipts,
             'checkpoints':checkpoints,'quarantined_or_invalidated_checkpoints':quarantined,
             'states':states,'fresh_calls':True,'history_replay':False,'frozen_sources_unchanged':True,
             'all_budgets_fit':True,'max_charge_plus_output':max(c['conservative_input_charge']+1024 for c in calls.values()),
             'max_prompt_tokens':max(c['response']['prompt_eval_count'] for c in calls.values()),
             'elapsed_model_seconds':sum(c['elapsed_s'] for c in calls.values()),
             'max_thermal_state':max(h['thermal_state'] for h in health)}
    (root/'analysis.json').write_text(json.dumps(summary,indent=2)+'\n')
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('directory');a=p.parse_args()
    print(json.dumps(audit(Path(a.directory).resolve()),indent=2))
