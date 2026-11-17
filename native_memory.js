'use strict';
// Native chat can move assistant output into a user-role summary during
// compaction. Carry an authenticated receipt pointer, never a global last chat.
const crypto=require('node:crypto');
const KEY='karma.nativeMemory.v1';
const SUMMARY='Summarize the conversation history so far, paying special attention to the most recent agent commands and tool results that triggered this summarization.';
function text(vscode,message){return (message?.content||[]).filter(p=>p instanceof vscode.LanguageModelTextPart).map(p=>p.value).join('');}
function isCompaction(vscode,messages){
  const last=messages.at(-1);
  return last?.role===vscode.LanguageModelChatMessageRole.User&&text(vscode,last).trimStart().startsWith(SUMMARY);
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
  signature(id){return crypto.createHmac('sha256',this.data.secret).update('karma-native-context-v1:'+id).digest('hex');}
  valid(id,sig){return /^[a-f0-9]{32}$/.test(id)&&/^[a-f0-9]{64}$/.test(sig)&&crypto.timingSafeEqual(Buffer.from(sig,'hex'),Buffer.from(this.signature(id),'hex'));}
  taskStack(parent){return parent&&this.data.taskStacks?.[parent];}
  async remember(id,options,stackChat){
    if(!/^[a-f0-9]{32}$/.test(id))throw Error('Invalid conversation receipt identity');
    const key=this.chatKey(options);if(key)this.data.chats[key]=id;
    if(stackChat){
      this.data.taskStacks??={};
      delete this.data.taskStacks[id];
      this.data.taskStacks[id]=stackChat;
      const ids=Object.keys(this.data.taskStacks);
      if(ids.length>1024)delete this.data.taskStacks[ids[0]];
    }
    await this.storage?.update(KEY,this.data);
    return this.signature(id);
  }
  resolve(vscode,messages,options,legacyParent){
    const key=this.chatKey(options),bound=key&&this.data.chats[key];
    // Never treat a marker pasted in the current user prompt as a checkpoint.
    for(const message of messages.slice(0,-1).reverse()){
      const content=text(vscode,message),matches=[...content.matchAll(/KARMA_CONTEXT_V1:([a-f0-9]{32}):([a-f0-9]{64})|karma-preview:\/conversation\/([a-f0-9]{32})\/Response\.md\?checkpoint=([a-f0-9]{64})/g)];
      for(const match of matches.reverse()){
        const id=match[1]||match[3],sig=match[2]||match[4];
        if(this.valid(id,sig))return {parent:id,source:'signed_checkpoint'};
      }
    }
    if(bound)return {parent:bound,source:'chat_binding'};
    return {parent:legacyParent,source:legacyParent?'assistant_receipt':'new_conversation'};
  }
  summary(id){return '<summary>\nSutra conversation context is retained in the local receipt ledger.\nKARMA_CONTEXT_V1:'+id+':'+this.signature(id)+'\nThe next governed call must revalidate this pointer. It grants no workspace or network permissions.\n</summary>';}
}
module.exports={NativeMemory,isCompaction,SUMMARY};
