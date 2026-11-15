"""Reproduce a review on a read-only backup of a saved task, with real inference."""
import argparse,json,os,sqlite3,subprocess,sys,threading,time
from pathlib import Path
SOURCE=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(SOURCE/'harness'))
from invocation_gateway import InvocationLedger
from review_budget import usage
from lifecycle import deadline
import auto_review,chat_runtime


def run(state,scratch,out,iid,model):
    scratch.mkdir(parents=True,exist_ok=False);out.mkdir(parents=True,exist_ok=True)
    original=sqlite3.connect((state/'state.sqlite').resolve().as_uri()+'?mode=ro',uri=True)
    copied=sqlite3.connect(scratch/'state.sqlite');original.backup(copied);copied.close();original.close()
    meta=json.loads((state/'workspace.json').read_text());workspace=Path(meta['workspace'])
    s=InvocationLedger(scratch/'state.sqlite');sid=meta['active']
    env=json.loads(s.db.execute('SELECT record_json FROM invocations WHERE id=?',(iid,)).fetchone()[0])
    old=json.loads(s.db.execute('SELECT payload_json FROM deliveries WHERE id=?',(env['delivery_id'],)).fetchone()[0])
    assert env['session']==sid
    before=usage(s,sid);contract=s.pinned(sid)
    report={'passed':False,'original_state_read_only':True,'old_invocation':iid,
            'previous_excerpts':{p:len(t.encode()) for p,t in old['excerpts'].items()},'before':before}
    telemetry=subprocess.Popen([str(SOURCE/'bin/telemetry')],stdout=subprocess.PIPE,text=True)
    def collect():
        for line in telemetry.stdout:
            try:
                sample=json.loads(line);temp=scratch/'telemetry.tmp';temp.write_text(json.dumps(sample));temp.replace(scratch/'telemetry.json')
            except (OSError,ValueError):pass
    threading.Thread(target=collect,daemon=True).start()
    try:
        for _ in range(50):
            if (scratch/'telemetry.json').exists():break
            time.sleep(.1)
        os.environ.update(SUTRA_THINKING='off',SUTRA_ACTIVITY='0')
        with deadline(700):
            for _ in range(100):
                mapped=auto_review.step(s,meta,workspace,scratch,{'explain':False})
                print('mapping',mapped['auto']['mapped_files'],mapped['auto']['status'],flush=True)
                if mapped['auto']['status']=='structural_pass_complete':break
                assert mapped['auto']['status'] in ('ready','page_rejected'),mapped
            else:raise AssertionError('Mapping did not finish')
            request={'prompt':old['request'],'models':dict(low=model,mid=model,high=model),'tier':'high',
                     'conversation_id':old['conversation_handoff']['invocation_id']}
            result=chat_runtime.answer(s,meta,workspace,scratch,request)
        report['result']=result
        assert result['result']['admission']['admitted'],result['result']['admission']
        new=result['result']['invocation_id'];row=json.loads(s.db.execute('SELECT record_json FROM invocations WHERE id=?',(new,)).fetchone()[0])
        p=json.loads(s.db.execute('SELECT payload_json FROM deliveries WHERE id=?',(row['delivery_id'],)).fetchone()[0])
        report['packet']={k:p[k] for k in ['workspace_map','evidence_coverage','review_capabilities','memory_budget']}
        report['input_charge']=row['charge'];report['after']=usage(s,sid)
        assert p['workspace_map']['source_files']==mapped['auto']['source_files']
        assert p['workspace_map']['omitted_files']==0
        assert len(p['excerpts'])>=min(6,len(old['excerpts']))
        review=result['result']['accepted_output'].get('report')
        assert review and len(review['components'])>=3 and len(review['flow'])>=2
        assert review['strengths'] and review['risks'] and review['next_steps']
        assert all(len(row['description'].split())>=4 for row in review['flow'])
        assert '\n\n## Main components\n\n' in result['report_markdown']
        assert result['review_diagram'] is not None
        report['words']=len(result['report_markdown'].split())
        report['excerpt_locations']=p['excerpt_locations']
        (out/'Live-review.md').write_text(result['report_markdown']+'\n')
        (out/'Live-source-diagram.json').write_text(json.dumps(result['review_diagram'],indent=2)+'\n')
        assert s.pinned(sid)==contract and report['after']['limits']==before['limits']
        assert report['after']['used']['calls']>before['used']['calls']
        assert s.verify_invocations(sid,complete=False)
        report.update(passed=True,contract_preserved=True,budgets_not_reset=True)
    except BaseException as e:report['error']=str(e);raise
    finally:
        telemetry.terminate();telemetry.wait(timeout=5);s.close()
        (out/'Live-review-context.json').write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--state',type=Path,required=True);p.add_argument('--scratch',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--invocation',required=True);p.add_argument('--model',default='local-assistant:latest')
    a=p.parse_args();run(a.state,a.scratch,a.output,a.invocation,a.model)
