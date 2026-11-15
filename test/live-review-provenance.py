"""Explicit, bounded live validation in a disposable project; no user-state mutation."""
import argparse,json,os,subprocess,sys,threading,time
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'harness'))
import auto_review,chat_runtime,model_identity,review_budget
from invocation_gateway import InvocationLedger
from vscode_bridge import handle
from lifecycle import deadline
from test_chat import Fake
from api_runtime import provider_for
from review_context import context_budget


def run(output):
    output.mkdir(parents=True,exist_ok=True)
    work=output/'live';work.mkdir(exist_ok=False)
    ws=work/'project';ws.mkdir();store=work/'store';store.mkdir()
    (ws/'entry.py').write_text('from service import double\ndef main(value):\n    return double(value)\n')
    (ws/'service.py').write_text('def double(value):\n    return value * 2\n\ndef countdown(n):\n    return countdown(n-1) if n > 0 else 0\n')
    telemetry=subprocess.Popen([str(ROOT/'bin/telemetry')],stdout=subprocess.PIPE,text=True)
    def collect():
        for line in telemetry.stdout:
            try:
                sample=json.loads(line);tmp=store/'telemetry.tmp';tmp.write_text(json.dumps(sample));tmp.replace(store/'telemetry.json')
            except (ValueError,OSError):pass
    threading.Thread(target=collect,daemon=True).start()
    report={'passed':False,'fixture_only':True,'steps':[]}
    s=None
    try:
        for _ in range(100):
            if (store/'telemetry.json').exists():break
            time.sleep(.1)
        os.environ.update(SUTRA_THINKING='off',SUTRA_ACTIVITY='0',SUTRA_LOGIT_SENSOR='1')
        ids={n:model_identity.observe(n) for n in ('local-assistant:latest','qwen3.8:27b','qwen3:4b','qwen3.6:35b')}
        report['identities']=ids
        assert ids['local-assistant:latest']['weights_sha256']==ids['qwen3.8:27b']['weights_sha256']
        handle(store,ws,{'op':'auto_new','goal':'Review fresh source; do not edit, test or access the network.','roots':['.'],'review_tokens':250000})
        meta=json.loads((store/'workspace.json').read_text());sid=meta['active'];s=InvocationLedger(store/'state.sqlite')
        original=s.pinned(sid)
        auto_review.step(s,meta,ws,store,{'explain':False})
        def wait_ready():
            from monitoring import require_resources
            from context_manager import InvalidUpdate
            for _ in range(60):
                try:return require_resources(store)
                except InvalidUpdate:time.sleep(.5)
            raise AssertionError('Resources did not recover within the validation wait')
        def recover(out):
            errors=out['result']['admission'].get('errors',[])
            if not out['result']['admission']['admitted'] and any('RESOURCE_WAIT' in e for e in errors):
                # Explicit validation of the saved candidate, without replaying or refunding its writer.
                # Production may require the user to resume after this resource pause.
                from monitoring import require_resources
                from context_manager import InvalidUpdate
                import report_review,review_report
                print('waiting for observed resource recovery',flush=True)
                wait_ready()
                cid=out['result']['invocation_id']
                review=report_review.run(s,sid,store,cid,strict=True)
                assert review['admission']['admitted'],review['admission']
                value,pub=report_review.published(s,sid,cid,review['invocation_id'])
                out['resource_recovery']={'original_errors':errors,'same_candidate':cid,'writer_replayed':False,'budget_refunded':False,'explicit_validation_resume':True}
                out['result'].update(accepted_output=value,publication=pub,admission={'admitted':True,'errors':[]})
                out['report_review']=pub;out['report_markdown']=review_report.markdown(value)
                meta['chat']={'checkpoint':cid,'reviewer_id':review['invocation_id'],'route':out['route']}
            return out
        previous=None
        for index,writer in enumerate(('local-assistant:latest','qwen3.6:35b')):
            print('architecture writer',writer,flush=True)
            wait_ready()
            before=review_budget.usage(s,sid)
            with deadline(650):
                prompt=('Explain the architecture, its strengths and limitations. Keep it brief: two components, two flow steps and one concrete next check. Write each next check as a sentence of at least six words.' if index==0 else 'Continue from the previous review: explain how countdown terminates. Use two short sentences, preserving the task constraints.')
                out=chat_runtime.answer(s,meta,ws,store,{'prompt':prompt,
                    'models':dict(low=writer,mid=writer,high=writer),'tier':'high','require_distinct_reviewer':True})
            out=recover(out)
            report['steps'].append(out);(output/'Live-validation.json').write_text(json.dumps(report,indent=2))
            assert out['result']['admission']['admitted'],out['result']['admission']
            if index==0:
                assert out['report_review']['independence']['weight_relation']=='different'
                assert out['report_review']['independence']['reviewer_model']=='qwen3:4b'
            else:
                assert out['report_review'] is None  # A focused follow-up has no new architecture report.
            env=json.loads(s.db.execute('SELECT record_json FROM invocations WHERE id=?',(out['result']['invocation_id'],)).fetchone()[0])
            payload=json.loads(s.db.execute('SELECT payload_json FROM deliveries WHERE id=?',(env['delivery_id'],)).fetchone()[0])
            assert payload['contract']==original
            if previous:assert payload['handoff']['invocation_id']==previous
            previous=out['result']['invocation_id']
            after=review_budget.usage(s,sid);assert after['limits']==before['limits'] and after['used']['calls']>=before['used']['calls']+(2 if index==0 else 1)
            (output/('Architecture-'+writer.split(':')[0]+'.md')).write_text(out['report_markdown'])
        print('corrupted claim review',flush=True)
        writer=Fake(fault=lambda r,p:r['report']['flow'][0].update(description='The double function recursively calls itself forever.',paths=['service.py']))
        wait_ready()
        reader=provider_for(store,'qwen3:4b',review=True);context_budget(reader,s,sid);reader.keep_alive=0
        out=chat_runtime.answer(s,meta,ws,store,{'prompt':'Architecture overview.','models':dict(low=writer.model,mid=writer.model,high=writer.model),'tier':'low'},provider=writer,review_provider=reader)
        report['corrupted']=out
        assert out['result']['admission']['admitted'],out['result']['admission']
        assert any(c['claim']=='flow/0' for c in out['report_review']['omitted']),out['report_review']
        assert 'recursively calls itself forever' not in out['report_markdown']
        assert s.verify_invocations(sid) and s.pinned(sid)==original
        from logit_sensor import read
        report['sensor']=read(store)
        report['passed']=True
    finally:
        if s:s.close()
        telemetry.terminate();telemetry.wait(timeout=5)
        (output/'Live-validation.json').write_text(json.dumps(report,indent=2)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    run(p.parse_args().output)
