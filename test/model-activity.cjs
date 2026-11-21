'use strict';
const assert=require('node:assert/strict');
const {decoder,ActivityLog}=require('../model_activity');
const progressMetrics=require('../media/generation_progress');
const progressSnapshot=(extra={})=>({profile:'literal-generation-progress-v1',received_chars:4096,observed_chars:4096,
 omitted_chars:0,invalid_chars:0,invalid_feeds:0,long_units:32,duplicate_units:28,novel_units:4,recent_units:32,
 recent_duplicate_fraction:.875,recent_novelty_fraction:.125,repetition_indicator:true,current_repetition:true,
 alert:true,alert_emitted:true,analysis_complete:true,pending_chars:0,finished:false,semantic_assessment:'unassessed',
 limits:{...progressMetrics.limits},...extra});
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

// The literal sensor is a bounded observation. It cannot create an action or
// smuggle source text, forged counts or model identities through diagnostics.
{
 const good=progressSnapshot({patterns:['PRIVATE PATTERN'],text:'PRIVATE TEXT',permissions:{write:true},
  kernel:{adapter:'karma',profile:'generation-progress-observation-v1',profile_version:1,decision:'BRIDGE',
   omega:.3,sigma:0,k:.15,sigma_clause:1,observation_only:true,semantic_correctness_verified:false,grant:true}});
 const clean=progressMetrics.normalize(good);assert(clean);assert.equal(clean.kernel.observation_only,true);
 assert(!JSON.stringify(clean).includes('PRIVATE'));assert(!JSON.stringify(clean).includes('permissions'));assert(!('grant' in clean.kernel));
 for(const extra of [
  {received_chars:4095},{duplicate_units:33},{recent_units:33},{pending_chars:4097},
  {recent_duplicate_fraction:.5},{recent_novelty_fraction:NaN},{long_units:true},{invalid_chars:-1},
  {finished:true,pending_chars:1},{analysis_complete:false},{repetition_indicator:false},
  {semantic_assessment:'correct'},{limits:{...progressMetrics.limits,recent_units:64}},
  {limits:{...progressMetrics.limits,raw:'PRIVATE'}}
 ])assert.equal(progressMetrics.normalize(progressSnapshot(extra)),null,JSON.stringify(extra));
 assert.equal(progressMetrics.normalize(undefined),null);
 const seen=[],reader=decoder(e=>seen.push(e),()=>{}),emit=e=>reader.write(Buffer.from('SUTRA_EVENT '+JSON.stringify(e)+'\n'));
 emit({kind:'progress',id:'forged',seq:1,generation_progress:good});
 emit({kind:'start',id:'p',seq:1,model:'real-model',role:'subagent',node_id:'real-task'});
 emit({kind:'thinking',id:'p',seq:2,text:'Actual commentary.'});
 emit({kind:'progress',id:'p',seq:900,generation_progress:{...good,received_chars:0}});
 emit({kind:'progress',id:'p',seq:3,model:'forged',node_id:'other-task',generation_progress:good});
 emit({kind:'progress',id:'p',seq:4,generation_progress:good});
 emit({kind:'thinking',id:'p',seq:4,text:' Continued.'});
 const completed=progressSnapshot({alert:false,finished:true,current_repetition:false,long_units:40,duplicate_units:28,novel_units:12,
  recent_duplicate_fraction:.625,recent_novelty_fraction:.375});
 emit({kind:'finish',id:'p',seq:5,status:'accepted',generation_progress:completed});
 emit({kind:'progress',id:'p',seq:6,generation_progress:good});
 assert.deepEqual(seen.map(e=>e.kind),['start','thinking','progress','thinking','finish']);
 assert.equal(seen[2].model,'real-model');assert.equal(seen[2].node_id,'real-task');
 assert.equal(seen[2].generation_progress.current_repetition,true);
 assert.equal(seen[4].generation_progress.current_repetition,false);
 const log=new ActivityLog();seen.forEach(e=>log.accept(e));
 assert.equal(log.calls[0].generation_progress.finished,true);
 assert.equal(log.calls[0].text,'Actual commentary. Continued.');
 log.accept({kind:'progress',id:'p',seq:7,generation_progress:good});
 assert.equal(log.calls[0].generation_progress.finished,true);
 const capped=[],bounded=decoder(e=>capped.push(e),()=>{});
 bounded.write(Buffer.from('SUTRA_EVENT '+JSON.stringify({kind:'start',id:'bounded',seq:1,model:'m',role:'subagent'})+'\n'));
 bounded.write(Buffer.from('SUTRA_EVENT '+JSON.stringify({kind:'progress',id:'bounded',seq:2,generation_progress:{...good,raw:'x'.repeat(70000)}})+'\n'));
 bounded.write(Buffer.from('SUTRA_EVENT '+JSON.stringify({kind:'finish',id:'bounded',seq:2,status:'accepted'})+'\n'));
 assert.deepEqual(capped.map(e=>e.kind),['start','finish'],'Oversized progress must not consume a valid finish sequence');
 console.log('PASS strict literal-progress snapshots, namespace filtering, call identity, malformed sequence isolation, one alert and finish metrics');
}

// The concurrency bound must not become a lifetime limit for a persistent bridge.
{
 const seen=[],reader=decoder(e=>seen.push(e),()=>{}),send=e=>reader.write(Buffer.from('SUTRA_EVENT '+JSON.stringify(e)+'\n'));
 for(let n=0;n<65;n++)send({kind:'start',id:'pending-'+n,seq:1,model:'m',role:'subagent'});
 assert.equal(seen.length,64,'At most64 unfinished calls are accepted');
 send({kind:'finish',id:'pending-0',seq:2,status:'accepted'});
 send({kind:'start',id:'pending-64',seq:1,model:'replacement',role:'subagent'});
 assert.equal(seen.filter(e=>e.kind==='start').length,65,'Finishing one call releases its active slot');
 const count=seen.length;
 send({kind:'start',id:'pending-0',seq:1,model:'replayed',role:'subagent'});
 send({kind:'progress',id:'pending-0',seq:100,generation_progress:progressSnapshot()});
 assert.equal(seen.length,count,'Recent completed identities cannot reopen or emit late progress');
 reader.close();const closed=seen.length;reader.close();
 send({kind:'start',id:'after-close',seq:1,model:'m',role:'subagent'});reader.end();
 assert.equal(seen.length,closed,'Closing is idempotent and rejects all later events');
 assert.equal(seen.filter(e=>e.kind==='finish'&&e.status==='interrupted').length,64);
 console.log('PASS concurrent activity bound, released slots, recent replay protection and closed-decoder finality');
}

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
 assert.deepEqual(parts,[], 'A thinking-off call must not insert host diagnostic chatter into the answer');
 // Finish and late chunks cannot reopen the same call or fabricate reasoning.
 const count=parts.length;emit({kind:'thinking',id:'router',text:'late'});emit({kind:'finish',id:'router'});assert.equal(parts.length,count);
 emit({kind:'start',id:'missing',model:'large',role:'subagent'});
 emit({kind:'finish',id:'missing',status:'withheld',thinking_chars:20});
 assert.deepEqual(parts,[], 'A positive character count without emitted text is not a thinking stream');
 emit({kind:'start',id:'interrupted',model:'large',role:'subagent'});
 emit({kind:'finish',id:'interrupted',status:'interrupted'});
 assert.deepEqual(parts,[], 'Interruption without reasoning must remain quiet inline');
 emit({kind:'start',id:'visible',model:'large',role:'subagent',node_id:'child-1'});
 emit({kind:'control',id:'visible',requested:'low'});
 emit({kind:'thinking',id:'visible',text:'   '});
 assert.deepEqual(parts,[], 'Whitespace cannot create a fake thinking block');
 emit({kind:'thinking',id:'visible',text:'Real model deliberation.'});
 emit({kind:'thinking',id:'visible',text:' More evidence.'});
 emit({kind:'finish',id:'visible',status:'accepted',usage:{input_tokens:17,output_tokens:29}});
 assert(parts.every(p=>p instanceof Thinking), 'Host metadata and reasoning must not leak into ordinary answer text');
 assert(parts.some(p=>p.value.includes('Child review · large')&&p.value.includes('Reasoning control: low')));
 assert(parts.some(p=>p.value==='Real model deliberation.'));
 assert(parts.some(p=>p.value===' More evidence.'));
 assert(parts.some(p=>p.value.includes('Protocol: checks passed')&&p.value.includes('17 input / 29 output tokens')));
 assert.equal(parts.at(-1).metadata.vscode_reasoning_done,true);
 const completedCount=parts.length;
 emit({kind:'thinking',id:'visible',text:'late private detail'});
 assert.equal(parts.length,completedCount);
 delete v.LanguageModelThinkingPart;
 emit({kind:'start',id:'old-host',model:'large',role:'subagent'});
 emit({kind:'thinking',id:'old-host',text:'PRIVATE DIAGNOSTIC'});
 emit({kind:'finish',id:'old-host',status:'accepted'});
 assert.equal(parts.length,completedCount, 'An old host must use the archive instead of exposing reasoning as answer text');
 assert(!parts.some(p=>p.value.includes('PRIVATE DIAGNOSTIC')));
 console.log('PASS quiet metadata-only calls, authentic inline thinking with protocol details, and old-host privacy');
}

// Host progress text is distinct from model reasoning and appears at most once.
{
 const {chatActivity}=require('../model_activity');
 class Text{constructor(value){this.value=value;}}
 class Thinking{constructor(value,id,metadata){Object.assign(this,{value,id,metadata});}}
 const parts=[],emit=chatActivity({LanguageModelTextPart:Text,LanguageModelThinkingPart:Thinking},{report:p=>parts.push(p)});
 emit({kind:'progress',id:'unknown',seq:2,generation_progress:progressSnapshot()});assert.equal(parts.length,0);
 emit({kind:'start',id:'live',seq:1,model:'model',role:'subagent'});
 emit({kind:'thinking',id:'live',seq:2,text:'PRIVATE MODEL COMMENTARY'});
 emit({kind:'progress',id:'live',seq:3,generation_progress:progressSnapshot()});
 emit({kind:'progress',id:'live',seq:4,generation_progress:progressSnapshot()});
 emit({kind:'finish',id:'live',seq:5,status:'accepted',generation_progress:progressSnapshot({alert:false,finished:true})});
 assert.equal(parts.filter(p=>p instanceof Text).length,1);
 assert(parts.find(p=>p instanceof Text).value.includes('literal signal; correctness unverified'));
 assert(!parts.find(p=>p instanceof Text).value.includes('PRIVATE MODEL COMMENTARY'));
 assert(parts.some(p=>p instanceof Thinking&&p.value==='PRIVATE MODEL COMMENTARY'));
 const count=parts.length;emit({kind:'progress',id:'live',seq:6,generation_progress:progressSnapshot()});assert.equal(parts.length,count);
 const fallback=[],finishOnly=chatActivity({LanguageModelTextPart:Text},{report:p=>fallback.push(p)});
 finishOnly({kind:'start',id:'finish-only',seq:1,model:'model',role:'subagent'});
 finishOnly({kind:'finish',id:'finish-only',seq:2,status:'accepted',generation_progress:progressSnapshot({alert:false,finished:true})});
 assert.equal(fallback.length,1);assert(fallback[0] instanceof Text);
 const legacy=[],legacyEmit=chatActivity({LanguageModelTextPart:Text},{report:p=>legacy.push(p)});
 legacyEmit({kind:'start',id:'legacy',model:'model',role:'conversation'});
 legacyEmit({kind:'progress',id:'legacy',seq:2,generation_progress:progressSnapshot()});
 legacyEmit({kind:'finish',id:'legacy',status:'accepted'});assert.equal(legacy.length,0);
 const broken=chatActivity({LanguageModelTextPart:Text},{report(){throw Error('broken diagnostic display');}});
 broken({kind:'start',id:'broken',seq:1,model:'model',role:'subagent'});
 assert.doesNotThrow(()=>broken({kind:'progress',id:'broken',seq:2,generation_progress:progressSnapshot()}));
 console.log('PASS one inert host repetition notice, authentic thinking separation, final-snapshot fallback, legacy quietness and diagnostic failure isolation');
}

const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {ActivityArchive,activityUri,parseActivityUri}=require('../model_activity');
{
 const {chatActivity,controlLabel}=require('../model_activity');
 const remoteId='d'.repeat(32),received=[],rendered=[];
 const label='model default; thinking-only; off switch unsupported';
 class Thinking{constructor(value,id,metadata){Object.assign(this,{value,id,metadata});}}
 const show=chatActivity({LanguageModelThinkingPart:Thinking},{report:part=>rendered.push(part)});
 const stream=decoder(event=>{received.push(event);show(event);},()=>{});
 const feed=event=>stream.write(Buffer.from('SUTRA_EVENT '+JSON.stringify(event)+'\n'));
 feed({kind:'start',id:remoteId,seq:1,model:'sutra:remote/rtx3090',role:'conversation'});
 feed({kind:'control',id:remoteId,seq:2,requested:false,effective:'model_default',switch_supported:false,
       thinking_only:true,arbitrary:'PRIVATE CONTROL FIELD',policy:{network_allowed:true}});
 feed({kind:'thinking',id:remoteId,seq:3,text:'Actual received model commentary.'});
 feed({kind:'finish',id:remoteId,seq:4,status:'accepted',thinking_chars:33,usage:{input_tokens:5,output_tokens:10}});
 assert.equal(received[1].effective,'model_default');assert.equal(received[1].switch_supported,false);
 assert.equal(received[1].thinking_only,true);assert.equal(received[1].requested,false);
 assert(!('arbitrary' in received[1]));assert(!('policy' in received[1]));
 assert(rendered[0].value.includes('Unverified model reasoning.'));
 assert(rendered[0].value.includes('Reasoning control: '+label));
 assert(!rendered[0].value.includes('Reasoning control: off requested'));
 assert.equal(controlLabel(false),'off requested');assert.equal(controlLabel(true),'on requested');
 assert.equal(controlLabel(null),'model default');assert.equal(controlLabel('low'),'low');
 assert.equal(controlLabel(false,{effective:'model_default',switch_supported:false,thinking_only:true}),label);
 assert.equal(controlLabel(false,{effective:'off',switch_supported:false,thinking_only:true}),'off requested');
 const invalid=[],bad=decoder(event=>invalid.push(event),()=>{});
 bad.write(Buffer.from('SUTRA_EVENT '+JSON.stringify({kind:'start',id:'invalid-control',seq:1,model:'m',role:'conversation'})+'\n'));
 bad.write(Buffer.from('SUTRA_EVENT '+JSON.stringify({kind:'control',id:'invalid-control',seq:2,requested:'x'.repeat(300),
   effective:{private:'not an enum'},switch_supported:'false',thinking_only:1,control_label:'PRIVATE UNTRUSTED LABEL'})+'\n'));
 assert.equal(invalid[1].requested.length,80);
 for(const key of ['effective','switch_supported','thinking_only','control_label'])assert(!(key in invalid[1]));
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'sutra-remote-control-'));
 try{
  const saved=new ActivityArchive(directory);received.forEach(event=>saved.accept(event));saved.dispose();
  const restored=new ActivityArchive(directory),call=restored.calls[0];
  assert.equal(call.effective,'model_default');assert.equal(call.switch_supported,false);assert.equal(call.thinking_only,true);
  assert.equal(call.control_label,label);assert.equal(call.requested,false);
  assert(restored.document(remoteId).includes('Reasoning control: '+label));
  assert(!fs.readFileSync(restored.file,'utf8').includes('PRIVATE CONTROL FIELD'));
  const forged=JSON.parse(fs.readFileSync(restored.file,'utf8'));
  Object.assign(forged.calls[0],{effective:'off',switch_supported:'false',thinking_only:'true',control_label:'PRIVATE UNTRUSTED LABEL'});
  fs.writeFileSync(restored.file,JSON.stringify(forged));
  const sanitized=new ActivityArchive(directory);
  assert.equal(sanitized.calls[0].control_label,'off requested');
  assert(!sanitized.document(remoteId).includes('PRIVATE UNTRUSTED LABEL'));
  restored.dispose();sanitized.dispose();
 }finally{fs.rmSync(directory,{recursive:true,force:true});}
 stream.close();bad.close();
 console.log('PASS bounded remote reasoning capability metadata, truthful inline/archive labels, legacy controls and forged-field rejection');
}
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
 archive.accept({kind:'start',id:id(42),seq:1,role:'subagent',model:'large'});
 archive.accept({kind:'progress',id:id(42),seq:2,generation_progress:progressSnapshot({text:'PRIVATE PATTERN'})});
 archive.accept({kind:'finish',id:id(42),seq:3,status:'accepted',generation_progress:progressSnapshot({alert:false,finished:true})});
 const withProgress=new ActivityArchive(dir),savedProgress=withProgress.calls.find(c=>c.id===id(42));
 assert(savedProgress.generation_progress.finished);assert(!JSON.stringify(savedProgress).includes('PRIVATE PATTERN'));
 assert(withProgress.document(id(42)).includes('Repeated reasoning text observed (literal signal; correctness unverified)'));
 assert(withProgress.document(id(42)).includes('4096 of 4096 received characters analyzed'));
 archive.accept({kind:'start',id:id(40),role:'conversation',model:'small'});
 archive.accept({kind:'control',id:id(40),requested:false});
 archive.accept({kind:'finish',id:id(40),status:'accepted',thinking_chars:0,usage:{input_tokens:3,output_tokens:5}});
 const noThought=new ActivityArchive(dir);
 assert(noThought.document(id(40)).includes('Reasoning characters reported: 0'));
 assert(noThought.document(id(40)).includes('Reasoning control: off requested'));
 assert(noThought.document(id(40)).includes('No separate reasoning text received.'));
 assert(noThought.document(id(40)).includes('Status: accepted'));
 assert(noThought.document(id(40)).includes('Tokens: 3 input / 5 output'));
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
 const continuous=new ActivityArchive(path.join(temp,'c'.repeat(24))),visible=[],parts=[];
 class ProgressText{constructor(value){this.value=value;}}
 const show=require('../model_activity').chatActivity({LanguageModelTextPart:ProgressText},{report:p=>parts.push(p)});
 const stream=decoder(e=>{visible.push(e);continuous.accept(e);show(e);},()=>{});
 const emit=e=>stream.write(Buffer.from('SUTRA_EVENT '+JSON.stringify(e)+'\n'));
 for(let n=1000;n<1220;n++){
  emit({kind:'start',id:id(n),seq:1,model:n%2?'small':'large',role:'subagent'});
  emit({kind:'progress',id:id(n),seq:2,generation_progress:progressSnapshot()});
  emit({kind:'finish',id:id(n),seq:3,status:'accepted',generation_progress:progressSnapshot({alert:false,finished:true})});
 }
 assert.equal(visible.filter(e=>e.kind==='start').length,220);
 assert.equal(visible.filter(e=>e.kind==='progress').length,220);
 assert.equal(visible.filter(e=>e.kind==='finish').length,220);
 assert.equal(parts.filter(p=>p instanceof ProgressText).length,220,'Each sequential call remains visible beyond the old64-call ceiling');
 assert.equal(continuous.calls.length,12);assert(fs.statSync(continuous.file).size<=1024*1024);
 assert.equal(new ActivityArchive(continuous.directory).calls.length,12);
 assert(new ActivityArchive(continuous.directory).document(id(1219)).includes('literal signal; correctness unverified'));
 const beforeReplay=visible.length;
 emit({kind:'start',id:id(1219),seq:1,model:'replayed',role:'subagent'});
 emit({kind:'thinking',id:id(1219),seq:99,text:'late'});assert.equal(visible.length,beforeReplay);
 stream.close();const afterClose=visible.length;stream.close();emit({kind:'start',id:id(1221),seq:1,model:'m',role:'subagent'});
 assert.equal(visible.length,afterClose);continuous.dispose();
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
 view.activity({kind:'start',id:id(62),seq:1,model:'large',role:'subagent'});
 view.activity({kind:'progress',id:id(62),seq:2,generation_progress:progressSnapshot()});
 assert(sent.at(-1).activity.find(c=>c.id===id(62)).generation_progress.repetition_indicator);
 view.activity({kind:'finish',id:id(62),seq:3,status:'accepted',generation_progress:progressSnapshot({alert:false,finished:true})});
 assert(view.activityDocument(view.activityLink({id:id(62)})).includes('literal signal; correctness unverified'));
 current=path.join(tmp,'b'.repeat(24));view.reset();assert.equal(sent.at(-1).activity.length,0);
 assert(view.activityDocument(childUri).includes('different workspace'));
 assert(view.activityDocument('karma-preview:/activity/../../private').includes('different workspace'));
 console.log('PASS Monitor/archive integration: call links, plain read-only documents, live refresh, reload retention and workspace isolation');
 fs.rmSync(tmp,{recursive:true,force:true});
})().catch(e=>{console.error(e);process.exitCode=1;});
