"""Local-model checks of parser/selector protocol, with labelled wire faults."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
import time
from unittest.mock import patch
from budget_preflight import host_seed
from dag_runtime import run_paged
from experiment import build, definitions
from health import HealthMonitor
from invocation_gateway import InvocationLedger
from local_model import LocalModel
from protocol import SELECT_SYSTEM
from paging import PageFault
from runtime import ingest
from validate_dag import metadata, POLICY_REQUEST, POLICY_CONTRACT

ROOT=Path(__file__).resolve().parent


class WireFault:
    def __init__(self,base,role,always=False):
        self.base=base;self.role=role;self.always=always;self.count=0
        self.model=base.model;self.budget=base.budget
    def call(self,cid,system,payload,schema):
        answer,record=self.base.call(cid,system,payload,schema)
        role='selector' if system==SELECT_SYSTEM else 'parser' if 'operations' in schema['properties'] else 'worker'
        if role==self.role:
            self.count+=1
            if self.always or self.count==1:
                record['model_answer_before_injection']=copy.deepcopy(answer)
                if role=='parser':
                    answer={'operations':[{'key':'network_allowed','value':'true','source_unit':0}]}
                    label='test adapter proposes permission opposite to No network access'
                else:
                    answer={'read_pages':['../../outside-task'],'browse':[]}
                    label='test adapter requests an unadvertised page'
                record['fault_injection']=label;record['answer']=copy.deepcopy(answer)
                (self.base.log_dir/(cid+'.json')).write_text(json.dumps(record,indent=2))
        return answer,record


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',default='gateway-live');p.add_argument('--port',type=int,default=11434)
    p.add_argument('--model',default='local-assistant:latest');args=p.parse_args()
    out=Path(args.out).resolve()
    if out.exists():raise ValueError('Use a fresh output directory')
    out.mkdir();(out/'cases').mkdir()
    cases=['G01-parser-bootstrap','G02-parser-repair','G03-parser-exhaustion',
           'G04-selector-repair','G05-selector-exhaustion','G06-shared-call-budget','G07-quarantine-across-apis']
    (out/'plan.json').write_text(json.dumps({'cases':cases,'model':args.model,'fault_injection':'Explicit test adapters; original API outputs retained','fresh_calls':True},indent=2))
    (out/'source-hashes.json').write_text(json.dumps({str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in list(ROOT.glob('*.py')) + list((ROOT/'stp_canonical').glob('*.py'))},indent=2))
    (out/'before.json').write_text(json.dumps(metadata(args.port,args.model),indent=2))
    health=HealthMonitor(ROOT);time.sleep(2);model=LocalModel(out/'calls',args.port,args.model,health)
    rows=[]
    try:
        for name in cases:
            directory=out/'cases'/name;db=directory/'state.sqlite'
            if name[:3] in ('G01','G02','G03'):
                directory.mkdir();s=InvocationLedger(db);s.create('active')
                provider=model if name.startswith('G01') else WireFault(model,'parser',always=name.startswith('G03'))
                r=ingest(s,'active','No network access.',provider,name)
                checks={'expected_commit':r['committed'] != name.startswith('G03'),
                        'bounded':len(r['calls'])==(1 if name.startswith('G01') else 2),
                        'permission_preserved':s.snapshot('active')['constraints'].get('network_allowed') is (None if name.startswith('G03') else False)}
                s.close()
            elif name.startswith('G07'):
                definition=copy.deepcopy(definitions()[0]);definition.update(id=name,initial=POLICY_REQUEST,expected=copy.deepcopy(POLICY_CONTRACT))
                with patch('experiment.ingest',host_seed):c=build(definition,directory,None)
                def dispatch(suffix,parents=()):
                    return run_paged(db,'active',c['request'],c['required_facts'],model,name+suffix,
                        initial_resident=['src/policy.py','tests/test_policy.py'],checkpoint_ids=parents)
                parent=dispatch('-parent');child=dispatch('-child',[parent['checkpoint_id']]);independent=dispatch('-independent')
                s=InvocationLedger(db)
                checks={'all_initially_admitted':all(r['admission']['admitted'] for r in (parent,child,independent))}
                s.quarantine('active',parent['checkpoint_id'],'live cross-API revocation')
                for label,r in [('parent',parent),('child',child)]:
                    try:s.load_result('active',r['invocation_id']);checks[label+'_call_read_blocked']=False
                    except PageFault:checks[label+'_call_read_blocked']=True
                s.quarantine_invocation('active',independent['invocation_id'],'live invocation revocation')
                try:s.load_checkpoint('active',independent['checkpoint_id']);checks['checkpoint_read_blocked']=False
                except PageFault:checks['checkpoint_read_blocked']=True
                checks['source_preserved']=s.read_page('active','src/policy.py').authority=='file';s.close()
                calls=[]
                for r in (parent,child,independent):
                    raw=r.pop('calls');calls.extend(raw);r['call_ids']=[c['call_id'] for c in raw]
                r={'runs':[parent,child,independent],'calls':calls}
            else:
                definition=copy.deepcopy(definitions()[0]);definition.update(id=name,initial=POLICY_REQUEST,expected=copy.deepcopy(POLICY_CONTRACT))
                with patch('experiment.ingest',host_seed):c=build(definition,directory,None)
                provider=WireFault(model,'selector',always=name.startswith('G05')) if not name.startswith('G06') else model
                r=run_paged(db,'active',c['request'],c['required_facts'],provider,name,max_calls=1 if name.startswith('G06') else 12)
                if name.startswith('G04'):
                    checks={'repaired':r['protocol']['state']=='ALLOW','bad_first_attempt_rejected':not r['attempts'][0]['admission']['admitted']}
                elif name.startswith('G05'):
                    checks={'bounded':len(r['calls'])==3,'no_release':r['released_answer'] is None,'fin':r['protocol']['state']=='FIN'}
                else:checks={'shared_budget':len(r['calls'])==1,'no_release':r['released_answer'] is None,'fin':r['protocol']['state']=='FIN',
                             'compound_fin_recorded':any('COMPOUND_FIN' in e for e in r['admission']['errors'])}
            s=InvocationLedger(db);checks['ledger_valid']=s.verify_invocations('active')
            for iid,raw in s.db.execute('SELECT invocation_id,record_json FROM invocation_receipts'):
                if json.loads(raw)['status']=='quarantined':
                    try:s.load_result('active',iid);checks['quarantine_'+iid]=False
                    except Exception:checks['quarantine_'+iid]=True
            s.close()
            r['call_ids']=[c['call_id'] for c in r.pop('calls')]
            row={'case':name,'passed':all(checks.values()),'checks':checks,'result':r};rows.append(row)
            (out/'results.json').write_text(json.dumps(rows,indent=2))
            print(json.dumps({'case':name,'passed':row['passed'],'failed':[k for k,v in checks.items() if not v]}),flush=True)
            if health.problem():raise RuntimeError(health.problem())
    finally:
        health.close();shutil.copy2(ROOT/'health.jsonl',out/'health.jsonl')
        (out/'after.json').write_text(json.dumps(metadata(args.port,args.model),indent=2))
    print(json.dumps({'complete':True,'cases':len(rows),'passed':sum(r['passed'] for r in rows)}),flush=True)


if __name__=='__main__':main()
