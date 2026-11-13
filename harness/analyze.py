"""Offline audit of the frozen comparison. No generation or state repair."""
import argparse,collections,hashlib,json,pathlib,statistics
from context_manager import canonical
from paging import PagedContext

def main():
    ap=argparse.ArgumentParser();ap.add_argument('directory',nargs='?',default=str(pathlib.Path(__file__).resolve().parent/'experiment'))
    args=ap.parse_args();root=pathlib.Path(args.directory)
    results=json.loads((root/'results.json').read_text());plan=json.loads((root/'plan.json').read_text())
    cases={p.parent.name:json.loads(p.read_text()) for p in (root/'cases').glob('*/case.json')}
    calls={p.stem:json.loads(p.read_text()) for p in (root/'calls').glob('*.json')}
    details=[];receipt_count=0
    for r in results:
        case=cases[r['case']]; records=[calls[c] for c in r['call_ids']]
        # A failed HTTP/generation attempt is still a real model call even if
        # the runner returned no successfully delivered record to the condition.
        prefix=r['case']+'-'+r['mode']
        attempts=[c for cid,c in calls.items() if cid==prefix or cid.startswith(prefix+'-')]
        item={k:v for k,v in r.items() if k!='turns'}
        want=case['expected']['facts']; got={f['key']:f['value'] for f in r['answer'].get('facts',[])}
        item['requested_facts_present']=all(got.get(k)==v for k,v in want.items()) if want else not got
        if r.get('blocked'): item['requested_facts_present']=False
        db=PagedContext(root/'cases'/r['case']/'state.sqlite')
        assert db.validate_chain(case['session']) and db.audit_valid()
        delivered=[]
        for turn,record in zip(r['turns'],records):
            rid=turn['delivery_id']
            row=db.db.execute('SELECT payload_json,payload_hash,refs_json FROM deliveries WHERE id=?',(rid,)).fetchone()
            assert row and db.db.execute('SELECT 1 FROM delivery_success WHERE id=?',(rid,)).fetchone()
            actual=record['request']['messages'][1]['content']
            assert canonical(json.loads(actual))==row[0] and hashlib.sha256(row[0].encode()).hexdigest()==row[1]
            delivered.extend(json.loads(row[2])); receipt_count+=1
        db.close()
        item['has_positive_fact_request']=bool(want)
        grounded=True
        final_pages={p['id']:p for p in json.loads(records[-1]['request']['messages'][1]['content']).get('pages',[])} if records else {}
        item['required_page_evidence_delivered']=bool(want) and not r.get('blocked') and all(
            any(p['authority']!='model' and p['status'] in ('current','aging') and p.get('facts',{}).get(key)==value
                for p in final_pages.values()) for key,value in want.items())
        for key,value in want.items():
            matches=[f for f in r['answer'].get('facts',[]) if f['key']==key and f['value']==value]
            if not any(f['page_id'] in final_pages and final_pages[f['page_id']]['authority']!='model'
                       and final_pages[f['page_id']].get('facts',{}).get(key)==value
                       and final_pages[f['page_id']]['status'] in ('current','aging') for f in matches): grounded=False
        item['grounded_requested_facts']=bool(want) and grounded and not r.get('blocked')
        item['delivered_page_versions']=sorted({(k,v) for k,v,_ in delivered})
        item['model_calls']=len(attempts)
        item['successful_calls']=len(records)
        item['failed_calls']=sum(not c.get('ok') for c in attempts)
        item['prompt_tokens']=sum(c.get('response',{}).get('prompt_eval_count',0) for c in attempts)
        item['output_tokens']=sum(c.get('response',{}).get('eval_count',0) for c in attempts)
        item['elapsed_s']=sum(c.get('elapsed_s',0) for c in attempts)
        item['strict_failures']=[k for k,v in r['score']['checks'].items() if not v]
        details.append(item)
    summary={}
    for mode in plan['modes']:
        rs=[r for r in details if r['mode']==mode]
        summary[mode]={'checkpoints':len(rs),'contract_preserved':sum(r['score']['contract_preserved'] for r in rs),
          'requested_facts_present':sum(r['requested_facts_present'] for r in rs),
          'positive_fact_cases':sum(r['has_positive_fact_request'] for r in rs),
          'required_page_evidence_delivered':sum(bool(r['required_page_evidence_delivered']) for r in rs),
          'grounded_requested_facts':sum(bool(r['grounded_requested_facts']) for r in rs),
          'strict_passed':sum(r['score']['passed'] for r in rs),
          'admitted':sum(r['admission']['admitted'] for r in rs),
          'admitted_but_strict_failed':sum(r['admission']['admitted'] and not r['score']['passed'] for r in rs),
          'blocked':sum(bool(r.get('blocked')) for r in rs),
          'model_calls':sum(r['model_calls'] for r in rs),
          'failed_calls':sum(r['failed_calls'] for r in rs),
          'clarify_responses':sum(r['answer'].get('action')=='clarify' for r in rs),
          'prompt_tokens':sum(r['prompt_tokens'] for r in rs),
          'output_tokens':sum(r['output_tokens'] for r in rs),
          'elapsed_s':sum(r['elapsed_s'] for r in rs)}
    for cid,c in calls.items():
        msgs=c['request']['messages'];assert len(msgs)==2 and [m['role'] for m in msgs]==['system','user']
        payload=json.loads(msgs[1]['content']); assert 'expected' not in payload
        if '-paging-' in cid: assert 'conversation' not in payload and 'history' not in payload
        if c.get('ok'): assert c['response']['prompt_eval_count']+1024<=8192
    ingestion=[c for k,c in calls.items() if '-ingest-' in k]
    health=[json.loads(line) for line in (root/'health.jsonl').read_text().splitlines()]
    totals={'modes':summary,'completed_conditions':len(results),'planned_conditions':len(plan['cases'])*len(plan['modes']),
            'ingested_contracts_matching_expectation':sum(
                c['stored_contract']['task_id']==c['expected']['task_id'] and
                str(c['stored_contract']['goal']).strip().rstrip('.')==c['expected']['goal'].strip().rstrip('.') and
                c['stored_contract']['constraints']==c['expected']['constraints'] and
                c['stored_contract']['selected_option']==c['expected']['selected_option'] for c in cases.values()),
            'ingestion':{'calls':len(ingestion),'prompt_tokens':sum(c.get('response',{}).get('prompt_eval_count',0) for c in ingestion),
                         'output_tokens':sum(c.get('response',{}).get('eval_count',0) for c in ingestion),
                         'elapsed_s':sum(c.get('elapsed_s',0) for c in ingestion)},
            'audit':{'call_logs':len(calls),'verified_delivery_receipts':receipt_count,'exactly_two_messages':True,
                     'no_history_in_paging_calls':True,'all_actual_prompts_plus_reserve_fit':True,
                     'responses_with_thinking_text':sum(bool(c.get('response',{}).get('message',{}).get('thinking')) for c in calls.values()),
                     'max_actual_prompt_tokens':max(c.get('response',{}).get('prompt_eval_count',0) for c in calls.values())},
            'health':{'samples':len(health),'min_free_gib':min(d['free_bytes'] for d in health)/1024**3,
                      'min_free_plus_inactive_gib':min(d['free_bytes']+d['inactive_bytes'] for d in health)/1024**3,
                      'max_thermal_state':max(d['thermal_state'] for d in health),
                      'max_memory_pressure':max(d['memory_pressure_raw'] for d in health),
                      'swapout_pages_delta':health[-1]['swapouts_pages']-health[0]['swapouts_pages']},
            'notes':['Contract score is exact identity, goal wording (ignoring final period), all constraints without extras, and selection.',
                     'Strict score also requires exactly the requested facts, expected next action, target, test status, and completion.',
                     'Requested-facts metric permits extra facts; admission still checks all returned structured facts against delivered current sources.',
                     'Admission governs provenance, versions and permitted operations; it is not a proof of the semantically best next action.',
                     'Large full-replay budget blocks are resource outcomes and must not be presented as model forgetting.',
                     'Failed generation attempts count toward calls, tokens and time; only confirmed deliveries count toward receipt audits.']}
    matched={r['case'] for r in details if r['mode']=='paging' and not r.get('blocked')} & {r['case'] for r in details if r['mode']=='full_replay' and not r.get('blocked')}
    totals['matched_dispatched_costs']={'cases':sorted(matched)}
    for mode in ('paging','full_replay'):
        rows=[r for r in details if r['mode']==mode and r['case'] in matched]
        totals['matched_dispatched_costs'][mode]={k:sum(r[k] for r in rows) for k in ('model_calls','prompt_tokens','output_tokens','elapsed_s')}
    (root/'analysis.json').write_text(json.dumps({'summary':totals,'details':details},indent=2))
    print(json.dumps(totals,indent=2))

if __name__=='__main__':main()
