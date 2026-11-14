'use strict';
const {StringDecoder}=require('node:string_decoder');
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const MAX_BYTES=1024*1024,MAX_AGE=7*86400000;
const KINDS=new Set(['start','control','thinking','finish']);
// Only the private bridge transport uses this decoder. Events never become tools,
// conversation memory, grants or ledger receipts.
function decoder(onEvent,onStderr){
  const utf8=new StringDecoder('utf8');let pending='',discard=false;
  const active=new Map();
  function line(raw){
    if(!raw.startsWith('SUTRA_EVENT ')){onStderr(raw+'\n');return;}
    let e;try{e=JSON.parse(raw.slice(12));}catch{return;}
    if(!e||!KINDS.has(e.kind)||typeof e.id!=='string'||e.id.length>128||!Number.isSafeInteger(e.seq))return;
    if(e.kind==='start'){
      if(active.has(e.id)||active.size>=64||e.seq!==1||typeof e.model!=='string'||typeof e.role!=='string')return;
      active.set(e.id,{seq:0,chars:0,done:false,identity:{id:e.id,model:e.model.slice(0,160),role:e.role.slice(0,80),node_id:typeof e.node_id==='string'?e.node_id.slice(0,128):null}});
    }
    const state=active.get(e.id);if(!state||state.done||e.seq<=state.seq)return;state.seq=e.seq;
    const event={...state.identity,kind:e.kind,seq:e.seq};
    if(e.kind==='thinking'){
      if(typeof e.text!=='string')return;
      event.text=e.text.slice(0,Math.min(2048,Math.max(0,16000-state.chars)));state.chars+=event.text.length;
      if(!event.text)return;
    }
    if(e.kind==='control')event.requested=(typeof e.requested==='string'||typeof e.requested==='boolean'||e.requested===null)?e.requested:null;
    if(e.kind==='finish'){
      state.done=true;event.status=e.status==='accepted'?'accepted':'withheld';event.display_truncated=!!e.display_truncated;
      event.usage={};for(const k of ['input_tokens','output_tokens'])event.usage[k]=Number.isSafeInteger(e.usage?.[k])&&e.usage[k]>=0?e.usage[k]:null;
    }
    try{onEvent(event);}catch{/* Diagnostics cannot abort governed work. */}
  }
  function feed(text){
    for(const chunk of text.split(/(?<=\n)/)){
      if(!discard)pending+=chunk;
      if(pending.length>65536){pending='';discard=true;}
      if(chunk.endsWith('\n')){if(!discard)line(pending.trimEnd());pending='';discard=false;}
    }
  }
  return {write:bytes=>feed(utf8.write(bytes)),end(){feed(utf8.end());if(pending&&!discard)line(pending);pending='';},
    close(){for(const s of active.values())if(!s.done){s.done=true;try{onEvent({...s.identity,kind:'finish',seq:s.seq+1,status:'interrupted',usage:{input_tokens:null,output_tokens:null}});}catch{}}}};
}

class ActivityLog{
  constructor(){this.calls=[];}
  accept(e){
    if(e.kind==='start'){
      this.calls.push({...e,text:'',status:'generating',startedAt:Date.now()});
      this.calls=this.calls.slice(-12);
    }else{
      const c=this.calls.find(x=>x.id===e.id);if(!c)return;
      if(e.kind==='thinking')c.text=(c.text+e.text).slice(0,16000);
      if(e.kind==='control')c.requested=e.requested;
      if(e.kind==='finish')Object.assign(c,{status:e.status,usage:e.usage,display_truncated:e.display_truncated,finishedAt:Date.now()});
    }
  }
  clear(){this.calls=[];}
}
// Diagnostic text only: never imported into task memory, contracts or receipts.
// Keep the archive small even when a model emits multi-byte Unicode or escapes.
class ActivityArchive extends ActivityLog{
  constructor(directory){
    super();this.directory=directory;this.file=path.join(directory,'reasoning.json');this.error='';this.lastWrite=0;
    let fd;
    try{
      fd=fs.openSync(this.file,fs.constants.O_RDONLY|fs.constants.O_NOFOLLOW);
      const stat=fs.fstatSync(fd);
      if(!stat.isFile()||stat.nlink!==1||stat.size>MAX_BYTES)throw Error('Invalid reasoning archive');
      const saved=JSON.parse(fs.readFileSync(fd,'utf8'));
      if(saved.version!==1||!Array.isArray(saved.calls))throw Error('Unknown reasoning archive');
      const seen=new Set();
      for(const raw of saved.calls.slice(-12)){
        const c=cleanCall(raw);if(!c||seen.has(c.id))continue;seen.add(c.id);
        if(c.status==='generating'){c.status='interrupted';c.finishedAt=c.updatedAt;c.interruptedOnReload=true;}
        this.calls.push(c);
      }
      this.prune();
    }catch(e){if(e.code!=='ENOENT')this.error='Saved reasoning could not be loaded.';}
    finally{if(fd!==undefined)fs.closeSync(fd);}
  }
  prune(){
    this.calls=this.calls.filter(c=>c.startedAt>=Date.now()-MAX_AGE).slice(-12);
    while(this.calls.length&&Buffer.byteLength(JSON.stringify({version:1,calls:this.calls}))>MAX_BYTES)this.calls.shift();
  }
  accept(e){
    if(!/^[a-f0-9]{32}$/.test(e.id))return;
    if(e.kind==='start'&&this.calls.some(c=>c.id===e.id))return;
    super.accept(e);
    const c=this.calls.find(c=>c.id===e.id);if(c)c.updatedAt=Date.now();
    this.calls=this.calls.map(cleanCall).filter(Boolean);this.prune();
    if(e.kind==='start'||e.kind==='finish'||Date.now()-this.lastWrite>=2000)this.flush();
    else if(!this.timer){this.timer=setTimeout(()=>{this.timer=undefined;this.flush();},2000);this.timer.unref();}
  }
  flush(){
    clearTimeout(this.timer);this.timer=undefined;this.prune();this.lastWrite=Date.now();
    const temp=path.join(this.directory,'.reasoning-'+crypto.randomBytes(12).toString('hex')+'.tmp');
    try{
      fs.mkdirSync(this.directory,{recursive:true,mode:0o700});
      fs.writeFileSync(temp,JSON.stringify({version:1,calls:this.calls}),{mode:0o600,flag:'wx'});
      fs.renameSync(temp,this.file);this.error='';
    }catch{this.error='Reasoning is visible in this window, but could not be saved for reload.';}
    finally{try{fs.unlinkSync(temp);}catch{}}
  }
  dispose(){this.flush();}
  document(id){
    this.prune();const c=this.calls.find(c=>c.id===id);
    if(!c)return 'Sutra reasoning details\n\nThis call is unavailable or outside the retained history (12 calls, 7 days, 1 MiB). Earlier versions did not save reasoning.\n';
    return ['Sutra reasoning details — unverified model commentary',
      'This is diagnostic text, not an accepted answer, task memory or protocol evidence.',
      '',`Invocation: ${c.id}`,`Model: ${c.model}`,`Stage: ${c.role}`,`Task: ${c.node_id||'not recorded'}`,
      `Status: ${c.status}${c.interruptedOnReload?' (window closed before completion was observed)':''}`,
      `Tokens: ${c.usage?.input_tokens??'?'} input / ${c.usage?.output_tokens??'?'} output (includes reasoning)`,
      c.display_truncated?'Display shortened at 16,000 characters; generation remains charged.':'',
      '', '--- Model commentary (plain text) ---', '',c.text||'No separate reasoning was emitted.', ''].join('\n');
  }
}
function cleanCall(c){
  if(!c||!/^[a-f0-9]{32}$/.test(c.id)||typeof c.model!=='string'||typeof c.role!=='string'||!Number.isFinite(c.startedAt)||c.startedAt>Date.now()+60000)return null;
  const number=n=>Number.isSafeInteger(n)&&n>=0?n:null;
  return {id:c.id,model:c.model.slice(0,160),role:c.role.slice(0,80),node_id:typeof c.node_id==='string'?c.node_id.slice(0,128):null,
    text:typeof c.text==='string'?c.text.slice(0,16000):'',status:['generating','accepted','withheld','interrupted'].includes(c.status)?c.status:'interrupted',
    startedAt:c.startedAt,updatedAt:Number.isFinite(c.updatedAt)?c.updatedAt:c.startedAt,finishedAt:Number.isFinite(c.finishedAt)?c.finishedAt:undefined,
    requested:typeof c.requested==='string'?c.requested.slice(0,80):typeof c.requested==='boolean'?c.requested:null,
    usage:c.usage?{input_tokens:number(c.usage.input_tokens),output_tokens:number(c.usage.output_tokens)}:undefined,
    display_truncated:!!c.display_truncated,interruptedOnReload:!!c.interruptedOnReload};
}
function activityUri(directory,id){
  const key=path.basename(directory);
  return /^[a-f0-9]{24}$/.test(key)&&/^[a-f0-9]{32}$/.test(id)?`karma-preview:/activity/${key}/${id}/Reasoning.txt`:null;
}
function parseActivityUri(uri){
  const match=String(uri).match(/^karma-preview:\/activity\/([a-f0-9]{24})\/([a-f0-9]{32})\/Reasoning\.txt$/);
  return match?{workspace:match[1],id:match[2]}:null;
}
module.exports={decoder,ActivityLog,ActivityArchive,activityUri,parseActivityUri};
