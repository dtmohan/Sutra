"""Opt-in real local-model switch; separate ledger and loopback transport only."""
import argparse,json,os,subprocess,sys,threading,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'harness'))
from api_runtime import provider_for
from conversation import ConversationModel,answer
from invocation_gateway import InvocationLedger
from review_budget import usage
from budget_accounting import used,reserved


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False);state=out/'state';state.mkdir()
    os.environ.update(SUTRA_THINKING='adaptive',SUTRA_MEMORY_OWNERSHIP='shared',SUTRA_RUNTIME_DIR=str(out/'runtime'))
    monitor=subprocess.Popen([str(ROOT/'bin/telemetry')],stdout=subprocess.PIPE,text=True)
    def samples():
        for line in monitor.stdout:
            try:json.loads(line);p=state/'telemetry.tmp';p.write_text(line);p.replace(state/'telemetry.json')
            except (ValueError,OSError):pass
    threading.Thread(target=samples,daemon=True).start()
    result={'passed':False,'scope':'three fresh local calls with a data-only model switch; isolated ledger','turns':[]}
    ledger=InvocationLedger(state/'state.sqlite');parent=None;original=None
    try:
        for _ in range(50):
            if (state/'telemetry.json').exists():break
            time.sleep(.1)
        models={'low':'qwen3:4b','mid':'local-assistant:latest','high':'local-assistant:latest'}
        requests=[('low','Remember project Cedar, ticket CED724. Do not edit files, run tests, access the network or add dependencies. Keep the STP kernel unchanged. Briefly acknowledge.'),
                  ('high','Correct the ticket to CED725. Keep every earlier restriction. State the project, current ticket and all restrictions.'),
                  ('low','What is the current project and corrected ticket? List the restrictions still in force. Use the saved conversation.')]
        for tier,prompt in requests:
            provider=provider_for(state,models[tier],model_class=ConversationModel);provider.keep_alive=0
            from model_context import allocation
            provider.budget,_=allocation(provider.context_metadata)
            started=time.monotonic()
            r=answer(ledger,{},state,{'prompt':prompt,'parent':parent,'models':models,'tier':tier},provider)
            result['turns'].append({'model':provider.model,'seconds':time.monotonic()-started,'result':r})
            (out/'results.json').write_text(json.dumps(result,indent=2)+'\n')
            assert r['result']['admission']['admitted'],r['result']['admission']
            text=r['result']['accepted_output']['answer'];print(provider.model,text,flush=True)
            parent=r['result']['invocation_id'];sid=r['session'];snapshot=ledger.snapshot(sid)
            if original is None:original=snapshot
            assert snapshot==original
            if len(result['turns'])>1:
                assert 'Cedar' in text and 'CED725' in text,text
                for word in ('edit','test','network','dependenc','kernel'):assert word in text.lower(),(word,text)
            assert ledger.verify_invocations(sid)
            ledger.close();ledger=InvocationLedger(state/'state.sqlite')
        calls=ledger.db.execute('SELECT i.record_json,r.record_json FROM invocations i JOIN invocation_receipts r ON r.invocation_id=i.id WHERE i.session=?',(sid,)).fetchall()
        expected=0
        for raw,receipt_raw in calls:
            env,rec=json.loads(raw),json.loads(receipt_raw);expected+=used(env,rec)
            assert rec['accounting_settlement']['basis']=='runtime_measured'
            assert env['model_interface']['role']=='conversation'
        assert usage(ledger,sid)['used']['tokens']==expected
        result.update(passed=True,usage=usage(ledger,sid),reserved_total=sum(reserved(json.loads(raw)) for raw,_ in calls),measured_total=expected)
    finally:
        (out/'results.json').write_text(json.dumps(result,indent=2)+'\n');ledger.close();monitor.terminate();monitor.wait(timeout=5)

if __name__=='__main__':main()
