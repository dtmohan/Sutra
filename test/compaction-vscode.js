const vscode=require('vscode'),fs=require('node:fs'),assert=require('node:assert/strict');
exports.run=async()=>{
 const file=process.env.KARMA_COMPACTION_OUTPUT,prior=fs.existsSync(file)?JSON.parse(fs.readFileSync(file)):{};
 const report=prior.phase==='reload_pending'?prior:{checks:[]};
 const phase=name=>{report.phase=name;fs.writeFileSync(file,JSON.stringify(report,null,2));};
 try{
  const ext=vscode.extensions.getExtension('deepak-local.karma-local-harness'),api=await ext.activate();assert.equal(api.version,'0.15.1');
  async function routeAfter(previous){
   const start=Date.now();while(Date.now()-start<210000){
    try{const native=api.getRoutingStatus(),r=native.routing;if(r&&!r.active&&r.invocationId&&r.invocationId!==previous){assert.equal(r.stage,'conversation',JSON.stringify(r));assert(!native.armed);return r;}}catch(e){if(!/request is running/.test(e.message))throw e;}
    await new Promise(r=>setTimeout(r,300));
   }throw Error('Native chat response timed out');
  }
  async function send(prompt,tier,previous){
   await vscode.commands.executeCommand('workbench.panel.chat.view.copilot.focus');
   await vscode.commands.executeCommand('workbench.action.chat.open',{query:prompt,modelSelector:{vendor:'karma',id:'karma-'+tier},toolsInclude:['karma_native_action']});
   return routeAfter(previous);
  }
  async function details(id){return (await vscode.workspace.openTextDocument(vscode.Uri.parse('karma-preview:/conversation/'+id+'/Response.md'))).getText();}
  async function compact(expected){
   await vscode.commands.executeCommand('workbench.panel.chat.view.copilot.focus');
   await vscode.commands.executeCommand('github.copilot.chat.compact');
   const start=Date.now();while(Date.now()-start<90000){const state=api.getRoutingStatus();if(state.compaction?.invocationId===expected){assert.equal(state.compaction.status,'checkpoint_preserved');return state.compaction;}await new Promise(r=>setTimeout(r,300));}
   throw Error('VS Code compact command did not preserve the checkpoint');
  }
  if(prior.phase!=='reload_pending'){
   phase('opening');await vscode.commands.executeCommand('karma.useNativeChat');
   report.firstPrompt='Please draw a diagram of my listening chain, without inventing device specs. Apple TV -> Dune Cinema Duo 8k -> optical -> WiiM Ultra -> LAN -> WiiM Ultra -> USB -> RME ADI-2 DAC -> Musmys C2860 class A preamp -> Brzhifi Class A 20 W amplifier -> Blumenstein Teton speakers. The Dune also feeds the Valerion Pro Max projector. The WiiM USB link is OUTPUT to the RME, not USB input.';
   phase('first_diagram');report.firstRoute=await send(report.firstPrompt,'high');report.firstDetails=await details(report.firstRoute.invocationId);
   assert(!report.firstRoute.memoryUsed);assert(report.firstDetails.includes('```mermaid'));assert(/Valerion/.test(report.firstDetails));
   phase('compacting');report.firstCompaction=await compact(report.firstRoute.invocationId);
   report.checks.push('real native chat generates the listening diagram; real VS Code compact command preserves its authenticated receipt');
   phase('reload_pending');await vscode.commands.executeCommand('workbench.action.reloadWindow');return;
  }
  phase('opinion_after_reload');
  report.opinionPrompt='What is your opinion about the chain? You can search the web as needed. Report back if you cannot access network. Do not invent product specifications.';
  report.secondRoute=await send(report.opinionPrompt,'mid',report.firstRoute.invocationId);report.secondDetails=await details(report.secondRoute.invocationId);
  assert(report.secondRoute.memoryUsed,JSON.stringify(report.secondRoute));assert.equal(report.secondRoute.model,'qwen3.6:35b');assert(/WiiM|RME|Dune/i.test(report.secondDetails));assert(!/blockchain/i.test(report.secondDetails));
  report.checks.push('after real compaction and window reload, switching to the MoE model retains the audio topic without replaying history');
  phase('second_compaction');report.secondCompaction=await compact(report.secondRoute.invocationId);
  phase('redraw');report.thirdRoute=await send('Draw that same listening chain again. Keep both WiiM devices separate and retain the projector branch and USB output direction.','high',report.secondRoute.invocationId);report.thirdDetails=await details(report.thirdRoute.invocationId);
  assert(report.thirdRoute.memoryUsed);assert(report.thirdDetails.includes('```mermaid'));assert(/C2860/.test(report.thirdDetails));assert(/Valerion/.test(report.thirdDetails));
  report.checks.push('a second real compaction and model switch preserve the original description for redrawing');
  phase('new_chat');await vscode.commands.executeCommand('karma.useNativeChat');report.newRoute=await send('Hello. What name did I give my audio setup? If I have not provided one here, say so.','low',report.thirdRoute.invocationId);assert(!report.newRoute.memoryUsed);report.newDetails=await details(report.newRoute.invocationId);
  report.checks.push('a separate native chat does not inherit the prior listening-chain memory');
  report.passed=true;phase('complete');await vscode.commands.executeCommand('workbench.action.quit');
 }catch(error){if(report.phase==='reload_pending'&&/Canceled|disposed|closed/i.test(String(error)))return;report.passed=false;report.error=String(error.stack||error);phase('failed');await vscode.commands.executeCommand('workbench.action.quit');throw error;}
};
