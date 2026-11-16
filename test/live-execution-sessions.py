"""Opt-in local-model replay: source paging, a saved pause, and model switching."""
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
import orchestration as machine
import orchestration_flow as flow_api


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--worker',default='qwen3.6:35b')
    args=parser.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    ws=out/'workspace';ws.mkdir();state=out/'state';state.mkdir()
    for i in range(4):
        (ws/f'part{i}.py').write_text(f'def part{i}(value):\n    """Cedar CED724: clamp a negative value before doubling."""\n    return max(0, value) * 2\n'+'# Source padding for the bounded page fixture.\n'*40)
    source={p.name:p.read_bytes() for p in ws.iterdir()}
    models={'low':'qwen3:4b','mid':args.worker,'high':args.worker}
    env=dict(os.environ,SUTRA_THINKING='adaptive',SUTRA_MEMORY_OWNERSHIP='shared',
             KARMA_TYPESCRIPT=str(ROOT/'vendor/typescript/typescript.js'))
    monitor=subprocess.Popen([str(ROOT/'bin/telemetry')],stdout=subprocess.PIPE,text=True)
    def collect():
        for line in monitor.stdout:
            try:
                json.loads(line);temp=state/'telemetry.tmp';temp.write_text(line);temp.replace(state/'telemetry.json')
            except (ValueError,OSError):pass
    threading.Thread(target=collect,daemon=True).start()
    result={'passed':False,'models':models,'rows':[],'checks':{}}
    def save():(out/'results.json').write_text(json.dumps(result,indent=2)+'\n')
    def send(label,**req):
        start=time.monotonic()
        proc=subprocess.run([sys.executable,'-B',str(ROOT/'harness/vscode_bridge.py'),
              '--workspace',str(ws),'--storage',str(state)],input=json.dumps(req),capture_output=True,text=True,env=env,timeout=480)
        response=json.loads(proc.stdout) if proc.stdout.strip() else {'ok':False,'error':proc.stderr}
        result['rows'].append({'label':label,'seconds':round(time.monotonic()-start,3),'response':response});save()
        print(json.dumps({'label':label,'ok':response.get('ok'),'state':response.get('data',{}).get('state')}),flush=True)
        assert proc.returncode==0 and response.get('ok'),response
        return response['data']
    def step(f,event='step',tier='low'):
        return send(event,op='orchestrate',event=event,flow_id=f['flow_id'],cursor=f['cursor'],
                    binding=f.get('binding'),armed=True,models=models,tier=tier)
    try:
        for _ in range(40):
            if (state/'telemetry.json').exists():break
            time.sleep(.1)
        send('task',op='auto_new',goal='Review project Cedar CED724. Keep the STP kernel unchanged. Read only; no network, edits, tests or dependencies.',
             roots=['.'],review_source_kib=10,review_tokens=80000,review_calls=20)
        f=send('router',op='orchestrate',event='start',prompt='Review the high-level architecture of this workspace for project Cedar CED724. Explain the supplied components and a worked input/output example, citing fresh source. Keep this read only; no network, edits, tests or dependencies. Preserve the STP kernel.',
               armed=False,models=models,tier='low')
        for _ in range(25):
            if f['state'] in ('SYNTHESIS','DONE','PAUSED'):break
            f=step(f)
        assert f['state']=='SYNTHESIS',f
        sid=f['binding']['session'];nid=f['execution_id']
        ledger=InvocationLedger(state/'state.sqlite')
        try:
            before=usage(ledger,sid);contract=ledger.pinned(sid);session=machine.node(ledger,nid)
            # Inject a recoverable stop without a model call or budget reset.
            flow_api.move(ledger,f['flow_id'],'PAUSED','CONTEXT_STOPPED',workflow_stop={
                'status':'context_budget','stage':'SYNTHESIS','error':'Validation pause before synthesis; no dispatch.'})
            f=flow_api.response(ledger,f['flow_id'])
        finally:ledger.close()
        f=step(f,event='resume',tier='mid')
        ledger=InvocationLedger(state/'state.sqlite')
        try:
            assert usage(ledger,sid)==before
            assert ledger.pinned(sid)==contract and machine.node(ledger,nid)==session
            result['checks']['reload_resume_no_reset']=True
        finally:ledger.close()
        f=step(f,tier='mid');assert f['state']=='DONE',f
        data=f['data'];assert data['result']['admission']['admitted'],data['result']['admission']
        ledger=InvocationLedger(state/'state.sqlite')
        try:
            after=usage(ledger,sid);result['before']=before;result['after']=after
            packets=[json.loads(raw) for raw, in ledger.db.execute("SELECT d.payload_json FROM deliveries d JOIN invocations i ON i.delivery_id=d.id WHERE i.session=? AND json_extract(i.record_json,'$.role')='chat_reviewer'",(sid,))]
            result['checks'].update(model_switched=data['route']['model']==args.worker,
                pinned_memory_retained=bool(packets and 'CED724' in json.dumps(packets[-1].get('memory'))),
                contract_unchanged=ledger.pinned(sid)==contract,
                source_paged=data['coverage']['deferred_files']>0,
                spending_increased=all(after['used'][k]>=before['used'][k] for k in before['used']) and after['used']['calls']>before['used']['calls'],
                limits_unchanged=after['limits']==before['limits'] and after['allocation_version']==0,
                execution_closed=machine.state(ledger,nid)=='DONE',
                receipt_integrity=ledger.verify_invocations(sid) and ledger.audit_valid(),
                no_source_changes=source=={p.name:p.read_bytes() for p in ws.iterdir()})
        finally:ledger.close()
        result['passed']=all(result['checks'].values())
        if not result['passed']:raise AssertionError(result['checks'])
    except BaseException as exc:
        result['error']=str(exc);raise
    finally:
        save();monitor.terminate();monitor.wait(timeout=5)


if __name__=='__main__':main()
