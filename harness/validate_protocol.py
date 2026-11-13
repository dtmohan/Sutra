"""Live protocol scenarios. Fault injection is explicitly logged, never hidden."""
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
from governance import GovernedContext
from health import HealthMonitor
from local_model import LocalModel
from paging import PageFault
from validate_dag import build_new, new_definitions, metadata

ROOT = Path(__file__).resolve().parent


class FaultAdapter:
    def __init__(self, model, *, bad='none', after=None):
        self.base = model; self.budget = model.budget; self.model = model.model
        self.bad = bad; self.after = after; self.count = 0
    def call(self, call_id, system, payload, schema):
        answer, record = self.base.call(call_id, system, payload, schema)
        self.count += 1
        if self.bad == 'always' or (self.bad == 'first' and self.count == 1):
            original = copy.deepcopy(answer); answer = copy.deepcopy(answer); answer['interpretation'] = 'DENY'
            record['model_answer_before_injection'] = original
            record['fault_injection'] = 'test adapter replaces interpretation with DENY before host validation'
            record['answer'] = copy.deepcopy(answer)
            (self.base.log_dir / (call_id + '.json')).write_text(json.dumps(record, indent=2))
        if self.after and self.count == 1: self.after()
        return answer, record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default='protocol-validation')
    parser.add_argument('--port', type=int, default=11434)
    parser.add_argument('--model', default='local-assistant:latest')
    args = parser.parse_args(); out = Path(args.out).resolve()
    if out.exists(): raise ValueError('Choose a fresh output directory')
    out.mkdir(); (out/'cases').mkdir()
    plan = ['P01-clean-receipts','P02-one-bad-response-repair','P03-repair-exhaustion',
            'P04-contract-amendment','P05-missing-prerequisites','P06-handoff-and-ancestor-quarantine',
            'P07-rejected-checkpoint-reentry','P08-source-revision']
    (out/'plan.json').write_text(json.dumps({'cases':plan,'model':args.model,'max_repairs':2,
        'context_limit':8192,'fixture_contract':'host-seeded to isolate protocol; natural-language ingestion covered in regression suite',
        'fault_injection':'P02 first response and P03 all responses use labelled DENY injection; original model output retained',
        'no_answer_feedback':True},indent=2))
    (out/'source-hashes.json').write_text(json.dumps({str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in list(ROOT.glob('*.py')) + list((ROOT/'stp_canonical').glob('*.py'))},indent=2))
    (out/'before.json').write_text(json.dumps(metadata(args.port,args.model),indent=2))
    monitor = HealthMonitor(ROOT); time.sleep(2)
    model = LocalModel(out/'calls', port=args.port, model=args.model, health=monitor)
    rows = []
    try:
        for name in plan:
            definition = copy.deepcopy(next(d for d in new_definitions() if d['id']=='N06-permitted-branch'))
            definition['id'] = name
            with patch('experiment.ingest', host_seed): case = build_new(definition, out/'cases'/name, None)
            checks, results = {}, []
            def dispatch(adapter=model, suffix='', **kwargs):
                r = run_paged(case['db'],case['session'],case['request'],case['required_facts'],adapter,
                    name+suffix,initial_resident=case['seed_pages'],policy_request=case['policy_request'],**kwargs)
                r['call_ids'] = [c['call_id'] for c in r.pop('calls')]; results.append(r)
                return r
            if name.startswith('P02'):
                r = dispatch(FaultAdapter(model,bad='first'))
                checks = {'repaired':r['protocol']['state']=='ALLOW' and 2<=len(r['attempts'])<=3,
                          'first_rejected':not r['attempts'][0]['admission']['admitted'],
                          'correct_release':r['released_answer'] is not None and r['released_answer']['interpretation']=='ALLOW'}
            elif name.startswith('P03'):
                r = dispatch(FaultAdapter(model,bad='always'))
                checks = {'bounded':len(r['call_ids'])==3 and len(r['attempts'])==3,
                          'exhausted':r['protocol']['reason']=='repair_exhausted', 'no_release':r['released_answer'] is None}
            elif name.startswith('P04'):
                def amend():
                    s=GovernedContext(case['db']); text='Limit attempts to 4.'
                    s.apply_user_patch(case['session'],'authorized-amendment',text,[{'key':'max_attempts','value':'4','quote':text}],s.snapshot(case['session'])['revision']);s.close()
                old=dispatch(FaultAdapter(model,after=amend),'-old')
                new=dispatch(model,'-new')
                checks={'old_not_released':old['released_answer'] is None,
                        'new_epoch':new['protocol']['epoch']==old['protocol']['epoch']+1,
                        'new_accepted':new['protocol']['state']=='ALLOW',
                        'new_constraint':{'key':'max_attempts','value':'4'} in new['answer']['constraints']}
            elif name.startswith('P05'):
                r=dispatch(expand=False)
                checks={'no_retry':len(r['attempts'])==1,'fin':r['protocol']['state']=='FIN','no_release':r['released_answer'] is None}
            elif name.startswith('P06'):
                parent=dispatch(model,'-parent'); child=dispatch(model,'-child',checkpoint_ids=[parent['checkpoint_id']])
                s=GovernedContext(case['db'])
                s.quarantine(case['session'],parent['checkpoint_id'],'test revocation of derived checkpoint')
                checks={'handoff_accepted':child['protocol']['state']=='ALLOW',
                        'child_quarantined':s.checkpoint_status(case['session'],child['checkpoint_id'])=='quarantined_ancestor',
                        'source_preserved':s.read_page(case['session'],'rule-0').authority=='user'}
                key='checkpoint-'+parent['checkpoint_id']
                checks['checkpoint_delivered']=any(key in t['metadata']['resident'] for t in child['turns'] if t['stage']=='worker')
                s.close()
            elif name.startswith('P07'):
                r=dispatch(FaultAdapter(model,bad='always'),max_repairs=0)
                before=len(list((out/'calls').glob('*.json')))
                try: dispatch(model,'-reentry',checkpoint_ids=[r['checkpoint_id']]); rejected=False
                except PageFault: rejected=True
                checks={'reentry_blocked':rejected,'no_extra_inference':before==len(list((out/'calls').glob('*.json')))}
            elif name.startswith('P08'):
                r=dispatch();s=GovernedContext(case['db']);doc=s.document(case['session'],'policy-source')
                s.put_document(case['session'],'policy-source',doc['text']+'\nRevision.',source={'fixture':'source-update'},expected_version=1)
                checks={'checkpoint_invalidated':s.checkpoint_status(case['session'],r['checkpoint_id']).startswith('quarantined'),
                        'old_source_retained':s.document(case['session'],'policy-source',1)['text']==doc['text']};s.close()
            else:
                r=dispatch();checks={'accepted':r['protocol']['state']=='ALLOW','receipts':len(r['receipt_ids'])==3}
            s=GovernedContext(case['db'])
            checks['ledger_valid']=s.verify_protocol(case['session'])
            for result in results:
                for attempt in result['attempts']:
                    if not attempt['admission']['admitted'] and attempt.get('checkpoint_id'):
                        checks['rejected_quarantined_'+attempt['checkpoint_id']]=s.checkpoint_status(case['session'],attempt['checkpoint_id']).startswith('quarantined')
            s.close()
            rows.append({'case':name,'checks':checks,'passed':all(checks.values()),'results':results})
            (out/'results.json').write_text(json.dumps(rows,indent=2))
            print(json.dumps({'case':name,'passed':all(checks.values()),'failed':[k for k,v in checks.items() if not v]}),flush=True)
            if monitor.problem(): raise RuntimeError(monitor.problem())
    finally:
        monitor.close();shutil.copy2(ROOT/'health.jsonl',out/'health.jsonl')
        (out/'after.json').write_text(json.dumps(metadata(args.port,args.model),indent=2))
    print(json.dumps({'complete':True,'cases':len(rows),'passed':sum(r['passed'] for r in rows)}),flush=True)


if __name__=='__main__':main()
