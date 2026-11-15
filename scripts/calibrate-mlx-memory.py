"""Explicit, isolated calibration. Never changes user tasks or unloads other clients.

Example: python3 scripts/calibrate-mlx-memory.py --output /tmp/sutra-calibration
Profile JSON can be copied to <Sutra global storage>/runtime-memory/memory-profiles.
All calls use the invocation gateway. Fixtures contain no workspace/source data.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import socket
import subprocess
import sys
import threading
import time
import uuid

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'harness'))
from context_manager import canonical,sha
from conversation import open_session,contract
from invocation_gateway import InvocationGateway,InvocationLedger
from local_model import LocalModel
from paging import Budget
from monitoring import require_resources,resource_state
from lifecycle import deadline
import memory_probe as probe
import memory_profiles as profiles
import memory_observation as observation
import runtime_memory as memory


class ProbeGuard:
    active=None
    phase=None
    reason=None
    def problem(self):return self.reason
    def stop(self,reason):
        self.reason=reason
        connection=self.active
        if connection and connection.sock:
            try:connection.sock.shutdown(socket.SHUT_RDWR)
            except OSError:pass


def invoke(store,model,chars,seed,output,guard):
    memory.fresh_resources(output)
    if memory.running():raise RuntimeError('Local model is already resident; calibration deferred')
    rng=random.Random(seed)
    fixture=' '.join(str(rng.randrange(10)) for _ in range(chars//2))
    s=InvocationLedger(store)
    try:
        sid=open_session(s,probe.REQUEST)
        payload={'contract':contract(s,sid),'request':probe.REQUEST,'fixture':fixture,'model':model}
        provider=LocalModel(output/'calls',port=11434,model=model,budget=Budget(65536,64,256),thinking=False,health=guard)
        provider.memory_probe=True
        tags=memory.api('/api/tags')['models'];tag=next(t for t in tags if t['name']==model)
        provider.context_metadata=memory.api('/api/show',{'model':model});provider.memory_weights=tag['size']
        if provider.context_metadata.get('remote_model') or provider.context_metadata.get('remote_host') or 'cloud' in model.lower():
            raise ValueError('Calibration requires a local model')
        from model_context import allocation
        provider.budget=Budget(allocation(provider.context_metadata)[0].context_limit,64,256)
        run=s.begin_run(sid,probe.REQUEST,[],None,max_calls=1,max_repairs=0,max_seconds=240)
        s.reserve(run,'ATTEMPT',{'attempt':0})
        delivery=s.prepare_delivery(sid,payload,provider.budget.check(probe.SYSTEM,payload,probe.SCHEMA))
        with deadline():result=InvocationGateway(s.path,run,provider).invoke('memory_probe',uuid.uuid4().hex,probe.SYSTEM,payload,probe.SCHEMA,delivery)
        if not result['admission']['admitted']:raise RuntimeError(str(result['admission']))
        s.event(run,'ALLOW',{'reason':'synthetic_probe_only','invocation_id':result['invocation_id']})
        assert s.verify_invocations(sid)
        response=result['record']['response']
        return {'invocation_id':result['invocation_id'],'requested_context_tokens':provider.runtime_context_tokens,
                'prompt_tokens':response['prompt_eval_count'],'output_tokens':response['eval_count'],
                'measured_duration_s':result['record']['elapsed_s'],'receipt_verified':True,
                'fixture_sha256':sha(fixture),'fixture_characters':len(fixture)}
    finally:s.close()


def main(args):
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=False)
    os.environ.update(SUTRA_RUNTIME_DIR=str(output/'runtime'),SUTRA_MEMORY_OWNERSHIP='shared',SUTRA_THINKING='off',SUTRA_LOGIT_SENSOR='')
    telemetry=subprocess.Popen([str(ROOT/'bin/telemetry')],stdout=subprocess.PIPE,text=True)
    def collect():
        for line in telemetry.stdout:
            try:
                value=json.loads(line);p=output/'telemetry.tmp';p.write_text(json.dumps(value));p.replace(output/'telemetry.json')
            except (ValueError,OSError):pass
    thread=threading.Thread(target=collect,daemon=True);thread.start()
    report={'format':'mlx-calibration-observations-v1','complete':False,'models':{},'sampling_interval_s':.1,
            'metric':'sum of macOS proc_pid_rusage ri_phys_footprint for Ollama listener and descendants',
            'caveat':'Sampled process footprint is not a proven worst-case GPU allocation. Text-only cold prefill workloads.'}
    try:
        for _ in range(50):
            if resource_state(output)['admitted']:break
            time.sleep(.1)
        require_resources(output)
        if memory.running():raise RuntimeError('Local models already resident; calibration deferred')
        version=memory.api('/api/version')['version'];env=observation.environment(version);pid=env['server_epoch']['pid']
        report['environment']=env
        tags=memory.api('/api/tags')['models']
        for model in args.models:
            tag=next(t for t in tags if t['name']==model);meta=memory.api('/api/show',{'model':model})
            if (meta.get('details')or{}).get('runner')!='mlx' or meta.get('remote_model') or meta.get('remote_host'):raise ValueError('Choose an installed local MLX model')
            key=profiles.identity(model,tag,meta,env);groups=[];report['models'][model]={'identity':key,'groups':groups}
            for size in args.characters:
                group=[];groups.append(group)
                for repeat in range(args.repeats):
                    memory.fresh_resources(output)
                    if memory.running():raise RuntimeError('Unexpected resident model; calibration stopped')
                    baseline=observation.sample_tree(pid);baseline_system=resource_state(output)
                    if baseline['footprint_bytes']>max(1024**3,tag['size']//20):
                        raise RuntimeError('Backend still holds substantial memory; cold calibration deferred')
                    samples=[];stop=threading.Event();errors=[];contamination=[];guard=ProbeGuard()
                    def sample():
                        while not stop.is_set():
                            try:
                                v=observation.sample_tree(pid);v['at']=time.time();v['hardware']=resource_state(output);samples.append(v)
                                if not v['hardware']['admitted']:
                                    contamination.append('resource_pressure');guard.stop('Resource gate stopped calibration')
                            except Exception as exc:
                                errors.append(str(exc));guard.stop('Memory sampler unavailable')
                            stop.wait(.1)
                    def inventory():
                        # A slow inventory response must not stall footprint samples.
                        while not stop.wait(1):
                            try:
                                if any(r['name']!=model for r in memory.running()):
                                    contamination.append('other_resident_model');guard.stop('Another model became resident')
                            except Exception as exc:
                                errors.append(str(exc));guard.stop('Inventory observer unavailable')
                    sampler=threading.Thread(target=sample,daemon=True);sampler.start()
                    inventory_thread=threading.Thread(target=inventory,daemon=True);inventory_thread.start()
                    print(json.dumps({'event':'start','model':model,'fixture_characters':size,'repeat':repeat}),flush=True)
                    try:row=invoke(output/'state.sqlite',model,size,1000+size+repeat,output,guard)
                    except Exception as exc:
                        row={'error':str(exc),'prompt_tokens':0,'output_tokens':0,'requested_context_tokens':0}
                        contamination.append('incomplete_invocation')
                    finally:stop.set();sampler.join(timeout=6);inventory_thread.join(timeout=3)
                    end=resource_state(output);resident=memory.running()
                    if resident:contamination.append('model_remained_resident')
                    if errors or len(samples)<3 or sampler.is_alive() or inventory_thread.is_alive():contamination.append('missing_measurements')
                    gaps=[b['at']-a['at'] for a,b in zip(samples,samples[1:])]
                    if gaps and max(gaps)>1:contamination.append('sampling_gap')
                    current_env=observation.environment(memory.api('/api/version')['version'])
                    current_tag=next(t for t in memory.api('/api/tags')['models'] if t['name']==model)
                    current_meta=memory.api('/api/show',{'model':model})
                    if profiles.identity(model,current_tag,current_meta,current_env)!=key:contamination.append('model_or_backend_changed')
                    peak=max((s['footprint_bytes'] for s in samples),default=0)
                    row.update(baseline_bytes=baseline['footprint_bytes'],peak_footprint_bytes=peak,
                               increment_bytes=max(0,peak-baseline['footprint_bytes']),samples=len(samples),max_sample_gap_s=max(gaps,default=0),
                               baseline_system=baseline_system,end_system=end,contamination=sorted(set(contamination)),
                               eligible=not contamination,sampling_errors=errors)
                    group.append(row)
                    name=hashlib.sha256((model+str(size)+str(repeat)).encode()).hexdigest()[:16]+'.json'
                    (output/name).write_text(json.dumps(samples))
                    row['samples_file']=name;row['samples_sha256']=hashlib.sha256((output/name).read_bytes()).hexdigest()
                    (output/'Observations.json').write_text(json.dumps(report,indent=2)+'\n')
                    print(json.dumps({k:row[k] for k in ('prompt_tokens','requested_context_tokens','increment_bytes','eligible','contamination')}),flush=True)
                    if not row['eligible']:raise RuntimeError('Calibration interrupted: observation not eligible')
            evidence=sha(canonical(report['models'][model]))
            profile=profiles.build(key,groups,evidence)
            target=output/('profile-'+profile['sha256'][:16]+'.json');target.write_text(json.dumps(profile,indent=2)+'\n')
            print(json.dumps({'event':'profile','model':model,'file':str(target),'points':profile['points']}),flush=True)
        report['complete']=True
    except BaseException as exc:
        report['error']=type(exc).__name__+': '+str(exc);raise
    finally:
        telemetry.terminate();telemetry.wait(timeout=5);thread.join(timeout=1);telemetry.stdout.close()
        (output/'Observations.json').write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    p.add_argument('--models',nargs='+',default=['local-assistant:latest','qwen3.6:35b'])
    p.add_argument('--characters',type=int,nargs='+',default=[4000,8000,16000,32000])
    p.add_argument('--repeats',type=int,default=2)
    args=p.parse_args()
    if not 1<=len(args.models)<=3 or len(set(args.models))!=len(args.models) or not 2<=args.repeats<=3 or not 3<=len(args.characters)<=6 or any(not 1000<=n<=56000 for n in args.characters):p.error('Use 3..6 fixture sizes of 1000..56000 characters and 2..3 repeats')
    main(args)
