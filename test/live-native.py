"""Real local models, fresh calls, real host edits and sandboxed process receipts."""
import hashlib,json,sys,tempfile,threading,subprocess,time,traceback
from pathlib import Path
source=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(source/'harness'))
from test_host_actions import SOURCE,TEST
from vscode_bridge import handle
from review_budget import DEFAULTS,usage
from invocation_gateway import InvocationLedger
from review_role import constraint_rows
root=Path(tempfile.mkdtemp(prefix='karma-native-live-'));workspace=root/'project';storage=root/'store'
for directory in ('src','tests'):(workspace/directory).mkdir(parents=True)
(workspace/'src/retry.py').write_text(SOURCE);(workspace/'tests/test_retry.py').write_text(TEST);storage.mkdir()
report={'root':str(root),'calls':[],'checks':[]};out=source/'live-native.json'
telemetry=subprocess.Popen([str(source/'bin/telemetry')],stdout=subprocess.PIPE,text=True)
def collect():
 for line in telemetry.stdout:
  try:
   sample=json.loads(line);tmp=storage/'telemetry.tmp';tmp.write_text(json.dumps(sample));tmp.replace(storage/'telemetry.json')
  except Exception:pass
threading.Thread(target=collect,daemon=True).start()
try:
 while not (storage/'telemetry.json').exists():time.sleep(.1)
 c={**DEFAULTS,'native_task':True,'allowed_paths':['src/retry.py','tests/test_retry.py'],'network_allowed':False,'dependencies_allowed':False,'edits_allowed':True,'tests_allowed':True,'test_paths':['tests/test_retry.py'],'test_timeout_ms':10000,'max_actions':4,'review_calls':12}
 st=handle(storage,workspace,{'op':'new','goal':'Fix the retry off-by-one error and run its existing tests. Keep MAX_ATTEMPTS=3, never add dependencies or use network, and do not change test files.','constraints':c})
 binding={'session':st['session'],'contract_hash':st['contract']['contract_hash'],'calibration_hash':st['calibration']['binding']['hash']}
 models={'low':'qwen3:4b','mid':'qwen3.6:35b','high':'local-assistant:latest'}
 for tier,prompt,kind in [('low','Fix the off-by-one loop in src/retry.py using a minimal exact edit. The original expression is range(MAX_ATTEMPTS - 1). Leave tests unchanged.','edit'),('high','Continue the pinned retry task. The host already applied the edit. Run the permitted tests/test_retry.py without changing it.','run_tests')]:
  start=time.time();result=handle(storage,workspace,{'op':'native_work',**binding,'models':models,'tier':tier,'prompt':prompt})
  (root/(tier+'.json')).write_text(json.dumps(result,indent=2));r=result['result'];report['calls'].append({'tier':tier,'model':result['route']['model'],'elapsed_s':time.time()-start,'admission':r['admission'],'budget':result['native_budget']});out.write_text(json.dumps(report,indent=2))
  assert r['admission']['admitted'],r['admission'];assert r['accepted_output']['action']['kind']==kind,r['accepted_output']
  preview=handle(storage,workspace,{'op':'native_preview',**binding,'invocation_id':r['invocation_id']})
  executed=handle(storage,workspace,{'op':'native_execute',**binding,'invocation_id':r['invocation_id']});receipt=executed['action_receipt'];report['calls'][-1]['receipt']=receipt
  assert executed['state']['contract']['contract_hash']==binding['contract_hash']
  assert executed['state']['contract']['contract']['constraints']==st['contract']['contract']['constraints']
  assert receipt['result']['status']==('applied' if kind=='edit' else 'process_exited_zero'),receipt
  print(json.dumps({'tier':tier,'status':receipt['result']['status'],'seconds':round(time.time()-start,2)}),flush=True)
 from verify_native import verify
 report.update(verify(storage,workspace,binding,models,c,TEST))
 report['passed']=True
except Exception as e:report['passed']=False;report['error']=traceback.format_exc();print(report['error']);raise
finally:
 telemetry.terminate();telemetry.wait();out.write_text(json.dumps(report,indent=2)+'\n')
