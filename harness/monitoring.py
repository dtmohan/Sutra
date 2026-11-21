"""Measured hardware signals and read-only diagnostic snapshots."""
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from context_manager import ContextStore, InvalidUpdate, canonical, sha
from karma_adapter import KarmaAdapter, StaleHardwareSample


def _collect_hardware(max_wait=2):
    """Use the same installed collectors as the VS Code host in one-shot mode."""
    harness = Path(__file__).resolve().parent
    if sys.platform == 'darwin' and platform.machine() == 'arm64':
        command = [str(harness.parent / 'bin' / 'telemetry'), '--once']
        collector = 'native-telemetry'
    else:
        node = os.environ.get('KARMA_NODE') or shutil.which('node')
        if not node or not Path(node).is_absolute():
            raise ValueError('Portable telemetry requires an installed Node executable')
        command = [node, str(harness / 'telemetry_portable.js'), '--once']
        collector = 'portable-telemetry'
    from lifecycle import timeout
    result = subprocess.run(command, check=True, capture_output=True, encoding='utf-8',
                            timeout=timeout(min(2, max_wait)), env=dict(os.environ, ELECTRON_RUN_AS_NODE='1'))
    return json.loads(result.stdout), collector


def _unavailable(reason):
    return {'profile':'hardware-v1','available':False,'decision':None,'admitted':False,'reason':str(reason)}


def _on_demand_state(max_wait):
    try:
        sample, collector = _collect_hardware(max_wait)
        return dict(KarmaAdapter().hardware(sample, time.time()),
                    sample_source='host_on_demand', collector=collector)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        return _unavailable(f'Local telemetry refresh failed: {error}')


def resource_state(root, now=None, *, refresh=False, newer_than=None, max_wait=2):
    """Read observed state. Admission may request a fresh local measurement."""
    refresh = refresh and now is None
    observed = time.time() if now is None else now
    states, missing_or_stale, invalid = [], [], []
    for file, source, private in ((Path(root)/'telemetry.json', 'telemetry_file', False),
                                  (Path(root)/'coordinator'/'telemetry.json', 'coordinator_telemetry_file', True)):
        try:
            if private:
                directory = file.parent.lstat()
                if (not stat.S_ISDIR(directory.st_mode) or hasattr(os, 'getuid') and directory.st_uid != os.getuid()
                        or os.name != 'nt' and directory.st_mode & 0o077):
                    raise ValueError('Coordinator telemetry directory is not private')
            fd = os.open(file, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536
                        or private and (hasattr(os, 'getuid') and info.st_uid != os.getuid()
                                        or os.name != 'nt' and info.st_mode & 0o077)):
                    raise ValueError('Telemetry is not a bounded regular measurement file')
                raw = os.read(fd, 65537)
                if len(raw) > 65536: raise ValueError('Telemetry exceeds its measurement allocation')
            finally:
                os.close(fd)
            sample = json.loads(raw)
            states.append(dict(KarmaAdapter().hardware(sample, observed), sample_source=source))
        except (FileNotFoundError, StaleHardwareSample) as exc:
            missing_or_stale.append(exc)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            invalid.append(exc)
    if not states:
        # Preserve fail-closed handling of malformed observations. A missing or
        # expired stream can be refreshed; an invalid one is not healthy evidence.
        if invalid: return _unavailable(invalid[0])
        reason = missing_or_stale[0] if missing_or_stale else 'No local telemetry observation'
        if refresh:
            fresh = _on_demand_state(max_wait)
            return fresh if fresh['available'] else _unavailable(f'{reason}. {fresh["reason"]}')
        return _unavailable(reason)
    # Fresh pressure wins over a simultaneous healthy reading. Otherwise use
    # the newest validated measurement, regardless of which process sampled it.
    priority = {'ALLOW': 0, 'BRIDGE': 1, 'FIN': 2, 'COMPOUND_FIN': 3}
    state = max(states, key=lambda value: (value['sample']['wall_time'], priority.get(value['decision'], 4)))
    sample = state['sample']
    if refresh and state['admitted'] and newer_than is not None and sample['wall_time'] <= newer_than:
        fresh = _on_demand_state(max_wait)
        if fresh['available']:return fresh
        # A streaming collector can still provide the next sample. Recovery
        # will wait for and verify its timestamp before admitting work.
    return state


def wait_category(state):
    explicit = state.get('blocker')
    if explicit in ('runtime_busy', 'runtime_timeout', 'runtime_uncertain', 'runtime_starting',
                    'model_admission', 'measurement_unavailable', 'hardware_pressure'):
        return explicit
    runtime=state.get('runtime') or {}
    if runtime.get('kind')=='model_capacity':
        return 'model_admission'
    if runtime.get('kind')=='busy':
        return 'runtime_busy'
    if runtime.get('kind') in ('identity_changed', 'interrupted'):
        return 'runtime_uncertain'
    return 'hardware_pressure' if state.get('available') and state.get('sample') else 'measurement_unavailable'


class ResourceWait(InvalidUpdate):
    """A measured, recoverable admission stop, not a provider failure."""
    def __init__(self, state):
        self.state = state
        self.category = wait_category(state)
        details = [state.get('reason', 'Resource measurement unavailable')]
        if state.get('profile') == 'remote-nvidia-v1':
            details.append('remote GPU admission: '+str(state.get('decision') or 'unavailable'))
        elif state.get('available'):
            details.append('OS thermal pressure: '+str(state.get('thermal_label', 'unknown')))
            pressure = (state.get('sample') or {}).get('memory_pressure_raw')
            details.append('memory pressure: '+{1:'normal',2:'warning',4:'critical'}.get(pressure, 'unavailable'))
            if state.get('reclaimable_estimate_gib') is not None:
                details.append(str(state['reclaimable_estimate_gib'])+' GiB available estimate')
            details.append('kernel: '+str(state.get('decision')))
        super().__init__('RESOURCE_WAIT: '+'; '.join(details))


def require_resources(root, *, newer_than=None):
    state=resource_state(root, refresh=True, newer_than=newer_than)
    if not state['admitted']:raise ResourceWait(state)
    return state



class DiagnosticLedger:
    """Query-only view with the history/proof methods budget readers require.

    Never initialize the mutable ledger, install triggers, repair records or
    promote a local seal to authority while rendering Monitor. The caller owns
    one read transaction so all sections describe the same ledger snapshot.
    """
    def __init__(self, db):
        self.db = db
        self._histories = {}
        self._audit_checked = None

    def _contract_history(self, session):
        if session not in self._histories:
            from governance import GovernedContext
            if not ContextStore.validate_chain(self, session):
                raise InvalidUpdate('Task event chain is corrupt')
            history = GovernedContext._contract_history(self, session)
            rows = self.db.execute('SELECT record_json,digest FROM contract_epochs WHERE session=? ORDER BY epoch',
                                   (session,)).fetchall()
            if rows != [(canonical(record), digest) for record, digest in history]:
                raise InvalidUpdate('Contract epoch commitment mismatch')
            self._histories[session] = history
        return self._histories[session]

    def origin(self, session):
        history = self._contract_history(session)
        if not history:
            raise InvalidUpdate('No user contract established')
        record, digest = history[-1]
        return dict(record, contract_hash=digest)

    def snapshot(self, session):
        return self.origin(session)['contract']

    def audit_valid(self, deep=False):
        if self._audit_checked is None or deep:
            previous, expected = 'GENESIS', 1
            for seq, event, raw, parent, digest in self.db.execute('SELECT * FROM page_audit ORDER BY seq'):
                if seq != expected or parent != previous or sha(canonical([seq,event,raw,parent])) != digest:
                    self._audit_checked = False
                    return False
                expected, previous = expected + 1, digest
            self._audit_checked = True
        return self._audit_checked


def _diagnostic_error(data, section, error):
    data['diagnostic_errors'].append({'section': section, 'message': str(error)[:1000]})


def dashboard(root):
    root=Path(root);data={'time':time.time(),'hardware':resource_state(root),'runs':[],
                        'invocations':{'total':0,'accepted':0,'quarantined':0,'pending':0},'actions':[],
                        'adapters':{'model':'local Ollama / fresh messages','edit':'exact replacement + file-version check',
                                    'test':'Sandboxed process observation only; assertions unverified','network':'test process always denied'},
                        'kernel':{'name':'Smriti four-decision','order':['COMPOUND_FIN','ALLOW','BRIDGE','FIN'],
                                  'sha256':hashlib.sha256((Path(__file__).parent/'stp_canonical/kernel.py').read_bytes()).hexdigest()},
                        'context':None,'token_usage':{'calls':[],'total_calls':0,'limit':250},'diagnostic_errors':[],
                        'scope':'This workspace and Karma calls only'}
    from runtime_memory import snapshot
    try:data['runtime_memory']=snapshot()
    except Exception as exc:_diagnostic_error(data,'runtime_memory',exc)
    from remote_memory import snapshot as remote_snapshot
    try:data['remote_memory']=remote_snapshot(root)
    except Exception as exc:_diagnostic_error(data,'remote_memory',exc)
    from storage_policy import usage
    try:data['storage']=usage(root)
    except Exception as exc:_diagnostic_error(data,'storage',exc)
    path=root/'state.sqlite'
    if not path.exists():return data
    from ledger_sqlite import read_connection
    try:db=read_connection(path, timeout=.2)
    except Exception as exc:
        _diagnostic_error(data,'ledger',exc)
        return data
    try:
        db.execute('BEGIN')
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
        from coherence_observations import latest as latest_coherence
        try:data['coherence_observations']=latest_coherence(SimpleNamespace(db=db))
        except Exception as exc:_diagnostic_error(data,'coherence_observations',exc)
        from child_checkpoint_progress import latest as latest_review_progress
        try:data['review_progress_observations']=latest_review_progress(db)
        except Exception as exc:_diagnostic_error(data,'review_progress_observations',exc)
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
                ledger=DiagnosticLedger(db)
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
            limit=(data.get('review_budget') or {}).get('limits',{}).get('operations') or (json.loads(contract[0])['contract']['constraints'].get('max_tools') if contract else None)
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
            recorded=json.loads(receipt[0]) if receipt else {}
            tokens=recorded.get('token_usage',{})
            data['context'].update(model_packet=env.get('model_interface'),accounting_settlement=recorded.get('accounting_settlement'))
            data['context'].update(output_reserve=env.get('budget_profile',{}).get('response_reserve',1024),limit=env.get('budget_profile',{}).get('context_limit',8192),actual_prompt_tokens=tokens.get('input_tokens'),output_tokens=tokens.get('output_tokens'))
            profile = env.get('budget_profile', {})
            data['context'].update(context_profile=profile.get('context_profile', 1),
                                   reservation_limit=profile.get('reservation_limit'))
            physical = db.execute("""SELECT record_json FROM invocations WHERE session=?
                AND json_extract(record_json,'$.physical_context') IS NOT NULL
                ORDER BY rowid DESC LIMIT 1""", (session,)).fetchone()
            if physical:
                measured_env = json.loads(physical[0]); measured = measured_env['physical_context']
                data['context']['latest_physical_window'] = {
                    'model': measured_env['model'], 'role': measured_env['role'],
                    'prompt_tokens': measured['prompt_tokens'], 'output_reserve': measured['output_reserve'],
                    'protocol_margin': measured.get('context_slack_tokens', 0),
                    'context_tokens': measured['context_tokens'], 'basis': measured['basis'],
                    'decision': measured['capacity']['decision']}
        data['adapter_health']='busy' if any(r['state'] not in ('ALLOW','FIN','RESOURCE_WAIT') for r in data['runs'][:1]) else 'idle'
        if data.get('repository_pending'):data['adapter_health']='repository operation pending; inspection required'
        if any(a['result']['status']=='pending' for a in data['actions']):data['adapter_health']='action running or interrupted; receipt pending'
        data['recent_gate_events']=[{'event':event,'data':json.loads(raw)} for event,raw in db.execute("SELECT event,payload_json FROM page_audit WHERE event IN ('RESOURCE_GATE','ACTION_PLAN_DENIED','ACTION_DENIED','INTERRUPTION_RECONCILED') ORDER BY seq DESC LIMIT 5")]
        return data
    except Exception as exc:
        # Task diagnostics are not a dependency of hardware telemetry. Return
        # the measured sections and expose the failed read; never infer ALLOW.
        _diagnostic_error(data,'ledger',exc)
        data['adapter_health']='Some task diagnostics are unavailable'
        return data
    finally:db.close()
