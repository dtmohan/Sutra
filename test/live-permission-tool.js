'use strict';
// Real isolated Chat Agent, native_provider and native tool confirmation.
// Backend is deterministic; fixture files and production data are never read.
const vscode=require('vscode'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const ROOT=process.env.SUTRA_PERMISSION_ROOT,FILE=process.env.SUTRA_PERMISSION_RESULT;
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
let controller,authorization,flow,commands=[],requests=[],statuses=[],fixtureTier='Sutra Auto';
const models={low:'fixture-low',mid:'fixture-mid',high:'fixture-high'};
const originalPrompt='Review entry.py and leaf.py. Explain their architecture. Keep the review read only with no network or dependencies.';
const report={passed:false,scope:'Real isolated VS Code native Chat Agent, native_provider and native permission-tool confirmation; deterministic backend, no inference/source access.',checks:[]};
if(process.env.SUTRA_PERMISSION_MODE==='participant')report.scope='Real isolated VS Code fixture Chat participant, native_provider and native tool confirmation; deterministic adapter dispatches the provider/tool loop, no inference/source access. Full Chat Agent preprocessing excluded.';
function save(phase,extra={}){Object.assign(report,extra,{phase});fs.writeFileSync(FILE+'.tmp',JSON.stringify(report,null,2));fs.renameSync(FILE+'.tmp',FILE);}
const clone=value=>structuredClone(value);
async function until(test,label,ms=45000){const end=Date.now()+ms;while(!test()){if(Date.now()>end)throw Error(label);await sleep(100);}}
exports.activate=context=>{
 const host={...vscode,commands:{...vscode.commands,executeCommand(...args){commands.push(args);return vscode.commands.executeCommand(...args);}}};
 const workspace=()=>vscode.workspace.workspaceFolders[0].uri.fsPath;
 const state={session:'fixture-session',contract:{contract_hash:'contract-fixture',contract:{goal:originalPrompt,
  constraints:{native_task:true,allowed_paths:['entry.py','leaf.py'],edits_allowed:false,tests_allowed:false,
   network_allowed:false,dependencies_allowed:false}}},calibration:{binding:{hash:'calibration-fixture'}}};
 const backend=async request=>{
  requests.push(clone(request));save(report.phase||'installing',{requests,flow,status:controller?.status()});
  if(request.op==='state')return clone(state);
  if(request.op==='conversation_read')return {result:{invocation_id:request.invocation_id,admission:{admitted:true},accepted_output:{summary:'Read-only architecture review requested.'}}};
  if(request.op!=='orchestrate')throw Error('Unexpected fixture operation '+request.op);
  if(request.event==='pending')return clone(flow||{flow_id:null,state:'DONE'});
  if(request.event==='start'){
   assert(!flow||flow.state==='DONE','Retry must retain the original flow');
   const n=requests.filter(r=>r.event==='start').length;
   const conversation={session:'fixture-conversation',route:{model:models[request.tier]||models.low},result:{invocation_id:String(n).padStart(32,'0'),admission:{admitted:true,errors:[]},accepted_output:{intent:'workspace',summary:'Workspace review requested.',answer:'I can review the two source files.'}}};
   flow={flow_id:String(n).padStart(32,'a'),cursor:'1',state:'NEED_ARM',prompt:request.prompt,conversation_id:conversation.result.invocation_id,permission:{operation:'read',paths:['entry.py','leaf.py']},children:[],conversation,spent:{calls:1,tokens:120}};
   return clone(flow);
  }
  assert(flow&&request.flow_id===flow.flow_id,'Recovery must address the original flow');
  if(request.event==='inspect')return clone(flow);
  if(request.event==='step'){
   assert(request.armed,'No workspace stage before authorization');assert.equal(controller.status().accessProfile,'read_only');
   flow={...flow,cursor:'2',state:'DONE',spent:{calls:2,tokens:270},data:{state,route:{model:models[request.tier]||models.mid},coverage:{mapped_files:2,source_files:2},source_files:['entry.py','leaf.py'],result:{invocation_id:'3'.repeat(32),admission:{admitted:true,errors:[]},accepted_output:{answer:'The fixture entry module calls the leaf module. This is a deterministic UI test report, not an inferred source review.',remaining:[],action:{kind:'report'}}}}};
   delete flow.conversation;return clone(flow);
  }
  throw Error('Unexpected fixture transition '+request.event);
 };
 authorization=require(path.join(ROOT,'workspace_authorization')).createStore(host,context);
 controller=require(path.join(ROOT,'native_provider')).install(host,context,{workspace,authorization,isBusy:()=>false,modelSettings:()=>models,configureModels:async()=>models,request:backend,publishReview:()=>null,
  onStatus(status){statuses.push(clone(status));save(report.phase||'installing',{status,requests,flow});},onRoute(){},showDiff:async()=>{},presentReview:async()=>{}});
 context.subscriptions.push(controller);
 if(process.env.SUTRA_PERMISSION_MODE==='participant'){
  const history=[];
  context.subscriptions.push(vscode.chat.createChatParticipant('sutra.permissionFixture',async(request,_context,response,token)=>{
   history.push(vscode.LanguageModelChatMessage.User(request.prompt));
   const model=(await vscode.lm.selectChatModels({vendor:'karma'})).find(m=>m.name===fixtureTier);
   assert(model,'Missing fixture tier '+fixtureTier);
   for(let round=0;round<4;round++){
    const answer=await model.sendRequest(history,{justification:'Validate native consent in this isolated fixture.',tools:vscode.lm.tools.filter(t=>['karma_native_action','karma_workspace_access','karma_workspace_agent_access'].includes(t.name))},token);
    const parts=[],calls=[];for await(const p of answer.stream){parts.push(p);if(p instanceof vscode.LanguageModelToolCallPart)calls.push(p);else if(p instanceof vscode.LanguageModelTextPart)response.markdown(p.value);}
    history.push(vscode.LanguageModelChatMessage.Assistant(parts));
    if(!calls.length)return;
    for(const call of calls){
     let content;
     try{content=(await vscode.lm.invokeTool(call.name,{input:call.input,toolInvocationToken:request.toolInvocationToken},token)).content;}
     catch(error){report.toolDenial=String(error);content=[new vscode.LanguageModelTextPart('Native host did not approve this tool: '+String(error))];}
     history.push(vscode.LanguageModelChatMessage.User([new vscode.LanguageModelToolResultPart(call.callId,content)]));
    }
   }
   throw Error('Fixture provider exceeded bounded tool-loop rounds');
  }));
 }
 if(process.env.SUTRA_PERMISSION_AUTORUN==='1')setTimeout(()=>exports.run(),500);
 return {status:()=>controller.status()};
};
exports.run=async()=>{
 try{
  await vscode.extensions.getExtension('deepak-local.karma-local-harness').activate();
  await vscode.commands.executeCommand('karma.useNativeChat');save('agent_submitting',{prompt:originalPrompt});
  const submit=async(query,tier='Sutra Auto')=>{fixtureTier=tier;return vscode.commands.executeCommand('workbench.action.chat.open',{query:(process.env.SUTRA_PERMISSION_MODE==='participant'?'@sutraPermissionFixture ':'')+query,mode:'agent',isPartialQuery:false,modelSelector:{vendor:'karma',id:tier==='Sutra Mid'?'karma-mid':'karma-auto',family:'karma'},toolsInclude:['karma_native_action','karma_workspace_access','karma_workspace_agent_access']});};
  await submit(originalPrompt);
  save('agent_waiting_for_permission');
  const end=Date.now()+45000;while(!controller.status().permissionPending&&Date.now()<end)await sleep(100);
  if(!controller.status().permissionPending){save('host_blocked',{reason:'Native Chat Agent did not reach Sutra permission boundary within 45s.',commands,requests,status:controller.status()});return;}
  assert.equal(requests.filter(r=>r.event==='start').length,1);assert(!requests.some(r=>r.event==='step'));
  save('deny_native_permission',{flow,requests,status:controller.status()});
  await until(()=>fs.existsSync(FILE+'.denied'),'UI did not decline native permission');
  await until(()=>controller.status().routing?.stage==='permission_declined'&&!controller.status().routing?.active,'Native denial did not settle normally');
  assert(!controller.status().armed);assert.equal(flow.state,'NEED_ARM');assert.equal(flow.spent.calls,1);assert(!requests.some(r=>r.event==='step'));
  report.checks.push('Native denial leaves the saved workflow and consumed classification spending intact without workspace access or a model-interruption error');
  const deniedId=flow.flow_id;save('retry_after_denial');await submit('continue');
  await until(()=>controller.status().permissionPending&&!controller.status().routing?.active,'Saved request did not offer fresh native consent');
  assert.equal(flow.flow_id,deniedId);assert.equal(requests.filter(r=>r.event==='start').length,1);
  save('approve_native_permission',{flow,requests,status:controller.status()});
  await until(()=>flow.state==='DONE','Native tool approval did not auto-continue the original saved request');
  await until(()=>!controller.status().routing?.active,'Native tool continuation did not settle');
  assert.equal(flow.prompt,originalPrompt);assert.equal(flow.spent.calls,2);assert.equal(requests.filter(r=>r.event==='start').length,1);assert.equal(requests.filter(r=>r.event==='step').length,1);
  assert.equal(controller.status().accessProfile,'read_only');assert(!commands.some(c=>/resumeWorkflow/.test(c[0])));
  report.checks.push('Native tool approval automatically continues the original saved flow without a manual continue or copied instruction');
  save('persisted_second_request');
  await submit('Explain the same fixture architecture again on Mid, still read only.','Sutra Mid');
  await until(()=>requests.filter(r=>r.event==='start').length===2&&flow.state==='DONE','Saved read-only grant was not reused');
  assert.equal(controller.status().accessProfile,'read_only');report.checks.push('Persisted read-only workspace access is reused on another tier without a second native consent');
  await until(()=>!controller.status().routing?.active,'Mid request did not settle');
  await vscode.commands.executeCommand('karma.disarmNative');assert(!controller.status().armed);assert.equal(authorization.read().revoked,true);
  save('revocation_decline');await submit('Review the same architecture after explicit workspace revocation.');
  await until(()=>fs.existsSync(FILE+'.regrant-declined'),'Cached native session approval bypassed explicit revocation instead of asking again');
  await until(()=>controller.status().routing?.stage==='permission_declined'&&!controller.status().routing?.active,'Declined regrant did not settle');
  assert(!controller.status().armed);assert.equal(authorization.read().revoked,true);assert.equal(flow.state,'NEED_ARM');assert.equal(flow.spent.calls,1);assert.equal(requests.filter(r=>r.event==='step').length,2);
  const revokedFlowId=flow.flow_id;save('revocation_approve');await submit('continue');
  await until(()=>flow.state==='DONE'&&!controller.status().routing?.active,'Explicit regrant did not resume the saved revoked task');
  assert.equal(flow.flow_id,revokedFlowId);assert.equal(flow.spent.calls,2);assert.equal(requests.filter(r=>r.event==='start').length,3);assert.equal(requests.filter(r=>r.event==='step').length,3);assert.equal(controller.status().accessProfile,'read_only');
  report.checks.push('Cached native Allow-in-Session cannot undo explicit revocation: a real regrant modal is required; decline preserves revoked access and spending, approval resumes the same saved task');
  save('host_checks_complete',{flow,requests,status:controller.status(),commands});
  await until(()=>fs.existsSync(FILE+'.checked'),'UI did not verify transcript');report.passed=true;save('complete');
 }catch(error){save('failed',{error:error.stack,commands,requests,status:controller?.status()});}
 finally{await sleep(1500);await vscode.commands.executeCommand('workbench.action.quit');}
};
