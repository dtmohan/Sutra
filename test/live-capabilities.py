"""Real approved package downloads and sandbox diagnostics in an isolated fixture."""
import json,os,subprocess,sys,threading,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'harness'))
from invocation_gateway import InvocationLedger
from vscode_bridge import handle
from review_budget import usage
import capabilities
repo=Path(__file__).resolve().parents[1]
base=Path(sys.argv[1]).resolve();base.mkdir(parents=True,exist_ok=True)
ws=base/'repo';ws.mkdir(exist_ok=True);(ws/'code.py').write_text('import os\nanswer: int = "wrong"\n')
root=base/'state';root.mkdir(exist_ok=True)
telemetry=subprocess.Popen([str(repo/'bin/telemetry')],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
def samples():
 for line in telemetry.stdout:
  try:json.loads(line);(root/'telemetry.tmp').write_text(line);(root/'telemetry.tmp').replace(root/'telemetry.json')
  except (ValueError,OSError):pass
thread=threading.Thread(target=samples,daemon=True);thread.start()
try:
 for _ in range(80):
  if (root/'telemetry.json').exists():break
  time.sleep(.1)
 state=handle(root,ws,{'op':'auto_new','goal':'Validate tool installation and diagnostics; no project edits, test execution or dependency changes.','roots':['.']})
 s=InvocationLedger(root/'state.sqlite');sid=state['session'];snapshot=s.snapshot(sid)
 results=[]
 for name in ['ruff','pyright']:
  for action in ['install','check']:
   p=capabilities.preview(s,sid,ws,root,name,action,[])
   r=capabilities.execute(s,sid,ws,root,p['id'],{'kind':'user','proposal_id':p['id']})
   results.append(r);print(json.dumps({'tool':name,'action':action,'result':{k:v for k,v in r['result'].items() if k!='files'}}),flush=True)
   assert r['result']['status'] in ('installed','finished'),r
   if action=='check':
    assert r['result']['returncode'] in (0,1),r
    assert 'code.py' in r['result']['output'],r
    diagnostics=json.loads(r['result']['output'])
    if name=='ruff':assert any(d.get('code')=='F401' for d in diagnostics),r
    else:
     assert any(d.get('rule')=='reportAssignmentType' for d in diagnostics['generalDiagnostics']),r
     assert not any('not defined' in d['message'] for d in diagnostics['generalDiagnostics']),r
  assert snapshot==s.snapshot(sid)
 summary={'results':results,'budget':usage(s,sid),'constraints_unchanged':s.snapshot(sid)==snapshot,'audit_valid':s.audit_valid()}
 (base/'results.json').write_text(json.dumps(summary,indent=2)+'\n')
 print(json.dumps({k:v for k,v in summary.items() if k!='results'}),flush=True)
 s.close()
finally:
 telemetry.terminate();telemetry.wait(timeout=5)
