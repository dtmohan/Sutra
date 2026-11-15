"""Small governed thinking-on parser probe; local model diagnostics only."""
import argparse,json,os,subprocess,sys,threading,time
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'harness'))
from invocation_gateway import InvocationLedger
from local_model import LocalModel
from paging import Budget
from runtime import ingest
from logit_sensor import read
from monitoring import require_resources
from context_manager import InvalidUpdate


def run(output):
    output.mkdir(parents=True,exist_ok=False)
    telemetry=subprocess.Popen([str(ROOT/'bin/telemetry')],stdout=subprocess.PIPE,text=True)
    def collect():
        for line in telemetry.stdout:
            try:
                x=json.loads(line);p=output/'telemetry.tmp';p.write_text(json.dumps(x));p.replace(output/'telemetry.json')
            except (ValueError,OSError):pass
    threading.Thread(target=collect,daemon=True).start()
    s=InvocationLedger(output/'state.sqlite');result={'passed':False}
    try:
        for _ in range(60):
            try:require_resources(output);break
            except InvalidUpdate:time.sleep(.5)
        else:raise AssertionError('Resource gate did not become ready')
        os.environ.update(SUTRA_LOGIT_SENSOR='1',SUTRA_ACTIVITY='0')
        s.create('sensor')
        provider=LocalModel(output/'calls',11434,'qwen3:4b',budget=Budget(8192,2048,256),thinking=True)
        provider.keep_alive=0
        answer=ingest(s,'sensor','No network access.',provider)
        rows=read(output)
        assert s.verify_invocations('sensor')
        if not answer['committed']:
            statuses=[json.loads(r[0])['status'] for r in s.db.execute('SELECT record_json FROM invocation_receipts')]
            assert statuses and all(value=='quarantined' for value in statuses)
            assert s.snapshot('sensor')['constraints'].get('network_allowed') is not True
        assert rows and rows[-1]['available'] and rows[-1]['counts']['thinking']>0
        result={'passed':True,'governed_parser':True,'model':'qwen3:4b','thinking_requested':True,'model_proposal_committed':bool(answer['committed']),
                'counts':rows[-1]['counts'],'attribution':rows[-1]['attribution'],
                'sensor_is_advisory':True,'raw_reasoning_in_report':False}
        print(json.dumps(result))
    finally:
        telemetry.terminate();telemetry.wait(timeout=5);s.close()
        (output/'Thinking-sensor.json').write_text(json.dumps(result,indent=2)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);run(p.parse_args().output)
