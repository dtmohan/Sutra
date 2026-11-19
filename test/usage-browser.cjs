// Render the real docked view with synthetic receipts; no inference or source reads.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.KARMA_PLAYWRIGHT||'playwright');
const {install}=require('../usage_view');
const root=path.resolve(__dirname,'..'),out=process.env.KARMA_UI_OUTPUT;
if(!out)throw Error('Set KARMA_UI_OUTPUT to an artifact directory');
const now=Date.now()/1000;
const calls=[['a','conversation','qwen3:4b','accepted',1284,112,now],['b','conversation','qwen3:4b','quarantined',1000,50,now],['c','chat_reviewer','local-assistant:latest','accepted',4000,200,now],['d','coder','<img src=x onerror=alert(1)>','pending',null,null,now-172800],['e','conversation','qwen3:4b','accepted',0,0,now]].map(([invocation_id,stage,model,status,input_tokens,output_tokens,created_at])=>({invocation_id,stage,model,status,input_tokens,output_tokens,created_at,conservative_input_charge:9000,output_reserve:2048}));
const frame=decision=>({decision,omega:0,sigma:0,k:.15,sigma_clause:1,profile:'fixture-v1'});
const monitor={session:'test',adapter_health:'idle',hardware:{...frame('ALLOW'),available:true,admitted:true,age_seconds:0,thermal_label:'nominal',sample:{cpu_busy_pct:20,memory_pressure_raw:1},reclaimable_estimate_gib:24.5,temperatures:{age_seconds:0,cpu:{available:true,maximum_celsius:48.2,mean_celsius:46},gpu:{available:true,maximum_celsius:44.6,mean_celsius:43}}},storage:frame('ALLOW'),review_budget:frame('BRIDGE'),context:{conservative_input_charge:3000,output_reserve:1000,limit:8192,actual_prompt_tokens:1200,last_kernel_budget:frame('ALLOW')},kernel:{name:'Smriti four-decision',sha256:'fixture',order:['COMPOUND_FIN','ALLOW','BRIDGE','FIN']}};
monitor.logical_memory={counters:{deliveries:4,page_ins:9,evictions:3,refaults:2},retention:{authority:'durable; never automatically evicted or pruned',working_set:'evict from prompt only; retain exact backing pages'},events:[{kind:'fault',code:'source_changed',error:'Changed <img src=x onerror=alert(1)>',recovery:'refresh_source_and_dependents'},{kind:'working_set',resident_pages:3,evictions:1,refaults:2,thrashing_observed:true}]};
monitor.orchestration={execution:'serial local dispatch',nodes:[{id:'flow-fixture',kind:'flow',state:'CHILDREN',objective:'Review architecture',files:[]},{id:'child-fixture',kind:'branch',state:'DONE',files:['entry.py','leaf.py','a.py','b.py'],model:'local-assistant:latest',used:{calls:1,tokens:3000},limits:{calls:3,tokens:16000},kernel:frame('ALLOW')}]};
monitor.orchestration.nodes.push({id:'paused-fixture',kind:'flow',state:'PAUSED',objective:'Review larger files',stop:{status:'needs_attention',failure_count:1,failures:[{path:'large.py',error:'Static graph work-unit allocation exceeded'}]},files:[]});
const snapshot={monitor,calls,total_calls:300,limit:250,workspace:'Nadi · example receipts',updatedAt:new Date().toISOString()};
monitor.hardware.sample.compressed_bytes=6*1024**3;monitor.hardware.sample.swap_used_bytes=2*1024**3;
monitor.runtime_memory={enabled:true,ownership:'shared',inventory_available:true,inventory_at:Date.now()/1000,resident_models:[{name:'small <img src=x>',size:3*1024**3,context_length:8192,runner:'fixture'}],plan:{model:'small',context_tokens:8192,estimated_increment_bytes:3*1024**3,system_reserve_bytes:4*1024**3,decision:'ALLOW',estimate_scope:'weights only; KV/backend workspace unknown'},events:[{event:'recovery',action:'observe_only_shared_server',at:Date.now()/1000}]};
monitor.runtime_memory.plan.calibration={profile_sha256:'a'.repeat(64),covered_tokens:16384,margin_bytes:1024**3};
Object.assign(monitor.runtime_memory.plan,{cold_estimate_bytes:6*1024**3,resident_weight_credit_bytes:3*1024**3,resident_credit_basis:'Exact resident identity <img src=x>'});
monitor.orchestration.nodes[0].resource_fallback={from_model:'large',model:'small <img src=x>'};
function monitorHost(){
 let viewProvider,posted,receive;
 const commands=[],disposable=()=>({dispose(){}});
 const uri=fsPath=>({fsPath,toString:()=>fsPath});
 const vscode={
  Uri:{file:uri},StatusBarAlignment:{Right:1},
  window:{createStatusBarItem:()=>({show(){}}),registerWebviewViewProvider(_id,provider){viewProvider=provider;}},
  commands:{registerCommand:disposable,executeCommand(...args){commands.push(args);}},
  workspace:{getConfiguration:()=>({get:()=> 'adaptive'}),onDidChangeConfiguration:disposable}
 };
 const view=install(vscode,{extensionPath:root,subscriptions:[]},{storage(){throw Error('No file-backed archive in this fixture');},refresh(){}});
 viewProvider.resolveWebviewView({
  webview:{asWebviewUri:uri=>uri,postMessage:message=>{posted=message;},onDidReceiveMessage(handler){receive=handler;}},
  onDidChangeVisibility:disposable,onDidDispose:disposable
 });
 receive({type:'ready'});
 return {view,commands,receive,get posted(){return posted;}};
}
(async()=>{
 fs.mkdirSync(out,{recursive:true});const browser=await chromium.launch({headless:true,...(process.env.KARMA_BROWSER?{executablePath:process.env.KARMA_BROWSER}:{})});
 try{
 const page=await browser.newPage({viewport:{width:1140,height:720}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 const html=saved=>fs.readFileSync(path.join(root,'media/usage.html'),'utf8').replaceAll('NONCE_VALUE','test-nonce').replace('<script nonce="test-nonce" src="TOKENS_URI"></script>','<script nonce="test-nonce">'+fs.readFileSync(path.join(root,'media/token_usage.js'),'utf8')+'</script>').replace('<script nonce="test-nonce" src="SCRIPT_URI"></script>','<script nonce="test-nonce">window.sent=[];window.saved='+JSON.stringify(saved||{})+';function acquireVsCodeApi(){return {postMessage:m=>sent.push(m),setState:s=>window.saved=s,getState:()=>window.saved}}\n'+fs.readFileSync(path.join(root,'media/usage.js'),'utf8')+'</script>');
 await page.setContent(html());const send=data=>page.evaluate(d=>window.dispatchEvent(new MessageEvent('message',{data:d})),{type:'usage',...data,snapshot:data.snapshot?{...data.snapshot,updatedAt:data.aged?data.snapshot.updatedAt:new Date().toISOString()}:undefined});
 assert.deepEqual(await page.evaluate(()=>sent),[{type:'ready'}]);assert.equal(await page.locator('#input').textContent(),'—');
 await send({snapshot,reasoningPreference:'model-default'});
 const host=monitorHost(),id='a'.repeat(32);
 const dashboard=stop=>({...monitor,token_usage:{calls,total_calls:300,limit:250},runs:[{id,state:'FIN',recent_events:[{kind:'FIN',at:now,
   data:{architecture_stop:{run_id:id,terminal:false,reason:'insufficient_evidence',message:'More source is needed.',covered_files:1,required_files:2,repository_budget:{used:2,limit:5},source:'PRIVATE SOURCE',...stop}}}]}]});
 host.view.setStatus({enabled:true,armed:true,accessProfile:'read_only'});
 host.view.setData(dashboard(),snapshot.workspace);
 assert(!JSON.stringify(host.posted).includes('PRIVATE SOURCE'));assert(!('runs' in host.posted.snapshot.monitor));
 await send(host.posted);
 assert.equal(await page.locator('#workspace-access').textContent(),'Workspace access · Read only');
 assert((await page.locator('#architecture-stops').textContent()).includes('1 of 2 required files inspected. 3 source operations remaining.'));
 await page.locator('#architecture-read-and-retry').click();
 const recover=await page.evaluate(()=>sent.at(-1));
 assert.deepEqual(recover,{type:'architecture_read_and_retry',run_id:id});
 host.receive({...recover,run_id:'old-run'});assert.deepEqual(host.commands,[]);
 host.receive(recover);assert.deepEqual(host.commands,[['karma.architectureReadAndRetry',id]]);
 host.view.setData(dashboard({terminal:true,message:'Repository budget exhausted. Start a new review.'}),snapshot.workspace);
 await send(host.posted);
 assert((await page.locator('#architecture-stops').textContent()).includes('Architecture review stopped. Repository budget exhausted.'));
 assert(await page.locator('#architecture-read-and-retry').isHidden());
 host.receive(recover);assert.equal(host.commands.length,1,'A stale recovery action cannot retry a terminal stop');
 const newerRun=dashboard();newerRun.runs.unshift({state:'ALLOW',recent_events:[{kind:'ALLOW'}]});
 host.view.setData(newerRun,snapshot.workspace);await send(host.posted);
 assert(await page.locator('#saved-review').isHidden());
 const taskPicker=page.getByRole('button',{name:'Open tasks',exact:true});
 assert(await taskPicker.isVisible(),'Task picker remains available when no recovery is focused');
 host.view.setStatus({enabled:true,armed:true,openTaskCount:2,working:true});
 await send(host.posted);
 assert(await page.getByRole('button',{name:'Open tasks (2)',exact:true}).isDisabled());
 const beforeOpen=host.commands.length;
 host.receive({type:'open_tasks'});assert.equal(host.commands.length,beforeOpen);
 host.view.setStatus({enabled:true,armed:true,openTaskCount:2,working:false});
 await send(host.posted);
 const openTasks=page.getByRole('button',{name:'Open tasks (2)',exact:true});
 await openTasks.focus();await send(host.posted);
 assert(await openTasks.evaluate(e=>e===document.activeElement),'Task picker retains focus during refresh');
 await openTasks.press('Enter');
 assert.deepEqual(await page.evaluate(()=>sent.at(-1)),{type:'open_tasks'});
 host.receive({type:'open_tasks',flow_id:'untrusted-id'});
 assert.deepEqual(host.commands.slice(beforeOpen),[['karma.openTasks']]);
 await send({snapshot,native:{enabled:true,permissionPending:true,permissionCanChoose:true}});
 const accessChoice=page.getByRole('button',{name:'Workspace access…',exact:true});
 assert(await accessChoice.isVisible());await accessChoice.focus();
 await send({snapshot,native:{enabled:true,permissionPending:true,permissionCanChoose:true}});
 assert(await accessChoice.evaluate(e=>e===document.activeElement),'Refreshing the monitor preserves the permission action focus');
 await accessChoice.press('Enter');
 assert.deepEqual(await page.evaluate(()=>sent.at(-1)),{type:'workspace_access'});
 host.view.setStatus({enabled:true,permissionPending:true,permissionCanChoose:true});
 const beforePermission=host.commands.length;
 host.receive({type:'workspace_access',profile:'untrusted'});
 assert.deepEqual(host.commands.slice(beforePermission),[['karma.armNative']],'The host permission command accepts no profile from the webview');
 host.view.setStatus({enabled:true,permissionPending:true,permissionCanChoose:false,working:true});
 host.receive({type:'workspace_access'});
 assert.equal(host.commands.length,beforePermission+1,'A different active request blocks permission mutation');
 await send({snapshot,native:{enabled:true,permissionPending:true,permissionCanChoose:false}});
 assert(await accessChoice.isDisabled(),'A different active request cannot change workspace access');
 await send({snapshot,native:{enabled:true,permissionPending:false,permissionCanChoose:true}});
 assert(await accessChoice.isHidden());
 const accounting={basis:'runtime_measured',reserved_tokens:11048,used_tokens:1396,released_unused_reservation:9652,overrun_tokens:0};
 await send({snapshot:{...snapshot,calls:[{...calls[0],accounting_settlement:accounting},...calls.slice(1)],monitor:{...monitor,context:{...monitor.context,model_packet:{prompt_utf8_bytes:2400,schema_allowance:344,template_allowance:256},accounting_settlement:accounting}}}});
 assert((await page.locator('#rows').textContent()).includes('1,396 (measured)'));
 assert((await page.locator('#rows').textContent()).includes('Original reservation'));
 assert((await page.locator('#context-packet').textContent()).includes('2,400 UTF-8 bytes'));
 assert((await page.locator('#context-packet').textContent()).includes('Unused reservation released: 9,652'));
 await page.screenshot({path:path.join(out,'Model-packet-accounting.png'),fullPage:true});
 await send({snapshot,reasoningPreference:'model-default'});
 assert.equal(await page.locator('#resource').getAttribute('role'),null);
 const bar=page.locator('#timeline .call-bar').first();
 await bar.focus();await send({snapshot,reasoningPreference:'model-default'});
 assert(await bar.evaluate(e=>e===document.activeElement),'A telemetry refresh preserves keyboard focus in the chart');
 await page.evaluate(()=>{
  window.liveUpdates=0;
  window.liveObserver=new MutationObserver(records=>window.liveUpdates+=records.length);
  for(const item of document.querySelectorAll('[role="status"]'))window.liveObserver.observe(item,{childList:true,characterData:true,subtree:true});
 });
 await send({snapshot,reasoningPreference:'model-default'});
 assert.equal(await page.evaluate(()=>window.liveUpdates),0,'An unchanged heartbeat leaves live regions untouched');

 assert.equal(await page.locator('#reasoning-preference').textContent(),'Shared reasoning: Model default');
 assert(await page.locator('#tier-reasoning-summary').isHidden());
 assert((await page.locator('#memory-summary').textContent()).includes('3 evictions · 2 refaults'));
 await send({snapshot:{...snapshot,monitor:{...monitor,ram_compartments:{
  enabled:true,max_bytes:64*1048576,active_bytes:1048576,warm_bytes:3*1048576,
  active_entries:6,warm_entries:14,last_request:{active_hits:4,warm_hits:7,misses:2,
   evictions:1,decode_ns:1200000,copy_ns:400000,lookup_ns:100000}
 }}}});
 assert.equal(await page.locator('#ram-active').textContent(),'1 MiB');
 assert.equal(await page.locator('#ram-warm').textContent(),'3 MiB');
 assert((await page.locator('#ram-compartments-hits').textContent()).includes('7 warm promotions'));
 assert((await page.locator('#ram-compartments-time').textContent()).includes('1.20 ms'));
 await send({snapshot});
 assert((await page.locator('#memory-events').textContent()).includes('refresh source and dependents'));
 assert.equal(await page.locator('#memory-events img').count(),0);
 assert((await page.locator('#memory-retention').textContent()).includes('never automatically evicted'));
 await send({reasoningPreference:'model-default',snapshot:{...snapshot,monitor:{...monitor,logical_memory:{...monitor.logical_memory,diagnostic_issues:1}}}});
 assert((await page.locator('#memory-events').textContent()).includes('Some memory diagnostics are unreadable'));
 await send({snapshot,reasoningPreference:'model-default'});
 await page.locator('[aria-label="Context memory"] summary').click();
 await page.screenshot({path:path.join(out,'Memory-lifecycle.png'),fullPage:true});
 await page.click('#reasoning-settings');assert.deepEqual((await page.evaluate(()=>sent)).at(-1),{type:'reasoning_settings'});
 await send({snapshot,reasoningPreference:'adaptive',tierReasoning:{low:'off',mid:'inherit',high:'on'}});
 assert.equal(await page.locator('#reasoning-preference').textContent(),'Shared reasoning: Adaptive');
 assert.equal(await page.locator('#tier-reasoning-summary').textContent(),'Sutra Low · Off | Sutra High · On');
 const adaptiveNote=await page.locator('#reasoning-preference-note').textContent();
 assert.match(adaptiveNote,/Sutra Auto and inheriting tiers use the shared setting/);
 assert.match(adaptiveNote,/multi-file report synthesis can use thinking/);
 assert.match(adaptiveNote,/Think on Next Request in the Command Palette for a one-turn override/);
 await page.screenshot({path:path.join(out,'Reasoning-setting.png')});
 const observations=[{gateway_status:'accepted',observation:{mode:'observe_only',kind:'commitment',status:'recorded',invocation_id:'commit-1',version:1,model:'qwen3:4b',request:'Explain source <img src=x onerror=alert(1)>',value:{understanding:'Read source',criteria:[{id:'c1',deliverable:'Overview',done_when:'Source references'}],assumptions:[]},issues:[]}},
 {gateway_status:'quarantined',observation:{mode:'observe_only',kind:'assessment',status:'recorded',invocation_id:'assess-1',model:'larger-model',scope:'worker_response',commitment_id:'commit-1',commitment_version:1,request:'Explain source',value:{judgement:'complete',delivered:'A draft <script>bad</script>',remaining:[],items:[{criterion_id:'c1',status:'met',evidence:'entry.py'}]},issues:['Completion is a model claim.']}}];
 await send({snapshot:{...snapshot,monitor:{...monitor,task_observations:observations}}});
 assert.equal(await page.locator('#task-observations details').count(),2);
 await page.locator('#task-observations summary').first().click();
 assert((await page.locator('#task-observations').textContent()).includes('Gateway: quarantined'));
 assert.equal(await page.locator('#task-observations img,#task-observations script').count(),0);
 await page.waitForTimeout(1200);assert(await page.locator('#task-observations details').first().evaluate(e=>e.open));
 await page.locator('#task-observations').scrollIntoViewIfNeeded();
 await page.screenshot({path:path.join(out,'Task-observations.png')});
 await send({snapshot});assert.equal(await page.locator('#task-observations details').count(),0);
 assert(await page.locator('#task-observation-empty').isVisible());
 await send({snapshot});assert.equal(await page.locator('#input').textContent(),'6,284');assert.equal(await page.locator('#output').textContent(),'362');assert.equal(await page.locator('#unknown').textContent(),'1');assert.equal(await page.locator('#calls').textContent(),'5');assert((await page.locator('#window').textContent()).includes('latest 5 of 300'));assert.equal(await page.locator('.call-bar').count(),5);
 assert.equal(await page.locator('#cpu-temp').textContent(),'48.2 °C');assert.equal(await page.locator('#gpu-temp').textContent(),'44.6 °C');assert.equal(await page.locator('#cpu-load').textContent(),'20%');assert.equal(await page.locator('#ram').textContent(),'24.5 GiB');assert.equal(await page.locator('#resource').textContent(),'System pressure: ALLOW');assert((await page.locator('#gates').textContent()).includes('BRIDGE'));
 assert.equal(await page.locator('#compressed-memory').textContent(),'6.0 GiB');assert.equal(await page.locator('#swap-memory').textContent(),'2.0 GiB');assert((await page.locator('#memory-policy').textContent()).includes('Shared Ollama'));assert((await page.locator('#resident-models').textContent()).includes('small <img'));assert.equal(await page.locator('#resident-models img').count(),0);assert((await page.locator('#memory-plan').textContent()).includes('8,192 context tokens'));assert((await page.locator('#memory-plan').textContent()).includes('Peak memory is not verified'));assert((await page.locator('#memory-recovery').textContent()).includes('observe only shared server'));assert((await page.locator('#memory-plan').textContent()).includes('MLX calibration aaaaaaaaaaaa'));assert((await page.locator('#memory-plan').textContent()).includes('16,384 tokens'));assert((await page.locator('#memory-plan').textContent()).includes('1.0 GiB uncertainty margin'));
 assert.equal(await page.locator('#model-allocation').textContent(),'Last model allocation: ALLOW');
 assert((await page.locator('#memory-plan').textContent()).includes('Cold estimate 6.0 GiB · reusable resident weights 3.0 GiB'));
 assert((await page.locator('#memory-plan').textContent()).includes('This is reuse, not newly freed memory'));
 assert.equal(await page.locator('#memory-plan img').count(),0);
 const nativeMemory={enabled:true,backend:'strata',ownership:'native_owner',inventory_available:true,inventory_at:Date.now()/1000,
  resident_models:[{name:'strata:profile/native-small',size:null,context_length:4096,runner:'strata'}],
  plan:{model:'strata:profile/native-small',context_tokens:4096,estimated_increment_bytes:null,system_reserve_bytes:4*1024**3,decision:'ALLOW',estimate_scope:'Native hardware admission; model peak unmeasured'},events:[]};
 await send({snapshot:{...snapshot,monitor:{...monitor,runtime_memory:nativeMemory}}});
 assert.equal(await page.locator('#memory-policy').textContent(),'Native Strata · profile switching and idle stops managed by Sutra');
 const nativeInventory=await page.locator('#resident-models').textContent();
 assert(nativeInventory.includes('Memory unmeasured · 4,096 context tokens · strata'));
 assert(!nativeInventory.includes('Unavailable reported'));
 assert((await page.locator('#memory-plan').textContent()).includes('incremental memory unmeasured'));
 const ownedMemory={...nativeMemory,backend:'sutra_engine',ownership:'sutra_owned',
  resident_models:[{name:'sutra:engine/registered <img>',size:null,context_length:16384,runner:'sutra_engine'}],
  plan:{...nativeMemory.plan,model:'sutra:engine/registered',context_tokens:16384}};
 await send({snapshot:{...snapshot,monitor:{...monitor,runtime_memory:ownedMemory}}});
 assert((await page.locator('#memory-policy').textContent()).includes('Sutra-owned engine worker'));
 assert((await page.locator('#resident-models').textContent()).includes('Memory unmeasured · 16,384'));
 assert.equal(await page.locator('#resident-models img').count(),0);
 assert((await page.locator('#memory-plan').textContent()).includes('incremental memory unmeasured'));
 await send({snapshot});
 await send({snapshot:{...snapshot,monitor:{...monitor,runtime_memory:{...monitor.runtime_memory,plan:{...monitor.runtime_memory.plan,decision:'BRIDGE'}}}}});
 assert.equal(await page.locator('#resource').textContent(),'System pressure: ALLOW');
 assert.equal(await page.locator('#model-allocation').textContent(),'Last model allocation: BRIDGE');
 assert((await page.locator('#memory-plan').textContent()).includes('not a live fit guarantee'));
 await send({snapshot});
 assert.equal(await page.locator('#flow-nodes .gate').count(),3);assert((await page.locator('#flow-nodes').textContent()).includes('local-assistant:latest'));assert((await page.locator('#flow-nodes').textContent()).includes('read only'));
 await page.locator('#flow-nodes summary').click();await page.locator('#flow-nodes summary').focus();
 assert((await page.locator('#flow-nodes').textContent()).includes('Auto memory fallback: large → small <img'));
 assert((await page.locator('#flow-nodes').textContent()).includes('permissions and spent budget are retained'));
 assert.equal(await page.locator('#flow-nodes img').count(),0);
 await page.locator('[aria-label="Runtime memory"]').scrollIntoViewIfNeeded();
 await page.screenshot({path:path.join(out,'Warm-memory-and-fallback.png'),fullPage:true});
 await page.waitForTimeout(1200);assert(await page.locator('#flow-nodes details').evaluate(e=>e.open));assert.equal(await page.evaluate(()=>document.activeElement.tagName),'SUMMARY');
 await send({snapshot:{...snapshot,monitor:{...monitor,orchestration:{...monitor.orchestration,nodes:monitor.orchestration.nodes.map(n=>({...n,state:'READY'}))}}}});
 assert(await page.locator('#flow-nodes details').evaluate(e=>e.open));assert.equal(await page.evaluate(()=>document.activeElement.tagName),'SUMMARY');
 await send({snapshot,native:{pending:{state:'PAUSED'},working:false}});await page.click('#resume-workflow');await page.click('#discard-workflow');
 assert.deepEqual((await page.evaluate(()=>sent)).slice(-2),[{type:'resume'},{type:'discard'}]);
 const activity=[{id:'parent-1',node_id:'task',role:'conversation',model:'local-large',status:'accepted',text:'Compare the options.',startedAt:Date.now()-3000,finishedAt:Date.now(),usage:{input_tokens:30,output_tokens:40}},
 {id:'child-2',node_id:'child',role:'subagent',model:'local-small',status:'generating',text:'<img src=x onerror=alert(1)> Unverified child reasoning',startedAt:Date.now()}];
 await send({snapshot,activity});assert.equal(await page.locator('#reasoning-calls details').count(),2);
 const noThought={id:'no-thought',role:'conversation',model:'qwen3:4b',status:'accepted',text:'',thinking_chars:0,requested:false,startedAt:Date.now()-1000,finishedAt:Date.now()};
 await send({snapshot,activity:[...activity,noThought]});
 const routerCard=page.locator('[data-id="no-thought"]');
 assert.equal(await routerCard.locator('pre').textContent(),'No separate reasoning text received.');
 assert(!(await routerCard.textContent()).includes('yet'));
 assert((await routerCard.textContent()).includes('off requested'));
 assert((await routerCard.textContent()).includes('Token counts unavailable for this call'));
 await send({snapshot,reasoningPreference:'adaptive',activity:[...activity,{...noThought,usage:{input_tokens:2740,output_tokens:212}}]});
 await routerCard.locator('summary').click();
 await page.screenshot({path:path.join(out,'Call-without-reasoning.png'),fullPage:true});
 await send({snapshot,activity:[...activity,{...noThought,thinking_chars:25}]});
 assert((await routerCard.locator('pre').textContent()).includes('its text is unavailable'));
 await send({snapshot,activity});
 const child=page.locator('#reasoning-calls details').first();await child.locator('summary').click();await child.locator('summary').focus();
 await send({snapshot,activity:activity.map(c=>c.id==='child-2'?{...c,text:c.text+' updated',status:'withheld',usage:{input_tokens:12,output_tokens:80}}:c)});
 assert(await child.evaluate(e=>e.open));assert.equal(await page.evaluate(()=>document.activeElement.tagName),'SUMMARY');assert.equal(await child.locator('img').count(),0);assert((await child.textContent()).includes('withheld'));assert((await child.textContent()).includes('80 output'));
 await child.getByRole('button',{name:'Open call details'}).click();
 assert.deepEqual((await page.evaluate(()=>sent)).at(-1),{type:'activity_details',id:'child-2'});
 await send({snapshot,activity,activityError:'Could not save diagnostic history'});assert((await page.locator('#reasoning-error').textContent()).includes('Could not save'));
 await page.screenshot({path:path.join(out,'Live-child-reasoning.png'),fullPage:true});
 await page.screenshot({path:path.join(out,'Sutra-monitor.png'),fullPage:true});
 await page.selectOption('#stage','conversation');assert.equal(await page.locator('#input').textContent(),'2,284');await send({snapshot});assert.equal(await page.locator('#calls').textContent(),'3');assert.equal(await page.locator('#stage').inputValue(),'conversation');assert.equal(await page.locator('#cpu-temp').textContent(),'48.2 °C');
 await page.selectOption('#status','quarantined');assert.equal(await page.locator('#output').textContent(),'50');await page.selectOption('#model','local-assistant:latest');assert.equal(await page.locator('#input').textContent(),'—');assert(await page.locator('#empty').isVisible());assert.equal(await page.locator('#empty-title').textContent(),'No calls match these filters');
 await page.click('#reset');await page.selectOption('#time','86400');assert.equal(await page.locator('#calls').textContent(),'4');assert.equal(await page.locator('#unknown').textContent(),'0');
 await page.click('#reset');await page.selectOption('#status','pending');assert.equal(await page.locator('#input').textContent(),'—');assert.equal(await page.locator('#rows img').count(),0);assert((await page.locator('#rows').textContent()).includes('<img'));
 await page.click('#reset');await send({snapshot:{...snapshot,calls:[calls[4]],total_calls:1}});assert.equal(await page.locator('#input').textContent(),'0');assert.equal(await page.locator('#output').textContent(),'0');assert.equal(await page.locator('#unknown').textContent(),'0');
 await send({snapshot:{...snapshot,calls:[],total_calls:0}});assert.equal(await page.locator('#input').textContent(),'—');assert.equal(await page.locator('#empty-title').textContent(),'No model calls yet');assert(!(await page.locator('#visuals').isVisible()));
 await send({snapshot,native:{enabled:true,armed:true,accessProfile:'workspace_agent'}});
 assert.equal(await page.locator('#workspace-access').textContent(),'Workspace access · Workspace agent');
 assert((await page.locator('#native-request').textContent()).includes('No Sutra request observed'));
 await send({snapshot,native:{enabled:true,armed:false,accessProfile:null}});
 assert.equal(await page.locator('#workspace-access').textContent(),'Workspace access · No workspace access');
 await send({snapshot,routing:{active:true}});assert.equal(await page.locator('#activity').textContent(),'Request in progress');assert(!(await page.locator('#model').isDisabled()));
 assert((await page.locator('#flow-nodes').textContent()).includes('large.py: Static graph work-unit allocation exceeded'));
 await send({snapshot,error:'Reader unavailable'});assert((await page.locator('#error').textContent()).includes('last recorded snapshot'));assert.equal(await page.locator('#input').textContent(),'6,284');assert.equal(await page.locator('#cpu-temp').textContent(),'—');assert.equal(await page.locator('#resource').textContent(),'System pressure unavailable');
 await send({snapshot});assert(!(await page.locator('#error').isVisible()));await page.click('#refresh');await page.click('#telemetry');assert.deepEqual((await page.evaluate(()=>sent)).slice(-2),[{type:'refresh'},{type:'telemetry'}]);
 await page.setViewportSize({width:390,height:844});await page.locator('#details').evaluate(e=>e.open=true);assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:path.join(out,'Token-usage-narrow.png'),fullPage:true});
 await page.selectOption('#model','qwen3:4b');const saved=await page.evaluate(()=>window.saved);await page.goto('about:blank');await page.setContent(html(saved));await send({snapshot,activity});assert.equal(await page.locator('#reasoning-calls details').count(),2);assert((await page.locator('#reasoning-calls').textContent()).includes('Unverified child reasoning'));assert.equal(await page.locator('#model').inputValue(),'qwen3:4b');assert.equal(await page.locator('#calls').textContent(),'3');
 await send({});assert.equal(await page.locator('#input').textContent(),'—');assert(!(await page.locator('#visuals').isVisible()));assert.equal(await page.locator('#input-note').textContent(),'Waiting for measurements');
 await send({snapshot, native:{armed:false,compaction:{status:'checkpoint_preserved',time:new Date().toISOString()}},routing:{model:'small',stage:'conversation',memoryUsed:true,memoryAllocation:{context_limit:32768,response_reserve:4096,summary_max_chars:2048},memoryStatus:{stored_turns:12,omitted_turns:5}}});assert((await page.locator('#compaction').textContent()).includes('signed checkpoint retained'));assert((await page.locator('#native-request').textContent()).includes('Smriti memory restored'));assert((await page.locator('#context-packet').textContent()).includes('32,768 context tokens'));assert((await page.locator('#context-packet').textContent()).includes('12 saved turns · 5 outside this working set'));
 await send({snapshot,native:{compaction:{status:'missing_checkpoint',time:new Date().toISOString()}}});assert((await page.locator('#compaction').textContent()).includes('no authenticated checkpoint'));
 await send({snapshot,native:{pending:{state:'PAUSED',stop:{status:'context_budget',error:'41,403 budget units remain; required review context cannot fit.'}}}});
 assert((await page.locator('#recovery-status').textContent()).includes('41,403 budget units remain'));
 assert(await page.locator('#increase-allocation').isVisible());
 await page.locator('#increase-allocation').click();
 assert.deepEqual((await page.evaluate(()=>sent)).at(-1),{type:'increase_allocation'});
 await send({snapshot:{...snapshot,monitor:{...monitor,review_budget:{...frame('ALLOW'),used:{tokens:228975,calls:12,operations:457,seconds:950,source_bytes:65540928},limits:{tokens:478975,calls:52,operations:1457,seconds:2751,source_bytes:132649792},allocation_version:1}}},native:{pending:{state:'PAUSED',stop:{status:'context_budget'}},working:true}});
 assert(await page.locator('#increase-allocation').isDisabled());
 assert((await page.locator('#task-allocation').textContent()).includes('228,975 / 478,975'));
 await page.screenshot({path:path.join(out,'Allocation-recovery.png'),fullPage:true});
 await send({snapshot,native:{pending:{state:'PAUSED',stop:{status:'resource_wait'}}}});
 assert(!(await page.locator('#increase-allocation').isVisible()));
 await send({snapshot,native:{pending:{state:'PAUSED',stop:{status:'context_budget',error:'The packet does not fit.',automatic_allocation:{status:'model_context',reason:'The selected model has reached its context limit.'}}}}});
 assert((await page.locator('#recovery-status').textContent()).includes('selected model has reached its context limit'));
 assert(!(await page.locator('#increase-allocation').isVisible()));

 for(const code of ['BRIDGE','FIN','COMPOUND_FIN']){await send({snapshot:{...snapshot,monitor:{...monitor,hardware:{...monitor.hardware,decision:code,admitted:false}}}});assert.equal(await page.locator('#resource').textContent(),'System pressure: '+code);}
 await send({snapshot:{...snapshot,monitor:{...monitor,hardware:{...monitor.hardware,available:false,reason:'No collector'}}}});assert.equal(await page.locator('#cpu-temp').textContent(),'—');assert.equal(await page.locator('#cpu-load').textContent(),'—');
 await send({snapshot:{...snapshot,monitor:{...monitor,hardware:{...monitor.hardware,sample:{cpu_busy_pct:0},temperatures:{age_seconds:0,cpu:{available:false,reason:'No CPU sensor'},gpu:{available:false,reason:'No GPU sensor'}}}}}});assert.equal(await page.locator('#cpu-temp').textContent(),'—');assert.equal(await page.locator('#cpu-load').textContent(),'0%');
 await send({snapshot:{...snapshot,updatedAt:new Date(Date.now()-10000).toISOString()},aged:true});assert.equal(await page.locator('#cpu-temp').textContent(),'—');assert.equal(await page.locator('#resource').textContent(),'System pressure unavailable');assert((await page.locator('#gates').textContent()).includes('Stale · ALLOW'));assert.equal(await page.locator('#input').textContent(),'2,284');
 await send({snapshot});await page.evaluate(()=>{window.realNow=Date.now;Date.now=()=>window.realNow()+10000;});await page.waitForTimeout(1100);assert.equal(await page.locator('#cpu-temp').textContent(),'—');await page.evaluate(()=>Date.now=window.realNow);
 assert.deepEqual(errors,[]);fs.writeFileSync(path.join(out,'Usage-browser-validation.json'),JSON.stringify({passed:true,fixture_data:true,checks:['totals include repair/withheld calls and distinguish missing counts from real zero','bounded history is disclosed','model, stage, outcome and time filters combine and reset','repeated snapshots do not duplicate totals; selections survive updates and view recreation','empty workspace and no matches have distinct guidance','stale snapshots are labelled and recovery clears errors','active inference does not disable filtering','model text cannot inject HTML','refresh and telemetry buttons emit read-only navigation messages','1140px and 390px views fit without horizontal page overflow','workspace reset clears previous totals','live hardware is independent of token filters','all four kernel decisions remain distinct','missing/expired/disconnected readings clear temperatures and resource permission','stalled host messages expire via heartbeat','checkpoint-preserved and missing-checkpoint states are distinct','no browser script errors']},null,2));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
