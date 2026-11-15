"""Isolated live model switch with an injected resource pause; no source tools."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'harness'))
from invocation_gateway import InvocationLedger
from conversation import answer
from review_budget import usage
from monitoring import resource_state
from lifecycle import deadline
import runtime_memory as memory


def main(output, first, second, require_calibration=False):
    output.mkdir(parents=True,exist_ok=True)
    store=output/'isolated-state';store.mkdir(exist_ok=False)
    workspace=output/'empty-workspace';workspace.mkdir()
    os.environ.update(SUTRA_RUNTIME_DIR=str(output/'runtime'),SUTRA_MEMORY_OWNERSHIP='shared',SUTRA_THINKING='off')
    process=subprocess.Popen([str(ROOT/'bin/telemetry')],stdout=subprocess.PIPE,text=True)
    def collect():
        for line in process.stdout:
            try:
                value=json.loads(line);tmp=store/'telemetry.tmp';tmp.write_text(json.dumps(value));tmp.replace(store/'telemetry.json')
            except (OSError,ValueError):pass
    thread=threading.Thread(target=collect,daemon=True);thread.start()
    report={'passed':False,'pressure_injected':True,'real_pressure_stress':False,'workspace_tools_used':False,'models':[first,second]}
    s=None
    try:
        for _ in range(30):
            if (store/'telemetry.json').exists():break
            time.sleep(.1)
        assert resource_state(store)['admitted'],'Current resources are not ALLOW; live experiment deferred'
        assert not memory.running(),'Another model is already resident; live experiment deferred'
        s=InvocationLedger(store/'state.sqlite');meta={'workspace':str(workspace)}
        models={'low':first,'mid':first,'high':second}
        prompt='Remember project Cedar, ticket CED724. Keep all work read only. No network or new dependencies. Reply with one sentence confirming these facts and constraints.'
        with deadline():one=answer(s,meta,store,{'prompt':prompt,'models':models,'tier':'low'})
        assert one['result']['admission']['admitted'],one['result']['admission']
        sid=one['session'];parent=one['result']['invocation_id'];contract=s.snapshot(sid);spent=usage(s,sid)['used']
        blocked=dict(resource_state(store),admitted=False,decision='BRIDGE',reason='Injected memory warning')
        with patch('monitoring.resource_state',return_value=blocked):pause=memory.recover(store)
        assert not pause['admitted'] and pause['action']=='observe_only_shared_server'
        assert usage(s,sid)['used']==spent
        s.close();s=InvocationLedger(store/'state.sqlite')
        recovered=memory.recover(store)
        assert recovered['admitted'],'Resources have not recovered; live experiment paused'
        assert usage(s,sid)['used']==spent and s.snapshot(sid)==contract
        with deadline():two=answer(s,meta,store,{'prompt':'What project and ticket did I name? Repeat my three work constraints. Do not perform any work.','parent':parent,'models':models,'tier':'high'})
        assert two['result']['admission']['admitted'],two['result']['admission']
        assert two['session']==sid and two['memory_used'] and s.snapshot(sid)==contract
        text=two['result']['accepted_output']['answer']
        assert 'CED724' in text and 'Cedar' in text and 'network' in text.lower(),text
        assert 'dependenc' in text.lower() and ('read-only' in text.lower() or 'read only' in text.lower()),text
        assert usage(s,sid)['used']['calls']>spent['calls'] and usage(s,sid)['used']['tokens']>spent['tokens']
        assert s.verify_invocations(sid)
        traces=[]
        events={e['call_id']:e for e in memory.read_state().get('events',[]) if e['event']=='dispatch'}
        for p in sorted((store/'calls').glob('*.json')):
            d=json.loads(p.read_text());response=d.get('response',{});event=events.get(d['call_id'],{})
            traces.append({'model':event.get('model'),'requested_context_tokens':event.get('context_tokens'),'context_source':'host dispatch record','input_tokens':response.get('prompt_eval_count'),'output_tokens':response.get('eval_count')})
        plans=[]
        for raw, in s.db.execute('SELECT record_json FROM orchestration_events'):
            event=json.loads(raw)
            if event['kind']=='DISPATCH' and event['data'].get('kind')=='model':
                plan=(event['data'].get('resource')or{}).get('runtime_memory')
                if plan:plans.append(plan)
        if require_calibration:
            assert len(plans)>=2 and all(p.get('calibration') for p in plans),plans
            assert len({p['calibration']['profile_sha256'] for p in plans})>=2,plans
        report.update(calibrated_plans=plans,passed=True,answer=text,contract_preserved=True,budget_before_pause=spent,budget_after=usage(s,sid)['used'],traces=traces,runtime=memory.snapshot(),receipts_verified=True)
    except BaseException as exc:
        report['error']=str(exc)
        raise
    finally:
        if s:s.close()
        process.terminate();process.wait(timeout=5);thread.join(timeout=1);process.stdout.close()
        (output/'Live-memory-switch.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:report.get(k) for k in ('passed','models','error','traces')}),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);parser.add_argument('--first',default='qwen3:4b');parser.add_argument('--second',default='local-assistant:latest')
    parser.add_argument('--require-calibration',action='store_true')
    args=parser.parse_args();main(args.output,args.first,args.second,args.require_calibration)
