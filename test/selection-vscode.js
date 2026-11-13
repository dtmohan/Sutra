const vscode=require('vscode'),fs=require('node:fs'),assert=require('node:assert/strict');
exports.run=async()=>{
 const out=process.env.KARMA_SELECTION_OUTPUT;const prior=fs.existsSync(out)?JSON.parse(fs.readFileSync(out)):{};const report=['reload_pending','restart_pending'].includes(prior.phase)?prior:{checks:[]};
 const save=phase=>{report.phase=phase;fs.writeFileSync(out,JSON.stringify(report,null,2));};
 const wait=async tag=>{const file=out+'.'+tag,start=Date.now();while(!fs.existsSync(file)){if(Date.now()-start>90000)throw Error('UI check timeout: '+tag);await new Promise(r=>setTimeout(r,150));}};
 try{
  const ext=vscode.extensions.getExtension('deepak-local.karma-local-harness'),api=await ext.activate();assert.equal(api.version,require('../package.json').version);
  const models=await vscode.lm.selectChatModels({vendor:'karma'});assert.deepEqual(models.map(m=>m.id).sort(),['karma-auto','karma-high','karma-low','karma-mid']);
  const chat=vscode.workspace.getConfiguration('chat');
  const verifyDefaults=()=>{const cfg=vscode.workspace.getConfiguration('chat');assert.equal(cfg.get('defaultModel'),'karma-auto');assert.equal(cfg.get('editor.preferCopilotHarness'),false);assert.equal(cfg.get('defaultToCopilotHarness'),false);assert.equal(cfg.get('editor.localAgent.enabled'),true);};
  if(prior.phase==='restart_pending'){
   verifyDefaults();await vscode.commands.executeCommand('workbench.action.chat.newChat');await vscode.commands.executeCommand('workbench.action.chat.open');assert(!(await api.getState()).native.armed);save('after_restart');await wait('restart_checked');report.checks.push('full VS Code process restart retains Local/Sutra defaults and ordinary New Chat selection');report.passed=true;save('complete');await vscode.commands.executeCommand('workbench.action.quit');return;
  }
  if(prior.phase==='reload_pending'){
   verifyDefaults();assert(!(await api.getState()).native.armed);await vscode.commands.executeCommand('workbench.action.chat.open');save('after_reload');await wait('reload_checked');report.checks.push('default and visible prompt selection persist after window reload; arming remains off');
   const current=await api.getState();assert.equal(current.native.routing,null);assert.equal(current.native.label,'Sutra: chat ready · workspace unarmed');
   report.checks.push('reload leaves conversation ready and workspace tools unarmed, without making a model call');
   save('restart_pending');await vscode.commands.executeCommand('workbench.action.quit');return;
  }
  await vscode.lm.selectChatModels({vendor:'ollama-models'});assert.equal(chat.get('defaultModel'),'local-assistant:latest');report.before=chat.get('defaultModel');
  await vscode.commands.executeCommand('karma.useNativeChat',{model:'local-assistant:latest'});verifyDefaults();assert(!(await api.getState()).native.armed);report.after=vscode.workspace.getConfiguration('chat').get('defaultModel');report.checks.push('explicit command selects unique Sutra default without inference, task creation or arming');save('selected');await wait('selected_checked');
  await vscode.commands.executeCommand('workbench.action.chat.newChat');await vscode.commands.executeCommand('workbench.action.chat.open');save('new_chat');await wait('new_checked');report.checks.push('ordinary New Chat opens Local with Sutra Auto instead of Copilot and direct Ollama');save('reload_pending');await vscode.commands.executeCommand('workbench.action.reloadWindow');
 }catch(e){if(['reload_pending','restart_pending'].includes(report.phase)&&/Canceled|disposed|closed/i.test(String(e)))return;report.passed=false;report.error=String(e.stack||e);save('failed');await vscode.commands.executeCommand('workbench.action.quit');throw e;}
};
