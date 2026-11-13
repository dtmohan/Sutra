"""Real Ollama children; a fresh bridge process for every state transition."""
import argparse,json,os,subprocess,sys,threading,time
from pathlib import Path
SOURCE=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(SOURCE/'harness'))
from invocation_gateway import InvocationLedger
import orchestration as m


def main(out):
    out.mkdir(parents=True,exist_ok=True);ws=out/'repo';ws.mkdir();root=out/'state';root.mkdir()
    files={'entry.py':'from service import calculate\ndef start(value):\n    return calculate(value)\n',
           'service.py':'from adapter import normalize\ndef calculate(value):\n    return normalize(value) * 2\n',
           'adapter.py':'def normalize(value):\n    return max(0, value)\n',
           'settings.py':'FACTOR = 2\ndef configured_factor():\n    return FACTOR\n'}
    for p,t in files.items():(ws/p).write_text(t)
    telemetry=subprocess.Popen([str(SOURCE/'bin/telemetry')],stdout=subprocess.PIPE,text=True)
    def collect():
        for line in telemetry.stdout:
            try:
                sample=json.loads(line);tmp=root/'telemetry.tmp';tmp.write_text(json.dumps(sample));tmp.replace(root/'telemetry.json')
            except (ValueError,OSError):pass
    threading.Thread(target=collect,daemon=True).start()
    report={'passed':False,'fresh_bridge_per_step':True,'steps':[],'models':[],'semantic_correctness_verified':False}
    models={'low':'qwen3:4b','mid':'qwen3.6:35b','high':'local-assistant:latest'}
    prompt='Inspect the current workspace source and explain its architecture. Remember project Cedar and reference CED724. Keep this review read only. No network access and no new dependencies. Cite the supplied source and report coverage limits.'
    def save(): (out/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    def request(req):
        started=time.monotonic();r=subprocess.run([sys.executable,'-B',str(SOURCE/'harness/vscode_bridge.py'),'--storage',str(root),'--workspace',str(ws)],input=json.dumps(req),capture_output=True,text=True,timeout=480,env=dict(os.environ,KARMA_TYPESCRIPT=str(SOURCE/'vendor/typescript/typescript.js')))
        if r.returncode:raise RuntimeError(r.stdout+r.stderr)
        data=json.loads(r.stdout)
        if not data['ok']:raise RuntimeError(data)
        data=data['data'];entry={'event':req['event'],'state':data['state'],'elapsed':round(time.monotonic()-started,2),'flow_id':data['flow_id']}
        for key in ('conversation','child','data'):
            if key in data:
                entry[key]={'route':data[key].get('route'),'result':data[key].get('result')}
                if data[key].get('route'):report['models'].append(data[key]['route']['model'])
        report['steps'].append(entry);save();print(json.dumps({k:entry[k] for k in ('event','state','elapsed')}),flush=True)
        return data
    try:
        for _ in range(50):
            if (root/'telemetry.json').exists():break
            time.sleep(.1)
        result=request({'op':'orchestrate','event':'start','prompt':prompt,'models':models,'tier':'mid'})
        assert result['state']=='NEED_ARM',result
        children=0;binding=None
        for _ in range(30):
            if result['state']=='DONE':break
            tier='high' if children else 'mid'
            result=request({'op':'orchestrate','event':'step','flow_id':result['flow_id'],'cursor':result['cursor'],'binding':result.get('binding'),'armed':True,'models':models,'tier':tier})
            if result.get('binding'):
                if binding:assert binding==result['binding']
                binding=result['binding']
            if result.get('child'):
                assert result['child']['result']['admission']['admitted'],result['child']['result']['admission'];children+=1
        assert result['state']=='DONE' and children>=2,result
        assert result['data']['result']['admission']['admitted'],result['data']['result']['admission']
        s=InvocationLedger(root/'state.sqlite')
        try:
            sid=binding['session'];contract=s.pinned(sid);report['binding']=binding
            packets=[]
            for role,raw in s.db.execute("SELECT json_extract(i.record_json,'$.role'),d.payload_json FROM invocations i JOIN deliveries d ON d.id=i.delivery_id WHERE i.session=? ORDER BY i.rowid",(sid,)):
                p=json.loads(raw)
                if role in ('subagent','chat_reviewer'):
                    assert p['contract']==contract;assert 'CED724' in json.dumps(p['memory']);packets.append(p)
            report['children']=children;report['goal_and_constraints_preserved']=True;report['smriti_facts_preserved']=True
            report['parent_budget']=m.spent(s,packets[0]['branch']['parent_id'])
            report['child_models']=list(dict.fromkeys(p['route']['model'] for p in packets if 'branch' in p))
            assert len(report['child_models'])==2
            assert not contract['constraints']['edits_allowed'] and not contract['constraints']['network_allowed']
            report['ledger_valid']=s.verify_invocations(sid);assert report['ledger_valid']
            report['audit_valid']=s.audit_valid();assert report['audit_valid']
            report['kernel_sha256']=__import__('hashlib').sha256((SOURCE/'harness/stp_canonical/kernel.py').read_bytes()).hexdigest()
            report['answer']=result['data']['result']['accepted_output'];report['passed']=True
        finally:s.close()
    except BaseException as e:report['error']=str(e);raise
    finally:
        telemetry.terminate();telemetry.wait(timeout=5);save()
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);main(p.parse_args().output)
