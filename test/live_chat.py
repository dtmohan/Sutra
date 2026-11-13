"""Explicit live local-model smoke test in an isolated workspace."""
import argparse,json,subprocess,sys,threading,time
from pathlib import Path
source=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(source/'harness'))
from vscode_bridge import handle
from invocation_gateway import InvocationLedger

def main(output):
    output.mkdir(parents=True,exist_ok=True);workspace=output/'repo';workspace.mkdir(exist_ok=True);storage=output/'state';storage.mkdir(exist_ok=True)
    (workspace/'entry.py').write_text('from leaf import run\ndef start():\n    return run()\n')
    (workspace/'leaf.py').write_text('def run():\n    return 1\n')
    telemetry=subprocess.Popen([str(source/'bin/telemetry')],stdout=subprocess.PIPE,text=True)
    def collect():
        for line in telemetry.stdout:
            try:
                sample=json.loads(line);temp=storage/'telemetry.tmp';temp.write_text(json.dumps(sample));temp.replace(storage/'telemetry.json')
            except (OSError,ValueError):pass
    threading.Thread(target=collect,daemon=True).start()
    report={'turns':[],'passed':False,'live_local_inference':True,'semantic_accuracy_verified':False}
    try:
        for _ in range(30):
            if (storage/'telemetry.json').exists():break
            time.sleep(.1)
        initial=handle(storage,workspace,{'op':'auto_new','goal':'Explain entry and leaf architecture. Preserve read-only scope and no network access.','roots':['.'],'review_calls':6})
        mapped=handle(storage,workspace,{'op':'auto_step','explain':False})
        assert mapped['auto']['mapped_files']==2
        models={'low':'qwen3:4b','mid':'qwen3.6:35b','high':'local-assistant:latest'}
        for prompt in ('Explain start and cite the source.','Review the architecture and explain what remains to be verified.'):
            started=time.monotonic();data=handle(storage,workspace,{'op':'chat_answer','prompt':prompt,'models':models,'tier':'auto'})
            report['turns'].append({'route':data['route'],'admission':data['result']['admission'],'invocation_id':data['result']['invocation_id'],'answer':data['result']['accepted_output'],'elapsed_seconds':round(time.monotonic()-started,2)})
            (output/'result.json').write_text(json.dumps(report,indent=2)+'\n')
            print(json.dumps({'model':data['route']['model'],'admitted':data['result']['admission']['admitted'],'elapsed':report['turns'][-1]['elapsed_seconds']}),flush=True)
            assert data['result']['admission']['admitted'],data['result']['admission']
            assert data['state']['contract']==initial['contract']
        s=InvocationLedger(storage/'state.sqlite')
        try:
            rows=[json.loads(r[0]) for r in s.db.execute("SELECT payload_json FROM deliveries ORDER BY rowid")]
            accepted=[p for p in rows if 'excerpts' in p]
            assert accepted[-1]['handoff']['invocation_id']==report['turns'][0]['invocation_id']
            assert not any({'messages','history','conversation'}&set(p) for p in accepted)
            report['ledger_valid']=s.verify_invocations(initial['session']);assert report['ledger_valid']
            report['contract_preserved']=True;report['compact_handoff_preserved']=True;report['passed']=True
        finally:s.close()
    except Exception as exc:report['error']=str(exc);raise
    finally:
        telemetry.terminate();telemetry.wait(timeout=5);(output/'result.json').write_text(json.dumps(report,indent=2)+'\n')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();main(args.output)
