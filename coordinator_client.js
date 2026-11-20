'use strict';

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const net = require('node:net');
const {spawn: spawnProcess} = require('node:child_process');

const VERSION = 1;
const TERMINAL = new Set(['completed', 'cancelled', 'interrupted', 'failed', 'acknowledged']);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const error = (message, code='COORDINATOR_UNAVAILABLE') => Object.assign(new Error(message), {code});
const canonical = value => JSON.stringify(sort(value));
function sort(value) {
  if (Array.isArray(value)) return value.map(sort);
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(key => [key, sort(value[key])]));
  return value;
}
function hash(value) {return crypto.createHash('sha256').update(value).digest('hex');}
function scopeFor(workspace, storage) {return hash(canonical({workspace:path.resolve(workspace), storage:path.resolve(storage)}));}
function privateDirectory(directory) {
  fs.mkdirSync(directory, {recursive:true, mode:0o700});
  const info = fs.lstatSync(directory);
  if (!info.isDirectory() || info.isSymbolicLink() || (process.getuid && info.uid !== process.getuid()) ||
      (process.platform !== 'win32' && (info.mode & 0o077))) throw error('The coordinator directory must be private.');
}
function privateJSON(file, limit=32768) {
  const descriptor = fs.openSync(file, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0) | (fs.constants.O_NONBLOCK || 0));
  try {
    const info = fs.fstatSync(descriptor);
    if (!info.isFile() || info.nlink !== 1 || info.size > limit ||
        (process.getuid && info.uid !== process.getuid()) ||
        (process.platform !== 'win32' && (info.mode & 0o077))) throw error('Invalid private coordinator record.');
    const raw=Buffer.alloc(limit+1), count=fs.readSync(descriptor,raw,0,raw.length,0);
    if(count>limit)throw error('Coordinator record exceeds its bound.');
    return JSON.parse(raw.subarray(0,count).toString('utf8'));
  } finally {fs.closeSync(descriptor);}
}
function runtimeHash(extensionPath) {
  const names = ['runtime-integrity.json', 'coordinator_service.js', 'coordinator_client.js', 'bridge_worker.js'];
  return hash(names.map(name => name+'\0'+hash(fs.readFileSync(path.join(extensionPath,name)))).join('\0'));
}
function endpointFor(root, scope) {
  const value = privateJSON(path.join(root, 'endpoint.json'));
  if (value.version !== VERSION || !Number.isInteger(value.port) || value.port < 1 || value.port > 65535 ||
      !Number.isInteger(value.pid) || value.pid <= 0 || !/^[a-f0-9]{64}$/.test(value.token || '') ||
      !/^[a-f0-9]{32}$/.test(value.instance_id || '') || !/^[a-f0-9]{64}$/.test(value.runtime_hash || '') || value.scope !== scope) {
    throw error('The coordinator identity does not match this workspace.');
  }
  return value;
}
function rpc(endpoint, op, fields={}, timeoutMs=5000) {
  const frame = JSON.stringify({version:VERSION, token:endpoint.token, runtime_hash:endpoint.runtime_hash,
    scope:endpoint.scope, op, ...fields})+'\n';
  if (Buffer.byteLength(frame)>220000) return Promise.reject(error('Coordinator request exceeds its transport bound.'));
  return new Promise((resolve,reject) => {
    const socket = net.createConnection({host:'127.0.0.1',port:endpoint.port});
    let buffer='', ended=false;
    const finish=(failure,value) => {if(ended)return;ended=true;socket.destroy();failure?reject(failure):resolve(value);};
    socket.setEncoding('utf8');
    socket.setTimeout(timeoutMs,()=>finish(error('Coordinator status transport timed out. Running work was retained.','COORDINATOR_DETACHED')));
    socket.on('connect',()=>socket.write(frame));
    socket.on('error',e=>finish(error('Coordinator connection unavailable: '+e.message)));
    socket.on('end',()=>{if(!ended)finish(error('Coordinator disconnected before its acknowledgement. Reconnect by request ID.','COORDINATOR_DETACHED'));});
    socket.on('data',chunk=>{
      buffer+=chunk;
      if(Buffer.byteLength(buffer)>6*1024*1024){finish(error('Coordinator response exceeds its transport bound.'));return;}
      const end=buffer.indexOf('\n');if(end<0)return;
      try {
        const value=JSON.parse(buffer.slice(0,end));
        if(typeof value?.ok!=='boolean')throw error('Invalid coordinator response.');
        if(!value.ok)throw error(value.error||'Coordinator refused this operation.',value.code||'COORDINATOR_REFUSED');
        finish(null,value.data);
      } catch(e) {finish(e);}
    });
  });
}

class CoordinatorClient {
  constructor({workspace,storage,python,extensionPath,node=process.execPath,spawn=spawnProcess,report=()=>{},pollMs=150}={}) {
    this.workspace=fs.realpathSync(workspace);
    fs.mkdirSync(storage,{recursive:true});this.storage=fs.realpathSync(storage);this.python=python;
    this.extensionPath=path.resolve(extensionPath);this.node=node;this.spawn=spawn;this.report=report;
    this.root=path.join(this.storage,'coordinator');this.scope=scopeFor(this.workspace,this.storage);
    this.runtimeHash=runtimeHash(this.extensionPath);this.detached=false;this.pollMs=pollMs;
    this.bootstrap=null;
  }
  async existing() {
    let endpoint;
    try {endpoint=endpointFor(this.root,this.scope);}
    catch(e) {if(e.code==='ENOENT')return null;throw e;}
    try {await rpc(endpoint,'status');return endpoint;}
    catch(e) {
      // A disconnected UI never infers request completion. Only process death
      // permits bootstrapping a replacement, whose journal marks work interrupted.
      try {process.kill(endpoint.pid,0);} catch(gone) {if(gone.code==='ESRCH')return null;throw gone;}
      throw e;
    }
  }
  async ensure({start=true,previous=false}={}) {
    if(this.detached)throw error('Chat disconnected. The runner retains accepted work.','COORDINATOR_DETACHED');
    if(!start&&!fs.existsSync(this.root))return null;
    privateDirectory(this.root);
    let endpoint=await this.existing();
    if(endpoint) {
      if(previous||endpoint.runtime_hash===this.runtimeHash)return endpoint;
      const status=await rpc(endpoint,'status');
      if(status.busy||status.active||status.queued>0)throw error('The previous Sutra runner is finishing retained work. Reconnect to it before starting work under this version.','COORDINATOR_VERSION_BUSY');
      await rpc(endpoint,'shutdown');
      for(let i=0;i<50;i++){try{process.kill(endpoint.pid,0);}catch(e){if(e.code==='ESRCH')break;}await sleep(20);}
      endpoint=null;
    }
    if(!start)return null;
    if(this.bootstrap)return this.bootstrap;
    this.bootstrap=this.start();
    try{return await this.bootstrap;}finally{this.bootstrap=null;}
  }
  async start() {
    const lock=path.join(this.root,'bootstrap.lock');let held=false,config;
    for(let attempt=0;attempt<100;attempt++) {
      try {
        fs.mkdirSync(lock,{mode:0o700});
        fs.writeFileSync(path.join(lock,'owner.json'),JSON.stringify({pid:process.pid}),{mode:0o600,flag:'wx'});
        held=true;
        break;
      } catch(e) {
        if(e.code!=='EEXIST')throw e;
        try {
          const owner=privateJSON(path.join(lock,'owner.json'));
          if(!Number.isInteger(owner.pid)||owner.pid<=0)throw error('Invalid coordinator startup owner.');
          try{process.kill(owner.pid,0);}catch(gone){if(gone.code==='ESRCH'){fs.unlinkSync(path.join(lock,'owner.json'));fs.rmdirSync(lock);continue;}throw gone;}
        } catch(read) {
          if(read.code!=='ENOENT')throw read;
          // A UI may die between mkdir and writing its startup owner. Removing
          // only an empty directory cannot remove a published owner record;
          // the service's independent lock still serializes journal ownership.
          try{fs.rmdirSync(lock);}catch(remove){if(!['ENOENT','ENOTEMPTY','EEXIST'].includes(remove.code))throw remove;}
        }
        const ready=await this.existing();if(ready&&ready.runtime_hash===this.runtimeHash)return ready;
        await sleep(50);
      }
    }
    if(!held)throw error('Another client is starting the Sutra runner. Retry connecting.');
    try {
      const ready=await this.existing();if(ready)return ready;
      config=path.join(this.root,'startup-'+crypto.randomBytes(16).toString('hex')+'.json');
      fs.writeFileSync(config,JSON.stringify({workspace:this.workspace,storage:this.storage,python:this.python,
        extensionPath:this.extensionPath,runtimeHash:this.runtimeHash}),{mode:0o600,flag:'wx'});
      const child=this.spawn(this.node,[path.join(this.extensionPath,'coordinator_service.js'),'--config',config],
        {cwd:this.workspace,stdio:'ignore',detached:true,windowsHide:true,env:{...process.env,ELECTRON_RUN_AS_NODE:'1'}});
      let spawnError;
      child.once('error',e=>{spawnError=e;});child.unref();
      for(let attempt=0;attempt<100;attempt++) {
        if(spawnError)throw error('Could not start the Sutra runner: '+spawnError.message);
        const endpoint=await this.existing();
        if(endpoint) {
          if(endpoint.runtime_hash!==this.runtimeHash)throw error('A different Sutra runtime owns the coordinator.');
          return endpoint;
        }
        await sleep(50);
      }
      throw error('The Sutra runner did not become ready. No request was submitted.');
    } finally {
      if(config)try{fs.unlinkSync(config);}catch(e){if(e.code!=='ENOENT')this.report('Coordinator startup record cleanup: '+e.message);}
      try{fs.unlinkSync(path.join(lock,'owner.json'));fs.rmdirSync(lock);}catch{}
    }
  }
  async allocate(chatKey='') {
    const endpoint=await this.ensure();return rpc(endpoint,'allocate',{chat_key:chatKey});
  }
  async send(request,environment,{token,onActivity=()=>{},transport,authorization}={}) {
    if(token?.isCancellationRequested)throw error('Sutra request cancelled before submission.','COORDINATOR_DETACHED');
    const endpoint=await this.ensure();
    const chatKey=transport?.chatKey||'';
    const id=transport?.id||(await rpc(endpoint,'allocate',{chat_key:chatKey})).id;
    const wire=JSON.parse(JSON.stringify({request,environment:Object.fromEntries(Object.entries(environment).map(([key,value])=>[key,value??null]))}));
    if(authorization)wire.authorization=authorization;
    const digest=hash(canonical(wire));
    // The ID is durable before a model or source operation begins. A lost
    // submission acknowledgement must be recovered by ID, never blind replay.
    await rpc(endpoint,'submit',{id,digest,chat_key:chatKey,...wire});
    const response=await this.wait(endpoint,id,chatKey,{token,onActivity});
    if(!transport?.id)await rpc(endpoint,'ack',{id,chat_key:chatKey});
    return response;
  }
  async resume(ticket,{token,onActivity=()=>{}}={}) {
    const endpoint=await this.ensure({previous:true});
    return this.wait(endpoint,ticket.id,ticket.chatKey||ticket.nativeChat||'',{token,onActivity});
  }
  async wait(endpoint,id,chatKey,{token,onActivity}) {
    let cursor=0;
    while(true) {
      if(this.detached||token?.isCancellationRequested)throw error('Stopped displaying this request. The runner retains it; reconnect with continue or stop it in Sutra Monitor.','COORDINATOR_DETACHED');
      const status=await rpc(endpoint,'status',{id,chat_key:chatKey});
      if(status.response_evicted)throw error('This result was already acknowledged. Its saved workflow and ledger remain available.','COORDINATOR_RESULT_RETIRED');
      const replay=await rpc(endpoint,'events',{id,chat_key:chatKey,after:cursor});
      if(replay.dropped_before>cursor) {
        for(const event of replay.active_calls||[])try{onActivity(event);}catch{}
        cursor=replay.dropped_before;
      }
      for(const row of replay.events||[]) {
        if(row.seq<=cursor)continue;
        cursor=row.seq;
        try{onActivity(row.event);}catch{/* Presentation cannot change execution. */}
      }
      if(TERMINAL.has(status.state)) {
        if(cursor<(status.last_event_seq||0))continue;
        if(status.state==='cancelled')throw error(status.error||'The runner acknowledged cancellation. Backend completion remains unverified.','COORDINATOR_CANCELLED');
        if(status.state==='interrupted')throw error(status.error||'The runner was interrupted. Durable work remains charged and was not replayed.','COORDINATOR_INTERRUPTED');
        if(status.response)return status.response;
        throw error(status.error||'The runner failed without a response. Saved work was not replayed.','COORDINATOR_FAILED');
      }
      await sleep(this.pollMs);
    }
  }
  async ack(ticket) {
    const endpoint=await this.ensure({previous:true});return rpc(endpoint,'ack',{id:ticket.id,chat_key:ticket.chatKey||ticket.nativeChat||''});
  }
  async cancel(ticket) {
    const endpoint=await this.ensure({start:false,previous:true});
    if(!endpoint)throw error('No runner is available to acknowledge cancellation.');
    return rpc(endpoint,'cancel',{id:ticket.id,chat_key:ticket.chatKey||ticket.nativeChat||''});
  }
  async cancelAll() {
    const endpoint=await this.ensure({start:false,previous:true});
    return endpoint?rpc(endpoint,'cancel_all'):{requested:0};
  }
  async snapshot() {
    const endpoint=await this.ensure({start:false,previous:true});
    return endpoint?rpc(endpoint,'status',{all:true}):{running:false,busy:false,jobs:[]};
  }
  dispose() {this.detached=true;}
}

module.exports={CoordinatorClient,canonical,scopeFor,runtimeHash,privateJSON,rpc};
