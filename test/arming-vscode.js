'use strict';
const vscode=require('vscode'),fs=require('node:fs');
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
exports.run=async()=>{
 const file=process.env.SUTRA_ARM_RESULT,report={passed:false,checks:[]};
 try{
  const extension=vscode.extensions.getExtension('deepak-local.karma-local-harness');
  await extension.activate();
  for(let i=0;i<50;i++){if((await vscode.lm.selectChatModels({vendor:'karma'})).length)break;await sleep(100);}
  fs.writeFileSync(file+'.phase','decline');
  await vscode.commands.executeCommand('karma.armNative');
  if(extension.exports.getRoutingStatus().armed)throw Error('Cancelled confirmation armed the workspace');
  report.checks.push('Cancel in the real VS Code dialog leaves workspace access unarmed');
  fs.writeFileSync(file+'.phase','approve');
  await vscode.commands.executeCommand('karma.armNative',{goal:'must-not-be-used',roots:['/']});
  const status=extension.exports.getRoutingStatus();
  if(!status.armed)throw Error('Arming did not finish: '+status.reason);
  report.checks.push('One user confirmation arms Sutra in actual VS Code');
  if(status.session)throw Error('Arming created a workspace task before a request');
  report.checks.push('Arming makes no model call and creates no task');
  fs.writeFileSync(file+'.phase','reuse');
  await vscode.commands.executeCommand('karma.armNative');
  if(!extension.exports.getRoutingStatus().armed)throw Error('Existing grant was not retained');
  report.checks.push('An already armed workspace retains the grant without another dialog');
  await vscode.commands.executeCommand('karma.disarmNative');
  if(extension.exports.getRoutingStatus().armed)throw Error('Disarm did not clear state');
  report.checks.push('Disarm switches workspace access off');report.passed=true;
 }catch(e){report.error=e.stack;}
 finally{fs.writeFileSync(file,JSON.stringify(report,null,2));await sleep(1000);await vscode.commands.executeCommand('workbench.action.quit');}
};
