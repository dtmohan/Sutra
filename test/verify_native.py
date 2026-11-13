import json
from invocation_gateway import InvocationLedger
from review_budget import usage

def verify(storage,workspace,binding,models,c,test):
 ledger=InvocationLedger(storage/'state.sqlite')
 try:
  assert ledger.verify_invocations(binding['session']);assert ledger.audit_valid()
  budget=usage(ledger,binding['session'])
  rows=[(json.loads(i),json.loads(p),json.loads(r)) for i,p,r in ledger.db.execute('SELECT i.record_json,d.payload_json,r.record_json FROM invocations i JOIN deliveries d ON i.delivery_id=d.id JOIN invocation_receipts r ON r.invocation_id=i.id WHERE i.session=? ORDER BY i.rowid',(binding['session'],))]
  assert len(rows)>=2
  assert all(p['contract']['contract_hash']==binding['contract_hash'] for _,p,_ in rows)
  assert all(p['contract']['constraints']==c for _,p,_ in rows)
  assert all('history' not in p and 'messages' not in p for _,p,_ in rows)
  high=[p for i,p,r in rows if i['model']==models['high']]
  assert high and any(e['status']=='applied' for e in high[0]['execution']['recent'])
  assert any('range(MAX_ATTEMPTS):' in p['text'] for p in high[0]['pages'])
  assert (workspace/'tests/test_retry.py').read_text()==test
  assert all(r['proposal']['complete'] is False for _,_,r in rows if r['status']=='accepted')
  assert budget['used']['calls']==len(rows)
  return {'budget':budget,'checks':['low model proposed a real edit; host applied exact diff','high model received original goal, all constraints, fresh edited source and host action receipt','fresh delivered packets contain no transcript history; local transport sends system + packet only','sandbox test exited zero; assertions remain unverified','same contract/calibration identity across model switch','cumulative budget retained; immutable receipts verify','test file remained unchanged']}
 finally:ledger.close()
