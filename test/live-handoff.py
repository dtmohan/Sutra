"""Read-only copy of a saved session; classify a real prompt without source execution."""
import argparse,json,os,sqlite3,subprocess,sys,threading,time
from pathlib import Path
SOURCE=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(SOURCE/'harness'))
from invocation_gateway import InvocationLedger
from vscode_bridge import handle
from lifecycle import deadline
from context_manager import InvalidUpdate
from review_budget import usage


def main(source_state,scratch,out,parent,model):
 scratch.mkdir(parents=True,exist_ok=False);out.mkdir(parents=True,exist_ok=True)
 original=sqlite3.connect((source_state/'state.sqlite').resolve().as_uri()+'?mode=ro',uri=True)
 copied=sqlite3.connect(scratch/'state.sqlite');original.backup(copied);copied.close();original.close()
 meta=json.loads((source_state/'workspace.json').read_text());(scratch/'workspace.json').write_text(json.dumps(meta))
 workspace=Path(meta['workspace']);sid=meta['active']
 report={'passed':False,'original_state_read_only':True,'model':model,'source_state':str(source_state)}
 s=InvocationLedger(scratch/'state.sqlite');contract=s.pinned(sid);before=usage(s,sid)['used']
 source_ops=s.db.execute('SELECT count(*) FROM repository_ops').fetchone()[0]
 actions=s.db.execute('SELECT count(*) FROM host_actions').fetchone()[0]
 prior=s.db.execute('SELECT session FROM invocations WHERE id=?',(parent,)).fetchone();assert prior
 old_contract=s.snapshot(prior[0]);s.close()
 telemetry=subprocess.Popen([str(SOURCE/'bin/telemetry')],stdout=subprocess.PIPE,text=True)
 def collect():
  for line in telemetry.stdout:
   try:
    sample=json.loads(line);tmp=scratch/'telemetry.tmp';tmp.write_text(json.dumps(sample));tmp.replace(scratch/'telemetry.json')
   except (OSError,ValueError):pass
 threading.Thread(target=collect,daemon=True).start()
 try:
  for _ in range(30):
   if (scratch/'telemetry.json').exists():break
   time.sleep(.1)
  os.environ.update(SUTRA_THINKING='on',SUTRA_ACTIVITY='0')
  with deadline():
   result=handle(scratch,workspace,{'op':'orchestrate','event':'start','armed':False,'parent':parent,
       'prompt':'\ncan you do an indepth review. you can recursively access files beyond the 8 file limit\n',
       'models':dict(low=model,mid=model,high=model),'tier':'high'})
  report['state']=result['state'];report['result']=result.get('conversation')
  assert result['state']=='NEED_ARM',result
  chat=result['conversation'];assert chat['memory_used'] and chat['result']['admission']['admitted']
  assert chat['result']['accepted_output']['intent']=='workspace'
  try:handle(scratch,workspace,{'op':'orchestrate','event':'step','armed':False,'flow_id':result['flow_id'],'cursor':result['cursor']})
  except InvalidUpdate as e:assert 'user-armed' in str(e)
  else:raise AssertionError('Unarmed workflow did not stop')
  s=InvocationLedger(scratch/'state.sqlite')
  try:
   assert contract==s.pinned(sid) and before==usage(s,sid)['used']
   assert s.snapshot(prior[0])==old_contract
   assert source_ops==s.db.execute('SELECT count(*) FROM repository_ops').fetchone()[0]
   assert actions==s.db.execute('SELECT count(*) FROM host_actions').fetchone()[0]
   assert s.verify_invocations(chat['session'])
   report.update(same_conversation_session=chat['session']==prior[0],legacy_goal=old_contract['goal'],workspace_contract_preserved=True,
                 workspace_budget_preserved=True,no_source_reads=True,no_actions=True,unarmed_dispatch_refused=True,receipts_verified=True,passed=True)
  finally:s.close()
 except BaseException as e:report['error']=str(e);raise
 finally:
  telemetry.terminate();telemetry.wait(timeout=5);(out/'Live-saved-session.json').write_text(json.dumps(report,indent=2)+'\n')
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--source-state',type=Path,required=True);p.add_argument('--scratch',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--parent',required=True);p.add_argument('--model',default='local-assistant:latest');a=p.parse_args();main(a.source_state,a.scratch,a.output,a.parent,a.model)
