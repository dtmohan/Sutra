// Exercise the real extension entry point using a VS Code host fixture.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os'),vm=require('node:vm');
const {EventEmitter}=require('node:events');
const root=path.resolve(__dirname,'..'),tmp=fs.mkdtempSync(path.join(os.tmpdir(),'karma-host-test-'));
const views=new Map(),statusItems=[],commands=new Map(),tools=new Map(),participants=new Map(),intervals=[],timers=[],prompts=[],requests=[],changes=[],panelMessages=[];
const tierModels={low:'fixture-small',mid:'fixture-mid',high:'fixture-large'};
const runtimeUpdates=[];
let provider,panel,holdDashboard=false,pendingDashboard,bridgeCount=0,dashboardSpawns=0;
let state={contract:{contract_hash:'fixture-contract',contract:{constraints:{}}},calibration:{binding:{hash:'fixture-calibration'}},session:'fixture',model:'fixture-model',report:'architecture v1',api:{report:'API v1'}};
const disposable=()=>({dispose(){}}),uri=value=>({scheme:'file',fsPath:value,toString(){return value;}});
class Emitter{event=(cb)=>{this.cb=cb;return disposable();};fire(value){changes.push(value.toString());this.cb?.(value);}dispose(){}}
function spawn(exe,args){
 const streaming=args.includes('--dashboard-stream');if(streaming)dashboardSpawns++;
 const child=new EventEmitter();child.stdout=new EventEmitter();child.stderr=new EventEmitter();child.stdin=new EventEmitter();child.kill=()=>true;
 child.stdin.end=child.stdin.write=raw=>{const req=streaming?{op:'dashboard'}:JSON.parse(raw);requests.push(req);bridgeCount++;
  const finish=()=>{if(req.op==='auto_new')state={...state,contract:{contract_hash:'fixture-contract',contract:{constraints:{auto_review:true}}},calibration:{binding:{hash:'fixture-calibration'}},session:'new-review',auto:{pending_files:0,mapped_files:2,source_files:2}};
   const data=req.op==='orchestrate'?{flow_id:'fixture-flow',state:'DONE',data:{state,route:{tier:'low',model:tierModels.low},result:{invocation_id:'fixture',admission:{admitted:true},accepted_output:{answer:'Fixture answer',remaining:[]}},coverage:{mapped_files:2,source_files:2},source_files:['a.py']}}:req.op==='models'?{models:Object.values(tierModels).map(name=>({name,eligible:true})),suggested_tiers:tierModels}:req.op==='chat_answer'?{state,route:{tier:'low',model:tierModels.low,reason:'focused request'},result:{invocation_id:'fixture-invocation',admission:{admitted:true},accepted_output:{answer:'Fixture answer',remaining:[]}},coverage:{mapped_files:2,source_files:2},source_files:['a.py']}:req.op==='calibration_list'?{binding:{hash:'fixture',profiles:[]},profiles:[]}:req.op==='dashboard'?{hardware:{available:false,temperatures:{cpu:{available:true,maximum_celsius:42.12},gpu:{available:true,maximum_celsius:40.82}}},runs:[],invocations:{},actions:[]}:state;
   child.stdout.emit('data',Buffer.from(JSON.stringify({ok:true,data})+'\n'));if(!streaming)child.emit('close',0,null);};
  if(req.op==='dashboard'&&holdDashboard)pendingDashboard=finish;else queueMicrotask(finish);
 };return child;
}
const vscode={
 EventEmitter:Emitter,Uri:{file:uri,parse:uri},StatusBarAlignment:{Right:1},ViewColumn:{One:1},
 env:{appRoot:'/must/not/load/internal/typescript'},
 MarkdownString:class{constructor(value){this.value=value;}},ConfigurationTarget:{Global:1},chat:{createChatParticipant(id,handler){participants.set(id,handler);return disposable();}},
 workspace:{isTrusted:true,workspaceFolders:[{uri:uri(tmp)}],textDocuments:[],getConfiguration(){return {get(k,d){return k==='models'?tierModels:d;},async update(k,v){if(k==='models')throw Error('Existing model choices must not be overwritten by discovery');runtimeUpdates.push([k,v]);}};},
  registerTextDocumentContentProvider(s,p){provider=p;return disposable();},async openTextDocument(u){return {uri:u};},fs:{writeFile:async()=>{}}},
 commands:{registerCommand(n,f){commands.set(n,f);return disposable();},async executeCommand(n,...a){return commands.get(n)?.(...a);}},
 window:{createOutputChannel(){return {appendLine(){},dispose(){}};},registerWebviewViewProvider(id,provider){views.set(id,provider);return disposable();},createStatusBarItem(){const item={show(){},dispose(){}};statusItems.push(item);return item;},
  async showInputBox(options){prompts.push(options);return undefined;},async showQuickPick(items,options){prompts.push(options);},async showOpenDialog(options){prompts.push(options);},async showTextDocument(){},
  createWebviewPanel(){panel={visible:true,reveal(){},onDidDispose(){return disposable();},webview:{postMessage(m){panelMessages.push(m);},asWebviewUri:u=>u,onDidReceiveMessage(cb){this.receive=cb;return disposable();}}};return panel;}},
 lm:{registerTool(n,t){tools.set(n,t);return disposable();}},LanguageModelToolResult:class{constructor(content){this.content=content;}},LanguageModelTextPart:class{constructor(value){this.value=value;}}
};
const moduleFixture={exports:{}};
const context={module:moduleFixture,exports:moduleFixture.exports,require:n=>n==='vscode'?vscode:n==='node:child_process'?{spawn}:n==='./model_activity'?require('../model_activity'):n==='./media/token_usage'?require('../media/token_usage'):n==='./usage_view'?require('../usage_view'):n==='./diagram_view'?require('../diagram_view'):n==='./model_selection'?require('../model_selection'):n==='./native_provider'?require('../native_provider'):require(n),process,Buffer,console,queueMicrotask,structuredClone,
 setInterval:fn=>{intervals.push(fn);return fn;},clearInterval(){},setTimeout:(fn,ms)=>{timers.push({fn,ms});return fn;},clearTimeout(){}};
vm.runInNewContext(fs.readFileSync(path.join(root,'extension.js'),'utf8'),context,{filename:'extension.js'});
const subscriptions=[];
(async()=>{
 const api=moduleFixture.exports.activate({subscriptions,extensionPath:root,globalStorageUri:uri(tmp),extension:{packageJSON:{version:'0.18.0'}}});
 assert.deepEqual(Object.keys(api).sort(),['getRoutingStatus','getState','open','version']);assert(Object.isFrozen(api));const beforeRouting=requests.length;assert.equal(api.getRoutingStatus(),null);assert.equal(requests.length,beforeRouting);
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
 const beforeRuntime=requests.length;
 for(const settings of [{storageLimitMiB:0,pythonPath:''},{storageLimitMiB:512.5,pythonPath:''},{storageLimitMiB:512,pythonPath:'python3\nanything'},{storageLimitMiB:512,pythonPath:'',extra:true}])await panel.webview.receive({op:'save_runtime_settings',settings});
 assert.equal(runtimeUpdates.length,0);
 await panel.webview.receive({op:'save_runtime_settings',settings:{storageLimitMiB:1024,pythonPath:' /test/python3 ',thinking:'on'}});
 assert.deepEqual(runtimeUpdates,[['storageLimitMiB',1024],['pythonPath','/test/python3'],['thinking','on']]);assert.equal(requests.length,beforeRuntime,'Runtime settings must not mutate the task or dispatch');
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
 const cancelledBefore=requests.length;await participants.get('karma.local')({prompt:'Cancelled'}, {},stream,{isCancellationRequested:true});assert.equal(requests.length,cancelledBefore);
 await commands.get('karma.openReport')();await commands.get('karma.openAPIReport')();
 assert.equal(provider.provideTextDocumentContent(uri('karma-preview:/fixture/Architecture.md')),'architecture v1');
 assert.equal(typeof provider.onDidChange,'function');
 state={...state,report:'architecture v2',api:{report:'API v2'}};await api.getState();
 assert.equal(provider.provideTextDocumentContent(uri('karma-preview:/fixture/Architecture.md')),'architecture v2');
 assert.equal(provider.provideTextDocumentContent(uri('karma-preview:/fixture/Reviewed-API-map.md')),'API v2');
 assert(changes.includes('karma-preview:/fixture/Architecture.md'));
 state={...state,report:undefined,api:undefined};await api.getState();assert(provider.provideTextDocumentContent(uri('karma-preview:/fixture/Architecture.md')).includes('withheld'));
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
 console.log(JSON.stringify({passed:true,checks:['runtime settings validate before saving and never dispatch or change the task','greeting with legacy task requires no model or state mutation','legacy task yields actionable guidance without duplicate error or model discovery','explicit /new creates a separate review and dispatches','new-review command arguments cannot bypass user goal entry','first-open model picker waits for webview readiness','status renders measured CPU/GPU Celsius','real errors render once','restricted public API','command arguments cannot bypass user input','calibration commands require user selection','no agent calibration mutation tools','agent tool boundary','native @sutra uses private bridge and configured models, never VS Code inference or transcript replay','cancelled native chat never dispatches','model discovery preserves configured choices; command arguments cannot replace them','live report refresh and invalidation','persistent single-flight dashboard','Darwin reclaimable-memory parser','report and calibration commands declared']},null,2));
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(()=>{for(const d of subscriptions)d.dispose?.();fs.rmSync(tmp,{recursive:true,force:true});});
