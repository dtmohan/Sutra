// Real VS Code chat assembly/compaction with a stubbed inference transport.
// The test launcher injects the transport only in a disposable extracted package.
const vscode=require('vscode'),fs=require('node:fs'),assert=require('node:assert/strict');
exports.run=async()=>{
 const file=process.env.SUTRA_HOST_RESULT,report={fixture_inference:true,checks:[]};
 const phase=value=>{report.phase=value;fs.writeFileSync(file,JSON.stringify(report,null,2));};
 try{
  const api=await vscode.extensions.getExtension('deepak-local.karma-local-harness').activate();
  await vscode.commands.executeCommand('karma.useNativeChat');
  async function send(prompt,previous){
   await vscode.commands.executeCommand('workbench.panel.chat.view.copilot.focus');
   await vscode.commands.executeCommand('workbench.action.chat.open',{query:prompt,modelSelector:{vendor:'karma',id:'karma-auto'},toolsInclude:['karma_native_action']});
   const start=Date.now();while(Date.now()-start<180000){const r=api.getRoutingStatus().routing;if(r&&!r.active&&r.invocationId&&r.invocationId!==previous){assert.equal(r.stage,'conversation');await new Promise(r=>setTimeout(r,700));return r;}await new Promise(r=>setTimeout(r,150));}throw Error('Native request timed out');
  }
  phase('short_turns');let r=await send('Hello Sutra');assert.equal(api.getRoutingStatus().compaction,undefined);
  r=await send('Can you tell me more about yourself?',r.invocationId);assert.equal(api.getRoutingStatus().compaction,undefined);assert(r.memoryUsed);
  r=await send('Thank you, please continue.',r.invocationId);assert.equal(api.getRoutingStatus().compaction,undefined);
  report.checks.push('three real native short turns complete without automatic compaction; pointer memory remains bound');
  phase('large_history');let compact,expected;
  // Grow history using realistic <=6,000-character replies. A single enormous
  // reply can be dropped wholesale by the host, including its receipt footer.
  for(let i=0;i<85&&!compact;i++){expected=r.invocationId;r=await send('Append bounded fixture history.',expected);compact=api.getRoutingStatus().compaction;report.history_turns=i+1;phase('large_history');}
  assert(compact,'Large assembled history must still compact');assert.equal(compact.status,'checkpoint_preserved');assert(r.memoryUsed);
  report.checks.push('oversized host history automatically compacts and preserves the signed checkpoint without model summarization');
  report.compaction=compact;report.fixture=JSON.parse(fs.readFileSync(process.env.SUTRA_HOST_AUDIT));assert(report.fixture.calls.length>=5);
  assert(report.fixture.calls.every(c=>!/^Summarize the conversation/.test(c.prompt)));assert.equal(report.fixture.calls.at(-1).parent,expected);
  report.passed=true;phase('complete');
 }catch(e){report.passed=false;report.error=String(e.stack||e);phase('failed');throw e;}
 finally{await vscode.commands.executeCommand('workbench.action.quit');}
};
