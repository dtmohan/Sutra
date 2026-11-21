'use strict';

// Measured daemon observations only. The existing hardware adapter decides
// admission; sampler errors never become inferred pressure or permission.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {spawn}=require('node:child_process');
const {StringDecoder}=require('node:string_decoder');
const MAX_SAMPLE=64*1024,MAX_HISTORY=1024*1024,MAX_SAMPLES=60;

function privateDirectory(directory){
  fs.mkdirSync(directory,{recursive:true,mode:0o700});
  const info=fs.lstatSync(directory);
  if(!info.isDirectory()||info.isSymbolicLink()||process.getuid&&info.uid!==process.getuid()||
     process.platform!=='win32'&&(info.mode&0o077))throw new Error('Coordinator telemetry directory is not private');
}
function privateFile(file){
  try{
    const info=fs.lstatSync(file);
    if(!info.isFile()||info.nlink!==1||process.getuid&&info.uid!==process.getuid()||
       process.platform!=='win32'&&(info.mode&0o077))throw new Error('Coordinator telemetry file is not private and unlinked');
  }catch(error){if(error.code!=='ENOENT')throw error;}
}
function atomic(file,value,limit){
  const raw=JSON.stringify(value);if(Buffer.byteLength(raw)>limit)throw new Error('Coordinator telemetry exceeds its allocation');
  privateFile(file);
  const temporary=file+'.'+crypto.randomBytes(8).toString('hex')+'.tmp';let descriptor;
  try{
    descriptor=fs.openSync(temporary,fs.constants.O_CREAT|fs.constants.O_EXCL|fs.constants.O_WRONLY|(fs.constants.O_NOFOLLOW||0),0o600);
    fs.writeFileSync(descriptor,raw);fs.closeSync(descriptor);descriptor=undefined;
    fs.renameSync(temporary,file);
  }finally{
    if(descriptor!==undefined)fs.closeSync(descriptor);
    try{fs.unlinkSync(temporary);}catch(error){if(error.code!=='ENOENT')throw error;}
  }
}

class CoordinatorTelemetry{
  constructor({storage,extensionPath,node=process.execPath,platform=process.platform,arch=process.arch,
    spawn:spawnCollector=spawn,onError=()=>{},retryMs=1000,now=()=>Date.now()/1000}){
    this.directory=path.join(path.resolve(storage),'coordinator');this.extensionPath=path.resolve(extensionPath);
    this.node=node;this.platform=platform;this.arch=arch;this.spawn=spawnCollector;this.onError=onError;
    this.retryMs=retryMs;this.now=now;this.history=[];this.child=null;this.timer=null;this.disposed=false;
  }
  report(error){try{this.onError(String(error?.message||error).slice(0,500));}catch{}}
  ensure(){
    if(this.disposed)return false;
    if(this.child||this.timer)return true;
    try{
      privateDirectory(this.directory);
      const native=this.platform==='darwin'&&this.arch==='arm64';
      const child=this.spawn(native?path.join(this.extensionPath,'bin','telemetry'):this.node,
        native?[]:[path.join(this.extensionPath,'harness','telemetry_portable.js')],
        {stdio:['ignore','pipe','pipe'],env:{...process.env,ELECTRON_RUN_AS_NODE:'1'},windowsHide:true});
      this.child=child;
      const decoder=new StringDecoder('utf8');let pending='',discard=false;
      child.stdout.on('data',chunk=>{
        if(this.disposed||this.child!==child)return;
        const text=decoder.write(Buffer.isBuffer(chunk)?chunk:Buffer.from(chunk));
        for(const part of text.match(/[^\n]*\n|[^\n]+$/g)||[]){
          const ended=part.endsWith('\n');
          if(!discard){
            pending+=ended?part.slice(0,-1):part;
            if(Buffer.byteLength(pending)>MAX_SAMPLE){discard=true;pending='';this.report('Coordinator telemetry sample exceeded its allocation');}
          }
          if(ended){if(!discard)this.sample(pending);pending='';discard=false;}
        }
      });
      child.stderr.on('data',chunk=>{if(!this.disposed&&this.child===child)this.report(String(chunk).slice(0,500));});
      const stopped=error=>{
        if(this.child!==child)return;
        this.child=null;
        if(error){try{child.kill('SIGTERM');}catch{}this.report(error);}
        if(!this.disposed)this.retry();
      };
      child.once('error',stopped);child.once('close',()=>stopped());
      return true;
    }catch(error){this.report(error);this.retry();return false;}
  }
  retry(){
    if(this.disposed||this.timer)return;
    this.timer=setTimeout(()=>{this.timer=null;this.ensure();},this.retryMs);this.timer.unref?.();
  }
  sample(raw){
    try{
      const value=JSON.parse(raw),now=this.now();
      if(!value||typeof value!=='object'||Array.isArray(value)||typeof value.wall_time!=='number'||
         !Number.isFinite(value.wall_time)||value.wall_time<0||value.wall_time>now||now-value.wall_time>8)
        throw new Error('Coordinator telemetry has no fresh measured timestamp');
      if(this.history.length&&value.wall_time<=this.history.at(-1).wall_time)return;
      const history=[...this.history,value].slice(-MAX_SAMPLES);
      while(Buffer.byteLength(JSON.stringify(history))>MAX_HISTORY)history.shift();
      privateDirectory(this.directory);
      atomic(path.join(this.directory,'telemetry.json'),value,MAX_SAMPLE);
      atomic(path.join(this.directory,'telemetry-history.json'),history,MAX_HISTORY);
      this.history=history;
    }catch(error){this.report(error);}
  }
  dispose(){
    if(this.disposed)return;this.disposed=true;clearTimeout(this.timer);this.timer=null;
    const child=this.child;this.child=null;if(child)try{child.kill('SIGTERM');}catch(error){this.report(error);}
  }
}
module.exports={CoordinatorTelemetry};
