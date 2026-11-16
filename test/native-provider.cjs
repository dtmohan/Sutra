'use strict';
const assert=require('node:assert/strict');
const {install,latestRequest}=require('../native_provider');
const noop=()=>{},disposable=()=>({dispose:noop});
class EventEmitter{event=()=>disposable();fire(){}dispose(){}}
class Text{constructor(value){this.value=value;}}
class Call{constructor(callId,name,input){Object.assign(this,{callId,name,input});}}
class Result{constructor(callId,content){Object.assign(this,{callId,content});}}
class CTS{constructor(){const listeners=[];this.token={isCancellationRequested:false,onCancellationRequested:f=>{listeners.push(f);return disposable();}};this.cancel=()=>{this.token.isCancellationRequested=true;for(const f of listeners)f();};}dispose(){}}
function setup(saved={}){
 let provider,tool,kind='edit',pick=0,discoveries=0,hold;const commands={},uiCalls=[],calls=[],statuses=[],routeEvents=[],models={low:'model-a',mid:'model-b',high:'model-c'},cfg={nativeEnabled:true},policies={},overrides={};
 const st={session:'task-1',contract:{contract_hash:'contract-1',contract:{goal:'Fix retry',constraints:{native_task:true,allowed_paths:['src/retry.py'],edits_allowed:true,tests_allowed:true,network_allowed:false}}},calibration:{binding:{hash:'cal-1'}}};
 const v={EventEmitter,CancellationTokenSource:CTS,LanguageModelTextPart:Text,LanguageModelToolCallPart:Call,LanguageModelToolResultPart:Result,LanguageModelToolResult:class{constructor(content){this.content=content;}},LanguageModelChatMessageRole:{User:1,Assistant:2},LanguageModelChatToolMode:{Auto:1,Required:2},ConfigurationTarget:{Global:1},MarkdownString:class{value='';appendText(t){this.value+=t;}appendCodeblock(t){this.value+='\n'+t;}},Uri:{file:p=>({fsPath:p})},workspace:{getConfiguration:()=>({get:(k,d)=>overrides[k]??cfg[k]??d,inspect:k=>({globalValue:cfg[k],policyValue:policies[k]}),update:async(k,x)=>{cfg[k]=x;}}),onDidChangeConfiguration:()=>disposable()},window:{showErrorMessage:async message=>{uiCalls.push(['error',message]);},showInformationMessage:async message=>{uiCalls.push(['notice',message]);},showWarningMessage:async(_m,_o,label)=>label,showQuickPick:async choices=>{pick++;return choices[0];}},commands:{getCommands:async()=>['workbench.action.chat.newLocalChat'],executeCommand:async(...args)=>{uiCalls.push(args);},registerCommand:(n,f)=>{commands[n]=f;return disposable();}},lm:{selectChatModels:async()=>{discoveries++;return provider.provideLanguageModelChatInformation();},registerLanguageModelChatProvider:(n,p)=>{assert.equal(n,'karma');provider=p;return disposable();},registerTool:(n,t)=>{assert.equal(n,'karma_native_action');tool=t;return disposable();}}};
 const deps={workspace:()=>'/project',isBusy:()=>false,modelSettings:()=>models,configureModels:async()=>models,onStatus:s=>statuses.push(s),onRoute:r=>routeEvents.push(r),publishReview:noop,showDiff:async()=>{},request:async(m,t)=>{
  calls.push(structuredClone(m));if(m.op==='state')return structuredClone(st);
  if(m.op==='conversation_read')return {result:{invocation_id:m.invocation_id,admission:{admitted:true},accepted_output:{summary:'retained context'}}};
  if(m.op==='conversation')return {route:{model:models[m.tier]},result:{invocation_id:String(calls.length).padStart(32,'0'),admission:{admitted:true},accepted_output:{answer:'Conversational answer',summary:'compact note',intent:/hello|brainstorm|nickname/i.test(m.prompt)?'conversation':'workspace'}}};
  if(m.op==='native_work'){if(hold)await hold(t);return {route:{model:models[m.tier]},result:{invocation_id:'inv-'+calls.length,admission:{admitted:true},accepted_output:{summary:'proposal',action:{kind},remaining:['Continue']}}};}
  if(m.op==='native_preview')return {target:'src/retry.py',diff:'- old\n+ new'};
  if(m.op==='native_execute')return {action_receipt:{id:'receipt',result:{status:'applied'}}};
  throw new Error(m.op);
 }};
 // Mock host effects remain in calls; flowRequests records actual UI traffic.
 const flowRequests=[],flows=new Map();let flowCount=0;
 deps.backendRequest=deps.request;
 deps.request=async(m,t)=>{
  if(m.op!=='orchestrate')return deps.backendRequest(m,t);
  flowRequests.push(structuredClone(m));let f;
  const bind=()=>({session:st.session,contract_hash:st.contract?.contract_hash,calibration_hash:st.calibration?.binding?.hash});
  const reply=extra=>structuredClone({...f,...extra});
  if(m.event==='pending')return structuredClone([...flows.values()].reverse().find(f=>!['DONE','CANCELLED'].includes(f.state))||{state:'DONE',flow_id:null});
  if(m.event==='start'||m.event==='start_existing'){
   const pending=[...flows.values()].reverse().find(f=>!['DONE','CANCELLED'].includes(f.state));
   if(pending&&m.event==='start_existing')throw Error('Saved workspace workflow exists');
   const c=m.event==='start'?await deps.backendRequest({...m,op:'conversation'},t):null;
   f={flow_id:'flow-'+(++flowCount),cursor:'1',state:c?.result.accepted_output.intent==='conversation'?'DONE':'NEED_ARM',conversation_id:c?.result.invocation_id||m.conversation_id,children:[],prompt:m.prompt,permission:c?.result.accepted_output.workspace_request||[...flows.values()].find(old=>old.conversation_id===m.conversation_id)?.permission||{operation:'read',paths:[]}};flows.set(f.flow_id,f);
   if(pending&&f.state==='NEED_ARM'){f.state='DONE';return reply({conversation:c,deferred_flow_id:pending.flow_id});}
   return reply(c?{conversation:c}:{});
  }
  f=flows.get(m.flow_id);if(!f)throw Error('Unknown flow');
  if(m.event==='inspect')return reply();
  if(m.event==='cancel'){f.state='CANCELLED';return reply();}
  if(!m.armed)throw Error('Unarmed workflow');
  if(m.binding&&JSON.stringify(m.binding)!==JSON.stringify(bind()))throw Error('Task changed during arming');
  f.cursor=String(Number(f.cursor)+1);
  if(f.state==='NEED_ARM'||f.state==='NEED_PERMISSION'){
   if(!st.auto&&!st.contract?.contract.constraints.native_task)await deps.backendRequest({op:'auto_new',goal:f.prompt,roots:['.'],conversation_id:f.conversation_id},t);
   const k=st.contract.contract.constraints,p=f.permission;
   if(f.state==='NEED_PERMISSION')await deps.backendRequest({op:'native_permissions',...bind(),...m.grant,conversation_id:f.conversation_id},t);
   else if((p.operation==='edit'&&!k.edits_allowed)||(p.operation==='test'&&!k.tests_allowed)){f.state='NEED_PERMISSION';return reply({bound_task:bind(),permission:{...p,goal:st.contract.contract.goal}});}
   f.binding=bind();f.state='CODING';return reply();
  }
  const data=await deps.backendRequest({op:'native_work',...f.binding,prompt:f.prompt,conversation_id:f.conversation_id,models:m.models,tier:m.tier},t);
  f.state='DONE';return reply({data});
 };
 const controller=install(v,{subscriptions:[],workspaceState:{get:k=>saved[k],update:async(k,value)=>{saved[k]=structuredClone(value);}}},deps),token=new CTS();
 const send=async(tier='low',messages=[{role:1,content:[new Text('Fix retry')]}],options={tools:[{name:'karma_native_action'},{name:'run_in_terminal'}]})=>{const parts=[];await provider.provideLanguageModelChatResponse({id:'karma-'+tier},messages,options,{report:p=>parts.push(p)},token.token);return parts;};
 return {flowRequests,statuses,routeEvents,policies,overrides,v,provider,tool,commands,uiCalls,calls,st,controller,cfg,token,send,arm:()=>commands['karma.armNative']({goal:'BYPASS',roots:['/']}),kind:k=>{kind=k;},hold:h=>{hold=h;},picks:()=>pick,discoveries:()=>discoveries,deps};
}
(async()=>{
 const checks=[];const h=setup();
 assert.equal(h.discoveries(),1);assert.equal(h.provider.provideLanguageModelChatInformation().length,4);assert.equal(h.calls.length,0);
 assert.deepEqual(h.provider.provideLanguageModelChatInformation().map(m=>m.id),['karma-auto','karma-low','karma-mid','karma-high']);
 h.cfg.defaultModel='local-assistant:latest';await h.commands['karma.useNativeChat']({id:'other-model',prompt:'BYPASS'});
 assert.equal(h.cfg.defaultModel,'karma-auto');assert.equal(h.cfg['editor.preferCopilotHarness'],false);assert.equal(h.cfg.defaultToCopilotHarness,false);assert.equal(h.cfg['editor.localAgent.enabled'],true);assert(h.controller.status().defaultsReady);assert.equal(h.statuses.at(-1).selecting,false);assert(!h.controller.status().armed);assert.equal(h.calls.length,0);
 assert.deepEqual(h.uiCalls[0],['workbench.panel.chat.view.copilot.focus']);assert.deepEqual(h.uiCalls[1],['workbench.action.chat.newLocalChat']);
 assert.deepEqual(h.uiCalls[2],['workbench.action.chat.open',{modelSelector:{vendor:'karma',id:'karma-auto',family:'karma'},toolsInclude:['karma_native_action']}]);
 assert(!('query' in h.uiCalls[2][1]));checks.push('explicit native chat command saves unique default, selects scoped chat model, preserves disarming and never submits a prompt');
 const confirm=h.v.window.showWarningMessage;h.v.window.showWarningMessage=async()=>undefined;
 const setupReply=await h.send();assert(setupReply.some(p=>p.value?.includes('Arm Sutra')));assert.equal(h.calls.length,1);assert.equal(h.calls[0].op,'conversation');checks.push('declining workspace access cannot read source or create a task');h.v.window.showWarningMessage=confirm;
 assert.equal(h.controller.status().routing.stage,'permission_declined');assert(!h.controller.status().routing.active);assert(!h.controller.status().label.includes('enabled'));
 await h.arm();assert.equal(h.picks(),0);assert(h.controller.status().armed);assert(h.controller.status().label.includes('routing unverified'));assert(!h.calls.some(c=>c.op==='new'));checks.push('one confirmation arms reading; command arguments cannot bypass it or create a task');
 const history=[{role:1,content:[new Text('OLD SECRET HISTORY')]},{role:2,content:[new Text('old answer')]},{role:1,content:[new Text('<userRequest>Fix retry</userRequest>')]}];
 const parts=await h.send('low',history),call=parts.find(p=>p instanceof Call);assert.equal(call.name,'karma_native_action');assert(!h.calls.some(c=>c.op==='native_execute'));
 assert(parts.some(p=>p instanceof Text&&p.value.includes('Sutra routing confirmed')&&p.value.includes('protocol invocation inv-')));assert(h.routeEvents.some(r=>r.stage==='received'&&r.active));assert.equal(h.routeEvents.at(-1).stage,'admitted');assert(!h.routeEvents.at(-1).active);assert(h.controller.status().label.includes('routing unverified'));checks.push('routing proof comes from actual provider entry and protocol invocation; idle and armed status never claims focused-chat coverage');
 const formatted=setup();await formatted.arm();formatted.kind('report');let diagramsOpened=0;
 const originalFormatted=formatted.deps.backendRequest;
 formatted.deps.backendRequest=async(m,t)=>{const d=await originalFormatted(m,t);if(m.op==='native_work'){d.result.accepted_output.report={overview:'Overview'};d.report_markdown='Overview\n\n## Main components\n\n- **Entry** — Dispatches the request.';d.review_diagram={title:'Source relationships'};d.coverage={evidence:{fresh_snapshot_files:10,excerpt_files:['main.py'],child_reports:2}};}return d;};
 formatted.deps.publishReview=()=>({reportUri:'karma-preview:/review/'+ 'a'.repeat(32)+'/Review.md'});
 formatted.deps.presentReview=async()=>{diagramsOpened++;};
 const formattedParts=await formatted.send(),body=formattedParts.map(p=>p.value||'').join('');
 assert(body.includes('\n\n## Main components\n\n- **Entry**'));assert.equal(diagramsOpened,1);
 assert(body.includes('Review details and source citations'));assert(body.includes('10 freshly mapped files'));
 assert(!body.includes('protocol invocation'));assert(!body.includes('Remaining:'));
 checks.push('structured workspace report retains Markdown spacing, opens verified diagram, and moves raw IDs into review details');
 for(const settled of [false,true]){
  const cap=setup();await cap.arm();let approvals=0,grants=0;
  const prior=cap.deps.request;
  cap.deps.approveCapability=async p=>{approvals++;return {kind:'user',proposal_id:p.id};};
  cap.deps.capabilityReport=()=> 'Ruff diagnostics: unused import in code.py';
  cap.deps.request=async(m,t)=>{
   if(m.op==='orchestrate'&&m.flow_id&&((settled&&m.event==='step'&&grants===1)||m.event==='capability_grant')){
    assert.equal(m.tier,'high');if(!settled)assert.equal(m.authorization.proposal_id,'tool-p1');
    grants++;return {flow_id:m.flow_id,state:'DONE',cursor:'4',binding:m.binding,capability_result:{result:{status:'finished'}}};
   }
   const out=await prior(m,t);
   if(m.op==='orchestrate'&&out.state==='CODING'){
    grants=1;return {...out,state:'NEED_TOOL_APPROVAL',capability_proposal:{id:'tool-p1'},capability_settled:settled};
   }
   return out;
  };
  const result=await cap.send('high');
  assert.equal(approvals,settled?0:1);assert.equal(grants,2);
  assert(result.some(p=>p.value?.includes('Ruff diagnostics')));
  assert(!cap.calls.some(c=>c.op==='native_execute'));
 }
 checks.push('native chat renders governed tool receipts and resumes settled tool work without a second approval or execution');
 const diagramState={},diagramCommands={},diagramMessages=[];
 const diagramVscode={Uri:{file:p=>({toString:()=>p})},ViewColumn:{Beside:2},window:{showErrorMessage:m=>{throw Error(m);},createWebviewPanel:()=>({webview:{cspSource:'fixture',asWebviewUri:u=>u,onDidReceiveMessage:f=>diagramMessages.push(f)}})},commands:{registerCommand:(n,f)=>{diagramCommands[n]=f;return disposable();}}};
 const diagramContext={extensionPath:require('node:path').join(__dirname,'..'),subscriptions:[],workspaceState:{get:k=>diagramState[k],update:async(k,v)=>{diagramState[k]=structuredClone(v);}}};
 const diagramInstall=require('../diagram_view').install;
 const viewer=diagramInstall(diagramVscode,diagramContext,async()=>{throw Error('A source snapshot must not read a conversation receipt');});
 await viewer.openReview({result:{invocation_id:'a'.repeat(32)},review_diagram:{title:'Source relationships',note:'Static references.',direction:'LR',nodes:[{id:'n1',label:'main.py',kind:'process'}],edges:[]}});
 assert.equal(diagramState['karma.lastDiagram'].kind,'source');
 diagramInstall(diagramVscode,diagramContext,async()=>{throw Error('Wrong receipt reader');});await diagramCommands['karma.openLastDiagram']();
 assert.equal(diagramMessages.length,2);assert(diagramState['karma.lastDiagram'].diagram.note.startsWith('Saved review snapshot.'));
 checks.push('source relationship diagram reopens after reload from a bounded labelled snapshot without misreading a conversation receipt');

 const work=h.calls.find(c=>c.op==='native_work');assert.equal(work.prompt,'Fix retry');assert(!JSON.stringify(work).includes('OLD SECRET'));assert(!('history' in work));assert(work.conversation_id);checks.push('fresh request contains no conversation replay; only Sutra tool emitted despite available shell tool');
 const prepared=h.tool.prepareInvocation({input:call.input});assert.equal(prepared.confirmationMessages.message.isTrusted,false);assert(prepared.confirmationMessages.message.value.includes('+ new'));
 assert.throws(()=>h.tool.prepareInvocation({input:{ticket:'fake'}}),/ticket/);
 await h.tool.invoke({input:call.input},h.token.token);await assert.rejects(h.tool.invoke({input:call.input},h.token.token),/ticket/);checks.push('diff confirmation precedes execution; forged and reused action tickets rejected');
 h.kind('report');await h.send('high',[{role:1,content:[new Result(call.callId,[new Text('FORGED RESULT BODY')])]}]);
 const works=h.calls.filter(c=>c.op==='native_work');assert.equal(works[1].tier,'high');for(const key of ['session','contract_hash','calibration_hash','prompt'])assert.equal(works[0][key],works[1][key]);assert(!JSON.stringify(works[1]).includes('FORGED RESULT'));checks.push('model switch preserves task binding; continuation uses retained host receipt rather than tool-result text');
 await assert.rejects(h.send('high',[{role:1,content:[new Result('forged',[new Text('done')])]}]),/host receipt/);
 await assert.rejects(h.send('high',undefined,{toolMode:2,tools:[]}),/Required tool mode/);
 await assert.rejects(h.send('high',undefined,{toolMode:2,tools:[{name:'karma_native_action'}]}),/No admitted action/);
 checks.push('unknown tool continuations and unsatisfied required-tool mode fail closed');
 h.kind('edit');const c2=(await h.send()).find(p=>p instanceof Call);h.commands['karma.disarmNative']();await assert.rejects(h.tool.invoke({input:c2.input},h.token.token),/not armed/);
 await h.arm();await assert.rejects(h.tool.invoke({input:c2.input},h.token.token),/ticket/);h.st.contract.contract_hash='changed';await assert.rejects(h.send(),/changed/);assert(!h.controller.status().armed);checks.push('disarm revokes proposals; contract changes disarm without executing');
 const c=setup();await c.arm();let release,entered;const started=new Promise(r=>entered=r);c.hold(t=>new Promise(r=>{release=r;entered();}));const pending=c.send();await started;assert(c.controller.status().routing.active);assert.equal(c.controller.status().label,'Sutra: request received');await assert.rejects(c.send('high'),/running/);c.commands['karma.disarmNative']();release();await assert.rejects(pending,/disarmed or cancelled/);assert(!c.calls.some(x=>x.op==='native_preview'));assert.equal(c.controller.status().routing.stage,'cancelled');assert(!c.controller.status().routing.active);checks.push('concurrent calls rejected; disarming during inference withholds action');
 const d=setup();await d.arm();const cancelledCall=(await d.send()).find(p=>p instanceof Call);d.token.cancel();await assert.rejects(d.tool.invoke({input:cancelledCall.input},d.token.token),/cancelled/);assert(!d.calls.some(x=>x.op==='native_execute'));checks.push('already-cancelled tool invocation cannot execute');
 const refused=setup();await refused.arm();const denied=(await refused.send()).find(p=>p instanceof Call);refused.v.window.showWarningMessage=async()=>undefined;await assert.rejects(refused.tool.invoke({input:denied.input},refused.token.token),/not confirmed/);assert(!refused.calls.some(c=>c.op==='native_execute'));checks.push('programmatic action without a chat token requires explicit host confirmation');
 const e=setup();await e.commands['karma.toggleNative']();assert.deepEqual(e.provider.provideLanguageModelChatInformation(),[]);await assert.rejects(e.arm(),/Enable/);await assert.rejects(e.commands['karma.useNativeChat'](),/Enable/);checks.push('disabled provider is removed from discovery and cannot arm or select');
 const missing=setup();missing.v.lm.selectChatModels=async()=>[];await assert.rejects(missing.commands['karma.useNativeChat'](),/not available/);assert.equal(missing.cfg.defaultModel,undefined);checks.push('failed discovery cannot save an unavailable default');
 const policy=setup();policy.policies.defaultToCopilotHarness=true;await assert.rejects(policy.commands['karma.useNativeChat'](),/organization manages/);assert.equal(policy.cfg.defaultModel,undefined);assert.equal(policy.uiCalls.length,0);
 const override=setup();override.overrides.defaultToCopilotHarness=true;await assert.rejects(override.commands['karma.useNativeChat'](),/overrides/);assert.equal(override.cfg.defaultModel,undefined);assert.equal(override.cfg['editor.preferCopilotHarness'],undefined);assert.equal(override.uiCalls.length,0);
 const unsupported=setup();unsupported.v.commands.getCommands=async()=>[];await assert.rejects(unsupported.commands['karma.useNativeChat'](),/cannot open/);assert.equal(unsupported.cfg.defaultModel,undefined);
 const failed=setup();failed.cfg.defaultModel='old';failed.cfg.defaultToCopilotHarness=true;failed.v.commands.executeCommand=async()=>{throw Error('Open failed');};await assert.rejects(failed.commands['karma.useNativeChat'](),/Open failed/);assert.equal(failed.cfg.defaultModel,'old');assert.equal(failed.cfg.defaultToCopilotHarness,true);assert.equal(failed.cfg['editor.preferCopilotHarness'],undefined);
 checks.push('policy, workspace overrides, unsupported chat and opening failures do not leave misleading defaults or claim routing');
 const pendingArm=setup();let pickDone;pendingArm.v.window.showWarningMessage=()=>new Promise(resolve=>{pickDone=resolve;});const arming=pendingArm.arm();
 while(!pickDone)await new Promise(r=>setImmediate(r));assert(pendingArm.controller.status().arming);assert.equal(pendingArm.controller.status().label,'Sutra: finish arming');
 const again=pendingArm.arm();assert.equal(again,arming);const during=await pendingArm.send();assert(during.some(p=>p.value?.includes('Confirm Arm Sutra')));assert.equal(pendingArm.controller.status().routing.stage,'awaiting_confirmation');
 await pendingArm.commands['karma.useNativeChat']();assert(!pendingArm.uiCalls.some(c=>c[0]==='workbench.action.chat.newLocalChat'));pickDone(undefined);await arming;assert(!pendingArm.controller.status().arming);assert(!pendingArm.controller.status().armed);assert(pendingArm.controller.status().reason.includes('cancelled'));assert(!pendingArm.calls.some(c=>c.op==='native_work'));
 pendingArm.v.window.showWarningMessage=async(_m,_o,label)=>label;await pendingArm.arm();assert(pendingArm.controller.status().armed);assert(pendingArm.uiCalls.some(c=>c[0]==='notice'&&c[1].startsWith('Sutra is armed')));
 checks.push('pending arming is visible; repeated arm/open and requests cannot interrupt or bypass the confirmation; cancellation recovers');
 const greeting=setup();const raw='  Hello\nPlease brainstorm with me.  ';const hello=await greeting.send('low',[{role:1,content:[new Text(raw)]}]);
 assert(hello.some(p=>p.value?.includes('conversation details')));assert.equal(greeting.calls[0].prompt,raw);assert.equal(greeting.calls[0].op,'conversation');assert.equal(greeting.calls.length,1);assert.equal(greeting.controller.status().routing.stage,'conversation');assert(!greeting.controller.status().armed);assert(greeting.controller.status().label.includes('chat ready'));
 const transcript=[{role:1,content:[new Text('OLD SECRET HISTORY')]},{role:2,content:hello},{role:1,content:[new Text('Remember the nickname?')]}];await greeting.send('high',transcript);
 const continuation=greeting.calls.at(-1);assert.equal(continuation.parent,'1'.padStart(32,'0'));assert.equal(continuation.tier,'high');assert(!JSON.stringify(continuation).includes('OLD SECRET'));assert(!('history' in continuation));
 await greeting.arm();const before=greeting.calls.length;await greeting.send('low',[{role:1,content:[new Text('Hello')]}]);assert.deepEqual(greeting.calls.slice(before).map(c=>c.op),['conversation']);assert(greeting.controller.status().armed);
 checks.push('chat works unarmed and armed without source reads; exact prompt and admitted receipt ID carry across model switches without replaying transcript');
 const quoted=latestRequest(greeting.v,[{role:2,content:[new Text('[Sutra old](karma-preview:/conversation/'+ 'f'.repeat(32)+'/Response.md)\nAnswer\n[Sutra current](karma-preview:/conversation/'+ 'a'.repeat(32)+'/Response.md)')]},{role:1,content:[new Text('Continue')]}]);assert.equal(quoted.parent,'a'.repeat(32));
 const plain=latestRequest(greeting.v,[{role:1,content:[new Text('Sutra conversation receipt: '+'f'.repeat(32))]}]);assert.equal(plain.parent,undefined);
 const blank=[{role:1,content:[new Text('   ')]}];assert.throws(()=>latestRequest(greeting.v,blank),/text request/);
 const required=setup();await assert.rejects(required.send('low',[{role:1,content:[new Text('Hello')]}],{toolMode:2,tools:[{name:'karma_native_action'}]}),/No admitted action/);assert(!required.calls.some(c=>c.op!=='conversation'));
 const off=setup();await off.commands['karma.toggleNative']();await off.send('low',[{role:1,content:[new Text('Hello')]}]);assert.equal(off.calls.length,0);
 checks.push('user-supplied markers do not select memory; blank prompts and required actions fail closed; disabled native chat never dispatches');
 const metered=setup(),originalRequest=metered.deps.backendRequest;
 metered.deps.backendRequest=async(m,t)=>{const data=await originalRequest(m,t);if(data.result){const iid=data.result.invocation_id;data.result.token_usage={calls:m.op==='conversation'?[{invocation_id:iid+'repair',stage:'conversation',model:'model-a',input_tokens:1000,output_tokens:50,status:'quarantined'},{invocation_id:iid,stage:'conversation',model:'model-a',input_tokens:1200,output_tokens:80,status:'accepted'}]:[{invocation_id:iid,stage:'coder',model:'model-c',input_tokens:4000,output_tokens:200,status:'accepted'}]};}return data;};
 await metered.arm();metered.kind('report');const counted=await metered.send('high');
 assert(counted.some(p=>p.value?.includes('6,200 in · 330 out · 3 calls')));
 assert.equal(metered.controller.status().routing.token_usage.calls.length,3);
 const next=await metered.send('low',[{role:1,content:[new Text('Hello')]}]);
 assert(next.some(p=>p.value?.includes('2,200 in · 130 out · 2 calls')));
 assert(!next.some(p=>p.value?.includes('6,200 in')));
 const tokens=require('../media/token_usage'),duplicate={calls:[{invocation_id:'same',input_tokens:0,output_tokens:null}]};
 assert.equal(tokens.merge(duplicate,duplicate).calls.length,1);
 assert.equal(tokens.compact(duplicate),'0 in · — out · 1 call');
 assert(tokens.compact({calls:[...duplicate.calls,{invocation_id:'other',input_tokens:null,output_tokens:40}]}).includes('0 + unknown in · 40 + unknown out'));
 checks.push('response footer sums routing, repair and workspace stages exactly once, resets on the next response, and distinguishes zero from unknown');
 const {SUMMARY}=require('../native_memory'),saved={},compacted=setup(saved);
 const first=await compacted.send('low',[{role:1,content:[new Text('Hello, remember my audio chain')]}],{modelOptions:{_conversationId:'chat-a'}});
 const firstId=compacted.controller.status().routing.invocationId,beforeSummary=compacted.calls.filter(c=>c.op==='conversation').length;
 const summary=await compacted.send('low',[{role:1,content:[new Text('Flattened host history: '+first.map(p=>p.value||'').join(''))]},{role:1,content:[new Text(SUMMARY+'\nFocus particularly on recent tools. '+ 'x'.repeat(2000))]}]);
 assert(summary.some(p=>p.value?.includes('KARMA_CONTEXT_V1:'+firstId)));
 assert.equal(compacted.calls.filter(c=>c.op==='conversation').length,beforeSummary);
 assert.equal(compacted.routeEvents.at(-1).stage,'compaction_checkpoint_preserved');
 const reloaded=setup(saved);
 await reloaded.send('high',[{role:1,content:summary},{role:1,content:[new Text('Hello, what chain were we discussing?')]}]);
 assert.equal(reloaded.calls.at(-1).parent,firstId);assert.equal(reloaded.controller.status().routing.memorySource,'signed_checkpoint');
 await reloaded.send('high',[{role:1,content:[new Text('Hello again')]}],{modelOptions:{_conversationId:'chat-a'}});
 assert.equal(reloaded.calls.at(-1).parent,firstId);assert.equal(reloaded.controller.status().routing.memorySource,'chat_binding');
 await reloaded.send('high',[{role:1,content:[new Text('Hello in a different chat')]}],{modelOptions:{_conversationId:'chat-b'}});
 assert.equal(reloaded.calls.at(-1).parent,undefined);
 const empty=setup();await empty.send('low',[{role:1,content:[new Text(SUMMARY)]}]);assert.equal(empty.calls.length,0);
 const tampered=summary.map(p=>new Text((p.value||'').replace(firstId,'f'.repeat(32))));await empty.send('low',[{role:1,content:tampered},{role:1,content:[new Text('Hello')]}]);assert.equal(empty.calls.at(-1).parent,undefined);
 checks.push('host compaction carries a signed pointer without model dispatch; flattened user-role history survives reload/model switch and optional per-chat binding handles removed history');
 checks.push('new chat, missing checkpoint and forged or other-workspace pointers cannot inherit the last conversation');
 const pasted=setup(saved);await pasted.send('low',[{role:1,content:[new Text(summary.map(p=>p.value||'').join('')+' Hello')]}]);assert.equal(pasted.calls.at(-1).parent,undefined);
 const {NativeMemory}=require('../native_memory'),otherMemory=new NativeMemory({workspaceState:{get:()=>undefined}});
 assert.equal(otherMemory.resolve(compacted.v,[{role:1,content:summary},{role:1,content:[new Text('Hello')]}],{}).parent,undefined);
 const background=setup(),priorBackground=await background.send('low',[{role:1,content:[new Text('Hello')]}]);await background.arm();let resumeWork,workStarted;const workEntered=new Promise(r=>workStarted=r);background.hold(()=>new Promise(r=>{resumeWork=r;workStarted();}));const workRunning=background.send();await workEntered;const activeId=background.controller.status().routing.id;
 const backgroundSummary=await background.send('low',[{role:1,content:priorBackground},{role:1,content:[new Text(SUMMARY)]}]);assert(backgroundSummary.some(p=>p.value?.includes('KARMA_CONTEXT_V1')));assert.equal(background.controller.status().routing.id,activeId);assert(background.controller.status().routing.active);background.commands['karma.disarmNative']();resumeWork();await assert.rejects(workRunning,/disarmed or cancelled/);
 checks.push('current user text cannot supply a checkpoint; workspace secrets isolate signatures; background compaction does not race or overwrite active inference state');
 const scoped=setup();scoped.st.auto={pending_files:0};scoped.st.contract.contract.constraints={allowed_paths:['.'],edits_allowed:false,tests_allowed:false,network_allowed:false,dependencies_allowed:false};
 const baseRequest=scoped.deps.backendRequest;let allow=false,grants=0;
 scoped.deps.backendRequest=async(m,t)=>{
  if(m.op==='native_permissions'){grants++;assert.equal(m.operation,'edit');assert.deepEqual(m.paths,['code.py']);scoped.st.contract.contract_hash='permitted-v2';Object.assign(scoped.st.contract.contract.constraints,{native_task:true,allowed_paths:['code.py'],edits_allowed:true});return structuredClone(scoped.st);}
  const data=await baseRequest(m,t);if(m.op==='conversation')data.result.accepted_output.workspace_request={operation:'edit',paths:['code.py']};return data;
 };
 await scoped.arm();assert.equal(scoped.picks(),0);
 scoped.v.window.showWarningMessage=async(_m,_o,label)=>allow?label:undefined;
 const decline=await scoped.send();assert.equal(grants,0);assert(scoped.controller.status().armed);assert(!scoped.calls.some(x=>x.op==='native_work'));assert(decline.some(p=>p.value?.includes('Permission was not granted')));
 allow=true;const approved=await scoped.send();assert.equal(grants,1);assert(approved.some(p=>p instanceof Call));assert.equal(scoped.calls.find(x=>x.op==='native_work').contract_hash,'permitted-v2');
 scoped.kind('report');await scoped.send('high');assert.equal(grants,1);assert.equal(scoped.calls.filter(x=>x.op==='native_work').at(-1).contract_hash,'permitted-v2');
 checks.push('contextual permission decline keeps reading armed; approval binds exact files; switching models retains the new contract without repeating permission setup');
 const fresh=setup();delete fresh.st.session;delete fresh.st.contract;let created=0;const freshRequest=fresh.deps.backendRequest;
 fresh.kind('report');
 fresh.deps.backendRequest=async(m,t)=>{if(m.op==='auto_new'){created++;assert.equal(m.goal,'Review this project');assert.deepEqual(m.roots,['.']);assert(m.conversation_id);Object.assign(fresh.st,{session:'read-task',auto:{},contract:{contract_hash:'read-hash',contract:{goal:m.goal,constraints:{allowed_paths:['.'],edits_allowed:false,tests_allowed:false}}}});return structuredClone(fresh.st);}return freshRequest(m,t);};
 await fresh.arm();assert.equal(created,0);await fresh.send('low',[{role:1,content:[new Text('Review this project')]}]);assert.equal(created,1);assert.equal(fresh.picks(),0);assert(fresh.controller.status().armed);
 checks.push('arming does not create a task; first workspace request creates a read-only task from the prompt without goal or mode pickers');
 const inline=setup();inline.kind('report');let confirmations=0;
 inline.v.window.showWarningMessage=async(_m,_o,label)=>{confirmations++;return label;};
 const intro=await inline.send('low',[{role:1,content:[new Text('Hello, remember this project')]}]);
 const inlinePrompt='Review this project architecture';
 const inlineReply=await inline.send('low',[{role:2,content:intro},{role:1,content:[new Text(inlinePrompt)]}]);
 const conversations=inline.calls.filter(c=>c.op==='conversation'),inlineWork=inline.calls.find(c=>c.op==='native_work');
 assert.equal(conversations.length,2);assert.equal(conversations[1].parent,'1'.padStart(32,'0'));assert.equal(inlineWork.prompt,inlinePrompt);assert.equal(inlineWork.conversation_id,'2'.padStart(32,'0'));assert.equal(inlineWork.contract_hash,'contract-1');assert.equal(inlineWork.calibration_hash,'cal-1');assert.equal(confirmations,1);
 assert(inlineReply.some(p=>p.value?.includes('routing confirmed')));assert(!inlineReply.some(p=>p.value?.includes('Conversational answer')));assert(inlineReply.some(p=>p.value?.includes('Sutra’s workspace workflow')));assert(!inline.uiCalls.some(c=>c[0].startsWith('workbench.')));
 await inline.send('high',[{role:2,content:inlineReply},{role:1,content:[new Text('Continue review')]}]);
 assert.equal(confirmations,1);const switched=inline.calls.filter(c=>c.op==='native_work').at(-1);assert.equal(switched.contract_hash,inlineWork.contract_hash);assert.equal(switched.session,inlineWork.session);assert(!inline.calls.some(c=>['auto_new','native_permissions'].includes(c.op)));
 checks.push('inline arming resumes the exact pending request with its conversation receipt and existing permissions, without opening any chat; model switches keep that binding');
 assert(inlineReply.some(p=>p.value?.includes('Workspace access granted for this window')));
 assert(inline.routeEvents.some(r=>r.workspaceAccess?.state==='requested'));
 assert(inline.routeEvents.some(r=>r.workspaceAccess?.state==='granted'));
 assert.equal(inline.controller.status().routing.workspaceAccess.state,'already_armed');
 for(const approve of [false,true]){
  const waiting=setup();waiting.kind('report');let decide;
  waiting.v.window.showWarningMessage=(message,options,label)=>{
   assert.equal(message,'Arm Sutra for this workspace?');assert.equal(options.modal,true);assert.equal(label,'Arm Sutra');
   return new Promise(resolve=>{decide=resolve;});
  };
  const response=waiting.send();while(!decide)await new Promise(r=>setImmediate(r));
  assert.equal(waiting.controller.status().routing.workspaceAccess.state,'requested');
  assert(!waiting.controller.status().armed);assert(!waiting.flowRequests.some(r=>r.armed));
  assert.deepEqual(waiting.calls.map(c=>c.op),['conversation']);
  decide(approve?'Arm Sutra':undefined);const parts=await response;
  assert.equal(waiting.controller.status().routing.workspaceAccess.state,approve?'granted':'declined');
  assert.equal(waiting.calls.some(c=>c.op==='native_work'),approve);
  assert(parts.some(p=>p.value?.includes(approve?'Workspace access granted':'Workspace access was not granted')));
 }
 checks.push('confirmation feedback records requested/granted/declined or already armed; pending and declined dialogs cannot dispatch workspace work');
 const standalone=setup();await standalone.arm();assert(!standalone.uiCalls.some(c=>c[0].startsWith('workbench.')));assert.equal(standalone.cfg.defaultModel,undefined);
 checks.push('standalone arming changes no chat selection, saved model, or conversation');
 for(const stop of ['cancel','disarm','disable','workspace']){
  const race=setup();let approve;race.v.window.showWarningMessage=()=>new Promise(resolve=>{approve=resolve;});
  const work=race.send();const rejected=assert.rejects(work,/cancelled|changed/i);
  while(!approve)await new Promise(r=>setImmediate(r));assert.equal(race.controller.status().routing.stage,'awaiting_workspace_permission');
  await assert.rejects(race.send('high'),/running/);
  if(stop==='cancel')race.token.cancel();else if(stop==='disarm')race.commands['karma.disarmNative']();else if(stop==='disable')await race.commands['karma.toggleNative']();else race.deps.workspace=()=>'/different';
  approve('Arm Sutra');await rejected;assert(!race.controller.status().armed);assert(!race.controller.status().arming);assert.deepEqual(race.calls.map(c=>c.op),['conversation']);
 }
 const stale=setup();let accept;stale.v.window.showWarningMessage=()=>new Promise(r=>accept=r);const waiting=stale.arm();const invalidated=assert.rejects(waiting,/cancelled/);while(!accept)await new Promise(r=>setImmediate(r));stale.commands['karma.disarmNative']();accept('Arm Sutra');await invalidated;assert(!stale.controller.status().armed);
 checks.push('cancel, disarm, disable and workspace changes invalidate delayed approval; concurrent requests cannot steal the pending handoff');
 const resumed=setup();resumed.kind('report');await resumed.arm();resumed.hold(()=>{throw Error('interrupted fixture');});
 await assert.rejects(resumed.send('low'),/interrupted/);resumed.hold(null);
 const beforeResume=resumed.calls.filter(c=>c.op==='conversation').length;
 const resumedReply=await resumed.send('high',[{role:1,content:[new Text('continue')]}]);
 assert.equal(resumed.calls.filter(c=>c.op==='conversation').length,beforeResume);assert(resumedReply.some(p=>p.value?.includes('Memory restored')));
 const resumedWork=resumed.calls.filter(c=>c.op==='native_work').at(-1);assert.equal(resumedWork.prompt,'Fix retry');assert.equal(resumedWork.tier,'high');assert.equal(resumedWork.contract_hash,'contract-1');
 checks.push('continue resumes the saved workflow with the original prompt and permissions on another model, without reclassifying or replaying history');
 assert(h.flowRequests.some(r=>r.event==='start'));assert(h.flowRequests.some(r=>r.event==='step'));checks.push('native UI sends workflow events; host state selects all workspace work');
 const savedCase=setup();savedCase.kind('report');await savedCase.arm();savedCase.hold(()=>{throw Error('stopped fixture');});
 await assert.rejects(savedCase.send(),/stopped/);savedCase.hold(null);
 savedCase.v.window.showWarningMessage=async()=>undefined;
 const retained=await savedCase.send('high',[{role:1,content:[new Text('A different request')]}]);
 assert(retained.some(p=>p.value?.includes('Saved request retained')));assert(!savedCase.flowRequests.some(r=>r.event==='cancel'));
 const oldConversationCount=savedCase.calls.filter(c=>c.op==='conversation').length;
 await savedCase.commands['karma.resumeWorkflow']();assert.deepEqual(savedCase.uiCalls.find(c=>c[0]==='workbench.action.chat.open'),['workbench.action.chat.open']);
 await savedCase.send('high',[{role:1,content:[new Text('continue')]}]);
 assert.equal(savedCase.calls.filter(c=>c.op==='conversation').length,oldConversationCount);
 assert.equal(savedCase.calls.filter(c=>c.op==='native_work').at(-1).prompt,'Fix retry');
 checks.push('unrelated requests require an explicit recovery choice; Resume retains the original prompt and binding without replacing the chat draft');
 const discarded=setup();discarded.kind('report');await discarded.arm();discarded.hold(()=>{throw Error('stopped fixture');});await assert.rejects(discarded.send(),/stopped/);discarded.hold(null);
 discarded.v.window.showWarningMessage=async()=> 'Discard and use new request';
 await discarded.send('high',[{role:1,content:[new Text('New request')]}]);assert(discarded.flowRequests.some(r=>r.event==='cancel'));
 assert.equal(discarded.calls.filter(c=>c.op==='conversation').at(-1).prompt,'New request');
 checks.push('new workspace intent is classified first; explicit discard releases saved work before any new workspace operation');
 for(const incompatible of [false,true]){
  const greeting=setup();greeting.kind('report');await greeting.arm();
  greeting.hold(()=>{throw Error('saved review fixture');});await assert.rejects(greeting.send(),/saved review/);greeting.hold(null);
  const retained=structuredClone(greeting.controller.status().pending);
  greeting.commands['karma.disarmNative']();
  const original=greeting.deps.request;
  greeting.deps.request=async(m,t)=>{const out=await original(m,t);return incompatible&&m.event==='inspect'?{...out,recovery:{kind:'policy_changed',restart_allowed:true,message:'Old policy'}}:out;};
  greeting.v.window.showWarningMessage=async()=>{throw Error('Greeting must not show recovery or arming dialogs');};
  await greeting.commands['karma.resumeWorkflow']();
  const retainedCursor=greeting.controller.status().pending.cursor;
  const before=greeting.calls.length,events=greeting.flowRequests.length;
  const hello=await greeting.send('low',[{role:1,content:[new Text('hello there')]}],{modelOptions:{_conversationId:'new-chat'}});
  assert(hello.some(p=>p.value?.includes('Conversational answer')));
  assert.deepEqual(greeting.calls.slice(before).map(c=>c.op),['conversation']);
  assert(!greeting.flowRequests.slice(events).some(c=>['step','cancel','restart_review','resume'].includes(c.event)));
  assert.equal(greeting.controller.status().pending.flow_id,retained.flow_id);
  assert.equal(greeting.controller.status().pending.cursor,retainedCursor);
  assert(!greeting.controller.status().armed);
 }
 checks.push('greetings bypass saved review recovery, including older policies and Resume selection, with one conversation call and no arming or workspace effects');
 const routingFlow=setup();let routingStep=0;const routingEvents=[];
 const routingChat=(id,model,followup)=>({route:{model},routing_followup:followup,result:{invocation_id:id,admission:{admitted:true},accepted_output:{intent:'conversation',answer:followup?'UNPUBLISHED DRAFT':'Final routed answer'},token_usage:{calls:[{invocation_id:id,model,stage:'conversation',input_tokens:100,output_tokens:20}]}}});
 routingFlow.deps.request=async m=>{
  routingEvents.push(m);
  if(m.event==='pending')return {state:'DONE',flow_id:null};
  const base={flow_id:'routed-flow',cursor:String(++routingStep),children:[]};
  if(routingStep===1)return {...base,state:'CLASSIFYING',routing_step:routingChat('a'.repeat(32),'small',{kind:'page_in'})};
  if(routingStep===2)return {...base,state:'CLASSIFYING',routing_step:routingChat('b'.repeat(32),'small',{kind:'reasoning',control:true})};
  if(routingStep===3)return {...base,state:'CLASSIFYING',routing_step:routingChat('c'.repeat(32),'small',{kind:'escalate',tier:'mid'})};
  return {...base,state:'DONE',conversation:routingChat('d'.repeat(32),'medium',null)};
 };
 routingFlow.v.window.showWarningMessage=async()=>{throw Error('Conversation escalation must not arm workspace access');};
 const routedParts=await routingFlow.send('auto',[{role:1,content:[new Text('Explain this')]}]);
 const routedText=routedParts.map(p=>p.value||'').join('');
 assert(routedText.includes('Final routed answer'));assert(!routedText.includes('UNPUBLISHED DRAFT'));
 assert(routedText.includes('Loading the requested conversation context'));assert(routedText.includes('remaining budget'));
 assert(routedText.includes('400 in')&&routedText.includes('80 out')&&routedText.includes('4 calls'));
 assert(routedText.includes('higher supported reasoning setting on the same model'));
 assert.deepEqual(routingEvents.map(e=>e.event),['pending','start','step','step','step']);
 assert(!routingFlow.controller.status().armed);assert(!routingFlow.controller.status().pending);
 checks.push('Auto pages context, increases reasoning on the same model, then escalates; withholds drafts, charges all stages, and never arms conversation-only work');
 if(process.platform==='darwin'){
 const named=setup();named.kind('report');named.st.contract.contract.constraints.tests_allowed=false;let selected;
 const previous=named.deps.backendRequest;named.deps.backendRequest=async(m,t)=>{if(m.op==='native_permissions'){selected=m.test_paths;named.st.contract.contract.constraints.tests_allowed=true;return named.st;}const x=await previous(m,t);if(m.op==='conversation')x.result.accepted_output.workspace_request={operation:'test',paths:['checks/foo_test.py','tests/support.py']};return x;};
 await named.arm();await named.send();assert.deepEqual(selected,['checks/foo_test.py','tests/support.py']);checks.push('explicit test file names are preserved without a test-prefix filter');
 }
 const paused=setup();paused.kind('report');await paused.arm();const flowRequest=paused.deps.request;let shouldPause=true;
 paused.deps.request=async(m,t)=>{
  const r=await flowRequest(m,t);
  if(shouldPause&&m.op==='orchestrate'&&r.state==='CODING')return {...r,state:'PAUSED',stop:{status:'needs_attention',mapped_files:2,source_files:3,failure_count:1,failures:[{path:'large.py',error:'Static graph work-unit allocation exceeded'}]}};
  return r;
 };
 const pausedReply=await paused.send();assert(pausedReply.some(p=>p.value?.includes('Sutra paused the review')));
 assert(pausedReply.some(p=>p.value?.includes('large.py: Static graph work-unit allocation exceeded')));
 assert(pausedReply.some(p=>p.value?.includes('Mapped 2 of 3')));assert.equal(paused.controller.status().routing.stage,'paused');
 assert(!paused.controller.status().routing.active);assert(!paused.calls.some(c=>c.op==='native_work'));
 assert.equal(paused.controller.status().pending.stop.failure_count,1);
 shouldPause=false;await paused.send('high',[{role:1,content:[new Text('continue')]}]);
 const afterPause=paused.calls.find(c=>c.op==='native_work');assert.equal(afterPause.prompt,'Fix retry');assert.equal(afterPause.tier,'high');assert.equal(afterPause.contract_hash,'contract-1');
 checks.push('mapping pauses return actionable details without a client error; original context and permission binding resume on another model');
 const tight=setup();tight.kind('report');await tight.arm();const tightRequest=tight.deps.request;
 tight.deps.request=async(m,t)=>{
  const r=await tightRequest(m,t);
  if(m.op==='orchestrate'&&r.state==='CODING')return {...r,state:'PAUSED',stop:{status:'context_budget',stage:'SYNTHESIS',error:'Review packet cannot fit: 10,350 per call; 41,403 task budget units remain.'}};
  return r;
 };
 const tightReply=await tight.send();
 assert(tightReply.some(p=>p.value?.includes('41,403 task budget units remain')));
 assert.equal(tight.controller.status().routing.stage,'paused');
 assert.equal(tight.controller.status().pending.stop.status,'context_budget');
 assert(!tight.calls.some(c=>c.op==='native_work'));
 checks.push('context allocation shortages return a saved, explained pause instead of a retriable provider exception');
 assert(tightReply.some(p=>p.value?.includes('Increase allocation')));
 assert(tightReply.some(p=>p.value?.includes('No reasoning stream was generated')));
 {
  const raw=tight.deps.request,grants=[];let previewCount=0,detail='';
  const proposal={version:0,goal:'Review project',before:{tokens:250000},used:{tokens:228975},additions:{tokens:228975},after:{tokens:478975},remaining_after:{tokens:250000}};
  tight.deps.request=async(m,t)=>{
   if(m.event==='allocation_preview'){previewCount++;return {flow_id:m.flow_id,cursor:m.cursor,state:'PAUSED',allocation_proposal:proposal};}
   if(m.event==='allocation_grant'){grants.push(m);return {...tight.controller.status().pending,cursor:'amended',allocation_version:1,allocation_amendment:{version:1}};}
   return raw(m,t);
  };
  tight.v.window.showWarningMessage=async(_m,o)=>{detail=o.detail;return undefined;};
  const callsBefore=tight.calls.length,chatBefore=tight.uiCalls.length;
  await tight.commands['karma.increaseAllocation']({confirmed:true,proposal:{after:{tokens:999999}}});
  assert.equal(grants.length,0);assert.equal(previewCount,1);
  assert(detail.includes('250,000 + 228,975 = 478,975'));assert(detail.includes('does not reset spending'));
  tight.v.window.showWarningMessage=async(_m,_o,label)=>label;
  await tight.commands['karma.increaseAllocation']();
  assert.equal(grants.length,1);assert.deepEqual(grants[0].proposal,proposal);assert.equal(grants[0].confirmed,true);
  assert.equal(tight.calls.length,callsBefore);assert(!tight.uiCalls.slice(chatBefore).some(x=>x[0].includes('chat.')));
  assert.equal(tight.controller.status().pending.allocation_version,1);
  assert.equal(tight.controller.status().pending.allocation_proposal,undefined);
  assert.equal(tight.controller.status().pending.allocation_amendment,undefined);
  assert.equal(tight.controller.status().pending.state,'PAUSED');
  checks.push('allocation preview requires explicit modal acceptance, ignores command arguments, preserves chat and does not dispatch a model');
 }

 const pressured=setup();pressured.kind('report');await pressured.arm();const pressuredRequest=pressured.deps.request;
 pressured.deps.request=async(m,t)=>{const r=await pressuredRequest(m,t);return m.op==='orchestrate'&&r.state==='CODING'?{...r,state:'PAUSED',stop:{status:'resource_wait',resume_phase:'CODING',error:'RESOURCE_WAIT: OS thermal pressure: serious; kernel: BRIDGE'}}:r;};
 const pressureReply=await pressured.send();
 assert(pressureReply.some(p=>p.value?.includes('Last model allocation')));assert(pressureReply.some(p=>p.value?.includes('will not automatically retry')));
 assert.equal(pressured.controller.status().routing.stage,'paused');assert(!pressured.calls.some(c=>c.op==='native_work'));
 checks.push('thermal/resource waits produce normal chat pauses with recovery guidance and no workspace dispatch');
 {
  const fallback=setup();fallback.kind('report');await fallback.arm();
  const original=fallback.deps.request;let selected=false;
  fallback.deps.request=async(m,t,emit)=>{
   const out=await original(m,t,emit);
   if(m.op==='orchestrate'&&out.state==='CODING'&&!selected){
    selected=true;return {...out,resource_fallback:{from_model:'large',model:'small',tier:'low'},
     progress:'Memory admission blocked large. Auto will try small with fresh paging and the same task, permissions and remaining budget.'};
   }
   return out;
  };
  const reply=await fallback.send('auto');
  assert(reply.some(p=>p.value?.includes('Host workflow · Memory admission blocked large. Auto will try small')));
  assert.equal(fallback.calls.filter(c=>c.op==='native_work').length,1);
  assert.equal(fallback.calls.find(c=>c.op==='native_work').contract_hash,'contract-1');
  assert(!reply.some(p=>p.value?.includes('Sutra paused')));
  checks.push('host fallback progress continues the same native request with its binding, without a pause or duplicate worker call');
 }
 {
  const routed=setup();routed.kind('report');
  class Thinking{constructor(value,id,metadata){Object.assign(this,{value,id,metadata});}}
  routed.v.LanguageModelThinkingPart=Thinking;
  const real=routed.deps.request;
  routed.deps.activityLink=e=>'karma-preview:/activity/'+ 'a'.repeat(24)+'/'+e.id+'/Reasoning.txt';
  routed.deps.request=async(m,t,emit)=>{
   if(m.op==='orchestrate'&&m.event==='start'){
    const id='1'.repeat(32);emit({kind:'start',id,role:'conversation',model:'small'});
    emit({kind:'control',id,requested:false});emit({kind:'finish',id,status:'accepted',thinking_chars:0,usage:{input_tokens:2740,output_tokens:212}});
   }
   const out=await real(m,t);
   return m.op==='orchestrate'&&out.state==='CODING'?{...out,state:'PAUSED',progress:'Fresh source mapping: structural_pass_complete',stop:{status:'resource_wait',resume_phase:'SYNTHESIS',error:'RESOURCE_WAIT: Selected model does not fit'}}:out;
  };
  const reply=await routed.send('low',[{role:1,content:[new Text('Give a high-level workspace overview')]}]);
  assert.equal(reply.filter(p=>p instanceof Thinking).length,0);
  assert.equal(reply.filter(p=>p instanceof Text&&p.value.includes('/Reasoning.txt')).length,1);
  assert(reply.some(p=>p.value?.includes('No separate reasoning text received.')));
  assert(reply.some(p=>p.value?.includes('Host workflow · Fresh source mapping')));
  assert(reply.some(p=>p.value?.includes('Host resource check · Workspace synthesis paused')));
  assert(!routed.calls.some(c=>c.op==='native_work'));
  checks.push('router without reasoning followed by workspace memory pause shows one call, no phantom thinking, and explicit host stages');
 }
 const cooling=setup();let blockedResource=true,resourceFlow;
 const normalCooling=cooling.deps.request;
 cooling.deps.request=async(m,t)=>{
  if(m.op!=='orchestrate')return normalCooling(m,t);
  if(m.event==='pending')return resourceFlow||{state:'DONE',flow_id:null};
  if(m.event==='start')resourceFlow={flow_id:'resource-flow',cursor:'1',state:'PAUSED',prompt:m.prompt,children:[],stop:{status:'resource_wait',resume_phase:'CLASSIFYING',error:'RESOURCE_WAIT: memory pressure: warning'}};
  if(m.event==='resume'&&!blockedResource)resourceFlow={...resourceFlow,state:'CLASSIFYING',stop:null};
  if(m.event==='step'&&resourceFlow.state==='CLASSIFYING'){
   const conversation=await cooling.deps.backendRequest({...m,op:'conversation',prompt:resourceFlow.prompt},t);
   resourceFlow={...resourceFlow,state:'DONE',conversation};
  }
  return structuredClone(resourceFlow);
 };
 let coolReply=await cooling.send('low',[{role:1,content:[new Text('Hello')]}]);
 assert(coolReply.some(p=>p.value?.includes('Sutra paused the request')));assert(!cooling.controller.status().armed);assert.equal(cooling.calls.length,0);
 await cooling.send('high',[{role:1,content:[new Text('continue')]}]);assert.equal(cooling.calls.length,0);
 blockedResource=false;coolReply=await cooling.send('high',[{role:1,content:[new Text('continue')]}]);
 assert(coolReply.some(p=>p.value?.includes('Conversational answer')));assert.equal(cooling.calls[0].prompt,'Hello');assert.equal(cooling.calls[0].tier,'high');assert(!cooling.controller.status().armed);
 checks.push('classification resource pause resumes the original conversational prompt on another model without arming files or replaying while blocked');
 for(const approve of [false,true]){
  const upgraded=setup();upgraded.kind('report');
  const originalPrompt='Review saved workspace architecture';
  const original=await upgraded.deps.request({op:'orchestrate',event:'start',prompt:originalPrompt,tier:'low'});
  const backend=upgraded.deps.request;let restart,arms=0;
  const recovery={kind:'policy_changed',restart_allowed:true,message:'Sutra was updated. Restart retains task constraints and consumed budget.'};
  upgraded.deps.request=async(m,t)=>{
   if(m.op==='orchestrate'&&m.event==='inspect'&&m.flow_id===original.flow_id)return {...original,recovery};
   if(m.op==='orchestrate'&&m.event==='restart_review'){
    restart=structuredClone(m);await backend({...m,event:'cancel'},t);
    return backend({...m,event:'start',prompt:originalPrompt},t);
   }
   return backend(m,t);
  };
  upgraded.v.window.showWarningMessage=async(message,options,label)=>{
   if(label==='Restart review')return approve?label:undefined;
   if(label==='Arm Sutra')arms++;
   return label;
  };
  const result=await upgraded.send('high',[{role:1,content:[new Text('continue')]}]);
  if(approve){
   assert(restart?.armed);assert.equal(restart.binding.contract_hash,'contract-1');
   const work=upgraded.calls.find(c=>c.op==='native_work');assert.equal(work.prompt,originalPrompt);assert.equal(work.tier,'high');assert.equal(arms,1);
  }else{
   assert(!restart);assert.equal(arms,0);assert(!upgraded.calls.some(c=>c.op==='native_work'));
   assert(result.some(p=>p.value?.includes('Saved request retained')));
  }
  assert(!upgraded.uiCalls.some(c=>c[0]==='workbench.action.chat.newLocalChat'));
 }
 checks.push('policy upgrade recovery is explicit before arming; decline preserves saved work and approval retains the original request in the same chat on another model');
 {const live=setup();
 class Thinking{constructor(value,id,metadata){this.value=value;this.id=id;this.metadata=metadata;}}
 live.v.LanguageModelThinkingPart=Thinking;
 const realRequest=live.deps.request;let invocation=0;
 live.deps.activityLink=e=>'karma-preview:/activity/'+ 'a'.repeat(24)+'/'+e.id+'/Reasoning.txt';
 live.deps.request=async(m,t,emit)=>{
  if(m.op==='orchestrate'&&m.event==='start'){
   live.deps.lateEmit=emit;
   for(const role of ['conversation','subagent','chat_reviewer']){
   const id=String(++invocation).padStart(32,'0'),model=m.models[m.tier];
   emit?.({kind:'start',id,role,model,node_id:'child-'+invocation});
   emit?.({kind:'control',id,requested:false});
   emit?.({kind:'thinking',id,text:'UNVERIFIED: pretend tests passed'});
   emit?.({kind:'finish',id,status:role==='conversation'?'accepted':'withheld',usage:{input_tokens:10,output_tokens:20}});
   }
  }
  return realRequest(m,t);
 };
 const first=await live.send('low',[{role:1,content:[new Text('hello')]}]);
 assert.equal(first.filter(p=>p instanceof Text&&p.value.includes('/Reasoning.txt')).length,3);
 assert.equal(first.filter(p=>p instanceof Thinking&&p.metadata?.vscode_reasoning_done).length,3);
 assert.equal(new Set(first.filter(p=>p instanceof Thinking).map(p=>p.id)).size,3);
 assert(first.some(p=>p instanceof Thinking&&p.value.includes('off requested')));
 assert(first.some(p=>p instanceof Thinking&&p.value.includes('task completion and answer correctness are not verified')));
 const beforeLate=first.length;live.deps.lateEmit({kind:'thinking',id:'stale',text:'STALE'});assert.equal(first.length,beforeLate);
 const second=await live.send('high',[{role:1,content:[new Text('hello again')]}]);
 assert(first.some(p=>p instanceof Thinking&&p.value.includes('UNVERIFIED')));
 assert(!first.some(p=>p instanceof Text&&p.value.includes('UNVERIFIED')));
 assert(!JSON.stringify(live.calls).includes('UNVERIFIED'));
 assert(first.some(p=>p instanceof Thinking&&p.value.includes('model-a')));
 assert(second.some(p=>p instanceof Thinking&&p.value.includes('model-c')));
 assert.notEqual(first.find(p=>p instanceof Thinking).id,second.find(p=>p instanceof Thinking).id);
 delete live.v.LanguageModelThinkingPart;
 assert((await live.send('low',[{role:1,content:[new Text('hello')]}])).some(p=>p instanceof Text&&p.value.includes('Conversational answer')));
 checks.push('three calls in one response have independent reasoning links and end markers; parent/child reasoning is a separate optional stream; model switches use distinct identities; it never becomes text answers, memory or action proof');
 }
 console.log(JSON.stringify({passed:true,checks},null,2));
})().catch(e=>{console.error(e);process.exitCode=1;});

// Observe-only summaries cannot impersonate host delivery or render model links.
{
 const {observationText}=require('../native_provider');
 const commitment={status:'recorded',version:1,request:'Explain this code',value:{understanding:'Read [click](command:bad)',criteria:[{deliverable:'Explain flow',done_when:'Source-backed account'}]}};
 const assessment={status:'recorded',value:{delivered:'All done [run](command:bad)',judgement:'complete',remaining:[]},issues:[]};
 const start=observationText({commitment});assert(start.includes('observe only'));assert(!start.includes('[click](command:bad)'));
 const end=observationText({commitment,assessment,delivery_status:'withheld'},true);
 assert(end.includes('Response withheld.'));assert(end.includes('(unverified)'));assert(!end.includes('[run](command:bad)'));
 assert(observationText({commitment,assessment,delivery_status:'awaiting_action'},true).includes('execution is pending'));
 assert(observationText({commitment,assessment:{status:'malformed'},delivery_status:'admitted'},true).includes('Completion assessment: malformed'));
 assert.equal(observationText({}), '');
}
