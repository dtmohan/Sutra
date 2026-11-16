'use strict';
const vscode=require('vscode'),fs=require('node:fs'),assert=require('node:assert/strict');
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
exports.run=async()=>{
 const file=process.env.SUTRA_ARM_RESULT,report={passed:false,checks:[]};
 try{
  const extension=vscode.extensions.getExtension('deepak-local.karma-local-harness');
  await extension.activate();
  for(let i=0;i<50;i++){if((await vscode.lm.selectChatModels({vendor:'karma'})).length)break;await sleep(100);}
  fs.writeFileSync(file+'.phase','decline');
  await vscode.commands.executeCommand('karma.armNative');
  assert.equal(extension.exports.getRoutingStatus().armed,false);
  report.checks.push('Cancel in the real VS Code dialog leaves workspace access off');
  fs.writeFileSync(file+'.phase','approve');
  await vscode.commands.executeCommand('karma.armNative',{goal:'must-not-be-used',roots:['/']});
  const status=extension.exports.getRoutingStatus();
  assert(status.armed,status.reason);
  assert.equal(status.accessProfile,'read_only');
  assert.equal(status.session,undefined);
  assert.equal(status.routing,null);
  report.checks.push('Read only grants workspace access without starting a task or routing a request');
  fs.writeFileSync(file+'.phase','keep');
  await vscode.commands.executeCommand('karma.armNative');
  assert.equal(extension.exports.getRoutingStatus().accessProfile,'read_only');
  report.checks.push('Cancelling a profile change retains the saved choice');
  await vscode.commands.executeCommand('karma.disarmNative');
  assert.equal(extension.exports.getRoutingStatus().armed,false);
  report.checks.push('Explicit revocation switches workspace access off');report.passed=true;
 }catch(e){report.error=e.stack;}
 finally{fs.writeFileSync(file,JSON.stringify(report,null,2));await sleep(1000);await vscode.commands.executeCommand('workbench.action.quit');}
};
