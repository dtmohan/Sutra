'use strict';
const assert=require('node:assert/strict');
const {decoder,ActivityLog}=require('../model_activity');
const events=[],logs=[],d=decoder(e=>events.push(e),s=>logs.push(s));
const send=e=>d.write(Buffer.from('SUTRA_EVENT '+JSON.stringify(e)+'\n'));
send({kind:'thinking',id:'forged',seq:1,text:'cannot start a call'});
send({kind:'start',id:'a',seq:1,model:'small',role:'subagent',node_id:'child-1'});
const unicode=Buffer.from('SUTRA_EVENT '+JSON.stringify({kind:'thinking',id:'a',seq:2,text:'🧵 reasoning'})+'\n');
for(const b of unicode)d.write(Buffer.from([b]));
send({kind:'thinking',id:'a',seq:2,text:'duplicate'});
send({kind:'thinking',id:'a',seq:3,model:'forged',node_id:'parent',text:'next'});
send({kind:'grant',id:'a',seq:4});
send({kind:'finish',id:'a',seq:5,status:'accepted',thinking_chars:14,usage:{input_tokens:20,output_tokens:30}});
send({kind:'thinking',id:'a',seq:6,text:'late'});
send({kind:'start',id:'b',seq:1,model:'large',role:'subagent',node_id:'child-2'});d.close();
assert.equal(events.length,6);assert.equal(events[1].text,'🧵 reasoning');
assert.equal(events[2].model,'small');assert.equal(events[2].node_id,'child-1');
assert.equal(events.at(-1).status,'interrupted');assert.equal(events.at(-1).usage.output_tokens,null);
const log=new ActivityLog();events.forEach(e=>log.accept(e));
assert.equal(log.calls.length,2);assert.equal(log.calls[0].text,'🧵 reasoningnext');assert.equal(log.calls[1].model,'large');
assert.equal(log.calls[0].thinking_chars,14);
for(let i=0;i<20;i++)log.accept({kind:'start',id:String(i)});
assert.equal(log.calls.length,12);log.clear();assert.equal(log.calls.length,0);
const oversized=decoder(()=>assert.fail('oversized frame accepted'),()=>{});oversized.write(Buffer.from('SUTRA_EVENT '+ 'x'.repeat(70000)+'\n'));oversized.end();
console.log('PASS bounded activity framing, Unicode, identity, sequence, interruption, model switching and retention');

// Metadata-only calls must never create a VS Code "Finished thinking" block.
{
 const {chatActivity}=require('../model_activity');
 class Text{constructor(value){this.value=value;}}
 class Thinking{constructor(value,id,metadata){Object.assign(this,{value,id,metadata});}}
 const parts=[],v={LanguageModelTextPart:Text,LanguageModelThinkingPart:Thinking};
 const emit=chatActivity(v,{report:p=>parts.push(p)},e=>'karma-preview:/'+e.id);
 emit({kind:'start',id:'router',model:'small',role:'conversation'});
 emit({kind:'control',id:'router',requested:false});
 emit({kind:'finish',id:'router',status:'accepted',thinking_chars:0});
 assert.equal(parts.filter(p=>p instanceof Thinking).length,0);
 assert(parts.some(p=>p.value.includes('call details')));
 assert(parts.some(p=>p.value.includes('No separate reasoning text received.')&&p.value.includes('off requested')));
 // Finish and late chunks cannot reopen the same call or fabricate reasoning.
 const count=parts.length;emit({kind:'thinking',id:'router',text:'late'});emit({kind:'finish',id:'router'});assert.equal(parts.length,count);
 emit({kind:'start',id:'missing',model:'large',role:'subagent'});
 emit({kind:'finish',id:'missing',status:'withheld',thinking_chars:20});
 assert(parts.some(p=>p.value.includes('reported but no text was received')));
 emit({kind:'start',id:'interrupted',model:'large',role:'subagent'});
 emit({kind:'finish',id:'interrupted',status:'interrupted'});
 assert(parts.some(p=>p.value.includes('before interruption')));
 assert.equal(parts.filter(p=>p instanceof Thinking).length,0);
 delete v.LanguageModelThinkingPart;
 emit({kind:'start',id:'old-host',model:'large',role:'subagent'});
 emit({kind:'thinking',id:'old-host',text:'PRIVATE DIAGNOSTIC'});
 emit({kind:'finish',id:'old-host',status:'accepted'});
 assert(parts.some(p=>p.value.includes('details are available in Sutra Monitor')));
 assert(!parts.some(p=>p.value.includes('PRIVATE DIAGNOSTIC')));
 console.log('PASS reasoning UI requires emitted text; absent, missing, interrupted and old-host streams are distinguished');
}

const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {ActivityArchive,activityUri,parseActivityUri}=require('../model_activity');
const temp=fs.mkdtempSync(path.join(os.tmpdir(),'sutra-reasoning-'));
const key='a'.repeat(24),dir=path.join(temp,key),id=n=>n.toString(16).padStart(32,'0');
try{
 const archive=new ActivityArchive(dir);
 for(const [n,role,model] of [[1,'conversation','small'],[2,'subagent','large'],[3,'chat_reviewer','mid']]){
  archive.accept({kind:'start',id:id(n),role,model,node_id:'task-'+n});
  archive.accept({kind:'thinking',id:id(n),text:'Unverified '+role+' 🧵 ``` <script>do not execute</script>'});
  archive.accept({kind:'finish',id:id(n),status:n===2?'withheld':'accepted',usage:{input_tokens:10,output_tokens:30}});
 }
 const restored=new ActivityArchive(dir);
 assert.equal(restored.calls.length,3);assert.equal(restored.calls[1].model,'large');assert.equal(restored.calls[1].status,'withheld');
 assert(restored.document(id(2)).includes('Unverified subagent'));assert(!restored.document(id(2)).includes('Unverified conversation'));
 archive.accept({kind:'start',id:id(40),role:'conversation',model:'small'});
 archive.accept({kind:'control',id:id(40),requested:false});
 archive.accept({kind:'finish',id:id(40),status:'accepted',thinking_chars:0});
 const noThought=new ActivityArchive(dir);
 assert(noThought.document(id(40)).includes('Reasoning characters reported: 0'));
 assert(noThought.document(id(40)).includes('Reasoning control: off requested'));
 assert(noThought.document(id(40)).includes('No separate reasoning text received.'));
 archive.accept({kind:'start',id:id(41),role:'subagent',model:'large'});
 archive.accept({kind:'finish',id:id(41),status:'withheld',thinking_chars:25});
 assert(new ActivityArchive(dir).document(id(41)).includes('its text is unavailable'));
 const uri=activityUri(dir,id(2));assert.deepEqual(parseActivityUri(uri),{workspace:key,id:id(2)});
 for(const u of [uri+'/../secret',uri+'?path=/secret','karma-preview:/activity/../Reasoning.txt'])assert.equal(parseActivityUri(u),null);
 assert.equal(activityUri(dir,'../secret'),null);
 archive.accept({kind:'start',id:id(4),role:'subagent',model:'large'});
 archive.accept({kind:'thinking',id:id(4),text:'Partial before restart'});archive.dispose();
 const interrupted=new ActivityArchive(dir);assert.equal(interrupted.calls.at(-1).status,'interrupted');assert(interrupted.calls.at(-1).interruptedOnReload);
 assert.equal(interrupted.calls.at(-1).text,'Partial before restart');
 for(let n=5;n<30;n++){
  interrupted.accept({kind:'start',id:id(n),role:'subagent',model:'small',prompt:'PRIVATE PROMPT NOT RETAINED'});
  interrupted.accept({kind:'thinking',id:id(n),text:'\u0000'.repeat(40000)});
  interrupted.accept({kind:'finish',id:id(n),status:'accepted',usage:{input_tokens:1,output_tokens:2}});
 }
 assert(interrupted.calls.length<=12);assert(fs.statSync(interrupted.file).size<=1024*1024);
 assert(interrupted.calls.every(c=>c.text.length<=16000));assert(!fs.readFileSync(interrupted.file,'utf8').includes('PRIVATE PROMPT'));
 if(process.platform!=='win32')assert.equal(fs.statSync(interrupted.file).mode&0o777,0o600);
 assert(interrupted.document(id(1)).includes('unavailable'));
 const saved=JSON.parse(fs.readFileSync(interrupted.file));saved.calls[0].startedAt=Date.now()-8*86400000;
 fs.writeFileSync(interrupted.file,JSON.stringify(saved));assert.equal(new ActivityArchive(dir).calls.length,saved.calls.length-1);
 fs.writeFileSync(interrupted.file,'corrupt');const corrupt=new ActivityArchive(dir);assert.equal(corrupt.calls.length,0);assert(corrupt.error);
 fs.writeFileSync(interrupted.file,'x'.repeat(1024*1024+1));assert(new ActivityArchive(dir).error);
 fs.unlinkSync(interrupted.file);const secret=path.join(temp,'secret');fs.writeFileSync(secret,'untouched');fs.symlinkSync(secret,interrupted.file);
 assert(new ActivityArchive(dir).error);interrupted.flush();assert.equal(fs.readFileSync(secret,'utf8'),'untouched');assert(!fs.lstatSync(interrupted.file).isSymbolicLink());
 const blocked=path.join(temp,'blocked');fs.writeFileSync(blocked,'file');const fail=new ActivityArchive(blocked);
 assert.doesNotThrow(()=>fail.accept({kind:'start',id:id(50),role:'subagent',model:'small'}));assert(fail.error);assert.equal(fail.calls.length,1);
 const isolated=new ActivityArchive(path.join(temp,'b'.repeat(24)));assert(isolated.document(id(2)).includes('unavailable'));
 console.log('PASS persistent per-call diagnostics: reload, stage/model isolation, interruptions, count/age/byte bounds, privacy, unsafe files and failure isolation');
}finally{fs.rmSync(temp,{recursive:true,force:true});}

(async()=>{
 const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'sutra-reasoning-view-'));
 let current=path.join(tmp,'a'.repeat(24));const changes=[],opened=[],sent=[],commands=[];
 let receive,configChanged,preference='model-default',tierReasoning={};
 const fake={StatusBarAlignment:{Right:1},ViewColumn:{Beside:2},Uri:{file:p=>({fsPath:p}),parse:s=>({toString:()=>s})},
  window:{createStatusBarItem:()=>({show(){},dispose(){}}),registerWebviewViewProvider:(name,p)=>{
   p.resolveWebviewView({visible:true,webview:{asWebviewUri:u=>({toString:()=>u.fsPath}),postMessage:m=>sent.push(m),onDidReceiveMessage:cb=>{receive=cb;}},onDidChangeVisibility(){},onDidDispose(){}});return {dispose(){}};
  },showTextDocument:async(doc,options)=>opened.push({doc,options})},
  commands:{registerCommand:()=>({dispose(){}}),executeCommand:async(...args)=>commands.push(args)},workspace:{getConfiguration:()=>({get:key=>key==='tierReasoning'?tierReasoning:preference}),onDidChangeConfiguration:cb=>{configChanged=cb;return {dispose(){}};},openTextDocument:async uri=>({uri,text:view.activityDocument(uri.toString())})}};
 const context={extensionPath:path.resolve(__dirname,'..'),subscriptions:[]};
 const {install}=require('../usage_view');
 const deps={storage:()=>current,refresh:()=>{},activityChanged:u=>changes.push(u.toString())};
 let view=install(fake,context,deps);receive({type:'ready'});
 assert.equal(sent.at(-1).reasoningPreference,'model-default');
 preference='adaptive';configChanged({affectsConfiguration:key=>key==='karma.thinking'});
 assert.equal(sent.at(-1).reasoningPreference,'adaptive');
 tierReasoning={low:'off',mid:'inherit',high:'on'};configChanged({affectsConfiguration:key=>key==='karma.tierReasoning'});
 assert.deepEqual(sent.at(-1).tierReasoning,tierReasoning);
 assert.equal(sent.at(-1).reasoningPreference,'adaptive');
 await receive({type:'reasoning_settings'});assert.deepEqual(commands.at(-1),['karma.configureReasoning']);
 view.activity({kind:'start',id:id(60),model:'small',role:'conversation'});
 view.activity({kind:'thinking',id:id(60),text:'Router details'});
 view.activity({kind:'finish',id:id(60),status:'accepted'});
 view.activity({kind:'start',id:id(61),model:'large',role:'subagent',node_id:'child-node'});
 view.activity({kind:'thinking',id:id(61),text:'Child retained details'});
 view.activity({kind:'finish',id:id(61),status:'accepted'});
 const childUri=view.activityLink({id:id(61)});assert(changes.includes(childUri));
 view.reset();assert.equal(sent.at(-1).activity.length,2);
 await receive({type:'activity_details',id:id(61)});await new Promise(r=>setImmediate(r));
 assert.equal(opened[0].options.viewColumn,2);assert(opened[0].doc.text.includes('Child retained details'));
 assert(!opened[0].doc.text.includes('Router details'));
 context.subscriptions.forEach(s=>s.dispose());
 view=install(fake,{...context,subscriptions:[]},deps);receive({type:'ready'});
 assert.equal(sent.at(-1).activity.length,2);assert(view.activityDocument(childUri).includes('Child retained details'));
 current=path.join(tmp,'b'.repeat(24));view.reset();assert.equal(sent.at(-1).activity.length,0);
 assert(view.activityDocument(childUri).includes('different workspace'));
 assert(view.activityDocument('karma-preview:/activity/../../private').includes('different workspace'));
 console.log('PASS Monitor/archive integration: call links, plain read-only documents, live refresh, reload retention and workspace isolation');
 fs.rmSync(tmp,{recursive:true,force:true});
})().catch(e=>{console.error(e);process.exitCode=1;});
