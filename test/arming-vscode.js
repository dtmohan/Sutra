'use strict';
const vscode=require('vscode'),fs=require('node:fs');
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
exports.run=async()=>{
 const file=process.env.SUTRA_ARM_RESULT,report={passed:false,checks:[]};
 try{
  const extension=vscode.extensions.getExtension('deepak-local.karma-local-harness');
  await extension.activate();
  for(let i=0;i<50;i++){if((await vscode.lm.selectChatModels({vendor:'karma'})).length)break;await sleep(100);}
  fs.writeFileSync(file+'.ready','ready');
  await vscode.commands.executeCommand('karma.armNative',{goal:'must-not-be-used',roots:['/']});
  const status=extension.exports.getRoutingStatus();
  if(!status.armed)throw Error('Arming did not finish: '+status.reason);
  report.checks.push('One user confirmation arms Sutra in actual VS Code');
  if(status.session)throw Error('Arming created a workspace task before a request');
  report.checks.push('Arming makes no model call and creates no task');
  await vscode.commands.executeCommand('karma.disarmNative');
  if(extension.exports.getRoutingStatus().armed)throw Error('Disarm did not clear state');
  report.checks.push('Disarm switches workspace access off');report.passed=true;
 }catch(e){report.error=e.stack;}
 finally{fs.writeFileSync(file,JSON.stringify(report,null,2));await sleep(1000);await vscode.commands.executeCommand('workbench.action.quit');}
};
