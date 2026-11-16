"""Opt-in: real model switches over receipted static diagnostics; no tool replay."""
import json, os, subprocess, sys, threading, time
from contextlib import closing
from pathlib import Path
repo=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(repo/'harness'))
from invocation_gateway import InvocationLedger
from review_budget import usage
import capabilities
base=Path(sys.argv[1]).resolve();ws=base/'repo';root=base/'state'
models={'low':'qwen3:4b','mid':'qwen3.6:35b','high':'qwen3.6:35b'}
env=dict(os.environ,SUTRA_THINKING='adaptive',SUTRA_MEMORY_OWNERSHIP='shared')
monitor=subprocess.Popen([str(repo/'bin/telemetry')],stdout=subprocess.PIPE,text=True)
def samples():
 for line in monitor.stdout:
  try:json.loads(line);(root/'telemetry.tmp').write_text(line);(root/'telemetry.tmp').replace(root/'telemetry.json')
  except (ValueError,OSError):pass
threading.Thread(target=samples,daemon=True).start()
rows=[];parent=None
meta=json.loads((root/'workspace.json').read_text());sid=meta['active']
with closing(InvocationLedger(root/'state.sqlite')) as s:
 snapshot=s.snapshot(sid);spent=usage(s,sid)['used'];receipt=capabilities.context(s,sid)['receipt']
try:
 for _ in range(40):
  if (root/'telemetry.json').exists():break
  time.sleep(.1)
 prompts=[('low','Remember the project nickname Cedar CED724. Use Pyright to check code.py. Keep source unchanged; no edits, tests or project dependency changes.'),
 ('mid','Do not run anything again. Explain the saved Pyright diagnostics in two short sentences, recall my project nickname and constraints from the previous turn.'),
 ('low','What is my project nickname, what source issue did the tool report, and what must remain unchanged? Answer briefly without running tools.')]
 for tier,prompt in prompts:
  req={'op':'conversation','models':models,'tier':tier,'prompt':prompt,'parent':parent}
  start=time.monotonic()
  run=subprocess.run([sys.executable,'-B',str(repo/'harness/vscode_bridge.py'),'--workspace',str(ws),'--storage',str(root)],input=json.dumps(req),capture_output=True,text=True,env=env,timeout=480)
  result=json.loads(run.stdout) if run.stdout.strip() else {'ok':False,'error':run.stderr}
  rows.append({'tier':tier,'seconds':time.monotonic()-start,'response':result})
  (base/'model-switch.json').write_text(json.dumps({'rows':rows},indent=2)+'\n')
  assert run.returncode==0 and result.get('ok'),result
  data=result['data'];r=data['result'];assert r['admission']['admitted'],r
  parent=r['invocation_id'];answer=r['accepted_output']
  if tier=='mid':assert 'CED724' in json.dumps(answer),answer
  if len(rows)==1:assert answer['workspace_request']['capability']=='pyright',answer
  with closing(InvocationLedger(root/'state.sqlite')) as s:
   assert snapshot==s.snapshot(sid)
   assert spent==usage(s,sid)['used'] # model proposals never dispatch a tool
   assert receipt==capabilities.context(s,sid)['receipt']
   assert s.audit_valid()
  print(json.dumps({'tier':tier,'seconds':round(time.monotonic()-start,2),'answer':answer}),flush=True)
 with closing(InvocationLedger(root/'state.sqlite')) as s:
  all_usage={i:usage(s,i) for i, in s.db.execute('SELECT id FROM sessions')}
  assert all(s.verify_invocations(i,complete=False) for i in all_usage)
 (base/'model-switch.json').write_text(json.dumps({'passed':True,'rows':rows,'workspace_budget_unchanged':spent,'all_usage':all_usage,'constraints_unchanged':True,'tool_receipt_unchanged':receipt},indent=2)+'\n')
finally:
 monitor.terminate();monitor.wait(timeout=5)
