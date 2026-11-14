"""One bounded real local-model call with reasoning; isolated storage, no source actions."""
import argparse,contextlib,hashlib,json,os,subprocess,sys,threading,time
from pathlib import Path
SOURCE=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(SOURCE/'harness'))
from vscode_bridge import handle
from lifecycle import deadline


def main(out,model):
 out.mkdir(parents=True,exist_ok=True);root=out/'state';root.mkdir();ws=out/'repo';ws.mkdir()
 report={'passed':False,'model':model,'events':[]};started=time.monotonic()
 telemetry=subprocess.Popen([str(SOURCE/'bin/telemetry')],stdout=subprocess.PIPE,text=True)
 def collect():
  for line in telemetry.stdout:
   try:
    sample=json.loads(line);temp=root/'telemetry.tmp';temp.write_text(json.dumps(sample));temp.replace(root/'telemetry.json')
   except (OSError,ValueError):pass
 threading.Thread(target=collect,daemon=True).start()
 class Capture:
  def write(self,line):
   if line.startswith('SUTRA_EVENT '):
    e=json.loads(line[12:]);e['observed_seconds']=round(time.monotonic()-started,3);report['events'].append(e)
  def flush(self):pass
 try:
  for _ in range(30):
   if (root/'telemetry.json').exists():break
   time.sleep(.1)
  os.environ.update(SUTRA_ACTIVITY='1',SUTRA_THINKING='on')
  with contextlib.redirect_stderr(Capture()),deadline():
   data=handle(root,ws,{'op':'orchestrate','event':'start','prompt':'Hello. Please greet me in one short sentence.','models':dict(low=model,mid=model,high=model),'tier':'high'})
  result=data['conversation']['result'];report['admission']=result['admission'];report['usage']=result['token_usage'];report['state']=data['state']
  assert result['admission']['admitted'],result['admission']
  thoughts=[e for e in report['events'] if e['kind']=='thinking'];assert thoughts
  finish=report['events'][-1];assert finish['kind']=='finish' and finish['status']=='accepted'
  assert thoughts[0]['observed_seconds']<finish['observed_seconds']
  report['answer']=result['accepted_output']['answer'];report['streamed_before_admission']=True
  report['kernel_sha256']=hashlib.sha256((SOURCE/'harness/stp_canonical/kernel.py').read_bytes()).hexdigest();report['passed']=True
 except BaseException as e:report['error']=str(e);raise
 finally:
  telemetry.terminate();telemetry.wait(timeout=5);(out/'result.json').write_text(json.dumps(report,indent=2)+'\n')
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--model',default='local-assistant:latest');a=p.parse_args();main(a.output,a.model)
