'use strict';
const {StringDecoder}=require('node:string_decoder');
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const retention=require('./harness/memory-policy.json').diagnostics;
const MAX_BYTES=retention.reasoning_bytes,MAX_AGE=retention.max_age_seconds*1000;
const KINDS=new Set(['start','control','thinking','finish']);
function callLabel(role){
  return {review_reader:'Architecture evidence selection',report_reviewer:'Architecture claim review',subagent:'Child review',chat_reviewer:'Workspace review',conversation:'Conversation / routing'}[role]||'Model call';
}
function controlLabel(requested){
  return requested===undefined?'not reported':requested===null?'model default':requested===false?'off requested':requested===true?'on requested':String(requested);
}
// A call starting (or finishing) does not establish that it emitted reasoning.
// Keep metadata out of VS Code's thinking UI until actual reasoning arrives.
function chatActivity(vscode,progress,activityLink){
  const calls=new Map();
  const text=value=>progress.report(new vscode.LanguageModelTextPart(value));
  return e=>{
    if(e.kind==='start'){
      if(calls.has(e.id))return;
      calls.set(e.id,{...e,opened:false,done:false});
      const uri=activityLink?.(e),label=callLabel(e.role)+' · '+e.model+' · call details';
      if(uri)text(`\n\n[${label}](${uri})\n\n`);
      return;
    }
    const c=calls.get(e.id);if(!c||c.done)return;
    if(e.kind==='control'){c.requested=e.requested;return;}
    const canThink=typeof vscode.LanguageModelThinkingPart==='function';
    if(e.kind==='thinking'&&typeof e.text==='string'&&e.text.trim())c.received=true;
    if(e.kind==='thinking'&&typeof e.text==='string'&&canThink){
      if(!c.opened){
        if(!e.text.trim())return;
        c.opened=true;
        const header=callLabel(c.role)+' · '+c.model+' · '+c.role+'\n'+(c.node_id?'Task '+c.node_id+'\n':'')+
          'Unverified local-model reasoning.\nReasoning control: '+controlLabel(c.requested)+'.\n';
        progress.report(new vscode.LanguageModelThinkingPart(header,e.id));
      }
      progress.report(new vscode.LanguageModelThinkingPart(e.text,e.id));
    }
    if(e.kind!=='finish')return;
    c.done=true;
    const status=e.status==='accepted'?'checks passed; task completion and answer correctness are not verified':e.status;
    const shortened=e.display_truncated?' Display shortened; full generation still charged.':'';
    if(c.opened){
      progress.report(new vscode.LanguageModelThinkingPart('\nProtocol: '+status+' · '+(e.usage?.input_tokens??'?')+
        ' input / '+(e.usage?.output_tokens??'?')+' output tokens (reasoning included).'+shortened+'\n',e.id));
      progress.report(new vscode.LanguageModelThinkingPart('',e.id,{vscode_reasoning_done:true}));
    }else{
      const reasoning=e.thinking_chars>0||c.received
        ? (canThink?'Reasoning was reported but no text was received for inline display.':'Reasoning details are available in Sutra Monitor.')
        : e.status==='interrupted'?'No separate reasoning text was received before interruption.':'No separate reasoning text received.';
      text('\n\n'+callLabel(c.role)+' · '+c.model+' · '+(e.status==='accepted'?'call finished':e.status)+'. '+reasoning+
        ' Reasoning control: '+controlLabel(c.requested)+'.'+shortened+'\n\n');
    }
  };
}
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
      event.text=e.text.slice(0,Math.min(2048,Math.max(0,retention.reasoning_chars-state.chars)));state.chars+=event.text.length;
      if(!event.text)return;
    }
    if(e.kind==='control')event.requested=(typeof e.requested==='string'||typeof e.requested==='boolean'||e.requested===null)?e.requested:null;
    if(e.kind==='finish'){
      state.done=true;event.status=e.status==='accepted'?'accepted':'withheld';event.display_truncated=!!e.display_truncated;
      event.thinking_chars=Number.isSafeInteger(e.thinking_chars)&&e.thinking_chars>=0?e.thinking_chars:null;
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
      this.calls=this.calls.slice(-retention.reasoning_calls);
    }else{
      const c=this.calls.find(x=>x.id===e.id);if(!c)return;
      if(e.kind==='thinking')c.text=(c.text+e.text).slice(0,retention.reasoning_chars);
      if(e.kind==='control')c.requested=e.requested;
      if(e.kind==='finish')Object.assign(c,{status:e.status,usage:e.usage,thinking_chars:e.thinking_chars,display_truncated:e.display_truncated,finishedAt:Date.now()});
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
      for(const raw of saved.calls.slice(-retention.reasoning_calls)){
        const c=cleanCall(raw);if(!c||seen.has(c.id))continue;seen.add(c.id);
        if(c.status==='generating'){c.status='interrupted';c.finishedAt=c.updatedAt;c.interruptedOnReload=true;}
        this.calls.push(c);
      }
      this.prune();
    }catch(e){if(e.code!=='ENOENT')this.error='Saved reasoning could not be loaded.';}
    finally{if(fd!==undefined)fs.closeSync(fd);}
  }
  prune(){
    this.calls=this.calls.filter(c=>c.startedAt>=Date.now()-MAX_AGE).slice(-retention.reasoning_calls);
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
    return ['Sutra model call details — reasoning is unverified model commentary',
      'This is diagnostic text, not an accepted answer, task memory or protocol evidence.',
      '',`Invocation: ${c.id}`,`Model: ${c.model}`,`Stage: ${c.role}`,`Task: ${c.node_id||'not recorded'}`,
      `Status: ${c.status}${c.interruptedOnReload?' (window closed before completion was observed)':''}`,
      'Protocol admission does not verify task completion or answer correctness.',
      `Reasoning control: ${controlLabel(c.requested)}`,
      `Reasoning characters reported: ${c.thinking_chars??'not recorded'}`,
      `Tokens: ${c.usage?.input_tokens??'?'} input / ${c.usage?.output_tokens??'?'} output (includes reasoning)`,
      c.display_truncated?'Display shortened at 16,000 characters; generation remains charged.':'',
      '', '--- Model commentary (plain text) ---', '',c.text||(c.status==='generating'?'No separate reasoning text received yet.':
        c.thinking_chars>0?'Reasoning was reported but its text is unavailable in this archive.':'No separate reasoning text received.'), ''].join('\n');
  }
}
function cleanCall(c){
  if(!c||!/^[a-f0-9]{32}$/.test(c.id)||typeof c.model!=='string'||typeof c.role!=='string'||!Number.isFinite(c.startedAt)||c.startedAt>Date.now()+60000)return null;
  const number=n=>Number.isSafeInteger(n)&&n>=0?n:null;
  return {id:c.id,model:c.model.slice(0,160),role:c.role.slice(0,80),node_id:typeof c.node_id==='string'?c.node_id.slice(0,128):null,
    text:typeof c.text==='string'?c.text.slice(0,retention.reasoning_chars):'',status:['generating','accepted','withheld','interrupted'].includes(c.status)?c.status:'interrupted',
    startedAt:c.startedAt,updatedAt:Number.isFinite(c.updatedAt)?c.updatedAt:c.startedAt,finishedAt:Number.isFinite(c.finishedAt)?c.finishedAt:undefined,
    requested:typeof c.requested==='string'?c.requested.slice(0,80):typeof c.requested==='boolean'?c.requested:c.requested===null?null:undefined,
    thinking_chars:number(c.thinking_chars),
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
module.exports={decoder,ActivityLog,ActivityArchive,activityUri,parseActivityUri,chatActivity};
