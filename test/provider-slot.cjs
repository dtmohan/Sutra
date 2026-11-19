'use strict';
const assert=require('node:assert/strict');
const {EventEmitter}=require('node:events');
const {PassThrough}=require('node:stream');
const {withSlot}=require('../provider_slot');

function fixture({ready='{"held":true}\n',beforeReady,closeOnRelease=true,releaseCode=0}={}){
 const calls=[],releases=[],kills=[];
 const child=new EventEmitter();child.stdout=new PassThrough();child.stdin=new EventEmitter();
 child.stdin.end=value=>{releases.push(value);if(closeOnRelease)queueMicrotask(()=>child.emit('close',releaseCode,null));};
 child.kill=signal=>{kills.push(signal);queueMicrotask(()=>child.emit('close',null,signal));return true;};
 const spawn=(command,args,options)=>{
  calls.push({command,args,options});
  queueMicrotask(()=>{if(beforeReady)beforeReady(child);if(ready!==null)child.stdout.write(ready);});
  return child;
 };
 return {child,spawn,calls,releases,kills};
}
const options=subject=>({python:'python3',helper:'/installed Sutra/harness/provider_startup.py',
 runtimeDir:'/private provider runtime',environment:{PATH:'/trusted/bin'},spawn:subject.spawn,
 readyTimeoutMs:20,releaseTimeoutMs:20});

(async()=>{
 const normal=fixture();let actions=0;
 const result=await withSlot(options(normal),async control=>{
  actions++;control.assertHeld();assert.equal(control.signal.aborted,false);
  assert.equal(normal.releases.length,0,'The lease must remain held throughout the action');
  return {ready:true};
 });
 assert.deepEqual(result,{ready:true});assert.equal(actions,1);assert.deepEqual(normal.releases,['\n']);
 assert.deepEqual(normal.kills,[]);
 assert.deepEqual(normal.calls[0],{command:'python3',args:['-B','/installed Sutra/harness/provider_startup.py',
  '--hold-slot','--runtime-directory','/private provider runtime'],
  options:{env:{PATH:'/trusted/bin'},stdio:['pipe','pipe','ignore'],shell:false,windowsHide:true}});

 const failure=fixture(),original=new Error('Native operation failed');
 await assert.rejects(withSlot(options(failure),async()=>{throw original;}),error=>error===original);
 assert.deepEqual(failure.releases,['\n']);assert.deepEqual(failure.kills,[]);

 for(const ready of ['{"held":false,"reason":"Provider slot is busy"}\n','not JSON\n',
  '{"ready":true}\n','{"held":true}\n{"held":true}\n','x'.repeat(4097)]){
  const rejected=fixture({ready});let seen=0;
  await assert.rejects(withSlot(options(rejected),async()=>{seen++;}),/provider slot/i);
  assert.equal(seen,0,'Invalid or busy readiness cannot dispatch an action');
  assert.deepEqual(rejected.releases,['\n']);
 }
 const partial=fixture({ready:'{"held":true}'});
 await assert.rejects(withSlot({...options(partial),readyTimeoutMs:1},async()=>assert.fail('No complete handshake')),/Timed out/);
 assert.deepEqual(partial.releases,['\n']);

 const early=fixture({ready:null,beforeReady:child=>child.emit('close',2,null)});
 await assert.rejects(withSlot(options(early),async()=>assert.fail('Exited helper cannot run an action')),/lost before/);
 assert.deepEqual(early.releases,[],'An exited helper already released its OS lease');
 const spawnFailure=fixture({ready:null,beforeReady:child=>child.emit('error',new Error('ENOENT'))});
 await assert.rejects(withSlot(options(spawnFailure),async()=>assert.fail('Failed spawn cannot run action')),/could not run/);
 assert.deepEqual(spawnFailure.releases,['\n']);
 const pipe=fixture({ready:null,beforeReady:child=>child.stdin.emit('error',new Error('EPIPE'))});
 await assert.rejects(withSlot(options(pipe),async()=>assert.fail('Broken pipe cannot run action')),/pipe failed/);
 assert.deepEqual(pipe.releases,['\n']);

 const loss=fixture();let lostSignal;
 await assert.rejects(withSlot(options(loss),async control=>{
  lostSignal=control.signal;loss.child.emit('close',1,null);control.assertHeld();
 }),error=>error.code==='PROVIDER_SLOT_LOST');
 assert.equal(lostSignal.aborted,true);assert.deepEqual(loss.kills,[]);

 const beforeCancel=new AbortController();beforeCancel.abort();
 const untouched=fixture();
 await assert.rejects(withSlot({...options(untouched),signal:beforeCancel.signal},async()=>assert.fail()),/cancelled before/);
 assert.equal(untouched.calls.length,0);
 const during=new AbortController(),cancelled=fixture();
 await assert.rejects(withSlot({...options(cancelled),signal:during.signal},async control=>{
  const observed=new Promise(resolve=>control.signal.addEventListener('abort',resolve,{once:true}));
  during.abort();await observed;
 }),error=>error.code==='PROVIDER_SLOT_CANCELLED');
 assert.deepEqual(cancelled.releases,['\n']);

 const hung=fixture({closeOnRelease:false});let finished=0;
 await assert.rejects(withSlot({...options(hung),releaseTimeoutMs:1},async()=>{finished++;}),/did not confirm release/);
 assert.equal(finished,1);assert.deepEqual(hung.releases,['\n']);assert.deepEqual(hung.kills,['SIGKILL']);
 const badRelease=fixture({releaseCode:2});
 await assert.rejects(withSlot(options(badRelease),async()=>true),/release error/);

 let invalidCalls=0;
 await assert.rejects(withSlot({python:'python3',helper:'relative.py',runtimeDir:'/runtime',spawn:()=>{invalidCalls++;}},async()=>true),/configuration is invalid/);
 assert.equal(invalidCalls,0);
 if(process.argv.includes('--real-python')){
  const fs=require('node:fs'),path=require('node:path'),os=require('node:os');
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'sutra-provider-slot-'));
  try{
   const real={python:process.env.SUTRA_PYTHON||'python3',helper:path.join(__dirname,'..','harness','provider_startup.py'),runtimeDir:directory};
   const outer=await withSlot(real,async()=>{
    await assert.rejects(withSlot(real,async()=>assert.fail('Concurrent Python lease dispatched')),error=>error.code==='PROVIDER_SLOT_BUSY');
    return 'outer held';
   });
   assert.equal(outer,'outer held');
   assert.equal(await withSlot(real,async()=> 'released and reacquired'),'released and reacquired');
   console.log('Real Python OS lease blocked a concurrent helper and became available after EOF release.');
  }finally{fs.rmSync(directory,{recursive:true,force:true});}
 }
 console.log('Provider slot checks passed for exact helper args, exclusive action, busy/invalid/timeout/pipe/exit/cancellation recovery and bounded helper-only release.');
})().catch(error=>{console.error(error);process.exitCode=1;});
