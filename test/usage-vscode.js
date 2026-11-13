// Run as an isolated extension-host test with test/usage-ui.cjs watching the UI.
const vscode=require('vscode'),fs=require('node:fs'),assert=require('node:assert/strict');
exports.run=async()=>{
 const file=process.env.SUTRA_USAGE_RESULT,report={checks:[]};
 const save=phase=>{report.phase=phase;fs.writeFileSync(file,JSON.stringify(report,null,2));};
 const wait=async tag=>{const start=Date.now();while(!fs.existsSync(file+'.'+tag)){if(Date.now()-start>60000)throw Error('UI timeout: '+tag);await new Promise(r=>setTimeout(r,150));}};
 try{
  const ext=vscode.extensions.getExtension('deepak-local.karma-local-harness'),api=await ext.activate();assert.equal(api.version,ext.packageJSON.version);
  await vscode.commands.executeCommand('karma.openUsage');save('opened');await wait('opened');report.checks.push('packaged extension opens the docked usage view with a working local reader');
  await vscode.commands.executeCommand('workbench.action.closePanel');save('hidden');await wait('reopened');report.checks.push('status-bar Sutra Monitor reopens the correct panel without arming or inference');
  assert.equal(api.getRoutingStatus().routing,null);report.passed=true;save('complete');
 }catch(e){report.passed=false;report.error=String(e.stack||e);save('failed');throw e;}
 finally{await vscode.commands.executeCommand('workbench.action.quit');}
};
