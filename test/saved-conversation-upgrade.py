"""Validate conversation migration on an isolated SQLite backup, without inference."""
import argparse,json,sqlite3,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'harness'))
import conversation,conversation_memory
from context_manager import canonical,sha
from invocation_gateway import InvocationLedger
from paging import PageFault
from test_conversation import Fake


def run(state,output):
    output.mkdir(parents=True,exist_ok=True)
    copied=output/'conversation-backup.sqlite'
    assert not copied.exists()
    original=sqlite3.connect((state/'state.sqlite').resolve().as_uri()+'?mode=ro',uri=True)
    backup=sqlite3.connect(copied);original.backup(backup);backup.close();original.close()
    s=InvocationLedger(copied)
    try:
        row=s.db.execute("SELECT i.id,i.session FROM invocations i JOIN invocation_receipts r ON r.invocation_id=i.id WHERE json_extract(i.record_json,'$.role')='conversation' AND json_extract(r.record_json,'$.status')='accepted' ORDER BY i.rowid DESC LIMIT 1").fetchone()
        assert row
        iid,sid=row
        before=conversation_memory.history(s,sid,iid);contract=s.current_contract(sid)
        try:s.load_result(sid,iid)
        except PageFault:old_executable_withheld=True
        else:old_executable_withheld=False
        assert old_executable_withheld
        model=Fake()
        out=conversation.answer(s,{'workspace':str(output)},output,{'prompt':'Continue our existing conversation.',
            'parent':iid,'models':{'low':model.model,'mid':model.model,'high':model.model},'tier':'low'},provider=model)
        assert out['result']['admission']['admitted'],out
        memory=model.payloads[0]['memory']
        assert memory['legacy_data_restored'] and memory['stored_turns']==len(before)
        assert out['session']==sid and s.current_contract(sid)==contract
        assert s.verify_invocations(sid,complete=False)
        result={'passed':True,'source_read_only':True,'inference':False,'saved_turns':len(before),
                'same_session':True,'contract_preserved':True,'old_executable_withheld':True,
                'memory_sha256':sha(canonical(memory))}
        (output/'Saved-conversation-upgrade.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result))
    finally:s.close()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--state',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();run(a.state,a.output)
