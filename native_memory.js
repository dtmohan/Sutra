'use strict';
// Native chat can move assistant output into a user-role summary during
// compaction. Carry an authenticated receipt pointer, never a global last chat.
const crypto=require('node:crypto');
const KEY='karma.nativeMemory.v1';
const SUMMARY='Summarize the conversation history so far, paying special attention to the most recent agent commands and tool results that triggered this summarization.';
const COMPRESSED='The following is a compressed version of the preceeding history in the current conversation.';
function text(vscode,message){return (message?.content||[]).filter(p=>p instanceof vscode.LanguageModelTextPart).map(p=>p.value).join('');}
function isCompaction(vscode,messages){
  const last=messages.at(-1);
  return last?.role===vscode.LanguageModelChatMessageRole.User&&text(vscode,last).trimStart().startsWith(SUMMARY);
}
function checkpointText(vscode,message){
  const content=text(vscode,message).trim();
  const roles=vscode.LanguageModelChatMessageRole;
  if(message.role===roles.Assistant)return content;
  if(message.role!==roles.User)return '';
  if(/^<summary>[\s\S]*<\/summary>$/.test(content))return content;
  if(!content.startsWith(COMPRESSED)){
    return content.match(/^<conversation-summary>\s*([\s\S]*?)<\/conversation-summary>/)?.[1]||'';
  }
  return [...content.matchAll(/<(user|assistant|conversation-summary|tool)>\s*([\s\S]*?)<\/\1>/g)]
    .filter(match=>match[1]==='assistant'||match[1]==='conversation-summary').map(match=>match[2]).join('\n');
}
class NativeMemory{
  constructor(context){
    this.storage=context.workspaceState;
    const saved=this.storage?.get(KEY);
    this.data=saved&&/^[a-f0-9]{64}$/.test(saved.secret)?structuredClone(saved):{secret:crypto.randomBytes(32).toString('hex'),chats:{}};
  }
  chatKey(options){
    // Optional compatibility hint supplied by the installed VS Code harness.
    // Correctness also works without this private hint, using signed pointers.
    const id=options?.modelOptions?._conversationId;
    return typeof id==='string'&&id.length>0&&id.length<=512?crypto.createHash('sha256').update(id).digest('hex'):undefined;
  }
  reportSectionReceipts(axis){
    if(!/^[a-f0-9]{64}$/.test(axis||''))return [];
    const saved=this.data.reportSections?.[axis];
    return Array.isArray(saved)?saved:[];
  }
  async rememberReportSection(axis,id){
    if(!/^[a-f0-9]{64}$/.test(axis||''))return;
    const receipts=this.reportSectionReceipts(axis);
    if(receipts.includes(id))return;
    this.data.reportSections??={};
    delete this.data.reportSections[axis];
    this.data.reportSections[axis]=[...receipts,id].slice(-128);
    const axes=Object.keys(this.data.reportSections);
    if(axes.length>32)delete this.data.reportSections[axes[0]];
    await this.storage?.update(KEY,this.data);
  }
  taskStack(parent){return parent&&this.data.taskStacks?.[parent];}
  signature(id,axis=this.taskStack(id)){
    const subject=axis?'karma-native-context-v2:'+axis+':'+id:'karma-native-context-v1:'+id;
    return crypto.createHmac('sha256',this.data.secret).update(subject).digest('hex');
  }
  valid(id,sig){
    if(!/^[a-f0-9]{32}$/.test(id)||!/^[a-f0-9]{64}$/.test(sig))return false;
    const actual=Buffer.from(sig,'hex');
    return [this.signature(id),this.signature(id,null)].some(expected=>
      crypto.timingSafeEqual(actual,Buffer.from(expected,'hex')));
  }
  async remember(id,options,stackChat){
    if(!/^[a-f0-9]{32}$/.test(id))throw Error('Invalid conversation receipt identity');
    const key=this.chatKey(options),axis=stackChat||key,owner=this.taskStack(id);
    if((key&&key!==axis)||(owner&&owner!==axis))throw Error('Receipt belongs to another Sutra chat');
    if(key)this.data.chats[key]=id;
    if(axis){
      this.data.taskStacks??={};
      delete this.data.taskStacks[id];
      this.data.taskStacks[id]=axis;
      const ids=Object.keys(this.data.taskStacks);
      if(ids.length>1024)delete this.data.taskStacks[ids[0]];
    }
    await this.storage?.update(KEY,this.data);
    return this.signature(id);
  }
  resolve(vscode,messages,options,legacyParent){
    const key=this.chatKey(options),bound=key&&this.data.chats[key];
    const sameAxis=id=>{
      const owner=this.taskStack(id);
      return !!key&&(owner?owner===key:bound===id);
    };
    // The host chat ID is authoritative when available. A signed marker in
    // a recognized transcript is a continuity credential for older hosts.
    for(const message of messages.slice(0,-1).reverse()){
      const content=checkpointText(vscode,message);
      const matches=[...content.matchAll(/KARMA_CONTEXT_V1:([a-f0-9]{32}):([a-f0-9]{64})|karma-preview:\/conversation\/([a-f0-9]{32})\/Response\.md\?checkpoint=([a-f0-9]{64})/g)];
      for(const match of matches.reverse()){
        const id=match[1]||match[3],sig=match[2]||match[4];
        if((!key||sameAxis(id))&&this.valid(id,sig))return {parent:id,source:'signed_checkpoint'};
      }
    }
    if(bound&&sameAxis(bound))return {parent:bound,source:'chat_binding'};
    if(legacyParent&&sameAxis(legacyParent))return {parent:legacyParent,source:'assistant_receipt'};
    return {parent:undefined,source:'new_conversation'};
  }
  summary(id){return '<summary>\nSutra conversation context is retained in the local receipt ledger.\nKARMA_CONTEXT_V1:'+id+':'+this.signature(id)+'\nThe next governed call must revalidate this pointer. It grants no workspace or network permissions.\n</summary>';}
}
module.exports={NativeMemory,isCompaction,SUMMARY};
