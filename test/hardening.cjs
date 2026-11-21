// Exercise the real extension entry point using a VS Code host fixture.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os'),vm=require('node:vm');
const {EventEmitter}=require('node:events');
const root=path.resolve(__dirname,'..'),tmp=fs.mkdtempSync(path.join(os.tmpdir(),'karma-host-test-'));
const views=new Map(),statusItems=[],commands=new Map(),tools=new Map(),participants=new Map(),intervals=[],timers=[],prompts=[],requests=[],changes=[],panelMessages=[];
const tierModels={low:'fixture-small',mid:'fixture-mid',high:'fixture-large'};
const documentPrompt='Review this resume at "/tmp/Candidate Resume (1).docx"';
const documentNotice='Paste the document text for review.';
const runtimeUpdates=[],bridgeAllocations=[],bridgeThinking=[],bridgeChildThinking=[];
const nativeAssignments=[],engineAssignments=[],remoteAssignments=[];let userModels,userEngineApprovals,userRemoteRegistry;
const expandedContextAssignments=[];
const requestThinking=new WeakMap(),requestChildThinking=new WeakMap();
const runtimeSettings=new Map();
const inputValues=[],openedDocuments=[];
const repoAllocation={source_bytes:1024000000};
let userAllocation,provider,panel,holdDashboard=false,pendingDashboard,bridgeCount=0,dashboardSpawns=0,requestSpawns=0;
let nativeRequest,nativeStatus,architectureDispatch,fixtureCatalog,allowModelSave=false,warningChoice;
const nativeProvider={install(_vscode,_context,deps){
 nativeRequest=deps.request;
 nativeStatus=deps.onStatus;
 return {status:()=>({working:false}),permissionEvidence:()=>undefined,publish(){},dispose(){}};
}};
let state={contract:{contract_hash:'fixture-contract',contract:{constraints:{}}},calibration:{binding:{hash:'fixture-calibration'}},session:'fixture',model:'fixture-model',report:'architecture v1',api:{report:'API v1'}};
const disposable=()=>({dispose(){}}),uri=value=>({scheme:'file',fsPath:value,toString(){return value;}});
class Emitter{event=(cb)=>{this.cb=cb;return disposable();};fire(value){changes.push(value.toString());this.cb?.(value);}dispose(){}}
function spawn(exe,args,options){
 const streaming=args.includes('--dashboard-stream'),requestStreaming=args.includes('--request-stream');if(streaming)dashboardSpawns++;if(requestStreaming)requestSpawns++;
 const child=new EventEmitter();child.stdout=new EventEmitter();child.stderr=new EventEmitter();child.stdin=new EventEmitter();child.kill=signal=>{queueMicrotask(()=>child.emit('close',0,signal));return true;};
 child.stdin.end=child.stdin.write=raw=>{const envelope=streaming?null:JSON.parse(raw),req=streaming?{op:'dashboard'}:requestStreaming?envelope.request:envelope;requests.push(req);bridgeCount++;
  const environment=requestStreaming?envelope.environment:options.env;
  bridgeAllocations.push(environment.SUTRA_WORKSPACE_ALLOCATION);
  nativeAssignments.push({models:environment.SUTRA_STRATA_MODELS,only:environment.SUTRA_STRATA_ONLY});
  engineAssignments.push({models:environment.SUTRA_ENGINE_MODELS,only:environment.SUTRA_ENGINE_ONLY,python:environment.SUTRA_ENGINE_PYTHON,registry:environment.SUTRA_ENGINE_REGISTRY,approvals:environment.SUTRA_ENGINE_APPROVALS});
  remoteAssignments.push({models:environment.SUTRA_REMOTE_MODELS,only:environment.SUTRA_REMOTE_ONLY,registry:environment.SUTRA_REMOTE_REGISTRY});
  expandedContextAssignments.push(environment.SUTRA_EXPANDED_REVIEW_CONTEXT_MODELS);
  if(!streaming){bridgeThinking.push(environment.SUTRA_THINKING);bridgeChildThinking.push(environment.SUTRA_CHILD_THINKING);requestThinking.set(req,environment.SUTRA_THINKING);requestChildThinking.set(req,environment.SUTRA_CHILD_THINKING);}
  const finish=()=>{if(req.op==='auto_new')state={...state,contract:{contract_hash:'fixture-contract',contract:{constraints:{auto_review:true}}},calibration:{binding:{hash:'fixture-calibration'}},session:'new-review',auto:{pending_files:0,mapped_files:2,source_files:2}};
   const data=req.op==='orchestrate'?{flow_id:'fixture-flow',state:'DONE',data:{state,route:{tier:'low',model:tierModels.low},result:{invocation_id:'fixture',admission:{admitted:true},accepted_output:{answer:'Fixture answer',remaining:[]}},coverage:{mapped_files:2,source_files:2},source_files:['a.py']}}:req.op==='models'?fixtureCatalog||{models:Object.values(tierModels).map(name=>({name,eligible:true})),suggested_tiers:tierModels}:req.op==='chat_answer'?{state,route:{tier:'low',model:tierModels.low,reason:'focused request'},result:{invocation_id:'fixture-invocation',admission:{admitted:true},accepted_output:{answer:'Fixture answer',remaining:[]}},coverage:{mapped_files:2,source_files:2},source_files:['a.py']}:req.op==='calibration_list'?{binding:{hash:'fixture',profiles:[]},profiles:[]}:req.op==='dashboard'?{hardware:{available:false,temperatures:{cpu:{available:true,maximum_celsius:42.12},gpu:{available:true,maximum_celsius:40.82}}},runs:[],invocations:{},actions:[]}:state;
   let response=req.op==='architecture'&&architectureDispatch?architectureDispatch(req):data;
   if(req.op==='state'&&req.review_prompt===documentPrompt)response={request_notice:documentNotice};
   child.stdout.emit('data',Buffer.from(JSON.stringify({ok:true,data:response})+'\n'));if(requestStreaming)child.stderr.emit('data',Buffer.from('SUTRA_BRIDGE_REQUEST_END\n'));if(!streaming&&!requestStreaming)child.emit('close',0,null);};
  if(req.op==='dashboard'&&holdDashboard)pendingDashboard=finish;else queueMicrotask(finish);
 };return child;
}
// Keep the real worker envelope/activity ordering in this host boundary test,
// while replacing the detached socket service with a fixture-owned coordinator.
// Real process/socket reconnection is exercised by coordinator-client.cjs.
const coordinatorOwners=new Map(),coordinatorClients=[];
class CoordinatorFixture {
 constructor(options){
  this.detached=false;this.options=options;coordinatorClients.push(this);
  const key=JSON.stringify([options.workspace,options.storage,options.python]);
  if(!coordinatorOwners.has(key)){
   const {BridgeWorker}=require('../bridge_worker');
   const worker=new BridgeWorker({spawn,command:options.python,
    args:['-B',path.join(options.extensionPath,'harness','vscode_bridge.py'),'--workspace',options.workspace,'--storage',options.storage,'--request-stream'],
    options:{cwd:options.workspace,stdio:['pipe','pipe','pipe'],env:{...process.env}},activity:require('../model_activity').decoder});
   coordinatorOwners.set(key,{worker,busy:false,jobs:[],issued:0});
  }
  this.owner=coordinatorOwners.get(key);
 }
 async send(request,environment,{token,onActivity}={}){
  if(this.detached)throw Error('Fixture UI is detached');
  if(token?.isCancellationRequested)throw Error('Sutra request cancelled before submission.');
  if(this.owner.busy)throw Error('A coordinator-owned bridge request is already running.');
  this.owner.busy=true;
  try{return await this.owner.worker.send(request,environment,{onActivity});}
  finally{this.owner.busy=false;}
 }
 async snapshot(){return {instance_id:'f'.repeat(32),busy:this.owner.busy,jobs:this.owner.jobs};}
 async allocate(){return {id:(++this.owner.issued).toString(16).padStart(32,'0'),instance_id:'f'.repeat(32)};}
 async resume(){throw Error('Unexpected resume in the extension hardening fixture');}
 async ack(){return {acknowledged:true};}
 async cancelAll(){return {cancelled_queued:0,busy:this.owner.busy};}
 dispose(){this.detached=true;}
}
const vscode={
 EventEmitter:Emitter,Uri:{file:uri,parse:uri},StatusBarAlignment:{Right:1},ViewColumn:{One:1},
 env:{appRoot:'/must/not/load/internal/typescript'},
 MarkdownString:class{constructor(value){this.value=value;}},ConfigurationTarget:{Global:1},chat:{createChatParticipant(id,handler){participants.set(id,handler);return disposable();}},
 workspace:{onDidChangeConfiguration:()=>disposable(),isTrusted:true,workspaceFolders:[{uri:uri(tmp)}],textDocuments:[],getConfiguration(){return {get(k,d){return k==='models'?tierModels:k==='workspaceAllocation'?repoAllocation:runtimeSettings.get(k)??d;},inspect(k){return k==='workspaceAllocation'?{globalValue:userAllocation,workspaceValue:repoAllocation}:k==='models'?{globalValue:userModels,workspaceValue:{low:'strata:profile/unapproved'}}:k==='remoteRegistry'?{globalValue:userRemoteRegistry,workspaceValue:path.join(tmp,'repo-controlled-remote.json')}:k==='engine.approvals'?{globalValue:userEngineApprovals,workspaceValue:{'sutra:engine/unapproved':'f'.repeat(64)}}:undefined;},async update(k,v,target){if(k==='models'){if(!allowModelSave)throw Error('Existing model choices must not be overwritten by discovery');userModels=v;return;}if(k==='engine.approvals'){assert.equal(target,vscode.ConfigurationTarget.Global);userEngineApprovals=v;}runtimeSettings.set(k,v);runtimeUpdates.push([k,v]);}};},
  registerTextDocumentContentProvider(s,p){provider=p;return disposable();},async openTextDocument(u){return {uri:u};},fs:{writeFile:async()=>{}}},
 commands:{registerCommand(n,f){commands.set(n,f);return disposable();},async executeCommand(n,...a){return commands.get(n)?.(...a);}},
 window:{createOutputChannel(){return {appendLine(){},dispose(){}};},registerWebviewViewProvider(id,provider){views.set(id,provider);return disposable();},createStatusBarItem(){const item={show(){},dispose(){}};statusItems.push(item);return item;},
  async showWarningMessage(title,options,choice){prompts.push({title,...options,choice});return warningChoice;},async showInputBox(options){prompts.push(options);return inputValues.shift();},async showQuickPick(items,options){prompts.push(options);},async showOpenDialog(options){prompts.push(options);},async showTextDocument(document){openedDocuments.push(document.uri.toString());},
  createWebviewPanel(){panel={visible:true,reveal(){},onDidDispose(){return disposable();},webview:{postMessage(m){panelMessages.push(m);},asWebviewUri:u=>u,onDidReceiveMessage(cb){this.receive=cb;return disposable();}}};return panel;}},
 lm:{registerTool(n,t){tools.set(n,t);return disposable();}},LanguageModelToolResult:class{constructor(content){this.content=content;}},LanguageModelTextPart:class{constructor(value){this.value=value;}}
};
const moduleFixture={exports:{}};
const context={module:moduleFixture,exports:moduleFixture.exports,require:n=>n==='vscode'?vscode:n==='node:child_process'?{spawn}:n==='./coordinator_client'?{CoordinatorClient:CoordinatorFixture}:n==='./model_activity'?require('../model_activity'):n==='./media/token_usage'?require('../media/token_usage'):n==='./usage_view'?require('../usage_view'):n==='./diagram_view'?require('../diagram_view'):n==='./model_selection'?require('../model_selection'):n==='./strata_startup'?require('../strata_startup'):n==='./strata_management'?require('../strata_management'):n==='./engine_management'?{create:()=>({command:async()=>({ok:true,models:[],environment_ready:false,running:false})})}:n==='./strata_setup'?require('../strata_setup'):n==='./bridge_worker'?require('../bridge_worker'):n==='./capabilities'?require('../capabilities'):n==='./native_provider'?nativeProvider:n==='./workspace_authorization'?require('../workspace_authorization'):require(n),process,Buffer,console,queueMicrotask,structuredClone,
 setInterval:fn=>{intervals.push(fn);return fn;},clearInterval(){},setTimeout:(fn,ms)=>{timers.push({fn,ms});return fn;},clearTimeout(){}};
context.process={...process,env:{...process.env,SUTRA_WORKSPACE_ALLOCATION:'{"tokens":999999}',SUTRA_ENGINE_APPROVALS:JSON.stringify({'sutra:engine/inherited':'f'.repeat(64)}),SUTRA_REMOTE_REGISTRY:path.join(tmp,'inherited-remote.json'),SUTRA_REMOTE_MODELS:'["sutra:remote/inherited"]',SUTRA_EXPANDED_REVIEW_CONTEXT_MODELS:'["sutra:remote/inherited"]'}};
vm.runInNewContext(fs.readFileSync(path.join(root,'extension.js'),'utf8'),context,{filename:'extension.js'});
const subscriptions=[];
(async()=>{
 const api=moduleFixture.exports.activate({subscriptions,extensionPath:root,globalStorageUri:uri(tmp),extension:{packageJSON:{version:'0.18.0'}}});
 assert.deepEqual(Object.keys(api).sort(),['getRoutingStatus','getState','open','version']);assert(Object.isFrozen(api));const beforeRouting=requests.length;assert.equal(api.getRoutingStatus(),null);assert.equal(requests.length,beforeRouting);
 await api.getState();
 assert.deepEqual(JSON.parse(nativeAssignments.at(-1).models),[],'Repository model choices cannot authorize native activation');
 assert.deepEqual(JSON.parse(remoteAssignments.at(-1).models),[],'Workspace and inherited values cannot authorize remote model selection');
 assert.deepEqual(JSON.parse(expandedContextAssignments.at(-1)),[],'Inherited values cannot opt in to a wider context');
 assert.equal(remoteAssignments.at(-1).registry,path.join(os.homedir(),'Library','Application Support','Sutra','Remote','models.json'),'Workspace and inherited registry paths are ignored');
 userRemoteRegistry=path.join(tmp,'User Remote','models.json');
 userModels={low:'sutra:remote/rtx3090',mid:'sutra:remote/rtx3090',high:'sutra:remote/rtx3090'};
 await api.getState();
 assert.deepEqual(JSON.parse(remoteAssignments.at(-1).models),Object.values(userModels));
 assert.equal(remoteAssignments.at(-1).only,'1');
 assert.equal(remoteAssignments.at(-1).registry,userRemoteRegistry,'Only the User registry setting reaches the worker');
 userModels={low:'fixture-small',mid:'fixture-mid',high:'sutra:remote/rtx3090'};
 await api.getState();
 assert.deepEqual(JSON.parse(remoteAssignments.at(-1).models),['sutra:remote/rtx3090']);
 assert.equal(remoteAssignments.at(-1).only,null,'A model switch clears remote-only mode');
 const beforeInvalidRegistry=requests.length;
 userRemoteRegistry='relative/models.json';
 await assert.rejects(api.getState(),/absolute file path in User Settings/);
 assert.equal(requests.length,beforeInvalidRegistry,'An invalid registry must not dispatch');
 userRemoteRegistry=undefined;userModels=undefined;
 userModels={low:'strata:profile/approved-low',mid:'strata:profile/approved-mid',high:'strata:profile/approved-high'};
 await api.getState();
 assert.deepEqual(JSON.parse(nativeAssignments.at(-1).models),Object.values(userModels));
 assert.equal(nativeAssignments.at(-1).only,'1');
 userModels={low:'fixture-small',mid:'fixture-mid',high:'strata:profile/approved-high'};
 await api.getState();
 assert.deepEqual(JSON.parse(nativeAssignments.at(-1).models),['strata:profile/approved-high']);
 assert.equal(nativeAssignments.at(-1).only,null,'Switching back to mixed providers clears native-only mode');
 userModels={low:'sutra:engine/approved-low',mid:'sutra:engine/approved-mid',high:'sutra:engine/approved-high'};
 await api.getState();
 assert.deepEqual(JSON.parse(engineAssignments.at(-1).models),Object.values(userModels));
 assert.equal(engineAssignments.at(-1).only,'1');
 assert.equal(nativeAssignments.at(-1).only,null,'All-engine configuration clears legacy-only mode');
 userModels={low:'fixture-small',mid:'fixture-mid',high:'sutra:engine/approved-high'};
 await api.getState();
 assert.deepEqual(JSON.parse(engineAssignments.at(-1).models),['sutra:engine/approved-high']);
 assert.equal(engineAssignments.at(-1).only,null,'Mixed-provider choices clear engine-only mode');
 userModels=undefined;
 await api.getState();
 assert.deepEqual(JSON.parse(engineAssignments.at(-1).models),[],'No inherited or workspace profile can authorize engine loading');
 assert.deepEqual(JSON.parse(engineAssignments.at(-1).approvals),{},'Inherited and repository values cannot approve engine execution');
 userEngineApprovals={'sutra:engine/approved-low':'a'.repeat(64)};
 await api.getState();
 assert.deepEqual(JSON.parse(engineAssignments.at(-1).approvals),userEngineApprovals,'Only explicit User approvals reach the worker');
 userEngineApprovals=undefined;
 assert.deepEqual(JSON.parse(bridgeAllocations.at(-1)),{},'Repository and inherited environment cannot supply workspace ceilings');
 userAllocation={tokens:300000,source_bytes:10000000,byFolder:{[tmp]:{source_bytes:20000000,enabled:false},[tmp+'-other']:{calls:1000000}}};
 await api.getState();
 assert.deepEqual(JSON.parse(bridgeAllocations.at(-1)),{tokens:300000,source_bytes:20000000,enabled:false},'Only the active folder override applies');
 userAllocation={source_bytes:12000000,byFolder:{[tmp]:{enabled:true},[tmp+'-other']:{source_bytes:20000000}}};
 await api.getState();
 assert.deepEqual(JSON.parse(bridgeAllocations.at(-1)),{source_bytes:12000000,enabled:true});
 userAllocation={byFolder:{[tmp]:true}};
 await api.getState();
 assert.equal(JSON.parse(bridgeAllocations.at(-1)),null,'An invalid matching folder override must fail closed');
 userAllocation=undefined;
 assert.equal(requestSpawns,1,'Successive normal operations must reuse the same governed bridge worker');
 for(const name of ['karma.reviewCodebase','karma.automaticReview','karma.newChatReview']){
  const before=requests.length;await commands.get(name)({goal:'agent supplied scope',roots:['..'],run:false});
  assert.equal(requests.length,before);assert(prompts.length>0);
 }
 assert(![...tools.keys()].some(k=>/new|execute|edit/.test(k)));
 for(const name of ['karma.importCalibration','karma.selectCalibrations','karma.revokeCalibration','karma.evaluateCalibration']){
  const before=requests.length,promptCount=prompts.length;
  await commands.get(name)({profile:{kernel:{k:100}},profiles:[],reason:'agent supplied',sample:{value:0}});
  assert(prompts.length>promptCount,'Calibration command skipped the user picker');
  assert(!requests.slice(before).some(r=>/^calibration_(register|bind|revoke|evaluate)$/.test(r.op)),'Command arguments mutated calibration');
 }
 assert(![...tools.keys()].some(k=>/calibration/i.test(k)));
 await commands.get('karma.configureModels')({low:'agent-choice',mid:'agent-choice',high:'agent-choice'});
 assert(!panelMessages.some(m=>m.type==='show-section'),'Focus must wait for the panel script');
 await panel.webview.receive({type:'ready'});
 assert(panelMessages.some(m=>m.type==='show-section'&&m.section==='models'),'First-open model picker lost its focus request');
 assert(panelMessages.some(m=>m.type==='runtime-settings'&&m.settings.storageLimitMiB===512));
 assert.deepEqual(structuredClone(panelMessages.filter(m=>m.type==='runtime-settings').at(-1).settings.tierReasoning),
  {low:'inherit',mid:'inherit',high:'inherit'});
 const beforeRuntime=requests.length;
 for(const settings of [{storageLimitMiB:0,pythonPath:''},{storageLimitMiB:512.5,pythonPath:''},{storageLimitMiB:512,pythonPath:'python3\nanything'},{storageLimitMiB:512,pythonPath:'',extra:true}])await panel.webview.receive({op:'save_runtime_settings',settings});
 assert.equal(runtimeUpdates.length,0);
 await panel.webview.receive({op:'save_runtime_settings',settings:{storageLimitMiB:1024,pythonPath:' /test/python3 ',thinking:'on'}});
 assert.deepEqual(runtimeUpdates,[['storageLimitMiB',1024],['pythonPath','/test/python3'],['thinking','on']]);assert.equal(requests.length,beforeRuntime,'Runtime settings must not mutate the task or dispatch');
 await panel.webview.receive({op:'save_runtime_settings',settings:{storageLimitMiB:1024,pythonPath:'',thinking:'adaptive'}});
 assert.deepEqual(runtimeUpdates.slice(-3),[['storageLimitMiB',1024],['pythonPath',''],['thinking','adaptive']]);
 assert.equal(requests.length,beforeRuntime,'Choosing Adaptive must not dispatch a model or change a task');
 const beforeTierSave=runtimeUpdates.length;
 for(const settings of [null,{low:'on'},{low:'on',mid:'off',high:'unsupported'},
  {low:'on',mid:'off',high:'inherit',auto:'off'}]){
  await panel.webview.receive({op:'save_tier_reasoning',settings});
 }
 assert.equal(runtimeUpdates.length,beforeTierSave,'Malformed tier preferences must not be persisted');
 const fixedReasoning={low:'off',mid:'model-default',high:'inherit'};
 await panel.webview.receive({op:'save_tier_reasoning',settings:fixedReasoning});
 assert.deepEqual(runtimeUpdates.slice(beforeTierSave),[['tierReasoning',fixedReasoning]]);
 assert.deepEqual(structuredClone(panelMessages.filter(m=>m.type==='runtime-settings').at(-1).settings.tierReasoning),fixedReasoning);
 assert.equal(panelMessages.at(-1).type,'tier-reasoning-saved');
 assert.equal(requests.length,beforeRuntime,'Saving tier reasoning must not dispatch or change a task');
 await commands.get('karma.configureReasoning')();
 assert(panelMessages.some(m=>m.type==='show-section'&&m.section==='reasoning'));
 const native=participants.get('karma.local'),rendered=[],buttons=[],token={isCancellationRequested:false,onCancellationRequested:()=>disposable()};
 const capture={markdown(m){rendered.push(typeof m==='string'?m:m.value);},progress(){},reference(){},button(b){buttons.push(b);}};
 assert(views.has('karma.usage'));assert(statusItems.some(i=>i.command==='karma.openMonitor'));
 const beforeUsage=requests.length;await native({command:'usage',prompt:''},{},capture,token);assert.equal(requests.length,beforeUsage);
 const beforeGreeting=requests.length,oldState=JSON.stringify(state);
 const greeting=await native({prompt:'hello'}, {},capture,token);
 assert(!greeting?.errorDetails);assert.equal(requests.length,beforeGreeting);assert.equal(JSON.stringify(state),oldState);
 for(const command of ['karma.open','karma.configureModels','karma.newChatReview'])assert(buttons.some(b=>b.command===command));
 const beforeBlocked=requests.length;
 const blocked=await native({prompt:'Review this codebase'}, {},capture,token);
 assert(!blocked?.errorDetails);assert.deepEqual(requests.slice(beforeBlocked).map(r=>r.op),['state']);assert.equal(JSON.stringify(state),oldState);
 assert(buttons.some(b=>b.command==='karma.openAdvanced'));
 const beforeDocument=requests.length;
 const document=await native({command:'new',prompt:documentPrompt},{},capture,token);
 assert(!document?.errorDetails);
 assert.deepEqual(requests.slice(beforeDocument),[{op:'state',review_prompt:documentPrompt}]);
 assert.equal(rendered.at(-1),documentNotice);
 assert.equal(JSON.stringify(state),oldState);
 await commands.get('karma.openAdvanced')();assert(panelMessages.some(m=>m.type==='show-section'&&m.section==='advanced'));
 const beforeFresh=requests.length;
 await native({command:'new',prompt:'Review fresh sources'}, {},capture,token);
 const freshCalls=requests.slice(beforeFresh);assert.equal(freshCalls.filter(r=>r.op==='auto_new').length,1);
 const creation=freshCalls.find(r=>r.op==='auto_new');assert.equal(creation.goal,'Review fresh sources');assert.deepEqual(creation.roots,['.']);assert(freshCalls.some(r=>r.op==='orchestrate'&&r.event==='start_review'));
 state=JSON.parse(oldState);
 await native({command:'status',prompt:''}, {},capture,token);assert(rendered.some(m=>m.includes('42.1 °C')&&m.includes('40.8 °C')));
 const errorsBefore=rendered.length;vscode.workspace.textDocuments=[{isDirty:true,uri:uri(path.join(tmp,'a.py'))}];
 const failure=await native({prompt:'Review sources'}, {},capture,token);assert(failure.errorDetails);assert.equal(rendered.length,errorsBefore,'Errors must render once through errorDetails');vscode.workspace.textDocuments=[];
 assert.equal(participants.size,1);state={...state,auto:{pending_files:0,mapped_files:2,source_files:2}};
 const stream={markdown(){},progress(){},reference(){},button(){}},beforeChat=requests.length;
 await participants.get('karma.local')({prompt:'Explain start.',model:{sendRequest(){throw Error('Must never use VS Code model provider');}}},{history:[{content:'Do not forward transcript'}]},stream,{isCancellationRequested:false,onCancellationRequested:()=>disposable()});
 const chatCalls=requests.slice(beforeChat);assert(chatCalls.some(r=>r.op==='orchestrate'&&r.event==='start_review'));
 const chatRequest=chatCalls.find(r=>r.op==='orchestrate');assert.deepEqual(chatRequest.models,tierModels);assert.equal(chatRequest.prompt,'Explain start.');assert(!('history' in chatRequest));assert(!chatCalls.some(r=>r.op==='auto_new'));
 assert.equal(requestThinking.get(chatRequest),'adaptive','Auto cannot inherit the reasoning of the routed model tier');
 const beforeFixedChat=requests.length;
 await native({command:'low',prompt:'Explain the start function.'},{},stream,token);
 const fixedChat=requests.slice(beforeFixedChat).find(r=>r.op==='orchestrate');
 assert.equal(fixedChat.tier,'low');
 assert.equal(requestThinking.get(fixedChat),'off','Explicit legacy chat must use its fixed tier preference');
 const cancelledBefore=requests.length;await participants.get('karma.local')({prompt:'Cancelled'}, {},stream,{isCancellationRequested:true});assert.equal(requests.length,cancelledBefore);
 await commands.get('karma.openReport')();await commands.get('karma.openAPIReport')();
 assert.equal(provider.provideTextDocumentContent(uri('karma-preview:/fixture/Architecture.md')),'architecture v1');
 assert.equal(typeof provider.onDidChange,'function');
 state={...state,report:'architecture v2',api:{report:'API v2'}};await api.getState();
 assert.equal(provider.provideTextDocumentContent(uri('karma-preview:/fixture/Architecture.md')),'architecture v2');
 assert.equal(provider.provideTextDocumentContent(uri('karma-preview:/fixture/Reviewed-API-map.md')),'API v2');
 assert(changes.includes('karma-preview:/fixture/Architecture.md'));
 state={...state,report:undefined,api:undefined};await api.getState();assert(provider.provideTextDocumentContent(uri('karma-preview:/fixture/Architecture.md')).includes('withheld'));
 const reviewRequest='Explain the workspace architecture with citations.';
 inputValues.push(reviewRequest,'.');
 architectureDispatch=req=>{
   if(req.mode==='guided')return {state,result:{admission:{admitted:true},accepted_output:{tool:{kind:'read'}}},tool_receipt:{result:{status:'ok'}}};
   if(req.mode==='recover_evidence')assert.equal(req.run_id,'a'.repeat(32));
   state={...state,report:'Reviewed architecture'};
   return {state,result:{admission:{admitted:true},accepted_output:{tool:{kind:'report'}}}};
 };
 const beforeGuided=requests.length;
 await commands.get('karma.reviewCodebase')();
 const guidedRequests=requests.slice(beforeGuided).filter(req=>req.op==='architecture');
 assert.deepEqual(guidedRequests.map(req=>req.mode),['guided','guided','guided','guided','report']);
 assert(guidedRequests.every(req=>req.prompt===reviewRequest),'The review must carry the original request into report mode');
 assert.equal(openedDocuments.at(-1),'karma-preview:/fixture/Architecture.md');
 await panel.webview.receive({op:'architecture_read_and_retry',run_id:'a'.repeat(32)});
 assert.equal(requests.at(-1).mode,'recover_evidence');
 assert(!Object.hasOwn(requests.at(-1),'prompt'),'Recovery must use the saved prompt');
 architectureDispatch=undefined;
 await api.open();await new Promise(setImmediate);
 const dock={visible:true,webview:{asWebviewUri:u=>u,postMessage(m){this.latest=m;},onDidReceiveMessage(cb){this.receive=cb;return disposable();}},onDidChangeVisibility(cb){this.visibility=cb;return disposable();},onDidDispose(cb){this.close=cb;return disposable();}};
 views.get('karma.usage').resolveWebviewView(dock);
 assert(dock.webview.html.includes("default-src 'none'"));assert(!dock.webview.html.includes('NONCE_VALUE'));
 const beforeUntrusted=requests.length;for(const op of ['new','execute','work'])dock.webview.receive({type:op,op,edits_allowed:true});assert.equal(requests.length,beforeUntrusted);
 dock.webview.receive({type:'ready'});await new Promise(setImmediate);assert.equal(dock.webview.latest.type,'usage');
 holdDashboard=true;const count=bridgeCount,spawnCount=dashboardSpawns;const first=intervals[0]();await Promise.resolve();
 await intervals[0]();assert.equal(bridgeCount,count+1,'Dashboard polls overlapped');
 pendingDashboard();await first;const second=intervals[0]();await Promise.resolve();assert.equal(bridgeCount,count+2);pendingDashboard();await second;assert.equal(dashboardSpawns,spawnCount,'Dashboard spawned a process for each sample');
 panel.visible=false;const dockOnly=intervals[0]();await Promise.resolve();assert.equal(bridgeCount,count+3);pendingDashboard();await dockOnly;
 dock.visible=false;await intervals[0]();assert.equal(bridgeCount,count+3,'Hidden dashboards should not read the bridge');
 dock.close();assert(statusItems.some(i=>i.command==='karma.openMonitor'));
 const telemetry=require(path.join(root,'harness/telemetry_portable.js'));
 assert.equal(telemetry.darwinAvailable('Mach Virtual Memory Statistics: (page size of 4096 bytes)\nPages free: 1.\nPages inactive: 2000000.\n'),4096*2000001);
 assert.throws(()=>telemetry.darwinAvailable('unavailable'));
 const pkg=JSON.parse(fs.readFileSync(path.join(root,'package.json')));assert(pkg.contributes.commands.some(c=>c.command==='karma.openReport'));
 for(const name of ['importCalibration','selectCalibrations','revokeCalibration','evaluateCalibration'])assert(pkg.contributes.commands.some(c=>c.command==='karma.'+name));
 vscode.lm.registerLanguageModelChatProvider=()=>disposable();
 moduleFixture.exports.activate({subscriptions,extensionPath:root,globalStorageUri:uri(tmp),extension:{packageJSON:{version:'0.18.0'}}});
 nativeStatus({enabled:false,label:'Sutra: off',explanation:'Sutra is off.',reason:'Saved setting.'});
 const sutraStatus=statusItems.find(i=>i.command==='karma.nativeControls');
 assert(sutraStatus,'The primary status item must open quick controls');
 assert.equal(sutraStatus.name,'Sutra controls');assert(sutraStatus.tooltip.includes('Click for Sutra controls'));
 assert.equal(sutraStatus.accessibilityInformation.role,'button');assert(sutraStatus.accessibilityInformation.label.includes('Sutra: off'));
 nativeStatus({enabled:true,label:'Sutra: request received',explanation:'Request reached Sutra.',reason:'',routing:{active:true}});
 assert(sutraStatus.text.startsWith('$(sync~spin)'));assert.equal(sutraStatus.command,'karma.nativeControls');
 const requestsBeforeOverride=requests.length,updatesBeforeOverride=runtimeUpdates.length;
 for(const event of ['start','step']){
  await nativeRequest({op:'orchestrate',event,tier:'low',think_for_turn:true});
  assert.equal(bridgeThinking.at(-1),'on');
  assert.equal(bridgeChildThinking.at(-1),'off','One-shot orchestration thinking must preserve the configured child preference');
  assert(!Object.hasOwn(requests.at(-1),'think_for_turn'),'Private transport flag reached the bridge payload');
 }
 await nativeRequest({op:'orchestrate',event:'start'});
 assert.equal(bridgeThinking.at(-1),'adaptive');
 assert.equal(bridgeChildThinking.at(-1),null,'The next ordinary request must clear the child override explicitly');
 for(const msg of [{op:'orchestrate',event:'start',think_for_turn:'on'},{op:'orchestrate',event:'pending',think_for_turn:true},{op:'state',think_for_turn:true}])
  await assert.rejects(nativeRequest(msg),/Invalid Sutra reasoning override/);
 await panel.webview.receive({op:'orchestrate',event:'start',think_for_turn:true});
 assert.equal(requests.length,requestsBeforeOverride+3,'Untrusted reasoning overrides dispatched');
 assert.equal(runtimeUpdates.length,updatesBeforeOverride,'One-request reasoning changed persisted settings');
 assert(panelMessages.some(m=>m.type==='error'&&m.message.includes('Invalid Sutra reasoning override')));
 for(const [tier,wanted] of [['mid','model-default'],['high','adaptive']]){
  await nativeRequest({op:'orchestrate',event:'step',tier,think_for_turn:true});
  assert.equal(bridgeThinking.at(-1),'on');
  assert.equal(bridgeChildThinking.at(-1),wanted,'One-shot thinking must preserve each configured base preference');
 }
 const savedBaseThinking=runtimeSettings.get('thinking');runtimeSettings.set('thinking','on');
 await nativeRequest({op:'orchestrate',event:'step',tier:'auto',think_for_turn:true});
 assert.equal(bridgeThinking.at(-1),'on');assert.equal(bridgeChildThinking.at(-1),'on');
 assert.equal(runtimeSettings.get('thinking'),'on','One-shot thinking must not change an existing On setting');
 await nativeRequest({op:'orchestrate',event:'step',tier:'auto'});
 assert.equal(bridgeThinking.at(-1),'on');assert.equal(bridgeChildThinking.at(-1),null);
 runtimeSettings.set('thinking',savedBaseThinking);
 for(const [event,tier,wanted] of [
  ['start','low','off'],['step','mid','model-default'],['resume','low','off'],
  ['step','high','adaptive'],['step','auto','adaptive']
 ]){
  await nativeRequest({op:'orchestrate',event,tier});
  assert.equal(requestThinking.get(requests.at(-1)),wanted,`Wrong reasoning for ${event} at ${tier}`);
  assert.equal(requestChildThinking.get(requests.at(-1)),null,'Ordinary requests must not inherit the prior child override');
 }
 assert.deepEqual(runtimeSettings.get('tierReasoning'),fixedReasoning);
 assert.equal(runtimeSettings.get('thinking'),'adaptive');
 const beforeNativeRejection=requests.length;
 await panel.webview.receive({op:'strata_autostart',enabled:'yes'});
 const nativeError=panelMessages.filter(m=>m.type==='strata-state').at(-1);
 assert.equal(typeof nativeError.data.error,'string');
 assert.equal(nativeError.data.busy,false,'A rejected action must release pending native UI controls');
 context.process.platform='linux';
 await panel.webview.receive({op:'strata_load',id:'forged-profile'});
 assert.match(panelMessages.filter(m=>m.type==='strata-state').at(-1).data.error,/local macOS/);
 context.process.platform=process.platform;
 assert.equal(requests.length,beforeNativeRejection,'Rejected native controls must not dispatch a task');
 const ref='sutra:engine/unapproved',engineRow={name:ref,eligible:true,model:'Locally installed fixture',profile_hash:'a'.repeat(64),executable_path:'/trusted/Sutra/Engine/native',executable_sha256:'b'.repeat(64)};
 fixtureCatalog={models:[...Object.values(tierModels).map(name=>({name,eligible:true})),engineRow],suggested_tiers:tierModels};
 const engineSelection={low:ref,mid:ref,high:ref};allowModelSave=true;
 const beforeApproval=runtimeUpdates.length;
 await panel.webview.receive({op:'save_model_tiers',models:engineSelection});
 assert.equal(userModels,undefined,'Dismissed execution approval cannot assign tiers');
 assert.equal(runtimeUpdates.length,beforeApproval,'Dismissed execution approval cannot persist trust');
 const approvalPrompt=prompts.at(-1);
 assert.equal(approvalPrompt.title,'Allow Sutra to run these local engine profiles?');
 for(const evidence of [engineRow.executable_path,engineRow.executable_sha256,engineRow.model,engineRow.profile_hash])assert(approvalPrompt.detail.includes(evidence),'Approval omitted executable/model identity');
 warningChoice='Approve engine profiles';
 await panel.webview.receive({op:'save_model_tiers',models:engineSelection});
 assert.deepEqual(userEngineApprovals,{[ref]:engineRow.profile_hash});
 assert.deepEqual(userModels,engineSelection);
 const promptsAfterApproval=prompts.length;
 await panel.webview.receive({op:'save_model_tiers',models:engineSelection});
 assert.equal(prompts.length,promptsAfterApproval,'An unchanged approved profile does not prompt again');
 fixtureCatalog.models.at(-1).profile_hash='c'.repeat(64);warningChoice=undefined;
 await panel.webview.receive({op:'save_model_tiers',models:engineSelection});
 assert.equal(prompts.length,promptsAfterApproval+1,'Changed engine identity requires approval again');
 assert.equal(userEngineApprovals[ref],'a'.repeat(64),'Dismissal preserves the prior approval instead of widening it');
 console.log(JSON.stringify({passed:true,checks:['runtime settings validate before saving and never dispatch or change the task','greeting with legacy task requires no model or state mutation','legacy task yields actionable guidance without duplicate error or model discovery','explicit /new creates a separate review and dispatches','new-review command arguments cannot bypass user goal entry','first-open model picker waits for webview readiness','status renders measured CPU/GPU Celsius','real errors render once','restricted public API','command arguments cannot bypass user input','calibration commands require user selection','no agent calibration mutation tools','agent tool boundary','native @sutra uses private bridge and configured models, never VS Code inference or transcript replay','cancelled native chat never dispatches','one-request reasoning boosts orchestration while delegated children keep configured preferences','fixed tiers keep distinct saved reasoning without affecting Auto','model discovery preserves configured choices; command arguments cannot replace them','live report refresh and invalidation','persistent single-flight dashboard','Darwin reclaimable-memory parser','report and calibration commands declared']},null,2));
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{for(const d of subscriptions)d.dispose?.();for(const owner of coordinatorOwners.values())owner.worker.dispose();await new Promise(setImmediate);fs.rmSync(tmp,{recursive:true,force:true});});
