"""Opt-in live warm-model and Auto fallback tests in isolated workspace/state.

The capacity stop is injected; no real memory exhaustion is induced. Warm loading
uses Ollama's empty preload request after the ordinary cold allocation fits.
No user files, settings, permissions or ledgers are changed.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.request
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'harness'))
from invocation_gateway import InvocationLedger
from conversation import answer, contract
from context_manager import canonical
from monitoring import resource_state
from lifecycle import deadline
from review_budget import usage
import runtime_memory as memory
import orchestration as machine
import orchestration_flow as flow


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--large',default='local-assistant:latest');args=parser.parse_args()
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    root=out/'state';root.mkdir();ws=out/'workspace';ws.mkdir()
    (ws/'app.py').write_text('def normalize(value):\n    return max(0, value)\n\ndef calculate(value):\n    return normalize(value) * 2\n')
    os.environ.update(SUTRA_RUNTIME_DIR=str(out/'runtime'),SUTRA_MEMORY_OWNERSHIP='shared',SUTRA_THINKING='off')
    proc=subprocess.Popen([str(ROOT/'bin/telemetry')],stdout=subprocess.PIPE,text=True)
    def collect():
        for line in proc.stdout:
            try:
                json.loads(line);tmp=root/'telemetry.tmp';tmp.write_text(line);tmp.replace(root/'telemetry.json')
            except (ValueError,OSError):pass
    threading.Thread(target=collect,daemon=True).start()
    report={'passed':False,'capacity_stop_injected':True,'real_pressure_stress':False,'checks':{},'calls':[]}
    s=None
    def save(): (out/'results.json').write_text(json.dumps(report,indent=2)+'\n')
    def tell(message):print(message,flush=True)
    try:
        for _ in range(40):
            if (root/'telemetry.json').exists():break
            time.sleep(.1)
        assert resource_state(root)['admitted'],'System is not ready for live validation'
        for _ in range(60):
            if not memory.running():break
            time.sleep(1)
        assert not memory.running(),'Another resident model exists; live validation deferred'
        s=InvocationLedger(root/'state.sqlite');meta={'workspace':str(ws)}
        models={'low':'qwen3:4b','mid':args.large,'high':args.large}
        parent=None;sid=None;pin=None;spent=None
        prompts=[('low','Remember project Cedar-724. Never change the STP kernel. No network, edits or tests. Reply only: noted.'),
                 ('high','Recall the project label and what must stay unchanged. Repeat the operations I prohibited. Answer in one sentence.'),
                 ('low','Repeat the project label and all my restrictions. One sentence; no tools.')]
        for index,(tier,prompt) in enumerate(prompts):
            if index==1:
                from api_runtime import provider_for
                p=provider_for(root,args.large,review=True)
                tags=memory.api('/api/tags')['models'];tag=next(t for t in tags if t['name']==args.large)
                cold=memory.estimate(args.large,p.context_metadata,tag['size'],[],16384,resource_state(root),p.memory_profile,p.manifest_digest)
                assert cold['decision']=='ALLOW','Insufficient cold capacity to preload safely'
                request=urllib.request.Request('http://127.0.0.1:11434/api/generate',data=json.dumps({'model':args.large,'stream':False,'keep_alive':'2m','options':{'num_ctx':16384}}).encode(),headers={'Content-Type':'application/json'})
                with urllib.request.urlopen(request,timeout=120) as response:json.load(response)
                rows=memory.running();warm=memory.estimate(args.large,p.context_metadata,tag['size'],rows,16384,resource_state(root),p.memory_profile,p.manifest_digest)
                assert warm['resident_weight_credit_bytes']>0,warm
                assert warm['estimated_increment_bytes']<cold['estimated_increment_bytes']
                assert warm['calibration']['margin_bytes']==cold['calibration']['margin_bytes']
                report.update(warm_plan=warm,cold_plan=cold);tell('Warm inventory matched; retained full uncertainty margin and reserve.')
            with deadline():result=answer(s,meta,root,{'prompt':prompt,'parent':parent,'models':models,'tier':tier})
            assert result['result']['admission']['admitted'],result['result']['admission']
            parent=result['result']['invocation_id'];sid=result['session'];current=contract(s,sid);used=usage(s,sid)['used']
            if pin is None:pin=current
            else:
                assert current==pin and used['calls']>spent['calls'] and used['tokens']>spent['tokens']
                packet=json.loads(s.db.execute('SELECT d.payload_json FROM deliveries d JOIN invocations i ON i.delivery_id=d.id WHERE i.id=?',(parent,)).fetchone()[0])
                assert all(t in canonical(packet['memory']) for t in ('Cedar-724','STP kernel','No network, edits or tests'))
                assert 'Cedar-724' in result['result']['accepted_output']['answer']
            spent=used
            report['calls'].append({'model':result['route']['model'],'answer':result['result']['accepted_output']['answer'],'usage':result['result'].get('token_usage')})
            tell('Memory turn '+str(index+1)+' accepted on '+result['route']['model']);save()
            s.close();s=InvocationLedger(root/'state.sqlite')
        report['checks']['three_turn_model_switch_and_reload']=True
        report['checks']['warm_dispatch_credit']=any((json.loads(raw).get('data',{}).get('resource',{}).get('runtime_memory') or {}).get('resident_weight_credit_bytes',0)>0 for raw, in s.db.execute('SELECT record_json FROM orchestration_events'))
        assert report['checks']['warm_dispatch_credit']
        prompt='Review this workspace architecture briefly. Preserve project Cedar-724, keep the STP kernel unchanged, and use no network, edits or tests.'
        current=None;before_fallback=None;fallback_seen=False;workspace_pin=None
        real_estimate=memory.estimate
        inject_capacity=[True]
        def limited(model,*a,**kw):
            value=real_estimate(model,*a,**kw)
            if model==args.large and inject_capacity[0]:
                value.update(memory.KarmaAdapter().memory_allocation(value['estimated_increment_bytes'],5*memory.GIB,memory.HEADROOM))
                inject_capacity[0]=False
            return value
        for _ in range(30):
            request={'models':models,'tier':'auto','armed':True}
            if current:
                request.update(event='step',flow_id=current['flow_id'],cursor=current['cursor'],binding=current.get('binding'))
            else:request.update(event='start',prompt=prompt,parent=parent)
            with patch.object(memory,'estimate',side_effect=limited),deadline():
                current=flow.handle(s,meta,ws,root,request)
            if current.get('binding'):
                wsid=meta['active']
                if workspace_pin is None:workspace_pin=s.pinned(wsid)
                assert s.pinned(wsid)==workspace_pin
            if current.get('resource_fallback') and not fallback_seen:
                fallback_seen=True;before_fallback=usage(s,meta['active'])['used']
                report['fallback']=current['resource_fallback'];tell('Capacity stop governed fallback to '+current['resource_fallback']['model'])
                s.close();s=InvocationLedger(root/'state.sqlite')
            tell('Workspace state '+current['state'])
            if current['state']=='PAUSED':raise AssertionError(current.get('stop'))
            if current['state']=='DONE':break
        assert fallback_seen and current['state']=='DONE',current
        result=current['data']['result'];assert result['admission']['admitted'],result['admission']
        assert current['data']['route']['model']==models['low']
        after=usage(s,meta['active'])['used'];assert after['calls']>before_fallback['calls'] and after['tokens']>before_fallback['tokens']
        assert s.pinned(meta['active'])==workspace_pin and s.verify_invocations(meta['active'])
        assert not s.snapshot(meta['active'])['constraints']['network_allowed']
        report['checks']['governed_fallback_repage_reload_constraints_budgets']=True
        report.update(workspace_output=result['accepted_output'],budget_before=before_fallback,budget_after=after,passed=True)
    except BaseException as exc:
        report['error']=str(exc);raise
    finally:
        if s:s.close()
        proc.terminate();proc.wait(timeout=5);save()
    tell('PASS warm-model estimates, live model switches, reload and bounded Auto fallback.')


if __name__=='__main__':main()
