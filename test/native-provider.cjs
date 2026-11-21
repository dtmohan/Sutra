'use strict';
const assert=require('node:assert/strict');
const {install,latestRequest}=require('../native_provider');
const {forRequest}=require('../model_selection');
const noop=()=>{},disposable=()=>({dispose:noop});
class EventEmitter{event=()=>disposable();fire(){}dispose(){}}
class Text{constructor(value){this.value=value;}}
class Call{constructor(callId,name,input){Object.assign(this,{callId,name,input});}}
class Result{constructor(callId,content){Object.assign(this,{callId,content});}}
class CTS{constructor(){const listeners=[];this.token={isCancellationRequested:false,onCancellationRequested:f=>{listeners.push(f);return disposable();}};this.cancel=()=>{this.token.isCancellationRequested=true;for(const f of listeners)f();};}dispose(){}}
const lastInvocation=new WeakMap();
function setup(saved={}){
 const configurationListeners=[];
 let provider,tool,accessTool,agentAccessTool,kind='edit',pick=0,discoveries=0,hold;const commands={},contextKeys={},uiCalls=[],copied=[],calls=[],statuses=[],routeEvents=[],models={low:'model-a',mid:'model-b',high:'model-c'},cfg={nativeEnabled:true},policies={},overrides={};
 const st={session:'task-1',contract:{contract_hash:'contract-1',contract:{goal:'Fix retry',constraints:{native_task:true,allowed_paths:['src/retry.py'],edits_allowed:true,tests_allowed:true,network_allowed:false}}},calibration:{binding:{hash:'cal-1'}}};
 const v={env:{clipboard:{writeText:async text=>copied.push(text)}},EventEmitter,CancellationTokenSource:CTS,LanguageModelTextPart:Text,LanguageModelToolCallPart:Call,LanguageModelToolResultPart:Result,LanguageModelToolResult:class{constructor(content){this.content=content;}},LanguageModelChatMessageRole:{User:1,Assistant:2},LanguageModelChatToolMode:{Auto:1,Required:2},ConfigurationTarget:{Global:1},MarkdownString:class{value='';appendText(t){this.value+=t;}appendCodeblock(t){this.value+='\n'+t;}},Uri:{file:p=>({fsPath:p})},workspace:{getConfiguration:()=>({get:(k,d)=>overrides[k]??cfg[k]??d,inspect:k=>({globalValue:cfg[k],policyValue:policies[k]}),update:async(k,x)=>{cfg[k]=x;}}),onDidChangeConfiguration:callback=>{configurationListeners.push(callback);return disposable();}},window:{showErrorMessage:async message=>{uiCalls.push(['error',message]);},showInformationMessage:async message=>{uiCalls.push(['notice',message]);},showWarningMessage:async(_m,_o,...labels)=>labels.at(-1),showQuickPick:async choices=>{pick++;return choices[0];}},commands:{getCommands:async()=>['workbench.action.chat.newLocalChat'],executeCommand:async(...args)=>{if(args[0]==='setContext')contextKeys[args[1]]=args[2];else uiCalls.push(args);},registerCommand:(n,f)=>{commands[n]=f;return disposable();}},lm:{selectChatModels:async()=>{discoveries++;return provider.provideLanguageModelChatInformation();},registerLanguageModelChatProvider:(n,p)=>{assert.equal(n,'karma');provider=p;return disposable();},registerTool:(n,t)=>{if(n==='karma_workspace_access')accessTool=t;else if(n==='karma_workspace_agent_access')agentAccessTool=t;else{assert.equal(n,'karma_native_action');tool=t;}return disposable();}}};
 let uriHandler;
 v.window.registerUriHandler=handler=>{uriHandler=handler;return disposable();};
 const deps={workspace:()=>'/project',authorization:{
  scope:()=>deps.workspace(),read:()=>saved.access,write:record=>{saved.access=structuredClone(record);}
 },isBusy:()=>false,modelSettings:()=>models,configureModels:async()=>models,onStatus:s=>statuses.push(s),onRoute:r=>routeEvents.push(r),publishReview:noop,showDiff:async()=>{},request:async(m,t)=>{
  calls.push(structuredClone(m));if(m.op==='state')return structuredClone(st);
  if(m.op==='conversation_read')return {result:{invocation_id:m.invocation_id,admission:{admitted:true},accepted_output:{summary:'retained context'}}};
  if(m.op==='conversation'){
   const next=Math.max(calls.length,(lastInvocation.get(saved)||0)+1);
   lastInvocation.set(saved,next);
   return {route:{model:models[m.tier]},result:{invocation_id:String(next).padStart(32,'0'),admission:{admitted:true},accepted_output:{answer:'Conversational answer',summary:'compact note',intent:/hello|brainstorm|nickname/i.test(m.prompt)?'conversation':'workspace'}}};
  }
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
  if(m.event==='recoverable_review')return {state:'DONE',flow_id:null};
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
 const send=async(tier='low',messages=[{role:1,content:[new Text('Fix retry')]}],options={tools:[{name:'karma_native_action'},{name:'karma_workspace_access'},{name:'karma_workspace_agent_access'},{name:'run_in_terminal'}]})=>{const parts=[];await provider.provideLanguageModelChatResponse({id:'karma-'+tier},messages,options,{report:p=>parts.push(p)},token.token);return parts;};
 return {changeConfiguration:key=>{for(const listener of configurationListeners)listener({affectsConfiguration:name=>name===key});},flowRequests,statuses,routeEvents,policies,overrides,v,provider,tool,accessTool,agentAccessTool,commands,contextKeys,uiCalls,copied,calls,st,controller,cfg,token,send,uriHandler,arm:()=>controller.requestWorkspaceAccess(),kind:k=>{kind=k;},hold:h=>{hold=h;},picks:()=>pick,discoveries:()=>discoveries,deps};
}
(async()=>{
 const checks=[];const h=setup();
 assert.equal(h.discoveries(),1);assert.equal(h.provider.provideLanguageModelChatInformation().length,4);assert.equal(h.calls.length,0);
 assert.deepEqual(h.provider.provideLanguageModelChatInformation().map(m=>m.id),['karma-auto','karma-low','karma-mid','karma-high']);
 h.cfg.defaultModel='local-assistant:latest';await h.commands['karma.useNativeChat']({id:'other-model',prompt:'BYPASS'});
 assert.equal(h.cfg.defaultModel,'karma-auto');assert.equal(h.cfg['editor.preferCopilotHarness'],false);assert.equal(h.cfg.defaultToCopilotHarness,false);assert.equal(h.cfg['editor.localAgent.enabled'],true);assert(h.controller.status().defaultsReady);assert.equal(h.statuses.at(-1).selecting,false);assert(!h.controller.status().armed);assert.equal(h.calls.length,0);
 assert.deepEqual(h.uiCalls[0],['workbench.panel.chat.view.copilot.focus']);assert.deepEqual(h.uiCalls[1],['workbench.action.chat.newLocalChat']);
 assert.deepEqual(h.uiCalls[2],['workbench.action.chat.open',{modelSelector:{vendor:'karma',id:'karma-auto',family:'karma'},toolsInclude:['karma_native_action','karma_workspace_access','karma_workspace_agent_access']}]);
 assert(!('query' in h.uiCalls[2][1]));checks.push('explicit native chat command saves unique default, selects scoped chat model, preserves disarming and never submits a prompt');
 {
  const thinking=setup();
  thinking.cfg.thinking='adaptive';
  const thinkNext='karma.toggleThinkingForNextRequest',cancelThinking='karma.cancelThinkingForNextRequest';
  const thinkingQueued='sutraThinkingForNextRequest';
  const hello=[{role:1,content:[new Text('Hello')]}];
  await thinking.commands[thinkNext]();
  assert(thinking.contextKeys[thinkingQueued]);
  await assert.rejects(thinking.send('low',[{role:1,content:[new Text(' ')]}]),/text request/);
  assert(thinking.contextKeys[thinkingQueued]);
  await thinking.commands[cancelThinking]();
  assert(!thinking.contextKeys[thinkingQueued]);
  await thinking.send('low',hello);
  assert.equal(thinking.flowRequests.find(x=>x.event==='start').think_for_turn,undefined);
  await thinking.commands[thinkNext]();
  await thinking.arm();
  await thinking.send();
  assert(thinking.flowRequests.some(x=>x.event==='step'));
  assert(thinking.flowRequests.filter(x=>x.prompt==='Fix retry'||x.event==='step').every(x=>x.think_for_turn===true));
  assert(!thinking.contextKeys[thinkingQueued]);
  await thinking.send('high',hello);
  assert.equal(thinking.flowRequests.at(-1).think_for_turn,undefined);
  assert.equal(thinking.cfg.thinking,'adaptive');
  checks.push('chat toggle is cancellable, skips invalid requests and applies across one governed turn before returning to Adaptive');
 }
 const confirm=h.v.window.showWarningMessage;h.v.window.showWarningMessage=async()=>undefined;
 const setupReply=await h.send();assert(setupReply.some(p=>p.value?.includes('Workspace access needed')));assert.equal(h.calls.length,1);assert.equal(h.calls[0].op,'conversation');checks.push('declining workspace access cannot read source or create a task');h.v.window.showWarningMessage=confirm;
 assert.equal(h.controller.status().routing.stage,'awaiting_workspace_permission');assert(h.controller.status().permissionCanChoose);assert(!h.controller.status().routing.active);assert(!h.controller.status().armed);
 await h.arm();assert.equal(h.picks(),0);assert(h.controller.status().armed);assert(h.controller.status().label.includes('routing unverified'));assert(!h.calls.some(c=>c.op==='new'));checks.push('one confirmation arms reading; command arguments cannot bypass it or create a task');
 const history=[{role:1,content:[new Text('OLD SECRET HISTORY')]},{role:2,content:[new Text('old answer')]},{role:1,content:[new Text('<userRequest>continue</userRequest>')]}];
 const parts=await h.send('low',history),call=parts.find(p=>p instanceof Call);assert.equal(call.name,'karma_native_action');assert(!h.calls.some(c=>c.op==='native_execute'));
 assert(parts.some(p=>p instanceof Text&&p.value.includes('proposal')));assert(!parts.some(p=>p.value?.includes('protocol invocation')));assert(h.routeEvents.some(r=>r.stage==='received'&&r.active));assert.equal(h.routeEvents.at(-1).stage,'admitted');assert(!h.routeEvents.at(-1).active);assert(h.controller.status().label.includes('routing unverified'));checks.push('routing proof comes from actual provider entry and protocol invocation; idle and armed status never claims focused-chat coverage');
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
 {
  const note='This review used the configured Low tier after a model-capacity refusal.';
  assert(!body.includes(note));
  for(const [route,admitted,expected] of [
   [{model:'model-a',tier:'low',mode:'resource-low',reason:'governed resource fallback'},true,true],
   [{model:'model-a',tier:'low',mode:'low',reason:'governed resource fallback'},true,false],
   [{model:'model-a',tier:'low',mode:'resource-low',reason:'governed resource fallback'},false,false]
  ]){
   const host=setup();await host.arm();host.kind('report');
   const original=host.deps.backendRequest;
   host.deps.backendRequest=async(m,t)=>{
    const data=await original(m,t);
    if(m.op==='native_work'){
     data.route=route;data.report_markdown='Architecture report';data.result.accepted_output.report={overview:'Architecture report'};
     data.result.admission={admitted,errors:admitted?[]:['Citations not admitted']};
    }
    return data;
   };
   const response=(await host.send('auto')).map(part=>part.value||'').join('');
   assert.equal(response.includes(note),expected);
   assert.equal(host.controller.status().lastRoute,'model-a');
  }
  checks.push('committed resource fallback discloses the configured Low review tier without attributing withheld or unrelated routes');
 }
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
 h.kind('edit');const c2=(await h.send()).find(p=>p instanceof Call);h.commands['karma.disarmNative']();await assert.rejects(h.tool.invoke({input:c2.input},h.token.token),/access is required/);
 await h.arm();await assert.rejects(h.tool.invoke({input:c2.input},h.token.token),/ticket/);const beforeChange=(await h.send()).find(p=>p instanceof Call);
 h.st.contract.contract_hash='changed';
 await assert.rejects(h.tool.invoke({input:beforeChange.input},h.token.token),/Task or calibration changed/);
 assert(h.controller.status().armed);
 assert((await h.send()).some(p=>p instanceof Call));
 checks.push('revocation and contract changes invalidate action tickets while a new contract can reuse workspace permission');
 const c=setup();await c.arm();let release,entered;const started=new Promise(r=>entered=r);c.hold(t=>new Promise(r=>{release=r;entered();}));const pending=c.send();await started;assert(c.controller.status().routing.active);assert.equal(c.controller.status().label,'Sutra: request received');
 const activeRoute=structuredClone(c.controller.status().routing),activeCalls=c.calls.length,activeFlows=c.flowRequests.length;
 const concurrent=await c.send('high');assert(concurrent.some(part=>part.value?.includes('Sutra already has a request running')));
 assert.equal(c.calls.length,activeCalls);assert.equal(c.flowRequests.length,activeFlows);assert.deepEqual(c.controller.status().routing,activeRoute);
 c.commands['karma.disarmNative']();release();await assert.rejects(pending,/cancelled|permission changed/);assert(!c.calls.some(x=>x.op==='native_preview'));assert.equal(c.controller.status().routing.stage,'cancelled');assert(!c.controller.status().routing.active);checks.push('concurrent chat gets host guidance without another dispatch or replacing the active route; revocation still withholds its action');
 {
  const saved={},host=setup(saved);await host.arm();host.kind('report');
  const options={modelOptions:{_conversationId:'same-chat-busy'},tools:[]};
  let entered,release;const started=new Promise(resolve=>entered=resolve);
  host.hold(()=>new Promise(resolve=>{release=resolve;entered();}));
  const current=host.send('low',undefined,options);await started;
  await host.commands['karma.toggleThinkingForNextRequest']();
  const before=structuredClone(saved),route=host.controller.status().routing,traffic=host.flowRequests.length;
  const busy=await host.send('high',[{role:1,content:[new Text('continue')]}],options);
  assert(busy.some(part=>part.value?.includes('current request in this chat')));
  assert.deepEqual(saved,before,'A busy reply cannot overwrite saved tasks or runner identity');
  assert.deepEqual(host.controller.status().routing,route);assert.equal(host.flowRequests.length,traffic);
  assert(host.contextKeys.sutraThinkingForNextRequest,'An ignored overlap cannot consume the next-request thinking override');
  release();await current;host.hold(undefined);
  await host.send('high',[{role:1,content:[new Text('hello')]}],options);
  assert.equal(host.flowRequests.at(-1).tier,'high');assert.equal(host.flowRequests.at(-1).think_for_turn,true);
  assert(!host.contextKeys.sutraThinkingForNextRequest);assert(!host.controller.status().working);
  checks.push('same-chat overlap preserves saved state, selected active model and next-turn thinking; a later explicit tier request proceeds normally');
 }
 {
  const host=setup();let busy=true;host.deps.isBusy=()=>busy;
  setImmediate(()=>{busy=false;});
  const reply=await host.send('low',[{role:1,content:[new Text('hello')]}]);
  assert(reply.some(part=>part.value?.includes('Conversational answer')));assert.equal(host.calls.length,1);
  assert(!host.controller.status().working);
  checks.push('transient host setup completes before the first chat dispatch rather than failing the user request');
 }
 {
  const host=setup();host.deps.isBusy=()=>true;
  const reply=await host.send('low',[{role:1,content:[new Text('hello')]}]);
  assert(reply.some(part=>part.value?.includes('finishing another host operation')));
  assert.equal(host.calls.length,0);assert.equal(host.flowRequests.length,0);assert.equal(host.controller.status().routing,null);
  host.deps.isBusy=()=>false;
  await host.send('low',[{role:1,content:[new Text('hello')]}]);assert.equal(host.calls.length,1);
  checks.push('persistent host contention gives bounded in-chat guidance without inference, saved-task mutation or a sticky busy state');
 }
 {
  const host=setup();host.deps.isBusy=()=>true;
  const waiting=host.send('low',[{role:1,content:[new Text('hello')]}]);setImmediate(()=>host.token.cancel());
  await assert.rejects(waiting,/cancelled/);
  assert.equal(host.calls.length,0);assert.equal(host.flowRequests.length,0);assert(!host.controller.status().working);
  checks.push('cancelling the host-readiness wait cannot dispatch a delayed model request');
 }
 {
  for(const asynchronous of [false,true]){
   const host=setup();
   const failed=()=>{if(asynchronous)return Promise.reject(Error('Diagnostic sink offline'));throw Error('Diagnostic sink offline');};
   host.deps.onStatus=failed;host.deps.onRoute=failed;
   await host.send('low',[{role:1,content:[new Text('hello')]}]);
   assert(!host.controller.status().routing.active);assert(!host.controller.status().working);
   await host.send('high',[{role:1,content:[new Text('hello again')]}]);
   assert.equal(host.calls.length,2);assert.equal(host.controller.status().routing.model,'model-c');
   assert(!host.controller.status().routing.active);assert(!host.controller.status().working);
  }
  checks.push('throwing or rejected status and route sinks cannot fail chat or strand execution ownership');
 }
 const d=setup();await d.arm();const cancelledCall=(await d.send()).find(p=>p instanceof Call);d.token.cancel();await assert.rejects(d.tool.invoke({input:cancelledCall.input},d.token.token),/cancelled/);assert(!d.calls.some(x=>x.op==='native_execute'));checks.push('already-cancelled tool invocation cannot execute');
 const refused=setup();await refused.arm();const denied=(await refused.send()).find(p=>p instanceof Call);refused.v.window.showWarningMessage=async()=>undefined;await assert.rejects(refused.tool.invoke({input:denied.input},refused.token.token),/not confirmed/);assert(!refused.calls.some(c=>c.op==='native_execute'));checks.push('programmatic action without a chat token requires explicit host confirmation');
 const e=setup();await e.commands['karma.toggleNative']();assert.deepEqual(e.provider.provideLanguageModelChatInformation(),[]);await assert.rejects(e.arm(),/Enable/);await assert.rejects(e.commands['karma.useNativeChat'](),/Enable/);checks.push('disabled provider is removed from discovery and cannot arm or select');
 {
  const saved={access:{scope:'/project',id:'saved-grant',profile:'read_only'},'sutra.pendingOrchestration':{workspace:'/project',flow_id:'saved-review',state:'PAUSED',spent:{tokens:724}}};
  const control=setup(saved);await control.commands['karma.toggleNative']();
  const retained=structuredClone(saved);let offered;
  control.v.window.showQuickPick=async(items,options)=>{offered={items,options};return items.find(x=>x.id==='enable');};
  await control.commands['karma.nativeControls']({id:'disable'});
  assert.equal(offered.options.title,'Sutra — off');assert(!offered.items.some(x=>x.id==='access'));
  assert.equal(control.cfg.nativeEnabled,true);assert.equal(control.controller.status().enabled,true);
  assert.equal(control.provider.provideLanguageModelChatInformation().length,4);
  assert.equal(control.calls.length,0);assert.equal(control.flowRequests.length,0);
  assert(!control.uiCalls.some(x=>String(x[0]).startsWith('workbench.')),'Enable alone must keep the existing chat');
  assert.deepEqual(saved,retained,'Enable must not reset saved grants, task context or spending');
  control.v.window.showQuickPick=async items=>items.find(x=>x.id==='disable');
  await control.commands['karma.nativeControls']();assert.equal(control.cfg.nativeEnabled,false);assert.deepEqual(saved,retained);
  control.v.window.showQuickPick=async items=>items.find(x=>x.id==='enable-chat');
  await control.commands['karma.nativeControls']();
  assert.equal(control.cfg.nativeEnabled,true);assert.equal(control.cfg.defaultModel,'karma-auto');
  assert(control.uiCalls.some(x=>x[0]==='workbench.action.chat.newLocalChat'));
  assert(control.uiCalls.some(x=>x[0]==='workbench.action.chat.open'&&x[1].modelSelector.id==='karma-auto'));
  assert.equal(control.calls.length,0);assert.equal(control.flowRequests.length,0);assert.deepEqual(saved,retained);
  checks.push('bottom-bar controls enable/disable persistently, keep current chat on enable alone, explicitly open Sutra Auto without prompts or file grants, and retain saved tasks and spending');
 }
 {
  const cancelled=setup();await cancelled.commands['karma.toggleNative']();
  cancelled.v.window.showQuickPick=async()=>undefined;
  await cancelled.commands['karma.nativeControls']({id:'enable'});
  assert.equal(cancelled.cfg.nativeEnabled,false);assert.equal(cancelled.calls.length,0);
  const stale=setup();await stale.commands['karma.toggleNative']();
  stale.v.window.showQuickPick=async items=>{stale.cfg.nativeEnabled=true;return items.find(x=>x.id==='enable');};
  await stale.commands['karma.nativeControls']();assert.equal(stale.cfg.nativeEnabled,true);
  assert(stale.uiCalls.some(x=>x[0]==='notice'&&x[1].includes('changed')));
  const workspace=setup();workspace.v.window.showQuickPick=async items=>{workspace.deps.workspace=()=>'/other';return items.find(x=>x.id==='disable');};
  await workspace.commands['karma.nativeControls']();assert.equal(workspace.cfg.nativeEnabled,true);
  let release,shown=0;const duplicate=setup();
  duplicate.v.window.showQuickPick=items=>{shown++;return new Promise(r=>{release=()=>r(undefined);});};
  const first=duplicate.commands['karma.nativeControls']();await duplicate.commands['karma.nativeControls']();
  assert.equal(shown,1);release();await first;
  const external=setup();external.cfg.nativeEnabled=false;external.changeConfiguration('karma.nativeEnabled');
  assert.equal(external.statuses.at(-1).enabled,false);assert.equal(external.statuses.at(-1).label,'Sutra: off');
  external.cfg.nativeEnabled=true;external.changeConfiguration('karma.nativeEnabled');assert.equal(external.statuses.at(-1).enabled,true);
  assert.equal(external.calls.length,0);
  checks.push('cancelled, stale and duplicate status menus cannot silently change settings or act in another workspace; command arguments cannot choose an action');
 }
 {
  const busy=setup();busy.deps.isBusy=()=>true;let offered;
  busy.v.window.showQuickPick=async(items,options)=>{offered={items,options};return items.find(x=>x.id==='monitor');};
  await busy.commands['karma.nativeControls']();assert.deepEqual(offered.items.map(x=>x.id),['monitor']);
  assert.equal(offered.options.title,'Sutra — working');assert.equal(busy.cfg.nativeEnabled,true);
  assert(busy.uiCalls.some(x=>x[0]==='karma.openMonitor'));assert.equal(busy.calls.length,0);
  const race=setup();race.v.window.showQuickPick=async items=>{race.deps.isBusy=()=>true;return items.find(x=>x.id==='disable');};
  await race.commands['karma.nativeControls']();assert.equal(race.cfg.nativeEnabled,true);
  const settings=setup();for(const [id,command] of [['models','karma.configureModels'],['reasoning','karma.configureReasoning'],['monitor','karma.openMonitor']]){
   settings.v.window.showQuickPick=async items=>items.find(x=>x.id===id);await settings.commands['karma.nativeControls']();
   assert(settings.uiCalls.some(x=>x[0]===command));
  }
  assert.equal(settings.calls.length,0);assert.equal(settings.flowRequests.length,0);
  checks.push('busy status menus keep Monitor available and recheck busy state; model and reasoning actions use existing controls without dispatch');
 }
 {
  const managed=setup();await managed.commands['karma.toggleNative']();managed.policies.nativeEnabled=false;
  managed.v.window.showQuickPick=async items=>items.find(x=>x.id==='enable');
  await managed.commands['karma.nativeControls']();assert.equal(managed.cfg.nativeEnabled,false);
  assert(managed.uiCalls.some(x=>x[0]==='error'&&x[1].includes('organization')));
  const overridden=setup();await overridden.commands['karma.toggleNative']();overridden.overrides.nativeEnabled=false;
  overridden.v.window.showQuickPick=async items=>items.find(x=>x.id==='enable');
  await overridden.commands['karma.nativeControls']();assert.equal(overridden.cfg.nativeEnabled,false);
  assert(overridden.uiCalls.some(x=>x[0]==='error'&&x[1].includes('overrides')));
  const missing=setup();await missing.commands['karma.toggleNative']();missing.v.commands.getCommands=async()=>[];
  missing.v.window.showQuickPick=async items=>items.find(x=>x.id==='enable-chat');
  await missing.commands['karma.nativeControls']();assert.equal(missing.cfg.nativeEnabled,true);
  assert.equal(missing.cfg.defaultModel,undefined);assert(missing.uiCalls.some(x=>x[0]==='error'&&x[1].includes('cannot open')));
  checks.push('managed/overridden enable settings remain truthful; failed chat opening leaves enable on without misleading chat defaults');
 }
 {
  const updating=setup();await updating.commands['karma.toggleNative']();let finish;
  const original=updating.v.workspace.getConfiguration;
  updating.v.workspace.getConfiguration=(...args)=>({...original(...args),update:async(k,value)=>{
   updating.cfg[k]=value;if(k==='nativeEnabled')await new Promise(r=>{finish=r;});
  }});
  const changing=updating.commands['karma.toggleNative']();assert.equal(updating.controller.status().working,true);
  const notice=await updating.send('low',[{role:1,content:[new Text('Hello')]}]);
  assert(notice.some(part=>part.value?.includes('Sutra settings are updating')));
  assert.equal(updating.calls.length,0);finish();await changing;assert.equal(updating.controller.status().working,false);
  checks.push('a delayed enable/disable setting gives host guidance without dispatching a racing request');
 }
 {
  const disabling=setup();await disabling.arm();
  const proposal=(await disabling.send()).find(p=>p instanceof Call);let finish,sourceRead=false;
  const original=disabling.v.workspace.getConfiguration;
  disabling.v.workspace.getConfiguration=(...args)=>({...original(...args),update:async(k,value)=>{
   if(k==='nativeEnabled')await new Promise(r=>{finish=r;});disabling.cfg[k]=value;
  }});
  const changing=disabling.commands['karma.toggleNative']();assert.equal(disabling.cfg.nativeEnabled,true);
  assert.throws(()=>disabling.tool.prepareInvocation({input:proposal.input}),/controls are updating/);
  await assert.rejects(disabling.tool.invoke({input:proposal.input},disabling.token.token),/controls are updating/);
  await assert.rejects(disabling.controller.withWorkspaceAccess(disabling.token.token,async()=>{sourceRead=true;}),/controls are updating/);
  await assert.rejects(disabling.arm(),/controls are updating/);
  assert.equal(sourceRead,false);assert(!disabling.calls.some(c=>c.op==='native_execute'));
  finish();await changing;assert.equal(disabling.cfg.nativeEnabled,false);
  checks.push('a delayed disable blocks outstanding action tickets, workspace tools and access dialogs before the configuration write completes');
 }
 {
  const saved={},permission=setup(saved);permission.kind('report');
  const options={tools:[{name:'karma_native_action'},{name:'karma_workspace_access'}],modelOptions:{_conversationId:'permission-chat'}};
  const first=await permission.send('low',[{role:1,content:[new Text('Review the workspace architecture and draw its main blocks.')]}],options);
  const access=first.find(p=>p instanceof Call);assert(access,'Native chat must receive a confirmation tool call');
  assert.equal(access.name,'karma_workspace_access');assert.equal(permission.calls.length,1);
  assert(!permission.controller.status().armed);assert(!permission.flowRequests.some(r=>r.event==='step'));
  const pending=structuredClone(saved['sutra.pendingOrchestration']);
  assert.throws(()=>permission.agentAccessTool.prepareInvocation({input:access.input}),/ticket/);
  const preview=permission.accessTool.prepareInvocation({input:access.input});
  assert.equal(preview.confirmationMessages.message.isTrusted,false);
  assert(preview.confirmationMessages.message.value.includes('Review the workspace architecture'));
  assert(preview.confirmationMessages.message.value.includes('remembered'));
  assert(!first.some(p=>p.value?.includes('send **continue**')||p.value?.includes('/access/')));
  permission.v.window.showWarningMessage=async()=>{throw Error('Native tool consent must not open a separate modal');};
  const result=await permission.accessTool.invoke({input:access.input,toolInvocationToken:{}},permission.token.token);
  assert.equal(JSON.parse(result.content[0].value).status,'workspace_access_granted');
  assert.equal(permission.controller.status().accessProfile,'read_only');assert.equal(permission.calls.length,1);
  assert.equal(saved['sutra.pendingOrchestration'].flow_id,pending.flow_id);
  const continuation=[{role:2,content:first},{role:1,content:[new Result(access.callId,result.content)]}];
  await assert.rejects(permission.send('high',continuation,{...options,modelOptions:{_conversationId:'foreign-chat'}}),/another Sutra chat/);
  const reply=await permission.send('high',continuation,options);
  assert(reply.some(p=>p.value?.includes('Workspace access approved. Continuing your request.')));
  assert(reply.some(p=>p.value?.includes('proposal')));assert.equal(permission.flowRequests.filter(r=>r.event==='start').length,1);
  assert(permission.flowRequests.filter(r=>r.event==='step').every(r=>r.flow_id===pending.flow_id&&r.tier==='high'));
  assert.equal(permission.calls.filter(r=>r.op==='conversation').length,1);
  assert(!permission.calls.some(r=>r.op==='native_execute'));assert.equal(permission.controller.status().accessProfile,'read_only');
  await assert.rejects(permission.accessTool.invoke({input:access.input,toolInvocationToken:{}},permission.token.token),/ticket|Edits and tests/);
  const reloaded=setup(saved);reloaded.kind('report');
  const afterReload=await reloaded.send('low',[{role:1,content:[new Text('Review the workspace architecture again')]}],options);
  assert(!afterReload.some(p=>p instanceof Call));assert.equal(reloaded.controller.status().accessProfile,'read_only');
  const legacy=setup();legacy.kind('report');
  const legacyFirst=await legacy.send();const legacyCall=legacyFirst.find(p=>p instanceof Call);
  const legacyResult=await legacy.accessTool.invoke({input:legacyCall.input,toolInvocationToken:{}},legacy.token.token);
  const legacyReply=await legacy.send('high',[{role:2,content:legacyFirst},{role:1,content:[new Result(legacyCall.callId,legacyResult.content)]}]);
  assert(legacyReply.some(p=>p.value?.includes('Workspace access approved')));
  assert.equal(legacy.calls.filter(r=>r.op==='conversation').length,1);
  checks.push('native workspace approval resumes the exact saved request automatically on another model without classifier replay, manual continue, modal or copied key; reload reuses read-only access');
 }
 {
  const denied=setup();const first=await denied.send();const call=first.find(p=>p instanceof Call);
  denied.v.window.showWarningMessage=async()=>undefined;
  await assert.rejects(denied.accessTool.invoke({input:call.input},denied.token.token),/not granted/);
  assert(!denied.controller.status().armed);assert(!denied.flowRequests.some(r=>r.event==='step'));
  assert.equal(denied.calls.filter(r=>r.op==='conversation').length,1);
  const denial=await denied.send('high',[{role:2,content:first},{role:1,content:[new Result(call.callId,[new Text('User did not approve this tool')])]}]);
  assert(denial.some(p=>p.value?.includes('Workspace access was not granted')));
  assert.equal(denied.calls.length,1);assert(!denied.controller.status().armed);
  denied.v.window.showWarningMessage=async(_message,_options,label)=>label;
  await denied.accessTool.invoke({input:call.input},denied.token.token);assert.equal(denied.controller.status().accessProfile,'read_only');
  for(const change of ['cancel','revoke','workspace','pending','dispose','disable']){
   const state={},item=setup(state);const parts=await item.send();const t=parts.find(p=>p instanceof Call);
   if(change==='cancel')item.token.cancel();
   if(change==='revoke')item.commands['karma.disarmNative']();
   if(change==='workspace')item.deps.workspace=()=>'/another-project';
   if(change==='pending')state['sutra.pendingOrchestration'].cursor='999';
   if(change==='dispose')item.controller.dispose();
   if(change==='disable')await item.commands['karma.toggleNative']();
   await assert.rejects(item.accessTool.invoke({input:t.input,toolInvocationToken:{}},item.token.token));
   assert(!item.controller.status().armed);assert(!item.flowRequests.some(r=>r.event==='step'));
  }
  checks.push('workspace consent cannot be forged, replayed, used in another chat/workspace, or granted after cancellation, revocation, disable or lost pending state; programmatic calls require explicit approval');
 }
 {
  const state={access:{id:'revoked-before-reload',revoked:true}},revoked=setup(state);
  const parts=await revoked.send();const call=parts.find(p=>p instanceof Call);let prompts=0;
  revoked.v.window.showWarningMessage=async(_title,options)=>{prompts++;assert(options.detail.includes('was revoked'));return undefined;};
  await assert.rejects(revoked.accessTool.invoke({input:call.input,toolInvocationToken:{}},revoked.token.token),/not granted/);
  assert.equal(prompts,1);assert(!revoked.controller.status().armed);assert(state.access.revoked);
  assert(!revoked.flowRequests.some(r=>r.event==='step'));
  revoked.v.window.showWarningMessage=async(_title,_options,label)=>label;
  await revoked.accessTool.invoke({input:call.input,toolInvocationToken:{}},revoked.token.token);
  assert.equal(revoked.controller.status().accessProfile,'read_only');assert(!state.access.revoked);
  revoked.commands['karma.disarmNative']();assert.equal(state.access.revoked,true);
  checks.push('a remembered native Allow-in-Session cannot bypass persisted workspace revocation; explicit new consent is required after reload, and read consent cannot authorize the separate agent-access tool');
 }
 {
  const stopped=setup();stopped.deps.request=async()=>{throw Error('Child is quarantined or cancelled; result cannot be joined');};
  await assert.rejects(stopped.send(),/Child is quarantined/);
  assert.equal(stopped.controller.status().routing.stage,'blocked');
  assert(!stopped.token.token.isCancellationRequested);
  checks.push('an error describing a cancelled child does not falsely label the current user request as cancelled');
 }
 {
  const rejected=setup();await rejected.arm();const original=rejected.deps.request;
  rejected.deps.request=async(m,t)=>{
   const result=await original(m,t);
   if(m.op==='orchestrate'&&result.state==='CODING')return {...result,state:'PAUSED',stop:{status:'child_review_rejected',error:'Expected a structured child report.',retryable:true}};
   return result;
  };
  const parts=await rejected.send();
  assert(parts.some(p=>p.value?.includes('rejected output has not been used')));
  assert(parts.some(p=>p.value?.includes('one bounded replacement')));
  assert.equal(rejected.controller.status().routing.stage,'paused');assert(!rejected.token.token.isCancellationRequested);
  checks.push('rejected child output is a saved validation pause with bounded-retry guidance rather than a failed or cancelled parent request');
 }
 {
  const repeated=setup();await repeated.arm();const original=repeated.deps.request;
  repeated.deps.request=async(m,t)=>{
   const result=await original(m,t);
   if(m.op==='orchestrate'&&result.state==='CODING')return {...result,state:'PAUSED',stop:{status:'child_repair_unchanged_request',resume_phase:'CHILDREN',branch_id:'saved-child',error:'Identical JSON repair would repeat.'}};
   return result;
  };
  const parts=await repeated.send();
  assert(parts.some(p=>p.value?.includes('unused repair attempt are saved')));
  assert(parts.some(p=>p.value?.includes('budget increase alone does not change this request')));
  assert(!parts.some(p=>p.value?.includes('one bounded replacement')));
  assert.equal(repeated.controller.status().pending.stop.branch_id,'saved-child');
  assert.equal(repeated.controller.status().routing.stage,'paused');
  assert(!repeated.token.token.isCancellationRequested);
  checks.push('an unchanged failed child request stays paused with the same checkpoint and material-change guidance, without a replacement or automatic retry');
 }
 const missing=setup();missing.v.lm.selectChatModels=async()=>[];await assert.rejects(missing.commands['karma.useNativeChat'](),/not available/);assert.equal(missing.cfg.defaultModel,undefined);checks.push('failed discovery cannot save an unavailable default');
 const policy=setup();policy.policies.defaultToCopilotHarness=true;await assert.rejects(policy.commands['karma.useNativeChat'](),/organization manages/);assert.equal(policy.cfg.defaultModel,undefined);assert.equal(policy.uiCalls.length,0);
 const override=setup();override.overrides.defaultToCopilotHarness=true;await assert.rejects(override.commands['karma.useNativeChat'](),/overrides/);assert.equal(override.cfg.defaultModel,undefined);assert.equal(override.cfg['editor.preferCopilotHarness'],undefined);assert.equal(override.uiCalls.length,0);
 const unsupported=setup();unsupported.v.commands.getCommands=async()=>[];await assert.rejects(unsupported.commands['karma.useNativeChat'](),/cannot open/);assert.equal(unsupported.cfg.defaultModel,undefined);
 const failed=setup();failed.cfg.defaultModel='old';failed.cfg.defaultToCopilotHarness=true;failed.v.commands.executeCommand=async()=>{throw Error('Open failed');};await assert.rejects(failed.commands['karma.useNativeChat'](),/Open failed/);assert.equal(failed.cfg.defaultModel,'old');assert.equal(failed.cfg.defaultToCopilotHarness,true);assert.equal(failed.cfg['editor.preferCopilotHarness'],undefined);
 checks.push('policy, workspace overrides, unsupported chat and opening failures do not leave misleading defaults or claim routing');
 const pendingArm=setup();let pickDone,armDialogs=0;pendingArm.v.window.showWarningMessage=()=>{armDialogs++;return new Promise(resolve=>{pickDone=resolve;});};const arming=pendingArm.arm();
 while(!pickDone)await new Promise(r=>setImmediate(r));assert(pendingArm.controller.status().arming);assert.equal(pendingArm.controller.status().label,'Sutra: awaiting workspace access');
 const again=pendingArm.arm();assert.equal(armDialogs,1,'Concurrent access commands must share one modal choice');const during=await pendingArm.send();assert(during.some(p=>p.value?.includes('Choose workspace access in the dialog')));assert.equal(pendingArm.controller.status().routing.stage,'awaiting_confirmation');
 await pendingArm.commands['karma.useNativeChat']();assert(!pendingArm.uiCalls.some(c=>c[0]==='workbench.action.chat.newLocalChat'));pickDone(undefined);assert.deepEqual(await Promise.all([arming,again]),[false,false]);assert.equal(armDialogs,1);assert(!pendingArm.controller.status().arming);assert(!pendingArm.controller.status().armed);assert(!pendingArm.calls.some(c=>c.op==='native_work'));
 pendingArm.v.window.showWarningMessage=async(_m,_o,label)=>label;await pendingArm.arm();assert(pendingArm.controller.status().armed);assert.equal(pendingArm.controller.status().accessProfile,'read_only');
 checks.push('pending arming is visible; repeated arm/open and requests cannot interrupt or bypass the confirmation; cancellation recovers');
 const greeting=setup();const raw='  Hello\nPlease brainstorm with me.  ';const hello=await greeting.send('low',[{role:1,content:[new Text(raw)]}]);
 assert(hello.some(p=>p.value?.includes('conversation details')));assert.equal(greeting.calls[0].prompt,raw);assert.equal(greeting.calls[0].op,'conversation');assert.equal(greeting.calls.length,1);assert.equal(greeting.controller.status().routing.stage,'conversation');assert(!greeting.controller.status().armed);assert(greeting.controller.status().label.includes('chat only'));
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
 const compressed='The following is a compressed version of the preceeding history in the current conversation.';
 const summary=await compacted.send('low',[{role:1,content:[new Text(compressed+'\n<assistant>'+first.map(p=>p.value||'').join('')+'</assistant>')]},{role:1,content:[new Text(SUMMARY+'\nFocus particularly on recent tools. '+ 'x'.repeat(2000))]}]);
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
 const backgroundSummary=await background.send('low',[{role:2,content:priorBackground},{role:1,content:[new Text(SUMMARY)]}]);assert(backgroundSummary.some(p=>p.value?.includes('KARMA_CONTEXT_V1')));assert.equal(background.controller.status().routing.id,activeId);assert(background.controller.status().routing.active);background.commands['karma.disarmNative']();resumeWork();await assert.rejects(workRunning,/cancelled|permission changed/);
 checks.push('current user text cannot supply a checkpoint; workspace secrets isolate signatures; background compaction does not race or overwrite active inference state');
 {
  const {createHash,createHmac}=require('node:crypto');
  const chatA={modelOptions:{_conversationId:'resume-chat'}};
  const chatB={modelOptions:{_conversationId:'paper-chat'}};
  const sameWorkspace={},windowA=setup(sameWorkspace);
  const firstReply=await windowA.send('low',[{role:1,content:[new Text('Hello. Remember the résumé review.')] }],chatA);
  const origin=windowA.controller.status().routing.invocationId;
  const axis=createHash('sha256').update(chatA.modelOptions._conversationId).digest('hex');
  const secret=sameWorkspace['karma.nativeMemory.v1'].secret;
  const oldSig=createHmac('sha256',secret).update('karma-native-context-v1:'+origin).digest('hex');
  const scopedSig=createHmac('sha256',secret).update('karma-native-context-v2:'+axis+':'+origin).digest('hex');
  assert(firstReply.some(p=>p.value?.includes('checkpoint='+scopedSig)));
  const copiedAssistant={role:2,content:firstReply};
  const oldSummary={role:1,content:[new Text('<conversation-summary>KARMA_CONTEXT_V1:'+origin+':'+oldSig+'</conversation-summary>')]};
  const unsigned={role:2,content:[new Text('Sutra conversation receipt: '+origin)]};
  const ask={role:1,content:[new Text('Hello. This is my working paper.') ]};
  const windowB=setup(sameWorkspace);
  for(const previous of [copiedAssistant,oldSummary,unsigned]){
    await windowB.send('high',[previous,ask],chatB);
    assert.notEqual(windowB.calls.at(-1).parent,origin);
  }
  await windowB.send('high',[copiedAssistant,ask],chatA);
  assert.equal(windowB.calls.at(-1).parent,origin);
  await windowB.send('high',[oldSummary,ask],chatA);
  assert.equal(windowB.calls.at(-1).parent,origin);
  await windowB.send('high',[{role:1,content:[new Text('Copied footer '+firstReply.map(p=>p.value||'').join(''))]},ask],{});
  assert.equal(windowB.calls.at(-1).parent,undefined);
  await windowB.send('high',[unsigned,ask],{});
  assert.equal(windowB.calls.at(-1).parent,undefined);
  checks.push('signed and legacy memory checkpoints stay on their native chat axis across windows and reloads');
 }
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
 inline.v.window.showWarningMessage=async(_m,_o,...labels)=>{confirmations++;return labels.at(-1);};
 const intro=await inline.send('low',[{role:1,content:[new Text('Hello, remember this project')]}]);
 const inlinePrompt='Review this project architecture';
 const permissionReply=await inline.send('low',[{role:2,content:intro},{role:1,content:[new Text(inlinePrompt)]}]);
 assert(permissionReply.some(p=>p.value?.includes('Workspace access needed')));
 assert.equal(confirmations,0,'No dialog or model response waits for consent');
 assert(!inline.controller.status().working);assert(inline.controller.status().permissionCanChoose);
 assert(!inline.calls.some(c=>c.op==='native_work'));
 await inline.commands['karma.armNative']();assert.equal(confirmations,1);assert(inline.controller.status().armed);
 const inlineReply=await inline.send('low',[{role:2,content:permissionReply},{role:1,content:[new Text('continue')]}]);
 const conversations=inline.calls.filter(c=>c.op==='conversation'),inlineWork=inline.calls.find(c=>c.op==='native_work');
 assert.equal(conversations.length,2);assert.equal(conversations[1].parent,'1'.padStart(32,'0'));assert.equal(inlineWork.prompt,inlinePrompt);assert.equal(inlineWork.conversation_id,'2'.padStart(32,'0'));assert.equal(inlineWork.contract_hash,'contract-1');assert.equal(inlineWork.calibration_hash,'cal-1');
 assert(!inlineReply.some(p=>p.value?.includes('Conversational answer')));assert(!inline.uiCalls.some(c=>c[0].startsWith('workbench.')));
 await inline.send('high',[{role:2,content:inlineReply},{role:1,content:[new Text('Continue review')]}]);
 assert.equal(confirmations,1);const switched=inline.calls.filter(c=>c.op==='native_work').at(-1);assert.equal(switched.contract_hash,inlineWork.contract_hash);assert.equal(switched.session,inlineWork.session);assert(!inline.calls.some(c=>['auto_new','native_permissions'].includes(c.op)));
 checks.push('permission pauses end the model response normally; explicit consent and continue retain the exact request, receipt, contract and model-switch binding');
 {
  const stages=setup();stages.kind('report');await stages.arm();
  const original=stages.deps.request;let step=0,saved;
  const settled=[
   {invocation_id:'parent-reader',stage:'architect',model:'model-c',input_tokens:10,output_tokens:20,status:'accepted'},
   {invocation_id:'parent-draft',stage:'chat_reviewer',model:'model-c',input_tokens:30,output_tokens:40,status:'accepted'}
  ];
  stages.deps.request=async(m,t)=>{
   if(saved&&m.op==='orchestrate'&&m.event==='inspect')return saved;
   if(saved&&m.op==='orchestrate'&&m.event==='resume')return {...saved,
    stop:{status:'resource_wait',resume_phase:'SYNTHESIS',error:'Fixture resource wait before inference.'}};
   if(saved&&m.op==='orchestrate'&&m.event==='step'){
    step++;
    const state=step===1?'CHILDREN':step<5?'SYNTHESIS':'PAUSED';
    saved={...saved,state,cursor:String(step+3),progress:'Fixture host checkpoint '+step,
     claim_review_usage:{calls:step<3?settled.slice(0,1):step<5?settled:[...settled,
      {invocation_id:'interrupted-claim',stage:'report_reviewer',model:'model-a',input_tokens:null,output_tokens:null,status:'interrupted'}]},
     ...(state==='PAUSED'?{stop:{status:'interrupted',resume_phase:'SYNTHESIS',error:'Bridge operation deadline reached'}}:{})};
    return saved;
   }
   const out=await original(m,t);
   if(m.op==='orchestrate'&&out.state==='CODING'){saved={...out,state:'MAPPING'};return saved;}
   return out;
  };
  const reply=await stages.send(),body=reply.map(p=>p.value||'').join('');
  for(const label of ['Reading project files.','Reviewing the source in separate parts.',
    'Combining the findings and checking source references.'])assert.equal(body.split(label).length-1,1,label);
  assert.equal(stages.controller.status().routing.stage,'paused');
  assert.equal(stages.controller.status().routing.token_usage.calls.length,3);
  assert(body.includes('3 calls'));
  assert(body.includes('40 + unknown in')&&body.includes('60 + unknown out'));
  assert(!stages.calls.some(c=>c.op==='native_work'));
  const resumed=(await stages.send('high',[{role:1,content:[new Text('continue')]}])).map(p=>p.value||'').join('');
  assert(resumed.includes('Fixture resource wait before inference.'));
  assert(resumed.includes('40 + unknown in')&&resumed.includes('60 + unknown out'));
  assert(resumed.includes('3 calls'));
  assert.equal(step,5,'Blocked resume cannot dispatch another model step');
  checks.push('bounded parent checkpoints show each host phase once and retain deduplicated settled plus unknown interrupted usage before publication');
 }
 {
  const timeout=setup();timeout.kind('report');await timeout.arm();
  const original=timeout.deps.request;
  timeout.deps.request=async(m,t)=>{
   const out=await original(m,t);
   if(m.op==='orchestrate'&&out.state==='CODING')return {...out,state:'PAUSED',stop:{
    status:'claim_review_timeout',resume_phase:'SYNTHESIS',review_page:1,total_pages:3,
    retry_mode:'explicit_off',control_wait:true,
    error:'Thinking On is explicit. Select Thinking Off, then Resume the saved review.'}};
   return out;
  };
  const body=(await timeout.send('high')).map(part=>part.value||'').join('');
  for(const text of ['Thinking On is explicit','draft and completed claim-review pages are saved',
    'Change the reasoning setting indicated above','claim-review page 1 of 3','Resume this request'])assert(body.includes(text),text);
  assert.equal(timeout.controller.status().routing.stage,'paused');
  assert(!timeout.calls.some(c=>c.op==='native_work'));
  checks.push('an explicit-thinking claim timeout preserves the saved page and explains its control change without another dispatch');
 }
 for(const approve of [false,true]){
  const waiting=setup();waiting.kind('report');let decide;
  const first=await waiting.send();assert(first.some(p=>p.value?.includes('Workspace access needed')));
  waiting.v.window.showWarningMessage=(message,options,...labels)=>{
   assert.equal(message,'Workspace access for Sutra');assert.equal(options.modal,true);assert.deepEqual(labels,['Read only','Workspace agent']);
   return new Promise(resolve=>{decide=resolve;});
  };
  const consent=waiting.commands['karma.armNative']();while(!decide)await new Promise(r=>setImmediate(r));
  assert(!waiting.controller.status().routing.active,'Consent is outside the finished model call');
  assert(!waiting.controller.status().armed);assert(!waiting.flowRequests.some(r=>r.armed));
  assert.deepEqual(waiting.calls.map(c=>c.op),['conversation']);
  decide(approve?'Workspace agent':undefined);await consent;
  assert.equal(waiting.controller.status().armed,approve);
  assert(!waiting.calls.some(c=>c.op==='native_work'),'Consent alone never starts inference');
  assert(waiting.uiCalls.some(c=>c[0]==='notice'&&c[1].includes(approve?'Workspace access saved':'Workspace access was not granted')));
  if(approve)await waiting.send('high',[{role:2,content:first},{role:1,content:[new Text('continue')]}]);
  assert.equal(waiting.calls.some(c=>c.op==='native_work'),approve);
 }
 checks.push('modal decline/cancellation cannot interrupt a model stream or grant source access; approval waits for explicit continuation');
 const standalone=setup();await standalone.arm();assert(!standalone.uiCalls.some(c=>c[0].startsWith('workbench.')));assert.equal(standalone.cfg.defaultModel,undefined);
 checks.push('standalone access consent changes no chat selection, saved model, or conversation');
 for(const stop of ['disarm','disable','workspace']){
  const race=setup();await race.send();let approve;race.v.window.showWarningMessage=()=>new Promise(resolve=>{approve=resolve;});
  const consent=race.arm();const rejected=assert.rejects(consent,/cancelled|changed/i);
  while(!approve)await new Promise(r=>setImmediate(r));
  if(stop==='disarm')race.commands['karma.disarmNative']();else if(stop==='disable')await race.commands['karma.toggleNative']();else race.deps.workspace=()=>'/different';
  approve('Workspace agent');await rejected;assert(!race.controller.status().armed);assert(!race.controller.status().arming);assert.deepEqual(race.calls.map(c=>c.op),['conversation']);
 }
 const stale=setup();let accept;stale.v.window.showWarningMessage=()=>new Promise(r=>accept=r);const waiting=stale.arm();const invalidated=assert.rejects(waiting,/cancelled/);while(!accept)await new Promise(r=>setImmediate(r));stale.commands['karma.disarmNative']();accept('Workspace agent');await invalidated;assert(!stale.controller.status().armed);
 checks.push('cancel, disarm, disable and workspace changes invalidate delayed approval; concurrent requests cannot steal the pending handoff');
 const resumed=setup();resumed.kind('report');await resumed.arm();resumed.hold(()=>{throw Error('interrupted fixture');});
 await assert.rejects(resumed.send('low'),/interrupted/);resumed.hold(null);
 const beforeResume=resumed.calls.filter(c=>c.op==='conversation').length;
 const resumedReply=await resumed.send('high',[{role:1,content:[new Text('continue')]}]);
 assert.equal(resumed.calls.filter(c=>c.op==='conversation').length,beforeResume);assert(resumedReply.some(p=>p.value?.includes('Memory restored')));
 const resumedWork=resumed.calls.filter(c=>c.op==='native_work').at(-1);assert.equal(resumedWork.prompt,'Fix retry');assert.equal(resumedWork.tier,'high');assert.equal(resumedWork.contract_hash,'contract-1');
 checks.push('continue resumes the saved workflow with the original prompt and permissions on another model, without reclassifying or replaying history');
 assert(h.flowRequests.some(r=>r.event==='start'));assert(h.flowRequests.some(r=>r.event==='step'));checks.push('native UI sends workflow events; host state selects all workspace work');
 const originalChat={modelOptions:{_conversationId:'original-chat'}},otherChat={modelOptions:{_conversationId:'other-chat'}};
 const chatUser=value=>[{role:1,content:[new Text(value)]}];
 const savedCase=setup();savedCase.kind('report');await savedCase.arm();savedCase.hold(()=>{throw Error('stopped fixture');});
 await assert.rejects(savedCase.send('low',chatUser('Fix retry'),originalChat),/stopped/);savedCase.hold(null);
 savedCase.v.window.showWarningMessage=async()=>undefined;
 const retained=await savedCase.send('high',[{role:1,content:[new Text('A different request')]}]);
 assert(retained.some(p=>p.value?.includes('Saved request retained')));assert(!savedCase.flowRequests.some(r=>r.event==='cancel'));
 const oldConversationCount=savedCase.calls.filter(c=>c.op==='conversation').length;
 const workBeforeResume=savedCase.calls.filter(c=>c.op==='native_work').length;
 const resumeInstruction=()=>savedCase.copied.at(-1);
 let copyChoice,copyNotices=0;
 savedCase.v.window.showInformationMessage=async(message,...labels)=>{
  savedCase.uiCalls.push(['notice',message]);
  if(labels.includes('Copy resume instruction')){copyNotices++;return copyChoice;}
 };
 await savedCase.commands['karma.resumeWorkflow']();
 assert.equal(copyNotices,1);assert.equal(savedCase.copied.length,0);
 assert.equal(savedCase.calls.filter(c=>c.op==='native_work').length,workBeforeResume,'Dismissing Resume guidance cannot advance a task');
 copyChoice='Copy resume instruction';
 await savedCase.commands['karma.resumeWorkflow']();
 assert.equal(copyNotices,2,'Dismissing the first notice must not suppress a new explicit Resume action');
 const opened=resumeInstruction();assert.match(opened,/^Resume saved Sutra request \[[a-f0-9]{32}\]$/);
 assert(!savedCase.uiCalls.some(c=>c[0]==='workbench.action.chat.open'),'Resume cannot open Copilot or a foreign chat');
 await savedCase.commands['karma.resumeWorkflow']();assert.equal(savedCase.copied.length,1);assert.equal(copyNotices,2);
 const otherReply=await savedCase.send('auto',chatUser(opened),otherChat);
 assert(otherReply.some(p=>p.value?.includes('originating Sutra chat')));
 assert(!savedCase.calls.some(c=>c.op==='native_work'&&c.tier==='auto'));
 const manualOther=await savedCase.send('high',chatUser('continue'),otherChat);
 assert(manualOther.some(p=>p.value?.includes('originating Sutra chat')));
 await savedCase.commands['karma.resumeWorkflow']();assert.notEqual(resumeInstruction(),opened);
 const replay=await savedCase.send('auto',chatUser(opened),originalChat);assert(replay.some(p=>p.value?.includes('expired')));
 await savedCase.commands['karma.resumeWorkflow']();
 const inspectRequest=savedCase.deps.request;
 savedCase.deps.request=async(m,t)=>{const result=await inspectRequest(m,t);return m.event==='inspect'?{...result,cursor:'changed-cursor'}:result;};
 const changedCursor=await savedCase.send('auto',chatUser(resumeInstruction()),originalChat);
 assert(changedCursor.some(p=>p.value?.includes('saved Sutra request advanced')));
 savedCase.deps.request=inspectRequest;
 await savedCase.commands['karma.resumeWorkflow']();savedCase.deps.workspace=()=>'/different-workspace';
 const otherWorkspace=await savedCase.send('auto',chatUser(resumeInstruction()),originalChat);assert(otherWorkspace.some(p=>p.value?.includes('expired')));
 savedCase.deps.workspace=()=>'/project';await savedCase.commands['karma.resumeWorkflow']();
 await savedCase.send('auto',chatUser(resumeInstruction()),originalChat);
 assert.equal(savedCase.calls.filter(c=>c.op==='conversation').length,oldConversationCount);
 assert.equal(savedCase.calls.filter(c=>c.op==='native_work').at(-1).prompt,'Fix retry');
 checks.push('Monitor Resume redisplays after dismissal, copies one single-use originating-chat instruction, rejects foreign chats/workspaces/stale cursors and never opens a sign-in surface');
 const discarded=setup();discarded.kind('report');await discarded.arm();discarded.hold(()=>{throw Error('stopped fixture');});await assert.rejects(discarded.send(),/stopped/);discarded.hold(null);
 discarded.v.window.showWarningMessage=async()=> 'Discard and use new request';
 await discarded.send('high',[{role:1,content:[new Text('New request')]}]);assert(discarded.flowRequests.some(r=>r.event==='cancel'));
 assert.equal(discarded.calls.filter(c=>c.op==='conversation').at(-1).prompt,'New request');
 checks.push('new workspace intent is classified first; explicit discard releases saved work before any new workspace operation');
 {
  const persisted={},open=new Map(),traffic=[];
  const taskChat={modelOptions:{_conversationId:'task-stack-origin'}};
  const chatKey=require('node:crypto').createHash('sha256').update(taskChat.modelOptions._conversationId).digest('hex');
  let order=[],stackVersion=0,sequence=0;
  const attach=fixture=>{
   const original=fixture.deps.request;
   const response=(flow,extra={})=>{
    const focused=order.at(-1)??null;
    const open_tasks=order.map(id=>{
     const {flow_id,prompt,state,cursor}=open.get(id);
     return {flow_id,prompt,state,cursor,focused:id===focused};
    });
    const task_stack={focused,open_tasks,cursor:stackVersion?String(stackVersion):null,last_transition:null};
    return structuredClone({...flow,...extra,task_stack,focused_flow:open.get(focused)||null});
   };
   fixture.deps.request=async (m,t)=>{
    if(m.op!=='orchestrate')return original(m,t);
    assert.equal(m.chat_key,chatKey);
    traffic.push(structuredClone(m));
    if(m.event==='task_list')return response({});
    if(m.event==='recoverable_review')return response({state:'DONE',flow_id:null});
    if(m.event==='pending')return response(open.get(order.at(-1))||{state:'DONE',flow_id:null});
    if(m.event==='start'){
     const c=await fixture.deps.backendRequest({...m,op:'conversation'},t);
     const id=(++sequence).toString(16).padStart(32,'0');
     const binding={session:'session-'+sequence,contract_hash:'contract-'+sequence,calibration_hash:'cal-'+sequence};
     const flow={flow_id:id,prompt:m.prompt,state:'NEED_ARM',cursor:'1',conversation_id:c.result.invocation_id,binding};
     open.set(id,flow);order.push(id);stackVersion++;
     return response(flow,{conversation:c});
    }
    const flow=open.get(m.flow_id);assert(flow,'Host request needs a known flow ID');
    if(m.event==='inspect')return response(flow);
    assert.equal(m.cursor,flow.cursor,'Host request uses the selected task cursor');
    if(m.event==='task_focus'||m.event==='task_close'){
     assert.equal(m.stack_cursor,String(stackVersion),'Task choice needs the inspected stack revision');
     order=order.filter(id=>id!==flow.flow_id);
     stackVersion++;
     if(m.event==='task_focus')order.push(flow.flow_id);
     else{flow.state='CANCELLED';open.delete(flow.flow_id);}
     return response({});
    }
    assert(m.armed);
    assert.deepEqual(m.binding,flow.binding);
    assert.equal(order.at(-1),flow.flow_id,'Only a focused flow can run');
    flow.cursor=String(Number(flow.cursor)+1);
    if(m.event==='resume'){
     assert.equal(flow.state,'PAUSED');
     flow.state='SYNTHESIS';
     return response(flow);
    }
    assert.equal(m.event,'step');
    const previous=flow.stop?.budget.used.calls||0;
    flow.state='PAUSED';
    flow.stop={status:'resource_wait',resume_phase:'SYNTHESIS',error:'Fixture memory wait.',budget:{used:{calls:previous+1}}};
    return response(flow);
   };
  };
  const firstHost=setup(persisted);attach(firstHost);await firstHost.arm();
  firstHost.v.window.showWarningMessage=async()=>{throw Error('An independent task must not prompt to discard parked work');};
  const originalReply=await firstHost.send('low',chatUser('Review top-level architecture'),taskChat);
  const architecture=structuredClone(open.get(order[0]));
  await firstHost.send('mid',chatUser('Review parsing tests'),taskChat);
  const second=structuredClone(open.get(order[1]));
  assert.equal(open.size,2);
  assert.deepEqual(open.get(architecture.flow_id),architecture);
  assert.deepEqual(firstHost.calls.filter(c=>c.op==='conversation').map(c=>c.prompt),[architecture.prompt,second.prompt]);
  assert.equal(firstHost.controller.status().openTaskCount,2);
  assert(!traffic.some(m=>m.event==='cancel'));

  const reloadedHost=setup(persisted);attach(reloadedHost);
  assert(reloadedHost.controller.status().armed);
  const beforeDetached=traffic.length;
  const detached=await reloadedHost.send('high',chatUser('continue'),{});
  assert(detached.some(part=>part.value?.includes('chat that started it')));
  assert.equal(traffic.length,beforeDetached,'A detached chat cannot resume another chat’s task');
  let action='Focus task';
  reloadedHost.v.window.showQuickPick=async items=>items.find(item=>item.label===action||item.label===architecture.prompt);
  const beforeFocus=traffic.length;
  await reloadedHost.commands['karma.openTasks']();
  assert.equal(order.at(-1),architecture.flow_id);
  assert(traffic.slice(beforeFocus).some(m=>m.event==='task_focus'&&m.flow_id===architecture.flow_id));
  assert(!traffic.slice(beforeFocus).some(m=>['start','step','resume'].includes(m.event)));
  assert.deepEqual(open.get(architecture.flow_id),architecture);
  const beforeResume=traffic.length;
  const restoredChat=[{role:2,content:originalReply},...chatUser('continue')];
  const continued=await reloadedHost.send('high',restoredChat,{});
  const resumed=traffic.slice(beforeResume).find(m=>m.event==='resume');
  assert(continued.some(part=>part.value?.includes('Resuming the saved Sutra workflow')));
  assert.equal(resumed.flow_id,architecture.flow_id);
  assert.equal(resumed.cursor,architecture.cursor);
  assert.equal(resumed.tier,'high');
  assert.deepEqual(resumed.binding,architecture.binding);
  assert.equal(reloadedHost.calls.filter(m=>m.op==='conversation').length,0);
  assert.equal(open.get(architecture.flow_id).stop.budget.used.calls,2);
  assert.deepEqual(open.get(second.flow_id),second);

  action='Close task';
  await reloadedHost.commands['karma.openTasks']();
  assert(traffic.some(m=>m.event==='task_close'&&m.flow_id===architecture.flow_id));
  assert.equal(order.at(-1),second.flow_id);
  assert.equal(reloadedHost.controller.status().openTaskCount,1);
  assert.equal(reloadedHost.controller.status().pending.flow_id,second.flow_id);
  assert.deepEqual(open.get(second.flow_id),second);
  checks.push('open tasks keep distinct receipts across reload, focus without replay, resume on a new tier, and restore parked focus on close');
 }
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
 {
  const pendingKey='sutra.pendingOrchestration';
  const requestNotice='Sutra can review the résumé if you paste its text into this chat.';
  const document=[{role:1,content:[new Text('Can you look at the resume now please?')]}];
  const pending={flow_id:'saved-review',state:'PAUSED',cursor:'4',workspace:'/project',
   stop:{status:'task_budget',resume_phase:'SYNTHESIS'},binding:{session:'task-1',contract_hash:'contract-1'}};
  for(const retained of [undefined,pending]){
   const blocked=setup({[pendingKey]:retained}),events=[];
   blocked.v.window.showWarningMessage=async()=>{throw Error('Document guidance must not ask for workspace access');};
   blocked.deps.request=async m=>{
    assert.equal(m.op,'orchestrate');
    events.push(m.event);
    if(m.event==='pending')return {state:'DONE',flow_id:null};
    assert.equal(m.event,'start');
    assert.equal(m.prompt,document[0].content[0].value);
    return {state:'DONE',flow_id:retained?'document-request':null,request_notice:requestNotice};
   };
   const reply=await blocked.send('auto',document);
   assert.deepEqual(reply.map(part=>part.value),[requestNotice]);
   assert.deepEqual(events,retained?['start']:['pending','start']);
   const {armed,routing,pending:saved}=blocked.controller.status();
   assert.equal(armed,false);
   assert.deepEqual(saved,retained||null);
   assert.equal(routing.active,false);
   assert.equal(routing.stage,'guidance');
   assert.equal(routing.message,requestNotice);
   assert.equal(routing.invocationId,undefined);
   assert.equal(routing.token_usage,undefined);
  }
  const referred=setup({[pendingKey]:pending}),invocation='a'.repeat(32),eventsForReference=[];
  referred.v.window.showWarningMessage=async()=>{throw Error('Document routing must not ask for workspace access');};
  referred.deps.request=async m=>{
   eventsForReference.push(m.event);
   assert.equal(m.event,'start');
   return {state:'DONE',flow_id:'document-reference',request_notice:requestNotice,conversation:{
    route:{model:'model-a'},memory_used:true,result:{invocation_id:invocation,admission:{admitted:true},
     accepted_output:{intent:'document',answer:'DRAFT ROUTING MESSAGE'},
     token_usage:{calls:[{invocation_id:invocation,model:'model-a',stage:'conversation',input_tokens:100,output_tokens:20}]}}
   }};
  };
  const reply=await referred.send('auto',[{role:1,content:[new Text('Can you look at it now?')]}]);
  assert.equal(reply[0].value,requestNotice);
  assert(!reply.some(part=>part.value?.includes('DRAFT ROUTING MESSAGE')));
  assert.deepEqual(eventsForReference,['start']);
  assert.equal(referred.controller.status().routing.invocationId,invocation);
  assert.deepEqual(referred.controller.status().pending,pending);
  const classifying={...pending,state:'CLASSIFYING',prompt:document[0].content[0].value,stop:undefined};
  const recovering=setup({[pendingKey]:classifying}),events=[];
  recovering.v.window.showWarningMessage=async()=>{throw Error('Document recovery must not ask for workspace access');};
  recovering.deps.request=async m=>{
   assert.equal(m.op,'orchestrate');
   assert.equal(m.flow_id,classifying.flow_id);
   events.push(m.event);
   if(m.event==='inspect')return classifying;
   assert.equal(m.event,'step');
   return {state:'DONE',flow_id:classifying.flow_id,request_notice:requestNotice};
  };
  const resumedNotice=await recovering.send('auto',[{role:1,content:[new Text('continue')]}]);
  assert.deepEqual(events,['inspect','step']);
  assert.equal(resumedNotice.at(-1).value,requestNotice);
  assert.equal(recovering.controller.status().pending,null);
  assert.equal(recovering.controller.status().routing.stage,'guidance');
  checks.push('document input guidance ends a fresh or resumed request without a model call or permission prompt and preserves unrelated saved work');
 }
 {
  const persisted={},traffic=[],flowId='e'.repeat(32),invocation='a'.repeat(32);
  const goal='Can you review the résumé for validation roles?';
  const body='Résumé text:\nReduced hardware validation time by 17%.';
  const stop={status:'evidence_bridge',resume_phase:'CLASSIFYING',error:'Paste the résumé excerpt.',
   evidence_bridge:{target_kind:'document',target:'résumé',required:'text',decision:'BRIDGE'},
   budget:{used:{calls:1,tokens:1842}}};
  let flow,key;
  const attach=fixture=>{
   fixture.v.window.showWarningMessage=async()=>{throw Error('Document evidence must not ask for workspace access');};
   fixture.deps.request=async m=>{
    assert.equal(m.op,'orchestrate');
    assert(!m.armed);
    traffic.push(structuredClone(m));
    const reply=(value,extra={})=>{
     const focus=flow?.state!=='DONE'&&key===m.chat_key?flow:null;
     return structuredClone({...value,...extra,task_stack:{focused:focus?.flow_id||null,open_tasks:focus?[focus]:[]},focused_flow:focus});
    };
    if(m.event==='pending')return reply({state:'DONE',flow_id:null});
    if(m.event==='start'){
     if(m.prompt!==goal){
      assert.notEqual(m.chat_key,key,'Pasted text in another chat must remain independent');
      return reply({state:'DONE',flow_id:'d'.repeat(32)},{request_notice:'Independent request'});
     }
     key=m.chat_key;
     flow={flow_id:flowId,cursor:'1',state:'PAUSED',prompt:goal,stop};
     return reply(flow);
    }
    assert.equal(m.chat_key,key);
    assert.equal(m.flow_id,flowId);
    if(m.event==='inspect')return reply(flow);
    assert.equal(m.cursor,flow.cursor);
    assert.equal(m.tier,'high');
    if(m.event==='supply_evidence'){
     assert.equal(m.evidence_text,body);
     assert.equal(flow.prompt,goal);
     assert.deepEqual(flow.stop.budget.used,{calls:1,tokens:1842});
     flow={...flow,cursor:'2',state:'CLASSIFYING',stop:undefined};
     return reply(flow);
    }
    assert.equal(m.event,'step');
    flow={...flow,cursor:'3',state:'DONE'};
    return reply(flow,{conversation:{route:{model:'model-c'},result:{
     invocation_id:invocation,admission:{admitted:true},accepted_output:{intent:'conversation',answer:'The quantified validation result supports the résumé.'},
     token_usage:{calls:[{invocation_id:invocation,model:'model-c',stage:'conversation',input_tokens:100,output_tokens:20}]}
    }}});
   };
  };
  const user=value=>({role:1,content:[new Text(value)]});
  const first=setup(persisted);attach(first);
  const pause=await first.send('low',[user(goal)],{});
  assert(pause.some(part=>part.value?.includes('Paste the résumé excerpt.')));
  assert(pause.some(part=>part.value?.includes('[Saved evidence request]')));
  assert(!pause.some(part=>part.value?.includes('send **continue**')));
  const history=[user(goal),{role:2,content:pause}];
  const reloaded=setup(persisted);attach(reloaded);
  const beforeContinue=traffic.length;
  await reloaded.send('high',[...history,user('continue')],{});
  assert.deepEqual(traffic.slice(beforeContinue).map(m=>m.event),['inspect']);
  await reloaded.send('high',[user(body)],{});
  assert.equal(reloaded.controller.status().pending.flow_id,flowId);
  const beforeSupply=traffic.length;
  const result=await reloaded.send('high',[...history,user(body)],{});
  assert.deepEqual(traffic.slice(beforeSupply).map(m=>m.event),['inspect','supply_evidence','step']);
  assert(result.some(part=>part.value?.includes('quantified validation result')));
  assert(result.some(part=>part.value?.includes('Memory restored')));
  assert.equal(reloaded.controller.status().pending,null);
  checks.push('evidence bridge guides continue, preserves the originating flow across reload, and resumes pasted text on the selected model without workspace permission');
 }
 {
  const fixture=setup(),traffic=[],file='people/Candidate Resume (1).docx';
  const goal='Can you review this resume please?\nCandidate Resume (1).docx\nSuggest changes to reach more employers.';
  const user=value=>[{role:1,content:[new Text(value)]}];
  const chat={modelOptions:{_conversationId:'local-docx-review'}};
  let approve=false,dialogs=0,flow;
  fixture.v.window.showWarningMessage=async(message,options,...buttons)=>{
   dialogs++;
   assert.match(message,/Read this document/);
   assert.match(options.detail,/people\/Candidate Resume \(1\)\.docx/);
   assert.deepEqual(buttons,['Read document']);
   return approve?'Read document':undefined;
  };
  fixture.deps.request=async request=>{
   traffic.push(request.event);
   if(request.event==='pending')return {state:'DONE',flow_id:null};
   if(request.event==='start'){
    assert.equal(request.prompt,goal);
    flow={state:'PAUSED',cursor:'1',flow_id:'a'.repeat(32),prompt:goal,
     stop:{status:'evidence_bridge',resume_phase:'CLASSIFYING',
      evidence_bridge:{target_kind:'document',required:'document_file'}}};
    return flow;
   }
   assert.equal(request.flow_id,flow.flow_id);
   if(request.event==='inspect')return flow;
   assert.equal(request.cursor,flow.cursor);
   if(request.event==='locate_document'){
    flow={...flow,cursor:String(Number(flow.cursor)+1),stop:{...flow.stop,document_lookup:{status:'found',paths:[file]}}};
    return flow;
   }
   assert(approve);
   if(request.event==='approve_document'){
    assert.equal(request.document_path,file);
    assert.equal(request.confirmed,true);
    assert.equal(request.armed,false);
    assert.equal(request.tier,'high');
    assert.deepEqual(request.models,{low:'model-a',mid:'model-b',high:'model-c'},
     'The approved document segment captures models before automatic classification can continue');
    flow={...flow,state:'CLASSIFYING',cursor:'5',stop:undefined};
    return flow;
   }
   assert.equal(request.event,'step');
   assert.equal(request.tier,'high');
   return {...flow,state:'DONE',cursor:'6',conversation:{route:{model:'model-c'},result:{
    invocation_id:'b'.repeat(32),admission:{admitted:true},accepted_output:{intent:'conversation',answer:'Lead with the quantified validation work.'}
   }}};
  };
  const pause=await fixture.send('low',user(goal),chat);
  assert.deepEqual(traffic,['pending','start','locate_document']);
  assert(pause.some(part=>part.value?.includes('try the file lookup again')));
  approve=true;
  const answer=await fixture.send('high',user('continue'),chat);
  assert.deepEqual(traffic.slice(3),['inspect','locate_document','approve_document','step']);
  assert.equal(dialogs,2);
  assert(answer.some(part=>part.value?.includes('quantified validation')));
  assert.equal(fixture.controller.status().pending,null);
  checks.push('a named workspace DOCX requests only file-specific read consent, can resume after declining, and uses the newly selected tier');
 }
 {
  const fixture=setup(),traffic=[],file='remnant.docx',flowId='4'.repeat(32);
  const chat={modelOptions:{_conversationId:'classified-docx-review'}};
  const reply=(id,intent,answer,{basis,stage='conversation',model='model-a'}={})=>({route:{model},result:{
   invocation_id:id,admission:{admitted:true},accepted_output:{intent,answer,basis},
   token_usage:{calls:[{invocation_id:id,stage,model,input_tokens:11,output_tokens:12}]}
  }});
  let dialogs=0;
  fixture.v.window.showWarningMessage=async(_message,options,...buttons)=>{
   dialogs++;
   assert(options.detail.startsWith(file+'\n'));
   assert.deepEqual(buttons,['Read document']);
   return 'Read document';
  };
  let steps=0;
  const stop={status:'evidence_bridge',resume_phase:'CLASSIFYING',
   evidence_bridge:{target_kind:'document',required:'document_file',target:'/project/remnant.docx'}};
  fixture.deps.request=async request=>{
   traffic.push(request.event);
   if(request.event==='pending')return {state:'DONE',flow_id:null};
   if(request.event==='start')return {state:'CLASSIFYING',flow_id:flowId,cursor:'1'};
   assert.equal(request.flow_id,flowId);
   assert.equal(request.armed,false);
   if(request.event==='locate_document'){
    assert.equal(request.cursor,'2');
    return {state:'PAUSED',flow_id:flowId,cursor:'3',stop:{...stop,document_lookup:{status:'found',paths:[file]}}};
   }
   if(request.event==='approve_document'){
    assert.equal(request.cursor,'3');
    assert.equal(request.document_path,file);
    assert.equal(request.confirmed,true);
    return {state:'CLASSIFYING',flow_id:flowId,cursor:'4'};
   }
   assert.equal(request.event,'step');
   if(steps++===0)return {state:'PAUSED',flow_id:flowId,cursor:'2',stop,
    conversation:reply('5'.repeat(32),'document','Unpublished router draft')};
   if(steps===2){
    assert.equal(request.cursor,'4');
    return {state:'CLASSIFYING',flow_id:flowId,cursor:'5',progress:'Checking the draft against the document evidence.',
     routing_step:reply('6'.repeat(32),'conversation','Unpublished synthesis draft',{
      basis:'Unpublished basis',stage:'document_synthesis'})};
   }
   if(steps===3){
    assert.equal(request.cursor,'5');
    return {state:'CLASSIFYING',flow_id:flowId,cursor:'6',progress:'Document verdict review completed.',
     routing_step:reply('7'.repeat(32),'conversation','Unpublished reviewer output',{
      stage:'document_verdict_reviewer',model:'model-b'})};
   }
   assert.equal(request.cursor,'6');
   const approved=reply('8'.repeat(32),'conversation','```json\n{"verdict":"HOLD"}\n```',{
    basis:'The signed test took 16 minutes against a 15-minute limit [D4-E1].',
    stage:'document_synthesis',model:'model-b'});
   approved.document_verification={verdict:'supported',evidence_ids:['D4-E1']};
   return {state:'DONE',flow_id:flowId,cursor:'7',conversation:approved};
  };
  const output=await fixture.send('low',[{role:1,content:[new Text('Thoughts? /project/remnant.docx')]}],chat);
  assert.deepEqual(traffic,['pending','start','step','locate_document','approve_document','step','step','step']);
  assert.equal(dialogs,1);
  const visible=output.map(part=>part.value).join('');
  assert(visible.includes('```json\n{"verdict":"HOLD"}\n```\n\n**Basis for the verdict**'));
  assert(visible.includes('16 minutes against a 15-minute limit \\[D4-E1\\].'));
  assert(visible.includes('Checking the draft against the document evidence.'));
  assert(visible.includes('Document verdict review completed.'));
  assert(!visible.includes('Unpublished'));
  assert(visible.includes('4 calls'));
  checks.push('document review prompts for one scoped read, shows review progress and a final basis beside the answer, and counts withheld drafts without displaying them');
 }
 {
  const workspace=setup(),traffic=[],invocation='b'.repeat(32),flowId='c'.repeat(32);
  const chat={modelOptions:{_conversationId:'workspace-evidence'}};
  const user=value=>[{role:1,content:[new Text(value)]}];
  let scopeConfirmed=false,permissionPrompts=0,flow;
  workspace.v.window.showWarningMessage=async(_message,_options,...choices)=>{
   permissionPrompts++;
   assert(scopeConfirmed,'Workspace authorization can be requested after the target is selected');
   return choices.find(choice=>choice==='Read only');
  };
  const backend=workspace.deps.request;
  workspace.deps.request=async(m,t)=>{
   if(m.op!=='orchestrate')return backend(m,t);
   traffic.push(structuredClone(m));
   if(m.event==='pending')return {state:'DONE',flow_id:null};
   if(m.event==='start'){
    flow={flow_id:flowId,cursor:'1',state:'PAUSED',prompt:m.prompt,conversation_id:invocation,
     stop:{status:'evidence_bridge',resume_phase:'CLASSIFYING',error:'Reply with Use this workspace to choose this project.',
      evidence_bridge:{target_kind:'workspace',target:null,required:'scope',decision:'BRIDGE'}}};
    return {...flow,conversation:{route:{model:'model-a'},result:{
     invocation_id:invocation,admission:{admitted:true},accepted_output:{intent:'workspace',answer:'Unpublished routing proposal'}
    }}};
   }
   assert.equal(m.flow_id,flowId);
   if(m.event==='inspect')return flow;
   assert.equal(m.cursor,flow.cursor);
   if(m.event==='supply_evidence'){
    assert.equal(m.evidence_text,'Use this workspace.');
    assert(!m.armed);
    scopeConfirmed=true;
    flow={...flow,cursor:'2',state:'NEED_ARM',stop:undefined};
    return flow;
   }
   assert.equal(m.event,'step');
   assert(m.armed);
   return {...flow,cursor:'3',state:'DONE',data:{route:{model:'model-a'},result:{
    invocation_id:'review',admission:{admitted:true},accepted_output:{answer:'Workspace review completed'}
   }}};
  };
  const pause=await workspace.send('auto',user('Can you review the project?'),chat);
  assert(pause.some(part=>part.value?.includes('Use this workspace')));
  assert(pause.some(part=>part.value?.includes('/conversation/'+invocation)));
  assert(!pause.some(part=>part.value?.includes('Continuing this request through Sutra’s workspace workflow')));
  assert.equal(permissionPrompts,0);
  const before=traffic.length;
  const accessPause=await workspace.send('auto',user('Use this workspace.'),chat);
  assert.deepEqual(traffic.slice(before).map(m=>m.event),['inspect','supply_evidence']);
  assert(accessPause.some(part=>part.value?.includes('Workspace access needed')));
  assert.equal(permissionPrompts,0,'Selecting the scope must finish the response before consent');
  assert(!workspace.controller.status().armed);
  assert.equal(workspace.controller.status().pending.flow_id,flowId);
  await workspace.commands['karma.armNative']();assert.equal(permissionPrompts,1);
  const beforeContinue=traffic.length;
  const reviewed=await workspace.send('high',user('continue'),chat);
  assert.deepEqual(traffic.slice(beforeContinue).map(m=>m.event),['inspect','step']);
  assert.equal(traffic.at(-1).tier,'high');
  assert(reviewed.some(part=>part.value?.includes('Workspace review completed')));
  checks.push('a missing workspace target requests explicit scope, defers consent outside the response, and retains the workflow across model-switch continuation');
 }
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
 {
  const review=setup();review.kind('report');await review.arm();
  const configured={low:'model-a',mid:'model-b',high:'strata:high'};
  let highOnline=true,saved,resumed;
  review.deps.configureModels=async(force,_token,{tier,onFallback}={})=>{
   if(force)return configured;
   const models=[configured.low,configured.mid].map(name=>({name,eligible:true}));
   if(highOnline)models.push({name:configured.high,eligible:true,tier:'high'});
   const result=forRequest(configured,{models},tier);
   if(result.fallbacks.length)onFallback(result.fallbacks);
   return result.models;
  };
  const backend=review.deps.backendRequest;
  review.deps.backendRequest=async(m,t)=>{
   const result=await backend(m,t);
   if(m.op==='native_work')result.route={tier:'high',model:m.models.high};
   return result;
  };
  const request=review.deps.request;
  review.deps.request=async(m,t)=>{
   if(m.op==='orchestrate'&&m.flow_id===saved?.flow_id){
    if(m.event==='inspect')return saved;
    if(m.event==='resume'){
     resumed=m;
     return {...saved,state:'SYNTHESIS',stop:undefined};
    }
   }
   const result=await request(m,t);
   if(m.op==='orchestrate'&&result.state==='CODING'){
    saved={...result,state:'PAUSED',stop:{status:'child_slice_timeout',resume_phase:'CHILDREN',
      slice_ordinal:2,error:'Child checkpoint saved',budget:{used:{calls:2}}}};
    return saved;
   }
   return result;
  };
  const first=await review.send('auto');
  assert.equal(review.flowRequests.find(m=>m.event==='start').models.high,'strata:high');
  assert.equal(review.controller.status().pending.stop.budget.used.calls,2);
  highOnline=false;
  const followup=[{role:2,content:first},...chatUser('continue')];
  const blocked=await review.send('high',followup);
  assert(blocked.some(part=>part.value?.includes('Strata High is unavailable. Start your configured local Strata server')));
  assert.equal(review.controller.status().pending.flow_id,saved.flow_id);
  assert.equal(review.controller.status().routing.stage,'blocked');
  assert.equal(resumed,undefined);
  const completed=await review.send('auto',followup);
  assert.equal(completed.filter(part=>part.value?.includes('High (strata:high) is unavailable')).length,1);
  assert.equal(review.controller.status().routing.model,'model-b');
  assert.equal(review.controller.status().routing.stage,'admitted');
  assert(!completed.some(part=>part.value?.includes('Sutra routing confirmed')),'Routing metadata belongs in Monitor');
  assert.equal(resumed.flow_id,saved.flow_id);
  assert.equal(resumed.cursor,saved.cursor);
  assert.deepEqual(resumed.binding,saved.binding);
  assert.equal(resumed.models.high,'model-b');
  assert.equal(review.calls.filter(m=>m.op==='conversation').length,1);
  assert.equal(review.calls.find(m=>m.op==='native_work').prompt,'Fix retry');
  assert.equal(configured.high,'strata:high');
  checks.push('High going offline leaves a saved request intact on fixed High and lets Auto resume the original cursor and binding on Mid');
 }
 {
  const sliced=setup();sliced.kind('report');await sliced.arm();let saved,resumed;
  const original=sliced.deps.request;
  const childUsage=(id,input,output,status)=>({result:{token_usage:{calls:[
    {invocation_id:id,stage:'subagent',model:'model-a',input_tokens:input,output_tokens:output,status}
  ]}}});
  sliced.deps.request=async(m,t)=>{
   if(m.op==='orchestrate'&&saved&&m.flow_id===saved.flow_id){
    if(m.event==='inspect')return saved;
    if(m.event==='resume'){
     resumed=m;
     saved={...saved,state:'CHILDREN',cursor:'5',stop:undefined};
     return saved;
    }
    if(m.event==='step'&&!resumed){
     saved={...saved,state:'PAUSED',cursor:'4',progress:undefined,
       stop:{status:'child_slice_timeout',resume_phase:'CHILDREN',slice_ordinal:2,
         error:'The child review reached its time limit before returning a checkpoint.'}};
     return {...saved,child:childUsage('child-timeout',30,40,'quarantined')};
    }
   }
   const out=await original(m,t);
   if(m.op==='orchestrate'&&out.state==='CODING'){
    saved={...out,state:'CHILDREN',cursor:'3',progress:'Child review saved slice 1. Continuing the partition.'};
    return {...saved,child:childUsage('child-checkpoint',10,20,'accepted')};
   }
   return out;
  };
  const response=await sliced.send('low');
  const message=response.map(p=>p.value).join('');
  assert(sliced.controller.status().routing.progress.includes('Child review saved slice 1'));
  for(const phrase of ['reached its time limit','last completed child checkpoint',
     'different Sutra model tier','40 in · 60 out · 2 calls'])assert(message.includes(phrase),phrase);
  assert(!sliced.calls.some(c=>c.op==='native_work'));
  assert.equal(sliced.controller.status().pending.stop.slice_ordinal,2);
  assert.equal(sliced.controller.status().pending.child,undefined);
  await sliced.send('high',[{role:1,content:[new Text('continue')]}]);
  assert.equal(resumed.cursor,'4');assert.equal(resumed.tier,'high');
  assert.deepEqual(resumed.binding,saved.binding);
  assert.equal(sliced.calls.filter(c=>c.op==='conversation').length,1);
  const completed=sliced.calls.find(c=>c.op==='native_work');
  assert.equal(completed.prompt,'Fix retry');assert.equal(completed.contract_hash,'contract-1');
  checks.push('child checkpoints show progress, pause with charged usage, and resume the same bound task on another tier');
 }
 {
  const review=setup();review.kind('report');await review.arm();let resumed,publications=0;
  const original=review.deps.request;
  review.deps.publishReview=()=>{publications++;};
  review.deps.request=async(m,t)=>{
   if(m.op==='orchestrate'&&m.event==='resume'){
    resumed=m;
    return {...review.controller.status().pending,state:'SYNTHESIS',stop:undefined};
   }
   const out=await original(m,t);
   if(!resumed&&m.op==='orchestrate'&&out.state==='CODING'){
    return {...out,state:'PAUSED',stop:{status:'claim_review_output_limit',resume_phase:'SYNTHESIS',
     review_page:2,total_pages:3,error:'The claim reviewer reached its output allowance.'},
     claim_review_usage:{calls:[
      {invocation_id:'saved-draft',stage:'chat_reviewer',model:'model-c',input_tokens:123,output_tokens:456,status:'accepted'},
      {invocation_id:'failed-page',stage:'report_reviewer',model:'model-a',input_tokens:234,output_tokens:8192,status:'quarantined'}
     ]}};
   }
   return out;
  };
  const paused=await review.send();
  const message=paused.map(p=>p.value).join('');
  for(const label of ['reached its output allowance','draft and completed claim-review pages','Resume this request','**continue**','request thinking off','page 2 of 3','consumed budget','357 in · 8,648 out · 2 calls'])assert(message.includes(label));
  assert.equal(review.controller.status().routing.stage,'paused');
  const pending=review.controller.status().pending;
  assert.equal(pending.stop.status,'claim_review_output_limit');
  assert.equal(pending.claim_review_usage,undefined);
  assert.equal(publications,0);
  const [,resumeLink]=message.match(/\[Resume this request\]\((vscode:\/\/[^\s)]+)\)/);
  const uri=new URL(resumeLink);
  const click=()=>review.uriHandler.handleUri({authority:uri.host,path:uri.pathname,query:uri.search,fragment:uri.hash});
  review.v.window.showInformationMessage=async(message,...choices)=>{review.uiCalls.push(['notice',message]);return choices[0];};
  await click();
  assert(!review.uiCalls.some(c=>c[0]==='workbench.action.chat.open'),'Inline Resume must not open a foreign chat');
  assert.equal(review.copied.length,1);
  const instruction=review.copied[0];assert.match(instruction,/^Resume saved Sutra request \[[a-f0-9]{32}\]$/);
  assert(!resumed);assert.equal(publications,0);
  const history={role:2,content:paused};
  await review.send('high',[history,...chatUser(instruction)]);
  assert.equal(resumed.flow_id,pending.flow_id);
  assert.equal(resumed.cursor,pending.cursor);
  assert.deepEqual(resumed.binding,pending.binding);
  assert.equal(review.calls.filter(c=>c.op==='conversation').length,1);
  assert.equal(publications,1);
  assert.equal(resumed.tier,'high');
  await click();
  assert.equal(review.copied.length,1);
  assert(!review.uiCalls.some(c=>c[0]==='workbench.action.chat.open'));
  checks.push('inline Resume offers an explicit copied instruction, restores the exact claim-review cursor on another tier, and cannot replay a completed link');
 }
 for(const approve of [false,true]){
  const provenance=setup();provenance.kind('report');await provenance.arm();
  const original=provenance.deps.request;let selected,confirmation;
  const prompt='Review the latest upstream architecture.';
  provenance.deps.request=async(m,t)=>{
   if(m.op==='orchestrate'&&m.event==='step'&&!selected){
    return {flow_id:m.flow_id,cursor:m.cursor,state:'PAUSED',
            stop:{status:'provenance_confirmation',resume_phase:'BINDING',error:'Live upstream freshness was not checked.'}};
   }
   if(m.op==='orchestrate'&&m.event==='accept_provenance'){
    selected=m;
    return original({...m,event:'step'},t);
   }
   return original(m,t);
  };
  provenance.v.window.showWarningMessage=async(title,options,label)=>{
   confirmation={title,options,label};return approve?label:undefined;
  };
  const reply=await provenance.send('high',[{role:1,content:[new Text(prompt)]}]);
  assert.equal(confirmation.label,'Review open snapshot');
  assert(confirmation.options.modal);
  assert.equal(confirmation.options.detail,'Live upstream freshness was not checked.');
  if(approve){
   assert(selected.confirmed&&selected.armed);
   assert.equal(provenance.calls.find(c=>c.op==='native_work').prompt,prompt);
  }else{
   assert(!selected);
   assert(!provenance.calls.some(c=>c.op==='native_work'||c.op==='auto_new'));
   assert(reply.some(p=>p.value?.includes('Review paused before source mapping')));
  }
 }
 checks.push('an explicit upstream review waits for an in-chat snapshot choice before source work');
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
 for(const typed of [false,true]){
  const sourcePage=setup();sourcePage.kind('report');await sourcePage.arm();
  const request=sourcePage.deps.request;
  sourcePage.deps.request=async(m,t)=>{
   const result=await request(m,t);
   if(m.op!=='orchestrate'||result.state!=='CODING')return result;
   return {...result,state:'PAUSED',stop:{status:'memory_fault',resume_phase:'CHILDREN',
    error:'The review packet cannot fit the current allocation: 15,672 per call; 15,672 task/parent budget units remain.',
    memory_fault:{code:'capacity',recovery:'repartition_or_select_larger_eligible_context'},
    child_context:{branch_id:'saved-child',available_tokens:15672,effective_context_tokens:65536,
     cause:'No complete nonblank source line fits the child excerpt allocation',
     ...(typed?{failure_basis:'source_page'}:{})}}};
  };
  const reply=await sourcePage.send();const text=reply.map(part=>part.value||'').join('\n');
  assert(text.includes('Host source paging check'));
  assert(text.includes('No complete nonblank source line fits'));
  assert(!text.includes('Select a Sutra model with a larger context'));
  assert(!text.includes('Increase allocation'));
  assert(!text.includes('15,672 per call'));
  assert(!text.includes('Memory recovery: repartition'));
  assert(text.includes('Accepted source checkpoints'));
  assert.equal(sourcePage.controller.status().pending.stop.child_context.branch_id,'saved-child');
  assert(!sourcePage.calls.some(call=>call.op==='native_work'));
 }
 checks.push('child source-page misses use their observed paging cause and retained checkpoints, without invented financial or larger-model recovery');
 {
  const childCapacity=setup();childCapacity.kind('report');await childCapacity.arm();
  const request=childCapacity.deps.request;
  childCapacity.deps.request=async(m,t)=>{
   const result=await request(m,t);
   return m.op==='orchestrate'&&result.state==='CODING'?{...result,state:'PAUSED',stop:{
    status:'memory_fault',resume_phase:'CHILDREN',error:'The child packet did not fit.',
    memory_fault:{code:'capacity',recovery:'repartition_or_select_larger_eligible_context'},
    child_context:{failure_basis:'reservation_budget',available_tokens:15672,effective_context_tokens:65536}}}:result;
  };
  const reply=(await childCapacity.send()).map(part=>part.value||'').join('\n');
  assert(reply.includes('Check the saved fit measurements'));
  assert(!reply.includes('Select a Sutra model with a larger context'));
  assert(!reply.includes('Memory recovery: repartition'));
  checks.push('an unmeasured child fit failure does not claim a larger model is the remedy');
 }
 for(const physical of [false,true]){
  const measuredChild=setup();measuredChild.kind('report');await measuredChild.arm();
  const request=measuredChild.deps.request;
  measuredChild.deps.request=async(m,t)=>{
   const result=await request(m,t);
   return m.op==='orchestrate'&&result.state==='CODING'?{...result,state:'PAUSED',stop:{
    status:'context_budget',resume_phase:'CHILDREN',error:'Measured child admission bound.',
    memory_fault:{code:'capacity',recovery:'repartition_or_select_larger_eligible_context'},
    child_context:physical?{failure_basis:'physical_context',physical_context:{required_input_tokens:69000,context_limit_tokens:65536}}:
     {failure_basis:'reservation_budget',cause:'No complete nonblank source line fits the child excerpt allocation',available_tokens:15672,effective_context_tokens:65536,
      task_allocation_need:{dimension:'tokens',minimum:1000,required_context:16672,model_context:65536,slots:1}}}}:result;
  };
  const reply=(await measuredChild.send()).map(part=>part.value||'').join('\n');
  assert(reply.includes(physical?'Select a Sutra model with a larger context':'Increase allocation'));
  assert(!reply.includes(physical?'Increase allocation':'Select a Sutra model with a larger context'));
 }
 checks.push('measured child financial shortfalls and physical-window limits retain distinct recovery choices');
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
  assert(detail.includes('250,000 + 228,975 = 478,975'));assert(detail.includes('spending are retained'));
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

 {
  const automatic=setup();automatic.kind('report');await automatic.arm();
  const request=automatic.deps.request;
  let savedStop,resumed=false;
  automatic.deps.request=async(m,t)=>{
   if(savedStop&&m.op==='orchestrate'&&m.event==='inspect')return savedStop;
   if(savedStop&&m.op==='orchestrate'&&m.event==='resume'){
    assert.equal(m.tier,'high');
    assert.deepEqual(m.binding,savedStop.binding);
    resumed=true;
    return {...savedStop,state:'CODING',stop:null,allocation_version:1,allocation_amendment:{version:1},
      progress:'Sutra added source capacity within the workspace limits.'};
   }
   const result=await request(m,t);
   if(!resumed&&m.op==='orchestrate'&&result.state==='CODING'){
    savedStop={...result,state:'PAUSED',stop:{status:'task_budget',stage:'SYNTHESIS',error:'Source allocation exhausted.',
      automatic_allocation:{status:'denied',reason:'workspace_limit'}}};
    return savedStop;
   }
   return result;
  };
  assert((await automatic.send('low')).some(p=>p.value?.includes('automatic allocation ceiling')));
  assert.equal(automatic.controller.status().pending.stop.automatic_allocation.reason,'workspace_limit');
  assert(automatic.controller.status().pending.stop.automatic_allocation.message.includes('automatic allocation ceiling'));
  automatic.v.window.showWarningMessage=async()=>{throw Error('Automatic allocation must not ask for manual confirmation');};
  const reply=await automatic.send('high',[{role:1,content:[new Text('continue')]}]);
  assert.equal(automatic.controller.status().routing.progress,'Sutra added source capacity within the workspace limits.');
  assert(!reply.some(p=>p.value?.includes('Sutra added source capacity within the workspace limits')));
  assert.equal(automatic.calls.filter(c=>c.op==='native_work').at(-1).tier,'high');
  assert.equal(automatic.controller.status().pending,null);
  checks.push('automatic allocation progress on resume stays in the same model-switch request without a confirmation');
 }
 {
  const limited=setup();limited.kind('report');await limited.arm();
  const request=limited.deps.request;
  limited.deps.request=async(m,t)=>{
   const result=await request(m,t);
   return m.op==='orchestrate'&&result.state==='CODING'?{...result,state:'PAUSED',stop:{
    status:'context_budget',error:'Required context cannot fit.',automatic_allocation:{
     status:'model_context',reason:'The selected model has reached its context limit.'}}}:result;
  };
  const reply=await limited.send();
  assert(reply.some(p=>p.value?.includes('Select a Sutra tier with a larger context window')));
  assert(!reply.some(p=>p.value?.includes('Increase allocation')));
  checks.push('a model context limit suggests switching models without offering an allocation increase');
 }

 const pressured=setup();pressured.kind('report');await pressured.arm();const pressuredRequest=pressured.deps.request;
 pressured.deps.request=async(m,t)=>{const r=await pressuredRequest(m,t);return m.op==='orchestrate'&&r.state==='CODING'?{...r,state:'PAUSED',stop:{status:'resource_wait',resume_phase:'CODING',error:'RESOURCE_WAIT: OS thermal pressure: serious; kernel: BRIDGE'}}:r;};
 const pressureReply=await pressured.send();
 assert(pressureReply.some(p=>p.value?.includes('Last model allocation')));assert(pressureReply.some(p=>p.value?.includes('checks fresh measurements')));
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
  assert(fallback.controller.status().routing.progress.includes('Memory admission blocked large. Auto will try small'));
  assert(!reply.some(p=>p.value?.includes('Host workflow')),'Host progress must not clutter the answer');
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
  await routed.arm();
  const reply=await routed.send('low',[{role:1,content:[new Text('Give a high-level workspace overview')]}]);
  assert.equal(reply.filter(p=>p instanceof Thinking).length,0);
  assert.equal(reply.filter(p=>p instanceof Text&&p.value.includes('/Reasoning.txt')).length,0);
  assert(!reply.some(p=>p.value?.includes('No separate reasoning text received.')));
  assert(!reply.some(p=>p.value?.includes('Host workflow')));
  assert.equal(routed.controller.status().routing.progress,'Fresh source mapping: structural_pass_complete');
  assert(reply.some(p=>p.value?.includes('Host admission check · Workspace synthesis paused')));
  assert(!routed.calls.some(c=>c.op==='native_work'));
  checks.push('router without reasoning remains quiet inline; Monitor retains host progress and a workspace pause explains the blocking stage');
 }
 for(const firstThinking of [true,false]){
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
  if(firstThinking)await cooling.commands['karma.toggleThinkingForNextRequest']();
  let coolReply=await cooling.send('low',[{role:1,content:[new Text('Hello')]}]);
  assert(coolReply.some(p=>p.value?.includes('Sutra paused the request')));assert(!cooling.controller.status().armed);assert.equal(cooling.calls.length,0);
  await cooling.send('high',[{role:1,content:[new Text('continue')]}]);assert.equal(cooling.calls.length,0);
  if(!firstThinking)await cooling.commands['karma.toggleThinkingForNextRequest']();
  blockedResource=false;coolReply=await cooling.send('high',[{role:1,content:[new Text('continue')]}]);
  assert(coolReply.some(p=>p.value?.includes('Conversational answer')));assert.equal(cooling.calls[0].prompt,'Hello');assert.equal(cooling.calls[0].tier,'high');assert.equal(cooling.calls[0].think_for_turn,firstThinking?true:undefined);assert(!cooling.controller.status().armed);
 }
 checks.push('classification resource pause resumes the original conversational prompt on another model without arming files or replaying while blocked');
 {
  const saved={},waiting=setup(saved),greeting='Hello Karma - how are you?',failedId='7'.repeat(32);
  let providerFlow;const requests=[];
  const wire=instance=>async(m,t)=>{
   if(m.op!=='orchestrate')return instance.deps.backendRequest(m,t);
   requests.push(structuredClone(m));
   if(m.event==='pending')return providerFlow||{state:'DONE',flow_id:null};
   if(m.event==='start'){
    providerFlow={flow_id:'provider-wait-flow',cursor:'1',state:'PAUSED',prompt:m.prompt,children:[],
     stop:{status:'provider_wait',resume_phase:'CLASSIFYING',automatic_retry:false,
      failed_invocation_id:failedId,error:'The local provider did not return response headers before the timeout. It may still have been loading or waiting behind another client; generation was not observed.'}};
    return {...providerFlow,claim_review_usage:{calls:[{invocation_id:failedId,stage:'conversation',model:'model-a',
     input_tokens:null,output_tokens:null,status:'quarantined'}]}};
   }
   if(m.event==='resume'){
    assert.equal(m.tier,'high');assert.equal(m.armed,false);
    providerFlow={...providerFlow,state:'CLASSIFYING',cursor:'2',stop:null};
   }
   if(m.event==='step'&&providerFlow.state==='CLASSIFYING'){
    const conversation=await instance.deps.backendRequest({...m,op:'conversation',prompt:providerFlow.prompt},t);
    providerFlow={...providerFlow,state:'DONE',cursor:'3',conversation};
   }
   return structuredClone(providerFlow);
  };
  waiting.deps.request=wire(waiting);
  waiting.v.window.showWarningMessage=async()=>{throw Error('Provider wait cannot arm workspace access');};
  const first=await waiting.send('auto',[{role:1,content:[new Text(greeting)]}]);
  const text=first.map(part=>part.value||'').join('');
  assert(text.includes('Local provider wait · Conversation routing paused.'));
  assert(text.includes('other work')&&text.includes('failed call remains charged'));
  assert(text.includes('1 call'));
  assert(!text.includes('Sutra withheld')&&!text.includes('Last model allocation')&&!text.includes('Refresh the engine worker'));
  assert.deepEqual(requests.map(row=>row.event),['pending','start']);
  assert.equal(waiting.controller.status().routing.stage,'paused');
  assert.equal(waiting.controller.status().routing.token_usage.calls[0].invocation_id,failedId);
  assert(!waiting.controller.status().armed);
  const reloaded=setup(saved);reloaded.deps.request=wire(reloaded);
  reloaded.v.window.showWarningMessage=async()=>{throw Error('Resume cannot arm workspace access');};
  const next=await reloaded.send('high',[{role:1,content:[new Text('continue')]}]);
  assert(next.some(part=>part.value?.includes('Conversational answer')));
  assert.equal(reloaded.calls.filter(row=>row.op==='conversation').length,1);
  assert.equal(reloaded.calls.find(row=>row.op==='conversation').prompt,greeting);
  assert.equal(reloaded.calls.find(row=>row.op==='conversation').tier,'high');
  assert(!reloaded.calls.some(row=>row.op==='native_work'));
  assert.equal(reloaded.controller.status().pending,null);
  checks.push('provider timeout is a saved classifier pause with charged usage; reload and explicit model-switch continuation preserve the original greeting without arming or automatic retry');
 }
 for(const approve of [false,true]){
  const upgraded=setup();upgraded.kind('report');
  const originalPrompt='Review saved workspace architecture';
  const requested=await upgraded.deps.request({op:'orchestrate',event:'start',prompt:originalPrompt,tier:'low'});
  const original=await upgraded.deps.request({op:'orchestrate',event:'step',flow_id:requested.flow_id,cursor:requested.cursor,armed:true,tier:'low'});
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
  upgraded.v.window.showWarningMessage=async(message,options,...labels)=>{
   const label=labels.at(-1);
   if(label==='Restart review')return approve?label:undefined;
   if(label==='Workspace agent')arms++;
   return label;
  };
  const result=await upgraded.send('high',[{role:1,content:[new Text('continue')]}]);
  if(approve){
   assert(!restart,'Restart cannot mutate the saved task before workspace access is granted');
   assert.equal(arms,0);assert(!upgraded.calls.some(c=>c.op==='native_work'));
   assert(result.some(p=>p.value?.includes('Workspace access needed')));
   await upgraded.commands['karma.armNative']();assert.equal(arms,1);assert(!restart);
   await upgraded.send('high',[{role:2,content:result},{role:1,content:[new Text('continue')]}]);
   assert(restart?.armed);assert.equal(restart.binding.contract_hash,'contract-1');
   const work=upgraded.calls.find(c=>c.op==='native_work');assert.equal(work.prompt,originalPrompt);assert.equal(work.tier,'high');assert.equal(arms,1);
  }else{
   assert(!restart);assert.equal(arms,0);assert(!upgraded.calls.some(c=>c.op==='native_work'));
   assert(result.some(p=>p.value?.includes('Saved request retained')));
  }
  assert(!upgraded.uiCalls.some(c=>c[0]==='workbench.action.chat.newLocalChat'));
 }
 checks.push('policy upgrade recovery is explicit before arming; decline preserves saved work and approval retains the original request in the same chat on another model');
 for(const approve of [false,true]){
  const terminal=setup(),traffic=[],options={modelOptions:{_conversationId:'terminal-review-origin'}};
  terminal.kind('report');
  const chatKey=require('node:crypto').createHash('sha256').update(options.modelOptions._conversationId).digest('hex');
  const recovery={kind:'policy_changed',terminal_review:true,restart_allowed:true,stack_cursor:'older-stack',
   parked_flow_id:'older-auto-restored',message:'Unpublished review from an older policy. Spending is retained; the older open task will remain saved.'};
  const older={flow_id:'older-auto-restored',state:'PAUSED',cursor:'older-pause',prompt:'An older request',
   restarted_from:'older-original',binding:{session:'older-task',contract_hash:'older-contract',calibration_hash:'cal-1'}};
  const original={flow_id:'failed-paid-review',state:'DONE',cursor:'closed',prompt:'Review the original workspace architecture',
   conversation_id:'a'.repeat(32),binding:{session:'task-1',contract_hash:'contract-1',calibration_hash:'cal-1'},recovery,
   task_stack:{focused:older.flow_id,open_tasks:[older],cursor:'older-stack'},focused_flow:older};
  const frozen=structuredClone(original),backend=terminal.deps.request;
  terminal.deps.request=async(m,t)=>{
   if(m.op!=='orchestrate')return backend(m,t);
   traffic.push(structuredClone(m));
   if(m.event==='pending')return {...older,task_stack:original.task_stack,focused_flow:older};
   if(m.event==='recoverable_review')return m.chat_key===chatKey?structuredClone(original):{state:'DONE',flow_id:null};
   if(m.event==='inspect'&&m.flow_id===original.flow_id)return structuredClone(original);
   if(m.event==='restart_review'){
    assert.equal(m.confirmed,true);assert.equal(m.armed,true);assert.equal(m.chat_key,chatKey);
    assert.equal(m.flow_id,original.flow_id);assert.equal(m.binding.session,'task-1');
    assert.equal(m.stack_cursor,'older-stack');
    return backend({...m,event:'start_existing',flow_id:undefined,prompt:original.prompt,conversation_id:original.conversation_id},t);
   }
   return backend(m,t);
  };
  let dialogs=0;
  terminal.v.window.showWarningMessage=async(message,detail,...labels)=>{
   if(labels.at(-1)==='Restart review'){dialogs++;return approve?'Restart review':undefined;}
   return labels.at(-1);
  };
  const result=await terminal.send('high',[{role:1,content:[new Text('continue')]}],options);
  assert(traffic.some(m=>m.event==='recoverable_review'));assert.equal(dialogs,1);
  assert.equal(traffic[0].event,'recoverable_review');
  assert(!traffic.some(m=>m.event==='restart_review'));assert(!terminal.calls.some(m=>m.op==='native_work'));
  if(approve){
   assert(result.some(p=>p.value?.includes('Workspace access needed')));
   assert.equal(terminal.controller.status().pending.flow_id,original.flow_id);
   await terminal.commands['karma.armNative']();
   await terminal.send('high',[{role:1,content:[new Text('continue')]}],options);
   assert.equal(traffic.filter(m=>m.event==='restart_review').length,1);
   assert.equal(terminal.calls.find(m=>m.op==='native_work').prompt,original.prompt);
   assert.equal(terminal.calls.find(m=>m.op==='native_work').tier,'high');
  }else assert(result.some(p=>p.value?.includes('Saved request retained')));
  assert.deepEqual(original,frozen);assert(!traffic.some(m=>m.event==='cancel'));
 }
 for(const scenario of ['new ask','published','other chat']){
  const terminal=setup(),traffic=[],options={modelOptions:{_conversationId:scenario==='other chat'?'foreign-chat':'same-chat'}};
  terminal.kind('report');await terminal.arm();
  const backend=terminal.deps.request;
  terminal.deps.request=async(m,t)=>{
   if(m.op==='orchestrate'){
    traffic.push(structuredClone(m));
    if(m.event==='pending'||m.event==='recoverable_review')return {state:'DONE',flow_id:null};
   }
   return backend(m,t);
  };
  terminal.v.window.showWarningMessage=async(message,detail,...labels)=>{
   if(labels.at(-1)==='Restart review')throw Error('Unrelated or published work cannot request a restart');
   return labels.at(-1);
  };
  const prompt=scenario==='new ask'?'Review a different workspace question.':'continue';
  await terminal.send('low',[{role:1,content:[new Text(prompt)]}],options);
  assert.equal(traffic.some(m=>m.event==='recoverable_review'),scenario!=='new ask');
  assert(!traffic.some(m=>m.event==='restart_review'));
  assert.equal(terminal.calls.find(m=>m.op==='native_work').prompt,prompt);
 }
 checks.push('explicit Continue prioritizes the latest unpublished original ask over an auto-restored older successor; confirmed restart binds the intended task and stack cursor, parks older work, preserves old DONE, and cannot resurrect published, foreign-chat or newer requests');
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
 assert.equal(first.filter(p=>p instanceof Text&&p.value.includes('/Reasoning.txt')).length,0);
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
 checks.push('three calls in one response keep independent inline thinking identities and end markers without answer-text diagnostics; model switches use distinct identities; reasoning never becomes memory or action proof');
 }
 {
  const live=setup(),backend=live.deps.backendRequest;
  let diagnostic;
  live.deps.backendRequest=async(...args)=>{
   const result=await backend(...args);
   if(result.result&&diagnostic)result.result.coherence_observation=diagnostic;
   return result;
  };
  const hello=await live.send('low',[{role:1,content:[new Text('Hello')]}]);
  assert(!hello.some(p=>p instanceof Text&&p.value.includes('Coherence indicators')));
  diagnostic={format:1,profile:'stage-text-coherence-v1',mode:'observe_only',status:'partial',
   invocation_id:'a'.repeat(32),binding:{session:'s',task:'t',chat:'c'},
   issues:[{code:'possible_contradiction',kind:'semantic_indicator',detail:'<img src=x> [run](command:evil)'}]};
  const first=await live.send('low',[{role:1,content:[new Text('Brainstorm alternatives')]}]);
  const messages=first.filter(p=>p instanceof Text).map(p=>p.value);
  assert(messages.some(text=>text.includes('Conversational answer')));
  assert.equal(messages.filter(text=>text.includes('Coherence indicators')).length,1);
  assert(messages.some(text=>text.includes('Possible conflicting statements (unverified)')));
  assert(!messages.some(text=>text.includes('command:evil')||text.includes('<img')));
  assert(!first.some(p=>p instanceof Call));
  assert.equal(live.calls.filter(request=>request.op==='conversation').length,2,'One model call for each ordinary request');
  assert(!live.calls.some(request=>['native_work','native_permissions','native_execute'].includes(request.op)));
  assert(!live.flowRequests.some(request=>['grant','allocation_grant','capability_grant','approve_document'].includes(request.event)),'Coherence never creates a permission or allocation grant');
  diagnostic={format:99,profile:'stage-text-coherence-v1',mode:'enforce',issues:Array(1000).fill('bad')};
  const next=await live.send('low',[{role:1,content:[new Text('Brainstorm again')]}]);
  assert(next.some(p=>p instanceof Text&&p.value.includes('Conversational answer')));
  assert(!next.some(p=>p instanceof Text&&p.value.includes('Coherence indicators')));
  checks.push('native chat displays one bounded inert coherence notice, leaves greetings and malformed diagnostics quiet, and adds no actions, permissions or retries');
 }
 {
  const saved={},host=setup(saved),original=host.deps.request;await host.arm();
  const options={modelOptions:{_conversationId:'durable-task-final-report'},tools:[]};
  let submissions=0,allocations=0,acks=0;
  host.deps.allocateRequest=async()=>({id:(++allocations).toString(16).padStart(32,'0')});
  host.deps.resumeRequest=async()=>{throw Error('Initial task must submit once');};
  host.deps.ackRequest=async()=>{acks++;};
  host.deps.request=async(m,t,activity,transport)=>{
   if(!transport?.id)return original(m,t);
   assert.equal(transport.wholeTask,true);submissions++;
   let flow=await original(m,t);const conversation=structuredClone(flow.conversation);
   while(!['DONE','AWAITING_ACTION'].includes(flow.state)){
    const event={kind:'workflow',version:1,flow_id:flow.flow_id,cursor:flow.cursor,state:flow.state,
     disposition:'continue',progress:'Reading the approved project source.'};
    activity(event);activity(event);
    flow=await original({...m,event:'step',flow_id:flow.flow_id,cursor:flow.cursor,binding:flow.binding||flow.bound_task},t);
   }
   flow.data.report_markdown='## Architecture\n\nThe Entry component passes work to the worker.';
   flow.data.result.accepted_output={answer:'Architecture summary',summary:'Architecture summary',report:{overview:'Architecture summary'}};
   const usage=(invocation_id,stage)=>({invocation_id,stage,model:'model-a',status:'accepted',input_tokens:100,output_tokens:20});
   const rows=[usage(conversation.result.invocation_id,'conversation'),usage(flow.data.result.invocation_id,'chat_reviewer')];
   conversation.result.token_usage={calls:[rows[0]]};flow.data.result.token_usage={calls:[rows[1]]};
   return {...flow,conversation,runner_usage:{calls:rows}};
  };
  const parts=await host.send('auto',undefined,options),body=parts.map(part=>part.value||'').join('');
  assert(body.includes('## Architecture'));assert(!body.includes('Conversational answer'));
  assert.equal(submissions,1);assert.equal(allocations,1);assert.equal(acks,1);
  assert(host.flowRequests.every(request=>request.tier==='auto'),'The accepted task keeps its selected routing mode');
  assert.equal((body.match(/Reading the approved project source\./g)||[]).length,2,
   'A replayed workflow event is displayed once per distinct cursor, without opening model thinking');
  assert(body.includes('200 in · 40 out · 2 calls'),'Aggregated runner usage deduplicates conversation and final receipts');
  assert.equal(host.controller.status().routing.token_usage.calls.length,2);
  const key=require('node:crypto').createHash('sha256').update(options.modelOptions._conversationId).digest('hex');
  assert.equal(saved['sutra.coordinatorRequests.v1'][key],undefined);
  const anchor=body.match(/conversation\/([a-f0-9]{32})\/Response\.md/)?.[1];
  assert(anchor,'The final report includes its authenticated conversation-memory anchor');
  assert.equal(saved['karma.nativeMemory.v1'].chats[key],anchor);
  assert.equal(saved['karma.nativeMemory.v1'].taskStacks[anchor],key);
  checks.push('one durable task submission publishes its final report instead of classifier prose, streams deduplicated workflow progress and retains exact usage and conversation memory');
 }
 {
  for(const boundary of ['stop','dispose','extension-disconnect']){
   const saved={},host=setup(saved),original=host.deps.request,options={modelOptions:{_conversationId:'durable-task-'+boundary},tools:[]};
   const id='b'.repeat(32);let started,stops=0,submissions=0;
   const entered=new Promise(resolve=>started=resolve);
   host.deps.allocateRequest=async()=>({id});host.deps.resumeRequest=async()=>{throw Error('Initial submission expected');};
   host.deps.ackRequest=async()=>{throw Error('An unobserved task outcome cannot be acknowledged');};
   host.deps.cancelRequest=async ticket=>{assert.equal(ticket.id,id);assert.equal(ticket.wholeTask,true);stops++;};
   host.deps.request=async(m,t,_activity,transport)=>{
    if(!transport?.id)return original(m,t);
    assert.equal(transport.wholeTask,true);submissions++;
    return new Promise((_resolve,reject)=>{
     t.onCancellationRequested(()=>reject(Object.assign(Error('UI subscription ended'),{code:'COORDINATOR_DETACHED'})));
     started();
    });
   };
   const request=host.send('low',undefined,options);await entered;
   host.token.cancel();
   if(boundary==='dispose')host.controller.dispose();
   if(boundary==='extension-disconnect')host.deps.isDisconnecting=()=>true;
   await assert.rejects(request,/subscription ended/);
   assert.equal(stops,boundary==='stop'?1:0,'Only a live user Stop cancels its task runner');
   assert.equal(submissions,1);assert(!host.controller.status().working);
   const key=require('node:crypto').createHash('sha256').update(options.modelOptions._conversationId).digest('hex');
   assert.equal(saved['sutra.coordinatorRequests.v1'][key].id,id);
   assert.equal(saved['sutra.coordinatorRequests.v1'][key].wholeTask,true);
  }
  checks.push('live chat Stop cancels only its exact durable task; reload/disposal detaches without stopping the runner and retains its immutable ticket');
 }
 {
  const saved={},host=setup(saved),original=host.deps.request,options={modelOptions:{_conversationId:'monitor-stopped-task'},tools:[]};
  let acknowledgements=0,submissions=0;
  host.deps.allocateRequest=async()=>({id:'c'.repeat(32)});
  host.deps.resumeRequest=async()=>{throw Error('Initial submission expected');};
  host.deps.ackRequest=async()=>{acknowledgements++;};
  host.deps.request=async(m,t,activity,transport)=>{
   if(!transport?.id)return original(m,t);
   submissions++;await original(m,t);
   throw Object.assign(Error('The runner acknowledged cancellation.'),{code:'COORDINATOR_CANCELLED'});
  };
  const stopped=await host.send('low',undefined,options);
  assert(stopped.some(part=>part.value?.includes('Sutra stopped this task')));
  assert.equal(host.controller.status().routing.stage,'cancelled');assert(!host.controller.status().working);
  assert.equal(acknowledgements,1);assert.equal(submissions,1);assert.equal(host.calls.length,1);
  assert.equal(host.calls[0].op,'conversation');assert.equal(Object.keys(saved['sutra.coordinatorRequests.v1']).length,0);
  checks.push('an acknowledged Monitor runner stop is a cancelled task notice rather than a failed VS Code request, without replaying paid work');
 }
 {
  const saved={},host=setup(saved),original=host.deps.request;
  await host.arm();host.kind('report');
  const options={modelOptions:{_conversationId:'runner-footer-detach'},tools:[]};
  const key=require('node:crypto').createHash('sha256').update(options.modelOptions._conversationId).digest('hex');
  let allocations=0,submissions=0,acknowledgements=0,reconnections=0,cancellations=0,response;
  host.deps.allocateRequest=async()=>({id:(++allocations).toString(16).padStart(32,'0')});
  host.deps.resumeRequest=async()=>{throw Error('Initial request must submit once');};
  host.deps.ackRequest=async()=>{acknowledgements++;};
  host.deps.cancelRequest=async()=>{cancellations++;};
  host.deps.request=async(m,t,activity,transport)=>{
   if(transport?.id){submissions++;t.onCancellationRequested(()=>cancellations++);}
   const next=await original(m,t);
   if(transport?.id&&next.data){
    response=next;
    throw Object.assign(Error('Response stream detached; runner retained'),{code:'COORDINATOR_DETACHED'});
   }
   return next;
  };
  const progress={report:part=>{
   if(part.value?.includes('Open tokens, system and kernel status'))throw Error('Response stream closed during footer');
  }};
  await assert.rejects(host.provider.provideLanguageModelChatResponse({id:'karma-low'},
   [{role:1,content:[new Text('Review workspace')]}],options,progress,host.token.token),/stream closed during footer/);
  const retained=structuredClone(saved['sutra.coordinatorRequests.v1'][key]);
  assert(retained?.id);assert(!host.controller.status().working);assert(!host.controller.status().routing.active);
  assert.equal(cancellations,0,'Closing a response stream cannot cancel runner execution');
  const before={allocations,submissions,acknowledgements,modelCalls:host.calls.length};
  host.deps.resumeRequest=async ticket=>{reconnections++;assert.equal(ticket.id,retained.id);return structuredClone(response);};
  host.deps.allocateRequest=async()=>{throw Error('Reconnection cannot allocate another execution');};
  const restored=await host.send('high',[{role:1,content:[new Text('continue')]}],options);
  assert(restored.some(part=>part.value?.includes('Reconnecting to the runner')));
  assert(restored.some(part=>part.value?.includes('proposal')));
  assert.equal(reconnections,1);assert.equal(allocations,before.allocations);assert.equal(submissions,before.submissions);
  assert.equal(host.calls.length,before.modelCalls);assert.equal(acknowledgements,before.acknowledgements+1);
  assert.equal(saved['sutra.coordinatorRequests.v1'][key],undefined);assert.equal(cancellations,0);
  assert.equal(host.controller.status().lastRoute,'model-a','A UI tier change cannot relabel the already executed model');
  assert(!host.controller.status().working);
  checks.push('a discarded footer releases local busy ownership while same-chat retry on another tier reconnects the identical runner result without replay, cancellation or new spending');
 }
 {
  const saved={},first=setup(saved),original=first.deps.request;
  const options={modelOptions:{_conversationId:'runner-owned-origin'}};
  const key=require('node:crypto').createHash('sha256').update(options.modelOptions._conversationId).digest('hex');
  const id='d'.repeat(32),tickets='sutra.coordinatorRequests.v1';
  let entered,rejectView,response,modelSubmissions=0,cancelRequests=0,tokenCancellations=0;
  const started=new Promise(resolve=>entered=resolve);
  first.deps.allocateRequest=async chatKey=>{assert.equal(chatKey,key);return {id};};
  first.deps.resumeRequest=async()=>{throw Error('The first client must submit once');};
  first.deps.cancelRequest=async()=>{cancelRequests++;};
  first.deps.request=async(m,t,activity,transport)=>{
   if(!transport)return original(m,t);
   assert.deepEqual(transport,{id,chatKey:key,wholeTask:true});
   assert.equal(saved[tickets][key].id,id,'Persist the ticket before submission, including initial start');
   modelSubmissions++;
   t.onCancellationRequested(()=>tokenCancellations++);
   response=await original(m,t);
   entered();
   return new Promise((_resolve,reject)=>{rejectView=reject;});
  };
  const disconnected=first.send('low',[{role:1,content:[new Text('hello')]}],options);
  await started;
  assert.equal(saved['sutra.pendingOrchestration'],undefined,'The initial flow response never reached the UI');
  first.controller.dispose();
  assert.equal(tokenCancellations,0,'UI disposal must not cancel the runner');
  assert.equal(cancelRequests,0);
  rejectView(Object.assign(new Error('UI detached; runner retained'),{code:'COORDINATOR_DETACHED'}));
  await assert.rejects(disconnected,/UI detached/);
  assert.equal(saved[tickets][key].prompt,'hello');

  const foreign=setup(saved);let foreignReconnections=0;
  foreign.deps.resumeRequest=async()=>{foreignReconnections++;throw Error('Foreign chat cannot attach');};
  foreign.deps.allocateRequest=async()=>{throw Error('No saved request for this other chat');};
  await assert.rejects(foreign.send('high',[{role:1,content:[new Text('continue')]}],
    {modelOptions:{_conversationId:'foreign-runner-chat'}}),/No saved request/);
  assert.equal(foreignReconnections,0);assert.equal(saved[tickets][key].id,id);

  const reloaded=setup(saved);let reconnects=0,acknowledgements=0,inspections=0;
  const afterReloadRequest=reloaded.deps.request;
  reloaded.deps.request=async(m,t,a)=>{
   if(m.op==='orchestrate'&&m.event==='inspect'){
    inspections++;assert.equal(m.chat_key,key);assert.equal(m.flow_id,response.flow_id);
    return {...structuredClone(response),conversation:undefined};
   }
   return afterReloadRequest(m,t,a);
  };
  reloaded.deps.allocateRequest=async()=>{throw Error('Reconnection must not allocate or submit another request');};
  reloaded.deps.resumeRequest=async(ticket,_token,_activity)=>{
   reconnects++;assert.equal(ticket.id,id);assert.equal(ticket.chatKey,key);assert.equal(ticket.workspace,'/project');
   return structuredClone(response);
  };
  reloaded.deps.ackRequest=async ticket=>{
   acknowledgements++;assert.equal(ticket.id,id);
   assert.equal(saved[tickets][key].id,id,'Keep the ticket until the runner acknowledges durable receipt');
  };
  const restored=await reloaded.send('high',[{role:1,content:[new Text('continue')]}],options);
  assert.equal(reconnects,1);assert.equal(acknowledgements,1);assert.equal(modelSubmissions,1);
  assert.equal(saved[tickets][key],undefined);
  assert(restored.some(part=>part.value?.includes('Reconnecting to the runner')));
  assert(restored.some(part=>part.value?.includes('Conversational answer')));
  assert.equal(inspections,1,'Current host policy must inspect a cached workflow before its result is published');
  assert.equal(reloaded.flowRequests.length,0,'Initial result recovery cannot rerun start or model inference');
  assert.equal(reloaded.calls.length,0);
  assert.equal(reloaded.controller.status().pending,null);
  checks.push('runner request identity persists before initial start; UI disposal detaches, same-chat continue on another tier restores its result once, and another chat cannot attach or replay it');
 }
 {
  const saved={},host=setup(saved),options={modelOptions:{_conversationId:'runner-policy-change'}};
  const key=require('node:crypto').createHash('sha256').update(options.modelOptions._conversationId).digest('hex');
  saved['sutra.coordinatorRequests.v1']={[key]:{id:'c'.repeat(32),workspace:'/project',nativeChat:key,chatKey:key,stackChat:key,event:'step',prompt:'Review workspace'}};
  const old={flow_id:'old-flow',cursor:'old-cursor',state:'MAPPING',prompt:'Review workspace',
    conversation:{result:{accepted_output:{answer:'STALE CACHED ANSWER'}}}};
  let acknowledgements=0;
  host.deps.resumeRequest=async()=>structuredClone(old);
  host.deps.ackRequest=async()=>{acknowledgements++;};
  host.deps.allocateRequest=async()=>{throw Error('Declined recovery cannot submit work');};
  host.deps.request=async m=>{
   assert.equal(m.event,'inspect');assert.equal(m.flow_id,old.flow_id);assert.equal(m.chat_key,key);
   return {...old,conversation:undefined,recovery:{kind:'policy_changed',restart_allowed:true,
    message:'Current policy requires a fresh review.'}};
  };
  host.v.window.showWarningMessage=async()=>undefined;
  const parts=await host.send('high',[{role:1,content:[new Text('continue')]}],options);
  assert.equal(acknowledgements,1);assert.equal(saved['sutra.coordinatorRequests.v1'][key],undefined);
  assert(!parts.some(value=>value.value?.includes('STALE CACHED ANSWER')));
  assert(parts.some(value=>value.value?.includes('Saved request retained')));
  assert.equal(host.calls.length,0);
  checks.push('cached runner output receives current host inspection and follows explicit policy recovery before source answers can be published');
 }
 {
  const saved={},host=setup(saved),options={modelOptions:{_conversationId:'runner-revocation'}};
  const key=require('node:crypto').createHash('sha256').update(options.modelOptions._conversationId).digest('hex');
  const ticket={id:'e'.repeat(32),workspace:'/project',nativeChat:key,chatKey:key,stackChat:key,event:'start',prompt:'hello'};
  saved['sutra.coordinatorRequests.v1']={[key]:ticket};
  let cancellations=0;
  host.deps.cancelRequest=async value=>{assert.deepEqual(value,ticket);cancellations++;};
  await host.commands['karma.disarmNative']();
  assert.equal(cancellations,1,'Explicit workspace revocation cancels its retained runner request');
  assert.equal(saved['sutra.coordinatorRequests.v1'][key].id,ticket.id,'Cancellation does not erase the retained request identity or its ledger');
  checks.push('explicit workspace revocation requests runner cancellation while retaining request identity and consumed-budget provenance');
 }
 {
  for(const action of ['revoke','disable','enable','dispose']){
   const host=setup();let cancellations=0;
   host.deps.cancelWorkspaceRequests=async()=>{cancellations++;return {cancelled:1};};
   assert.equal(host.calls.length,0);assert.equal(cancellations,0,'Ordinary activation must not stop existing runner work');
   if(action==='revoke')await host.commands['karma.disarmNative']();
   if(action==='disable'){host.cfg.nativeEnabled=false;host.changeConfiguration('karma.nativeEnabled');}
   if(action==='enable'){host.cfg.nativeEnabled=true;host.changeConfiguration('karma.nativeEnabled');}
   if(action==='dispose')host.controller.dispose();
   assert.equal(cancellations,['revoke','disable'].includes(action)?1:0,
    'Only explicit revocation or disabling cancels workspace jobs, including operations with no chat ticket');
  }
  checks.push('workspace-wide revocation covers non-ticket source and action jobs; activation, enabling and UI disposal leave runner work intact');
 }
 for(const busy of [false,true]){
  const saved={},host=setup(saved);
  host.deps.resumeRequest=async()=>{};host.deps.cancelWorkspaceRequests=async()=>{};
  host.deps.isBusy=()=>busy;
  if(!busy)saved['sutra.coordinatorRequests.v1']={retained:{id:'c'.repeat(32),workspace:'/project'}};
  host.v.window.showQuickPick=async items=>items.find(item=>item.id==='stop-runner');
  await host.commands['karma.nativeControls']();
  assert(host.uiCalls.some(value=>value[0]==='karma.stopRunner'));
  assert.equal(host.calls.length,0);
 }
 checks.push('bottom-bar controls expose the explicit Stop runner command while working or holding a retained request without dispatching model work');
 {
  const saved={},host=setup(saved),options={modelOptions:{_conversationId:'runner-retired'}};
  const key=require('node:crypto').createHash('sha256').update(options.modelOptions._conversationId).digest('hex');
  saved['sutra.coordinatorRequests.v1']={[key]:{id:'f'.repeat(32),workspace:'/project',nativeChat:key,chatKey:key,stackChat:key,event:'start',prompt:'hello'}};
  host.deps.resumeRequest=async()=>{throw Object.assign(new Error('Cached response retired'),{code:'COORDINATOR_RESULT_RETIRED'});};
  host.deps.allocateRequest=async()=>{throw Error('A retired result cannot cause another start');};
  const result=await host.send('high',[{role:1,content:[new Text('continue')]}],options);
  assert(!host.flowRequests.some(value=>value.event==='start'));
  assert.equal(host.calls.length,0);
  assert(result.some(value=>value.value?.includes('no active workflow was recovered')));
  assert.equal(saved['sutra.coordinatorRequests.v1'][key],undefined);
  checks.push('a retired acknowledged transport response falls back to durable checkpoints and cannot silently submit a replacement start');
 }
 {
  const saved={},host=setup(saved),options={modelOptions:{_conversationId:'runner-missing-record'}};
  const key=require('node:crypto').createHash('sha256').update(options.modelOptions._conversationId).digest('hex');
  saved['sutra.coordinatorRequests.v1']={[key]:{id:'f'.repeat(32),workspace:'/project',nativeChat:key,chatKey:key,stackChat:key,event:'start',prompt:'Review original workspace'}};
  let reconnects=0,acks=0;
  host.deps.resumeRequest=async()=>{reconnects++;throw Object.assign(new Error('Transport record unavailable'),{code:'COORDINATOR_REQUEST_MISSING'});};
  host.deps.ackRequest=async()=>{acks++;};
  host.deps.allocateRequest=async()=>{throw Error('A missing record cannot submit another request');};
  const prior=host.deps.request;
  const checkpoint={flow_id:'retained-existing-flow',cursor:'saved-cursor',state:'NEED_ARM',prompt:'Review original workspace',children:[]};
  host.deps.request=async(m,t,a)=>{
   if(m.op==='orchestrate'&&['pending','inspect'].includes(m.event))return structuredClone(checkpoint);
   return prior(m,t,a);
  };
  const first=await host.send('high',[{role:1,content:[new Text('continue')]}],options);
  assert(first.some(value=>value.value?.includes('transport record is unavailable')));
  assert.equal(saved['sutra.coordinatorRequests.v1'][key],undefined);
  assert.equal(acks,0,'An absent record cannot be acknowledged as completed');
  await host.send('high',[{role:1,content:[new Text('continue')]}],options);
  assert.equal(reconnects,1,'A second continue must use the durable checkpoint rather than repeat a missing transport lookup');
  assert.equal(host.controller.status().pending.flow_id,checkpoint.flow_id);
  assert(!host.flowRequests.some(value=>value.event==='start'));assert.equal(host.calls.length,0);
  checks.push('a typed missing transport record clears only the stale ticket without ACK, then repeated continue uses the durable workflow without resubmission');
 }
 for(const terminalAtAttach of [false,undefined]){
  const saved={},host=setup(saved),options={modelOptions:{_conversationId:'monitor-stop-during-attach-'+terminalAtAttach}};
  const key=require('node:crypto').createHash('sha256').update(options.modelOptions._conversationId).digest('hex');
  const ticket={id:'d'.repeat(32),workspace:'/project',nativeChat:key,chatKey:key,stackChat:key,event:'step',prompt:'Review workspace',wholeTask:true};
  saved['sutra.coordinatorRequests.v1']={[key]:ticket};
  let acknowledgements=0;
  host.deps.resumeRequest=async()=>{throw Object.assign(Error('The task was stopped while attached.'),
   {code:'COORDINATOR_CANCELLED',...(terminalAtAttach===undefined?{}:{terminalAtAttach})});};
  host.deps.ackRequest=async value=>{assert.equal(value.id,ticket.id);acknowledgements++;};
  host.deps.allocateRequest=async()=>{throw Error('A live Stop cannot authorize a replacement task');};
  host.deps.request=async()=>{throw Error('A live Stop cannot inspect and automatically resume the stopped workflow');};
  const result=await host.send('high',[{role:1,content:[new Text('continue')]}],options);
  assert.equal(acknowledgements,1);assert.equal(saved['sutra.coordinatorRequests.v1'][key],undefined);
  assert.equal(host.controller.status().routing.stage,'cancelled');assert(!host.controller.status().working);
  assert(result.some(part=>part.value?.includes('Sutra stopped this task')));assert.equal(host.calls.length,0);
 }
 checks.push('Monitor Stop during a retained-result subscription ends that response without inspecting, resuming or submitting a replacement task; missing timing evidence also stops safely');
 for(const code of ['COORDINATOR_INTERRUPTED','COORDINATOR_CANCELLED','COORDINATOR_FAILED']){
  const saved={},host=setup(saved),options={modelOptions:{_conversationId:'runner-terminal-'+code}};
  const key=require('node:crypto').createHash('sha256').update(options.modelOptions._conversationId).digest('hex');
  const id='a'.repeat(32);saved['sutra.coordinatorRequests.v1']={[key]:{id,workspace:'/project',nativeChat:key,chatKey:key,stackChat:key,event:'start',prompt:'hello'}};
  let acknowledgements=0;
  host.deps.resumeRequest=async()=>{throw Object.assign(new Error('Execution has stopped'),{code,...(code==='COORDINATOR_CANCELLED'?{terminalAtAttach:true}:{})});};
  host.deps.allocateRequest=async()=>{throw Error('Terminal recovery must not resubmit');};
  host.deps.ackRequest=async ticket=>{assert.equal(ticket.id,id);acknowledgements++;};
  const result=await host.send('high',[{role:1,content:[new Text('continue')]}],options);
  assert.equal(acknowledgements,1);assert.equal(saved['sutra.coordinatorRequests.v1'][key],undefined);
  assert(!host.flowRequests.some(value=>value.event==='start'));assert.equal(host.calls.length,0);
  assert(result.some(value=>value.value?.includes('runner confirmed this execution has stopped')));
 }
 checks.push('confirmed interrupted, cancelled and failed runner results clear only their transport ticket and inspect durable workflow state without replay or refund');
 {
  const saved={},host=setup(saved),options={modelOptions:{_conversationId:'runner-failed-submit'}};
  const key=require('node:crypto').createHash('sha256').update(options.modelOptions._conversationId).digest('hex');
  const request=host.deps.request;let acknowledged=0;
  host.deps.allocateRequest=async()=>({id:'b'.repeat(32)});
  host.deps.resumeRequest=async()=>{throw Error('No reattachment expected');};
  host.deps.ackRequest=async ticket=>{assert.equal(ticket.chatKey,key);acknowledged++;};
  host.deps.request=async(m,t,a,transport)=>{
   if(!transport)return request(m,t,a);
   throw Object.assign(new Error('Terminal bridge validation failure'),{code:'COORDINATOR_FAILED'});
  };
  await assert.rejects(host.send('low',[{role:1,content:[new Text('hello')]}],options),/Terminal bridge validation failure/);
  assert.equal(acknowledged,1);assert.equal(saved['sutra.coordinatorRequests.v1'][key],undefined);
  assert.equal(host.calls.length,0);
  checks.push('a confirmed terminal bridge failure acknowledges the transport result without leaving a permanent reconnect barrier');
 }
 {
  const saved={},host=setup(saved);await host.arm();let evidenceCalls=0;
  const grant=structuredClone(saved.access);
  const authority={generation:grant.id,scope:grant.scope,profile:grant.profile,workspace:'/project'};
  host.deps.authorization.evidence=expected=>{
   evidenceCalls++;assert.equal(expected.generation,saved.access.id);assert.equal(expected.scope,saved.access.scope);
   assert.equal(expected.profile,saved.access.profile);return {path:'/private/authorization.json',digest:'a'.repeat(64)};
  };
  assert(host.controller.permissionEvidence(authority));assert.equal(evidenceCalls,1);
  for(const mismatch of [{...authority,scope:'/another-workspace'},
    {...authority,profile:grant.profile==='read_only'?'workspace_agent':'read_only'},
    {...authority,workspace:'/another-workspace'}])assert.throws(()=>host.controller.permissionEvidence(mismatch),/permission changed/);
  assert.equal(evidenceCalls,1,'Scope/profile mismatch must fail before obtaining file evidence');
  saved.access={...grant,id:'replacement-grant',profile:grant.profile==='read_only'?'workspace_agent':'read_only'};
  assert.throws(()=>host.controller.permissionEvidence(authority),/permission changed/);
  assert.equal(evidenceCalls,1,'A stale caller authority must not obtain proof from its replacement grant');
  assert(host.controller.permissionEvidence());assert.equal(evidenceCalls,2,'A later caller may explicitly validate the new current grant');
  checks.push('permission evidence is bound to the caller generation, scope and profile rather than silently inheriting a replacement grant');
 }
 {
  const saved={},host=setup(saved);await host.arm();const originalGrant=saved.access.id;
  const request=host.deps.request,configure=host.deps.configureModels;
  host.deps.authorization.evidence=()=>({path:'/private/authorization.json',digest:'b'.repeat(64)});
  host.deps.configureModels=async(...args)=>{
   const result=await configure(...args);saved.access={...saved.access,id:'changed-during-model-selection'};return result;
  };
  host.deps.request=async(m,t,a,transport)=>{
   if(m.event==='start'){
    assert.equal(transport.authority.generation,originalGrant);
    host.controller.permissionEvidence(transport.authority);
   }
   return request(m,t,a);
  };
  await assert.rejects(host.send('low',[{role:1,content:[new Text('Review workspace')]}]),/permission changed/);
  assert.equal(host.calls.length,0,'Changing a grant while selecting the model cannot authorize inference or source work');
  checks.push('orchestration passes its captured authority through submission and detects a grant replacement during asynchronous model selection');
 }
 {
  const saved={},host=setup(saved);await host.arm();
  const proposal=(await host.send()).find(value=>value instanceof Call);assert(proposal);
  const oldGrant=saved.access.id,request=host.deps.request;
  host.deps.authorization.evidence=()=>({path:'/private/authorization.json',digest:'c'.repeat(64)});
  host.deps.request=async(m,t,a,transport)=>{
   if(m.op==='native_execute'){
    assert.equal(transport.authority.generation,oldGrant);
    saved.access={...saved.access,id:'replacement-after-confirmation',profile:'read_only'};
    host.controller.permissionEvidence(transport.authority);
   }
   return request(m,t,a);
  };
  await assert.rejects(host.tool.invoke({input:proposal.input,toolInvocationToken:{}},host.token.token),/permission changed/);
  assert(!host.calls.some(value=>value.op==='native_execute'),'A confirmed old action cannot execute under a replacement grant');
  checks.push('confirmed native actions carry the exact validated access generation and fail before execution when the grant is replaced or downgraded');
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
