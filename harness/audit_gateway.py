"""Offline one-to-one audit of every model call and invocation receipt."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from context_manager import canonical,sha,decode_value
from invocation_gateway import InvocationLedger,request_hash
from invocation_roles import validate_shape,validate_permission_ops
from legacy_model import resolve_operations
from kernel_adapter import budget_decision


def audit(root):
    hashes=json.loads((root/'source-hashes.json').read_text())
    assert all(hashlib.sha256((root.parent/name).read_bytes()).hexdigest()==digest for name,digest in hashes.items())
    calls={p.stem:json.loads(p.read_text()) for p in (root/'calls').glob('*.json')}
    seen=set();roles=Counter();statuses=Counter();injections=0;receipts=0
    for path in (root/'cases').rglob('state.sqlite'):
        s=InvocationLedger(path)
        for session, in s.db.execute('SELECT id FROM sessions'):
            assert s.verify_invocations(session),str(path)
        for iid,session,cid,raw,digest in s.db.execute('SELECT id,session,call_id,record_json,digest FROM invocations'):
            assert cid not in seen and cid in calls;seen.add(cid)
            env=json.loads(raw);c=calls[cid];assert c['ok']
            run=s.run(env['run_id']); dispatches=[e for e in run['events'] if e['kind']=='DISPATCH']
            position=next(i for i,e in enumerate(dispatches) if e['data'].get('invocation_id')==iid)
            measured=budget_decision(position,run['config']['max_calls'])
            assert measured['decision']=='ALLOW'
            assert env['budget_kernel']==measured==dispatches[position]['data']['budget_kernel']
            messages=c['request']['messages'];assert len(messages)==2
            assert [m['role'] for m in messages]==['system','user']
            system=messages[0]['content'];payload=json.loads(messages[1]['content']);schema=c['request']['format']
            assert not {'history','conversation','expected'} & set(payload)
            assert env['request_hash']==request_hash(cid,c['request']['model'],system,payload,schema)
            assert env['schema']==schema and env['system_hash']==sha(system)
            assert env['charge']==c['conservative_input_charge']
            assert env['charge']+1024<=8192 and c['response']['prompt_eval_count']+1024<=8192
            delivery=s.db.execute('SELECT payload_json,payload_hash FROM deliveries WHERE id=?',(env['delivery_id'],)).fetchone()
            assert delivery==(canonical(payload),sha(canonical(payload)))
            receipt_raw=s.db.execute('SELECT record_json FROM invocation_receipts WHERE invocation_id=?',(iid,)).fetchone()[0]
            r=json.loads(receipt_raw);receipts+=1;roles[env['role']]+=1;statuses[env['role']+'/'+r['status']]+=1
            assert r['proposal']==c['answer'] and r['response_hash']==sha(canonical(c['answer']))
            assert r['contract_hash']==env['contract_hash'] and r['logic_hash']==env['logic_hash']
            original=json.loads(c['response']['message']['content'])
            if c.get('fault_injection'):
                injections+=1;assert c['model_answer_before_injection']==original
                label=c['fault_injection']
                if 'interpretation with DENY' in label:assert c['answer']==dict(original,interpretation='DENY')
                elif 'permission opposite' in label:assert c['answer']=={'operations':[{'key':'network_allowed','value':'true','source_unit':0}]}
                elif 'unadvertised page' in label:assert c['answer']=={'read_pages':['../../outside-task'],'browse':[]}
                else:raise AssertionError('Unknown fault injection')
            else:assert c['answer']==original
            if r['status']=='accepted':
                validate_shape(c['answer'],schema)
                if env['role']=='parser':
                    ops=resolve_operations(payload['CURRENT_USER_MESSAGE'],c['answer']['operations']);validate_permission_ops(ops)
                    event=s.db.execute('SELECT payload,revision,authority,raw_content FROM events WHERE session=? AND source_id=?',(session,'invocation-'+iid)).fetchone()
                    expected=[dict(op,value=decode_value(op['key'],op['value'])) for op in ops]
                    assert event==(canonical(expected),env['revision']+1,'user',payload['CURRENT_USER_MESSAGE'])
                if env['role']=='selector':
                    for b in c['answer']['browse']:
                        assert b['offset'] in {0}|{d['next_offset'] for d in payload['directory'] if d['group']==b['group'] and d['next_offset'] is not None}
            else:
                try:s.load_result(session,iid)
                except Exception:pass
                else:raise AssertionError('Rejected result readable')
                if env['role']=='parser':assert not s.db.execute('SELECT 1 FROM events WHERE source_id=?',('invocation-'+iid,)).fetchone()
        s.close()
    assert seen==set(calls),'Every provider log must have exactly one durable invocation and receipt'
    summary={'model_calls':len(calls),'invocation_receipts':receipts,'roles':dict(roles),'statuses':dict(statuses),
             'labelled_fault_injections':injections,'unreceipted_calls':0,'fresh_calls':True,'history_replay':False,
             'frozen_source_changes':[],'all_budgets_fit':True,'canonical_budget_decisions_verified':receipts}
    (root/'gateway-analysis.json').write_text(json.dumps(summary,indent=2)+'\n')
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('directory');a=p.parse_args()
    print(json.dumps(audit(Path(a.directory).resolve()),indent=2))
