'use strict';
const assert=require('node:assert/strict'),crypto=require('node:crypto');
const {latestRequest}=require('../native_provider');
const {NativeMemory}=require('../native_memory');
const pkg=require('../package.json');
class Text{constructor(value){this.value=value;}}
const vscode={LanguageModelTextPart:Text,LanguageModelToolResultPart:class{},LanguageModelChatMessageRole:{User:1,Assistant:2}};
const id='a'.repeat(32),secret='b'.repeat(64);
const oldSignature=crypto.createHmac('sha256',secret).update('karma-native-context-v1:'+id).digest('hex');
const saved={secret,chats:{}};
const memory=new NativeMemory({workspaceState:{get:key=>{assert.equal(key,'karma.nativeMemory.v1');return saved;}}});
for(const label of ['Karma','Sutra']){
 const history=[{role:2,content:[new Text(`[${label} · conversation details](karma-preview:/conversation/${id}/Response.md)`)]},{role:1,content:[new Text('Continue')]}];
 assert.equal(latestRequest(vscode,history).parent,id);
 history[0].content=[new Text(`[${label} details](karma-preview:/conversation/${id}/Response.md?checkpoint=${oldSignature})`)];
 assert.equal(memory.resolve(vscode,history,{}).parent,id);
}
assert.equal(memory.signature(id),oldSignature);
assert.equal(memory.resolve(vscode,[{role:1,content:[new Text(`<conversation-summary>KARMA_CONTEXT_V1:${id}:${oldSignature}</conversation-summary>`)]},{role:1,content:[new Text('Continue')]}],{}).parent,id);
assert.equal(pkg.displayName,'Sutra');
assert.equal(pkg.name,'karma-local-harness');assert.equal(pkg.publisher,'deepak-local');
assert.equal(pkg.contributes.chatParticipants[0].name,'sutra');
assert(pkg.contributes.commands.every(c=>c.command.startsWith('karma.')&&c.title.startsWith('Sutra:')));
assert(Object.keys(pkg.contributes.configuration.properties).every(k=>k.startsWith('karma.')));
console.log('Passed: legacy and Sutra receipt labels, signed checkpoints, settings identity and new public branding.');
