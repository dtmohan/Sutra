"""Known claim fixtures evaluated by the actual small local reviewer."""
import argparse,json,os,subprocess,sys,threading,time
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'harness'))
import auto_review,chat_runtime,review_budget
from invocation_gateway import InvocationLedger
from vscode_bridge import handle
from test_chat import Fake
from api_runtime import provider_for
from review_context import context_budget
from monitoring import require_resources
from context_manager import InvalidUpdate


def run(output):
    output.mkdir(parents=True,exist_ok=False)
    ws=output/'project';ws.mkdir();store=output/'store';store.mkdir()
    (ws/'entry.py').write_text('from leaf import run\ndef start(): return run()\n')
    (ws/'leaf.py').write_text('def run(): return 1\ndef recur(n): return recur(n-1) if n > 0 else 0\n')
    telemetry=subprocess.Popen([str(ROOT/'bin/telemetry')],stdout=subprocess.PIPE,text=True)
    def collect():
        for line in telemetry.stdout:
            try:
                x=json.loads(line);p=store/'telemetry.tmp';p.write_text(json.dumps(x));p.replace(store/'telemetry.json')
            except (ValueError,OSError):pass
    threading.Thread(target=collect,daemon=True).start()
    report={'passed':False,'cases':[]};s=None
    try:
        os.environ.update(SUTRA_THINKING='off',SUTRA_ACTIVITY='0',SUTRA_LOGIT_SENSOR='1')
        handle(store,ws,{'op':'auto_new','goal':'Review source without changing it.','roots':['.']})
        meta=json.loads((store/'workspace.json').read_text());sid=meta['active'];s=InvocationLedger(store/'state.sqlite')
        auto_review.step(s,meta,ws,store,{'explain':False},provider=Fake())
        contract=s.pinned(sid)
        for name,claim,accepted in [('unsupported','The run function calls itself recursively without a base case.',False),
                                    ('legitimate','The recur function calls itself with n minus one while n is positive.',True)]:
            for _ in range(60):
                try:require_resources(store);break
                except InvalidUpdate:time.sleep(.5)
            else:raise AssertionError('Resources unavailable')
            writer=Fake(fault=lambda r,p:r['report']['flow'][0].update(description=claim,paths=['leaf.py']))
            reader=provider_for(store,'qwen3:4b',review=True);context_budget(reader,s,sid);reader.keep_alive=0
            print('claim',name,flush=True)
            out=chat_runtime.answer(s,meta,ws,store,{'prompt':'Architecture overview.',
                    'models':dict(low=writer.model,mid=writer.model,high=writer.model),'tier':'low'},provider=writer,review_provider=reader)
            report['cases'].append({'case':name,'result':out})
            assert out['result']['admission']['admitted'],out['result']['admission']
            omitted={v['claim'] for v in out['report_review']['omitted']}
            assert ('flow/0' not in omitted)==accepted,out['report_review']
            assert (claim in out['report_markdown'])==accepted
            assert s.pinned(sid)==contract and s.verify_invocations(sid)
        report.update(passed=True,budget=review_budget.usage(s,sid),contract_preserved=True)
    finally:
        telemetry.terminate();telemetry.wait(timeout=5)
        if s:s.close()
        (output/'Corrupted-report-validation.json').write_text(json.dumps(report,indent=2)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);run(p.parse_args().output)
