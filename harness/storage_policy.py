"""Bounded disposable diagnostics; immutable evidence is never silently pruned."""
import json
import math
import os
from pathlib import Path
import shutil
import time
from context_manager import InvalidUpdate, canonical, sha
from karma_adapter import KarmaAdapter
from memory_layer import LIMITS

TRACE_BYTES=LIMITS['trace_bytes']
SNAPSHOT_BYTES=LIMITS['settled_snapshot_bytes']
MAX_AGE=LIMITS['max_age_seconds']

def size(path):
    path=Path(path)
    if path.is_symlink():return 0
    if path.is_file():return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob('*') if p.is_file() and not p.is_symlink()) if path.exists() else 0

def usage(root, reserve=0):
    limit=int(os.environ.get('KARMA_STORAGE_LIMIT_MIB','512'))*1024**2
    if not 64*1024**2<=limit<=8192*1024**2:raise InvalidUpdate('Storage allocation must be 64..8192 MiB')
    used=size(root);decision=KarmaAdapter().storage(used,limit,reserve)
    return {'used_bytes':used,'limit_bytes':limit,'reserved_next_bytes':reserve,'decision':decision['decision'],'adapter':'karma','profile':decision['profile'],'profile_version':decision['profile_version'],
            'trace_limit_bytes':TRACE_BYTES,'snapshot_limit_bytes':SNAPSHOT_BYTES,'retention_days':7,
            'ledger_policy':'preserve immutable evidence; stop new work before allocation is exhausted'}

def require_space(root, reserve=16*1024**2):
    frame=usage(root,reserve)
    if frame['decision']!='ALLOW':raise InvalidUpdate('STORAGE_BUDGET '+frame['decision']+': archive workspace history or increase Karma storageLimitMiB; evidence was preserved')
    return frame

def prune(root, s=None, now=None):
    root=Path(root);now=time.time() if now is None else now;removed=[]
    groups=[(sorted((root/'calls').glob('*.json'),key=lambda p:p.stat().st_mtime),TRACE_BYTES)]
    if s is not None:
        eligible=[]
        for aid,raw in s.db.execute('SELECT action_id,record_json FROM host_action_receipts'):
            if json.loads(raw)['result']['status'] in ('pending','interrupted_unknown'):continue
            path=root/'actions'/aid
            if path.is_dir() and not path.is_symlink():eligible.append(path)
        groups.append((sorted(eligible,key=lambda p:p.stat().st_mtime),SNAPSHOT_BYTES))
    for paths,cap in groups:
        total=sum(size(p) for p in paths)
        for p in paths:
            if p.is_symlink():continue
            length=size(p)
            if now-p.stat().st_mtime<=MAX_AGE and total<=cap:continue
            if p.is_dir():shutil.rmtree(p)
            else:p.unlink()
            removed.append(str(p.relative_to(root)));total-=length
    if removed and s is not None:
        with s.db:s._audit('DIAGNOSTIC_RETENTION',{'removed':removed,'immutable_ledger_preserved':True})
    return removed

def write_trace(directory, call_id, record):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    response=record.get('response',{})
    compact={'call_id':call_id,'ok':record.get('ok',False),'elapsed_s':record.get('elapsed_s'),
             'error':record.get('error'),'request_hash':sha(canonical(record.get('request',{}))),
             'conservative_input_charge':record.get('conservative_input_charge'),
             'response':{k:response[k] for k in ('model','done','done_reason','load_duration','prompt_eval_count','eval_count','total_duration') if k in response},
             'privacy':'Prompt, source excerpts and response bodies omitted; committed deliveries remain in the ledger'}
    if record.get('provider') in ('strata', 'sutra_engine'):
        compact['provider'] = record['provider']
        request = record.get('request') or {}
        compact['generation_limits'] = {
            key: request[key] for key in ('max_tokens', 'reasoning_budget_tokens')
            if type(request.get(key)) is int and 0 < request[key] <= 65536}
        compact['response']['first_answer_chunk_s'] = response.get('first_answer_chunk_s')
        timings = response.get('timings')
        if isinstance(timings, dict):
            compact['response']['timings'] = {key: timings[key] for key in
                ('cache_n', 'prompt_n', 'prompt_ms', 'prompt_per_second',
                 'predicted_n', 'predicted_ms', 'predicted_per_second')
                if type(timings.get(key)) in (int, float) and 0 <= timings[key] < 2 ** 53 and math.isfinite(timings[key])}
    compatibility = record.get('decoder_compatibility')
    if isinstance(compatibility, dict):
        compact['decoder_compatibility'] = {k: compatibility[k] for k in
            ('policy', 'runner', 'runtime_version') if k in compatibility and
            (compatibility[k] is None or isinstance(compatibility[k], str) and len(compatibility[k]) <= 128)}
    filename=sha(str(call_id)) if Path(str(call_id)).name!=str(call_id) else str(call_id)
    (directory/(filename+'.json')).write_text(json.dumps(compact,indent=2))
    prune(directory.parent)
