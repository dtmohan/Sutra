"""Opt-in local model commitment/assessment replay in an isolated workspace."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
PROMPT='Explain how app.py handles negative and positive inputs and give one worked example. Keep source unchanged. No network, edits or tests.'


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--worker',default='qwen3.6:35b');args=parser.parse_args()
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    ws=out/'workspace';ws.mkdir();state=out/'state';state.mkdir()
    source='def normalize(value):\n    return max(0, value)\n\ndef calculate(value):\n    return normalize(value) * 2\n'
    (ws/'app.py').write_text(source)
    env=dict(os.environ,SUTRA_THINKING='adaptive',SUTRA_MEMORY_OWNERSHIP='shared',
             KARMA_TYPESCRIPT=str(ROOT/'vendor/typescript/typescript.js'))
    models={'low':'qwen3:4b','mid':args.worker,'high':args.worker}
    monitor=subprocess.Popen([str(ROOT/'bin/telemetry')],stdout=subprocess.PIPE,text=True)
    def collect():
        for line in monitor.stdout:
            try:
                json.loads(line);tmp=state/'telemetry.tmp';tmp.write_text(line);tmp.replace(state/'telemetry.json')
            except (ValueError,OSError):pass
    threading.Thread(target=collect,daemon=True).start()
    report={'passed':False,'rows':[],'checks':{},'models':models,'thinking':'adaptive (workspace model default)'}
    def save(): (out/'results.json').write_text(json.dumps(report,indent=2)+'\n')
    def send(label,**request):
        start=time.monotonic()
        p=subprocess.run([sys.executable,'-B',str(ROOT/'harness/vscode_bridge.py'),'--workspace',str(ws),'--storage',str(state)],
                         input=json.dumps(request),capture_output=True,text=True,env=env,timeout=480)
        data=json.loads(p.stdout) if p.stdout.strip() else {'ok':False,'error':p.stderr}
        report['rows'].append({'label':label,'seconds':round(time.monotonic()-start,3),'response':data});save()
        print(json.dumps({'label':label,'seconds':report['rows'][-1]['seconds'],'ok':data.get('ok'),'state':data.get('data',{}).get('state')}),flush=True)
        assert p.returncode==0 and data.get('ok'),data
        return data['data']
    try:
        for _ in range(40):
            if (state/'telemetry.json').exists():break
            time.sleep(.1)
        send('create-task',op='auto_new',goal='Explain the helper source behavior without changing files.',roots=['.'])
        flow=send('commit',op='orchestrate',event='start',prompt=PROMPT,models=models,tier='auto',armed=False)
        commit=flow['task_observations']['commitment']
        report['checks']['recorded_before_workspace']=flow['state']=='NEED_ARM' and commit and commit['status']=='recorded'
        assert report['checks']['recorded_before_workspace'],flow
        for i in range(16):
            if flow['state'] in ('DONE','PAUSED'):break
            flow=send('work-'+str(i),op='orchestrate',event='step',flow_id=flow['flow_id'],cursor=flow['cursor'],
                      binding=flow.get('binding'),models=models,tier='mid',armed=True)
        a=flow['task_observations']['assessment'];data=flow.get('data',{})
        report['checks'].update(commitment_retained=flow['task_observations']['commitment']==commit,
            assessment_recorded=bool(a and a['status']=='recorded' and a['commitment_id']==commit['invocation_id']),
            worker_switched=bool(a and a['model']==args.worker and commit['model']!=a['model']),
            response_delivered=bool(data.get('result',{}).get('admission',{}).get('admitted')),
            source_unchanged=(ws/'app.py').read_text()==source)
        sys.path.insert(0,str(ROOT/'harness'))
        from invocation_gateway import InvocationLedger
        from review_budget import usage
        ledger=InvocationLedger(state/'state.sqlite')
        try:
            sid=flow['binding']['session'];c=ledger.pinned(sid)
            report['checks']['permissions_retained']=not any(c['constraints'][k] for k in ('edits_allowed','tests_allowed','network_allowed','dependencies_allowed'))
            report['checks']['no_actions']=ledger.db.execute('SELECT count(*) FROM host_actions').fetchone()[0]==0
            report['budgets']={sid:usage(ledger,sid)}
            report['checks']['receipts_verified']=all(ledger.verify_invocations(s,complete=False) for s, in ledger.db.execute('SELECT DISTINCT session FROM invocations').fetchall())
            report['checks']['one_worker_call']=report['budgets'][sid]['used']['calls']==1
        finally:ledger.close()
        report['passed']=all(report['checks'].values())
    except BaseException as exc:
        report['error']=str(exc);raise
    finally:
        save();monitor.terminate();monitor.wait(timeout=5)
    if not report['passed']:raise SystemExit('Some live observations failed; raw responses retained.')


if __name__=='__main__':main()
