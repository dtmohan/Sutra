"""Opt-in three fresh local calls with reload, model switches and durable memory.

No user workspace edits, model unloading or configuration changes. Eviction and
fault injection are covered separately by test_memory_layer.py.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'harness'))
from invocation_gateway import InvocationLedger
from review_budget import usage
from memory_layer import snapshot


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--worker',default='qwen3.6:35b')
    args=parser.parse_args()
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    ws=out/'workspace';ws.mkdir();state=out/'state';state.mkdir()
    env=dict(os.environ,SUTRA_THINKING='adaptive',SUTRA_MEMORY_OWNERSHIP='shared')
    models={'low':'qwen3:4b','mid':args.worker,'high':args.worker}
    monitor=subprocess.Popen([str(ROOT/'bin/telemetry')],stdout=subprocess.PIPE,text=True)
    def collect():
        for line in monitor.stdout:
            try:
                json.loads(line);tmp=state/'telemetry.tmp';tmp.write_text(line);tmp.replace(state/'telemetry.json')
            except (ValueError,OSError):pass
    threading.Thread(target=collect,daemon=True).start()
    report={'passed':False,'models':models,'calls':[],'checks':{}}
    def save(): (out/'results.json').write_text(json.dumps(report,indent=2)+'\n')
    parent=None;sid=None;prior_calls=0;contract=None;limits=None
    prompts=[
        ('low','Remember: the project label is Cedar-724. Never change the STP kernel. No network, edits or tests. Reply only: noted.'),
        ('mid','Recall the project label and what must stay unchanged. Do not use tools. Answer in one sentence.'),
        ('low','What is the project label, and what operations did I prohibit? Answer in one sentence.')]
    try:
        for _ in range(40):
            if (state/'telemetry.json').exists():break
            time.sleep(.1)
        for index,(tier,prompt) in enumerate(prompts):
            started=time.monotonic()
            request=dict(op='conversation',prompt=prompt,parent=parent,models=models,tier=tier)
            p=subprocess.run([sys.executable,'-B',str(ROOT/'harness/vscode_bridge.py'),
                '--workspace',str(ws),'--storage',str(state)],input=json.dumps(request),
                capture_output=True,text=True,env=env,timeout=480)
            data=json.loads(p.stdout) if p.stdout.strip() else {'ok':False,'error':p.stderr}
            report['calls'].append({'tier':tier,'seconds':round(time.monotonic()-started,3),'response':data});save()
            print(json.dumps({'turn':index+1,'tier':tier,'seconds':report['calls'][-1]['seconds'],'ok':data.get('ok')}),flush=True)
            assert p.returncode==0 and data.get('ok'),data
            value=data['data'];result=value['result']
            assert result['admission']['admitted'],result['admission']
            assert sid is None or sid==value['session']
            sid=value['session'];parent=result['invocation_id']
            s=InvocationLedger(state/'state.sqlite')
            try:
                current=s.origin(sid);spent=usage(s,sid)
                if contract is None:contract=current;limits=spent['limits']
                assert current==contract and spent['limits']==limits
                calls=s.db.execute('SELECT count(*) FROM invocations WHERE session=?',(sid,)).fetchone()[0]
                assert spent['used']['calls']==calls and calls>prior_calls
                prior_calls=calls
                raw=s.db.execute('SELECT d.payload_json FROM deliveries d JOIN invocations i ON i.delivery_id=d.id WHERE i.id=?',(parent,)).fetchone()[0]
                packet=json.loads(raw)
                if index:
                    pins=json.dumps(packet['memory']['pinned_user_statements'])
                    assert all(x in pins for x in ('Cedar-724','STP kernel','No network, edits or tests'))
                    assert 'Cedar-724' in result['accepted_output']['answer']
                assert s.verify_invocations(sid)
                if s.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='host_actions'").fetchone():
                    assert s.db.execute('SELECT count(*) FROM host_actions').fetchone()[0]==0
                report['budgets']=spent;report['memory']=snapshot(s.db)
                report['checks'][f'turn_{index+1}_contract_budget_and_receipts']=True
            finally:s.close()
        report['checks']['recall_after_two_model_switches']=True
        report['passed']=all(report['checks'].values())
    except BaseException as exc:
        report['error']=str(exc);raise
    finally:
        save();monitor.terminate();monitor.wait(timeout=5)


if __name__=='__main__':main()
