"""Measured hardware signals and read-only diagnostic snapshots."""
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from context_manager import InvalidUpdate
from karma_adapter import KarmaAdapter


def resource_state(root, now=None):
    try:
        sample=json.loads((Path(root)/'telemetry.json').read_text())
        return KarmaAdapter().hardware(sample,time.time() if now is None else now)
    except (OSError,ValueError,KeyError,TypeError) as exc:
        return {'profile':'hardware-v1','available':False,'decision':None,'admitted':False,'reason':str(exc)}


class ResourceWait(InvalidUpdate):
    """A measured, recoverable admission stop, not a provider failure."""
    def __init__(self, state):
        self.state = state
        details = [state.get('reason', 'Resource measurement unavailable')]
        if state.get('available'):
            details.append('OS thermal pressure: '+str(state.get('thermal_label', 'unknown')))
            pressure = (state.get('sample') or {}).get('memory_pressure_raw')
            details.append('memory pressure: '+{1:'normal',2:'warning',4:'critical'}.get(pressure, 'unavailable'))
            if state.get('reclaimable_estimate_gib') is not None:
                details.append(str(state['reclaimable_estimate_gib'])+' GiB available estimate')
            details.append('kernel: '+str(state.get('decision')))
        super().__init__('RESOURCE_WAIT: '+'; '.join(details))


def require_resources(root):
    state=resource_state(root)
    if not state['admitted']:raise ResourceWait(state)
    return state


def dashboard(root):
    root=Path(root);data={'time':time.time(),'hardware':resource_state(root),'runs':[],
                        'invocations':{'total':0,'accepted':0,'quarantined':0,'pending':0},'actions':[],
                        'adapters':{'model':'local Ollama / fresh messages','edit':'exact replacement + file-version check',
                                    'test':'Sandboxed process observation only; assertions unverified','network':'test process always denied'},
                        'kernel':{'name':'Smriti four-decision','order':['COMPOUND_FIN','ALLOW','BRIDGE','FIN'],
                                  'sha256':hashlib.sha256((Path(__file__).parent/'stp_canonical/kernel.py').read_bytes()).hexdigest()},
                        'context':None,'token_usage':{'calls':[],'total_calls':0,'limit':250},'scope':'This workspace and Karma calls only'}
    from runtime_memory import snapshot
    data['runtime_memory']=snapshot()
    from storage_policy import usage
    data['storage']=usage(root)
    path=root/'state.sqlite'
    if not path.exists():return data
    db=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=.2)
    try:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        from memory_layer import snapshot as memory_snapshot
        data['logical_memory']=memory_snapshot(db)
        from token_usage import history
        data['token_usage']=history(db)
        from orchestration import overview as orchestration_overview
        data['orchestration']=orchestration_overview(db)
        from task_observations import latest
        from types import SimpleNamespace
        data['task_observations']=latest(SimpleNamespace(db=db))
        meta=json.loads((root/'workspace.json').read_text()) if (root/'workspace.json').exists() else {}
        session=meta.get('active');data['session']=session;data['model']=meta.get('model');data['checkpoint']=meta.get('checkpoint')
        from types import SimpleNamespace
        from calibration import overview
        data['calibration']=overview(SimpleNamespace(db=db),session)
        data['domain_adapters']={'smriti':['page freshness','context capacity','selected uncertain profiles'],'karma':['operation budgets','task allocations','hardware','storage','selected uncertain profiles']}
        data['static_map']={k:v for k,v in meta.get('static_map',{}).items() if k not in ('versions','covered_sites')}
        if session:
            from types import SimpleNamespace
            from review_budget import decision
            row=db.execute('SELECT record_json,digest FROM contract_epochs WHERE session=? ORDER BY epoch DESC LIMIT 1',(session,)).fetchone()
            if row:
                contract=json.loads(row[0])['contract'];ledger=SimpleNamespace(db=db,snapshot=lambda _:contract,origin=lambda _:{'contract_hash':row[1]})
                data['review_budget']=decision(ledger,session)
            if meta.get('auto'):
                data['automatic_review']={k:v for k,v in meta['auto'].items() if k not in ('pages','preferred')}
                data['automatic_review']['mapped_page_receipts']=len(meta['auto']['pages'])
        data['adapters']['static']='fresh compiler snapshot + source hashes; model commentary has no graph authority'
        data['adapters']['hardware_profile']=data['hardware']['profile']
        if not session:return data
        row=db.execute('SELECT epoch,digest FROM contract_epochs WHERE session=? ORDER BY epoch DESC LIMIT 1',(session,)).fetchone()
        if row:data['contract']={'epoch':row[0],'hash':row[1]}
        if 'invocations' in tables:
            data['invocations']['total']=db.execute('SELECT COUNT(*) FROM invocations WHERE session=?',(session,)).fetchone()[0]
            for raw, in db.execute('SELECT record_json FROM invocation_receipts WHERE session=?',(session,)):
                status=json.loads(raw)['status'];data['invocations'][status]+=1
            data['invocations']['pending']=data['invocations']['total']-data['invocations']['accepted']-data['invocations']['quarantined']
            last=db.execute('SELECT record_json FROM invocations WHERE session=? ORDER BY rowid DESC LIMIT 1',(session,)).fetchone()
            if last:
                env=json.loads(last[0]);data['context']={'conservative_input_charge':env['charge'],'output_reserve':1024,'limit':8192,
                    'last_kernel_budget':env.get('budget_kernel'),'role':env['role'],'model':env['model']}
        for rid,config in db.execute('SELECT id,config_json FROM protocol_runs WHERE session=? ORDER BY rowid DESC LIMIT 5',(session,)):
            events=[json.loads(r[0]) for r in db.execute('SELECT record_json FROM protocol_events WHERE run_id=? ORDER BY seq',(rid,))]
            c=json.loads(config);data['runs'].append({'id':rid,'state':events[-1]['kind'] if events else 'START',
                'calls_used':sum(e['kind']=='DISPATCH' for e in events),'call_limit':c['max_calls'],
                'attempts_used':sum(e['kind']=='ATTEMPT' for e in events),'attempt_limit':c['max_repairs']+1,'recent_events':events[-4:]})
        if 'host_actions' in tables:
            used=0;limit=None
            contract=db.execute('SELECT record_json,digest FROM contract_epochs WHERE session=? ORDER BY epoch DESC LIMIT 1',(session,)).fetchone()
            if contract:
                limit=json.loads(contract[0])['contract']['constraints'].get('max_actions')
                used=sum(json.loads(r[0])['contract_hash']==contract[1] for r in db.execute('SELECT record_json FROM host_actions WHERE session=?',(session,)))
            if limit:
                from karma_adapter import budget_decision
                data['action_budget']=budget_decision(used,limit)
            for aid,raw in db.execute('SELECT id,record_json FROM host_actions WHERE session=? ORDER BY rowid DESC LIMIT 8',(session,)):
                a=json.loads(raw);r=db.execute('SELECT record_json FROM host_action_receipts WHERE action_id=?',(aid,)).fetchone()
                result=json.loads(r[0])['result'] if r else {'status':'pending'}
                data['actions'].append({'id':aid,'kind':a['kind'],'target':a['target'],'kernel':a['budget_kernel'],'result':result})
        if 'repository_ops' in tables:
            contract=db.execute('SELECT record_json,digest FROM contract_epochs WHERE session=? ORDER BY epoch DESC LIMIT 1',(session,)).fetchone()
            limit=json.loads(contract[0])['contract']['constraints'].get('max_tools') if contract else None
            if limit:
                from karma_adapter import budget_decision
                operations=[json.loads(r[0]) for r in db.execute('SELECT record_json FROM repository_ops WHERE session=?',(session,))]
                data['repository']=budget_decision(sum(o['contract_hash']==contract[1] for o in operations),limit)
                data['repository_operations']=[{'kind':o['kind'],'target':o['target'],'query':o['query'],'id':o['id'],'kernel':o['kernel']} for o in operations[-8:]]
                data['repository_pending']=db.execute('SELECT COUNT(*) FROM repository_ops o LEFT JOIN repository_receipts r ON o.id=r.operation_id WHERE o.session=? AND r.id IS NULL',(session,)).fetchone()[0]
        if 'api_reviews' in tables:
            rows=[dict(json.loads(raw),id=rid) for rid,raw in db.execute('SELECT id,record_json FROM api_reviews WHERE session=? ORDER BY seq DESC LIMIT 8',(session,))]
            data['api_reviews']=[{'id':r['id'],'candidate_id':r['candidate_id'],'reviewer_id':r['reviewer_id'],'decision':r['decision']} for r in rows]
        if data['context'] and last:
            receipt=db.execute('SELECT record_json FROM invocation_receipts WHERE invocation_id=?',(env['id'],)).fetchone()
            tokens=json.loads(receipt[0]).get('token_usage',{}) if receipt else {}
            data['context'].update(output_reserve=env.get('budget_profile',{}).get('response_reserve',1024),limit=env.get('budget_profile',{}).get('context_limit',8192),actual_prompt_tokens=tokens.get('input_tokens'),output_tokens=tokens.get('output_tokens'))
        data['adapter_health']='busy' if any(r['state'] not in ('ALLOW','FIN','RESOURCE_WAIT') for r in data['runs'][:1]) else 'idle'
        if data.get('repository_pending'):data['adapter_health']='repository operation pending; inspection required'
        if any(a['result']['status']=='pending' for a in data['actions']):data['adapter_health']='action running or interrupted; receipt pending'
        data['recent_gate_events']=[{'event':event,'data':json.loads(raw)} for event,raw in db.execute("SELECT event,payload_json FROM page_audit WHERE event IN ('RESOURCE_GATE','ACTION_PLAN_DENIED','ACTION_DENIED','INTERRUPTION_RECONCILED') ORDER BY seq DESC LIMIT 5")]
        return data
    finally:db.close()
