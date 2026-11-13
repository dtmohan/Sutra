// Actual extension host + Ollama; isolated profile only. UI driver approves one dialog.
'use strict';
const vscode=require('vscode'),fs=require('node:fs'),assert=require('node:assert/strict');
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
exports.run=async()=>{
 const file=process.env.SUTRA_ARM_RESULT,report={passed:false,checks:[],responses:[]};
 const save=phase=>{report.phase=phase;fs.writeFileSync(file,JSON.stringify(report,null,2));};
 const token=new vscode.CancellationTokenSource();
 try{
  const ext=vscode.extensions.getExtension('deepak-local.karma-local-harness'),api=await ext.activate();
  let models=[];for(let i=0;i<50;i++){models=await vscode.lm.selectChatModels({vendor:'karma'});if(models.length)break;await sleep(100);}
  await vscode.commands.executeCommand('karma.useNativeChat');save('chat_open');
  const deadline=Date.now()+60000;while(!fs.existsSync(file+'.draft')){if(Date.now()>deadline)throw Error('UI did not prepare the existing chat draft');await sleep(100);}
  let history=[];
  async function send(tier,prompt){
   const model=models.find(m=>m.name==='Sutra '+tier);assert(model);
   history.push(vscode.LanguageModelChatMessage.User(prompt));
   const response=await model.sendRequest(history,{justification:'Validate in-place Sutra arming and retained context in this isolated test workspace.'},token.token);
   const parts=[];for await(const part of response.stream)parts.push(part);
   const text=parts.map(p=>p.value||'').join('');report.responses.push({tier,prompt,text});save('response_'+report.responses.length);
   history.push(vscode.LanguageModelChatMessage.Assistant(parts));return text;
  }
  const hello=await send('Low','Remember: this project is Cedar, reference CED-724. Keep reviews read-only, with no network or new dependencies. Just acknowledge; do not inspect source yet.');
  assert(hello.includes('conversation details'));assert(!api.getRoutingStatus().armed);
  const review=await send('Mid','Now inspect the current workspace source. The earlier instruction to defer inspection applied only to the acknowledgement turn and is no longer applicable. Read entry.py and leaf.py to review their architecture and start-to-run relationship. No edits, network access, or new dependencies.');
  assert(review.startsWith('Continuing this request through Sutra’s workspace workflow.'),review);assert(review.includes('Sutra routing confirmed'),review);assert(!review.includes('withheld'),review);
  const workflow=api.getRoutingStatus().routing.orchestration;assert.equal(workflow.state,'DONE');assert(workflow.children.length>=2);assert(workflow.children.every(c=>c.state==='DONE'));report.workflow=workflow;report.checks.push('Native review uses persisted child reviews and joins them in the same request');
  const before=await api.getState();assert(api.getRoutingStatus().armed);assert(before.contract);
  assert.equal(before.contract.contract.constraints.edits_allowed,false);assert.equal(before.contract.contract.constraints.tests_allowed,false);
  report.checks.push('One real host confirmation resumes the original native request and produces an admitted source review');
  const switched=await send('High','Read entry.py and leaf.py again from the current workspace and continue the architecture review. Explain the observed call relationship and its limits. Preserve the read-only, no-network and no-new-dependency restrictions.');
  assert(switched.startsWith('Continuing this request through Sutra’s workspace workflow.'),switched);assert(switched.includes('Sutra routing confirmed'),switched);assert(switched.includes('Memory restored'),switched);
  const after=await api.getState();assert.equal(after.session,before.session);assert.deepEqual(after.contract,before.contract);assert.deepEqual(after.calibration.binding,before.calibration.binding);
  report.binding={session:after.session,contract:after.contract.contract_hash,calibration:after.calibration.binding.hash};
  report.checks.push('qwen3.6:35b to local-assistant:latest retains conversation receipts, task, calibration and read-only constraints');
  await vscode.commands.executeCommand('karma.disarmNative');assert(!api.getRoutingStatus().armed);
  report.passed=true;save('complete');
 }catch(error){report.error=error.stack;save('failed');}
 finally{token.dispose();await sleep(2000);await vscode.commands.executeCommand('workbench.action.quit');}
};
