'use strict';

const {spawn:spawnProcess}=require('node:child_process');
const {isAbsolute}=require('node:path');

const safeString=value=>typeof value==='string'&&value.length>0&&value.length<=4096&&
  !/[\x00-\x1f\x7f]/.test(value);
const slotError=(message,code='PROVIDER_SLOT_UNAVAILABLE')=>Object.assign(new Error(message),{code});

// The helper holds the same OS lease as Python activation and inference. Its
// lifetime encloses one host action; it is never a model server or a daemon.
async function withSlot({python,helper,runtimeDir,environment=process.env,
  spawn=spawnProcess,signal,readyTimeoutMs=8000,releaseTimeoutMs=2000}={},action){
  if(!safeString(python)||!safeString(helper)||!isAbsolute(helper)||
      !safeString(runtimeDir)||!isAbsolute(runtimeDir)||typeof action!=='function'){
    throw slotError('The native provider slot configuration is invalid.');
  }
  if(signal?.aborted)throw slotError('Native provider operation cancelled before acquiring its slot.','PROVIDER_SLOT_CANCELLED');
  let child;
  try{
    child=spawn(python,['-B',helper,'--hold-slot','--runtime-directory',runtimeDir],
      {env:{...environment},stdio:['pipe','pipe','ignore'],shell:false,windowsHide:true});
  }catch{
    throw slotError('Could not start the native provider slot helper.');
  }
  const controller=new AbortController();
  let phase='waiting',held=false,closed=false,protocolError,buffer='',readyTimer;
  let readyResolve,readyReject,closeResolve;
  const ready=new Promise((resolve,reject)=>{readyResolve=resolve;readyReject=reject;});
  const close=new Promise(resolve=>{closeResolve=resolve;});
  function fail(error){
    protocolError??=error;
    if(!controller.signal.aborted)controller.abort(protocolError);
    if(phase==='waiting')readyReject(protocolError);
  }
  const cancel=()=>fail(slotError('Native provider operation cancelled.','PROVIDER_SLOT_CANCELLED'));
  const pipeFailure=()=>fail(slotError('The native provider slot helper pipe failed.'));
  const childFailure=()=>fail(slotError('The native provider slot helper could not run.'));
  function receive(chunk){
    if(phase==='releasing')return;
    const text=typeof chunk==='string'?chunk:chunk.toString('utf8');
    if(phase!=='waiting'){
      if(text.trim())fail(slotError('The native provider slot helper sent an unexpected response.'));
      return;
    }
    buffer+=text;
    if(Buffer.byteLength(buffer,'utf8')>4096){fail(slotError('The native provider slot response exceeded its limit.'));return;}
    const end=buffer.indexOf('\n');
    if(end<0)return;
    let result;
    try{result=JSON.parse(buffer.slice(0,end));}
    catch{fail(slotError('The native provider slot helper returned invalid JSON.'));return;}
    if(buffer.slice(end+1).trim()||!result||typeof result.held!=='boolean'){
      fail(slotError('The native provider slot helper returned an invalid response.'));return;
    }
    if(result.held!==true){
      const reason=typeof result.reason==='string'?result.reason.replace(/[\x00-\x1f\x7f]/g,' ').slice(0,2048):'Another provider operation holds the slot.';
      fail(slotError('Native provider slot unavailable. '+reason,'PROVIDER_SLOT_BUSY'));return;
    }
    held=true;phase='acquired';clearTimeout(readyTimer);readyResolve();
  }
  function ended(){if(phase!=='releasing')fail(slotError('The native provider slot helper closed its response pipe.'));}
  function exited(code){
    closed=true;held=false;closeResolve();
    if(phase!=='releasing')fail(slotError('The native provider slot was lost before the operation finished.','PROVIDER_SLOT_LOST'));
    else if(code!==0&&code!==null)protocolError??=slotError('The native provider slot helper reported a release error.');
  }
  child.on('error',childFailure);child.on('close',exited);
  child.stdout?.setEncoding?.('utf8');
  child.stdout?.on('data',receive);child.stdout?.on('error',pipeFailure);child.stdout?.on('end',ended);
  child.stdin?.on('error',pipeFailure);
  signal?.addEventListener?.('abort',cancel,{once:true});
  readyTimer=setTimeout(()=>fail(slotError('Timed out waiting for the native provider slot.')),
    Math.min(8000,Math.max(1,readyTimeoutMs)));
  if(!child.stdin||!child.stdout)fail(slotError('The native provider slot helper pipes are unavailable.'));
  if(signal?.aborted)cancel();
  function assertHeld(){
    if(!held||closed||protocolError||controller.signal.aborted){
      throw protocolError||slotError('The native provider slot is no longer held.','PROVIDER_SLOT_LOST');
    }
  }
  let value,error;
  try{
    await ready;assertHeld();phase='active';
    value=await action({signal:controller.signal,assertHeld});
    assertHeld();
  }catch(caught){error=caught;}
  finally{
    clearTimeout(readyTimer);phase='releasing';held=false;
    signal?.removeEventListener?.('abort',cancel);
    if(!closed){
      try{child.stdin?.end('\n');}catch{}
      let timer;
      const finished=await Promise.race([close.then(()=>true),new Promise(resolve=>{
        timer=setTimeout(()=>resolve(false),Math.min(2000,Math.max(1,releaseTimeoutMs)));
      })]);
      clearTimeout(timer);
      if(!finished){
        // Only the helper is killed. The OS then releases its lease. Native
        // runtimes retain their own independent owner identities and lifetime.
        try{child.kill('SIGKILL');}catch{}
        protocolError??=slotError('The native provider slot helper did not confirm release.');
        let killTimer;
        await Promise.race([close,new Promise(resolve=>{killTimer=setTimeout(resolve,200);})]);
        clearTimeout(killTimer);
      }
    }
    child.removeListener('error',childFailure);child.removeListener('close',exited);
    child.stdout?.removeListener('data',receive);child.stdout?.removeListener('error',pipeFailure);
    child.stdout?.removeListener('end',ended);child.stdin?.removeListener('error',pipeFailure);
  }
  if(protocolError){if(error&&error!==protocolError)protocolError.cause=error;throw protocolError;}
  if(error)throw error;
  return value;
}

module.exports={withSlot};
