'use strict';
// Real VS Code host/dialogs with a deterministic orchestration backend. No model,
// repository read, edit, network request or production authorization is invoked.
const vscode=require('vscode'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const ROOT=process.env.SUTRA_PERMISSION_ROOT,FILE=process.env.SUTRA_PERMISSION_RESULT;
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
let controller,monitor,flow,state,commands=[],requests=[],statuses=[];
const models={low:'fixture-low',mid:'fixture-mid',high:'fixture-high'};
const originalPrompt='Review entry.py and leaf.py. Explain their architecture. Keep the review read only with no network or dependencies.';
const report={passed:false,scope:'Real isolated VS Code model API, native_provider and consent dialogs; deterministic backend. Full Agent preprocessing is excluded; no inference or source access.',checks:[],responses:[]};
function save(phase,extra={}){Object.assign(report,extra,{phase});fs.writeFileSync(FILE,JSON.stringify(report,null,2));}
async function until(test,label){const end=Date.now()+45000;while(!test()){if(Date.now()>end)throw Error(label);await sleep(100);}}
const clone=value=>structuredClone(value);

exports.activate=context=>{
  const host={...vscode,commands:{...vscode.commands,executeCommand(...args){commands.push(args);return vscode.commands.executeCommand(...args);}}};
  const workspace=()=>vscode.workspace.workspaceFolders[0].uri.fsPath;
  state={session:'fixture-session',contract:{contract_hash:'contract-fixture',contract:{goal:originalPrompt,
    constraints:{native_task:true,allowed_paths:['entry.py','leaf.py'],edits_allowed:false,tests_allowed:false,
      network_allowed:false,dependencies_allowed:false}}},calibration:{binding:{hash:'calibration-fixture'}}};
  const backend=async request=>{
    requests.push(clone(request));
    if(request.op==='state')return clone(state);
    if(request.op==='conversation_read')return {result:{invocation_id:request.invocation_id,
      admission:{admitted:true},accepted_output:{summary:'Read-only architecture review requested.'}}};
    if(request.op!=='orchestrate')throw Error('Unexpected fixture operation '+request.op);
    if(request.event==='pending')return clone(flow||{flow_id:null,state:'DONE'});
    if(request.event==='start'){
      assert(!flow,'A retry must retain the original flow');
      const conversation={session:'fixture-conversation',route:{model:models.low},result:{invocation_id:'2'.repeat(32),
        admission:{admitted:true,errors:[]},accepted_output:{intent:'workspace',summary:'Workspace review requested.',answer:'I can review the two source files.'}}};
      flow={flow_id:'1'.repeat(32),cursor:'1',state:'NEED_ARM',prompt:request.prompt,conversation_id:'2'.repeat(32),
        permission:{operation:'read',paths:['entry.py','leaf.py']},children:[],conversation,spent:{calls:1,tokens:120}};
      return clone(flow);
    }
    assert(flow&&request.flow_id===flow.flow_id,'Recovery must address the original flow');
    if(request.event==='inspect')return clone(flow);
    if(request.event==='step'){
      assert(request.armed,'No workspace stage before authorization');
      flow={...flow,cursor:'2',state:'DONE',spent:{calls:2,tokens:270},data:{state,route:{model:models.mid},
        coverage:{mapped_files:2,source_files:2},source_files:['entry.py','leaf.py'],
        result:{invocation_id:'3'.repeat(32),admission:{admitted:true,errors:[]},accepted_output:{
          answer:'The fixture entry module calls the leaf module. This is a deterministic UI test report, not an inferred source review.',remaining:[],action:{kind:'report'}}}}};
      delete flow.conversation;
      return clone(flow);
    }
    throw Error('Unexpected fixture transition '+request.event);
  };
  const authorization=require(path.join(ROOT,'workspace_authorization')).createStore(host,context);
  monitor=require(path.join(ROOT,'usage_view')).install(host,context,{storage:()=>context.globalStorageUri.fsPath,refresh(){}});
  controller=require(path.join(ROOT,'native_provider')).install(host,context,{
    workspace,authorization,isBusy:()=>false,modelSettings:()=>models,configureModels:async()=>models,
    request:backend,publishReview:()=>null,onStatus(status){statuses.push(clone(status));monitor.setStatus(status);},
    onRoute(){},showDiff:async()=>{},presentReview:async()=>{}
  });
  context.subscriptions.push(controller);
  if(process.env.SUTRA_PERMISSION_AUTORUN==='1')setTimeout(()=>exports.run(),300);
  return {status:()=>controller.status()};
};

exports.run=async()=>{
  const initialClipboard=await vscode.env.clipboard.readText();
  try{
    const extension=vscode.extensions.getExtension('deepak-local.karma-local-harness');await extension.activate();
    await vscode.commands.executeCommand('karma.useNativeChat');
    const candidates=await vscode.lm.selectChatModels({vendor:'karma'}),history=[];
    async function send(name,prompt){
      const model=candidates.find(row=>row.name===name);assert(model,'Missing provider model '+name);
      history.push(vscode.LanguageModelChatMessage.User(prompt));
      const response=await model.sendRequest(history,{justification:'Test the isolated Sutra permission response lifecycle.'},new vscode.CancellationTokenSource().token);
      const parts=[];for await(const part of response.stream)parts.push(part);
      const text=parts.map(part=>part.value||'').join('');
      report.responses.push({model:name,prompt,text});history.push(vscode.LanguageModelChatMessage.Assistant(parts));
      assert(!text.includes('the model stopped before finishing'));assert(!text.includes('Sorry, your request failed'));
      return text;
    }
    save('sending_first_provider_request',{prompt:originalPrompt});
    const first=await send('Sutra Low',originalPrompt);
    assert(first.includes('Workspace access needed'));
    await until(()=>controller.status().permissionPending&&!controller.status().routing?.active,'First request did not settle at the permission boundary');
    save('dismiss_permission',{status:controller.status()});
    const declined=vscode.commands.executeCommand('karma.armNative');
    await until(()=>fs.existsSync(FILE+'.dismissed'),'UI did not dismiss the first permission dialog');
    assert.equal(await declined,false);
    await until(()=>!controller.status().arming,'Dismissal did not settle');
    assert(!controller.status().armed);assert.equal(flow.spent.calls,1);assert(!requests.some(row=>row.event==='step'));
    assert(statuses.filter(s=>s.arming).every(s=>!s.routing?.active),'Permission dialog was opened while a model request was active');
    report.checks.push('Permission request ends normally before the host command opens the real modal; dismissal reads no source and preserves the saved flow');
    const beforeResume=commands.length;save('copy_resume');
    await vscode.commands.executeCommand('karma.resumeWorkflow');
    assert(!commands.slice(beforeResume).some(([name])=>/chat\.(?:open|newLocalChat)|copilot.*focus|sign.?in/i.test(name)),'Resume opened or focused another chat');
    const instruction=await vscode.env.clipboard.readText();assert.match(instruction,/^Resume saved Sutra request \[[a-f0-9]{32}\]$/);
    report.checks.push('Resume copies a bound instruction without opening another chat or initiating sign-in');
    save('approve_permission');
    await vscode.commands.executeCommand('karma.armNative');
    assert.equal(controller.status().accessProfile,'read_only');assert.equal(flow.spent.calls,1);
    save('resuming_same_provider_history',{instruction});
    const resumed=await send('Sutra Mid',instruction);
    assert(resumed.includes('deterministic UI test report'));
    await until(()=>flow.state==='DONE'&&controller.status().routing?.stage==='admitted','Original saved review did not finish after approval');
    assert.equal(requests.filter(row=>row.event==='start').length,1);
    assert.equal(requests.filter(row=>row.event==='step').length,1);
    assert.equal(flow.flow_id,'1'.repeat(32));assert.equal(flow.prompt,originalPrompt);assert.equal(flow.spent.calls,2);
    assert.equal(state.contract.contract.constraints.edits_allowed,false);assert.equal(state.contract.contract.constraints.network_allowed,false);
    assert(!requests.some(row=>['native_execute','repository_tool'].includes(row.op)));
    report.checks.push('The host command opens a real permission dialog; approval resumes the same provider history on another tier with retained constraints and cumulative fixture spending');
    const document=await vscode.workspace.openTextDocument({content:report.responses.map(row=>'# '+row.model+'\n\n'+row.prompt+'\n\n'+row.text).join('\n\n---\n\n'),language:'markdown'});
    await vscode.window.showTextDocument(document,{preview:false});
    save('host_checks_complete',{flow,commands,requests});
    await until(()=>fs.existsSync(FILE+'.checked'),'UI did not verify the final transcript');
    report.passed=true;save('complete');
  }catch(error){save('failed',{error:error.stack,commands,requests,status:controller?.status()});}
  finally{
    if(report.instruction&&await vscode.env.clipboard.readText()===report.instruction)
      await vscode.env.clipboard.writeText(initialClipboard);
    await sleep(1200);await vscode.commands.executeCommand('workbench.action.quit');
  }
};
