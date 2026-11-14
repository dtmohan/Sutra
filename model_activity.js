'use strict';
const {StringDecoder}=require('node:string_decoder');
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
module.exports={decoder,ActivityLog};
